"""KPIs and the A/B comparison (brief section 12; app/metrics.py).

Unit tests use synthetic GateDecision rows and pin the gate's scenario switches, so they do not depend on the
gate's rules. The golden test at the end runs the real gate on the 14 sample documents (fixture extraction) and
checks the scenario KPIs of tests/golden.yaml.
"""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml
from sqlalchemy.orm import Session

from app import gate, metrics, normalize, seed, world
from app.config import SCENARIOS
from app.db import SessionLocal, init_db
from app.models import GateDecision, InboundDocument, Run

GOLDEN = yaml.safe_load((Path(__file__).parent / "golden.yaml").read_text(encoding="utf-8"))

# The switches metrics reads from gate.SCENARIOS (values as in the shared contract).
CONFIGS = {
    "asis": {"contract_matching": False, "exception_routing": False, "doa_auto_approve_limit": None},
    "tobe": {"contract_matching": True, "exception_routing": True, "doa_auto_approve_limit": 500.0},
}

BASE_DETAILS: dict[str, Any] = dict(
    sample_no=None, supplier_name=None, invoice_number=None, invoice_number_norm=None, gross_total=0.0,
    net_total=0.0, currency="EUR", doc_type="invoice", party_id=None, true_party_id=None, account_id=None,
    resolution_method=None, bill_to_entity=None, posted_entity=None, posted=False, invoice_id=None,
    wrong_entity_posting=False, duplicate_posting=False, duplicate_of=None, commitment=None, po_number=None,
    contract_id=None, contract_period=None, doa_auto_approved=False, requester_name=None, next_owner_name=None,
    next_owner_role=None, credit_status=None, applied_to=None, flags=[], terms_days=None, terms_source=None,
    invoice_terms_days=None, agreed_terms_days=None, terms_variance_paid=False, touchless=False, path=None,
    cycle_breakdown={}, registration_lag_days=0, email_loop_days=None, line_checks=[], lookup_party_id=None,
    invoice_date=None, posted_on=None,
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


def mini_run(letter: str = "A") -> list[GateDecision]:
    """Four documents: an invoice posted twice (email loop), a clean PO posting and an unapplied credit note."""
    return [
        decision(f"{letter}-01", "exception", exception_type="email_loop", days=20, registration_lag_days=1,
                 terms_variance_paid=True, invoice_terms_days=14, agreed_terms_days=30, path="email_loop",
                 **NORDWIND),
        decision(f"{letter}-02", "exception", exception_type="email_loop", days=28, registration_lag_days=7,
                 duplicate_posting=True, duplicate_of=f"{letter}-01", path="email_loop",
                 **{**NORDWIND, "invoice_number": "NWL 2026 913 COPY", "account_id": "V-000117"}),
        decision(f"{letter}-03", days=2, registration_lag_days=1, touchless=True, true_party_id="P-0002",
                 invoice_number="INV-2026-0457", invoice_number_norm="INV20260457", gross_total=14400.0,
                 bill_to_entity="VFR", posted=True, posted_entity="VFR", commitment="po", po_number="4500117",
                 path="matched"),
        decision(f"{letter}-04", days=2, registration_lag_days=1, touchless=True, doc_type="credit_note",
                 true_party_id="P-0002", invoice_number="CN-2026-0031", invoice_number_norm="CN20260031",
                 gross_total=-1800.0, bill_to_entity="VFR", posted=True, posted_entity="VFR",
                 credit_status="unapplied", path="matched"),
    ]


@pytest.fixture()
def configs(monkeypatch):
    """Metrics reads the gate's scenario switches through metrics.scenario_config."""
    monkeypatch.setattr(metrics, "scenario_config", lambda scenario: CONFIGS[scenario])


def store(session: Session, decisions: list[GateDecision]) -> None:
    session.add_all(decisions)
    session.commit()


def test_synthetic_rows_follow_the_gate_contract() -> None:
    """Guards the synthetic data: same detail keys and scenario switches as the real gate."""
    assert set(BASE_DETAILS) == set(gate.DETAIL_KEYS)
    for scenario, switches in CONFIGS.items():
        assert {k: gate.SCENARIOS[scenario][k] for k in switches} == switches
        assert metrics.scenario_config(scenario) is gate.SCENARIOS[scenario]


# --------------------------------------------------------------------------------------------
# Formatting and arithmetic
# --------------------------------------------------------------------------------------------


def test_display_formats() -> None:
    assert metrics.fmt_pct(71.42857) == "71.4%"
    assert metrics.fmt_days(0.5) == "0.5 days"
    assert metrics.fmt_days(25.0, 0) == "25 days"
    assert metrics.fmt_money(29646) == "29,646.00 EUR"
    assert metrics.fmt_number(5) == "5"
    assert metrics.fmt_number(2.333, 1) == "2.3"
    for fmt in (metrics.fmt_pct, metrics.fmt_days, metrics.fmt_money, metrics.fmt_number):
        assert fmt(None) == metrics.NA


def test_amounts_are_never_converted_and_eur_comes_first() -> None:
    assert metrics.fmt_amounts({}) == "0.00 EUR"
    assert metrics.fmt_amounts({"USD": 500.0, "EUR": 1800.0}) == "1,800.00 EUR + 500.00 USD"


def test_pct_and_mean_handle_empty_denominators() -> None:
    assert metrics.pct(10, 14) == 71.4
    assert metrics.pct(0, 0) is None
    assert metrics.mean([2, 2, 1, 2] + [0] * 10) == 0.5
    assert metrics.mean([]) is None


# --------------------------------------------------------------------------------------------
# Pure KPI functions over decisions
# --------------------------------------------------------------------------------------------


def test_exceptions_by_type_counts_blocking_exceptions_only() -> None:
    decisions = [
        decision("B-05", "exception", exception_type="po_no_receipt"),
        decision("B-06", "exception", exception_type="price_qty_mismatch"),
        decision("B-12", "exception", exception_type="price_qty_mismatch"),
        decision("B-09", "human_review", exception_type="human_review"),
        decision("B-02", "blocked_duplicate", exception_type="duplicate_invoice"),  # blocked, not an exception
        decision("B-01", flags=[{"type": "terms_variance", "label": "", "owner_name": None, "detail": ""}]),
    ]
    assert metrics.exceptions_by_type(decisions) == {"po_no_receipt": 1, "price_qty_mismatch": 2, "human_review": 1}
    assert list(metrics.exceptions_by_type(decisions)) == ["po_no_receipt", "price_qty_mismatch", "human_review"]
    assert metrics.info_flags_by_type(decisions) == {"terms_variance": 1}
    assert metrics.outcome_counts(decisions) == {"posted": 1, "exception": 3, "human_review": 1,
                                                 "blocked_duplicate": 1}


def test_exception_without_a_type_is_counted_under_its_outcome() -> None:
    assert metrics.exceptions_by_type([decision("B-09", "human_review")]) == {"human_review": 1}


def test_duplicate_groups_by_real_supplier_and_normalised_number() -> None:
    decisions = mini_run()
    groups = metrics.duplicate_groups(decisions)
    assert [[d.doc_id for d in g] for g in groups] == [["A-01", "A-02"]]  # processing order: first is legitimate


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


def test_cash_leakage_is_repeated_postings_plus_unapplied_credits() -> None:
    assert metrics.cash_leakage(mini_run()) == {"EUR": 29646.0}  # 27,846.00 + |-1,800.00|


def test_cash_leakage_sums_each_currency_separately() -> None:
    usd = dict(true_party_id="P-0011", invoice_number="HFF-2026-0930", gross_total=17250.0, currency="USD",
               posted=True)
    decisions = mini_run() + [decision("A-14", **usd), decision("A-15", **usd), decision("A-16", **usd)]
    assert metrics.cash_leakage(decisions) == {"EUR": 29646.0, "USD": 34500.0}  # two repeats of the USD invoice


def test_cash_leakage_is_zero_without_duplicates_or_unapplied_credits() -> None:
    assert metrics.cash_leakage([decision("B-04", credit_status="applied", gross_total=-1800.0)]) == {}


@pytest.mark.parametrize("printed, expected", [("4500117", "4500117"), ("PO 4500117", "4500117"),
                                               ("po-4500117", "4500117"), (" 4500 117 ", "4500117"),
                                               ("P.O. #4500117", "4500117"), (None, "")])
def test_po_key(printed: str, expected: str) -> None:
    assert metrics.po_key(printed) == expected
    assert metrics.po_key(printed) == normalize.normalise_po_number(printed)  # the gate's normalisation


def test_has_commitment_by_po_or_contract() -> None:
    kwargs = dict(existing_pos={"4500117"}, contracts={("P-0001", "VDE")})
    assert metrics.has_commitment(["PO 4500117"], "P-0002", "VFR", use_contracts=False, **kwargs)
    assert not metrics.has_commitment(["4500123"], "P-0003", "VDE", use_contracts=True, **kwargs)  # PO not in ERP
    assert metrics.has_commitment([], "P-0001", "VDE", use_contracts=True, **kwargs)
    assert not metrics.has_commitment([], "P-0001", "VDE", use_contracts=False, **kwargs)  # contracts not used
    assert not metrics.has_commitment([], "P-0001", "VFR", use_contracts=True, **kwargs)  # other entity


# --------------------------------------------------------------------------------------------
# Vendor master quality (no run needed)
# --------------------------------------------------------------------------------------------


def test_vendor_master_quality_dirty_and_clean(session: Session) -> None:
    asis = metrics.vendor_master_quality(session, "asis")
    assert (asis["accounts"], asis["parties"], asis["ratio"]) == (28, 12, 2.3)
    assert (asis["with_vat_iban"], asis["pct_vat_iban"]) == (20, 71.4)  # D4: 8 accounts miss an identifier
    # D3 (5 linked accounts) + D1/D2 (8 unlinked accounts) have non-agreed terms; D5 leftovers keep agreed terms.
    assert (asis["terms_ok"], asis["terms_differ"], asis["pct_terms_ok"]) == (15, 13, 53.6)
    assert asis["duplicates_flagged"] > 0
    tobe = metrics.vendor_master_quality(session, "tobe")
    assert (tobe["accounts"], tobe["ratio"], tobe["pct_vat_iban"], tobe["pct_terms_ok"]) == (16, 1.3, 100.0, 100.0)
    assert tobe["duplicates_flagged"] == 0  # one account per party per legal entity is by design


def test_vendor_master_stats_without_data() -> None:
    stats = metrics.vendor_master_stats([], [], [])
    assert (stats["accounts"], stats["ratio"], stats["pct_vat_iban"], stats["pct_terms_ok"]) == (0, None, None, None)


# --------------------------------------------------------------------------------------------
# Reference path: a non-PO invoice sent to a store
# --------------------------------------------------------------------------------------------


def test_reference_path_asis_uses_the_mean_email_loop(configs) -> None:
    steps = metrics.reference_nonpo_store_path("asis")
    assert steps == {"store_forwarding": 7, "ap_open_and_key": 1, "email_loop": 12, "email_approval": 4, "posting": 1}
    assert sum(steps.values()) == 25


def test_reference_path_tobe_is_the_no_po_sla_plus_approval(configs) -> None:
    steps = metrics.reference_nonpo_store_path("tobe")
    assert steps["exception_sla"] == 2 and steps["workflow_approval"] == 1
    assert sum(steps.values()) == 3


# --------------------------------------------------------------------------------------------
# compute()
# --------------------------------------------------------------------------------------------


def test_compute_without_a_run(session: Session, configs) -> None:
    result = metrics.compute(session, "asis")
    assert result["available"] is False and result["run"] is None and result["documents"] == 0
    assert result["documents_total"] == 0  # empty inbox
    assert list(result["kpis"]) == list(metrics.KPI_DEFS)
    no_run_needed = {"accounts_per_supplier", "pct_accounts_vat_iban", "pct_accounts_terms_ok",
                     "reference_nonpo_store_days"}
    for key, kpi in result["kpis"].items():
        assert set(kpi) == {"key", "label", "value", "display", "formula", "group", "unit"}
        assert kpi["key"] == key and kpi["formula"] and kpi["group"] in ("upstream", "downstream")
        if key in no_run_needed:
            assert kpi["value"] is not None, key
        else:
            assert kpi["value"] is None and kpi["display"] == metrics.NA, key
            assert "Here:" not in kpi["formula"], key
    assert result["kpis"]["accounts_per_supplier"]["display"] == "2.3"
    assert result["kpis"]["reference_nonpo_store_days"]["value"] == 25
    assert result["kpis"]["reference_nonpo_store_days"]["display"] == "25 days"
    assert (result["exceptions_by_type"], result["info_flags_by_type"], result["outcomes"], result["cycle_by_doc"]) \
        == ({}, {}, {}, [])


def test_compute_reference_path_display_tobe(session: Session, configs) -> None:
    kpi = metrics.compute(session, "tobe")["kpis"]["reference_nonpo_store_days"]
    assert kpi["value"] == 3 and kpi["display"] == "3 days (0 under the 500 EUR DoA limit)"
    assert "no-PO exception SLA" in kpi["formula"]


def test_compute_on_synthetic_decisions(session: Session, configs) -> None:
    store(session, mini_run("A"))
    session.add(Run(run_id="run-asis-test", scenario="asis", started_on=datetime(2026, 10, 16, 9, 0),
                    finished_on=datetime(2026, 10, 16, 9, 1), summary_json={}))
    session.commit()
    result = metrics.compute(session, "asis")
    k = {key: kpi["value"] for key, kpi in result["kpis"].items()}
    assert result["available"] is True and result["documents"] == 4
    assert result["run"] == {"run_id": "run-asis-test", "finished_on": datetime(2026, 10, 16, 9, 1)}
    assert (k["touchless"], k["touchless_rate"], k["exceptions"], k["exception_rate"]) == (2, 50.0, 2, 50.0)
    assert (k["duplicates_blocked"], k["duplicate_postings"], k["duplicate_invoices"]) == (0, 2, 1)
    assert (k["credit_notes_applied"], k["credit_notes_unapplied"]) == (0, 1)
    assert (k["wrong_entity_postings"], k["terms_variance_paid"]) == (0, 1)
    assert k["cash_leakage_amount"] == 29646.0
    assert result["kpis"]["cash_leakage_amount"]["display"] == "29,646.00 EUR"
    assert ("Here: 1 repeated posting(s) 27,846.00 EUR + 1 unapplied credit note(s) 1,800.00 EUR; "
            "1 posting(s) on non-agreed terms") in result["kpis"]["cash_leakage_amount"]["formula"]
    assert k["registration_lag_days"] == 2.5  # (3 x 1 + 1 x 7) / 4
    assert result["kpis"]["registration_lag_days"]["formula"].endswith("Here: (3 × 1 + 1 × 7) ÷ 4.")
    assert (k["avg_cycle_days"], k["avg_cycle_followup_days"]) == (13.0, 24.0)  # 52 / 4 and 48 / 2
    # 3 invoices (the credit note is excluded); only A-03's PO 4500117 exists in the as-is ERP.
    assert k["po_contract_coverage"] == 33.3
    assert result["kpis"]["po_contract_coverage"]["formula"].endswith("Here: 1 of 3 invoices.")
    assert result["exceptions_by_type"] == {"email_loop": 2}
    assert result["outcomes"] == {"posted": 2, "exception": 2}
    assert result["cycle_by_doc"][1] == {"doc_id": "A-02", "sample_no": 2, "days": 28, "path": "email_loop"}


def test_compute_counts_documents_and_can_keep_the_sample_documents_only(session: Session, configs) -> None:
    template = seed.load_sample_documents(session, "asis")[0]  # 14 inbound sample documents
    session.add(InboundDocument(  # a webhook upload (sample_no 0)
        doc_id="A-W01", scenario="asis", sample_no=0, channel=template.channel, mailbox=template.mailbox,
        received_on=template.received_on, file_path=template.file_path, file_hash=template.file_hash,
        sender_email="billing@example.com", subject="Upload", registered=False, registered_on=None,
        doc_type="unknown"))
    store(session, mini_run("A") + [decision("A-W01", touchless=True, posted=True, registration_lag_days=1)])
    everything = metrics.compute(session, "asis")
    assert (everything["documents"], everything["documents_total"]) == (5, 15)  # 10 documents not processed
    assert everything["kpis"]["touchless_rate"]["value"] == 60.0  # 3 of 5
    sample = metrics.compute(session, "asis", sample_only=True)
    assert (sample["documents"], sample["documents_total"], sample["sample_only"]) == (4, 14, True)
    assert sample["kpis"]["touchless_rate"]["value"] == 50.0  # 2 of 4: the upload is left out
    assert sample["kpis"]["touchless_rate"]["formula"].endswith("Here: 2 ÷ 4.")
    assert [c["doc_id"] for c in sample["cycle_by_doc"]] == ["A-01", "A-02", "A-03", "A-04"]
    compared = metrics.compare(session)["asis"]  # the comparison uses the sample documents only
    assert (compared["documents"], compared["documents_total"], compared["sample_only"]) == (4, 14, True)


def test_touchless_formula_says_what_touchless_means_without_a_gate(session: Session, configs) -> None:
    asis = metrics.compute(session, "asis")["kpis"]["touchless"]["formula"]
    tobe = metrics.compute(session, "tobe")["kpis"]["touchless"]["formula"]
    assert 'Without a gate, "touchless" means no exception follow-up after intake; intake itself is delayed' in asis
    assert "Without a gate" not in tobe


def test_contracts_count_as_commitments_only_where_the_scenario_uses_them(session: Session, configs) -> None:
    store(session, mini_run("B"))
    # To-be: PO 4500117 exists and Nordwind has a recurring contract with VDE -> 3 of 3 invoices.
    assert metrics.compute(session, "tobe")["kpis"]["po_contract_coverage"]["value"] == 100.0


def test_compute_edge_cases_no_invoices_and_all_touchless(session: Session, configs) -> None:
    store(session, [decision("B-04", "applied_credit", doc_type="credit_note", credit_status="applied",
                             touchless=True, gross_total=-1800.0)])
    kpis = metrics.compute(session, "tobe")["kpis"]
    assert kpis["po_contract_coverage"]["value"] is None  # no invoice: no denominator
    assert kpis["avg_cycle_followup_days"]["value"] is None  # no document needed a human step
    assert kpis["touchless_rate"]["value"] == 100.0
    assert kpis["cash_leakage_amount"]["display"] == "0.00 EUR"


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
    (decision("A-08", posted=True, commitment="po", wrong_entity_posting=True), ["wrong entity"]),
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
    (decision("B-02", "blocked_duplicate", exception_type="duplicate_invoice"), ["blocked duplicate"]),
    (decision("B-04", "applied_credit", credit_status="applied"), ["credit applied"]),
    (decision("B-10", posted=True, commitment="none", doa_auto_approved=True), ["DoA auto-approved"]),
    (decision("B-09", posted=True, commitment="po"), ["3-way match"]),
    (decision("B-01", posted=True, commitment="contract",
              flags=[{"type": "terms_variance", "label": "", "owner_name": None, "detail": ""}]),
     ["contract match", "terms variance flagged"]),
    (decision("B-05", "exception", exception_type="po_no_receipt", commitment="po"), []),
])
def test_badges(dec: GateDecision, expected: list[str]) -> None:
    assert metrics.badges(dec) == expected


