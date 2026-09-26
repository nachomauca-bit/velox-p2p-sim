"""Metrics and the A/B comparison (brief v2 section 5; app/metrics.py).

Unit tests use synthetic GateDecision rows, so they do not depend on the gate's rules. The golden tests at the end
run the real gate on the 12 case documents (fixture extraction) and check the four metrics of tests/golden.yaml.
"""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import gate, metrics, seed, world
from app.config import SCENARIOS
from app.db import SessionLocal, init_db
from app.models import GateDecision, InboundDocument, Run

GOLDEN = yaml.safe_load((Path(__file__).parent / "golden.yaml").read_text(encoding="utf-8"))

BASE_DETAILS: dict[str, Any] = dict(
    sample_no=None, supplier_name=None, invoice_number=None, invoice_number_norm=None, gross_total=0.0,
    net_total=0.0, currency="EUR", doc_type="invoice", party_id=None, true_party_id=None, account_id=None,
    resolution_method=None, bill_to_entity=None, posted_entity=None, posted=False, invoice_id=None,
    wrong_entity_posting=False, duplicate_posting=False, duplicate_of=None, commitment=None, po_number=None,
    contract_id=None, contract_period=None, catalogue_id=None, requester_name=None, next_owner_name=None,
    next_owner_role=None, owner_title=None, gross_chf=None, duplicate_leg=None, first_pass_match=False,
    credit_status=None, applied_to=None, flags=[], terms_days=None, terms_source=None,
    invoice_terms_days=None, agreed_terms_days=None, terms_variance_paid=False, touchless=False, path=None,
    cycle_breakdown={}, registration_lag_days=0, email_loop_days=None, line_checks=[], lookup_party_id=None,
    invoice_date=None, posted_on=None, po_numbers=[], content_type="pdf", extraction_model=None,
)


def decision(doc_id: str, outcome: str = "posted", *, exception_type: str | None = None, days: float = 0,
             owner_name: str | None = None, sla_days: int | None = None, **details: Any) -> GateDecision:
    scenario = "asis" if doc_id.startswith("A") else "tobe"
    tail = doc_id.split("-")[-1]
    return GateDecision(
        doc_id=doc_id, scenario=scenario, steps=[], outcome=outcome, exception_type=exception_type,
        owner_role=None, owner_name=owner_name, sla_days=sla_days, reason="Synthetic decision.",
        simulated_days=days, decided_on=datetime(2026, 10, 16, 9, 0),
        details={**BASE_DETAILS, "sample_no": int(tail) if tail.isdigit() else 0, **details},
    )


NORDWIND = dict(true_party_id="P-0001", supplier_name="Nordwind Logistics GmbH", invoice_number="NWL-2026-00913",
                invoice_number_norm="NWL2026913", gross_total=27846.0, bill_to_entity="VDE", posted=True,
                posted_entity="VDE")


def mini_asis() -> list[GateDecision]:
    """As-is: an invoice posted twice after the email loop, a clean PO posting keyed by AP, an unapplied credit."""
    return [
        decision("A-01", "exception", exception_type="email_loop", days=20, registration_lag_days=1,
                 terms_variance_paid=True, invoice_terms_days=14, agreed_terms_days=30, path="email_loop",
                 **NORDWIND),
        decision("A-02", "exception", exception_type="email_loop", days=28, registration_lag_days=7,
                 duplicate_posting=True, duplicate_of="A-01", path="email_loop", doc_type="reminder",
                 **{**NORDWIND, "invoice_number": "NWL 2026 913 COPY", "account_id": "V-000117"}),
        decision("A-03", days=2, registration_lag_days=1, first_pass_match=True, true_party_id="P-0002",
                 invoice_number="INV-2026-0457", invoice_number_norm="INV20260457", gross_total=14400.0,
                 bill_to_entity="VFR", posted=True, posted_entity="VFR", commitment="po", po_number="4500117",
                 path="matched"),
        decision("A-04", days=2, registration_lag_days=1, doc_type="credit_note", true_party_id="P-0002",
                 invoice_number="CN-2026-0031", invoice_number_norm="CN20260031", gross_total=-1800.0,
                 bill_to_entity="VFR", posted=True, posted_entity="VFR", credit_status="unapplied", path="matched"),
    ]


