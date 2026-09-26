"""Control gate (brief v2 section 2): one ordered, scenario-aware pipeline per inbound document.

    register -> screen -> read (Gemini) -> confidence -> document type -> supplier (tax ID) -> legal entity ->
    duplicate -> credit note -> commitment -> terms -> tolerances -> approval limit -> post

Every step appends {step, result, detail} to the decision's trace and logs one line "[B-05] Rule → result — reason".
The model reads and classifies; every decision is a rule. Four outcomes: Post · Exception · Block · Human review.
Every as-is / to-be difference is a flag in SCENARIOS that the steps read; there are no scattered scenario checks.
The as-is has no gate: AP keys every document, picks an account by name and chases what does not match by email
(docs/ASSUMPTIONS.md section 8); the steps the as-is does not have are left out of its log.

Deterministic: a decision follows from the extraction JSON, the seed and the documents processed before it
(in order of registration). Durations come from app/sim.py, exception types and SLAs from app/taxonomy.py,
people from app/world.py.
"""
from __future__ import annotations

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Callable, Optional

from rapidfuzz import fuzz
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app import config, extract, seed, sim, taxonomy, world
from app.models import (
    Contract,
    CreditNoteApplication,
    GateDecision,
    InboundDocument,
    LegalEntity,
    Party,
    PendingVendorInvoice,
    ProductReceipt,
    PurchaseOrder,
    Run,
    VendorAccount,
)
from app.normalize import (
    NAME_SIMILARITY_THRESHOLD,
    name_similarity,
    names_match,
    normalise_currency,
    normalise_iban,
    normalise_invoice_number,
    normalise_po_number,
    normalise_vat,
)
from app.seed import LIVE_DATASET, SCENARIO_LETTER

Log = Callable[[str], None]

ON_THE_FLY_CREATOR = "ap.invoice-entry"  # created_by of vendor accounts AP opens to pay an invoice (as-is)
ON_THE_FLY_NOTE = "Created by AP to pay the invoice"
PO_TOLERANCE_PCT = 0.02  # PO amount tolerance: 2% or 50, whichever is larger (brief section 8)
PO_TOLERANCE_ABS = 50.0
DUPLICATE_AMOUNT_TOLERANCE = 0.01  # same invoice: gross within 1%
LINE_MATCH_THRESHOLD = 60  # rapidfuzz token_set_ratio to map an invoice line to a PO line by description
BILL_TO_FUZZY_THRESHOLD = 95  # rapidfuzz ratio on entity names with legal suffixes kept

STEP_NAMES = ("register", "screen", "extract", "confidence", "document_type", "resolve_vendor", "legal_entity",
              "duplicate_check", "credit_note", "commitment_match", "terms", "tolerances", "approval_limit", "post")

# The deck's words for each step (rule log, invoice trace). The as-is has no gate: AP does the work by hand.
STEP_LABELS: dict[str, dict[str, str]] = {
    "tobe": {"register": "Registered on arrival", "screen": "Screened", "extract": "Read with Gemini",
             "confidence": "Confidence", "document_type": "Document type", "resolve_vendor": "Supplier (tax ID)",
             "legal_entity": "Legal entity", "duplicate_check": "Duplicate", "credit_note": "Credit note",
             "commitment_match": "Commitment", "terms": "Terms", "tolerances": "Tolerances",
             "approval_limit": "Approval limit", "post": "Post"},
    "asis": {"register": "Registered", "extract": "Keyed by AP", "resolve_vendor": "Account (by name)",
             "duplicate_check": "Duplicate (same account)", "credit_note": "Credit note",
             "commitment_match": "PO lookup", "terms": "Terms", "tolerances": "Price and quantity",
             "post": "Posted by AP"},
}
# Result words in the rule log.
RESULT_WORDS = {"ok": "pass", "info": "info", "flag": "flag", "created": "account created", "skipped": "skipped",
                "exception": "Exception", "human_review": "Human review", "blocked": "Block",
                "applied": "credit note linked", "unapplied": "unapplied", "email_loop": "email loop"}
# The four outcomes of the gate (brief v2 section 2), and the as-is equivalents (no gate).
OUTCOME_WORDS: dict[str, dict[str, str]] = {
    "tobe": {"posted": "Post", "applied_credit": "Post", "exception": "Exception", "blocked_duplicate": "Block",
             "human_review": "Human review"},
    "asis": {"posted": "Posted by AP", "applied_credit": "Posted by AP", "exception": "Email loop — untracked",
             "blocked_duplicate": "Blocked (same account)", "human_review": "Email loop — untracked"},
}

# Every as-is / to-be difference of the gate (brief v2 section 2; the as-is behaviour in docs/ASSUMPTIONS.md 8).
# screening / confidence_threshold / doc_type_check / entity_check / approval_matrix: the to-be rules the as-is does
# not have (their steps are left out of the as-is log). po_vendor_match: a quoted PO is the supplier's when it is
# on the resolved vendor account ("account", as-is) or on any account of the same party ("party", to-be).
SCENARIOS: dict[str, dict[str, Any]] = {
    "asis": dict(registration="delayed", screening=False, confidence_threshold=None, vendor_resolution="naive_name",
                 flag_duplicate_vendor_accounts=False, unknown_vendor="create_account", entity_check=False,
                 duplicate_check="account_exact", credit_matching="account", contract_matching=False,
                 catalogue_matching=False, exception_routing=False, terms_source="invoice", approval_matrix=False,
                 doc_type_check=False, po_vendor_match="account"),
    "tobe": dict(registration="on_arrival", screening=True, confidence_threshold=config.CONFIDENCE_THRESHOLD,
                 vendor_resolution="party_identifiers", flag_duplicate_vendor_accounts=True,
                 unknown_vendor="exception", entity_check=True, duplicate_check="party_normalised",
                 credit_matching="party", contract_matching=True, catalogue_matching=True, exception_routing=True,
                 terms_source="master", approval_matrix=True, doc_type_check=True, po_vendor_match="party"),
}
# The scenario flag a step depends on; a step whose flag is off is not part of that scenario's process.
STEP_FLAGS = {"screen": "screening", "confidence": "confidence_threshold", "document_type": "doc_type_check",
              "legal_entity": "entity_check", "approval_limit": "approval_matrix"}

# Keys of GateDecision.details; all are always present (None / False / [] when not applicable).
DETAIL_KEYS = (
    "sample_no", "supplier_name", "invoice_number", "invoice_number_norm", "gross_total", "net_total", "currency",
    "doc_type", "party_id", "true_party_id", "account_id", "resolution_method", "bill_to_entity", "posted_entity",
    "posted", "invoice_id", "wrong_entity_posting", "duplicate_posting", "duplicate_of", "commitment", "po_number",
    "contract_id", "contract_period", "catalogue_id", "requester_name", "next_owner_name", "next_owner_role",
    "owner_title", "gross_chf", "duplicate_leg", "first_pass_match",
    "credit_status", "applied_to", "flags", "terms_days", "terms_source", "invoice_terms_days", "agreed_terms_days",
    "terms_variance_paid", "touchless", "path", "cycle_breakdown", "registration_lag_days", "email_loop_days",
    "line_checks", "lookup_party_id", "invoice_date", "posted_on", "po_numbers", "content_type", "extraction_model",
)
# lookup_party_id: the party found from the extraction's identifiers (to-be only), also for a document that stops
# before resolve_vendor, so later duplicate, credit-note and contract checks still see it. invoice_date: ISO date;
# posted_on: ISO datetime of the simulated posting. doc_type: the document type as read (invoice, credit_note,
# reminder, statement, other). po_numbers: every PO the commitment match used (more than one for a multi-PO invoice).
# content_type (pdf | ubl_xml | email_body) and extraction_model: how it arrived and was read. commitment: po |
# contract | catalogue | none. owner_title: the named owner's job title. gross_chf: the gross in the group currency
# at the simulated rate (approval limit). duplicate_leg: which leg of the duplicate rule matched. first_pass_match:
# matched to a commitment (PO + receipt / confirmation, contract schedule, card / catalogue) and within tolerance at
# the first pass, with no follow-up (deck A6, first-pass match rate).
_FALSE_KEYS = {"posted", "wrong_entity_posting", "duplicate_posting", "terms_variance_paid", "touchless",
               "first_pass_match"}
_LIST_KEYS = {"flags", "line_checks", "po_numbers"}
BLOCKING_OUTCOMES = ("exception", "human_review")


# --------------------------------------------------------------------------------------------
# Small pure helpers
# --------------------------------------------------------------------------------------------


def value(data: dict[str, Any], name: str) -> Any:
    """The extracted value of a field ({field: {"value", "confidence"}}), or None."""
    return (data.get(name) or {}).get("value")


def confidence(data: dict[str, Any], name: str) -> float:
    return float((data.get(name) or {}).get("confidence") or 0.0)


def _present(v: Any) -> bool:
    return v is not None and v != "" and v != []


def low_confidence_fields(data: dict[str, Any], threshold: float) -> list[str]:
    """Critical fields that are missing or below the threshold; [] when the extraction may be used.
    Supplier identity passes when the best of VAT ID / IBAN / name (those with a value) passes."""
    low = []
    identity = [confidence(data, f) for f in extract.SUPPLIER_IDENTITY_FIELDS if _present(value(data, f))]
    if not identity or max(identity) < threshold:
        low.append("supplier identity (tax ID, IBAN or name)")
    for name in extract.CRITICAL_FIELDS:
        if name not in extract.SUPPLIER_IDENTITY_FIELDS and (
                not _present(value(data, name)) or confidence(data, name) < threshold):
            low.append(extract.FIELD_LABELS[name].lower())
    return low


def entity_key(name: Optional[str]) -> str:
    """Lowercase, accents folded, punctuation removed, legal suffixes KEPT: 'Velox Retail S.A.S.' -> 'velox retail sas'.
    (normalize.normalise_name would strip GmbH / SAS / Inc and make the three Velox entities identical.)"""
    if not name:
        return ""
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower().replace(".", "")
    return " ".join(re.sub(r"[^a-z0-9]+", " ", s).split())


def map_bill_to(name: Optional[str], vat: Optional[str], entities: list[tuple[str, str, str]]) -> Optional[str]:
    """Legal entity code billed, from (code, name, vat_id) rows: VAT ID exact, else name exact (suffixes kept),
    else the best fuzz.ratio >= BILL_TO_FUZZY_THRESHOLD, else the entity whose full name starts the bill-to name as
    whole words ('Velox Retail GmbH Store Berlin 01'; longest match, only when unique); None when the bill-to is not
    a Velox entity."""
    vat_n = normalise_vat(vat)
    if vat_n:
        hit = next((code for code, _, entity_vat in entities if normalise_vat(entity_vat) == vat_n), None)
        if hit:
            return hit
    key = entity_key(name)
    if not key:
        return None
    hit = next((code for code, entity_name, _ in entities if entity_key(entity_name) == key), None)
    if hit:
        return hit
    score, code = max(((fuzz.ratio(key, entity_key(n)), c) for c, n, _ in entities), default=(0.0, None))
    if score >= BILL_TO_FUZZY_THRESHOLD:
        return code
    prefixes = [(len(ek), c) for c, n, _ in entities if (ek := entity_key(n)) and key.startswith(ek + " ")]
    longest = max((length for length, _ in prefixes), default=0)
    best = [c for length, c in prefixes if length == longest]
    return best[0] if len(best) == 1 else None


def amounts_close(a: Optional[float], b: Optional[float], tolerance: float = DUPLICATE_AMOUNT_TOLERANCE) -> bool:
    if a is None or b is None:
        return False
    return abs(a - b) <= tolerance * max(abs(a), abs(b)) + 0.005


def same_invoice(number_a: Optional[str], gross_a: Optional[float], number_b: Optional[str],
                 gross_b: Optional[float]) -> bool:
    """Same normalised invoice number and gross within 1% (the duplicate rule, brief section 8 step 5)."""
    norm = normalise_invoice_number(number_a)
    return bool(norm) and norm == normalise_invoice_number(number_b) and amounts_close(gross_a, gross_b)


def price_tolerance(expected: float) -> float:
    """PO_TOLERANCE_PCT of the expected amount or PO_TOLERANCE_ABS, whichever is larger."""
    return round(max(PO_TOLERANCE_PCT * abs(expected), PO_TOLERANCE_ABS), 2)


def within_tolerance(invoiced: float, expected: float) -> bool:
    return abs(round(invoiced - expected, 2)) <= price_tolerance(expected)


