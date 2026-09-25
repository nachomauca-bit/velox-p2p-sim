"""KPI export (app/export_bq.py): NDJSON files from a seeded database after a gate run, and the BigQuery load
path with a fake client and a fake google.cloud.bigquery module (the real library is not needed, and no test
reaches Google Cloud)."""
from __future__ import annotations

import json
import re
import sys
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from app import config, export_bq, gate, seed
from app.export_bq import TABLES, ExportError
from app.models import Base, GateDecision

ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
PY_TYPES = {"STRING": str, "INTEGER": int, "FLOAT": (int, float), "BOOLEAN": bool}


def quiet(_line: str) -> None:
    pass


@pytest.fixture()
def export_dir(tmp_path, monkeypatch) -> Path:
    folder = tmp_path / "export"
    monkeypatch.setattr(config, "EXPORT_DIR", folder)
    monkeypatch.setattr(config, "BQ_EXPORT", False)
    monkeypatch.setattr(config, "BQ_PROJECT", "velox-demo")
    monkeypatch.setattr(config, "BQ_DATASET", "velox_p2p")
    return folder


@pytest.fixture()
def after_runs(session):
    """Both scenarios loaded with the sample documents and run through the gate (fixture extraction)."""
    for scenario in config.SCENARIOS:
        seed.load_sample_documents(session, scenario)
        gate.run_scenario(session, scenario, log=quiet)
    return session


def read_ndjson(path: Path) -> list[dict]:
    text = path.read_text(encoding="utf-8")
    assert text == "" or text.endswith("\n")
    return [json.loads(line) for line in text.splitlines()]


def db_count(session, name: str) -> int:
    return session.scalar(select(func.count()).select_from(Base.metadata.tables[name]))


# --------------------------------------------------------------------------------------------
# NDJSON files
# --------------------------------------------------------------------------------------------


def test_export_writes_one_ndjson_file_per_table(after_runs, export_dir, capsys):
    counts = export_bq.export_all(after_runs)

    assert list(counts) == list(TABLES)
    for name in TABLES:
        rows = read_ndjson(export_dir / f"{name}.ndjson")
        assert counts[name] == len(rows) == db_count(after_runs, name), name
        schema = json.loads((export_dir / f"{name}.schema.json").read_text(encoding="utf-8"))
        assert schema == export_bq.table_schema(name)
        assert all(field["mode"] == "NULLABLE" for field in schema)
        names = [field["name"] for field in schema]
        assert len(names) == len(set(names)), name
        for row in rows:
            assert list(row) == names, name  # every row carries every column, in schema order
    assert counts["gate_decision"] == counts["inbound_document"] > 0 and counts["run"] == 2
    assert not list(export_dir.glob("*.tmp"))  # atomic writes leave nothing behind
    assert "[export] 12 tables written to" in capsys.readouterr().out


def test_values_match_their_bigquery_types(after_runs, export_dir):
    export_bq.export_all(after_runs)
    for name in TABLES:
        kinds = {field["name"]: field["type"] for field in export_bq.table_schema(name)}
        for row in read_ndjson(export_dir / f"{name}.ndjson"):
            for key, value in row.items():
                if value is None:
                    continue
                kind = kinds[key]
                if kind == "DATE":
                    assert ISO_DATE.match(value), (name, key, value)
                elif kind == "DATETIME":
                    datetime.fromisoformat(value)
                else:
                    assert isinstance(value, PY_TYPES[kind]), (name, key, value)
                    assert kind != "INTEGER" or not isinstance(value, bool), (name, key)


def test_gate_decisions_keep_their_json_and_get_flat_fields(after_runs, export_dir):
    export_bq.export_all(after_runs)
    rows = {row["doc_id"]: row for row in read_ndjson(export_dir / "gate_decision.ndjson")}
    decisions = list(after_runs.scalars(select(GateDecision)))
    assert set(rows) == {d.doc_id for d in decisions}
    for decision in decisions:
        row = rows[decision.doc_id]
        assert json.loads(row["details"]) == decision.details  # JSON text in a STRING column
        assert json.loads(row["steps"]) == decision.steps
        assert row["outcome"] == decision.outcome and row["scenario"] == decision.scenario
        assert row["touchless"] is bool(decision.details.get("touchless"))
        assert row["supplier_name"] == decision.details.get("supplier_name")
        assert row["gross_total"] == decision.details.get("gross_total")
        assert row["wrong_entity_posting"] is bool(decision.details.get("wrong_entity_posting"))
    tobe = [row for row in rows.values() if row["scenario"] == "tobe"]
    assert any(row["touchless"] for row in tobe) and any(not row["touchless"] for row in tobe)
    if "dataset" in rows[decisions[0].doc_id]:  # the document's dataset (models.InboundDocument.dataset)
        assert {row["dataset"] for row in rows.values()} == {"v1"}


