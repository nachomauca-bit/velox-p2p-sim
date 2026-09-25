"""Golden test for test set v2: all 26 documents in both scenarios against tests/golden_v2.yaml (docs/TEST_SET_V2.md).

Same approach as tests/test_golden.py: a fresh seed, test set v2 loaded into both scenarios, extraction in fixture
mode (ground truth from tests/fixtures_v2/, the UBL parser for document 11, nothing for the email body of document
14; no API call) and one gate run per scenario. Each "documents" entry is one test case; the "kpis" block is checked
through metrics.compute.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from sqlalchemy import select

from app import extract, gate, metrics, seed, world
from app.config import SCENARIOS
from app.db import SessionLocal, init_db
from app.models import GateDecision, InboundDocument

GOLDEN = yaml.safe_load((Path(__file__).parent / "golden_v2.yaml").read_text(encoding="utf-8"))
DECISION_COLUMNS = ("outcome", "exception_type", "owner_name", "sla_days", "simulated_days")
CASES = [(scenario, no) for no, per_scenario in GOLDEN["documents"].items() for scenario in per_scenario]


@pytest.fixture(scope="module")
def results() -> dict[str, Any]:
    """Run both scenarios once on test set v2. Returns plain data: decisions per (scenario, no), run summaries,
    metrics.compute per scenario, metrics.compare and the documents' dataset / content type."""
    init_db(drop=True)
    with SessionLocal() as session:
        seed.seed_all(session)
        runs = {}
        for scenario in SCENARIOS:
            docs = seed.load_sample_documents(session, scenario, "v2")
            extract.extract_documents(session, docs, allow_api=False)
            runs[scenario] = gate.run_scenario(session, scenario, log=lambda line: None).summary_json
        rows = {}
        for d in session.scalars(select(GateDecision)):
            row = {column: getattr(d, column) for column in DECISION_COLUMNS}
            rows[(d.scenario, d.details["sample_no"])] = {**row, "doc_id": d.doc_id, "reason": d.reason,
                                                          "details": dict(d.details)}
        docs = {(d.scenario, d.sample_no): {"doc_id": d.doc_id, "dataset": d.dataset, "content_type": d.content_type,
                                           "model": d.extraction.model if d.extraction else None}
                for d in session.scalars(select(InboundDocument))}
        kpis = {scenario: metrics.compute(session, scenario) for scenario in SCENARIOS}
        compared = metrics.compare(session)
    return {"decisions": rows, "runs": runs, "kpis": kpis, "compare": compared, "docs": docs}


def actual(row: dict[str, Any], key: str) -> Any:
    """The value of a golden_v2.yaml key: decision columns, account, flag types, else a details key."""
    if key == "account":
        return row["details"]["account_id"]
    if key == "flags":
        return [flag["type"] for flag in row["details"]["flags"]]
    if key in DECISION_COLUMNS:
        return row[key]
    return row["details"][key]


@pytest.mark.parametrize("scenario, no", CASES, ids=[f"{s}-doc{n:02d}" for s, n in CASES])
def test_document_matches_golden_v2(results, scenario: str, no: int) -> None:
    expected = GOLDEN["documents"][no][scenario]
    got = {key: actual(results["decisions"][(scenario, no)], key) for key in expected}
    assert got == expected


def test_every_v2_document_has_a_golden_entry_in_both_scenarios(results) -> None:
    assert set(results["decisions"]) == {(s, no) for no in GOLDEN["documents"] for s in SCENARIOS}
    assert len(GOLDEN["documents"]) == len(world.documents_for("v2")) == 26


def test_v2_documents_are_loaded_as_dataset_v2(results) -> None:
    for (scenario, no), doc in results["docs"].items():
        assert doc["dataset"] == "v2" and doc["doc_id"] == seed.doc_id_for(scenario, no, "v2")
    assert results["docs"][("tobe", 11)]["content_type"] == "ubl_xml"
    assert results["docs"][("tobe", 14)]["content_type"] == "email_body"
    assert results["docs"][("tobe", 14)]["model"] is None  # nothing to extract: the invoice is in the email text