def mini_tobe() -> list[GateDecision]:
    """To-be: a Human review above the approval limit, its Block, a Post, a linked credit note, an Exception."""
    return [
        decision("B-01", "human_review", exception_type="amount_above_approval_limit", days=2, sla_days=2,
                 owner_name="Stefan Keller", first_pass_match=True, commitment="contract", path="exception",
                 **{**NORDWIND, "posted": False, "posted_entity": None}),
        decision("B-02", "blocked_duplicate", exception_type="duplicate_invoice", owner_name="Marco Ruiz",
                 doc_type="reminder", path="touchless", **{**NORDWIND, "posted": False, "posted_entity": None}),
        decision("B-03", touchless=True, first_pass_match=True, posted=True, commitment="po", path="touchless"),
        decision("B-04", "applied_credit", touchless=True, doc_type="credit_note", posted=True,
                 credit_status="applied", path="touchless"),
        decision("B-05", "exception", exception_type="po_no_receipt", days=2, sla_days=2, owner_name="Jonas Weber",
                 commitment="po", path="exception"),
    ]


def store(session: Session, decisions: list[GateDecision]) -> None:
    session.add_all(decisions)
    session.commit()


def test_synthetic_rows_follow_the_gate_contract() -> None:
    """Guards the synthetic data: the same detail keys as the real gate."""
    assert set(BASE_DETAILS) == set(gate.DETAIL_KEYS)
    for scenario in SCENARIOS:
        assert metrics.scenario_config(scenario) is gate.SCENARIOS[scenario]


# --------------------------------------------------------------------------------------------
# The four metrics of the deck (slide 11) and nothing else
# --------------------------------------------------------------------------------------------


def test_the_metrics_are_the_four_of_slide_11_plus_registered_same_day() -> None:
    assert [label for label, _, _ in metrics.KPI_DEFS.values()] == [
        "First-pass match rate", "Accounts per supplier", "Touchless rate", "Invoice cycle time (business days)",
        "Registered same day"]
    assert metrics.HEADLINE == ("first_pass_match_rate", "accounts_per_supplier", "touchless_rate", "cycle_time_median")
    assert [group for _, group, _ in metrics.KPI_DEFS.values()] == [
        "upstream", "upstream", "downstream", "downstream", "indicator"]
    assert metrics.GROUP_TAGS["upstream"] == "Upstream · process health"
    assert metrics.GROUP_TAGS["downstream"] == "Downstream · automation efficiency"
    assert all(unit != "EUR" for _, _, unit in metrics.KPI_DEFS.values())  # brief v2: no euros


# --------------------------------------------------------------------------------------------
# Formatting and arithmetic
# --------------------------------------------------------------------------------------------


def test_display_formats() -> None:
    assert metrics.fmt_pct(71.42857) == "71.4%"
    assert metrics.fmt_days(0.5) == "0.5 days"
    assert metrics.fmt_days(25.0, 0) == "25 days"
    assert metrics.fmt_business_days(15.0) == "15 days"
    assert metrics.fmt_business_days(1.0) == "1 day"
    assert metrics.fmt_business_days(0.0) == "0 days (same day)"
    assert metrics.fmt_business_days(2.5) == "2.5 days"
    assert metrics.fmt_number(5) == "5"
    assert metrics.fmt_number(2.3333, 2) == "2.33"
    for fmt in (metrics.fmt_pct, metrics.fmt_days, metrics.fmt_business_days, metrics.fmt_number):
        assert fmt(None) == metrics.NA


def test_pct_mean_median_and_percentile_handle_empty_input() -> None:
    assert metrics.pct(8, 11) == 72.7
    assert metrics.pct(0, 0) is None
    assert metrics.mean([2, 2, 1, 2] + [0] * 10) == 0.5
    assert metrics.mean([]) is None
    assert metrics.median([2, 2, 2, 2, 15, 15, 15, 16, 16, 21, 21, 27]) == 15.0  # the as-is case documents
    assert metrics.median([0] * 8 + [2, 2, 5]) == 0.0  # the to-be case documents
    assert metrics.median([2, 20]) == 11.0 and metrics.median([]) is None
    assert metrics.percentile([0] * 8 + [2, 2, 5], 90) == 2.0  # nearest rank: the 10th of 11
    assert metrics.percentile([], 90) is None


# --------------------------------------------------------------------------------------------
# Pure functions over decisions
# --------------------------------------------------------------------------------------------