def test_mock_tables_hold_both_scenarios(after_runs, export_dir):
    export_bq.export_all(after_runs)
    accounts = read_ndjson(export_dir / "vendor_account.ndjson")
    assert {row["scenario"] for row in accounts} == {"asis", "tobe"}
    assert all(ISO_DATE.match(row["created_on"]) for row in accounts)
    assert all(isinstance(json.loads(row["corruption_rules"]), list) for row in accounts)
    runs = read_ndjson(export_dir / "run.ndjson")
    assert all(json.loads(row["summary_json"])["documents"] > 0 for row in runs)


def test_export_of_an_empty_run_writes_empty_files(session, export_dir):
    counts = export_bq.export_all(session)
    assert counts["gate_decision"] == counts["run"] == counts["inbound_document"] == 0
    assert (export_dir / "gate_decision.ndjson").read_text(encoding="utf-8") == ""
    assert counts["legal_entity"] > 0  # the mock ERP is seeded


def test_a_second_export_replaces_the_files(after_runs, export_dir):
    export_bq.export_all(after_runs)
    gate.clear_results(after_runs, "tobe")
    counts = export_bq.export_all(after_runs)
    assert len(read_ndjson(export_dir / "gate_decision.ndjson")) == counts["gate_decision"]
    assert counts["gate_decision"] == db_count(after_runs, "gate_decision")


@pytest.mark.parametrize("value, kind, expected", [
    (datetime(2026, 11, 2, 8, 15), "DATETIME", "2026-11-02T08:15:00"),
    (date(2026, 11, 2), "DATE", "2026-11-02"),
    ({"b": 1, "a": [1, "ü"]}, "STRING", '{"a": [1, "ü"], "b": 1}'),
    (["D1", "D4"], "STRING", '["D1", "D4"]'),
    (1, "BOOLEAN", True),
    ("7", "INTEGER", 7),
    ("n/a", "INTEGER", None),  # a value that does not fit becomes null, never a failed load
    (3, "FLOAT", 3.0),
    (None, "STRING", None),
])
def test_to_json_value(value, kind, expected):
    assert export_bq.to_json_value(value, kind) == expected


# --------------------------------------------------------------------------------------------
# BigQuery load (fake module and client)
# --------------------------------------------------------------------------------------------


class FakeJob:
    def __init__(self, error: Exception | None = None):
        self.error = error
        self.waited = False

    def result(self):
        self.waited = True
        if self.error:
            raise self.error
        return self


class FakeBigQueryClient:
    def __init__(self, project: str | None = None, fail_on: str | None = None):
        self.project = project
        self.fail_on = fail_on
        self.loads: list[dict] = []
        self.deleted: list[str] = []
        self.created: list[SimpleNamespace] = []
        self.jobs: list[FakeJob] = []

    def load_table_from_file(self, source, table_id, job_config=None):
        self.loads.append({"table_id": table_id, "data": source.read(), "config": job_config})
        job = FakeJob(RuntimeError("400 Error while reading data") if self.fail_on == table_id else None)
        self.jobs.append(job)
        return job

    def delete_table(self, table_id, not_found_ok=False):
        assert not_found_ok
        self.deleted.append(table_id)

    def create_table(self, table):
        self.created.append(table)
        return table


def fake_bigquery_module(clients: list[FakeBigQueryClient]) -> SimpleNamespace:
    """The parts of google.cloud.bigquery that export_bq uses."""
    def make_client(project=None):
        client = FakeBigQueryClient(project)
        clients.append(client)
        return client

    return SimpleNamespace(
        Client=make_client,
        LoadJobConfig=lambda **kwargs: SimpleNamespace(**kwargs),
        SchemaField=lambda name, field_type, mode="NULLABLE": (name, field_type, mode),
        SourceFormat=SimpleNamespace(NEWLINE_DELIMITED_JSON="NEWLINE_DELIMITED_JSON"),
        WriteDisposition=SimpleNamespace(WRITE_TRUNCATE="WRITE_TRUNCATE"),
        Table=lambda table_id, schema=None: SimpleNamespace(table_id=table_id, schema=schema),
    )


@pytest.fixture()
def fake_bq(monkeypatch) -> list[FakeBigQueryClient]:
    """google.cloud.bigquery replaced by a fake; returns the clients it created."""
    clients: list[FakeBigQueryClient] = []
    monkeypatch.setattr(export_bq, "_bigquery", lambda: fake_bigquery_module(clients))
    return clients


