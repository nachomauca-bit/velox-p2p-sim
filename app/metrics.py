"""KPIs (brief section 12): the same definitions as the case deck, computed from the gate decisions.

Upstream "process health": PO / contract coverage, vendor master quality, registration lag.
Downstream "automation efficiency": touchless rate, exceptions by type, duplicates, credit notes,
wrong-entity postings, terms variance, simulated cash leakage and cycle time.

Every KPI is a dict {key, label, value, display, formula, group, unit}. `formula` is the plain-English
definition (shown on hover or as a footnote), followed by the numbers behind the value. Percentages, ratios and
days are rounded to one decimal, money to two. KPIs that need a run are None until the scenario has run.

No currency conversion: cash leakage is summed per currency; `value` is the EUR part and `display` lists
every currency (all amounts involved in the sample are EUR).
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Sequence
from datetime import date, datetime, timedelta
from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import config, gate, normalize, seed, sim, taxonomy
from app.models import (Contract, Extraction, GateDecision, InboundDocument, Party, PurchaseOrder, Run,
                        VendorAccount)

NA = "—"
EXCEPTION_OUTCOMES = ("exception", "human_review")
OUTCOMES = ("posted", "exception", "human_review", "blocked_duplicate", "applied_credit")
OUTCOME_LABELS = {
    "posted": "Posted",
    "exception": "Exception",
    "human_review": "Human review",
    "blocked_duplicate": "Blocked duplicate",
    "applied_credit": "Credit applied",
}

# key -> (label, group, unit), in display order.
KPI_DEFS: dict[str, tuple[str, str, str]] = {
    "po_contract_coverage": ("PO / contract coverage", "upstream", "%"),
    "accounts_per_supplier": ("Vendor accounts per supplier", "upstream", "ratio"),
    "pct_accounts_vat_iban": ("Accounts with VAT ID and IBAN", "upstream", "%"),
    "pct_accounts_terms_ok": ("Accounts with the agreed payment terms", "upstream", "%"),
    "registration_lag_days": ("Registration lag", "upstream", "days"),
    "touchless": ("Touchless documents", "downstream", "count"),
    "touchless_rate": ("Touchless rate", "downstream", "%"),
    "exceptions": ("Exceptions", "downstream", "count"),
    "exception_rate": ("Exception rate", "downstream", "%"),
    "duplicates_blocked": ("Duplicates blocked", "downstream", "count"),
    "duplicate_postings": ("Duplicate postings", "downstream", "count"),
    "duplicate_invoices": ("Invoices posted more than once", "downstream", "count"),
    "credit_notes_applied": ("Credit notes applied", "downstream", "count"),
    "credit_notes_unapplied": ("Credit notes unapplied", "downstream", "count"),
    "wrong_entity_postings": ("Wrong-entity postings", "downstream", "count"),
    "non_invoice_postings": ("Non-invoice documents posted", "downstream", "count"),
    "terms_variance_paid": ("Posted on non-agreed payment terms", "downstream", "count"),
    "cash_leakage_amount": ("Simulated cash leakage", "downstream", "EUR"),
    "avg_cycle_days": ("Average cycle time (sample)", "downstream", "days"),
    "avg_cycle_followup_days": ("Average cycle time, documents with a human step", "downstream", "days"),
    "reference_nonpo_store_days": ("Reference path: non-PO invoice sent to a store", "downstream", "days"),
}

# Plain-English names of the sim.cycle_breakdown activities (reference path formula).
ACTIVITY_LABELS = {
    "store_forwarding": "store forwarding",
    "ap_open_and_key": "AP opening ap@ (the quick-fix tool keys it)",
    "email_loop": "email loop",
    "email_approval": "email approval",
    "posting": "posting",
    "registration": "registration",
    "extraction_and_gate": "extraction and gate",
    "exception_sla": "no-PO exception SLA (requester, assumed met)",
    "workflow_approval": "workflow approval",
}


# --------------------------------------------------------------------------------------------
# Formatting and arithmetic helpers
# --------------------------------------------------------------------------------------------


def pct(part: int, whole: int) -> Optional[float]:
    return round(100 * part / whole, 1) if whole else None


def mean(values: Sequence[float]) -> Optional[float]:
    return round(sum(values) / len(values), 1) if values else None


def fmt_pct(value: Optional[float]) -> str:
    return NA if value is None else f"{value:.1f}%"


def fmt_days(value: Optional[float], decimals: int = 1) -> str:
    return NA if value is None else f"{value:.{decimals}f} days"


def fmt_money(value: Optional[float], currency: str = "EUR") -> str:
    return NA if value is None else f"{value:,.2f} {currency}"


def fmt_number(value: Optional[float], decimals: int = 0) -> str:
    return NA if value is None else f"{value:,.{decimals}f}"


def fmt_amounts(amounts: dict[str, float]) -> str:
    """'29,646.00 EUR' or, with several currencies, '1,800.00 EUR + 500.00 USD' (EUR first; never converted)."""
    if not amounts:
        return fmt_money(0.0)
    order = sorted(amounts, key=lambda c: (c != "EUR", c))
    return " + ".join(fmt_money(amounts[c], c) for c in order)


def scenario_config(scenario: str) -> dict[str, Any]:
    """The gate's scenario switches: every as-is / to-be difference is read from gate.SCENARIOS."""
    return gate.SCENARIOS[scenario]