def test_exceptions_by_type_counts_exceptions_and_human_reviews_only() -> None:
    decisions = mini_tobe() + [
        decision("B-06", "exception", exception_type="price_qty_mismatch"),
        decision("B-10", flags=[{"type": "duplicate_vendor_account", "label": "", "owner_name": None, "detail": ""}]),
    ]
    assert metrics.exceptions_by_type(decisions) == {"po_no_receipt": 1, "price_qty_mismatch": 1,
                                                     "amount_above_approval_limit": 1}
    assert list(metrics.exceptions_by_type(decisions)) == ["po_no_receipt", "price_qty_mismatch",
                                                           "amount_above_approval_limit"]  # A3 order
    assert metrics.info_flags_by_type(decisions) == {"duplicate_vendor_account": 1}


def test_outcome_counts_use_the_four_outcomes_in_tobe_and_the_asis_words() -> None:
    assert metrics.outcome_counts(mini_tobe()) == {"Post": 2, "Exception": 1, "Block": 1, "Human review": 1}
    assert list(metrics.outcome_counts(mini_tobe())) == ["Post", "Exception", "Block", "Human review"]
    assert metrics.outcome_counts(mini_asis()) == {"Email loop — untracked": 2, "Posted by AP": 2}


def test_exception_without_a_type_is_counted_under_its_outcome() -> None:
    assert metrics.exceptions_by_type([decision("B-09", "human_review")]) == {"human_review": 1}


def test_duplicate_groups_by_real_supplier_and_normalised_number() -> None:
    groups = metrics.duplicate_groups(mini_asis())
    assert [[d.doc_id for d in g] for g in groups] == [["A-01", "A-02"]]  # a reminder groups with its invoice


def test_duplicate_groups_ignore_other_suppliers_unposted_documents_and_credit_notes() -> None:
    same_number = dict(invoice_number="2026-091", invoice_number_norm="2026091", posted=True)
    decisions = [
        decision("A-05", true_party_id="P-0003", **same_number),
        decision("A-06", true_party_id="P-0006", **same_number),  # another supplier, same number
        decision("A-07", true_party_id="P-0003", **{**same_number, "posted": False}),  # not posted
        decision("A-08", true_party_id="P-0003", doc_type="credit_note", **same_number),  # a credit note
    ]
    assert metrics.duplicate_groups(decisions) == []


def test_duplicate_groups_fall_back_to_the_supplier_name_and_the_raw_number() -> None:
    decisions = [
        decision("A-01", supplier_name="Nordwind Logistics GmbH", invoice_number="NWL-2026-00913", posted=True),
        decision("A-02", supplier_name="NORDWIND LOGISTICS", invoice_number="NWL 2026 913 (COPY)", posted=True),
        decision("A-03", supplier_name=None, invoice_number=None, posted=True),  # nothing to group on
    ]
    assert [[d.doc_id for d in g] for g in metrics.duplicate_groups(decisions)] == [["A-01", "A-02"]]


def test_populations_of_the_metrics() -> None:
    """First-pass denominator: every document received as an invoice (credit notes excluded). Touchless and cycle
    time population: what ends posted (a Block never is, nor a statement)."""
    tobe = {d.doc_id: d for d in mini_tobe()}
    assert [d for d in tobe if metrics.is_invoice_received(tobe[d])] == ["B-01", "B-02", "B-03", "B-05"]
    assert [d for d in tobe if metrics.ends_posted(tobe[d])] == ["B-01", "B-03", "B-04", "B-05"]
    assert not metrics.ends_posted(decision("B-07", "exception", exception_type="payment_status_query",
                                            doc_type="statement"))


# --------------------------------------------------------------------------------------------
# Vendor master quality (no run needed)
# --------------------------------------------------------------------------------------------


def test_vendor_master_quality_dirty_and_clean(session: Session) -> None:
    asis = metrics.vendor_master_quality(session, "asis")
    assert (asis["accounts"], asis["parties"], asis["ratio"]) == (28, 12, 2.33)  # the case's 2,800 / 1,200
    assert (asis["with_vat_iban"], asis["pct_vat_iban"]) == (20, 71.4)  # D4: 8 accounts miss an identifier
    # D3 (5 linked accounts) + D1/D2 (10 unlinked accounts) have non-agreed terms; D5 leftovers keep agreed terms.
    assert (asis["terms_ok"], asis["terms_differ"], asis["pct_terms_ok"]) == (13, 15, 46.4)
    assert asis["duplicates_flagged"] > 0
    tobe = metrics.vendor_master_quality(session, "tobe")
    assert (tobe["accounts"], tobe["ratio"], tobe["pct_vat_iban"], tobe["pct_terms_ok"]) == (14, 1.17, 100.0, 100.0)
    assert tobe["duplicates_flagged"] == 0  # one record per supplier per legal entity it serves is by design