def map_lines(invoice_descriptions: list[Optional[str]], po_descriptions: list[str]) -> list[Optional[int]]:
    """Index of the PO line for each invoice line, by description (token_set_ratio >= LINE_MATCH_THRESHOLD).

    1. One-to-one, best score first (ties -> the PO line at the same position, then the lowest line).
    2. A line left over (a split or repeated line) maps to its best PO line, which then carries several lines.
    3. A line whose description matches no PO line maps by position when the counts are equal and that PO line
       is still free (e.g. no descriptions were read); else None (not on the PO).
    """
    scores = [[float(fuzz.token_set_ratio(desc.lower(), p.lower())) if desc else 0.0 for p in po_descriptions]
              for desc in invoice_descriptions]
    mapping: list[Optional[int]] = [None] * len(invoice_descriptions)
    used: set[int] = set()
    pairs = sorted((-s, abs(i - j), i, j) for i, row in enumerate(scores) for j, s in enumerate(row)
                   if s >= LINE_MATCH_THRESHOLD)
    for _, _, i, j in pairs:
        if mapping[i] is None and j not in used:
            mapping[i] = j
            used.add(j)
    for i, row in enumerate(scores):
        if mapping[i] is None and row and max(row) >= LINE_MATCH_THRESHOLD:
            mapping[i] = min(range(len(row)), key=lambda j: (-row[j], abs(i - j), j))
            used.add(mapping[i])
    if len(invoice_descriptions) == len(po_descriptions):
        for i in range(len(mapping)):
            if mapping[i] is None and i not in used:
                mapping[i] = i
                used.add(i)
    return mapping


_DAY_FIRST_DATE = re.compile(r"(\d{1,2})([./])(\d{1,2})\2(\d{4})")


def parse_date(text: Any) -> Optional[date]:
    """ISO date (YYYY-MM-DD), else the day-first numeric forms DD.MM.YYYY and DD/MM/YYYY; None if unreadable."""
    s = str(text).strip() if text else ""
    if not s:
        return None
    try:
        return date.fromisoformat(s)
    except ValueError:
        pass
    m = _DAY_FIRST_DATE.fullmatch(s)
    try:
        return date(int(m[4]), int(m[3]), int(m[1])) if m else None
    except ValueError:
        return None


def contract_period(invoice_date: Optional[date]) -> Optional[str]:
    return f"{invoice_date:%Y-%m}" if invoice_date else None


def registration_on(doc: InboundDocument) -> datetime:
    """When the document is registered under its scenario's rule (to-be on arrival; as-is sim delays)."""
    if SCENARIOS[doc.scenario]["registration"] == "on_arrival":
        return doc.received_on
    return sim.registration_date("asis", doc.channel, doc.received_on)


def order_key(doc: InboundDocument) -> tuple[int, datetime, str]:
    """Processing order of a scenario's documents: the loaded sample set first (simulated Oct–Nov 2026 dates), then
    documents received live through the intake webhook (real dates); within each, registration datetime, doc_id.
    A live document is therefore checked against every sample document (duplicates, credit notes, contracts)."""
    return int(doc.dataset == LIVE_DATASET), registration_on(doc), doc.doc_id


def money(amount: Optional[float], currency: Optional[str] = None) -> str:
    if amount is None:
        return "an unknown amount"
    return f"{amount:,.2f}" + (f" {currency}" if currency else "")


def qty(x: float) -> str:
    return f"{x:,.0f}" if float(x).is_integer() else f"{x:,.2f}"


def log_line(doc_id: str, rule: str, result: str, reason: str = "") -> str:
    """'[B-05] Commitment → Exception — PO 4500123 exists but ...' (brief v2 section 2: "rule X → result")."""
    return f"[{doc_id}] {rule} → {result}" + (f" — {reason}" if reason else "")


# --------------------------------------------------------------------------------------------
# Vendor resolution (pure over lists of accounts / parties)
# --------------------------------------------------------------------------------------------


def find_party(data: dict[str, Any], accounts: list[VendorAccount],
               parties: list[Party]) -> tuple[Optional[str], Optional[str]]:
    """To-be supplier resolution at supplier level on active accounts: tax ID (VAT ID) exact, then IBAN exact (the
    deck's uniqueness rule). A name is never enough: guessing by name is what the as-is does. Returns
    (party_id, method)."""
    linked = [a for a in accounts if a.party_id]
    vat = normalise_vat(value(data, "supplier_vat_id"))
    if vat:
        hit = next((a.party_id for a in linked if normalise_vat(a.vat_id) == vat), None)
        if hit:
            return hit, "vat_id"
    iban = normalise_iban(value(data, "supplier_iban"))
    if iban:
        hit = next((a.party_id for a in linked if normalise_iban(a.iban) == iban), None)
        if hit:
            return hit, "iban"
    return None, None


METHOD_WORDS = {"vat_id": "tax ID", "iban": "IBAN", "exact_name": "exact name", "fuzzy_name": "similar name",
                "created": "account created"}


def true_party(data: dict[str, Any], parties: list[Party], linked_accounts: list[VendorAccount]) -> Optional[str]:
    """The real supplier, for truth metrics: party VAT ID, then the IBAN of any account linked to a party,
    then the canonical name."""
    vat = normalise_vat(value(data, "supplier_vat_id"))
    hit = next((p.party_id for p in parties if vat and normalise_vat(p.vat_id) == vat), None)
    iban = normalise_iban(value(data, "supplier_iban"))
    hit = hit or next((a.party_id for a in linked_accounts if iban and normalise_iban(a.iban) == iban), None)
    if hit:
        return hit
    name = value(data, "supplier_name")
    score, party_id = max(((name_similarity(name, p.canonical_name), p.party_id) for p in parties),
                          default=(0.0, None))
    return party_id if score >= NAME_SIMILARITY_THRESHOLD else None


def party_accounts(party: Party, accounts: list[VendorAccount]) -> list[VendorAccount]:
    """Active accounts of a party: linked to it, or unlinked with its VAT ID or a matching name."""
    vat = normalise_vat(party.vat_id)
    return [a for a in accounts if a.party_id == party.party_id or (
        a.party_id is None and ((vat and normalise_vat(a.vat_id) == vat) or names_match(a.display_name,
                                                                                         party.canonical_name)))]


def naive_account(name: Optional[str], accounts: list[Any]) -> tuple[Optional[Any], Optional[str]]:
    """As-is 'first name hit' over active accounts sorted by account_id: display name equal to the printed name
    (case-insensitive, trimmed, NOT normalised), else the first with names_match; (None, None) if none."""
    target = (name or "").strip().casefold()
    if not target:
        return None, None
    exact = next((a for a in accounts if a.display_name.strip().casefold() == target), None)
    if exact is not None:
        return exact, "exact_name"
    fuzzy = next((a for a in accounts if names_match(a.display_name, name)), None)
    return (fuzzy, "fuzzy_name") if fuzzy is not None else (None, None)


# --------------------------------------------------------------------------------------------
# PO line checks (three-way match)
# --------------------------------------------------------------------------------------------


def to_float(x: Any) -> Optional[float]:
    """A float, or None when the value is missing or not a number. Every number the gate stores is a float, so a
    record never depends on whether a seed row came from memory (int) or from the database (float)."""
    if x is None or isinstance(x, bool):
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def line_amount(line: dict[str, Any]) -> Optional[float]:
    """The amount of an extracted invoice line: as read, else quantity x unit price; None if neither."""
    amount, q, unit = to_float(line.get("amount")), to_float(line.get("quantity")), to_float(line.get("unit_price"))
    if amount is None and q is not None and unit is not None:
        amount = round(q * unit, 2)
    return amount


def lines_total_mismatch(lines: list[dict[str, Any]], net_total: Optional[float],
                         currency: Optional[str]) -> Optional[str]:
    """Why the extracted lines do not add up to the net total (PO_TOLERANCE rule on the net total); None when they
    do, or when a line amount or the net total is unknown (the line checks report a missing amount)."""
    amounts = [line_amount(line) for line in lines]
    if net_total is None or not amounts or any(a is None for a in amounts):
        return None
    total, tolerance = round(sum(amounts), 2), price_tolerance(net_total)
    if abs(round(total - net_total, 2)) <= tolerance:
        return None
    return (f"the invoice lines do not add up to the net total: {len(lines)} line{'s' if len(lines) != 1 else ''} "
            f"for {money(total, currency)} vs a net total of {money(net_total, currency)} "
            f"(tolerance {tolerance:,.2f})")


def _currency_issues(currency: Optional[str], po: PurchaseOrder) -> list[dict[str, Any]]:
    if currency and currency != po.currency:
        return [{"kind": "price", "receiver": None,
                 "text": f"The invoice is in {currency} but PO {po.po_number} is in {po.currency}"}]
    return []


def _po_kind(po: PurchaseOrder) -> str:
    return "service" if po.category == "service" else "goods"


def _received(pl: Any, receipts: list[ProductReceipt]) -> tuple[list[ProductReceipt], float]:
    rows = [r for r in receipts if r.line_no == pl.line_no]
    return rows, float(sum(float(r.qty_received) for r in rows))


def _receipt_issues(label: str, invoiced: float, pl: Any, po: PurchaseOrder,
                    receipts: list[ProductReceipt]) -> list[dict[str, Any]]:
    """Receipt check of one PO line for an invoiced quantity: a service confirmation (services), else goods
    received for the whole invoiced quantity (a shortfall goes to the receiver)."""
    if not pl.receipt_required:
        return []
    rows, received = _received(pl, receipts)
    if po.category == "service":
        if any(r.kind == "service_confirmation" for r in rows):
            return []
        return [{"kind": "no_receipt", "receiver": None,
                 "text": f"no service confirmation is recorded for line {pl.line_no}"}]
    if received <= 0:
        return [{"kind": "no_receipt", "receiver": None, "text": f"no goods receipt is recorded for line {pl.line_no}"}]
    if received < invoiced:
        return [{"kind": "quantity", "receiver": rows[-1].received_by,
                 "text": f"{label}: {qty(invoiced)} invoiced but only {qty(received)} received on PO {po.po_number}"}]
    return []


def fully_received(po: PurchaseOrder, receipts: list[ProductReceipt]) -> bool:
    """Every receipt-required goods line of the PO is received for its full ordered quantity."""
    return all(_received(pl, receipts)[1] >= float(pl.qty) for pl in po.lines if pl.receipt_required)


def _lines_label(nos: list[int], pl: Any) -> str:
    if len(nos) == 1:
        return f"Line {nos[0]}"
    return f"Lines {', '.join(str(n) for n in nos[:-1])} and {nos[-1]} (PO line {pl.line_no})"


def and_list(items: list[str]) -> str:
    """'4500128', '4500128 and 4500130', '4500128, 4500130 and 4500131'."""
    return items[0] if len(items) == 1 else f"{', '.join(items[:-1])} and {items[-1]}"


def _po_label(pos: list[PurchaseOrder]) -> str:
    """'PO 4500117' or 'POs 4500128 and 4500130'."""
    numbers = [po.po_number for po in pos]
    return f"PO {numbers[0]}" if len(numbers) == 1 else f"POs {and_list(numbers)}"


def _tag_po(items: list[dict[str, Any]], po: PurchaseOrder, multi: bool, key: str) -> list[dict[str, Any]]:
    """Issues (key "po") or line-check rows (key "po_number") of a multi-PO invoice carry their PO number."""
    return [dict(item, **{key: po.po_number}) for item in items] if multi else items


