"""Write ground-truth extraction fixtures from the world: tests/fixtures/ (v1) and tests/fixtures_v2/ (test set v2).

These are what a perfect extractor would return for each sample PDF. They let `make test` and
EXTRACTOR=fixture run without the Gemini API. They are labelled FIXTURE_MODEL, never as Gemini output.
Test set v2: no fixture for the UBL e-invoice (document 11, parsed by app.ubl) nor for the email body
(document 14, nothing to extract). The two scans carry SIMULATED confidences (document 12: 0.93; document 13:
0.62 on the gross total, 0.71 on the invoice number, 0.88 elsewhere) to exercise the human-review path, and
say so in their model label; real values come from Gemini once a key is set.

Run:  python -m tests.make_fixtures [--v2 | --all]   (default: v1 only)
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Optional

from app import config, world
from app.extract import FIXTURE_MODEL, InvoiceExtraction

CONFIDENCE = 0.99
SCAN_FIXTURE_MODEL = "fixture (ground truth, simulated scan confidences, no API call)"
# Simulated per-field confidences of the scans ("*": every other field that has a value).
SCAN_CONFIDENCES: dict[str, dict[str, float]] = {
    "clean": {"*": 0.93},
    "low": {"*": 0.88, "gross_total": 0.62, "invoice_number": 0.71},
}
CREATED_ON = {"v1": "2026-10-01T00:00:00", "v2": "2026-11-02T00:00:00"}
_WATERMARK_MEANING = {"KOPIE": "copy"}


def _field(value, confidence: float = CONFIDENCE):
    if value is None or value == [] or value == "":
        return {"value": None, "confidence": 0.0}
    return {"value": value, "confidence": confidence}


def _notes(spec: world.DocumentSpec) -> str | None:
    notes = []
    if spec.doc_type == "other":
        items = "; ".join(ln.description for ln in spec.lines)
        notes.append(f"Statement of account, not an invoice. Open items: {items}. "
                     f"Balance {spec.gross_total:,.2f} {spec.currency}.")
    if spec.watermark:
        meaning = _WATERMARK_MEANING.get(spec.watermark)
        notes.append(f"Marked as {spec.watermark} ({meaning})." if meaning else f"Marked as {spec.watermark}.")
    if "REMINDER" in spec.heading.upper():
        notes.append(f"Payment reminder for invoice {spec.invoice_number}.")
    if spec.contract_reference:
        notes.append(f"References contract {spec.contract_reference}.")
    if spec.referenced_invoice_number:
        notes.append(f"Credit note referencing invoice {spec.referenced_invoice_number}.")
    elif spec.doc_type == "credit_note":
        notes.append("Credit note without a reference to an invoice.")
    if spec.scan == "low":
        notes.append("Poor-quality scan: the invoice number and the total are smudged and only partly legible.")
    return " ".join(notes) or None


def _supplier_vat_as_read(spec: world.DocumentSpec) -> str:
    """The supplier VAT ID as printed, without the label and without spaces (hyphens and dots kept)."""
    return re.sub(r"\s+", "", spec.vat_display) if spec.vat_display else spec.party.vat_id


def _line(spec: world.DocumentSpec, line: world.InvoiceLineSpec) -> dict:
    if spec.doc_type == "other":  # a statement prints each open item's amount only
        return {"description": line.description, "quantity": None, "unit_price": None, "amount": line.amount}
    return {"description": line.description, "quantity": line.quantity, "unit_price": line.unit_price,
            "amount": line.amount}


def _confidence(spec: world.DocumentSpec, name: str) -> float:
    if spec.scan is None:
        return CONFIDENCE
    table = SCAN_CONFIDENCES[spec.scan]
    return table.get(name, table["*"])


def ground_truth(spec: world.DocumentSpec) -> dict:
    party, entity = spec.party, spec.bill_to
    statement = spec.doc_type == "other"  # no VAT block
    values = {
        "doc_type": spec.doc_type,
        "supplier_name": spec.printed_supplier_name,
        "supplier_vat_id": _supplier_vat_as_read(spec),
        "supplier_iban": party.bank,
        "supplier_country": party.country,
        "bill_to_name": entity.name,
        "bill_to_vat_id": entity.vat_id if spec.print_bill_to_vat else None,
        "invoice_number": spec.invoice_number,
        "invoice_date": spec.invoice_date.isoformat(),
        "due_date": spec.due_date.isoformat() if spec.due_date else None,
        "payment_terms_days": spec.payment_terms_days,
        "currency": spec.currency,
        "net_total": None if statement else spec.net_total,  # a statement prints a balance only (no net, no VAT)
        "tax_total": None if statement else (spec.tax_total or 0.0),  # never -0.0
        "gross_total": spec.gross_total,
        "po_numbers": list(spec.po_numbers),
        "referenced_invoice_number": spec.referenced_invoice_number,
        "lines": [_line(spec, ln) for ln in spec.lines],
        "notes": _notes(spec),
    }
    extraction = {name: _field(value, _confidence(spec, name)) for name, value in values.items()}
    InvoiceExtraction.model_validate(extraction)  # schema check
    return {
        "file_name": spec.filename,
        "file_sha256": None,
        "model": SCAN_FIXTURE_MODEL if spec.scan else FIXTURE_MODEL,
        "created_on": CREATED_ON[spec.dataset],
        "latency_ms": None,
        "usage": None,
        "extraction": extraction,
    }


def has_fixture(spec: world.DocumentSpec) -> bool:
    """PDFs only: the UBL e-invoice is parsed without a model, an email body has nothing to extract."""
    return spec.content == "pdf"


def write_fixtures(dataset: str, out_dir: Optional[Path] = None) -> list[Path]:
    """Write <file stem>.json for every document of a dataset that has a fixture; returns the paths."""
    out_dir = Path(out_dir or (config.FIXTURES_V2_DIR if dataset == "v2" else config.FIXTURES_DIR))
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for spec in world.documents_for(dataset):
        if not has_fixture(spec):
            continue
        path = out_dir / f"{Path(spec.filename).stem}.json"
        path.write_text(json.dumps(ground_truth(spec), indent=2, ensure_ascii=False) + "\n", encoding="utf-8",
                        newline="\n")  # LF on every OS, as in git
        paths.append(path)
    return paths


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="Write the ground-truth extraction fixtures.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--v2", action="store_true", help="test set v2 only (tests/fixtures_v2/)")
    group.add_argument("--all", action="store_true", help="v1 and test set v2")
    args = parser.parse_args(argv)
    datasets = ["v2"] if args.v2 else ["v1", "v2"] if args.all else ["v1"]
    for dataset in datasets:
        for path in write_fixtures(dataset):
            print(f"[fixtures] wrote {path.parent.name}/{path.name}")


if __name__ == "__main__":
    main()