# --------------------------------------------------------------------------------------------
# Pure functions over gate decisions (GateDecision rows or anything with the same attributes)
# --------------------------------------------------------------------------------------------


def detail(decision: GateDecision, key: str, default: Any = None) -> Any:
    value = (decision.details or {}).get(key)
    return default if value is None else value


def is_exception(decision: GateDecision) -> bool:
    """Blocking exceptions, human reviews and (as-is) the untracked email loop; not info flags."""
    return decision.outcome in EXCEPTION_OUTCOMES


def exception_key(decision: GateDecision) -> str:
    return decision.exception_type or decision.outcome


def ordered_counts(counter: Counter, order: Iterable[str]) -> dict[str, int]:
    """Counter as a dict in a fixed order (known keys first, then the rest alphabetically)."""
    order = list(order)
    keys = [k for k in order if k in counter] + sorted(k for k in counter if k not in order)
    return {k: counter[k] for k in keys}


def exceptions_by_type(decisions: Iterable[GateDecision]) -> dict[str, int]:
    counts = Counter(exception_key(d) for d in decisions if is_exception(d))
    return ordered_counts(counts, (t.key for t in taxonomy.EXCEPTION_TYPES))


def info_flags_by_type(decisions: Iterable[GateDecision]) -> dict[str, int]:
    counts = Counter(f.get("type") for d in decisions for f in detail(d, "flags", []) if f.get("type"))
    return ordered_counts(counts, (t.key for t in taxonomy.EXCEPTION_TYPES))


def outcome_counts(decisions: Iterable[GateDecision]) -> dict[str, int]:
    return ordered_counts(Counter(d.outcome for d in decisions), OUTCOMES)


def invoice_key(decision: GateDecision) -> Optional[tuple[str, str, str]]:
    """(real supplier, document type, normalised number) identifying one supplier document, or None."""
    number = detail(decision, "invoice_number_norm") or normalize.normalise_invoice_number(
        detail(decision, "invoice_number"))
    supplier = detail(decision, "true_party_id") or normalize.normalise_name(detail(decision, "supplier_name"))
    if not number or not supplier:
        return None
    return supplier, detail(decision, "doc_type", ""), number


def duplicate_groups(decisions: Iterable[GateDecision]) -> list[list[GateDecision]]:
    """Postings of the same invoice (same real supplier and normalised number) posted more than once, in
    processing order: the first posting of each group is legitimate, the others are the duplicates."""
    groups: dict[tuple[str, str, str], list[GateDecision]] = {}
    for d in decisions:
        key = invoice_key(d)
        if detail(d, "posted", False) and key:
            groups.setdefault(key, []).append(d)
    return [g for g in groups.values() if len(g) > 1]


def gross(decision: GateDecision) -> float:
    return abs(float(detail(decision, "gross_total", 0.0)))


def currency_of(decision: GateDecision) -> str:
    return str(detail(decision, "currency", "")).strip().upper() or "n/a"


def gross_by_currency(decisions: Iterable[GateDecision]) -> dict[str, float]:
    """Sum of |gross| per currency (never converted)."""
    amounts: dict[str, float] = {}
    for d in decisions:
        cur = currency_of(d)
        amounts[cur] = round(amounts.get(cur, 0.0) + gross(d), 2)
    return amounts


def repeated_postings(decisions: Sequence[GateDecision]) -> list[GateDecision]:
    """Every posting of a duplicated invoice after the first one."""
    return [d for group in duplicate_groups(decisions) for d in group[1:]]


def unapplied_credits(decisions: Sequence[GateDecision]) -> list[GateDecision]:
    return [d for d in decisions if detail(d, "credit_status") == "unapplied"]


def non_invoice_postings(decisions: Sequence[GateDecision]) -> list[GateDecision]:
    """Documents that are not invoices (doc_type "other", e.g. a supplier statement) posted as if they were."""
    return [d for d in decisions if detail(d, "posted", False) and detail(d, "doc_type") == "other"]


