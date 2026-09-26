"""Golden test: every case document in both scenarios against tests/golden.yaml (brief v2).

Seeds a fresh database (as the conftest `session` fixture does), loads the sample documents into both
scenarios, extracts them in fixture mode (ground truth, no API call) and runs the control gate once per
scenario. Each "documents" entry of golden.yaml is one test case.
"""
from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

import pytest
import yaml
from sqlalchemy import select

from app import extract, gate, metrics, seed
from app.config import SCENARIOS
from app.db import SessionLocal, init_db
from app.models import GateDecision

GOLDEN = yaml.safe_load((Path(__file__).parent / "golden.yaml").read_text(encoding="utf-8"))
DECISION_COLUMNS = ("outcome", "exception_type", "owner_name", "sla_days", "simulated_days")
CASES = [(scenario, no) for no, per_scenario in GOLDEN["documents"].items() for scenario in per_scenario]


@pytest.fixture(scope="module")
def results() -> dict[str, Any]:
    """Run both scenarios once. Returns plain data: {"decisions": {(scenario, no): row}, "runs": {scenario: summary},
    "metrics": {scenario: {key: value}}}."""
    init_db(drop=True)
    with SessionLocal() as session:
        seed.seed_all(session)
        runs = {}
        for scenario in SCENARIOS:
            docs = seed.load_sample_documents(session, scenario)
            extract.extract_documents(session, docs, allow_api=False)
            runs[scenario] = gate.run_scenario(session, scenario, log=lambda line: None).summary_json
        rows = {}
        for d in session.scalars(select(GateDecision)):
            row = {column: getattr(d, column) for column in DECISION_COLUMNS}
            rows[(d.scenario, d.details["sample_no"])] = {**row, "details": dict(d.details)}
        values = {s: {k: m["value"] for k, m in metrics.compute(session, s)["kpis"].items()} for s in SCENARIOS}
    return {"decisions": rows, "runs": runs, "metrics": values}


def actual(row: dict[str, Any], key: str) -> Any:
    """The value of a golden.yaml key: decision columns, account, flag types, else a details key."""
    if key == "account":
        return row["details"]["account_id"]
    if key == "flags":
        return [flag["type"] for flag in row["details"]["flags"]]
    if key in DECISION_COLUMNS:
        return row[key]
    return row["details"][key]


@pytest.mark.parametrize("scenario, no", CASES, ids=[f"{s}-doc{n:02d}" for s, n in CASES])
def test_document_matches_golden(results, scenario: str, no: int) -> None:
    expected = GOLDEN["documents"][no][scenario]
    got = {key: actual(results["decisions"][(scenario, no)], key) for key in expected}
    assert got == expected


def test_every_sample_document_has_a_golden_entry_in_both_scenarios(results) -> None:
    assert set(results["decisions"]) == {(s, no) for no in GOLDEN["documents"] for s in SCENARIOS}


# --------------------------------------------------------------------------------------------
# The scenario-level numbers of golden.yaml that follow directly from the decisions' details
# (metrics.py owns the KPI definitions; this checks the gate records what they need).
# --------------------------------------------------------------------------------------------


def _scenario_counts(results: dict[str, Any], scenario: str) -> dict[str, Any]:
    rows = [r for (s, _), r in results["decisions"].items() if s == scenario]
    details = [r["details"] for r in rows]
    blocking = [r for r in rows if r["outcome"] in gate.BLOCKING_OUTCOMES]
    return {
        "documents": len(rows),
        "touchless": sum(d["touchless"] for d in details),
        "exceptions": len(blocking),
        "exceptions_by_type": dict(Counter(r["exception_type"] for r in blocking)),
        "duplicates_blocked": sum(r["outcome"] == "blocked_duplicate" for r in rows),
        "duplicate_invoices": sum(d["duplicate_posting"] for d in details),
        "credit_notes_applied": sum(d["credit_status"] == "applied" for d in details),
        "credit_notes_unapplied": sum(d["credit_status"] == "unapplied" for d in details),
        "wrong_entity_postings": sum(d["wrong_entity_posting"] for d in details),
    }


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_scenario_counts_match_golden_kpis(results, scenario: str) -> None:
    counts = _scenario_counts(results, scenario)
    expected = {k: v for k, v in GOLDEN["kpis"][scenario].items() if k in counts}
    assert {k: counts[k] for k in expected} == expected


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_run_summary_matches_golden_kpis(results, scenario: str) -> None:
    summary, kpis = results["runs"][scenario], GOLDEN["kpis"][scenario]
    assert (summary["documents"], summary["touchless"], summary["exceptions"]) == (
        kpis["documents"], kpis["touchless"], kpis["exceptions"])
    assert summary["extraction"]["unavailable"] == 0 and summary["extraction"]["failed"] == 0


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_the_four_metrics_match_golden_kpis(results, scenario: str) -> None:
    """The four metrics of deck slide 11 (A6 definitions) and the fifth indicator, as metrics.py computes them."""
    expected = {k: v for k, v in GOLDEN["kpis"][scenario].items() if k in metrics.KPI_DEFS}
    assert set(expected) == set(metrics.KPI_DEFS)
    assert {k: results["metrics"][scenario][k] for k in expected} == expected