def test_a_record_of_a_supplier_not_in_the_master_counts_as_a_new_supplier(session: Session) -> None:
    """Accounts per supplier divides by unique suppliers: a record the as-is process opened for a supplier that is
    not in the master (test set v2 document 26) adds a supplier as well as a record."""
    from app.models import VendorAccount

    template = session.scalars(select(VendorAccount).where(VendorAccount.scenario == "asis")).first()
    session.add(VendorAccount(scenario="asis", account_id="V-000999", legal_entity_code="VDE",
                              display_name="Berliner Blumen GmbH", vat_id="DE305118442", iban=None,
                              payment_terms_days=14, status="active", party_id=None,
                              created_by="ap.invoice-entry", created_on=template.created_on))
    session.commit()
    asis = metrics.vendor_master_quality(session, "asis")
    assert (asis["accounts"], asis["parties"], asis["ratio"]) == (29, 13, 2.23)


def test_vendor_master_stats_without_data() -> None:
    stats = metrics.vendor_master_stats([], [], [])
    assert (stats["accounts"], stats["ratio"], stats["pct_vat_iban"], stats["pct_terms_ok"]) == (0, None, None, None)


# --------------------------------------------------------------------------------------------
# compute()
# --------------------------------------------------------------------------------------------


def test_compute_without_a_run(session: Session) -> None:
    result = metrics.compute(session, "asis")
    assert result["available"] is False and result["run"] is None and result["documents"] == 0
    assert result["documents_total"] == 0  # empty inbox
    assert list(result["kpis"]) == list(metrics.KPI_DEFS)
    for key, kpi in result["kpis"].items():
        assert set(kpi) == {"key", "label", "value", "display", "formula", "group", "tag", "unit"}
        assert kpi["key"] == key and kpi["formula"] and kpi["tag"] == metrics.GROUP_TAGS[kpi["group"]]
        if key == "accounts_per_supplier":  # needs no run
            assert (kpi["value"], kpi["display"]) == (2.33, "2.33")
        else:
            assert kpi["value"] is None and kpi["display"] == metrics.NA, key
            assert "Here:" not in kpi["formula"], key
    assert (result["exceptions_by_type"], result["info_flags_by_type"], result["outcomes"], result["cycle_by_doc"]) \
        == ({}, {}, {}, [])


def test_compute_on_synthetic_asis_decisions(session: Session) -> None:
    store(session, mini_asis())
    session.add(Run(run_id="run-asis-test", scenario="asis", started_on=datetime(2026, 10, 16, 9, 0),
                    finished_on=datetime(2026, 10, 16, 9, 1), summary_json={}))
    session.commit()
    result = metrics.compute(session, "asis")
    k = {key: kpi["value"] for key, kpi in result["kpis"].items()}
    assert result["available"] is True and result["documents"] == 4
    assert result["run"] == {"run_id": "run-asis-test", "finished_on": datetime(2026, 10, 16, 9, 1)}
    assert k == {"first_pass_match_rate": 33.3, "accounts_per_supplier": 2.33, "touchless_rate": 0.0,
                 "cycle_time_median": 11.0, "registered_same_day": 0.0}
    formulas = {key: kpi["formula"] for key, kpi in result["kpis"].items()}
    assert formulas["first_pass_match_rate"].endswith("Here: 1 of 3.")  # the credit note is excluded
    assert formulas["touchless_rate"].endswith("Here: 0 of 4.")
    assert formulas["cycle_time_median"].endswith("Here: median of 4 documents; P90 28 days.")
    assert formulas["registered_same_day"].endswith("Here: 0 of 4.")
    assert result["kpis"]["cycle_time_median"]["display"] == "11 days"
    assert result["exceptions_by_type"] == {"email_loop": 2}
    assert result["outcomes"] == {"Email loop — untracked": 2, "Posted by AP": 2}
    assert result["cycle_by_doc"][1] == {"doc_id": "A-02", "sample_no": 2, "days": 28, "path": "email_loop",
                                         "outcome": "exception"}