def leakage_postings(decisions: Sequence[GateDecision]) -> list[GateDecision]:
    """Repeated postings + unapplied credit notes + non-invoice postings, each decision once."""
    out: dict[int, GateDecision] = {}
    for d in repeated_postings(decisions) + unapplied_credits(decisions) + non_invoice_postings(decisions):
        out.setdefault(id(d), d)
    return list(out.values())


def cash_leakage(decisions: Sequence[GateDecision]) -> dict[str, float]:
    """Per currency: gross of every repeated posting of a duplicated invoice + |gross| of unapplied credit notes +
    gross of non-invoice documents posted as invoices. Terms variance is a count only (brief section 12), so it adds
    nothing here."""
    return gross_by_currency(leakage_postings(decisions))


def po_key(value: Any) -> str:
    """The PO number as the gate compares it (normalize.normalise_po_number), so coverage counts exactly the POs
    the gate finds: 'PO 4500117', 'po-4500117' and '4500117' -> '4500117'."""
    return normalize.normalise_po_number(value)


def has_commitment(po_numbers: Iterable[str], party_id: Optional[str], entity: Optional[str], *,
                   existing_pos: set[str], contracts: set[tuple[str, str]], use_contracts: bool) -> bool:
    """A usable commitment at arrival: a printed PO number that exists in the ERP, or (when the scenario uses
    contracts) a recurring contract for the real supplier and the billed entity."""
    if any(po_key(p) in existing_pos for p in po_numbers):
        return True
    return use_contracts and (party_id, entity) in contracts


def as_int(value: Optional[float]) -> Optional[int]:
    return None if value is None else int(round(value))


# --------------------------------------------------------------------------------------------
# Vendor master quality (same logic as the vendor master page's flags)
# --------------------------------------------------------------------------------------------


def find_party(acc: VendorAccount, parties: Sequence[Party]) -> tuple[Optional[Party], Optional[str]]:
    """Linked party; for unlinked accounts the party with the same VAT ID, else a matching name."""
    if acc.party_id:
        return next((p for p in parties if p.party_id == acc.party_id), None), None
    vat = normalize.normalise_vat(acc.vat_id)
    if vat:
        for p in parties:
            if normalize.normalise_vat(p.vat_id) == vat:
                return p, "VAT ID"
    for p in parties:
        if normalize.names_match(acc.display_name, p.canonical_name):
            return p, "name"
    return None, None


def agreed_terms(party: Optional[Party], entity: str,
                 contracts: Sequence[Contract]) -> tuple[Optional[int], Optional[str]]:
    """Terms of the recurring contract for party + entity, else the party's agreed terms."""
    if party is None:
        return None, None
    for c in contracts:
        if c.party_id == party.party_id and c.legal_entity_code == entity and c.recurring:
            return c.payment_terms_days, c.contract_id
    return party.agreed_terms_days, "supplier agreement"


def duplicate_reason(a: VendorAccount, b: VendorAccount) -> Optional[str]:
    """Why b looks like a duplicate of a, or None. One party with one account per legal entity is by design."""
    if a.party_id and a.party_id == b.party_id and a.legal_entity_code != b.legal_entity_code:
        return None
    vat_a = normalize.normalise_vat(a.vat_id)
    if vat_a and vat_a == normalize.normalise_vat(b.vat_id):
        return "same VAT ID"
    if normalize.names_match(a.display_name, b.display_name):
        return "similar name"
    return None


def vendor_master_stats(accounts: Sequence[VendorAccount], parties: Sequence[Party],
                        contracts: Sequence[Contract]) -> dict[str, Any]:
    """Accounts per supplier, completeness of identifiers, agreed terms and possible duplicates."""
    n = len(accounts)
    with_ids = sum(1 for a in accounts if a.vat_id and a.iban)
    terms_ok = terms_differ = 0
    for acc in accounts:
        agreed, _ = agreed_terms(find_party(acc, parties)[0], acc.legal_entity_code, contracts)
        terms_ok += agreed is not None and acc.payment_terms_days == agreed
        terms_differ += agreed is not None and acc.payment_terms_days != agreed
    duplicates = sum(1 for acc in accounts if any(other is not acc and duplicate_reason(acc, other)
                                                  for other in accounts))
    return {
        "accounts": n,
        "parties": len(parties),
        "ratio": round(n / len(parties), 1) if parties else None,
        "active": sum(1 for a in accounts if a.status == "active"),
        "with_vat_iban": with_ids,
        "pct_vat_iban": pct(with_ids, n),
        "terms_ok": terms_ok,
        "terms_differ": terms_differ,
        "pct_terms_ok": pct(terms_ok, n),
        "duplicates_flagged": duplicates,
    }


