"""Test set v2 (docs/TEST_SET_V2.md): the 26 specs, the generated files (native PDFs in four languages, statement,
KOPIE copy, two scans, UBL XML, email body), determinism, v1 unchanged, and the ground-truth fixtures."""
from __future__ import annotations

import hashlib
import io
import json
import math
import xml.etree.ElementTree as ET
from datetime import date, datetime
from pathlib import Path

import pytest
from pypdf import PdfReader

from app import config, invoices_gen, world, world_v2
from app.extract import FIXTURE_MODEL, InvoiceExtraction
from app.invoices_gen import LABELS, check_glyphs, fmt_amount
from tests import make_fixtures

SPECS = world_v2.DOCUMENTS_V2
NATIVE = [s for s in SPECS if s.content == "pdf" and not s.scan]
SCANS = [s for s in SPECS if s.scan]
PDFS = [s for s in SPECS if s.content == "pdf"]

# docs/TEST_SET_V2.md, one row per document:
# supplier, language, format, scan, channel, sender, received (Nov day, h, min), number, invoice date (month, day),
# terms, bill-to, net, VAT, gross, currency, POs, contract, referenced invoice, VAT as printed
EXPECTED = {
    1: ("Nordwind Logistics GmbH", "de", "pdf", None, "ap_mailbox", "billing@nordwind-logistics.de", (2, 8, 15),
        "NWL-2026-01027", (10, 31), 30, "VDE", 24100.00, 4579.00, 28679.00, "EUR", (), "CT-2025-001", None,
        "DE281947305"),
    2: ("Nordwind Logistics GmbH", "de", "pdf", None, "store_mailbox", "ar@nordwind-logistics.de", (10, 15, 20),
        "NWL-2026-01027", (10, 31), 30, "VDE", 24100.00, 4579.00, 28679.00, "EUR", (), "CT-2025-001", None,
        "DE281947305"),
    3: ("Nordwind Logistics GmbH", "de", "pdf", None, "ap_mailbox", "ar@nordwind-logistics.de", (5, 10, 0),
        "KA-2026-11", (11, 5), None, "VDE", 56525.00, 0.0, 56525.00, "EUR", (), None, None, None),
    4: ("Cleanspace Facilities BV", "en", "pdf", None, "ap_mailbox", "invoicing@cleanspace.nl", (2, 9, 40),
        "CSF-26-11412", (10, 31), 30, "VDE", 3150.00, 0.0, 3150.00, "EUR", (), "CT-2025-002", None, None),
    5: ("Cleanspace Facilities BV", "fr", "pdf", None, "ap_mailbox", "invoicing@cleanspace.nl", (2, 9, 41),
        "CSF-26-11413", (10, 31), 30, "VFR", 3300.00, 0.0, 3300.00, "EUR", (), "CT-2025-003", None, None),
    6: ("Bright Agency SARL", "fr", "pdf", None, "ap_mailbox", "billing@bright-agency.fr", (3, 11, 5),
        "INV-2026-0512", (10, 30), 45, "VFR", 6000.00, 1200.00, 7200.00, "EUR", ("4500121",), None, None, None),
    7: ("QuickPrint SAS", "fr", "pdf", None, "ap_mailbox", "accounts@quickprint.fr", (4, 14, 30),
        "AV-26-0088", (11, 4), None, "VFR", -200.00, -40.00, -240.00, "EUR", (), None, None, None),
    8: ("QuickPrint SAS", "fr", "pdf", None, "ap_mailbox", "luc.bernard@velox.com", (6, 16, 45),
        "QP-26-1107", (10, 28), 30, "VFR", 2400.00, 480.00, 2880.00, "EUR", ("4500114",), None, None, None),
    9: ("Atlas Displays SL", "es", "pdf", None, "ap_mailbox", "invoices@atlasdisplays.es", (3, 9, 12),
        "AD-2026/0861", (10, 27), 60, "VFR", 3360.00, 0.0, 3360.00, "EUR", ("4500105",), None, None,
        "ES-B86419273"),
    10: ("Atlas Displays SL", "es", "pdf", None, "ap_mailbox", "invoices@atlasdisplays.es", (3, 9, 13),
         "AD-2026/0862", (10, 27), 60, "VDE", 6300.00, 0.0, 6300.00, "EUR", ("4500109",), None, None, None),
    11: ("Metro Media GmbH", "de", "ubl_xml", None, "ap_mailbox", "einvoice@metromedia.de", (2, 7, 30),
         "MM-2026-248", (10, 30), 30, "VDE", 7500.00, 1425.00, 8925.00, "EUR", ("4500126",), None, None, None),
    12: ("FitOut Partners Ltd", "en", "pdf", "clean", "ap_mailbox", "accounts@fitoutpartners.co.uk", (4, 10, 20),
         "2026-104", (10, 30), 30, "VDE", 40000.00, 0.0, 40000.00, "EUR", ("4500101",), None, None,
         "GB293 8475 61"),
    13: ("Kaffee & Co OHG", "de", "pdf", "low", "store_mailbox", "info@kaffee-und-co.de", (2, 12, 0),
         "2026/139", (10, 30), 14, "VDE", 80.78, 5.65, 86.43, "EUR", (), None, None, None),
    14: ("Kaffee & Co OHG", "de", "email_body", None, "store_mailbox", "info@kaffee-und-co.de", (6, 9, 30),
         "2026/140", (11, 6), 14, "VDE", 49.00, 3.43, 52.43, "EUR", (), None, None, None),
    15: ("Lumen Store Lighting Ltd", "en", "pdf", None, "ap_mailbox", "accounts@lumenlighting.co.uk", (4, 8, 5),
         "LSL-INV-5602", (10, 29), 30, "VDE", 4200.00, 0.0, 4200.00, "EUR", ("4500112",), None, None, None),
    16: ("SecureNet AG", "en", "pdf", None, "ap_mailbox", "billing@securenet.ch", (5, 13, 10),
         "SN-2026-3391", (10, 31), 30, "VDE", 8300.00, 0.0, 8300.00, "EUR", ("4500128", "4500130"), None, None,
         None),
    17: ("Shopsys Software Inc.", "en", "pdf", None, "ap_mailbox", "billing@shopsys.io", (2, 7, 50),
         "SS-100517", (11, 1), 30, "VUS", 9600.00, 0.0, 9600.00, "EUR", ("4500131",), None, None, None),
    18: ("Harbor Freight Forwarders Inc.", "en", "pdf", None, "ap_mailbox", "billing@harborff.com", (2, 16, 30),
         "HFF-2026-1031", (10, 31), 30, "VUS", 16800.00, 0.0, 16800.00, "USD", (), "CT-2025-004", None, None),
    19: ("Harbor Freight Forwarders Inc.", "en", "pdf", None, "ap_mailbox", "billing@harborff.com", (6, 17, 0),
         "HFF-CN-2026-017", (11, 6), None, "VUS", -450.00, 0.0, -450.00, "USD", (), None, "HFF-2026-1031", None),
    20: ("Shopsys Software Inc.", "en", "pdf", None, "ap_mailbox", "billing@shopsys.io", (3, 8, 40),
         "SS-100522", (11, 2), 30, "VDE", 2400.00, 0.0, 2400.00, "EUR", ("4500107",), None, None, None),
    21: ("Bright Agency SARL", "fr", "pdf", None, "ap_mailbox", "billing@bright-agency.fr", (3, 11, 0),
         "INV-2026-0530", (11, 3), 45, "VFR", 12000.00, 2400.00, 14400.00, "EUR", ("4500117",), None, None,
         "FR 62 512 345 678"),
    22: ("Kaffee & Co OHG", "de", "pdf", None, "store_mailbox", "info@kaffee-und-co.de", (2, 12, 5),
         "2026/136", (10, 30), 14, "VDE", 188.26, 13.18, 201.44, "EUR", (), None, None, None),
    23: ("Nordwind Logistics GmbH", "de", "pdf", None, "ap_mailbox", "billing@nordwind-logistics.de", (9, 8, 30),
         "NWL-2026-01064", (11, 6), 30, "VDE", 7400.00, 1406.00, 8806.00, "EUR", (), "CT-2025-001", None, None),
    24: ("Lumen Store Lighting Ltd", "en", "pdf", None, "ap_mailbox", "accounts@lumenlighting.co.uk", (5, 9, 0),
         "LSL-INV-5611", (11, 2), 30, "VDE", 660.00, 0.0, 660.00, "EUR", ("4500999",), None, None, None),
    25: ("QuickPrint SAS", "en", "pdf", None, "ap_mailbox", "accounts@quickprint.fr", (5, 11, 15),
         "QP-26-1119", (11, 4), 30, "VDE", 1100.00, 0.0, 1100.00, "EUR", ("4500119",), None, None, None),
    26: ("Berliner Blumen GmbH", "de", "pdf", None, "store_mailbox", "rechnung@berliner-blumen.de", (5, 15, 40),
         "BB-26-318", (11, 5), 14, "VDE", 268.91, 18.82, 287.73, "EUR", (), None, None, "DE 305 118 442"),
}

