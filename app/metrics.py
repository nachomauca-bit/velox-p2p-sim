"""Metrics (brief v2 section 5): the four metrics of the deck's slide 11, with the definitions of appendix A6, computed
from the gate decisions, plus "registered same day" as a small fifth indicator. Nothing else is a KPI.

Upstream "process health": first-pass match rate, accounts per supplier.
Downstream "automation efficiency": touchless rate, invoice cycle time (business days, median).

Every metric is a dict {key, label, value, display, formula, group, unit}. `formula` is the definition (shown on
hover or focus), followed by the numbers behind the value. Durations are simulated (docs/ASSUMPTIONS.md, section 6).
No money figure is a metric (brief v2: no euros, no cost or savings figures).
"""
from __future__ import annotations

import math
from collections import Counter
from collections.abc import Iterable, Sequence
from datetime import date, datetime, timedelta
from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import config, gate, normalize, seed, taxonomy
from app.models import Contract, GateDecision, InboundDocument, Party, Run, VendorAccount

NA = "—"
EXCEPTION_OUTCOMES = ("exception", "human_review")
OUTCOMES = ("posted", "applied_credit", "exception", "human_review", "blocked_duplicate")
OUTCOME_WORDS = gate.OUTCOME_WORDS  # the four outcomes of the gate (to-be) and the as-is equivalents
FOUR_OUTCOMES = ("Post", "Exception", "Block", "Human review")

GROUP_TAGS = {"upstream": "Upstream · process health", "downstream": "Downstream · automation efficiency",
              "indicator": "Indicator"}

# key -> (label, group, unit), in the order of the deck's slide 11 (then the small fifth indicator).
KPI_DEFS: dict[str, tuple[str, str, str]] = {
    "first_pass_match_rate": ("First-pass match rate", "upstream", "%"),
    "accounts_per_supplier": ("Accounts per supplier", "upstream", "ratio"),
    "touchless_rate": ("Touchless rate", "downstream", "%"),
    "cycle_time_median": ("Invoice cycle time (business days)", "downstream", "days"),
    "registered_same_day": ("Registered same day", "indicator", "%"),
}
HEADLINE = ("first_pass_match_rate", "accounts_per_supplier", "touchless_rate", "cycle_time_median")

# Plain-English names of the sim.cycle_breakdown activities.
ACTIVITY_LABELS = {
    "store_forwarding": "store forwarding",
    "ap_open_and_key": "AP opening ap@ and keying",
    "email_loop": "email loop",
    "email_approval": "email approval",
    "posting": "posting",
    "registration": "registration",
    "extraction_and_gate": "reading and rules",
    "exception_sla": "owner resolves within the SLA",
    "past_sla": "days past the SLA",
    "workflow_approval": "workflow approval",
}


# --------------------------------------------------------------------------------------------
# Formatting and arithmetic helpers
# --------------------------------------------------------------------------------------------


def pct(part: int, whole: int) -> Optional[float]:
    return round(100 * part / whole, 1) if whole else None


def mean(values: Sequence[float]) -> Optional[float]:
    return round(sum(values) / len(values), 1) if values else None


def median(values: Sequence[float]) -> Optional[float]:
    if not values:
        return None
    s = sorted(values)
    mid = len(s) // 2
    return float(s[mid]) if len(s) % 2 else (s[mid - 1] + s[mid]) / 2


def percentile(values: Sequence[float], p: float) -> Optional[float]:
    """Nearest-rank percentile (P90 of the cycle time)."""
    if not values:
        return None
    s = sorted(values)
    return float(s[max(0, math.ceil(p / 100 * len(s)) - 1)])


def fmt_pct(value: Optional[float]) -> str:
    return NA if value is None else f"{value:.1f}%"


def fmt_days(value: Optional[float], decimals: int = 1) -> str:
    return NA if value is None else f"{value:.{decimals}f} days"