def vendor_master_quality(session: Session, scenario: str) -> dict[str, Any]:
    accounts = session.scalars(select(VendorAccount).where(VendorAccount.scenario == scenario)
                               .order_by(VendorAccount.account_id)).all()
    parties = session.scalars(select(Party).where(Party.scenario == scenario).order_by(Party.party_id)).all()
    contracts = session.scalars(select(Contract).where(Contract.scenario == scenario)).all()
    return vendor_master_stats(list(accounts), list(parties), list(contracts))


# --------------------------------------------------------------------------------------------
# Reference path (brief section 10): a non-PO invoice sent to a store
# --------------------------------------------------------------------------------------------


def reference_nonpo_store_path(scenario: str) -> dict[str, float]:
    """Business days per activity for a non-PO invoice sent to a store (a reference, not the sample).

    Without exception routing (as-is) it goes through the email loop, counted at its mean ((min + max) / 2);
    with routing (to-be) it is a no_po exception: the SLA of the requester (assumed met) plus a workflow approval.
    """
    if scenario_config(scenario)["exception_routing"]:
        steps = sim.cycle_breakdown(scenario, "exception", channel="store_mailbox", doc_key="reference",
                                    sla_days=taxonomy.get("no_po").sla_days or 0, approval=True)
        return {k: float(v) for k, v in steps.items()}
    d = sim.DURATIONS[scenario]
    steps = {k: float(v) for k, v in sim.cycle_breakdown(scenario, "email_loop", channel="store_mailbox",
                                                         doc_key="reference").items()}
    steps["email_loop"] = (d["email_loop_min"] + d["email_loop_max"]) / 2
    return steps


def _reference_formula(scenario: str, steps: dict[str, float]) -> str:
    parts = []
    for key, days in steps.items():
        label = ACTIVITY_LABELS.get(key, key.replace("_", " "))
        if key == "email_loop":
            d = sim.DURATIONS[scenario]
            label += f" (mean of {d['email_loop_min']}–{d['email_loop_max']})"
        parts.append(f"{label} {days:g}")
    text = (f"Reference path, not the sample average: {' + '.join(parts)} = {sum(steps.values()):g} business days "
            "for a non-PO invoice sent to a store.")
    limit = scenario_config(scenario).get("doa_auto_approve_limit")
    if limit:
        text += (f" Under the {limit:,.0f} EUR delegation-of-authority limit it is auto-approved and posted "
                 "the same day (0).")
    else:
        text += " The case reports 26 business days on average."
    return text


# --------------------------------------------------------------------------------------------
# compute / compare
# --------------------------------------------------------------------------------------------


def _kpi(key: str, value: Any, display: str, formula: str) -> dict[str, Any]:
    label, group, unit = KPI_DEFS[key]
    return {"key": key, "label": label, "value": value, "display": display, "formula": formula,
            "group": group, "unit": unit}


def is_sample(decision: GateDecision) -> bool:
    """One of the sample documents (sample_no > 0); webhook documents have sample_no 0."""
    return int(detail(decision, "sample_no", 0) or 0) > 0


def _decisions(session: Session, scenario: str, sample_only: bool = False) -> list[GateDecision]:
    rows = list(session.scalars(select(GateDecision).where(GateDecision.scenario == scenario)
                                .order_by(GateDecision.id)))
    return [d for d in rows if is_sample(d)] if sample_only else rows


def _documents_total(session: Session, scenario: str, sample_only: bool = False) -> int:
    """Inbound documents of the scenario (processed or not); only the sample documents with sample_only."""
    query = select(func.count()).select_from(InboundDocument).where(InboundDocument.scenario == scenario)
    if sample_only:
        query = query.where(InboundDocument.sample_no > 0)
    return int(session.scalar(query) or 0)


def _printed_po_numbers(session: Session, decisions: Sequence[GateDecision]) -> dict[str, list[str]]:
    """PO numbers printed on each document (from its extraction; else the PO the gate used)."""
    doc_ids = [d.doc_id for d in decisions]
    extracted = {e.doc_id: (e.json or {}).get("po_numbers", {}).get("value") or []
                 for e in session.scalars(select(Extraction).where(Extraction.doc_id.in_(doc_ids)))}
    out = {}
    for d in decisions:
        fallback = [detail(d, "po_number")] if detail(d, "po_number") else []
        out[d.doc_id] = extracted.get(d.doc_id, fallback)
    return out