def test_scheduled_payment_is_the_later_of_posting_and_due_date() -> None:
    before_due = decision("A-01", posted=True, posted_on="2026-10-05T09:00:00", **NORDWIND_TERMS)
    assert metrics.scheduled_payment(before_due) == date(2026, 10, 14)  # 30 Sep + 14 days
    after_due = decision("A-01", posted=True, posted_on="2026-10-22T08:42:00", **NORDWIND_TERMS)
    assert metrics.scheduled_payment(after_due) == date(2026, 10, 22)
    assert metrics.scheduled_payment(decision("A-01", posted=True, **NORDWIND_TERMS)) is None  # no posting date
    assert metrics.as_date("2026-09-30") == date(2026, 9, 30) and metrics.as_date("not a date") is None


def test_outcome_labels() -> None:
    assert metrics.outcome_label(decision("A-01", "exception", exception_type="email_loop")) == \
        "Untracked manual follow-up"
    assert metrics.outcome_label(decision("B-05", "exception", exception_type="po_no_receipt")) == \
        "PO exists, no receipt or service confirmation"
    assert metrics.outcome_label(decision("B-02", "blocked_duplicate", exception_type="duplicate_invoice")) == \
        "Blocked duplicate"
    assert metrics.outcome_label(decision("B-04", "applied_credit")) == "Credit applied"