def test_compute_on_synthetic_tobe_decisions(session: Session) -> None:
    store(session, mini_tobe())
    k = {key: kpi["value"] for key, kpi in metrics.compute(session, "tobe")["kpis"].items()}
    # first pass: B-01 and B-03 of B-01, B-02, B-03, B-05; touchless: B-03 and B-04 of the four that end posted
    assert k == {"first_pass_match_rate": 50.0, "accounts_per_supplier": 1.17, "touchless_rate": 50.0,
                 "cycle_time_median": 1.0, "registered_same_day": 100.0}


def test_compute_counts_documents_and_can_keep_the_sample_documents_only(session: Session) -> None:
    template = seed.load_sample_documents(session, "asis")[0]  # 12 inbound case documents
    session.add(InboundDocument(  # a webhook upload (sample_no 0)
        doc_id="A-W01", scenario="asis", sample_no=0, channel=template.channel, mailbox=template.mailbox,
        received_on=template.received_on, file_path=template.file_path, file_hash=template.file_hash,
        sender_email="billing@example.com", subject="Upload", registered=False, registered_on=None,
        doc_type="unknown"))
    store(session, mini_asis() + [decision("A-W01", posted=True, first_pass_match=True, registration_lag_days=0)])
    everything = metrics.compute(session, "asis")
    assert (everything["documents"], everything["documents_total"]) == (5, 13)  # 8 documents not processed
    assert everything["kpis"]["registered_same_day"]["value"] == 20.0  # 1 of 5
    sample = metrics.compute(session, "asis", sample_only=True)
    assert (sample["documents"], sample["documents_total"], sample["sample_only"]) == (4, 12, True)
    assert sample["kpis"]["registered_same_day"]["value"] == 0.0  # the upload is left out
    assert sample["kpis"]["first_pass_match_rate"]["formula"].endswith("Here: 1 of 3.")
    assert [c["doc_id"] for c in sample["cycle_by_doc"]] == ["A-01", "A-02", "A-03", "A-04"]
    compared = metrics.compare(session)["asis"]  # the comparison uses the sample documents only
    assert (compared["documents"], compared["documents_total"], compared["sample_only"]) == (4, 12, True)


def test_compute_edge_case_no_invoice(session: Session) -> None:
    store(session, [decision("B-04", "applied_credit", doc_type="credit_note", credit_status="applied",
                             touchless=True, posted=True, gross_total=-1800.0)])
    kpis = metrics.compute(session, "tobe")["kpis"]
    assert kpis["first_pass_match_rate"]["value"] is None  # no invoice: no denominator
    assert kpis["touchless_rate"]["value"] == 100.0
    assert kpis["cycle_time_median"]["display"] == "0 days (same day)"


# --------------------------------------------------------------------------------------------
# compare(): badges, cells and rows
# --------------------------------------------------------------------------------------------


# Nordwind's invoice NWL-2026-00913: dated 30 Sep 2026, 14 days printed, 30 days agreed (due 30 Oct 2026).
NORDWIND_TERMS = dict(terms_variance_paid=True, invoice_terms_days=14, agreed_terms_days=30, invoice_date="2026-09-30")