def _coverage(session: Session, scenario: str, decisions: Sequence[GateDecision]) -> tuple[int, int]:
    """(invoices with a usable commitment at arrival, invoices). Credit notes are excluded."""
    invoices = [d for d in decisions if detail(d, "doc_type") == "invoice"]
    existing = {po_key(n) for n in session.scalars(select(PurchaseOrder.po_number)
                                                   .where(PurchaseOrder.scenario == scenario))}
    contracts = {(c.party_id, c.legal_entity_code) for c in session.scalars(
        select(Contract).where(Contract.scenario == scenario, Contract.recurring.is_(True)))}
    printed = _printed_po_numbers(session, invoices)
    use_contracts = bool(scenario_config(scenario)["contract_matching"])
    covered = sum(1 for d in invoices if has_commitment(
        printed[d.doc_id], detail(d, "true_party_id"), detail(d, "bill_to_entity"),
        existing_pos=existing, contracts=contracts, use_contracts=use_contracts))
    return covered, len(invoices)


def _lag_breakdown(lags: Sequence[int]) -> str:
    """'(11 × 1 + 3 × 7) ÷ 14'."""
    counts = Counter(lags)
    return f"({' + '.join(f'{n} × {lag}' for lag, n in sorted(counts.items()))}) ÷ {len(lags)}"


