"""Write ground-truth extraction fixtures (tests/fixtures/<pdf stem>.json) from app/world.py.

These are what a perfect extractor would return for each sample PDF. They let `make test` and
EXTRACTOR=fixture run without the Gemini API. They are labelled FIXTURE_MODEL, never as Gemini output.

Run:  python -m tests.make_fixtures
"""
from __future__ import annotations

import json

from app import world
from app.config import FIXTURES_DIR
from app.extract import FIXTURE_MODEL, InvoiceExtraction

CONFIDENCE = 0.99


def _field(value):
    if value is None or value == [] or value == "":
        return {"value": None, "confidence": 0.0}
    return {"value": value, "confidence": CONFIDENCE}


def _notes(spec: world.DocumentSpec) -> str | None:
    notes = []
    if spec.watermark:
        notes.append(f"Marked as {spec.watermark}.")
    if "REMINDER" in spec.heading.upper():
        notes.append(f"Payment reminder for invoice {spec.invoice_number}.")
    if spec.contract_reference:
        notes.append(f"References contract {spec.contract_reference}.")
    if spec.referenced_invoice_number:
        notes.append(f"Credit note referencing invoice {spec.referenced_invoice_number}.")
    return " ".join(notes) or None


def ground_truth(spec: world.DocumentSpec) -> dict:
    party, entity = spec.party, spec.bill_to
    extraction = {
        "doc_type": _field(spec.doc_type),
        "supplier_name": _field(spec.printed_supplier_name),
        "supplier_vat_id": _field(party.vat_id),
        "supplier_iban": _field(party.bank),
        "supplier_country": _field(party.country),
        "bill_to_name": _field(entity.name),
        "bill_to_vat_id": _field(entity.vat_id if spec.print_bill_to_vat else None),
        "invoice_number": _field(spec.invoice_number),
        "invoice_date": _field(spec.invoice_date.isoformat()),
        "due_date": _field(spec.due_date.isoformat() if spec.due_date else None),
        "payment_terms_days": _field(spec.payment_terms_days),
        "currency": _field(spec.currency),
        "net_total": _field(spec.net_total),
        "tax_total": _field(spec.tax_total),
        "gross_total": _field(spec.gross_total),
        "po_numbers": _field(list(spec.po_numbers)),
        "referenced_invoice_number": _field(spec.referenced_invoice_number),
        "lines": _field([{"description": ln.description, "quantity": ln.quantity,
                          "unit_price": ln.unit_price, "amount": ln.amount} for ln in spec.lines]),
        "notes": _field(_notes(spec)),
    }
    InvoiceExtraction.model_validate(extraction)  # schema check
    return {
        "file_name": spec.filename,
        "file_sha256": None,
        "model": FIXTURE_MODEL,
        "created_on": "2026-10-01T00:00:00",
        "latency_ms": None,
        "usage": None,
        "extraction": extraction,
    }


def main() -> None:
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    for spec in world.DOCUMENTS:
        path = FIXTURES_DIR / spec.filename.replace(".pdf", ".json")
        path.write_text(json.dumps(ground_truth(spec), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"[fixtures] wrote {path.name}")


if __name__ == "__main__":
    main()