# Words every native document of a language prints (headings, meta and totals labels).
LANGUAGE_WORDS = {
    ("de", "invoice"): ["RECHNUNG", "Rechnungsnr.", "Rechnungsdatum", "Zahlungsbedingungen", "Tage netto",
                        "Nettobetrag", "Rechnungsbetrag", "Bitte überweisen Sie", "Seite 1 von 1"],
    ("de", "other"): ["KONTOAUSZUG", "Auszug Nr.", "Offener Saldo", "Bankverbindung", "Seite 1 von 1"],
    ("fr", "invoice"): ["FACTURE", "Facture n°", "Échéance", "jours net", "Total HT", "Total TTC", "Page 1 sur 1"],
    ("fr", "credit_note"): ["AVOIR", "Avoir n°", "Total HT", "Total avoir TTC", "Page 1 sur 1"],
    ("es", "invoice"): ["FACTURA", "Factura n.º", "Vencimiento", "días netos", "Base imponible", "Total factura",
                        "Página 1 de 1"],
    ("en", "invoice"): ["INVOICE", "Invoice no.", "Due date", "days net", "Net amount", "Total due", "Page 1 of 1"],
    ("en", "credit_note"): ["CREDIT NOTE", "Credit note no.", "Net amount", "Total credit", "Page 1 of 1"],
}