def test_cell() -> None:
    dec = decision("B-12", "exception", exception_type="price_qty_mismatch", days=2.0, owner_name="Tim Koch",
                   sla_days=2, next_owner_name="Sofia Brandt", account_id="V-000112", commitment="po")
    assert metrics.cell(dec) == {
        "doc_id": "B-12", "outcome": "exception", "exception_type": "price_qty_mismatch",
        "label": "Price or quantity outside tolerance", "owner_name": "Tim Koch", "next_owner_name": "Sofia Brandt",
        "sla_days": 2, "days": 2, "account_id": "V-000112", "posted_entity": None, "badges": [],
    }
    assert metrics.cell(None) is None


def test_compare_without_runs_lists_every_sample_document(session: Session, configs) -> None:
    result = metrics.compare(session)
    assert result["both_available"] is False
    assert [r["sample_no"] for r in result["rows"]] == sorted(d.no for d in world.DOCUMENTS)
    first = result["rows"][0]
    assert (first["supplier"], first["invoice_number"], first["gross_total"], first["currency"]) == \
        ("Nordwind Logistics GmbH", "NWL-2026-00913", 27846.0, "EUR")
    assert first["designed_to_show"] == world.DOCUMENT_BY_NO[1].designed_to_show
    assert all(r["asis"] is None and r["tobe"] is None for r in result["rows"])


