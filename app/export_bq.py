"""KPI export (brief section 17, optional): the gate decisions and the mock tables, one BigQuery table each, so
the KPI page can also be built in Looker Studio (Data Studio) on top of them.

- Always writes <EXPORT_DIR>/<table>.ndjson (newline-delimited JSON, one row per line) and <table>.schema.json
  (the BigQuery schema, also usable with `bq load`). Files are replaced atomically on every export.
- Values: DATE as YYYY-MM-DD, DATETIME as ISO 8601 (no time zone: the app's clock), JSON columns (steps,
  details, flags, ...) as JSON text in a STRING column (BigQuery's JSON_VALUE works on it). gate_decision also
  carries a few flattened fields of `details` (touchless, supplier, amount, ...) and the document's dataset,
  so a Looker Studio report needs no JSON parsing.
- When BQ_EXPORT is on, each file is loaded into BQ_PROJECT.BQ_DATASET.<table> with WRITE_TRUNCATE: every export
  replaces the tables. The export runs after a scenario run, a webhook document, a re-run and a reset (not after
  loading a set alone), so BigQuery reflects the database as of the last of those. google-cloud-bigquery
  (requirements-gcp.txt) is imported only then. The dataset must exist (deploy/04_bigquery.sh).

CLI:  python -m app.export_bq [--bq | --no-bq]      (default: load into BigQuery only if BQ_EXPORT is on)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import JSON, Boolean, Column, Date, DateTime, Float, Integer, Numeric, Table, select
from sqlalchemy.orm import Session

from app import config
from app.models import Base

# The exported tables, in load order (decisions first: they are what the KPI report reads).
TABLES = (
    "gate_decision", "pending_vendor_invoice", "credit_note_application", "inbound_document", "vendor_account",
    "purchase_order", "purchase_order_line", "product_receipt", "contract", "party", "legal_entity", "run",
)

# Flattened GateDecision.details fields added to gate_decision rows: (key, BigQuery type).
GATE_DETAIL_FIELDS = (
    ("sample_no", "INTEGER"), ("supplier_name", "STRING"), ("invoice_number", "STRING"), ("doc_type", "STRING"),
    ("gross_total", "FLOAT"), ("currency", "STRING"), ("account_id", "STRING"), ("bill_to_entity", "STRING"),
    ("posted_entity", "STRING"), ("po_number", "STRING"), ("contract_id", "STRING"), ("touchless", "BOOLEAN"),
    ("wrong_entity_posting", "BOOLEAN"), ("duplicate_posting", "BOOLEAN"), ("credit_status", "STRING"),
    ("terms_variance_paid", "BOOLEAN"), ("doa_auto_approved", "BOOLEAN"),
)


class ExportError(RuntimeError):
    """The export could not be completed (missing library or setting, or a failed BigQuery load)."""


# --------------------------------------------------------------------------------------------
# Schema and values
# --------------------------------------------------------------------------------------------


def bq_type(column: Column) -> str:
    """BigQuery type of a SQLAlchemy column (JSON columns are exported as JSON text)."""
    kind = column.type
    if isinstance(kind, JSON):
        return "STRING"
    if isinstance(kind, Boolean):
        return "BOOLEAN"
    if isinstance(kind, DateTime):
        return "DATETIME"
    if isinstance(kind, Date):
        return "DATE"
    if isinstance(kind, Integer):
        return "INTEGER"
    if isinstance(kind, (Float, Numeric)):
        return "FLOAT"
    return "STRING"


def _field(name: str, kind: str) -> dict[str, str]:
    return {"name": name, "type": kind, "mode": "NULLABLE"}


def _table(name: str) -> Table:
    return Base.metadata.tables[name]


def _has_dataset_column() -> bool:
    return "dataset" in _table("inbound_document").columns


def table_schema(name: str) -> list[dict[str, str]]:
    """The BigQuery schema of one exported table: its columns, plus the derived fields of gate_decision."""
    table = _table(name)
    fields = [_field(column.name, bq_type(column)) for column in table.columns]
    if name == "gate_decision":
        taken = set(table.columns.keys())
        if _has_dataset_column() and "dataset" not in taken:
            fields.append(_field("dataset", "STRING"))
        fields += [_field(key, kind) for key, kind in GATE_DETAIL_FIELDS if key not in taken]
    return fields


def to_json_value(value: Any, kind: str) -> Any:
    """A column value as BigQuery reads it from NDJSON; a value that does not fit the type becomes null."""
    if value is None:
        return None
    try:
        if kind == "DATETIME":
            return value.isoformat() if isinstance(value, datetime) else str(value)
        if kind == "DATE":
            return value.isoformat() if isinstance(value, date) else str(value)
        if kind == "BOOLEAN":
            return bool(value)
        if kind == "INTEGER":
            return int(value)
        if kind == "FLOAT":
            return float(value)
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        return str(value)
    except (TypeError, ValueError):
        return None


def table_rows(session: Session, name: str) -> list[dict[str, Any]]:
    """Every row of one table, ordered by primary key, as JSON-ready dicts matching table_schema(name)."""
    table = _table(name)
    kinds = {column.name: bq_type(column) for column in table.columns}
    result = session.execute(select(table).order_by(*table.primary_key.columns))
    rows = [{key: to_json_value(value, kinds[key]) for key, value in row._mapping.items()} for row in result]
    if name == "gate_decision":
        _add_decision_fields(session, rows, set(kinds))
    return rows


def _add_decision_fields(session: Session, rows: list[dict[str, Any]], taken: set[str]) -> None:
    """The dataset of each decision's document and the flattened details fields."""
    datasets: dict[str, Any] = {}
    add_dataset = _has_dataset_column() and "dataset" not in taken
    if add_dataset:
        docs = _table("inbound_document")
        datasets = dict(session.execute(select(docs.c.doc_id, docs.c.dataset)).tuples().all())
    for row in rows:
        details = json.loads(row["details"]) if row.get("details") else {}
        if add_dataset:
            row["dataset"] = datasets.get(row["doc_id"])
        for key, kind in GATE_DETAIL_FIELDS:
            if key not in taken:
                row[key] = to_json_value(details.get(key) if isinstance(details, dict) else None, kind)