def _text(path: Path) -> str:
    reader = PdfReader(path)
    return " ".join(" ".join(page.extract_text() or "" for page in reader.pages).split())


def _sha256_by_name(folder: Path) -> dict[str, str]:
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(folder.iterdir())}


@pytest.fixture(scope="module")
def v2_dir(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("invoices_v2")
    invoices_gen.generate_all_v2(out)
    return out


@pytest.fixture(scope="module")
def texts(v2_dir) -> dict[int, str]:
    """Text layer of every PDF, whitespace collapsed, keyed by document number."""
    return {spec.no: _text(v2_dir / spec.filename) for spec in PDFS}


# --------------------------------------------------------------------------------------------
# The specs
# --------------------------------------------------------------------------------------------


def test_26_documents_numbered_1_to_26():
    assert len(SPECS) == 26
    assert [s.no for s in SPECS] == list(range(1, 27))
    assert world_v2.DOCUMENT_V2_BY_NO == {s.no: s for s in SPECS}
    assert all(s.dataset == "v2" for s in SPECS)
    assert len({s.filename for s in SPECS}) == 26
    for spec in SPECS:
        assert spec.filename.startswith(f"{spec.no:02d}_")


def test_documents_for_dataset():
    assert world.documents_for("v1") is world.DOCUMENTS
    assert world.documents_for() is world.DOCUMENTS
    assert world.documents_for("v2") is world_v2.DOCUMENTS_V2
    assert world.document_for("v2", 26).supplier is world_v2.BERLINER_BLUMEN
    assert world.document_for("v1", 3).invoice_number == "INV-2026-0457"
    assert world.document_for("v2", 27) is None
    with pytest.raises(ValueError):
        world.documents_for("v3")


def test_v1_specs_keep_the_defaults():
    for spec in world.DOCUMENTS:
        assert (spec.language, spec.content, spec.scan, spec.email_body, spec.vat_display, spec.supplier,
                spec.dataset) == ("en", "pdf", None, None, None, None, "v1")
        assert spec.party is world.PARTY_BY_ID[spec.party_id]


@pytest.mark.parametrize("spec", SPECS, ids=lambda s: f"{s.no:02d}")
def test_spec_matches_the_test_set_table(spec):
    (supplier, language, content, scan, channel, sender, (day, hour, minute), number, (month, inv_day), terms,
     bill_to, net, tax, gross, currency, pos, contract, ref, vat_display) = EXPECTED[spec.no]
    assert spec.party.canonical_name == supplier == spec.printed_supplier_name
    assert (spec.language, spec.content, spec.scan, spec.channel, spec.sender_email) == (
        language, content, scan, channel, sender)
    assert spec.received_on == datetime(2026, 11, day, hour, minute)
    assert (spec.invoice_number, spec.invoice_date, spec.payment_terms_days, spec.bill_to_entity) == (
        number, date(2026, month, inv_day), terms, bill_to)
    assert (spec.net_total, spec.tax_total, spec.gross_total, spec.currency) == (net, tax, gross, currency)
    assert (spec.po_numbers, spec.contract_reference, spec.referenced_invoice_number, spec.vat_display) == (
        pos, contract, ref, vat_display)


@pytest.mark.parametrize("spec", SPECS, ids=lambda s: f"{s.no:02d}")
def test_totals_arithmetic(spec):
    assert spec.net_total == round(sum(line.quantity * line.unit_price for line in spec.lines), 2)
    assert spec.tax_total == round(spec.net_total * spec.tax_rate, 2)
    assert spec.gross_total == round(spec.net_total + spec.tax_total, 2)
    if spec.doc_type == "credit_note":
        assert spec.gross_total < 0 and spec.payment_terms_days is None


def test_special_documents():
    docs = world_v2.DOCUMENT_V2_BY_NO
    assert docs[2].heading == "RECHNUNG — KOPIE" and docs[2].watermark == "KOPIE"
    assert docs[3].doc_type == "other" and docs[3].heading == "KONTOAUSZUG"
    assert [(ln.description, ln.amount) for ln in docs[3].lines] == [
        ("Rechnung NWL-2026-00871 vom 31.08.2026", 27846.00), ("Rechnung NWL-2026-01027 vom 31.10.2026", 28679.00)]
    assert docs[7].doc_type == "credit_note" and docs[7].referenced_invoice_number is None
    assert docs[19].doc_type == "credit_note"
    assert docs[8].subject == "TR: Facture QP-26-1107"
    assert docs[8].email_body == ("Bonjour, facture reçue au magasin la semaine dernière (affiches et flyers de la "
                                  "campagne d'automne). Merci de la régler. Luc")
    assert docs[14].subject == "Rechnung 2026/140"
    assert docs[26].tax_rate == 0.07
    for no in (13, 14, 22, 26):
        assert docs[no].bill_to_attention == "Store Berlin 01"
    assert [(ln.quantity, ln.unit_price) for ln in docs[13].lines] == [(3, 18.50), (1, 14.98), (1, 10.30)]
    assert [(ln.quantity, ln.unit_price) for ln in docs[22].lines] == [(8, 18.50), (2, 14.98), (1, 10.30)]
    assert [(ln.quantity, ln.unit_price) for ln in docs[8].lines] == [(400, 3.50), (10000, 0.10)]
    for no in (13, 14, 22):  # food (coffee, milk): the reduced German VAT rate, one rate per document
        assert (docs[no].tax_rate, docs[no].tax_label) == (0.07, "MwSt. 7 %")
    for no in (13, 22):
        assert [ln.description for ln in docs[no].lines] == [
            "Kaffeebohnen Espresso 1 kg", "Vollmilch 3,5 %, 1 l, Karton à 12", "Lieferung"]
    assert docs[22].gross_total < 500  # still under the store manager's DoA limit


def test_v2_lists_no_case_document_invoice():
    """v2 is a separate run on the same seed, as if the 12 case documents had not been received."""
    case_numbers = {spec.invoice_number for spec in world.DOCUMENTS}
    for spec in SPECS:
        assert spec.invoice_number not in case_numbers, spec.no
        assert spec.referenced_invoice_number not in case_numbers, spec.no
        printed = [line.description for line in spec.lines] + list(spec.printed_notes) + [spec.email_body or ""]
        for text in printed:
            assert not any(number in text for number in case_numbers), (spec.no, text)


def test_unknown_supplier_is_not_in_the_vendor_master():
    bb = world_v2.BERLINER_BLUMEN
    assert world_v2.UNKNOWN_SUPPLIERS == [bb]
    assert (bb.party_id, bb.canonical_name, bb.country, bb.vat_id) == (
        "X-0001", "Berliner Blumen GmbH", "DE", "DE305118442")
    assert bb.bank == world.make_iban("DE", "100500000190123456")
    assert (bb.bank_name, bb.address, bb.email_domain, bb.brand_colour, bb.agreed_terms_days) == (
        "Berliner Volksbank", ("Kastanienallee 12", "10435 Berlin", "Germany"), "berliner-blumen.de", "#AD1457", 14)
    assert bb.party_id not in world.PARTY_BY_ID
    assert bb not in world.PARTIES
    assert all(p.vat_id != bb.vat_id for p in world.PARTIES)
    assert world_v2.DOCUMENT_V2_BY_NO[26].party is bb
    # every other document comes from a supplier the seed knows
    assert all(s.supplier is None and s.party_id in world.PARTY_BY_ID for s in SPECS if s.no != 26)


def test_labels_are_complete_and_printable():
    keys = set(LABELS["en"])
    for language, labels in LABELS.items():
        assert set(labels) == keys, language
        for text in labels.values():
            check_glyphs(text)
    for spec in SPECS:
        check_glyphs(spec.heading)
        for line in spec.lines:
            check_glyphs(line.description)


# --------------------------------------------------------------------------------------------
# The files
# --------------------------------------------------------------------------------------------


def test_every_file_generated_with_the_right_suffix(v2_dir):
    suffix = {"pdf": ".pdf", "ubl_xml": ".xml", "email_body": ".txt"}
    assert sorted(p.name for p in v2_dir.iterdir()) == sorted(s.filename for s in SPECS)
    for spec in SPECS:
        path = v2_dir / spec.filename
        assert path.suffix == suffix[spec.content]
        if spec.content == "pdf":
            assert path.read_bytes().startswith(b"%PDF")
            assert len(PdfReader(path).pages) == 1
    assert [s.no for s in SPECS if s.content == "ubl_xml"] == [11]
    assert [s.no for s in SPECS if s.content == "email_body"] == [14]
    assert [(s.no, s.scan) for s in SCANS] == [(12, "clean"), (13, "low")]


def test_output_is_deterministic(v2_dir, tmp_path):
    paths = invoices_gen.generate_all_v2(tmp_path)
    assert [p.name for p in paths] == [s.filename for s in SPECS]
    assert _sha256_by_name(tmp_path) == _sha256_by_name(v2_dir)


def test_committed_files_are_up_to_date(v2_dir):
    """data/invoices_v2/ is what the generator writes today. Scans are left out: their pixels depend on the
    pypdfium2 / Pillow build, so another machine may produce other (equally valid) bytes."""
    for spec in SPECS:
        if spec.scan:
            continue
        committed = config.INVOICES_V2_DIR / spec.filename
        assert committed.read_bytes() == (v2_dir / spec.filename).read_bytes(), spec.filename


def test_v1_pdfs_unchanged(tmp_path):
    paths = invoices_gen.generate_all(tmp_path)
    assert len(paths) == 12
    for path in paths:
        assert path.read_bytes() == (config.INVOICES_DIR / path.name).read_bytes(), path.name


@pytest.mark.parametrize("spec", NATIVE, ids=lambda s: f"{s.no:02d}")
def test_language_labels_in_the_text_layer(texts, spec):
    text = texts[spec.no]
    for word in LANGUAGE_WORDS[(spec.language, spec.doc_type)]:
        assert word in text, word
    assert spec.heading in text


@pytest.mark.parametrize("spec", NATIVE, ids=lambda s: f"{s.no:02d}")
def test_key_fields_printed(texts, spec):
    text = texts[spec.no]
    assert spec.invoice_number in text
    assert spec.printed_supplier_name in text
    assert spec.bill_to.name in text
    assert " ".join(fmt_amount(spec.gross_total, spec.party.country, spec.language).split()) in text
    assert spec.currency in text
    for po in spec.po_numbers:
        assert po in text
    if spec.vat_display:
        assert spec.vat_display in text
    if spec.bill_to_attention:
        assert spec.bill_to_attention in text


def test_vat_ids_printed_in_national_formats(texts):
    assert "USt-IdNr. DE281947305" in texts[1]
    assert "NIF-IVA: ES-B86419273" in texts[9]
    assert "N° TVA : FR 62 512 345 678" in texts[21]
    assert "USt-IdNr. DE 305 118 442" in texts[26]
    assert "USt-IdNr. DE 281 947 305" in texts[3]  # default German format when nothing else is printed


def test_kopie_watermark(texts):
    text = texts[2]
    assert "RECHNUNG — KOPIE" in text
    assert text.count("KOPIE") >= 2  # heading and the diagonal stamp
    assert "KOPIE" not in texts[1]


def test_statement_is_not_an_invoice(texts):
    text = texts[3]
    assert "KONTOAUSZUG" in text and "Offener Saldo 56.525,00 EUR" in text
    assert "Rechnung NWL-2026-00871 vom 31.08.2026 27.846,00" in text
    assert "Rechnung NWL-2026-01027 vom 31.10.2026 28.679,00" in text
    for word in ("RECHNUNG ", "USt. 19", "Nettobetrag", "Rechnungsbetrag", "Bitte überweisen", "Fällig am",
                 "Verwendungszweck"):
        assert word not in text, word  # no VAT block, no payment request


def test_forwarded_invoice_prints_the_supplier_contact(texts):
    text = texts[8]
    assert "accounts@quickprint.fr" in text
    assert "luc.bernard" not in text
    assert "Affiches A2 en magasin, quadrichromie" in text and "Flyers A5 recto-verso" in text


def test_credit_notes(texts):
    assert "AVOIR" in texts[7] and "Total avoir TTC -240,00 EUR" in texts[7]
    assert "Facture d'origine" not in texts[7]  # no reference
    assert "Merci de régler" not in texts[7]
    assert "Relates to invoice HFF-2026-1031" in texts[19] and "Total credit USD -450.00" in texts[19]


def test_multi_po_invoice(texts):
    assert "Your PO 4500128" in texts[16] and "Your PO 4500130" in texts[16]


def test_french_number_format():
    """A French supplier's French document groups thousands with a no-break space; its English documents (v1 and
    document 25) and other countries keep their own style."""
    nbsp = invoices_gen.NBSP
    assert fmt_amount(14400, "FR", "fr") == f"14{nbsp}400,00"
    assert fmt_amount(-1234567.5, "FR", "fr") == f"-1{nbsp}234{nbsp}567,50"
    assert fmt_amount(0.10, "FR", "fr") == "0,10"
    assert invoices_gen.fmt_quantity(10000, "FR", "fr") == f"10{nbsp}000"
    assert invoices_gen.fmt_money(2880, "EUR", "FR", "fr") == f"2{nbsp}880,00 EUR"
    assert fmt_amount(14400, "FR") == fmt_amount(14400, "FR", "en") == "14.400,00"  # v1 style
    assert fmt_amount(3300, "NL", "fr") == "3.300,00"  # a Dutch supplier writing in French keeps its own style
    assert fmt_amount(23400, "DE", "de") == "23.400,00" and fmt_amount(9600, "US", "fr") == "9,600.00"
    check_glyphs(nbsp)


def test_french_documents_print_french_grouping(texts):
    assert "Total TTC 14 400,00 EUR" in texts[21] and "Merci de régler 14 400,00 EUR avant le" in texts[21]
    assert "Flyers A5 recto-verso 10 000 0,10 1 000,00" in texts[8] and "Total HT 2 400,00 EUR" in texts[8]
    assert "Total TTC 7 200,00 EUR" in texts[6]
    assert "14.400" not in texts[21] and "10.000" not in texts[8]
    assert "Total due 1.100,00 EUR" in texts[25]  # QuickPrint in English, as in the case documents
    assert "Total TTC 3.300,00 EUR" in texts[5]  # Cleanspace (NL) in French


def test_wrapping_keeps_a_grouped_number_together():
    nbsp = invoices_gen.NBSP
    line = f"Merci de régler 14{nbsp}400,00 EUR avant le 18/12/2026 par virement sur le compte :"
    parts = invoices_gen._wrap([line], "Helvetica", 9.5, 90)
    assert len(parts) > 1 and f"14{nbsp}400,00" in " ".join(parts)
    assert " ".join(parts) == line
    assert invoices_gen._wrap(["no grouped number here"], "Helvetica", 9.5, 60) == invoices_gen.simpleSplit(
        "no grouped number here", "Helvetica", 9.5, 60)


def test_german_register_lines(texts):
    """Partnerships (OHG) in section A, companies (GmbH) in section B; Berlin's court is Charlottenburg."""
    assert "Kaffee & Co OHG · Sitz: Berlin · Amtsgericht Charlottenburg, HRA 168006 B" in texts[22]
    assert "Berliner Blumen GmbH · Sitz: Berlin · Amtsgericht Charlottenburg, HRB 197330 B" in texts[26]
    assert "Nordwind Logistics GmbH · Sitz: Hamburg · Amtsgericht Hamburg, HRB 109358" in texts[1]
    assert "Amtsgericht Berlin" not in " ".join(texts.values())
    kaffee = world.PARTY_BY_ID["P-0009"]
    assert invoices_gen.fake_registration(kaffee) == "Commercial register: Berlin local court, HRB 168006"  # v1


def test_localised_documents_spell_names_with_accents(texts):
    assert "42 avenue Jean Jaurès" in texts[8] and "Banque : Crédit Lyonnais" in texts[8]
    assert "Nos coordonnées bancaires : Crédit Lyonnais" in texts[7]
    for no in (5, 6, 7, 8, 9, 21):  # Velox Retail SAS, Paris
        assert "25 rue de la Chaussée-d'Antin" in texts[no], no
    assert "Calle de Alcalá 145" in texts[9] and "Calle de Alcalá 145" in texts[10]
    assert "Hafenstraße 12" in texts[1] and "Torstraße 140" in texts[1]
    assert "Oranienstraße 25" in texts[22] and "Rosenthaler Straße 40" in texts[22]
    assert "Rosenthaler Straße 40" in texts[26]
    for no in (4, 12, 15, 16, 17, 18, 19, 20, 24, 25):  # English documents print the ASCII spelling of the world
        assert not any(ch in texts[no] for ch in "éèáß"), no
    assert "Torstrasse 140" in texts[4] and "Bahnhofstrasse 10" in texts[16] and "8001 Zurich" in texts[16]
    assert "Chaussee" not in " ".join(texts[no] for no in (5, 6, 7, 8, 9, 21))


def test_spelling_follows_the_address_country():
    assert invoices_gen.spell("Bahnhofstrasse 10", "Switzerland", "de") == "Bahnhofstrasse 10"  # no ß in Switzerland
    assert invoices_gen.spell("8001 Zurich", "Switzerland", "de") == "8001 Zürich"
    assert invoices_gen.spell("Torstrasse 140", "Germany", "es") == "Torstraße 140"
    assert invoices_gen.spell("Torstrasse 140", "Germany", "en") == "Torstrasse 140"
    assert invoices_gen.localise_address(("Calle de Alcala 145", "28009 Madrid", "Spain"), "fr") == [
        "Calle de Alcalá 145", "28009 Madrid", "Espagne"]
    quickprint = world.PARTY_BY_ID["P-0008"]
    assert invoices_gen.bank_name(quickprint, "fr") == "Crédit Lyonnais"
    assert invoices_gen.bank_name(quickprint, "en") == quickprint.bank_name == "Credit Lyonnais"  # world unchanged
    for pairs in invoices_gen.SPELLINGS.values():
        for _, national in pairs:
            check_glyphs(national)
    for national in invoices_gen.BANK_SPELLINGS.values():
        check_glyphs(national)


@pytest.mark.parametrize("spec", SCANS, ids=lambda s: f"{s.no:02d}")
def test_scans_have_no_text_layer(v2_dir, spec):
    page = PdfReader(v2_dir / spec.filename).pages[0]
    assert (page.extract_text() or "").strip() == ""
    images = [xo.get_object() for xo in page["/Resources"]["/XObject"].values()]
    assert len(images) == 1 and images[0]["/Subtype"] == "/Image"
    assert "/DCTDecode" in images[0]["/Filter"]  # JPEG


@pytest.mark.parametrize("spec", SCANS, ids=lambda s: f"{s.no:02d}")
def test_scans_are_full_a4_at_exactly_300_dpi(v2_dir, spec):
    import pypdfium2 as pdfium

    assert invoices_gen.SCAN_DPI == 300 and invoices_gen.SCAN_SIZE == (2480, 3508)
    image = next(iter(PdfReader(v2_dir / spec.filename).pages[0]["/Resources"]["/XObject"].values())).get_object()
    assert (image["/Width"], image["/Height"]) == (2480, 3508)
    assert image["/ColorSpace"] == ("/DeviceGray" if spec.scan == "low" else "/DeviceRGB")
    pdf = pdfium.PdfDocument(v2_dir / spec.filename)
    try:
        [obj] = [o for o in pdf[0].get_objects() if o.type == pdfium.raw.FPDF_PAGEOBJ_IMAGE]
        meta = obj.get_metadata()
        assert (meta.width, meta.height) == (2480, 3508)
        assert round(meta.horizontal_dpi, 2) == round(meta.vertical_dpi, 2) == 300.0
        left, bottom, right, top = obj.get_bounds()  # the whole A4 page, centred (within 0.1 pt)
        assert max(abs(left), abs(bottom), abs(right - invoices_gen.PAGE_W), abs(top - invoices_gen.PAGE_H)) < 0.1
    finally:
        pdf.close()


@pytest.mark.parametrize("spec", SCANS, ids=lambda s: f"{s.no:02d}")
def test_scan_skew_keeps_the_page_content_inside_the_frame(spec):
    """The page is rotated about its centre within the 300 dpi frame: every inked pixel of the native page stays
    inside it, at least 5 mm from the edge (about 7 mm at 3°)."""
    from PIL import ImageOps

    native = io.BytesIO()
    invoices_gen._draw_native(spec, native)
    image, _ = invoices_gen._rasterise(native.getvalue(), [])
    assert image.size == invoices_gen.SCAN_SIZE
    left, top, right, bottom = ImageOps.invert(image.convert("L")).point(lambda v: 255 if v > 8 else 0).getbbox()
    width, height = image.size
    angle = math.radians(invoices_gen.SCAN_PROFILES[spec.scan].skew)
    assert invoices_gen.SCAN_PROFILES[spec.scan].skew == {"clean": 1.5, "low": 3.0}[spec.scan]
    margin = 5 / 25.4 * invoices_gen.SCAN_DPI  # 5 mm in pixels
    for x in (left, right):
        for y in (top, bottom):
            for sign in (1, -1):  # either direction of rotation
                dx, dy = x - width / 2, y - height / 2
                rx = width / 2 + dx * math.cos(angle) + sign * dy * math.sin(angle)
                ry = height / 2 - sign * dx * math.sin(angle) + dy * math.cos(angle)
                assert margin <= rx <= width - margin and margin <= ry <= height - margin, (x, y, rx, ry)


@pytest.mark.parametrize("scan", ["clean", "low"])
def test_degrade_rotates_inside_the_frame_with_the_scanner_lid_in_the_corners(scan):
    import random

    from PIL import Image

    profile = invoices_gen.SCAN_PROFILES[scan]
    page = Image.new("RGB", (400, 600), "white")
    out = invoices_gen._degrade(page, [], profile, random.Random(1))
    assert out.size == (400, 600)
    lid = profile.border if profile.greyscale else (profile.border,) * 3
    for corner in ((0, 0), (399, 0), (0, 599), (399, 599)):
        assert out.getpixel(corner) == lid, corner  # uncovered by the rotated page
    assert out.getpixel((200, 300)) != lid


def test_ubl_file(v2_dir):
    spec = world_v2.DOCUMENT_V2_BY_NO[11]
    data = (v2_dir / spec.filename).read_bytes()
    root = ET.fromstring(data)
    ns = {"cbc": "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2"}
    assert root.tag == "{urn:oasis:names:specification:ubl:schema:xsd:Invoice-2}Invoice"
    assert root.findtext("cbc:CustomizationID", namespaces=ns) == (
        "urn:cen.eu:en16931:2017#compliant#urn:fdc:peppol.eu:2017:poacc:billing:3.0")
    assert root.findtext("cbc:ProfileID", namespaces=ns) == "urn:fdc:peppol.eu:2017:poacc:billing:01:1.0"
    assert root.findtext("cbc:ID", namespaces=ns) == "MM-2026-248"


def test_ubl_file_round_trips_through_the_parser(v2_dir):
    ubl = pytest.importorskip("app.ubl")
    spec = world_v2.DOCUMENT_V2_BY_NO[11]
    parsed = ubl.parse_ubl((v2_dir / spec.filename).read_bytes())
    truth = make_fixtures.ground_truth(spec)["extraction"]
    for name, field in truth.items():
        if name == "notes":  # the parser labels the document itself
            continue
        assert parsed[name]["value"] == field["value"], name
        assert parsed[name]["confidence"] == (1.0 if field["value"] is not None else 0.0), name


def test_email_body_file(v2_dir):
    spec = world_v2.DOCUMENT_V2_BY_NO[14]
    data = (v2_dir / spec.filename).read_bytes()
    assert b"\r\n" not in data
    text = data.decode("utf-8")
    assert text == spec.email_body
    assert "2026/140" in text and "52,43 EUR" in text
    assert ("anbei unsere Rechnung 2026/140 vom 06.11.2026 über 52,43 EUR (netto 49,00 EUR zzgl. 7 % MwSt. "
            "3,43 EUR) für die Kaffeelieferung KW 45 an die Filiale Berlin 01.") in text
    assert f"IBAN {world.format_iban(world.PARTY_BY_ID['P-0009'].bank)} (Kaffee & Co)" in text
    assert text.startswith("Guten Tag,") and "Mit freundlichen Grüßen" in text and "Kaffee & Co OHG" in text


def test_cli_selects_the_dataset(monkeypatch):
    calls = []
    monkeypatch.setattr(invoices_gen, "generate_all", lambda out: calls.append("v1") or [])
    monkeypatch.setattr(invoices_gen, "generate_all_v2", lambda out: calls.append("v2") or [])
    for argv, expected in (([], ["v1"]), (["--v2"], ["v2"]), (["--all"], ["v1", "v2"])):
        calls.clear()
        invoices_gen.main(argv)
        assert calls == expected, argv


# --------------------------------------------------------------------------------------------
# Ground-truth fixtures (tests/fixtures_v2/)
# --------------------------------------------------------------------------------------------


def _fixture(spec) -> dict:
    return json.loads((config.FIXTURES_V2_DIR / f"{Path(spec.filename).stem}.json").read_text(encoding="utf-8"))


def test_one_fixture_per_pdf_only():
    names = sorted(p.name for p in config.FIXTURES_V2_DIR.glob("*.json"))
    assert names == sorted(f"{Path(s.filename).stem}.json" for s in PDFS)
    assert len(names) == 24  # none for the UBL e-invoice (11) and the email body (14)


@pytest.mark.parametrize("spec", PDFS, ids=lambda s: f"{s.no:02d}")
def test_fixtures_validate_and_are_up_to_date(spec):
    record = _fixture(spec)
    InvoiceExtraction.model_validate(record["extraction"])
    assert record == make_fixtures.ground_truth(spec)  # regenerate with: python -m tests.make_fixtures --v2
    assert record["file_name"] == spec.filename


@pytest.mark.parametrize("spec", PDFS, ids=lambda s: f"{s.no:02d}")
def test_fixture_conventions(spec):
    ex = _fixture(spec)["extraction"]
    vat, iban = ex["supplier_vat_id"]["value"], ex["supplier_iban"]["value"]
    assert " " not in vat and not vat.startswith(("USt", "VAT", "NIF", "N°"))
    assert iban == spec.party.bank
    assert iban.startswith("ABA ") or " " not in iban  # IBAN compact; US bank details "ABA <routing> ACCT <no>"
    assert ex["invoice_number"]["value"] == spec.invoice_number
    assert ex["gross_total"]["value"] == spec.gross_total
    if spec.doc_type == "credit_note":
        assert ex["gross_total"]["value"] < 0 and ex["net_total"]["value"] < 0
        assert all(line["amount"] < 0 and line["quantity"] > 0 for line in ex["lines"]["value"])


def test_statement_fixture():
    ex = _fixture(world_v2.DOCUMENT_V2_BY_NO[3])["extraction"]
    assert ex["doc_type"]["value"] == "statement"
    assert ex["invoice_number"]["value"] == "KA-2026-11"
    assert ex["gross_total"]["value"] == 56525.0
    assert ex["tax_total"]["value"] is None and ex["due_date"]["value"] is None
    assert [ln["amount"] for ln in ex["lines"]["value"]] == [27846.0, 28679.0]
    assert "Statement of account, not an invoice" in ex["notes"]["value"]


def test_scan_fixtures_carry_simulated_confidences():
    for no, expected in ((12, {"*": 0.93}), (13, {"*": 0.88, "gross_total": 0.62, "invoice_number": 0.71})):
        record = _fixture(world_v2.DOCUMENT_V2_BY_NO[no])
        assert record["model"] == make_fixtures.SCAN_FIXTURE_MODEL
        assert "simulated" in record["model"] and record["model"].startswith("fixture")
        for name, field in record["extraction"].items():
            wanted = expected.get(name, expected["*"]) if field["value"] is not None else 0.0
            assert field["confidence"] == wanted, (no, name)
    for spec in PDFS:
        if not spec.scan:
            record = _fixture(spec)
            assert record["model"] == FIXTURE_MODEL
            assert {f["confidence"] for f in record["extraction"].values() if f["value"] is not None} == {0.99}


def test_fixture_vat_ids_as_printed():
    assert _fixture(world_v2.DOCUMENT_V2_BY_NO[9])["extraction"]["supplier_vat_id"]["value"] == "ES-B86419273"
    assert _fixture(world_v2.DOCUMENT_V2_BY_NO[12])["extraction"]["supplier_vat_id"]["value"] == "GB293847561"
    assert _fixture(world_v2.DOCUMENT_V2_BY_NO[21])["extraction"]["supplier_vat_id"]["value"] == "FR62512345678"
    assert _fixture(world_v2.DOCUMENT_V2_BY_NO[26])["extraction"]["supplier_vat_id"]["value"] == "DE305118442"