def compute(session: Session, scenario: str, *, sample_only: bool = False) -> dict[str, Any]:
    """All KPIs of one scenario, plus the counts behind the charts. See the module docstring.

    "documents" is the number of decisions (the KPI denominators), "documents_total" the number of inbound
    documents of the scenario: fewer decisions than documents means the scenario was only partly processed.
    sample_only=True keeps the sample documents only (sample_no > 0), for decisions and denominators alike, so
    two scenarios are compared on the same documents even when one of them also received webhook uploads.
    """
    decisions = _decisions(session, scenario, sample_only)
    available = bool(decisions)
    run = session.scalars(select(Run).where(Run.scenario == scenario)
                          .order_by(Run.started_on.desc(), Run.id.desc())).first()
    vm = vendor_master_quality(session, scenario)
    n = len(decisions)
    routing = bool(scenario_config(scenario)["exception_routing"])
    use_contracts = bool(scenario_config(scenario)["contract_matching"])
    kpis: dict[str, dict[str, Any]] = {}

    def add(key: str, value: Any, display: str, formula: str, numbers: str = "") -> None:
        """Run-dependent KPI; the numbers behind the value are appended once the scenario has run."""
        kpis[key] = _kpi(key, value, display, f"{formula} {numbers}" if available and numbers else formula)

    # ---- upstream: process health --------------------------------------------------------------
    commitment = ("A commitment is the PO number printed on the invoice existing in the ERP, or a recurring "
                  "contract for the supplier and the billed entity." if use_contracts else
                  "A commitment is the PO number printed on the invoice existing in the ERP; this process does "
                  "not use contracts.")
    covered, invoices = _coverage(session, scenario, decisions) if available else (0, 0)
    coverage = pct(covered, invoices) if available else None
    add("po_contract_coverage", coverage, fmt_pct(coverage),
        f"Invoices with a usable commitment at arrival ÷ invoices × 100 (credit notes excluded). {commitment}",
        f"Here: {covered} of {invoices} invoices.")
    kpis["accounts_per_supplier"] = _kpi(
        "accounts_per_supplier", vm["ratio"], fmt_number(vm["ratio"], 1),
        f"Vendor accounts (including inactive ones) ÷ suppliers (parties): {vm['accounts']} ÷ {vm['parties']}.")
    kpis["pct_accounts_vat_iban"] = _kpi(
        "pct_accounts_vat_iban", vm["pct_vat_iban"], fmt_pct(vm["pct_vat_iban"]),
        "Vendor accounts with both a VAT ID and an IBAN / bank account ÷ vendor accounts × 100: "
        f"{vm['with_vat_iban']} of {vm['accounts']}.")
    kpis["pct_accounts_terms_ok"] = _kpi(
        "pct_accounts_terms_ok", vm["pct_terms_ok"], fmt_pct(vm["pct_terms_ok"]),
        "Vendor accounts whose payment terms equal the agreed terms (the recurring contract for that supplier "
        "and entity, else the supplier agreement) ÷ vendor accounts × 100. Unlinked accounts are matched to a "
        f"supplier by VAT ID, then by name: {vm['terms_ok']} of {vm['accounts']}.")
    lags = [int(detail(d, "registration_lag_days")) for d in decisions
            if detail(d, "registration_lag_days") is not None]
    lag = mean(lags)
    add("registration_lag_days", lag, fmt_days(lag),
        "Average business days from receipt to registration, over all documents.",
        f"Here: {_lag_breakdown(lags)}." if lags else "")

    # ---- downstream: automation efficiency ------------------------------------------------------
    def count(predicate) -> Optional[int]:
        return sum(1 for d in decisions if predicate(d)) if available else None

    touchless = count(lambda d: detail(d, "touchless", False))
    exceptions = count(is_exception)
    touchless_note = "" if routing else (
        " Without a gate, \"touchless\" means no exception follow-up after intake; intake itself is delayed "
        "(the store forwards its mail, AP opens ap@) before the quick-fix tool keys and posts the document.")
    add("touchless", touchless, fmt_number(touchless),
        "Documents fully handled with no human step: posted, blocked as a duplicate or credit applied "
        f"automatically.{touchless_note}")
    rate = pct(touchless, n) if available else None
    add("touchless_rate", rate, fmt_pct(rate), "Touchless documents ÷ documents × 100.",
        f"Here: {touchless} ÷ {n}.")
    add("exceptions", exceptions, fmt_number(exceptions),
        "Documents that stopped for a person: exceptions routed to an owner, low-confidence human reviews and, "
        "without a gate, the untracked email loop. Info flags do not count.")
    rate = pct(exceptions, n) if available else None
    add("exception_rate", rate, fmt_pct(rate), "Exceptions ÷ documents × 100.", f"Here: {exceptions} ÷ {n}.")

    blocked = count(lambda d: d.outcome == "blocked_duplicate")
    add("duplicates_blocked", blocked, fmt_number(blocked),
        "Documents blocked by the gate as a repeat of an invoice already registered or posted (same supplier, "
        "normalised invoice number, gross total within 1%).")
    groups = duplicate_groups(decisions)
    dup_postings = sum(len(g) for g in groups) if available else None
    dup_invoices = len(groups) if available else None
    add("duplicate_postings", dup_postings, fmt_number(dup_postings),
        "Postings of an invoice that was posted more than once in the scenario (same real supplier and "
        "normalised invoice number), counting every posting of it, the first one included.")
    add("duplicate_invoices", dup_invoices, fmt_number(dup_invoices),
        "Distinct invoices posted more than once in the scenario.")
    applied = count(lambda d: detail(d, "credit_status") == "applied")
    unapplied = count(lambda d: detail(d, "credit_status") == "unapplied")
    add("credit_notes_applied", applied, fmt_number(applied), "Credit notes applied to the invoice they reference.")
    add("credit_notes_unapplied", unapplied, fmt_number(unapplied),
        "Credit notes posted without being applied to their invoice, for example on another vendor account.")
    wrong = count(lambda d: detail(d, "wrong_entity_posting", False))
    add("wrong_entity_postings", wrong, fmt_number(wrong),
        "Postings to a Velox legal entity other than the one the invoice is billed to.")
    non_invoice = non_invoice_postings(decisions)
    add("non_invoice_postings", len(non_invoice) if available else None,
        fmt_number(len(non_invoice) if available else None),
        "Documents that are not invoices or credit notes (for example a supplier statement) posted as if they were "
        "invoices.")
    variance = count(lambda d: detail(d, "terms_variance_paid", False))
    add("terms_variance_paid", variance, fmt_number(variance),
        "Postings that took the payment terms printed on the invoice although they differ from the agreed terms, "
        "so the invoice is paid early or late. Counted, not valued.")

    leakage = cash_leakage(decisions)
    repeated, credit_docs = repeated_postings(decisions), unapplied_credits(decisions)
    statements = (f" + {len(non_invoice)} non-invoice document(s) posted "
                  f"{fmt_amounts(gross_by_currency(non_invoice))}" if non_invoice else "")
    add("cash_leakage_amount", round(leakage.get("EUR", 0.0), 2) if available else None,
        fmt_amounts(leakage) if available else NA,
        "Gross amount of every repeated posting of a duplicated invoice (the first posting is legitimate) + "
        "absolute amount of unapplied credit notes + gross amount of non-invoice documents posted as invoices. "
        "Payment-terms variance is part of the leakage but only counted (see its KPI), not valued. Amounts are "
        "never converted: other currencies are summed separately.",
        f"Here: {len(repeated)} repeated posting(s) {fmt_amounts(gross_by_currency(repeated))} + "
        f"{len(credit_docs)} unapplied credit note(s) {fmt_amounts(gross_by_currency(credit_docs))}{statements}; "
        f"{variance} posting(s) on non-agreed terms (count only).")

    days = [float(d.simulated_days) for d in decisions if d.simulated_days is not None]
    avg = mean(days)
    add("avg_cycle_days", avg, fmt_days(avg),
        "Average simulated business days from receipt to posting or resolution, over all documents of the sample.",
        f"Here: {sum(days):g} ÷ {len(days)}.")
    followup = [float(d.simulated_days) for d in decisions
                if d.simulated_days is not None and not detail(d, "touchless", False)]
    avg = mean(followup)
    add("avg_cycle_followup_days", avg, fmt_days(avg),
        "Average simulated business days over the documents that needed a human step (not touchless).",
        f"Here: {sum(followup):g} ÷ {len(followup)}." if followup else "Here: no such document.")
    steps = reference_nonpo_store_path(scenario)
    ref = round(sum(steps.values()), 1)
    limit = scenario_config(scenario).get("doa_auto_approve_limit")
    display = fmt_days(ref, 0) + (f" (0 under the {limit:,.0f} EUR DoA limit)" if limit else "")
    kpis["reference_nonpo_store_days"] = _kpi("reference_nonpo_store_days", ref, display,
                                              _reference_formula(scenario, steps))

    kpis = {key: kpis[key] for key in KPI_DEFS}  # display order
    return {
        "scenario": scenario,
        "available": available,
        "run": {"run_id": run.run_id, "finished_on": run.finished_on} if run else None,
        "sample_only": sample_only,
        "documents": n,
        "documents_total": _documents_total(session, scenario, sample_only),
        "kpis": kpis,
        "exceptions_by_type": exceptions_by_type(decisions),
        "info_flags_by_type": info_flags_by_type(decisions),
        "outcomes": outcome_counts(decisions),
        "cycle_by_doc": [{"doc_id": d.doc_id, "sample_no": detail(d, "sample_no"),
                          "days": as_int(d.simulated_days), "path": detail(d, "path")}
                         for d in sorted(decisions, key=lambda d: d.doc_id)],
    }