def test_compare_matches_scenarios_by_sample_number(session: Session, configs) -> None:
    store(session, mini_run("A") + [decision("B-02", "blocked_duplicate", exception_type="duplicate_invoice",
                                             owner_name="Marco Ruiz", sla_days=0, touchless=True)])
    result = metrics.compare(session)
    assert result["both_available"] is True
    row = next(r for r in result["rows"] if r["sample_no"] == 2)
    assert row["asis"]["doc_id"] == "A-02" and row["asis"]["badges"] == ["duplicate posting"]
    assert row["asis"]["account_id"] == "V-000117"
    assert row["tobe"]["doc_id"] == "B-02" and row["tobe"]["badges"] == ["blocked duplicate"]
    assert next(r for r in result["rows"] if r["sample_no"] == 5)["tobe"] is None


# --------------------------------------------------------------------------------------------
# Golden KPIs: the real gate on the 14 sample documents (fixture extraction; no API)
# --------------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def golden_run() -> dict[str, Any]:
    """Seed, load the sample documents and run both scenarios once; returns plain dicts only."""
    init_db(drop=True)
    with SessionLocal() as s:
        seed.seed_all(s)
        for scenario in SCENARIOS:
            seed.load_sample_documents(s, scenario)
            gate.run_scenario(s, scenario, allow_api=False, log=lambda _line: None)
        results: dict[str, Any] = {scenario: metrics.compute(s, scenario) for scenario in SCENARIOS}
        results["compare"] = metrics.compare(s)
    return results