# --------------------------------------------------------------------------------------------
# Files
# --------------------------------------------------------------------------------------------


def _write_atomic(path: Path, text: str) -> None:
    """Temp file + os.replace: a reader (or a BigQuery load) never sees a half-written file."""
    tmp = path.with_name(f"{path.name}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8", newline="\n")
        os.replace(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


def ndjson_path(name: str) -> Path:
    return config.EXPORT_DIR / f"{name}.ndjson"


def schema_path(name: str) -> Path:
    return config.EXPORT_DIR / f"{name}.schema.json"


def write_table(name: str, rows: list[dict[str, Any]]) -> Path:
    """Write <table>.ndjson and <table>.schema.json into EXPORT_DIR; returns the NDJSON path."""
    config.EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    _write_atomic(schema_path(name), json.dumps(table_schema(name), indent=2) + "\n")
    path = ndjson_path(name)
    _write_atomic(path, "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows))
    return path


# --------------------------------------------------------------------------------------------
# BigQuery (optional)
# --------------------------------------------------------------------------------------------


def _bigquery() -> Any:
    """google.cloud.bigquery, imported only when loading (requirements-gcp.txt). A seam for tests."""
    try:
        from google.cloud import bigquery
    except ImportError as exc:
        raise ExportError("google-cloud-bigquery is not installed: pip install -r requirements-gcp.txt") from exc
    return bigquery


def load_bigquery(counts: dict[str, int], *, client: Any = None) -> list[str]:
    """Load every exported NDJSON file into BQ_PROJECT.BQ_DATASET (WRITE_TRUNCATE); returns the table ids.

    An empty table is recreated empty with its schema (a load job needs at least one row).
    """
    if not config.BQ_PROJECT:
        raise ExportError("BQ_PROJECT (or GOOGLE_CLOUD_PROJECT) is not set: cannot load into BigQuery")
    bigquery = _bigquery()
    client = client or bigquery.Client(project=config.BQ_PROJECT)
    loaded: list[str] = []
    for name, count in counts.items():
        table_id = f"{config.BQ_PROJECT}.{config.BQ_DATASET}.{name}"
        schema = [bigquery.SchemaField(f["name"], f["type"], mode=f["mode"]) for f in table_schema(name)]
        try:
            if count:
                job_config = bigquery.LoadJobConfig(
                    source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
                    write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
                    schema=schema,
                )
                with ndjson_path(name).open("rb") as source:
                    client.load_table_from_file(source, table_id, job_config=job_config).result()
            else:
                client.delete_table(table_id, not_found_ok=True)
                client.create_table(bigquery.Table(table_id, schema=schema))
        except Exception as exc:  # google.api_core errors, network, auth: one readable line
            raise ExportError(f"BigQuery load of {table_id} failed: {type(exc).__name__}: {str(exc)[:300]}") from exc
        loaded.append(table_id)
    print(f"[export] bigquery: {len(loaded)} tables loaded into {config.BQ_PROJECT}.{config.BQ_DATASET} "
          f"(WRITE_TRUNCATE)")
    return loaded


# --------------------------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------------------------


def export_all(session: Session, *, load: Optional[bool] = None, client: Any = None) -> dict[str, int]:
    """Export every table in TABLES; returns {table: number of rows}.

    Always writes the NDJSON files to EXPORT_DIR. Loads them into BigQuery when `load` is True, or when it is
    None and config.BQ_EXPORT is on (`client`: an injected BigQuery client, for tests). Raises ExportError
    when the BigQuery part fails (the files are written by then) and OSError when a file cannot be written.
    """
    counts: dict[str, int] = {}
    for name in TABLES:
        rows = table_rows(session, name)
        write_table(name, rows)
        counts[name] = len(rows)
    summary = ", ".join(f"{name}={count}" for name, count in counts.items())
    print(f"[export] {len(counts)} tables written to {config.EXPORT_DIR} ({summary})")
    if config.BQ_EXPORT if load is None else load:
        load_bigquery(counts, client=client)
    return counts


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Export the gate decisions and the mock tables as NDJSON "
                                                 "(EXPORT_DIR) and optionally load them into BigQuery.")
    choice = parser.add_mutually_exclusive_group()
    choice.add_argument("--bq", dest="load", action="store_true", help="load into BigQuery even if BQ_EXPORT is off")
    choice.add_argument("--no-bq", dest="load", action="store_false", help="write the NDJSON files only")
    parser.set_defaults(load=None)
    args = parser.parse_args(argv)

    from app.db import SessionLocal, init_db

    init_db()
    with SessionLocal() as session:
        try:
            export_all(session, load=args.load)
        except (ExportError, OSError) as exc:
            print(f"[export] FAILED: {exc}")
            return 1
    if args.load is None and not config.BQ_EXPORT:
        print("[export] BigQuery load skipped (BQ_EXPORT is off; use --bq to load anyway)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