@pytest.mark.parametrize("dec, expected", [
    # the resend is posted on 13 Nov, after the agreed due date: paid late, not early
    (decision("A-02", "exception", exception_type="email_loop", duplicate_posting=True, posted=True,
              posted_on="2026-11-13T16:05:00", **NORDWIND_TERMS),
     ["duplicate posting", "terms paid late"]),
    # posted on 22 Oct, after its 14-day due date (14 Oct): paid on 22 Oct, before the agreed 30 Oct
    (decision("A-01", "exception", exception_type="email_loop", posted=True, posted_on="2026-10-22T08:42:00",
              **NORDWIND_TERMS), ["terms paid early"]),
    (decision("A-10", posted=True, commitment="none", wrong_entity_posting=True), ["wrong entity"]),
    (decision("A-04", posted=True, credit_status="unapplied"), ["unapplied credit"]),
    (decision("A-20", posted=True, resolution_method="created"), ["vendor account created"]),
    (decision("A-21", posted=True, terms_variance_paid=True, invoice_terms_days=60, agreed_terms_days=30,
              invoice_date="2026-10-01", posted_on="2026-10-05T09:00:00"),
     ["terms paid late"]),
    # posted exactly on the agreed due date after missing its own: neither early nor late
    (decision("A-22", posted=True, posted_on="2026-10-30T10:00:00", **NORDWIND_TERMS), ["terms variance"]),
    # no invoice date or posting date: neutral
    (decision("A-23", posted=True, terms_variance_paid=True, invoice_terms_days=14, agreed_terms_days=30),
     ["terms variance"]),
    (decision("B-02", "blocked_duplicate", exception_type="duplicate_invoice"), []),  # the outcome itself says Block
    (decision("B-04", "applied_credit", credit_status="applied"), ["credit note linked"]),
    (decision("B-09", posted=True, commitment="catalogue", catalogue_id="CAT-2026-001"), ["catalogue match"]),
    (decision("B-08", posted=True, commitment="po"), ["3-way match"]),
    (decision("B-10", posted=True, commitment="contract",
              flags=[{"type": "duplicate_vendor_account", "label": "", "owner_name": None, "detail": ""}]),
     ["contract match", "duplicate record flagged"]),
    (decision("B-05", "exception", exception_type="po_no_receipt", commitment="po"), []),
    (decision("B-01", "human_review", exception_type="amount_above_approval_limit", commitment="contract"), []),
])
def test_badges(dec: GateDecision, expected: list[str]) -> None:
    assert metrics.badges(dec) == expected


def test_no_badge_suggests_an_approval_by_the_system() -> None:
    """Brief v2 section 6: nothing on screen suggests the AI or the gate approves."""
    every = [b for d in mini_asis() + mini_tobe() for b in metrics.badges(d)]
    assert not any("approv" in b.lower() or "doa" in b.lower() for b in every)


def test_scheduled_payment_is_the_later_of_posting_and_due_date() -> None:
    before_due = decision("A-01", posted=True, posted_on="2026-10-05T09:00:00", **NORDWIND_TERMS)
    assert metrics.scheduled_payment(before_due) == date(2026, 10, 14)  # 30 Sep + 14 days
    after_due = decision("A-01", posted=True, posted_on="2026-10-22T08:42:00", **NORDWIND_TERMS)
    assert metrics.scheduled_payment(after_due) == date(2026, 10, 22)
    assert metrics.scheduled_payment(decision("A-01", posted=True, **NORDWIND_TERMS)) is None  # no posting date
    assert metrics.as_date("2026-09-30") == date(2026, 9, 30) and metrics.as_date("not a date") is None


def test_outcome_labels() -> None:
    assert metrics.outcome_label(decision("A-01", "exception", exception_type="email_loop")) == \
        "Email loop — untracked"
    assert metrics.outcome_label(decision("A-03")) == "Posted by AP"
    assert metrics.outcome_label(decision("B-05", "exception", exception_type="po_no_receipt")) == \
        "PO exists, no receipt or confirmation"
    assert metrics.outcome_label(decision("B-01", "human_review", exception_type="amount_above_approval_limit")) == \
        "Amount above approval limit"
    assert metrics.outcome_label(decision("B-02", "blocked_duplicate", exception_type="duplicate_invoice")) == "Block"
    assert metrics.outcome_label(decision("B-04", "applied_credit")) == "Post"
    assert metrics.outcome_label(decision("B-03")) == "Post"


def test_cell() -> None:
    dec = decision("B-06", "exception", exception_type="price_qty_mismatch", days=5.0, owner_name="Sofia Brandt",
                   sla_days=2, account_id="V-000106", commitment="po")
    assert metrics.cell(dec) == {
        "doc_id": "B-06", "outcome": "exception", "outcome_word": "Exception", "exception_type": "price_qty_mismatch",
        "label": "Price or quantity mismatch", "owner_name": "Sofia Brandt", "next_owner_name": None,
        "sla_days": 2, "days": 5, "account_id": "V-000106", "posted_entity": None, "badges": [],
    }
    assert metrics.cell(None) is None