def kpi_value(result: dict[str, Any], key: str) -> Any:
    if key in ("documents", "exceptions_by_type"):
        return result[key]
    return result["kpis"][key]["value"]


def same(actual: Any, expected: Any) -> bool:
    if isinstance(expected, float) or isinstance(actual, float):
        return actual is not None and round(float(actual), 1) == round(float(expected), 1)
    return actual == expected


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_golden_kpis(golden_run, scenario: str) -> None:
    result = golden_run[scenario]
    assert result["available"] is True and result["run"] is not None
    mismatches = {key: (kpi_value(result, key), expected) for key, expected in GOLDEN["kpis"][scenario].items()
                  if not same(kpi_value(result, key), expected)}
    assert not mismatches, f"(actual, expected) per KPI: {mismatches}"


def test_golden_displays_and_reference_paths(golden_run) -> None:
    asis, tobe = golden_run["asis"]["kpis"], golden_run["tobe"]["kpis"]
    assert asis["cash_leakage_amount"]["display"] == "29,646.00 EUR"
    assert tobe["cash_leakage_amount"]["display"] == "0.00 EUR"
    assert tobe["touchless_rate"]["display"] == "71.4%"
    assert tobe["avg_cycle_days"]["display"] == "0.5 days"
    assert asis["registration_lag_days"]["formula"].endswith("Here: (11 × 1 + 3 × 7) ÷ 14.")
    assert (asis["reference_nonpo_store_days"]["value"], tobe["reference_nonpo_store_days"]["value"]) == (25, 3)
    # Brief section 2: the as-is cycle is far longer than the to-be one (1–3 days for exceptions).
    assert asis["avg_cycle_days"]["value"] > 10 * tobe["avg_cycle_days"]["value"]
    assert tobe["avg_cycle_followup_days"]["value"] <= 3