def test_bq_export_loads_every_table_with_write_truncate(after_runs, export_dir, monkeypatch, fake_bq, capsys):
    monkeypatch.setattr(config, "BQ_EXPORT", True)
    counts = export_bq.export_all(after_runs)

    (client,) = fake_bq
    assert client.project == "velox-demo"
    loaded = {load["table_id"]: load for load in client.loads}
    assert list(loaded) == [f"velox-demo.velox_p2p.{name}" for name in TABLES if counts[name]]
    for name in TABLES:
        load = loaded[f"velox-demo.velox_p2p.{name}"]
        assert load["data"] == (export_dir / f"{name}.ndjson").read_bytes()  # the file as written
        cfg = load["config"]
        assert cfg.source_format == "NEWLINE_DELIMITED_JSON" and cfg.write_disposition == "WRITE_TRUNCATE"
        assert cfg.schema == [(f["name"], f["type"], f["mode"]) for f in export_bq.table_schema(name)]
    assert all(job.waited for job in client.jobs)  # each load job is awaited (errors surface)
    assert client.deleted == [] and client.created == []
    assert "[export] bigquery: 12 tables loaded into velox-demo.velox_p2p (WRITE_TRUNCATE)" in capsys.readouterr().out


def test_empty_tables_are_recreated_empty_with_their_schema(session, export_dir, fake_bq):
    counts = export_bq.export_all(session, load=True)
    (client,) = fake_bq
    empty = [name for name in TABLES if counts[name] == 0]
    assert "gate_decision" in empty and "run" in empty
    assert client.deleted == [f"velox-demo.velox_p2p.{name}" for name in empty]
    assert [t.table_id for t in client.created] == client.deleted
    gate_table = next(t for t in client.created if t.table_id.endswith(".gate_decision"))
    assert ("touchless", "BOOLEAN", "NULLABLE") in gate_table.schema
    assert {load["table_id"] for load in client.loads} == {
        f"velox-demo.velox_p2p.{name}" for name in TABLES if counts[name]}


def test_injected_client_is_used(session, export_dir, fake_bq):
    injected = FakeBigQueryClient("elsewhere")
    export_bq.export_all(session, load=True, client=injected)
    assert fake_bq == [] and injected.loads  # no client was created


def test_bq_export_off_never_touches_bigquery(after_runs, export_dir, monkeypatch):
    def refuse():
        raise AssertionError("BigQuery must not be imported when BQ_EXPORT is off")

    monkeypatch.setattr(export_bq, "_bigquery", refuse)
    assert export_bq.export_all(after_runs)["gate_decision"] > 0
    assert export_bq.export_all(after_runs, load=False)["gate_decision"] > 0


def test_load_failure_is_one_readable_error_after_the_files_are_written(after_runs, export_dir, monkeypatch):
    client = FakeBigQueryClient("velox-demo", fail_on="velox-demo.velox_p2p.gate_decision")
    monkeypatch.setattr(export_bq, "_bigquery", lambda: fake_bigquery_module([]))
    with pytest.raises(ExportError, match=r"BigQuery load of velox-demo\.velox_p2p\.gate_decision failed: "
                                          r"RuntimeError: 400 Error while reading data"):
        export_bq.export_all(after_runs, load=True, client=client)
    assert all((export_dir / f"{name}.ndjson").exists() for name in TABLES)


def test_missing_library_is_reported(session, export_dir, monkeypatch):
    monkeypatch.setitem(sys.modules, "google.cloud.bigquery", None)  # an import of it now raises ImportError
    with pytest.raises(ExportError, match="pip install -r requirements-gcp.txt"):
        export_bq.export_all(session, load=True)
    assert (export_dir / "gate_decision.ndjson").exists()


def test_missing_project_is_reported(session, export_dir, monkeypatch, fake_bq):
    monkeypatch.setattr(config, "BQ_PROJECT", "")
    with pytest.raises(ExportError, match="BQ_PROJECT"):
        export_bq.export_all(session, load=True)
    assert fake_bq == []


# --------------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------------


def test_cli_writes_the_files(after_runs, export_dir, capsys):
    assert export_bq.main(["--no-bq"]) == 0
    assert (export_dir / "gate_decision.ndjson").exists()
    out = capsys.readouterr().out
    assert "BigQuery load skipped" not in out  # --no-bq was explicit


def test_cli_follows_bq_export_and_reports_a_skip(session, export_dir, capsys):
    assert export_bq.main([]) == 0
    assert "BigQuery load skipped (BQ_EXPORT is off; use --bq to load anyway)" in capsys.readouterr().out


def test_cli_bq_with_fake_module(session, export_dir, fake_bq):
    assert export_bq.main(["--bq"]) == 0
    assert len(fake_bq) == 1 and fake_bq[0].loads


def test_cli_failure_exits_1(session, export_dir, monkeypatch, capsys):
    monkeypatch.setattr(config, "BQ_PROJECT", "")
    assert export_bq.main(["--bq"]) == 1
    assert "[export] FAILED: BQ_PROJECT" in capsys.readouterr().out