def check_po_lines(invoice_lines: list[dict[str, Any]], po: PurchaseOrder, receipts: list[ProductReceipt],
                   currency: Optional[str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Check invoice lines against PO lines and receipts. Returns (line_checks, issues); an issue is
    {"kind": "no_receipt" | "quantity" | "price", "text", "receiver"}.

    Invoice lines are mapped to PO lines (map_lines); the lines of one PO line (split or repeated) are checked
    together: summed quantity against the ordered and received quantity, summed amount against the PO unit price
    x summed quantity. line_checks keeps one row per invoice line, with the verdict of its PO line."""
    return check_lines_across_pos(invoice_lines, [po], {po.po_number: receipts}, currency)


def check_lines_across_pos(invoice_lines: list[dict[str, Any]], pos: list[PurchaseOrder],
                           receipts_by_po: dict[str, list[ProductReceipt]],
                           currency: Optional[str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """check_po_lines over the lines of one or more POs (a multi-PO invoice): every invoice line is mapped to the
    best PO line of any of them. With several POs each row carries "po_number" and each issue "po"."""
    multi = len(pos) > 1
    issues = [i for po in pos for i in _tag_po(_currency_issues(currency, po), po, multi, "po")]
    targets = [(po, pl) for po in pos for pl in po.lines]
    mapping = map_lines([ln.get("description") for ln in invoice_lines], [pl.description for _, pl in targets])
    groups: dict[int, list[int]] = {}
    for no, index in enumerate(mapping, start=1):
        if index is not None:
            groups.setdefault(index, []).append(no)
    checks: list[dict[str, Any]] = []
    for no, (line, index) in enumerate(zip(invoice_lines, mapping), start=1):
        desc = line.get("description")
        if index is None:
            row = {"line": no, "description": desc, "po_line": None, "kind": _po_kind(pos[0]),
                   "issues": ["price"], "result": "issue"}
            checks.append(dict(row, po_number=None) if multi else row)
            issues.append({"kind": "price", "receiver": None, **({"po": None} if multi else {}),
                           "text": f"Line {no} ('{desc or 'no description'}') is not on {_po_label(pos)}"})
        elif groups[index][0] == no:
            nos = groups[index]
            po, pl = targets[index]
            rows, found = _check_po_line(nos, [invoice_lines[n - 1] for n in nos], pl, po,
                                         receipts_by_po.get(po.po_number, []))
            checks.extend(_tag_po(rows, po, multi, "po_number"))
            issues.extend(_tag_po(found, po, multi, "po"))
    checks.sort(key=lambda c: c["line"])
    return checks, issues


def _check_po_line(nos: list[int], lines: list[dict[str, Any]], pl: Any, po: PurchaseOrder,
                   receipts: list[ProductReceipt]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """One PO line and the invoice lines mapped to it. Returns (a row per invoice line, issues)."""
    ordered, po_unit = float(pl.qty), float(pl.unit_price)
    values = []  # (quantity, unit price, amount) per invoice line; a missing quantity is the ordered quantity
    for line in lines:
        q = to_float(line.get("quantity"))
        q = ordered if q is None else q
        unit, amount = to_float(line.get("unit_price")), to_float(line.get("amount"))
        values.append((q, unit, amount if amount is not None else (round(q * unit, 2) if unit is not None else None)))
    q_total = sum(v[0] for v in values)
    amounts = [v[2] for v in values]
    amount = round(sum(amounts), 2) if all(a is not None for a in amounts) else None
    expected = round(po_unit * q_total, 2)
    tolerance = price_tolerance(expected)
    variance = round(amount - expected, 2) if amount is not None else None
    label = _lines_label(nos, pl)
    found: list[dict[str, Any]] = []
    if variance is None or abs(variance) > tolerance:
        shown_unit = values[0][1] if len(values) == 1 and values[0][1] is not None else (
            amount / q_total if amount is not None and q_total else None)
        found.append({"kind": "price", "receiver": None, "text": (
            f"{label}: {qty(q_total)} x {money(shown_unit)} vs PO {po.po_number} at {po_unit:,.2f} "
            f"({variance:+,.2f}, tolerance {tolerance:,.2f})" if variance is not None
            else f"{label}: no amount could be read to compare with PO {po.po_number}")})
    if q_total > ordered:
        found.append({"kind": "quantity", "receiver": None,
                      "text": f"{label}: {qty(q_total)} invoiced but only {qty(ordered)} ordered on PO {po.po_number}"})
    found.extend(_receipt_issues(label, q_total, pl, po, receipts))
    received = _received(pl, receipts)[1]
    kinds = [i["kind"] for i in found]
    rows = []
    for no, line, (q, unit, line_amt) in zip(nos, lines, values):
        line_expected = round(po_unit * q, 2)
        rows.append({"line": no, "description": line.get("description"), "po_line": pl.line_no, "kind": _po_kind(po),
                     "invoiced_qty": q, "ordered_qty": ordered, "unit_price": unit, "po_unit_price": po_unit,
                     "amount": line_amt, "expected": line_expected,
                     "variance": round(line_amt - line_expected, 2) if line_amt is not None else None,
                     "tolerance": price_tolerance(line_expected), "received_qty": received,
                     "issues": kinds, "result": "issue" if found else "ok"})
    return rows, found


def check_po_header(net_total: Optional[float], po: PurchaseOrder, receipts: list[ProductReceipt],
                    currency: Optional[str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], Optional[str]]:
    """No invoice lines were read: the net total must match the whole PO (unit price x quantity of every line),
    then every receipt-required line must be received in full (goods) or confirmed (services).
    Returns (line_checks, issues, why it cannot be decided without the lines, or None)."""
    return check_header_across_pos(net_total, [po], {po.po_number: receipts}, currency)


def check_header_across_pos(net_total: Optional[float], pos: list[PurchaseOrder],
                            receipts_by_po: dict[str, list[ProductReceipt]], currency: Optional[str]
                            ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], Optional[str]]:
    """check_po_header over one or more POs: the net total against the sum of their totals, then every line of each."""
    multi = len(pos) > 1
    issues = [i for po in pos for i in _tag_po(_currency_issues(currency, po), po, multi, "po")]
    if net_total is None:
        return [], issues, "neither the invoice lines nor the net total could be read"
    po_total = round(sum(float(pl.unit_price) * float(pl.qty) for po in pos for pl in po.lines), 2)
    tolerance = price_tolerance(po_total)
    variance = round(net_total - po_total, 2)
    what = "PO total" if not multi else f"total of {_po_label(pos)}"
    header: dict[str, Any] = {"line": None, "description": f"No invoice lines read: net total vs {what}",
                              "po_line": None, "kind": _po_kind(pos[0]), "amount": net_total, "expected": po_total,
                              "variance": variance, "tolerance": tolerance}
    if multi:
        header["po_number"] = None
    if abs(variance) > tolerance:
        header.update(issues=["price"], result="issue")
        return [header], issues, (f"the invoice lines could not be read and the net total "
                                  f"{money(net_total, currency)} does not match the {what} "
                                  f"{money(po_total, pos[0].currency)} (tolerance {tolerance:,.2f})")
    header.update(issues=[], result="ok")
    checks = [header]
    for po in pos:
        receipts = receipts_by_po.get(po.po_number, [])
        for pl in po.lines:
            label = f"PO {po.po_number} line {pl.line_no}" if multi else f"PO line {pl.line_no}"
            found = _receipt_issues(f"{label} (whole PO invoiced)", float(pl.qty), pl, po, receipts)
            row = {"line": None, "description": pl.description, "po_line": pl.line_no, "kind": _po_kind(po),
                   "invoiced_qty": float(pl.qty), "ordered_qty": float(pl.qty),
                   "po_unit_price": float(pl.unit_price), "received_qty": _received(pl, receipts)[1],
                   "issues": [i["kind"] for i in found], "result": "issue" if found else "ok"}
            checks.extend(_tag_po([row], po, multi, "po_number"))
            issues.extend(_tag_po(found, po, multi, "po"))
    return checks, issues, None


# --------------------------------------------------------------------------------------------
# Per-document state
# --------------------------------------------------------------------------------------------


def _days(n: int) -> str:
    """'1 business day' / '7 business days'."""
    return f"{n} business day{'' if n == 1 else 's'}"


def _blank_details(doc: InboundDocument) -> dict[str, Any]:
    details: dict[str, Any] = {k: (False if k in _FALSE_KEYS else [] if k in _LIST_KEYS else None)
                               for k in DETAIL_KEYS}
    details.update(sample_no=doc.sample_no, doc_type=doc.doc_type, registration_lag_days=0, cycle_breakdown={},
                   content_type=doc.content_type or "pdf",
                   extraction_model=doc.extraction.model if doc.extraction is not None else None)
    return details


@dataclass
class _Gate:
    session: Session
    doc: InboundDocument
    log: Log
    data: dict[str, Any]
    details: dict[str, Any]
    cfg: dict[str, Any] = field(init=False)
    spec: Optional[world.DocumentSpec] = field(init=False, default=None)  # a sample document's spec
    steps: list[dict[str, str]] = field(default_factory=list)
    earlier: list[GateDecision] = field(default_factory=list)  # decisions of documents processed before
    entities: dict[str, LegalEntity] = field(default_factory=dict)
    party: Optional[Party] = None
    account: Optional[VendorAccount] = None
    outcome: Optional[str] = None
    exception_type: Optional[str] = None
    owner_role: Optional[str] = None
    owner_name: Optional[str] = None
    sla_days: Optional[int] = None
    reason: Optional[str] = None
    approval: bool = False  # the resolution needs a workflow approval (to-be no_po)
    email_loop: bool = False  # as-is untracked follow-up: the document still posts afterwards
    loop_cause: str = ""
    stopped_at: Optional[str] = None
    posted_note: str = ""  # what the commitment step adds to the posting reason
    previous_invoice_id: Optional[str] = None  # kept on a re-run so credit applications stay valid
    decided_on: Optional[datetime] = None
    po_numbers: list[str] = field(default_factory=list)  # extracted PO numbers, normalised, in printed order
    invoice_date: Optional[date] = None
    # Price / quantity issues found by the commitment match, decided by the tolerances rule:
    # {"pos": [...], "issues": [...], "checks": [...], "ignored": [...], "note": str, "lines_ok": str}
    tolerance: Optional[dict[str, Any]] = None

    def __post_init__(self) -> None:
        self.cfg = SCENARIOS[self.doc.scenario]
        self.spec = seed.spec_for(self.doc.dataset, self.doc.sample_no)

    # -- helpers ---------------------------------------------------------------------------
    def v(self, name: str) -> Any:
        return value(self.data, name)

    @property
    def is_credit_note(self) -> bool:
        return self.doc.doc_type == "credit_note"

    @property
    def key(self) -> str:
        """Seed key of the as-is email loop: the case document's loop key (its number before the renumbering of 25 Sep
        2026) or sample number; a live (webhook) document's own id, so each one gets its own loop length."""
        if self.spec is not None and self.spec.loop_key:
            return self.spec.loop_key
        return f"{self.doc.sample_no:02d}" if self.doc.sample_no else self.doc.doc_id

    def entity_label(self, code: Optional[str]) -> str:
        le = self.entities.get(code or "")
        return f"{le.name} ({code})" if le else "an entity that is not a Velox legal entity"

    def step(self, name: str, result: str, detail: str, *, quiet: bool = False) -> None:
        """Record a step; a quiet one (not reached, not applicable) is kept in the steps but not in the rule log."""
        self.steps.append({"step": name, "result": result, "detail": detail, **({"quiet": True} if quiet else {})})
        if not quiet:
            rule = STEP_LABELS[self.doc.scenario].get(name, name)
            self.log(log_line(self.doc.doc_id, rule, RESULT_WORDS.get(result, result), detail))

    def add_flag(self, type_: str, owner: world.Person, detail: str) -> None:
        self.details["flags"].append({"type": type_, "label": taxonomy.label(type_), "owner_name": owner.name,
                                      "owner_role": taxonomy.BY_KEY[type_].owner_role, "detail": detail})

    def fail(self, step: str, type_: str, *, owner: Optional[str], reason: str, cause: str,
             next_owner: Optional[tuple[str, str]] = None, approval: bool = False) -> None:
        """A typed exception. With exception routing it goes to its owner (the A3 role, a named person) with the
        taxonomy SLA and outcome (Exception or Human review) and the pipeline stops; without it (as-is) it becomes
        the untracked email loop and the document continues."""
        if self.cfg["exception_routing"]:
            etype = taxonomy.get(type_)
            self.outcome = etype.outcome
            self.exception_type, self.owner_role, self.owner_name = type_, etype.owner_role, owner
            self.sla_days, self.reason, self.approval = etype.sla_days, reason, approval
            person = world.PERSON_BY_NAME.get(owner or "")
            self.details["owner_title"] = person.role if person else None
            if next_owner:
                self.details["next_owner_name"], self.details["next_owner_role"] = next_owner
            self.stopped_at = step
            self.step(step, "human_review" if etype.outcome == "human_review" else "exception", reason)
            return
        if not self.email_loop:
            self.email_loop, self.loop_cause = True, cause
            self.outcome, self.exception_type = "exception", "email_loop"
            self.details["email_loop_days"] = sim.email_loop_days(self.key)
        self.step(step, "email_loop", f"{cause}: untracked follow-up by email, with no owner and no SLA.")

    def stop(self, step: str) -> None:
        self.stopped_at = self.stopped_at or step

    def same_party(self, earlier: GateDecision) -> bool:
        """The earlier document is from this document's party: resolved to it, or (when it stopped before vendor
        resolution, e.g. in human review) found for it from its identifiers."""
        party = self.details["party_id"]
        other = earlier.details.get("party_id") or earlier.details.get("lookup_party_id")
        return bool(party) and other == party


# --------------------------------------------------------------------------------------------
# Steps 1-9
# --------------------------------------------------------------------------------------------


def _register(g: _Gate) -> None:
    reg = registration_on(g.doc)
    g.doc.registered, g.doc.registered_on = True, reg
    lag = sim.business_days_between(g.doc.received_on, reg)
    g.details["registration_lag_days"] = lag
    if lag == 0:
        g.step("register", "ok", f"{g.doc.doc_id} · {reg:%a %d %b %Y %H:%M} · {g.doc.mailbox}: from this minute the "
                                 "invoice exists and the clock runs.")
        return
    if g.doc.dataset == LIVE_DATASET and reg > datetime.now():  # received live: the registration day is ahead
        why = "the store forwards it to AP" if g.doc.channel == "store_mailbox" else "AP opens ap@ and keys it"
        g.step("register", "info", f"Not registered yet: expected {_days(lag)} after receipt, on "
                                   f"{reg:%a %d %b %Y}, when {why}.")
        return
    why = "the store forwarded it to AP" if g.doc.channel == "store_mailbox" else "AP opened ap@ and keyed it"
    g.step("register", "info", f"Registered {_days(lag)} after receipt, on {reg:%a %d %b %Y}, when {why}.")


def _screen(g: _Gate) -> None:
    """Simulated pre-model screening (deck slide 10: documents are screened before they reach the model)."""
    what = {"ubl_xml": "a structured e-invoice (XML)", "email_body": "the text of the email"}.get(
        g.doc.content_type or "pdf", "a PDF")
    g.step("screen", "ok", f"Screened before the model: {what} from {g.doc.sender_email or 'the sender'}; accepted "
                           "file type, no hidden instructions found (simulated check).")


def _extract(g: _Gate) -> None:
    """Read with Gemini (to-be) / keyed by AP (as-is). The model only reads and classifies: every decision on what it
    read is a rule of its own (confidence, document type, ...)."""
    if not g.cfg["exception_routing"]:
        if not g.data:
            cause = ("No document attached: the invoice is only in the email body, so AP keys it by hand"
                     if g.doc.content_type == "email_body" else "Nothing could be keyed from the document")
            g.fail("extract", "human_review", owner=None, reason=cause, cause=cause)
            g.stop("extract")
            return
        g.step("extract", "ok", "AP opened the document and keyed the invoice by hand: no confidence, no checks.")
        return
    if not g.data:
        what = ("No document is attached: the invoice is only in the email body" if g.doc.content_type == "email_body"
                else "No extraction is available for this document")
        g.step("extract", "info", f"{what}; nothing was read.")
        return
    if g.doc.content_type == "ubl_xml":
        g.step("extract", "ok", "Structured e-invoice: the fields are parsed from the XML, no model call.")
        return
    model = g.doc.extraction.model if g.doc.extraction else "unknown model"
    read = DOC_TYPE_WORDS.get(g.doc.doc_type, g.doc.doc_type)
    g.step("extract", "ok", f"Read by {model}: {len(extract.FIELDS)} fields, each with a confidence; document type "
                            f"'{read}' ({confidence(g.data, 'doc_type'):.2f}).")


DOC_TYPE_WORDS = {"invoice": "invoice", "credit_note": "credit note", "reminder": "reminder",
                  "statement": "statement", "other": "other", "unknown": "unknown"}


def _confidence(g: _Gate) -> None:
    """Confidence rule: every critical field present and at or above the threshold, else Human review (AP review)."""
    threshold, ap = g.cfg["confidence_threshold"], world.AP_SPECIALIST
    if not g.data:
        why = ("the invoice is only in the email body, so AP keys it from the email and verifies it"
               if g.doc.content_type == "email_body" else "nothing was read, so AP keys the invoice and verifies it")
        g.fail("confidence", "human_review", owner=ap.name, reason=f"No fields to check: {why} (AP review: {ap.name}).",
               cause="Nothing read")
        return
    low = low_confidence_fields(g.data, threshold)
    if low:
        g.fail("confidence", "human_review", owner=ap.name,
               reason=f"{', '.join(low).capitalize()} missing or below {threshold:.2f}: Human review, AP verifies the "
                      f"fields against the document (AP review: {ap.name}).", cause="Low extraction confidence")
        return
    lowest = min(confidence(g.data, f) for f in extract.CRITICAL_FIELDS if _present(g.v(f)))
    g.step("confidence", "ok", f"Every critical field at or above {threshold:.2f} (lowest {lowest:.2f}).")


def _document_type(g: _Gate) -> None:
    """Invoices and credit notes continue; a reminder continues as a copy of the invoice it reproduces (the duplicate
    rule decides); a statement is a supplier payment-status query; anything else goes to Human review."""
    read, ap = g.doc.doc_type, world.AP_SPECIALIST
    negative = [x for x in (g.details["gross_total"], g.details["net_total"]) if x is not None and x < 0]
    if read == "invoice" and negative:
        g.fail("document_type", "human_review", owner=ap.name,
               reason=f"Read as an invoice but the total is negative ({money(negative[0], g.details['currency'])}): "
                      f"probable credit note, Human review (AP review: {ap.name}).",
               cause="Negative total: probable credit note")
        return
    if read in ("invoice", "credit_note"):
        g.step("document_type", "ok", f"{DOC_TYPE_WORDS[read].capitalize()}: the rules apply.")
        return
    if read == "reminder":
        g.step("document_type", "ok", f"Payment reminder reproducing invoice {g.details['invoice_number'] or '?'}: "
                                      "checked like the invoice it copies (the duplicate rule decides).")
        return
    if read == "statement":
        g.fail("document_type", "payment_status_query", owner=ap.name,
               reason="A statement of account, not an invoice: nothing is posted. Supplier payment-status query: the "
                      f"agent drafts the reply with the status of the open items, AP ({ap.name}) approves it.",
               cause="Statement of account")
        return
    g.fail("document_type", "human_review", owner=ap.name,
           reason=f"Read as '{DOC_TYPE_WORDS.get(read, read)}': not an invoice or credit note, so nothing is posted. "
                  f"Human review (AP review: {ap.name}) files it or keys it.", cause="Not an invoice")


def _resolve_vendor(g: _Gate) -> None:
    scenario = g.doc.scenario
    accounts = list(g.session.scalars(select(VendorAccount).where(
        VendorAccount.scenario == scenario, VendorAccount.status == "active").order_by(VendorAccount.account_id)))
    if g.cfg["vendor_resolution"] == "party_identifiers":
        _resolve_party(g, accounts)
    else:
        account, method = naive_account(g.v("supplier_name"), accounts)
        if account is not None:
            _use_account(g, account, method, f"AP picks the first account whose name matches the printed name "
                                             f"'{g.v('supplier_name')}' ({METHOD_WORDS[method]}); tax ID and IBAN "
                                             "are not used.")
        else:
            _unknown_vendor(g)


def _resolve_party(g: _Gate, accounts: list[VendorAccount]) -> None:
    parties = {p.party_id: p for p in g.session.scalars(select(Party).where(Party.scenario == g.doc.scenario))}
    party_id, method = find_party(g.data, accounts, list(parties.values()))
    mine = party_accounts(parties[party_id], accounts) if party_id else []
    if not mine:
        _unknown_vendor(g)
        return
    g.party = parties[party_id]
    bill_to = g.details["bill_to_entity"]
    in_entity = [a for a in mine if a.legal_entity_code == bill_to]
    how = METHOD_WORDS.get(method, method)
    if len(in_entity) == 1:
        _use_account(g, in_entity[0], method, f"Resolved to {g.party.canonical_name} by {how} at supplier level; "
                                              f"its record in {bill_to} is {in_entity[0].account_id}.")
    elif in_entity:
        account = min(in_entity, key=lambda a: (a.party_id != g.party.party_id, a.account_id))
        others = ", ".join(a.account_id for a in in_entity if a is not account)
        detail = (f"{g.party.canonical_name} has {len(in_entity)} active accounts in {bill_to}; the canonical "
                  f"account {account.account_id} is used and {others} should be merged or deactivated.")
        if g.cfg["flag_duplicate_vendor_accounts"]:
            g.add_flag("duplicate_vendor_account", world.MASTER_DATA_OWNER, detail)
        _use_account(g, account, method, detail, result="flag" if g.cfg["flag_duplicate_vendor_accounts"] else "ok")
    else:
        account = mine[0]
        _use_account(g, account, method, f"Resolved to {g.party.canonical_name} by {how} at supplier level, but it "
                                         f"has no record in {bill_to or 'the billed entity'}; nearest record "
                                         f"{account.account_id} ({account.legal_entity_code}).")


def _use_account(g: _Gate, account: VendorAccount, method: str, detail: str, result: str = "ok") -> None:
    g.account = account
    if g.party is None and account.party_id:
        g.party = g.session.scalar(select(Party).where(Party.scenario == g.doc.scenario,
                                                       Party.party_id == account.party_id))
    g.details.update(account_id=account.account_id, resolution_method=method,
                     party_id=g.party.party_id if g.party else None)
    g.step("resolve_vendor", result, detail)


def _unknown_vendor(g: _Gate) -> None:
    name = g.v("supplier_name") or "the supplier"
    if g.cfg["unknown_vendor"] == "create_account":
        _create_account(g)
        return
    owner = world.MASTER_DATA_OWNER
    g.fail("resolve_vendor", "unknown_vendor", owner=owner.name,
           reason=f"{name} matches no supplier record by tax ID or IBAN: unknown vendor, routed to the master data "
                  f"owner {owner.name} to onboard it through the vendor request workflow.", cause="Unknown supplier")
    g.stop("resolve_vendor")


def _create_account(g: _Gate) -> None:
    """As-is: AP opens a new vendor account for any name it does not find, to pay the invoice."""
    ids = g.session.scalars(select(VendorAccount.account_id).where(VendorAccount.scenario == g.doc.scenario))
    n = max((int(i[2:]) for i in ids if i[2:].isdigit()), default=0) + 1
    terms = g.v("payment_terms_days")
    account = VendorAccount(
        scenario=g.doc.scenario, account_id=f"V-{n:06d}", legal_entity_code=g.details["bill_to_entity"] or "VDE",
        party_id=None, display_name=(g.v("supplier_name") or "Unknown supplier")[:120],
        vat_id=g.v("supplier_vat_id"), iban=g.v("supplier_iban"), payment_terms_days=int(terms) if terms else 30,
        created_by=ON_THE_FLY_CREATOR, created_on=g.doc.registered_on.date(), status="active",
        notes=ON_THE_FLY_NOTE, corruption_rules=["gate"])
    g.session.add(account)
    g.session.flush()
    g.account = account
    g.details.update(account_id=account.account_id, resolution_method="created", party_id=None)
    g.step("resolve_vendor", "created", f"No account named '{account.display_name}': AP opened "
                                        f"{account.account_id} in {account.legal_entity_code} to pay the invoice.")


def _legal_entity(g: _Gate) -> None:
    bill_to, acc = g.details["bill_to_entity"], g.account
    if bill_to == acc.legal_entity_code:
        g.step("legal_entity", "ok", f"Billed to {g.entity_label(bill_to)}, where {acc.account_id} is held.")
        return
    if bill_to is None:
        where = f"'{g.v('bill_to_name') or 'no name'}', which is not a Velox legal entity"
    else:
        name = g.party.canonical_name if g.party else acc.display_name
        where = (f"{g.entity_label(bill_to)}, but {name} has no vendor account there (its account "
                 f"{acc.account_id} is in {acc.legal_entity_code})")
    po = _purchase_order(g)
    if po is not None:
        where += f" and PO {po.po_number} belongs to {po.legal_entity_code}"
    ap = world.AP_SPECIALIST
    g.fail("legal_entity", "wrong_legal_entity", owner=ap.name,
           reason=f"Billed to {where}: wrong legal entity, routed to AP ({ap.name}) to ask the supplier to re-issue "
                  "it or re-assign it.", cause="Billed to the wrong Velox entity")


def _numbered(word: str, number: Any) -> str:
    """'Invoice INV-7', 'Credit note CN-3'; 'Invoice (no number read)' when the number is missing or blank."""
    text = str(number).strip() if number is not None else ""
    return f"{word} {text}" if text else f"{word} (no number read)"


def duplicate_leg(g: _Gate, earlier: GateDecision) -> Optional[str]:
    """Which leg of the to-be duplicate rule an earlier document of the same supplier matches (brief v2 section 2):
    the same invoice number, or the same gross amount and invoice date billed to the same entity; None if neither.
    A statement is never the original, and a credit note is only compared with credit notes."""
    det = earlier.details
    if det.get("doc_type") in ("statement", "other") or (det.get("doc_type") == "credit_note") != g.is_credit_note:
        return None
    number = g.details["invoice_number_norm"]
    if number and number == det.get("invoice_number_norm"):
        return f"same invoice number {g.details['invoice_number']}"
    if (g.invoice_date and det.get("invoice_date") == g.invoice_date.isoformat()
            and det.get("bill_to_entity") == g.details["bill_to_entity"]
            and amounts_close(g.details["gross_total"], det.get("gross_total"))):
        return (f"same amount {money(g.details['gross_total'], g.details['currency'])} and date "
                f"{g.invoice_date:%d %b %Y}")
    return None


def _duplicate_check(g: _Gate) -> None:
    number, gross = g.details["invoice_number"], g.details["gross_total"]
    blank = not (str(number).strip() if number is not None else "")
    leg = None
    if g.cfg["duplicate_check"] == "party_normalised":
        hit, leg = next(((d.doc_id, lg) for d in g.earlier if g.same_party(d) and (lg := duplicate_leg(g, d))),
                        (None, None))
        scope = f"from {g.party.canonical_name if g.party else 'this supplier'} on any of its records"
    else:
        # A missing number is not a number: it never equals the blank number of an earlier posting.
        earlier_ids = [d.doc_id for d in g.earlier]
        hit = None if blank else g.session.scalar(select(PendingVendorInvoice.doc_id).where(
            PendingVendorInvoice.scenario == g.doc.scenario, PendingVendorInvoice.doc_id.in_(earlier_ids),
            PendingVendorInvoice.vendor_account_id == g.account.account_id,
            PendingVendorInvoice.invoice_number == number).limit(1))
        scope = f"posted on account {g.account.account_id} (other accounts are not checked)"
    if hit is None:
        if g.cfg["duplicate_check"] == "party_normalised":
            detail = f"No earlier invoice {scope} with the same number, or the same amount and date."
        else:
            detail = (f"No invoice number read, so no earlier invoice {scope} can be matched." if blank
                      else f"No earlier invoice {number} {scope}.")
        g.step("duplicate_check", "ok", detail)
        return
    routed = g.cfg["exception_routing"]
    ap = world.AP_SPECIALIST
    g.outcome, g.exception_type = "blocked_duplicate", "duplicate_invoice"
    g.owner_role, g.owner_name = (taxonomy.get("duplicate_invoice").owner_role, ap.name) if routed else (None, None)
    g.sla_days = taxonomy.get("duplicate_invoice").sla_days if routed else None
    g.details.update(duplicate_of=hit, duplicate_leg=leg)
    supplier = g.party.canonical_name if g.party else g.account.display_name
    what = f"{_numbered('Invoice', number)} from {supplier} ({money(gross, g.details['currency'])})"
    g.reason = (f"{what} was already registered as {hit} ({leg}): Block before posting; AP replies to the supplier "
                f"with the status ({ap.name})." if routed else f"{what} was already posted as {hit}: blocked.")
    g.stopped_at = "duplicate_check"
    g.step("duplicate_check", "blocked", g.reason)


def _credit_note(g: _Gate) -> None:
    if not g.is_credit_note:
        g.step("credit_note", "skipped", "Not a credit note.", quiet=True)
        return
    ref = g.v("referenced_invoice_number")
    ref = str(ref).strip() if ref is not None else ""  # blank: no reference (never matches a blank number)
    if g.cfg["credit_matching"] == "party":
        ref_norm = normalise_invoice_number(ref)
        target = next((d for d in g.earlier if ref_norm and g.same_party(d)
                       and d.details.get("doc_type") not in ("credit_note", "other")
                       and d.details.get("invoice_number_norm") == ref_norm), None)
        applied_to = (target.details.get("invoice_id") or target.doc_id) if target else None
        where = f"document {target.doc_id}, same supplier" if target else ""
    else:
        # No reference: nothing to look up (a blank reference would equal the blank number of an earlier posting).
        earlier_ids = [d.doc_id for d in g.earlier]
        applied_to = g.session.scalar(select(PendingVendorInvoice.invoice_id).where(
            PendingVendorInvoice.scenario == g.doc.scenario, PendingVendorInvoice.doc_id.in_(earlier_ids),
            PendingVendorInvoice.vendor_account_id == g.account.account_id,
            PendingVendorInvoice.invoice_number == ref).limit(1)) if ref else None
        where = f"same vendor account {g.account.account_id}"
    if applied_to:
        g.details.update(credit_status="applied", applied_to=applied_to)
        g.step("credit_note", "applied", f"{_numbered('Credit note', g.details['invoice_number'])} linked to "
                                         f"invoice {ref} ({applied_to}; {where}).")
        return
    if g.cfg["credit_matching"] == "party":
        # Pending, not unapplied: nothing is posted until AP identifies the invoice (credit_status stays None, so it
        # is not an unapplied credit either).
        ap = world.AP_SPECIALIST
        g.fail("credit_note", "credit_note_without_invoice", owner=ap.name,
               reason=f"{_numbered('Credit note', g.details['invoice_number'])} "
                      f"({money(g.details['gross_total'], g.details['currency'])}) "
                      + (f"references invoice {ref}, which is not known for this supplier" if ref
                         else "references no invoice")
                      + f": credit note without invoice, routed to AP ({ap.name}) to identify the original invoice.",
               cause="Credit note without a known invoice")
        return
    g.details["credit_status"] = "unapplied"
    missing = f"No invoice {ref} on account" if ref else "No invoice referenced; nothing to apply it to on account"
    g.step("credit_note", "unapplied", f"{missing} {g.account.account_id} ({g.account.display_name}): the credit "
                                       "stays unapplied.")


def _purchase_orders(g: _Gate) -> list[PurchaseOrder]:
    """Every extracted PO number that exists in the scenario's ERP (both sides normalised), in printed order."""
    if not g.po_numbers:
        return []
    pos = {normalise_po_number(po.po_number): po for po in g.session.scalars(select(PurchaseOrder).where(
        PurchaseOrder.scenario == g.doc.scenario).order_by(PurchaseOrder.po_number))}
    return [pos[n] for n in g.po_numbers if n in pos]


def _purchase_order(g: _Gate) -> Optional[PurchaseOrder]:
    """The first extracted PO number that exists in the scenario's ERP (both sides normalised), or None."""
    found = _purchase_orders(g)
    return found[0] if found else None


def _supplier_po(g: _Gate, po: PurchaseOrder) -> bool:
    """The PO is the invoicing supplier's: on the resolved vendor account or, when the scenario matches by party
    (to-be), on another account of the same party."""
    if po.vendor_account_id == g.account.account_id:
        return True
    if g.cfg["po_vendor_match"] != "party" or not g.details["party_id"]:
        return False
    po_account = g.session.scalar(select(VendorAccount).where(VendorAccount.scenario == g.doc.scenario,
                                                              VendorAccount.account_id == po.vendor_account_id))
    return po_account is not None and po_account.party_id == g.details["party_id"]


def _usable_po(g: _Gate, po: PurchaseOrder) -> bool:
    """The supplier's PO, and (with the entity check) of the billed legal entity."""
    return _supplier_po(g, po) and (not g.cfg["entity_check"] or po.legal_entity_code == g.details["bill_to_entity"])


def _commitment_match(g: _Gate) -> None:
    if g.is_credit_note:
        g.step("commitment_match", "skipped", "Not applicable to a credit note.", quiet=True)
        return
    if g.details["po_number"]:
        _match_po(g)
    else:
        _match_without_po(g)


def _likely_requester(g: _Gate) -> Optional[str]:
    """The requester of the supplier's latest PO in the billed entity, else its non-PO requester; None if neither."""
    party, entity = g.details["party_id"], g.details["bill_to_entity"]
    if not party:
        return None
    accounts = set(g.session.scalars(select(VendorAccount.account_id).where(
        VendorAccount.scenario == g.doc.scenario, VendorAccount.party_id == party)))
    po = g.session.scalar(select(PurchaseOrder).where(
        PurchaseOrder.scenario == g.doc.scenario, PurchaseOrder.legal_entity_code == entity,
        PurchaseOrder.vendor_account_id.in_(accounts)).order_by(PurchaseOrder.order_date.desc()).limit(1))
    if po is not None:
        return po.requester_name
    key = world.NON_PO_REQUESTERS.get((party, entity))
    return world.PEOPLE[key].name if key else None


def _po_not_found(g: _Gate) -> None:
    """None of the quoted PO numbers is in the ERP: routed to the requester of the supplier's latest PO in the billed
    entity (A3: requester); AP only when the supplier has no PO there (a PO of another supplier goes to its
    requester instead)."""
    numbers = g.po_numbers
    if len(numbers) == 1:
        missing, cause = (f"PO {numbers[0]} quoted on the invoice is not in the ERP",
                          f"PO {numbers[0]} quoted on the invoice was never keyed in the ERP")
    else:
        missing = f"None of the PO numbers quoted on the invoice ({', '.join(numbers)}) is in the ERP"
        cause = f"None of the PO numbers quoted on the invoice ({', '.join(numbers)}) was keyed in the ERP"
    requester = _likely_requester(g)
    who = (f"the requester {requester} (latest PO of this supplier in the billed entity)" if requester
           else f"AP ({world.AP_SPECIALIST.name}), since the supplier has no PO in the billed entity")
    g.fail("commitment_match", "po_not_found", owner=requester or world.AP_SPECIALIST.name,
           reason=f"{missing}: PO not found, routed to {who} to provide the correct PO.", cause=cause)


def _who(name: Optional[str]) -> str:
    """'Jonas Weber (Store development manager DE)': the named owner with the job title, when it is known."""
    person = world.PERSON_BY_NAME.get(name or "")
    return f"{name} ({person.role})" if person else (name or "nobody")


def _match_po(g: _Gate) -> None:
    ap = world.AP_SPECIALIST
    g.details["commitment"] = "none"
    found = _purchase_orders(g)
    if not found:
        _po_not_found(g)
        return
    usable = [po for po in found if _usable_po(g, po)]
    ignored = [po for po in found if po not in usable]
    wrong_entity = [po for po in ignored if _supplier_po(g, po)]  # the supplier's own POs of another legal entity
    if usable and wrong_entity:
        _pos_of_another_entity(g, usable, wrong_entity, [po for po in ignored if po not in wrong_entity])
        return
    if len(usable) > 1:
        _match_pos(g, usable, ignored)
        return
    # None usable: the supplier's own PO (a legal-entity issue) says why before a PO of another supplier.
    po = usable[0] if usable else (wrong_entity or found)[0]
    others = _ignored_sentence([p for p in ignored if p is not po])
    number = g.details["po_number"] = po.po_number
    if not _supplier_po(g, po):
        g.fail("commitment_match", "po_not_found", owner=po.requester_name,
               reason=f"PO {number} belongs to another supplier (account {po.vendor_account_id}): wrong vendor, "
                      f"routed to the requester {_who(po.requester_name)} to provide the correct PO.{others}",
               cause=f"PO {number} belongs to another vendor account")
        return
    if g.cfg["entity_check"] and po.legal_entity_code != g.details["bill_to_entity"]:
        g.fail("commitment_match", "wrong_legal_entity", owner=ap.name,
               reason=f"Billed to {g.entity_label(g.details['bill_to_entity'])} but PO {number} belongs to "
                      f"{po.legal_entity_code}: wrong legal entity, routed to AP ({ap.name}) to ask for a re-issued "
                      f"invoice or re-assign it.{others}", cause=f"PO {number} belongs to another legal entity")
        return
    g.details.update(commitment="po", po_numbers=[number])
    receipts = _receipts(g, number)
    lines = [ln for ln in (g.v("lines") or []) if isinstance(ln, dict)]
    net, currency = g.details["net_total"], g.details["currency"]
    if lines:
        checks, issues = check_po_lines(lines, po, receipts, currency)
        unreadable = lines_total_mismatch(lines, net, currency)
        lines_ok = f"{len(lines)} line{'s' if len(lines) != 1 else ''} within tolerance of PO {number}"
    else:
        checks, issues, unreadable = check_po_header(net, po, receipts, currency)
        lines_ok = f"no invoice lines read, net total within tolerance of the total of PO {number}"
    g.details["line_checks"] = checks
    if unreadable:
        g.fail("commitment_match", "human_review", owner=ap.name,
               reason=f"PO {number}: {unreadable}. Human review: AP checks the invoice lines against the document "
                      f"(AP review: {ap.name}).{others}", cause=f"PO {number}: {unreadable}")
        return
    if _route_receipt_issues(g, [po], issues, checks, others):
        return
    skipped = _ignored_note(ignored)
    g.tolerance = dict(pos=[po], issues=issues, checks=checks, ignored=others, lines_ok=lines_ok)
    if po.category == "goods":
        received = "received in full" if fully_received(po, receipts) else "goods receipt recorded"
        g.posted_note = f"3-way match on PO {number}{skipped}"
        g.step("commitment_match", "ok", f"Goods → PO + receipt: PO {number}{skipped}, {received}.")
    else:
        g.posted_note = f"PO {number}{skipped} + service confirmation"
        g.step("commitment_match", "ok", f"Services → PO + confirmation: PO {number}{skipped}, service confirmation "
                                         "recorded.")


def _route_receipt_issues(g: _Gate, pos: list[PurchaseOrder], issues: list[dict[str, Any]],
                          checks: list[dict[str, Any]], note: str) -> bool:
    """A missing receipt or service confirmation stops the commitment rule (A3: PO exists, no receipt or
    confirmation -> the requester). Returns True when it did."""
    missing = [i for i in issues if i["kind"] == "no_receipt"]
    if not missing:
        return False
    if len(pos) > 1:
        _route_multi_po_issues(g, "commitment_match", pos, missing, checks, note)
    else:
        _route_po_issues(g, "commitment_match", pos[0], missing, note=note.strip())
    return True


def _ignored_note(ignored: list[PurchaseOrder]) -> str:
    """' (PO 4500105 quoted too, but not of this supplier and billed entity)'; '' when no quoted PO was ignored."""
    return f" ({_po_label(ignored)} quoted too, but not of this supplier and billed entity)" if ignored else ""


def _ignored_sentence(ignored: list[PurchaseOrder]) -> str:
    """The same as a sentence appended to an exception reason; '' when no quoted PO was ignored."""
    return f" {_po_label(ignored)} quoted too, but not of this supplier and billed entity." if ignored else ""


def _pos_of_another_entity(g: _Gate, usable: list[PurchaseOrder], wrong: list[PurchaseOrder],
                           others: list[PurchaseOrder]) -> None:
    """The invoice quotes POs of the billed entity and POs of the same supplier in another Velox entity: one invoice
    for two legal entities. Never matched on the billed entity's POs alone: AP asks for one invoice per entity (the
    single-PO entity check routes the same way)."""
    ap, label = world.AP_SPECIALIST, _po_label(wrong)
    verb = "belongs" if len(wrong) == 1 else "belong"
    entities = and_list(sorted({po.legal_entity_code for po in wrong}))
    g.details["po_number"] = wrong[0].po_number
    g.fail("commitment_match", "wrong_legal_entity", owner=ap.name,
           reason=f"Billed to {g.entity_label(g.details['bill_to_entity'])} with {_po_label(usable)}, but the invoice "
                  f"also quotes {label} of the same supplier, which {verb} to {entities}: wrong legal entity, routed "
                  f"to AP ({ap.name}) to ask for one invoice per legal entity or re-assign it."
                  f"{_ignored_sentence(others)}",
           cause=f"{label} quoted on the invoice {verb} to another legal entity")


def _receipts(g: _Gate, po_number: str) -> list[ProductReceipt]:
    return list(g.session.scalars(select(ProductReceipt).where(
        ProductReceipt.scenario == g.doc.scenario, ProductReceipt.po_number == po_number).order_by(ProductReceipt.id)))


def _match_pos(g: _Gate, pos: list[PurchaseOrder], ignored: list[PurchaseOrder]) -> None:
    """Multi-PO invoice: the invoice lines are mapped across the lines of every usable PO and each PO line is
    checked as for a single PO; an issue is routed to the requester / buyer of the PO it is on."""
    ap, label = world.AP_SPECIALIST, _po_label(pos)
    numbers = [po.po_number for po in pos]
    g.details.update(commitment="po", po_number=numbers[0], po_numbers=numbers)
    receipts = {n: _receipts(g, n) for n in numbers}
    lines = [ln for ln in (g.v("lines") or []) if isinstance(ln, dict)]
    net, currency = g.details["net_total"], g.details["currency"]
    if lines:
        checks, issues = check_lines_across_pos(lines, pos, receipts, currency)
        unreadable = lines_total_mismatch(lines, net, currency)
        lines_ok = f"{len(lines)} line{'s' if len(lines) != 1 else ''} within tolerance of {label}"
    else:
        checks, issues, unreadable = check_header_across_pos(net, pos, receipts, currency)
        lines_ok = f"no invoice lines read, net total within tolerance of the total of {label}"
    g.details["line_checks"] = checks
    skipped = _ignored_note(ignored)
    if unreadable:
        g.fail("commitment_match", "human_review", owner=ap.name,
               reason=f"{label}: {unreadable}. Human review: AP checks the invoice lines against the document "
                      f"(AP review: {ap.name}).{_ignored_sentence(ignored)}", cause=f"{label}: {unreadable}")
        return
    if _route_receipt_issues(g, pos, issues, checks, _ignored_sentence(ignored)):
        return
    g.tolerance = dict(pos=pos, issues=issues, checks=checks, ignored=_ignored_sentence(ignored), lines_ok=lines_ok)
    g.posted_note = f"Multi-PO match on {label}{skipped}"
    g.step("commitment_match", "ok", f"{g.posted_note}: a receipt or confirmation is recorded for every line.")


def _route_multi_po_issues(g: _Gate, step: str, pos: list[PurchaseOrder], issues: list[dict[str, Any]],
                           checks: list[dict[str, Any]], ignored: str = "") -> None:
    """Route the highest-priority issue (missing receipt, quantity, price) to the owners of the PO it is on; an
    invoice line on none of the POs goes to the buyer of the first one."""
    priority = ("no_receipt", "quantity", "price")
    first = min(issues, key=lambda i: priority.index(i["kind"]))
    po = next((p for p in pos if p.po_number == first.get("po")), pos[0])
    mine = [i for i in issues if i.get("po") in (po.po_number, None)]
    g.details["po_number"] = po.po_number
    amounts = [c.get("amount") for c in checks if c.get("po_number") == po.po_number and c.get("line") is not None]
    invoiced = (f"{money(round(sum(amounts), 2), g.details['currency'])} invoiced on it"
                if amounts and all(a is not None for a in amounts) else None)
    clean = [p for p in pos if not any(i.get("po") == p.po_number for i in issues)]
    note = f"Invoice lines on {_po_label(pos)}" + (f"; the lines on {_po_label(clean)} match." if clean else ".")
    _route_po_issues(g, step, po, mine, invoiced=invoiced, note=note + ignored)


def _route_po_issues(g: _Gate, step: str, po: PurchaseOrder, issues: list[dict[str, Any]], *,
                     invoiced: Optional[str] = None, note: str = "") -> None:
    """Deck A3: a missing receipt or confirmation -> the requester; a price or quantity mismatch -> the buyer.
    `invoiced` replaces the invoiced amount of the reason and `note` is appended to it (multi-PO invoices)."""
    by_kind = {kind: next((i for i in issues if i["kind"] == kind), None) for kind in ("no_receipt", "quantity", "price")}
    tail = f" {note}" if note else ""
    if by_kind["no_receipt"]:
        text = by_kind["no_receipt"]["text"]
        amount = invoiced or f"{money(g.details['net_total'], g.details['currency'])} invoiced"
        g.fail(step, "po_no_receipt", owner=po.requester_name,
               reason=f"PO {po.po_number} exists but {text} ({amount}): routed to the requester "
                      f"{_who(po.requester_name)} to confirm delivery.{tail}",
               cause=f"PO {po.po_number} exists but {text}")
        return
    issue = by_kind["quantity"] or by_kind["price"]
    kind = "quantity mismatch" if issue["kind"] == "quantity" else "price outside tolerance"
    g.fail(step, "price_qty_mismatch", owner=po.buyer_name,
           reason=f"{issue['text']}: {kind}, routed to the buyer {_who(po.buyer_name)}.{tail}", cause=issue["text"])


def _period_claimed(g: _Gate, contract: Contract, period: str) -> Optional[str]:
    """The earlier document that already claims the contract for the period: one matched to it, or one of the same
    party and billed entity, without a PO, that stopped before the commitment match (e.g. in human review)."""
    for d in g.earlier:
        det = d.details
        if det.get("contract_period") != period:
            continue
        if det.get("contract_id") == contract.contract_id:
            return d.doc_id
        if (det.get("commitment") is None and det.get("doc_type") != "credit_note" and not det.get("po_number")
                and det.get("bill_to_entity") == contract.legal_entity_code and g.same_party(d)):
            return d.doc_id
    return None


def _find_contract(g: _Gate) -> tuple[Optional[Contract], str, Optional[str]]:
    """A recurring contract of the party in the billed entity whose monthly range covers the net amount and
    whose period is not invoiced yet. Returns (contract, why no contract matched, what could not be read when the
    match needs a human check: the net amount, or the invoice date of a contract whose range fits)."""
    contracts = list(g.session.scalars(select(Contract).where(
        Contract.scenario == g.doc.scenario, Contract.party_id == g.details["party_id"],
        Contract.legal_entity_code == g.details["bill_to_entity"], Contract.recurring.is_(True))))
    if not contracts:
        return None, "no recurring contract", None
    amount, period = g.details["net_total"], g.details["contract_period"]
    if amount is None:
        return None, "", "the net amount could not be read"
    why = ""
    for c in contracts:
        if not c.expected_monthly_min <= amount <= c.expected_monthly_max:
            why = (f"{money(amount, g.details['currency'])} is outside the {c.expected_monthly_min:,.2f}–"
                   f"{c.expected_monthly_max:,.2f} monthly range of contract {c.contract_id}")
        elif period is None:  # an unknown period never matches, nor collides with, another one
            return None, "", "the invoice date could not be read"
        elif claimed := _period_claimed(g, c, period):
            why = f"contract {c.contract_id} is already invoiced for {period} ({claimed})"
        else:
            return c, "", None
    return None, why, None


def _match_without_po(g: _Gate) -> None:
    g.details["commitment"] = "none"
    gross, currency, entity = g.details["gross_total"], g.details["currency"], g.details["bill_to_entity"]
    why = "no contract check in the as-is process"
    if g.cfg["contract_matching"]:
        contract, why, unreadable = _find_contract(g)
        if unreadable:
            ap = world.AP_SPECIALIST
            supplier = g.party.canonical_name if g.party else "the supplier"
            g.fail("commitment_match", "human_review", owner=ap.name,
                   reason=f"No PO, and {unreadable}, so the invoice cannot be checked against the recurring contract "
                          f"of {supplier}: Human review, AP reads it from the document (AP review: {ap.name}).",
                   cause=f"No PO and {unreadable}")
            return
        if contract is not None:
            amount, period = g.details["net_total"], g.invoice_date
            g.details.update(commitment="contract", contract_id=contract.contract_id)
            g.posted_note = (f"Recurring contract {contract.contract_id} covers "
                             f"{f'{period:%B %Y}' if period else 'the period'}")
            g.tolerance = dict(pos=[], issues=[], checks=[], ignored="",
                               lines_ok=f"net {money(amount, currency)} inside the {contract.expected_monthly_min:,.2f}–"
                                        f"{contract.expected_monthly_max:,.2f} monthly range of contract "
                                        f"{contract.contract_id}")
            g.step("commitment_match", "ok", f"Recurring → contract schedule: {g.posted_note}; no PO needed.")
            return
    if g.cfg["catalogue_matching"] and _match_catalogue(g):
        return
    requester_key = world.NON_PO_REQUESTERS.get((g.details["party_id"], entity))
    requester = world.PEOPLE[requester_key] if requester_key else None
    owner_contract = g.session.scalar(select(Contract).where(
        Contract.scenario == g.doc.scenario, Contract.party_id == g.details["party_id"],
        Contract.legal_entity_code == entity).limit(1))
    if requester:
        owner, who = requester.name, f"the requester {_who(requester.name)}"
    elif owner_contract is not None:
        owner, who = owner_contract.owner_name, f"the contract owner {_who(owner_contract.owner_name)} as requester"
    else:
        owner, who = world.AP_SPECIALIST.name, f"AP ({world.AP_SPECIALIST.name}), since no requester is on file"
    g.details["requester_name"] = requester.name if requester else None
    g.fail("commitment_match", "no_po", owner=owner, approval=True,
           reason=f"No commitment for {money(gross, currency)}: no PO, no matching contract ({why}) and no catalogue. "
                  f"No PO, routed to {who} to confirm the purchase; then the budget owner approves.",
           cause=f"No PO on the invoice and {why}")


def _match_catalogue(g: _Gate) -> bool:
    """Small store purchase -> card / catalogue (deck slide 9): a catalogue of the supplier in the billed entity whose
    per-invoice limit (group currency, simulated rate) covers the gross. Returns True when it matched."""
    gross, currency = g.details["gross_total"], g.details["currency"]
    rows = list(g.session.scalars(select(Contract).where(
        Contract.scenario == g.doc.scenario, Contract.party_id == g.details["party_id"],
        Contract.legal_entity_code == g.details["bill_to_entity"], Contract.category == "catalogue")))
    chf = world.to_chf(gross, currency)
    for cat in rows:
        if chf is not None and 0 < chf <= cat.expected_monthly_max:
            g.details.update(commitment="catalogue", catalogue_id=cat.contract_id, gross_chf=chf)
            rate = world.CHF_RATES[(currency or "").upper()]
            g.posted_note = f"Card / catalogue {cat.contract_id} ({cat.description})"
            g.tolerance = dict(pos=[], issues=[], checks=[], ignored="",
                               lines_ok=f"{money(gross, currency)} ≈ CHF {chf:,.2f} (simulated rate {rate:.2f}) within "
                                        f"the CHF {cat.expected_monthly_max:,.0f} per-invoice limit of {cat.contract_id}")
            g.step("commitment_match", "ok", f"Small store purchase → card / catalogue: {cat.contract_id}, "
                                             f"{cat.description} (owner {cat.owner_name}).")
            return True
    return False


def _agreed_terms(g: _Gate) -> Optional[int]:
    """Terms agreed with the real supplier: its recurring contract in the billed entity, else the party's."""
    pid = g.details["true_party_id"]
    if not pid:
        return None
    contract = g.session.scalar(select(Contract).where(
        Contract.scenario == g.doc.scenario, Contract.party_id == pid,
        Contract.legal_entity_code == g.details["bill_to_entity"], Contract.recurring.is_(True)).limit(1))
    if contract is not None:
        return contract.payment_terms_days
    return g.session.scalar(select(Party.agreed_terms_days).where(Party.scenario == g.doc.scenario,
                                                                  Party.party_id == pid))


def _terms(g: _Gate) -> None:
    if g.is_credit_note:
        g.step("terms", "skipped", "Not applicable to a credit note.", quiet=True)
        return
    printed = g.v("payment_terms_days")
    printed = int(printed) if printed is not None else None
    master = g.account.payment_terms_days
    g.details.update(invoice_terms_days=printed, agreed_terms_days=_agreed_terms(g))
    if g.cfg["terms_source"] == "master" or printed is None:
        g.details.update(terms_days=master, terms_source="master")
        if g.cfg["terms_source"] == "master" and printed is not None and printed != master:
            g.step("terms", "ok", f"Master terms {master} days apply; the invoice states {printed} days, which is "
                                  "ignored.")
            return
        g.step("terms", "ok", f"Master terms: {master} days.")
        return
    g.details.update(terms_days=printed, terms_source="invoice")
    g.step("terms", "ok", f"AP takes the terms from the invoice: {printed} days (vendor account {master}, agreed "
                          f"{g.details['agreed_terms_days']}).")


def _tolerances(g: _Gate) -> None:
    """Price and quantity against the commitment (the PO lines found by the commitment rule); a mismatch goes to the
    buyer (deck A3). For a contract or catalogue the amount check is the commitment's own range or limit."""
    if g.is_credit_note:
        g.step("tolerances", "skipped", "Not applicable to a credit note.", quiet=True)
        return
    t = g.tolerance
    if t is None:
        g.step("tolerances", "info", "No commitment to compare the amounts with.")
        return
    issues = [i for i in t["issues"] if i["kind"] != "no_receipt"]
    if issues:
        if len(t["pos"]) > 1:
            _route_multi_po_issues(g, "tolerances", t["pos"], issues, t["checks"], t["ignored"])
        else:
            _route_po_issues(g, "tolerances", t["pos"][0], issues, note=t["ignored"].strip())
        return
    g.posted_note += f": {t['lines_ok']}"
    g.details["first_pass_match"] = not g.email_loop
    rule = f" (price and quantity: 2% or {PO_TOLERANCE_ABS:,.2f}, whichever is larger)" if t["pos"] else ""
    g.step("tolerances", "ok", f"{t['lines_ok'][:1].upper()}{t['lines_ok'][1:]}{rule}.")


def _approval_limit(g: _Gate) -> None:
    """Above the approval limit of the billed entity (group currency CHF, simulated rate): Human review by the next
    approver in the matrix (brief v2 section 2; deck A3: Amount above approval limit -> next approver, 2 days)."""
    if g.is_credit_note:
        g.step("approval_limit", "skipped", "Not applicable to a credit note.", quiet=True)
        return
    gross, currency, entity = g.details["gross_total"], g.details["currency"], g.account.legal_entity_code
    rule, chf = world.APPROVAL_MATRIX.get(entity), world.to_chf(gross, currency)
    g.details["gross_chf"] = chf
    if rule is None or chf is None:
        g.step("approval_limit", "info", f"No simulated CHF rate for {currency or 'an unknown currency'} or no "
                                         f"approval rule for {entity}: the limit cannot be checked.")
        return
    rate = world.CHF_RATES[(currency or "").upper()]
    amount = f"{money(gross, currency)} ≈ CHF {chf:,.0f} (simulated rate {rate:.2f})"
    if chf <= rule.limit_chf:
        g.step("approval_limit", "ok", f"{amount} within the CHF {rule.limit_chf:,.0f} approval limit of {entity}.")
        return
    approver = world.PEOPLE[rule.next_approver]
    g.fail("approval_limit", "amount_above_approval_limit", owner=approver.name,
           reason=f"{amount} is above the CHF {rule.limit_chf:,.0f} approval limit of {entity}: Human review by the "
                  f"next approver in the matrix, {_who(approver.name)}.",
           cause=f"Amount above the CHF {rule.limit_chf:,.0f} approval limit")


def _timing(g: _Gate) -> None:
    """Path and simulated cycle time (business days receipt -> posting / resolution; app/sim.py)."""
    if g.email_loop:
        path = "email_loop"
    elif g.outcome in BLOCKING_OUTCOMES:
        path = "exception"
    else:
        path = "touchless" if g.cfg["exception_routing"] else "matched"
    overrun = g.spec.sla_overrun_days if g.spec is not None and path == "exception" else 0
    breakdown = sim.cycle_breakdown(g.doc.scenario, path, channel=g.doc.channel, doc_key=g.key,
                                    sla_days=g.sla_days or 0, approval=g.approval, overrun_days=overrun)
    g.details.update(path=path, cycle_breakdown=breakdown)


def _next_invoice_id(g: _Gate) -> str:
    if g.previous_invoice_id:
        return g.previous_invoice_id
    prefix = f"PVI-{SCENARIO_LETTER[g.doc.scenario]}-"
    ids = g.session.scalars(select(PendingVendorInvoice.invoice_id).where(
        PendingVendorInvoice.scenario == g.doc.scenario))
    n = max((int(i[len(prefix):]) for i in ids if i.startswith(prefix) and i[len(prefix):].isdigit()), default=0)
    return f"{prefix}{n + 1:04d}"


def _post(g: _Gate) -> None:
    d, acc = g.details, g.account
    if g.outcome is None:
        applied_here = g.is_credit_note and d["credit_status"] == "applied" and g.cfg["credit_matching"] == "party"
        g.outcome = "applied_credit" if applied_here else "posted"
    _timing(g)
    days = sum(d["cycle_breakdown"].values())
    posted_on = sim.add_business_days(g.doc.received_on, days)
    duplicate = next((e.doc_id for e in g.earlier if e.details.get("posted") and d["true_party_id"]
                      and e.details.get("true_party_id") == d["true_party_id"]
                      and same_invoice(d["invoice_number"], d["gross_total"], e.details.get("invoice_number"),
                                       e.details.get("gross_total"))), None)
    invoice_date = g.invoice_date
    terms = d["terms_days"]
    due = invoice_date + timedelta(days=terms) if invoice_date and terms is not None else None
    gross = d["gross_total"] or 0.0
    total = -abs(gross) if g.is_credit_note else gross
    status = {"applied": "credit_applied", "unapplied": "unapplied_credit"}.get(d["credit_status"], "pending_payment")
    d.update(posted=True, invoice_id=_next_invoice_id(g), posted_entity=acc.legal_entity_code,
             posted_on=posted_on.isoformat(),
             # An unmapped bill-to is unknown, not a wrong entity.
             wrong_entity_posting=d["bill_to_entity"] is not None and acc.legal_entity_code != d["bill_to_entity"],
             duplicate_posting=duplicate is not None, duplicate_of=d["duplicate_of"] or duplicate,
             terms_variance_paid=bool(d["terms_source"] == "invoice" and d["agreed_terms_days"] is not None
                                      and terms != d["agreed_terms_days"]))
    variance = d["invoice_terms_days"] is not None and d["agreed_terms_days"] is not None and (
        d["invoice_terms_days"] != d["agreed_terms_days"])
    g.session.add(PendingVendorInvoice(
        scenario=g.doc.scenario, invoice_id=d["invoice_id"], doc_id=g.doc.doc_id, legal_entity_code=acc.legal_entity_code,
        vendor_account_id=acc.account_id, invoice_number=(d["invoice_number"] or "")[:40], invoice_date=invoice_date,
        due_date=due, total=total, currency=(d["currency"] or g.entities[acc.legal_entity_code].currency)[:3],
        terms_days=terms, terms_source=d["terms_source"] or "",  # no terms (credit note): no source (NOT NULL)
        po_number=d["po_number"] if d["commitment"] == "po" else None, posted_on=posted_on, status=status,
        flags={"wrong_entity": d["wrong_entity_posting"], "duplicate_of": d["duplicate_of"],
               "terms_variance": variance, "catalogue": d["commitment"] == "catalogue"}))
    g.decided_on = posted_on
    g.reason = _posting_reason(g)
    due_text = f", due {due:%d %b %Y}" if due else ""
    g.step("post", "ok", f"Posted to the ERP (mock) as {d['invoice_id']} on {acc.account_id} "
                         f"({acc.legal_entity_code}), {money(total, d['currency'])}{due_text}, on "
                         f"{posted_on:%a %d %b %Y}.")


def _posting_reason(g: _Gate) -> str:
    d, acc = g.details, g.account
    where = f"{acc.account_id} ({acc.legal_entity_code})"
    extra = ""
    if d["wrong_entity_posting"]:
        extra += f", although it is billed to {g.entity_label(d['bill_to_entity'])}"
    if d["duplicate_posting"]:
        extra += f" — the same invoice was already posted as {d['duplicate_of']}"
    if g.is_credit_note:
        amount = money(-abs(d["gross_total"] or 0.0), d["currency"])
        credit = _numbered("Credit note", d["invoice_number"])
        ref = g.v("referenced_invoice_number") or "no invoice"
        if d["credit_status"] == "applied":
            return f"{credit} ({amount}) applied to invoice {ref} ({d['applied_to']}) and posted to {where}{extra}."
        if not str(g.v("referenced_invoice_number") or "").strip():
            return (f"{credit} ({amount}) references no invoice and was posted to {where}: "
                    f"the credit stays unapplied{extra}.")
        return (f"{credit} ({amount}) posted to {where}, where no invoice {ref} exists: "
                f"the credit stays unapplied{extra}.")
    terms = (f"on the invoice terms ({d['terms_days']} days)" if d["terms_source"] == "invoice"
             else f"on master terms of {d['terms_days']} days")
    if g.email_loop:
        return (f"{g.loop_cause}: AP chased it by email for {d['email_loop_days']} business days with no owner and "
                f"no SLA, then posted it to {where} {terms}{extra}.")
    note = g.posted_note or "Matched"
    who = "posted with no human touch" if g.cfg["exception_routing"] else "AP keyed it and posted it"
    return f"{note}; {who} to {where} {terms}{extra}."


# --------------------------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------------------------

_PIPELINE: tuple[tuple[str, Callable[[_Gate], None]], ...] = (
    ("register", _register), ("screen", _screen), ("extract", _extract), ("confidence", _confidence),
    ("document_type", _document_type), ("resolve_vendor", _resolve_vendor), ("legal_entity", _legal_entity),
    ("duplicate_check", _duplicate_check), ("credit_note", _credit_note), ("commitment_match", _commitment_match),
    ("terms", _terms), ("tolerances", _tolerances), ("approval_limit", _approval_limit), ("post", _post),
)


def applies(scenario: str, step: str) -> bool:
    """The step is part of the scenario's process (e.g. the as-is has no screening, confidence or approval rule)."""
    flag = STEP_FLAGS.get(step)
    return flag is None or SCENARIOS[scenario][flag] not in (None, False)


def _earlier_decisions(session: Session, doc: InboundDocument) -> list[GateDecision]:
    """Decisions of the scenario's documents that come before `doc` in processing order (earliest first)."""
    docs = {d.doc_id: d for d in session.scalars(select(InboundDocument).where(
        InboundDocument.scenario == doc.scenario))}
    key = order_key(doc)
    rows = session.scalars(select(GateDecision).where(GateDecision.scenario == doc.scenario,
                                                      GateDecision.doc_id != doc.doc_id))
    earlier = [r for r in rows if r.doc_id in docs and order_key(docs[r.doc_id]) < key]
    return sorted(earlier, key=lambda r: order_key(docs[r.doc_id]))


def _delete_results(session: Session, doc: InboundDocument) -> Optional[str]:
    """Delete the document's decision, posting and credit application (and an account the as-is tool created
    for it, if nothing else uses it). Returns the previous invoice_id, reused by the next posting."""
    old = list(session.scalars(select(GateDecision).where(GateDecision.doc_id == doc.doc_id)))
    created = {o.details.get("account_id") for o in old if o.details.get("resolution_method") == "created"}
    previous = next((o.details.get("invoice_id") for o in old if o.details.get("invoice_id")), None)
    for model in (GateDecision, PendingVendorInvoice, CreditNoteApplication):
        session.execute(delete(model).where(model.doc_id == doc.doc_id))
    used = {r.details.get("account_id") for r in session.scalars(select(GateDecision).where(
        GateDecision.scenario == doc.scenario))}
    for account_id in created - used:
        session.execute(delete(VendorAccount).where(VendorAccount.scenario == doc.scenario,
                                                    VendorAccount.account_id == account_id,
                                                    VendorAccount.created_by == ON_THE_FLY_CREATOR))
    session.flush()
    return previous


def net_amount(data: dict[str, Any]) -> Optional[float]:
    """The net total as read, else gross - tax (both read), else the sum of the line amounts; None if unknown."""
    net = to_float(value(data, "net_total"))
    if net is not None:
        return net
    gross, tax = to_float(value(data, "gross_total")), to_float(value(data, "tax_total"))
    if gross is not None and tax is not None:
        return round(gross - tax, 2)
    amounts = [line_amount(ln) if isinstance(ln, dict) else None for ln in (value(data, "lines") or [])]
    return round(sum(amounts), 2) if amounts and all(a is not None for a in amounts) else None


def _prepare(g: _Gate) -> None:
    """Fill the details that come straight from the extraction and the seed (bill-to, true party, lookup party,
    PO numbers, invoice date, net amount)."""
    scenario, d = g.doc.scenario, g.details
    g.entities = {e.code: e for e in g.session.scalars(select(LegalEntity).where(LegalEntity.scenario == scenario))}
    if not g.data:
        return
    g.po_numbers = list(dict.fromkeys(n for n in map(normalise_po_number, g.v("po_numbers") or []) if n))
    g.invoice_date = parse_date(g.v("invoice_date"))
    d.update(supplier_name=g.v("supplier_name"), invoice_number=g.v("invoice_number"),
             invoice_number_norm=normalise_invoice_number(g.v("invoice_number")) or None,
             gross_total=to_float(g.v("gross_total")), net_total=net_amount(g.data),
             currency=normalise_currency(g.v("currency")),
             po_number=g.po_numbers[0] if g.po_numbers else None,
             invoice_date=g.invoice_date.isoformat() if g.invoice_date else None,
             contract_period=contract_period(g.invoice_date) if d["doc_type"] in ("invoice", "reminder") else None,
             bill_to_entity=map_bill_to(g.v("bill_to_name"), g.v("bill_to_vat_id"),
                                        [(e.code, e.name, e.vat_id) for e in g.entities.values()]))
    parties = list(g.session.scalars(select(Party).where(Party.scenario == scenario)))
    linked = list(g.session.scalars(select(VendorAccount).where(VendorAccount.scenario == scenario,
                                                                VendorAccount.party_id.is_not(None))))
    d["true_party_id"] = true_party(g.data, parties, linked)
    if g.cfg["vendor_resolution"] == "party_identifiers":  # without side effects, whatever step it stops at
        active = list(g.session.scalars(select(VendorAccount).where(
            VendorAccount.scenario == scenario, VendorAccount.status == "active").order_by(VendorAccount.account_id)))
        d["lookup_party_id"] = find_party(g.data, active, parties)[0]


def _touchless(g: _Gate) -> bool:
    """Posted with no human step (deck A6): a to-be Post. Never in the as-is, where AP keys every document; a Block is
    never posted (AP replies with the status), an Exception or Human review is posted after a human step."""
    return bool(g.cfg["exception_routing"]) and g.outcome in ("posted", "applied_credit")


def outcome_line(g: _Gate) -> str:
    """The last rule-log line of a document: '[B-05] Outcome → Exception — PO exists, no receipt or confirmation ·
    Jonas Weber · SLA 2 days'."""
    word = OUTCOME_WORDS[g.doc.scenario].get(g.outcome or "", g.outcome or "")
    parts = []
    if g.cfg["exception_routing"] and g.exception_type:
        parts.append(taxonomy.label(g.exception_type))
        if g.owner_name:
            parts.append(g.owner_name)
        parts.append("SLA —" if g.sla_days is None else f"SLA {g.sla_days} day{'s' if g.sla_days != 1 else ''}")
    elif g.email_loop:
        parts.append(f"{g.details['email_loop_days']} days of untracked follow-up, then posted by AP")
    days = sum(g.details["cycle_breakdown"].values())
    parts.append(f"{days} simulated business day{'s' if days != 1 else ''}")
    return log_line(g.doc.doc_id, "Outcome", word, " · ".join(parts))


def process(session: Session, doc: InboundDocument, *, log: Log = print) -> GateDecision:
    """Run one document through the gate and record its GateDecision (plus posting / credit application).

    Assumes the documents before it in processing order are processed. Re-processing replaces its results.
    """
    previous = _delete_results(session, doc)
    extraction = doc.extraction.json if doc.extraction is not None else {}
    g = _Gate(session=session, doc=doc, log=log, data=extraction or {}, details=_blank_details(doc))
    g.previous_invoice_id = previous
    g.earlier = _earlier_decisions(session, doc)
    g.decided_on = None
    _prepare(g)
    for name, step in _PIPELINE:
        if not applies(doc.scenario, name):
            continue
        if g.stopped_at:
            stopped = STEP_LABELS[doc.scenario].get(g.stopped_at, g.stopped_at.replace("_", " "))
            g.step(name, "skipped", f"Not reached: the document stopped at {stopped}.", quiet=True)
        else:
            step(g)
    if not g.details["posted"]:
        _timing(g)
        g.decided_on = g.doc.registered_on or g.doc.received_on
        if g.email_loop:
            g.reason = (f"{g.loop_cause}: AP follows up by email for {g.details['email_loop_days']} business days, "
                        "with no owner and no SLA.")
    d = g.details
    d["touchless"] = _touchless(g)
    if g.is_credit_note and d["credit_status"]:
        session.add(CreditNoteApplication(
            scenario=doc.scenario, credit_note_id=(d["invoice_number"] or doc.doc_id)[:20], doc_id=doc.doc_id,
            applied_to_invoice_id=d["applied_to"] if d["credit_status"] == "applied" else None,
            status=d["credit_status"]))
    days = sum(d["cycle_breakdown"].values())
    decision = GateDecision(doc_id=doc.doc_id, scenario=doc.scenario, steps=g.steps, outcome=g.outcome,
                            exception_type=g.exception_type, owner_role=g.owner_role, owner_name=g.owner_name,
                            sla_days=g.sla_days, reason=g.reason, simulated_days=days, details=d,
                            decided_on=g.decided_on)
    session.add(decision)
    session.commit()
    g.log(outcome_line(g))
    return decision


def clear_results(session: Session, scenario: str) -> None:
    """Remove the gate's results for a scenario: decisions, postings, credit applications, runs and the vendor
    accounts the as-is tool created on the fly. As-is documents go back to 'not registered yet'."""
    for model in (GateDecision, PendingVendorInvoice, CreditNoteApplication, Run):
        session.execute(delete(model).where(model.scenario == scenario))
    session.execute(delete(VendorAccount).where(VendorAccount.scenario == scenario,
                                                VendorAccount.created_by == ON_THE_FLY_CREATOR))
    if SCENARIOS[scenario]["registration"] == "delayed":
        for doc in session.scalars(select(InboundDocument).where(InboundDocument.scenario == scenario)):
            doc.registered, doc.registered_on = False, None
    session.commit()


def run_scenario(session: Session, scenario: str, *, allow_api: bool = False, log: Optional[Log] = None) -> Run:
    """Extract what is missing, clear the scenario's results, process every document in processing order and store
    a Run with the step log and the counts. The extraction comes first: if it raises, the previous results stay."""
    emit = log or print
    started = datetime.now()  # wall-clock time: run metadata only
    docs = list(session.scalars(select(InboundDocument).where(InboundDocument.scenario == scenario)))
    extraction = extract.extract_documents(session, [d for d in docs if d.extraction is None], allow_api=allow_api)
    clear_results(session, scenario)
    lines: list[str] = []

    def collect(line: str) -> None:
        lines.append(line)
        emit(line)

    decisions = [process(session, doc, log=collect) for doc in sorted(docs, key=order_key)]
    outcomes = Counter(d.outcome for d in decisions)
    touchless = sum(1 for d in decisions if d.details["touchless"])
    exceptions = sum(1 for d in decisions if d.outcome in BLOCKING_OUTCOMES)
    words = Counter(OUTCOME_WORDS[scenario].get(d.outcome or "", d.outcome or "") for d in decisions)
    order = list(dict.fromkeys(OUTCOME_WORDS[scenario].values()))  # Post, Exception, Block, Human review
    collect(f"[run] {SCENARIO_LETTER[scenario]}: {len(decisions)} documents · "
            + " · ".join(f"{w} {words[w]}" for w in sorted(words, key=lambda w: (w not in order, order.index(w)
                                                                                    if w in order else 0, w)))
            + f" · posted with no human touch {touchless}")
    run = Run(run_id=f"RUN-{SCENARIO_LETTER[scenario]}-{started:%Y%m%d-%H%M%S-%f}", scenario=scenario,
              started_on=started, finished_on=datetime.now(),
              summary_json={"log": lines, "outcomes": dict(outcomes), "documents": len(decisions),
                            "touchless": touchless, "exceptions": exceptions, "extraction": extraction})
    session.add(run)
    session.commit()
    return run


def rerun_document(session: Session, doc: InboundDocument, *, allow_api: bool = False,
                   log: Optional[Log] = None) -> GateDecision:
    """Process the document again: its decision, posting and credit application are replaced. Every earlier
    document of the scenario (processing order) that has no decision yet is processed first, in order, so the
    duplicate, credit-note and contract checks see them. Documents without an extraction are extracted first.
    Returns the document's decision."""
    emit = log or print
    decided = set(session.scalars(select(GateDecision.doc_id).where(GateDecision.scenario == doc.scenario)))
    key = order_key(doc)
    pending = sorted((d for d in session.scalars(select(InboundDocument).where(
        InboundDocument.scenario == doc.scenario, InboundDocument.doc_id != doc.doc_id))
        if d.doc_id not in decided and order_key(d) < key), key=order_key)
    missing = [d for d in [*pending, doc] if d.extraction is None]
    if missing:
        extract.extract_documents(session, missing, allow_api=allow_api)
    for earlier in pending:
        process(session, earlier, log=emit)
    return process(session, doc, log=emit)