def outcome_label(decision: GateDecision) -> str:
    """'Posted', 'Credit applied', or the plain-English label of the exception type."""
    if decision.exception_type and decision.outcome in EXCEPTION_OUTCOMES:
        return taxonomy.label(decision.exception_type)
    return OUTCOME_LABELS.get(decision.outcome, decision.outcome)


def as_date(value: Any) -> Optional[date]:
    """An ISO date / datetime string (decision details) or a date as a date; None when missing or unreadable."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value).date()
        except ValueError:
            return None
    return None


def scheduled_payment(decision: GateDecision) -> Optional[date]:
    """Payment date of a posting on the invoice terms: the due date on those terms, or the posting day when the
    invoice was posted after it (it cannot be paid before it is posted). None when a date is missing."""
    invoice_date, posted = as_date(detail(decision, "invoice_date")), as_date(detail(decision, "posted_on"))
    terms = detail(decision, "invoice_terms_days", detail(decision, "terms_days"))
    if invoice_date is None or posted is None or terms is None:
        return None
    return max(posted, invoice_date + timedelta(days=int(terms)))


def terms_badge(decision: GateDecision) -> str:
    """'terms paid early' / 'terms paid late': the scheduled payment date (max of the posting day and the due date
    on the invoice terms) against the agreed due date (invoice date + agreed terms). The neutral 'terms variance'
    when a date is missing or the two fall on the same day (e.g. a late posting on shorter terms)."""
    scheduled = scheduled_payment(decision)
    invoice_date, agreed = as_date(detail(decision, "invoice_date")), detail(decision, "agreed_terms_days")
    if scheduled is None or invoice_date is None or agreed is None:
        return "terms variance"
    agreed_due = invoice_date + timedelta(days=int(agreed))
    if scheduled == agreed_due:
        return "terms variance"
    return "terms paid early" if scheduled < agreed_due else "terms paid late"


FLAG_BADGES = {"terms_variance": "terms variance flagged", "duplicate_vendor_account": "duplicate account flagged"}


def ubl_model() -> Optional[str]:
    """The Extraction.model label of a UBL e-invoice parsed without a model call (app/ubl.py); None if unavailable."""
    try:
        from app import ubl
    except ImportError:
        return None
    return ubl.UBL_MODEL


def format_badges(decision: GateDecision) -> list[str]:
    """How the document arrived: a UBL e-invoice read without a model, or an invoice only in an email body."""
    out = []
    model = detail(decision, "extraction_model")
    if model and model == ubl_model():
        out.append("UBL e-invoice")
    if detail(decision, "content_type") == "email_body":
        out.append("email body only")
    return out


def badges(decision: GateDecision) -> list[str]:
    """Short tags for the comparison table, derived from the decision details: problems first."""
    out = []
    if detail(decision, "duplicate_posting", False):
        out.append("duplicate posting")
    if detail(decision, "wrong_entity_posting", False):
        out.append("wrong entity")
    if detail(decision, "credit_status") == "unapplied":
        out.append("unapplied credit")
    if detail(decision, "posted", False) and detail(decision, "doc_type") == "other":
        out.append("statement posted as invoice")
    if detail(decision, "terms_variance_paid", False):
        out.append(terms_badge(decision))
    if detail(decision, "resolution_method") == "created":
        out.append("vendor account created")
    if decision.outcome == "blocked_duplicate":
        out.append("blocked duplicate")
    if detail(decision, "credit_status") == "applied":
        out.append("credit applied")
    if detail(decision, "doa_auto_approved", False):
        out.append("DoA auto-approved")
    if decision.outcome == "posted" and detail(decision, "posted", False):
        commitment = detail(decision, "commitment")
        if commitment == "contract":
            out.append("contract match")
        elif commitment == "po" and not detail(decision, "wrong_entity_posting", False):
            out.append("3-way match")
    out += [FLAG_BADGES[f["type"]] for f in detail(decision, "flags", []) if f.get("type") in FLAG_BADGES]
    return out + format_badges(decision)


def cell(decision: Optional[GateDecision]) -> Optional[dict[str, Any]]:
    """One scenario's result for one document in the comparison table (None if not run)."""
    if decision is None:
        return None
    return {
        "doc_id": decision.doc_id,
        "outcome": decision.outcome,
        "exception_type": decision.exception_type,
        "label": outcome_label(decision),
        "owner_name": decision.owner_name,
        "next_owner_name": detail(decision, "next_owner_name"),
        "sla_days": decision.sla_days,
        "days": as_int(decision.simulated_days),
        "account_id": detail(decision, "account_id"),
        "posted_entity": detail(decision, "posted_entity"),
        "badges": badges(decision),
    }


