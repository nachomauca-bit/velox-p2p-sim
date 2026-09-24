"""The 12 sample PDFs: generated, byte-identical across runs, and carrying the traits of brief 5.6."""
from __future__ import annotations

import hashlib
from datetime import date
from pathlib import Path

import pytest
from pypdf import PdfReader

from app import invoices_gen, world
from app.invoices_gen import check_glyphs, fmt_amount, fmt_bank_account, fmt_date, fmt_quantity, fmt_vat


@pytest.fixture(scope="module")
def pdf_dir(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("invoices")
    invoices_gen.generate_all(out)
    return out


@pytest.fixture(scope="module")
def texts(pdf_dir) -> dict[int, str]:
    """Extracted text of every document, whitespace collapsed, keyed by document number."""
    result = {}
    for spec in world.DOCUMENTS:
        reader = PdfReader(pdf_dir / spec.filename)
        raw = " ".join(page.extract_text() or "" for page in reader.pages)
        result[spec.no] = " ".join(raw.split())
    return result


def _sha256_by_name(folder: Path) -> dict[str, str]:
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(folder.glob("*.pdf"))}


def test_twelve_one_page_pdfs(pdf_dir):
    assert len(world.DOCUMENTS) == 12
    for spec in world.DOCUMENTS:
        path = pdf_dir / spec.filename
        assert path.read_bytes().startswith(b"%PDF")
        assert len(PdfReader(path).pages) == 1


def test_output_is_deterministic(tmp_path):
    paths = invoices_gen.generate_all(tmp_path / "a")
    invoices_gen.generate_all(tmp_path / "b")
    assert [p.name for p in paths] == [d.filename for d in world.DOCUMENTS]
    first, second = _sha256_by_name(tmp_path / "a"), _sha256_by_name(tmp_path / "b")
    assert len(first) == 12
    assert first == second


@pytest.mark.parametrize("spec", world.DOCUMENTS, ids=lambda s: s.filename)
def test_key_fields_printed(texts, spec):
    text = texts[spec.no]
    assert spec.invoice_number in text
    assert spec.printed_supplier_name in text
    assert spec.bill_to.name in text
    assert fmt_amount(spec.gross_total, spec.party.country) in text
    assert spec.currency in text
    for po in spec.po_numbers:
        assert f"Your PO {po}" in text


def test_reminder_copy_prints_short_name_only(texts):
    text = texts[2]
    assert "COPY" in text and "REMINDER" in text
    assert "NORDWIND LOGISTICS" in text
    assert "Nordwind Logistics GmbH" not in text


def test_credit_note(texts):
    text = texts[4]
    assert "CREDIT NOTE" in text
    assert "Relates to invoice INV-2026-0457" in text
    assert "Bright Agency SARL" not in text
    assert "Total credit -1.800,00 EUR" in text
    assert "Amount will be credited to your account" in text
    assert "Please transfer" not in text


def test_wrong_entity_invoice(texts):
    assert "Velox Retail SAS" in texts[8]
    assert "4500126" in texts[8]


def test_price_mismatch_invoice_shows_unit_price_44(texts):
    assert f"150 {fmt_amount(44.0, 'ES')} {fmt_amount(6600.0, 'ES')}" in texts[6]  # "150 44,00 6.600,00"


def test_store_invoice_has_attention_line_and_no_bill_to_vat(texts):
    text, entity = texts[10], world.LEGAL_ENTITY_BY_CODE["VDE"]
    assert "Store Berlin 01" in text
    assert "Rosenthaler Strasse 40" in text
    assert fmt_vat(entity.vat_id, entity.country) not in text
    assert entity.vat_id not in text


def test_contract_references(texts):
    assert "Contract ref. CT-2025-001" in texts[1]
    assert "Contract ref. CT-2025-003" in texts[11]


def test_supplier_identity_blocks(texts):
    for spec in world.DOCUMENTS:
        party = spec.party
        assert fmt_vat(party.vat_id, party.country) in texts[spec.no]
        assert fmt_bank_account(party.bank) in texts[spec.no]
        assert party.bank_name in texts[spec.no]


def test_special_glyphs_survive(texts):
    # em dash, en dash and middle dot are WinAnsi glyphs: they must come back as text, not boxes
    assert "(Oct 2026 – Sep 2027)" in texts[7]
    assert "Autumn campaign 2026 — creative concept" in texts[3]
    assert "Tel. " in texts[3] and " · billing@bright-agency.fr" in texts[3]
    with pytest.raises(ValueError):
        check_glyphs("−1,500.00")  # U+2212 minus sign is not in WinAnsi


def test_locale_formats():
    assert fmt_amount(23400, "DE") == "23.400,00"
    assert fmt_amount(23400, "GB") == "23,400.00"
    assert fmt_amount(9600, "US") == "9,600.00"
    assert fmt_amount(-1800, "FR") == "-1.800,00"
    assert fmt_amount(0.10, "FR") == "0,10"
    assert fmt_quantity(10000, "FR") == "10.000"
    assert fmt_quantity(10000, "GB") == "10,000"
    assert fmt_quantity(150, "ES") == "150"
    d = date(2026, 9, 30)
    assert fmt_date(d, "DE") == "30.09.2026"
    assert fmt_date(d, "NL") == "30.09.2026"
    assert fmt_date(d, "FR") == "30/09/2026"
    assert fmt_date(d, "ES") == "30/09/2026"
    assert fmt_date(d, "GB") == "30 Sep 2026"
    assert fmt_date(date(2026, 10, 1), "US") == "Oct 1, 2026"


def test_vat_and_bank_formats():
    assert fmt_vat("DE281947305", "DE") == "DE 281 947 305"
    assert fmt_vat("GB293847561", "GB") == "GB 293 8475 61"
    assert fmt_vat("NL859374612B01", "NL") == "NL 8593 7461 2B01"
    assert fmt_vat("ESB86419273", "ES") == "ES B86419273"
    assert fmt_vat("CHE-419.287.563", "CH") == "CHE-419.287.563 MWST"
    assert fmt_vat("47-3829105", "US") == "EIN 47-3829105"
    assert fmt_bank_account("ABA 121000248 ACCT 4839201756") == "ABA routing 121000248 · Account 4839201756"
    assert fmt_bank_account("DE44500105175407324931") == "IBAN DE44 5001 0517 5407 3249 31"