def fmt_business_days(value: Optional[float]) -> str:
    """'15 days', '0 days (same day)', '2.5 days'."""
    if value is None:
        return NA
    text = f"{value:g} day{'s' if value != 1 else ''}"
    return f"{text} (same day)" if value == 0 else text


def fmt_number(value: Optional[float], decimals: int = 0) -> str:
    return NA if value is None else f"{value:,.{decimals}f}"


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
    """Exceptions, human reviews and (as-is) the untracked email loop; not info flags."""
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


def outcome_word(decision: GateDecision) -> str:
    """Post / Exception / Block / Human review (to-be); 'Posted by AP' / 'Email loop — untracked' (as-is)."""
    return OUTCOME_WORDS[decision.scenario].get(decision.outcome, decision.outcome)


def outcome_counts(decisions: Iterable[GateDecision]) -> dict[str, int]:
    """Documents per outcome word, the four gate outcomes first."""
    return ordered_counts(Counter(outcome_word(d) for d in decisions), FOUR_OUTCOMES)


def outcome_counts_from(scenario: str, by_code: dict[str, int]) -> dict[str, int]:
    """A run summary's counts per outcome code as counts per outcome word (posted + applied_credit = Post)."""
    counts: Counter = Counter()
    for code, n in by_code.items():
        counts[OUTCOME_WORDS[scenario].get(code, code)] += n
    return ordered_counts(counts, FOUR_OUTCOMES)


def invoice_key(decision: GateDecision) -> Optional[tuple[str, str]]:
    """(real supplier, normalised number) identifying one supplier invoice, or None. A credit note is its own key."""
    number = detail(decision, "invoice_number_norm") or normalize.normalise_invoice_number(
        detail(decision, "invoice_number"))
    supplier = detail(decision, "true_party_id") or normalize.normalise_name(detail(decision, "supplier_name"))
    if not number or not supplier:
        return None
    kind = "credit_note" if detail(decision, "doc_type") == "credit_note" else "invoice"
    return supplier, f"{kind}:{number}"


def duplicate_groups(decisions: Iterable[GateDecision]) -> list[list[GateDecision]]:
    """Postings of the same invoice (same real supplier and normalised number) posted more than once, in
    processing order: the first posting of each group is legitimate, the others are the duplicates."""
    groups: dict[tuple[str, str], list[GateDecision]] = {}
    for d in decisions:
        key = invoice_key(d)
        if detail(d, "posted", False) and key:
            groups.setdefault(key, []).append(d)
    return [g for g in groups.values() if len(g) > 1]


def is_invoice_received(decision: GateDecision) -> bool:
    """A document received as an invoice (first-pass denominator): everything but credit notes."""
    return detail(decision, "doc_type") != "credit_note"


def ends_posted(decision: GateDecision) -> bool:
    """Posted, now or once its owner resolves it (touchless and cycle-time population): not a Block, and not a
    document that is never posted (a statement, a document that is not an invoice)."""
    return decision.outcome != "blocked_duplicate" and detail(decision, "doc_type") not in ("statement", "other")


def as_int(value: Optional[float]) -> Optional[int]:
    return None if value is None else int(round(value))


# --------------------------------------------------------------------------------------------
# Vendor master quality (same logic as the vendor master page's flags)
# --------------------------------------------------------------------------------------------


def find_party(acc: VendorAccount, parties: Sequence[Party]) -> tuple[Optional[Party], Optional[str]]:
    """Linked party; for unlinked accounts the party with the same tax ID, else a matching name."""
    if acc.party_id:
        return next((p for p in parties if p.party_id == acc.party_id), None), None
    vat = normalize.normalise_vat(acc.vat_id)
    if vat:
        for p in parties:
            if normalize.normalise_vat(p.vat_id) == vat:
                return p, "tax ID"
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
        return "same tax ID"
    if normalize.names_match(a.display_name, b.display_name):
        return "similar name"
    return None