def test_golden_compare_rows(golden_run) -> None:
    result = golden_run["compare"]
    assert result["both_available"] is True and len(result["rows"]) == len(world.DOCUMENTS)
    for row in result["rows"]:
        for scenario in SCENARIOS:
            expected = GOLDEN["documents"][row["sample_no"]][scenario]
            cell = row[scenario]
            assert cell["outcome"] == expected["outcome"], (row["sample_no"], scenario)
            assert cell["account_id"] == expected["account"], (row["sample_no"], scenario)
            assert cell["doc_id"] == seed.doc_id_for(scenario, row["sample_no"])
    badges = {(r["sample_no"], s): r[s]["badges"] for r in result["rows"] for s in SCENARIOS}
    assert "duplicate posting" in badges[(2, "asis")] and "blocked duplicate" in badges[(2, "tobe")]
    assert "unapplied credit" in badges[(4, "asis")] and "credit applied" in badges[(4, "tobe")]
    assert "wrong entity" in badges[(8, "asis")] and "wrong entity" in badges[(11, "asis")]
    assert "DoA auto-approved" in badges[(10, "tobe")]
    assert all("contract match" in badges[(no, "tobe")] for no in (1, 11, 14))
    assert "terms variance flagged" in badges[(1, "tobe")]
    # same 14-day invoice terms (30 days agreed): the original (1) is posted before the agreed due date and paid
    # early, the resend (2) is posted after it and paid late (the gate stores invoice_date and posted_on)
    assert "terms paid early" in badges[(1, "asis")]
    assert "terms paid late" in badges[(2, "asis")] and "terms paid early" not in badges[(2, "asis")]
    assert result["asis"]["documents"] == result["asis"]["documents_total"] == len(world.DOCUMENTS)