def _decision_by_sample(session: Session, scenario: str, dataset: str = "v1") -> dict[int, GateDecision]:
    """Latest decision per sample document of `dataset` (webhook documents have sample_no 0 and are left out). The
    dataset of a decision is its document's, else the one its id encodes (B-01 v1, B2-01 v2)."""
    doc_datasets = dict(session.execute(select(InboundDocument.doc_id, InboundDocument.dataset)
                                        .where(InboundDocument.scenario == scenario)).all())
    out: dict[int, GateDecision] = {}
    for d in _decisions(session, scenario):
        no = detail(d, "sample_no")
        if no and (doc_datasets.get(d.doc_id) or seed.dataset_of_doc_id(d.doc_id)) == dataset:
            out[int(no)] = d
    return out


def compared_datasets(session: Session) -> tuple[str, dict[str, Optional[str]], Optional[str]]:
    """(dataset of the comparison rows, dataset loaded per scenario, warning when the two scenarios hold different
    sample documents). The rows follow the as-is scenario's dataset, else the to-be one, else the case documents."""
    loaded = {s: seed.loaded_dataset(session, s) for s in config.SCENARIOS}
    dataset = next((d for d in loaded.values() if d), "v1")
    warning = None
    if all(loaded.values()) and len(set(loaded.values())) > 1:
        held = ", ".join(f"{config.SCENARIO_LABELS[s]} the {seed.DATASET_LABELS[d]}" for s, d in loaded.items())
        warning = (f"The two scenarios hold different sample documents ({held}): load the same set in both to "
                   f"compare them. The rows below are the {seed.DATASET_LABELS[dataset]}.")
    return dataset, loaded, warning


def compare(session: Session) -> dict[str, Any]:
    """Scenario A vs B for the same sample documents: both KPI sets (sample documents only, so webhook uploads
    never change a denominator on one side) and one row per document of the dataset loaded (case documents or
    test set v2); a warning when the two scenarios hold different datasets."""
    asis, tobe = compute(session, "asis", sample_only=True), compute(session, "tobe", sample_only=True)
    dataset, loaded, warning = compared_datasets(session)
    by_sample = {s: _decision_by_sample(session, s, dataset) for s in ("asis", "tobe")}
    rows = []
    for spec in sorted(seed.documents_for(dataset), key=lambda s: s.no):
        rows.append({
            "sample_no": spec.no,
            "supplier": spec.party.canonical_name,
            "invoice_number": spec.invoice_number,
            "gross_total": spec.gross_total,
            "currency": spec.currency,
            "designed_to_show": spec.designed_to_show,
            "asis": cell(by_sample["asis"].get(spec.no)),
            "tobe": cell(by_sample["tobe"].get(spec.no)),
        })
    return {"asis": asis, "tobe": tobe, "both_available": asis["available"] and tobe["available"], "rows": rows,
            "dataset": dataset, "dataset_label": seed.DATASET_LABELS[dataset], "datasets": loaded,
            "dataset_warning": warning}