def vendor_master_stats(accounts: Sequence[VendorAccount], parties: Sequence[Party],
                        contracts: Sequence[Contract]) -> dict[str, Any]:
    """Records per supplier (all records, inactive included, as the deck's 2,800 ÷ 1,200), completeness of
    identifiers, agreed terms and possible duplicates."""
    n = len(accounts)
    with_ids = sum(1 for a in accounts if a.vat_id and a.iban)
    # unique suppliers: the known ones, plus one per tax ID (else per record) of a record that belongs to none of
    # them, e.g. a record the as-is process opened for a supplier that is not in the master
    unknown = {a.vat_id or a.account_id for a in accounts if find_party(a, parties)[0] is None}
    suppliers = len(parties) + len(unknown)
    terms_ok = terms_differ = 0
    for acc in accounts:
        agreed, _ = agreed_terms(find_party(acc, parties)[0], acc.legal_entity_code, contracts)
        terms_ok += agreed is not None and acc.payment_terms_days == agreed
        terms_differ += agreed is not None and acc.payment_terms_days != agreed
    duplicates = sum(1 for acc in accounts if any(other is not acc and duplicate_reason(acc, other)
                                                  for other in accounts))
    return {
        "accounts": n,
        "parties": suppliers,
        "ratio": round(n / suppliers, 2) if suppliers else None,
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
# compute / compare
# --------------------------------------------------------------------------------------------


def _kpi(key: str, value: Any, display: str, formula: str) -> dict[str, Any]:
    label, group, unit = KPI_DEFS[key]
    return {"key": key, "label": label, "value": value, "display": display, "formula": formula,
            "group": group, "tag": GROUP_TAGS[group], "unit": unit}


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


def compute(session: Session, scenario: str, *, sample_only: bool = False) -> dict[str, Any]:
    """The metrics of one scenario, plus the counts behind the charts. See the module docstring.

    "documents" is the number of decisions, "documents_total" the number of inbound documents of the scenario:
    fewer decisions than documents means the scenario was only partly processed (e.g. an email just received).
    sample_only=True keeps the sample documents only (sample_no > 0), so two scenarios are compared on the same
    documents even when one of them also received webhook uploads.
    """
    decisions = _decisions(session, scenario, sample_only)
    available = bool(decisions)
    run = session.scalars(select(Run).where(Run.scenario == scenario)
                          .order_by(Run.started_on.desc(), Run.id.desc())).first()
    vm = vendor_master_quality(session, scenario)
    kpis: dict[str, dict[str, Any]] = {}

    def add(key: str, value: Any, display: str, formula: str, numbers: str = "") -> None:
        """Run-dependent metric; the numbers behind the value are appended once the scenario has run."""
        kpis[key] = _kpi(key, value, display, f"{formula} {numbers}" if available and numbers else formula)

    # ---- upstream: process health --------------------------------------------------------------
    received = [d for d in decisions if is_invoice_received(d)]
    matched = [d for d in received if detail(d, "first_pass_match", False)]
    rate = pct(len(matched), len(received)) if available else None
    add("first_pass_match_rate", rate, fmt_pct(rate),
        "Invoices matched at the first pass to a commitment (PO + receipt or confirmation, contract schedule, or "
        "card / catalogue) and within tolerance, with no follow-up ÷ invoices received (credit notes excluded; "
        "deck A6, with card / catalogue counted as a commitment as in brief v2).",
        f"Here: {len(matched)} of {len(received)}.")
    kpis["accounts_per_supplier"] = _kpi(
        "accounts_per_supplier", vm["ratio"], fmt_number(vm["ratio"], 2),
        "Vendor records ÷ unique suppliers by tax ID (deck A6; inactive records included, as in the case's 2,800 ÷ "
        "1,200). One record per legal entity a supplier serves is legitimate (deck A1, S9). "
        f"Here: {vm['accounts']} ÷ {vm['parties']}.")

    # ---- downstream: automation efficiency ------------------------------------------------------
    population = [d for d in decisions if ends_posted(d)]
    touchless = [d for d in population if detail(d, "touchless", False)]
    rate = pct(len(touchless), len(population)) if available else None
    add("touchless_rate", rate, fmt_pct(rate),
        "Invoices posted with no human step ÷ invoices posted (deck A6). An Exception or Human review is posted "
        "after its owner resolves it; a Block is never posted. Without a gate AP keys every invoice, so nothing is "
        "touchless.", f"Here: {len(touchless)} of {len(population)}.")
    days = [float(d.simulated_days) for d in population if d.simulated_days is not None]
    med, p90 = median(days), percentile(days, 90)
    add("cycle_time_median", med, fmt_business_days(med),
        "Median simulated business days from arrival (registration, in to-be) to approved and ready to pay, over the "
        "invoices posted (deck A6).",
        f"Here: median of {len(days)} documents; P90 {fmt_business_days(p90)}.")
    lags = [d for d in decisions if detail(d, "registration_lag_days") is not None]
    same_day = [d for d in lags if int(detail(d, "registration_lag_days")) == 0]
    rate = pct(len(same_day), len(lags)) if available else None
    add("registered_same_day", rate, fmt_pct(rate),
        "Documents registered on the day they arrive ÷ documents (deck slide 5, RC3). As-is: the store forwards its "
        "mail (7 business days) and AP opens ap@ the next day.", f"Here: {len(same_day)} of {len(lags)}.")

    kpis = {key: kpis[key] for key in KPI_DEFS}  # display order
    return {
        "scenario": scenario,
        "available": available,
        "run": {"run_id": run.run_id, "finished_on": run.finished_on} if run else None,
        "sample_only": sample_only,
        "documents": len(decisions),
        "documents_total": _documents_total(session, scenario, sample_only),
        "kpis": kpis,
        "exceptions_by_type": exceptions_by_type(decisions),
        "info_flags_by_type": info_flags_by_type(decisions),
        "outcomes": outcome_counts(decisions),
        "cycle_by_doc": [{"doc_id": d.doc_id, "sample_no": detail(d, "sample_no"),
                          "days": as_int(d.simulated_days), "path": detail(d, "path"), "outcome": d.outcome}
                         for d in sorted(decisions, key=lambda d: d.doc_id)],
    }


def outcome_label(decision: GateDecision) -> str:
    """The A3 label of a to-be exception or human review; else the outcome word (Post, Block, Posted by AP, ...)."""
    if decision.exception_type and decision.outcome in EXCEPTION_OUTCOMES and decision.exception_type != "email_loop":
        return taxonomy.label(decision.exception_type)
    return outcome_word(decision)


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


FLAG_BADGES = {"duplicate_vendor_account": "duplicate record flagged"}


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
    if detail(decision, "posted", False) and detail(decision, "doc_type") in ("statement", "other"):
        out.append("statement posted as invoice")
    if detail(decision, "terms_variance_paid", False):
        out.append(terms_badge(decision))
    if detail(decision, "resolution_method") == "created":
        out.append("vendor account created")
    if detail(decision, "credit_status") == "applied":
        out.append("credit note linked")
    if decision.outcome == "posted" and detail(decision, "posted", False):
        commitment = detail(decision, "commitment")
        if commitment == "contract":
            out.append("contract match")
        elif commitment == "catalogue":
            out.append("catalogue match")
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
        "outcome_word": outcome_word(decision),
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
    """Scenario A vs B for the same sample documents: both metric sets (sample documents only, so webhook uploads
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
            "asis": cell(by_sample["asis"].get(spec.no)),
            "tobe": cell(by_sample["tobe"].get(spec.no)),
        })
    return {"asis": asis, "tobe": tobe, "both_available": asis["available"] and tobe["available"], "rows": rows,
            "dataset": dataset, "dataset_label": seed.DATASET_LABELS[dataset], "datasets": loaded,
            "dataset_warning": warning}