def test_compare_without_runs_lists_every_case_document(session: Session) -> None:
    result = metrics.compare(session)
    assert result["both_available"] is False
    assert [r["sample_no"] for r in result["rows"]] == list(range(1, 13))
    first = result["rows"][0]
    assert (first["supplier"], first["invoice_number"], first["gross_total"], first["currency"]) == \
        ("Nordwind Logistics GmbH", "NWL-2026-00913", 27846.0, "EUR")
    assert "designed_to_show" not in first  # brief v2: the comparison does not expose the test design
    assert all(r["asis"] is None and r["tobe"] is None for r in result["rows"])


def test_compare_matches_scenarios_by_sample_number(session: Session) -> None:
    store(session, mini_asis() + mini_tobe())
    result = metrics.compare(session)
    assert result["both_available"] is True
    row = next(r for r in result["rows"] if r["sample_no"] == 2)
    assert row["asis"]["doc_id"] == "A-02" and row["asis"]["badges"] == ["duplicate posting"]
    assert row["asis"]["account_id"] == "V-000117"
    assert row["tobe"]["doc_id"] == "B-02" and (row["tobe"]["outcome_word"], row["tobe"]["label"]) == ("Block", "Block")
    assert next(r for r in result["rows"] if r["sample_no"] == 6)["tobe"] is None


# --------------------------------------------------------------------------------------------
# Golden metrics: the real gate on the 12 case documents (fixture extraction; no API)
# --------------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def golden_run() -> dict[str, Any]:
    """Seed, load the case documents and run both scenarios once; returns plain dicts only."""
    init_db(drop=True)
    with SessionLocal() as s:
        seed.seed_all(s)
        for scenario in SCENARIOS:
            seed.load_sample_documents(s, scenario)
            gate.run_scenario(s, scenario, allow_api=False, log=lambda _line: None)
        results: dict[str, Any] = {scenario: metrics.compute(s, scenario) for scenario in SCENARIOS}
        results["compare"] = metrics.compare(s)
    return results


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_golden_metrics(golden_run, scenario: str) -> None:
    result = golden_run[scenario]
    assert result["available"] is True and result["run"] is not None
    expected = {k: v for k, v in GOLDEN["kpis"][scenario].items() if k in metrics.KPI_DEFS}
    assert {k: result["kpis"][k]["value"] for k in expected} == expected
    assert result["documents"] == GOLDEN["kpis"][scenario]["documents"]
    assert result["exceptions_by_type"] == GOLDEN["kpis"][scenario]["exceptions_by_type"]


def test_golden_displays_match_the_deck_story(golden_run) -> None:
    asis, tobe = golden_run["asis"]["kpis"], golden_run["tobe"]["kpis"]
    shown = {k: (asis[k]["display"], tobe[k]["display"]) for k in metrics.KPI_DEFS}
    assert shown == {"first_pass_match_rate": ("27.3%", "72.7%"), "accounts_per_supplier": ("2.33", "1.17"),
                     "touchless_rate": ("0.0%", "72.7%"), "cycle_time_median": ("15 days", "0 days (same day)"),
                     "registered_same_day": ("0.0%", "100.0%")}
    assert asis["first_pass_match_rate"]["value"] < 30  # deck slide 11: <30% today
    assert tobe["accounts_per_supplier"]["value"] <= 1.2  # deck slide 11 target
    assert asis["cycle_time_median"]["formula"].endswith("Here: median of 12 documents; P90 21 days.")
    # Brief v2: no euros (or any currency) in any metric.
    for kpi in list(asis.values()) + list(tobe.values()):
        assert not any(c in kpi["display"] + kpi["formula"] for c in ("EUR", "€", "USD", "CHF")), kpi["key"]