def kpi_value(result: dict[str, Any], key: str) -> Any:
    if key in ("documents", "exceptions_by_type"):
        return result[key]
    return result["kpis"][key]["value"]


def same(value: Any, expected: Any) -> bool:
    if isinstance(expected, float) or isinstance(value, float):
        return value is not None and round(float(value), 1) == round(float(expected), 1)
    return value == expected


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_kpis_match_golden_v2(results, scenario: str) -> None:
    result = results["kpis"][scenario]
    assert result["available"] is True and result["run"] is not None
    mismatches = {key: (kpi_value(result, key), expected) for key, expected in GOLDEN["kpis"][scenario].items()
                  if not same(kpi_value(result, key), expected)}
    assert not mismatches, f"(actual, expected) per KPI: {mismatches}"


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_run_summary_matches_golden_v2_kpis(results, scenario: str) -> None:
    summary, kpis = results["runs"][scenario], GOLDEN["kpis"][scenario]
    assert (summary["documents"], summary["touchless"], summary["exceptions"]) == (
        kpis["documents"], kpis["touchless"], kpis["exceptions"])
    # Document 14 has no document to extract; every other file is extracted (fixtures, UBL parser).
    assert summary["extraction"]["failed"] == 0


# --------------------------------------------------------------------------------------------
# What the rows designed to show must say (reasons and details behind the golden outcomes)
# --------------------------------------------------------------------------------------------


def test_statement_is_filed_in_tobe_and_posted_in_asis(results) -> None:
    tobe, asis = results["decisions"][("tobe", 3)], results["decisions"][("asis", 3)]
    assert tobe["details"]["doc_type"] == asis["details"]["doc_type"] == "other"
    assert "neither an invoice nor a credit note" in tobe["reason"] and tobe["details"]["contract_period"] is None
    assert asis["details"]["posted"] is True and asis["details"]["gross_total"] == 56525.0


def test_email_body_only_goes_to_human_review_with_its_reason(results) -> None:
    tobe, asis = results["decisions"][("tobe", 14)], results["decisions"][("asis", 14)]
    assert "the invoice is only in the email body" in tobe["reason"]
    assert (asis["details"]["account_id"], asis["details"]["posted"]) == (None, False)
    assert "only in the email body" in asis["reason"]


def test_multi_po_invoice_pinpoints_the_unconfirmed_po(results) -> None:
    row = results["decisions"][("tobe", 16)]
    assert row["details"]["po_numbers"] == ["4500128", "4500130"] and row["details"]["po_number"] == "4500130"
    assert "PO 4500130 exists but no service confirmation" in row["reason"]
    assert "the lines on PO 4500128 match" in row["reason"]
    assert {c["po_number"] for c in row["details"]["line_checks"]} == {"4500128", "4500130"}


def test_credit_note_without_reference_is_pending_not_unapplied(results) -> None:
    row = results["decisions"][("tobe", 7)]
    assert row["details"]["credit_status"] is None and row["details"]["posted"] is False


def test_compare_rows_are_the_v2_documents_with_format_badges(results) -> None:
    compared = results["compare"]
    assert compared["dataset"] == "v2" and compared["dataset_warning"] is None
    assert [r["sample_no"] for r in compared["rows"]] == list(range(1, 27))
    by_no = {r["sample_no"]: r for r in compared["rows"]}
    assert by_no[26]["supplier"] == "Berliner Blumen GmbH"
    for row in compared["rows"]:
        for scenario in SCENARIOS:
            assert row[scenario]["doc_id"] == seed.doc_id_for(scenario, row["sample_no"], "v2")
            assert row[scenario]["outcome"] == GOLDEN["documents"][row["sample_no"]][scenario]["outcome"]
    assert "statement posted as invoice" in by_no[3]["asis"]["badges"]
    assert "statement posted as invoice" not in by_no[3]["tobe"]["badges"]
    assert all("UBL e-invoice" in by_no[11][s]["badges"] for s in SCENARIOS)
    assert all("email body only" in by_no[14][s]["badges"] for s in SCENARIOS)