def test_golden_compare_rows(golden_run) -> None:
    result = golden_run["compare"]
    assert result["both_available"] is True and len(result["rows"]) == len(world.DOCUMENTS) == 12
    for row in result["rows"]:
        for scenario in SCENARIOS:
            expected = GOLDEN["documents"][row["sample_no"]][scenario]
            cell = row[scenario]
            assert cell["outcome"] == expected["outcome"], (row["sample_no"], scenario)
            assert cell["account_id"] == expected["account"], (row["sample_no"], scenario)
            assert cell["doc_id"] == seed.doc_id_for(scenario, row["sample_no"])
    badges = {(r["sample_no"], s): r[s]["badges"] for r in result["rows"] for s in SCENARIOS}
    assert "duplicate posting" in badges[(2, "asis")] and badges[(2, "tobe")] == []
    assert "unapplied credit" in badges[(4, "asis")] and "credit note linked" in badges[(4, "tobe")]
    assert "wrong entity" in badges[(10, "asis")]
    assert "catalogue match" in badges[(9, "tobe")]
    assert all("contract match" in badges[(no, "tobe")] for no in (10, 12))
    # same 14-day invoice terms (30 days agreed): the original (1) is posted before the agreed due date and paid
    # early, the resend (2) is posted after it and paid late (the gate stores invoice_date and posted_on)
    assert "terms paid early" in badges[(1, "asis")]
    assert "terms paid late" in badges[(2, "asis")] and "terms paid early" not in badges[(2, "asis")]
    words = {(r["sample_no"], s): r[s]["outcome_word"] for r in result["rows"] for s in SCENARIOS}
    assert {words[(no, "tobe")] for no in range(1, 13)} == {"Post", "Exception", "Block", "Human review"}
    assert result["asis"]["documents"] == result["asis"]["documents_total"] == len(world.DOCUMENTS)


# --------------------------------------------------------------------------------------------
# Statements, format badges, dataset-aware comparison
# --------------------------------------------------------------------------------------------

STATEMENT = dict(true_party_id="P-0001", supplier_name="Nordwind Logistics GmbH", invoice_number="KA-2026-11",
                 invoice_number_norm="KA202611", gross_total=56525.0, bill_to_entity="VDE", posted=True,
                 posted_entity="VDE", doc_type="statement")


def test_a_statement_posted_in_asis_is_badged_and_a_filed_one_is_never_counted_as_posted(session: Session) -> None:
    posted = decision("A-05", "exception", exception_type="email_loop", days=12, path="email_loop", **STATEMENT)
    assert "statement posted as invoice" in metrics.badges(posted)
    store(session, [decision("B-05", "exception", exception_type="payment_status_query", owner_name="Marco Ruiz",
                             **{**STATEMENT, "posted": False, "posted_entity": None})])
    kpis = metrics.compute(session, "tobe")["kpis"]
    assert kpis["touchless_rate"]["formula"].endswith("Here: 0 of 0.")  # never posted: not in the population
    assert kpis["first_pass_match_rate"]["formula"].endswith("Here: 0 of 1.")


def test_format_badges() -> None:
    ubl = pytest.importorskip("app.ubl")
    assert metrics.badges(decision("B-11", commitment="po", posted=True, extraction_model=ubl.UBL_MODEL)) == [
        "3-way match", "UBL e-invoice"]
    assert metrics.badges(decision("B-14", "human_review", exception_type="human_review",
                                   content_type="email_body")) == ["email body only"]
    assert metrics.badges(decision("A-03", "exception", exception_type="email_loop", **STATEMENT)) == [
        "statement posted as invoice"]
    assert metrics.badges(decision("B-01", extraction_model="gemini-3.8-flash", posted=True)) == []


def test_compare_follows_the_dataset_loaded_and_warns_on_a_mismatch(session: Session) -> None:
    seed.load_sample_documents(session, "asis", "v2")
    seed.load_sample_documents(session, "tobe", "v2")
    result = metrics.compare(session)
    assert (result["dataset"], result["dataset_label"], result["dataset_warning"]) == ("v2", "test set v2", None)
    assert len(result["rows"]) == 26 and result["rows"][25]["supplier"] == "Berliner Blumen GmbH"
    seed.load_sample_documents(session, "tobe", "v1")
    result = metrics.compare(session)
    assert result["datasets"] == {"asis": "v2", "tobe": "v1"} and result["dataset"] == "v2"
    assert result["dataset_warning"].startswith("The two scenarios hold different sample documents (A — As-is the "
                                                "test set v2, B — To-be the case documents)")


def test_compare_only_matches_decisions_of_the_rows_dataset(session: Session) -> None:
    """Sample number 2 exists in both datasets: a v1 decision (B-02) never fills the row of v2 document 2."""
    seed.load_sample_documents(session, "tobe", "v2")
    store(session, [decision("B-02", "blocked_duplicate", exception_type="duplicate_invoice"),
                    decision("B2-03", "exception", exception_type="payment_status_query",
                             **{**STATEMENT, "posted": False})])
    rows = {r["sample_no"]: r for r in metrics.compare(session)["rows"]}
    assert rows[2]["tobe"] is None and rows[3]["tobe"]["doc_id"] == "B2-03"
