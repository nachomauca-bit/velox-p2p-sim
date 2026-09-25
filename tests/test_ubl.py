"""UBL e-invoices (app/ubl.py): Peppol BIS Billing 3.0 rendering, parsing without a model call, safe XML.

The round trip parse_ubl(render_ubl(spec)) must give back the ground truth of tests/make_fixtures.py for every
field a UBL invoice carries, with confidence 1.0 instead of 0.99 (notes are the fixed UBL text).
"""
from __future__ import annotations

import math
import xml.etree.ElementTree as ET

import pytest

from app import ubl, world
from app.extract import FIELDS, InvoiceExtraction
from tests.make_fixtures import ground_truth

NS = ubl.NS
PDF_BYTES = b"%PDF-1.4\n1 0 obj << /Type /Catalog >> endobj\ntrailer << /Root 1 0 R >>\n%%EOF\n"


def xml_doc(body: str, root: str = "Invoice") -> bytes:
    """A hand-written UBL document around `body` (elements with cbc: / cac: prefixes)."""
    ns = ubl.INVOICE_NS if root == "Invoice" else ubl.CREDIT_NOTE_NS
    return (f'<?xml version="1.0" encoding="UTF-8"?>\n<{root} xmlns="{ns}" xmlns:cac="{ubl.CAC_NS}" '
            f'xmlns:cbc="{ubl.CBC_NS}">{body}</{root}>').encode("utf-8")


def assert_round_trip(spec: world.DocumentSpec) -> dict:
    parsed = ubl.parse_ubl(ubl.render_ubl(spec))
    truth = ground_truth(spec)["extraction"]
    for name in FIELDS:
        if name == "notes":
            continue
        assert parsed[name]["value"] == truth[name]["value"], name
        assert parsed[name]["confidence"] == (1.0 if truth[name]["value"] is not None else 0.0), name
    assert parsed["notes"] == {"value": ubl.UBL_NOTES, "confidence": 1.0}
    return parsed


# --------------------------------------------------------------------------------------------
# Round trip and schema
# --------------------------------------------------------------------------------------------


def test_fields_follow_the_extraction_schema():
    assert ubl.FIELDS == FIELDS


@pytest.mark.parametrize("spec", world.DOCUMENTS, ids=lambda s: f"doc{s.no:02d}")
def test_round_trip_gives_back_the_ground_truth(spec):
    parsed = assert_round_trip(spec)
    # Exactly the shape of InvoiceExtraction.model_dump(mode="json"), so extract.py can use it as is.
    assert InvoiceExtraction.model_validate(parsed).model_dump(mode="json") == parsed


def test_round_trip_invoice_and_credit_note():
    invoice = assert_round_trip(world.DOCUMENT_BY_NO[3])
    assert invoice["doc_type"]["value"] == "invoice"
    assert invoice["po_numbers"]["value"] == ["4500117"]
    assert invoice["payment_terms_days"]["value"] == 45
    credit = assert_round_trip(world.DOCUMENT_BY_NO[4])
    assert credit["doc_type"]["value"] == "credit_note"
    assert credit["referenced_invoice_number"]["value"] == "INV-2026-0457"
    assert (credit["net_total"]["value"], credit["tax_total"]["value"], credit["gross_total"]["value"]) == (
        -1500.0, -300.0, -1800.0)
    assert credit["lines"]["value"] == [{"description": world.DOCUMENT_BY_NO[4].lines[0].description,
                                         "quantity": 1.0, "unit_price": -1500.0, "amount": -1500.0}]


def test_round_trip_v2_document_11():
    world_v2 = pytest.importorskip("app.world_v2", reason="app/world_v2.py (dataset v2) is not there yet")
    spec = world_v2.DOCUMENT_V2_BY_NO[11]
    assert spec.content == "ubl_xml"
    parsed = assert_round_trip(spec)
    assert parsed["supplier_name"]["value"] == "Metro Media GmbH"
    assert parsed["po_numbers"]["value"] == ["4500126"]
    assert parsed["gross_total"]["value"] == 8925.0
    assert "Außenwerbung" in parsed["lines"]["value"][0]["description"]


# --------------------------------------------------------------------------------------------
# Rendering: Peppol BIS Billing 3.0
# --------------------------------------------------------------------------------------------


def test_render_writes_a_peppol_bis_3_invoice():
    spec = world.DOCUMENT_BY_NO[1]
    data = ubl.render_ubl(spec)
    assert data.startswith(b'<?xml version="1.0" encoding="UTF-8"?>')
    assert "Berlin DC — September 2026".encode("utf-8") in data  # UTF-8, not escaped
    assert data == ubl.render_ubl(spec)  # deterministic
    root = ET.fromstring(data)
    assert root.tag == f"{{{ubl.INVOICE_NS}}}Invoice"
    assert root.findtext("cbc:CustomizationID", namespaces=NS) == ubl.CUSTOMIZATION_ID
    assert root.findtext("cbc:ProfileID", namespaces=NS) == ubl.PROFILE_ID
    assert root.findtext("cbc:InvoiceTypeCode", namespaces=NS) == "380"
    assert root.findtext("cbc:DueDate", namespaces=NS) == "2026-10-14"
    assert root.findtext("cac:PaymentTerms/cbc:Note", namespaces=NS) == "14 days net"
    assert root.findtext("cac:ContractDocumentReference/cbc:ID", namespaces=NS) == "CT-2025-001"
    assert root.findtext("cbc:BuyerReference", namespaces=NS) == "VDE"  # no PO: Peppol needs a buyer reference
    supplier = root.find("cac:AccountingSupplierParty/cac:Party", NS)
    assert supplier.find("cbc:EndpointID", NS).attrib == {"schemeID": "9930"}
    assert supplier.findtext("cac:PostalAddress/cbc:PostalZone", namespaces=NS) == "20457"
    assert supplier.findtext("cac:PostalAddress/cbc:CityName", namespaces=NS) == "Hamburg"
    amounts = [el for el in root.iter() if el.tag.endswith("Amount")]
    assert amounts and all(el.get("currencyID") == "EUR" for el in amounts)
    assert root.findtext("cac:LegalMonetaryTotal/cbc:PayableAmount", namespaces=NS) == "27846.00"
    assert [el.get("unitCode") for el in root.iterfind("cac:InvoiceLine/cbc:InvoicedQuantity", NS)] == ["C62"] * 2


def test_render_writes_a_credit_note_with_positive_amounts():
    root = ET.fromstring(ubl.render_ubl(world.DOCUMENT_BY_NO[4]))
    assert root.tag == f"{{{ubl.CREDIT_NOTE_NS}}}CreditNote"
    assert root.findtext("cbc:CreditNoteTypeCode", namespaces=NS) == "381"
    assert root.find("cbc:DueDate", NS) is None  # UBL 2.1 credit notes have no DueDate
    assert root.findtext("cac:BillingReference/cac:InvoiceDocumentReference/cbc:ID", namespaces=NS) == "INV-2026-0457"
    assert root.findtext("cac:CreditNoteLine/cbc:CreditedQuantity", namespaces=NS) == "1"
    amounts = [el.text for el in root.iter() if el.tag.endswith("Amount")]
    assert amounts and not any(text.startswith("-") for text in amounts)  # the document type carries the sign


@pytest.mark.parametrize("no, category, percent", [
    (3, "S", "20"),  # French VAT 20%
    (5, "AE", "0"),  # reverse charge
    (6, "K", "0"),  # intra-Community supply
    (12, "G", "0"),  # export
    (7, "O", None),  # US: outside the scope of VAT, no rate
])
def test_tax_category_follows_the_rate_and_the_legal_mention(no, category, percent):
    spec = world.DOCUMENT_BY_NO[no]
    root = ET.fromstring(ubl.render_ubl(spec))
    tax = root.find("cac:TaxTotal/cac:TaxSubtotal/cac:TaxCategory", NS)
    assert tax.findtext("cbc:ID", namespaces=NS) == category
    assert tax.findtext("cbc:Percent", namespaces=NS) == percent
    reason = tax.findtext("cbc:TaxExemptionReason", namespaces=NS)
    assert (reason is None) == (category == "S")
    line_category = root.find("cac:InvoiceLine/cac:Item/cac:ClassifiedTaxCategory", NS)
    assert line_category.findtext("cbc:ID", namespaces=NS) == category


def test_render_keeps_us_and_attention_details():
    shopsys = ET.fromstring(ubl.render_ubl(world.DOCUMENT_BY_NO[7]))
    supplier = shopsys.find("cac:AccountingSupplierParty/cac:Party", NS)
    assert supplier.find("cbc:EndpointID", NS).attrib == {"schemeID": "EM"}  # no VAT-based address in the US
    assert supplier.findtext("cac:PartyTaxScheme/cac:TaxScheme/cbc:ID", namespaces=NS) == "TAX"  # an EIN
    assert supplier.findtext("cac:PostalAddress/cbc:CountrySubentity", namespaces=NS) == "CA"
    assert shopsys.findtext("cac:PaymentMeans/cbc:PaymentMeansCode", namespaces=NS) == "30"  # not SEPA
    kaffee = ET.fromstring(ubl.render_ubl(world.DOCUMENT_BY_NO[10]))
    customer = kaffee.find("cac:AccountingCustomerParty/cac:Party", NS)
    assert customer.findtext("cac:Contact/cbc:Name", namespaces=NS) == "Store Berlin 01"
    assert customer.findtext("cac:PostalAddress/cbc:StreetName", namespaces=NS) == "Rosenthaler Strasse 40"
    assert customer.find("cac:PartyTaxScheme", NS) is None  # the bill-to VAT ID is not printed on it


# --------------------------------------------------------------------------------------------
# Parsing: missing and odd values
# --------------------------------------------------------------------------------------------


def test_missing_elements_are_null_with_confidence_zero():
    parsed = ubl.parse_ubl(xml_doc("<cbc:ID>X-1</cbc:ID><cbc:IssueDate>2026-11-02</cbc:IssueDate>"))
    assert InvoiceExtraction.model_validate(parsed).model_dump(mode="json") == parsed
    present = {"doc_type": "invoice", "invoice_number": "X-1", "invoice_date": "2026-11-02", "notes": ubl.UBL_NOTES}
    for name in FIELDS:
        expected = {"value": present[name], "confidence": 1.0} if name in present else {"value": None,
                                                                                         "confidence": 0.0}
        assert parsed[name] == expected, name


def test_unreadable_values_are_null():
    parsed = ubl.parse_ubl(xml_doc(
        "<cbc:ID> </cbc:ID><cbc:IssueDate>31.10.2026</cbc:IssueDate><cbc:DocumentCurrencyCode>EUR"
        "</cbc:DocumentCurrencyCode><cac:LegalMonetaryTotal><cbc:TaxExclusiveAmount currencyID=\"EUR\">n/a"
        "</cbc:TaxExclusiveAmount><cbc:TaxInclusiveAmount currencyID=\"EUR\">119.00</cbc:TaxInclusiveAmount>"
        "</cac:LegalMonetaryTotal>"))
    assert parsed["invoice_number"] == {"value": None, "confidence": 0.0}
    assert parsed["invoice_date"] == {"value": None, "confidence": 0.0}  # not an ISO date
    assert parsed["net_total"] == {"value": None, "confidence": 0.0}
    assert parsed["gross_total"] == {"value": 119.0, "confidence": 1.0}


@pytest.mark.parametrize("terms, expected", [
    ("<cac:PaymentTerms><cbc:Note>Zahlbar innerhalb von 14 Tagen netto</cbc:Note></cac:PaymentTerms>", 14),
    ("<cac:PaymentTerms><cbc:Note>Paiement à 45 jours</cbc:Note></cac:PaymentTerms>", 45),
    ("<cac:PaymentTerms><cbc:Note>Payable on receipt</cbc:Note></cac:PaymentTerms>", 30),  # from the dates
    ("", 30),  # from the dates
])
def test_payment_terms_from_the_note_else_from_the_dates(terms, expected):
    parsed = ubl.parse_ubl(xml_doc(
        f"<cbc:ID>X-2</cbc:ID><cbc:IssueDate>2026-10-30</cbc:IssueDate><cbc:DueDate>2026-11-29</cbc:DueDate>{terms}"))
    assert parsed["payment_terms_days"] == {"value": expected, "confidence": 1.0}
    assert parsed["due_date"]["value"] == "2026-11-29"


def test_no_terms_without_a_note_or_a_due_date():
    parsed = ubl.parse_ubl(xml_doc("<cbc:ID>X-3</cbc:ID><cbc:IssueDate>2026-10-30</cbc:IssueDate>"))
    assert parsed["payment_terms_days"] == {"value": None, "confidence": 0.0}


def test_parties_bank_and_references_from_other_senders():
    parsed = ubl.parse_ubl(xml_doc(
        "<cbc:ID>X-4</cbc:ID><cbc:DocumentCurrencyCode>EUR</cbc:DocumentCurrencyCode>"
        "<cac:OrderReference><cbc:ID>4500128</cbc:ID></cac:OrderReference>"
        "<cac:AccountingSupplierParty><cac:Party><cac:PartyName><cbc:Name>SecureNet</cbc:Name></cac:PartyName>"
        "<cac:PartyTaxScheme><cbc:CompanyID>CHE-419.287.563 MWST-REG</cbc:CompanyID><cac:TaxScheme><cbc:ID>TAX"
        "</cbc:ID></cac:TaxScheme></cac:PartyTaxScheme>"
        "<cac:PartyTaxScheme><cbc:CompanyID>DE 318 273 645</cbc:CompanyID><cac:TaxScheme><cbc:ID>VAT</cbc:ID>"
        "</cac:TaxScheme></cac:PartyTaxScheme></cac:Party></cac:AccountingSupplierParty>"
        "<cac:AccountingCustomerParty><cac:Party><cac:PartyName><cbc:Name>Velox Retail GmbH</cbc:Name>"
        "</cac:PartyName></cac:Party></cac:AccountingCustomerParty>"
        "<cac:PaymentMeans><cbc:PaymentMeansCode>58</cbc:PaymentMeansCode><cac:PayeeFinancialAccount>"
        "<cbc:ID>CH12 0070 0110 0087 6543 2</cbc:ID></cac:PayeeFinancialAccount></cac:PaymentMeans>"
        "<cac:TaxTotal><cbc:TaxAmount currencyID=\"CHF\">15.00</cbc:TaxAmount></cac:TaxTotal>"
        "<cac:TaxTotal><cbc:TaxAmount currencyID=\"EUR\">16.00</cbc:TaxAmount></cac:TaxTotal>"
        "<cac:InvoiceLine><cbc:ID>1</cbc:ID><cbc:InvoicedQuantity unitCode=\"C62\">200</cbc:InvoicedQuantity>"
        "<cbc:LineExtensionAmount currencyID=\"EUR\">5.00</cbc:LineExtensionAmount>"
        "<cac:OrderLineReference><cbc:LineID>1</cbc:LineID><cac:OrderReference><cbc:ID>4500130</cbc:ID>"
        "</cac:OrderReference></cac:OrderLineReference>"
        "<cac:Item><cbc:Description>Cable ties</cbc:Description></cac:Item>"
        "<cac:Price><cbc:PriceAmount currencyID=\"EUR\">2.50</cbc:PriceAmount>"
        "<cbc:BaseQuantity unitCode=\"C62\">100</cbc:BaseQuantity></cac:Price></cac:InvoiceLine>"))
    assert parsed["supplier_name"]["value"] == "SecureNet"  # no registration name: the trading name
    assert parsed["supplier_vat_id"]["value"] == "DE318273645"  # the VAT scheme first, compacted
    assert parsed["bill_to_name"]["value"] == "Velox Retail GmbH"
    assert parsed["supplier_iban"]["value"] == "CH1200700110008765432"
    assert parsed["tax_total"]["value"] == 16.0  # the TaxTotal in the document currency
    assert parsed["po_numbers"]["value"] == ["4500128", "4500130"]  # plus a line-level order reference
    assert parsed["lines"]["value"] == [{"description": "Cable ties", "quantity": 200.0, "unit_price": 0.025,
                                         "amount": 5.0}]


def test_credit_note_amounts_are_negative_and_zero_stays_positive():
    parsed = ubl.parse_ubl(xml_doc(
        "<cbc:ID>CN-1</cbc:ID><cbc:DocumentCurrencyCode>USD</cbc:DocumentCurrencyCode>"
        "<cac:TaxTotal><cbc:TaxAmount currencyID=\"USD\">0.00</cbc:TaxAmount></cac:TaxTotal>"
        "<cac:LegalMonetaryTotal><cbc:TaxExclusiveAmount currencyID=\"USD\">450.00</cbc:TaxExclusiveAmount>"
        "<cbc:TaxInclusiveAmount currencyID=\"USD\">450.00</cbc:TaxInclusiveAmount></cac:LegalMonetaryTotal>",
        root="CreditNote"))
    assert parsed["doc_type"]["value"] == "credit_note"
    assert parsed["net_total"]["value"] == parsed["gross_total"]["value"] == -450.0
    tax = parsed["tax_total"]["value"]
    assert tax == 0.0 and math.copysign(1.0, tax) == 1.0  # not -0.0
    assert parsed["lines"] == {"value": None, "confidence": 0.0}


# --------------------------------------------------------------------------------------------
# Rejections: not UBL, not XML, DOCTYPE / entities (never expanded, never fetched)
# --------------------------------------------------------------------------------------------

BILLION_LAUGHS = (b'<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol">'
                  b'<!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">]>'
                  b'<Invoice xmlns="' + ubl.INVOICE_NS.encode() + b'">&lol2;</Invoice>')
EXTERNAL_ENTITY = (b'<?xml version="1.0"?><!DOCTYPE Invoice [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
                   b'<Invoice xmlns="' + ubl.INVOICE_NS.encode() + b'"><ID>&xxe;</ID></Invoice>')
PARAMETER_ENTITY = (b'<?xml version="1.0"?><!DOCTYPE Invoice [<!ENTITY % ext SYSTEM "http://example.com/x.dtd">'
                    b'%ext;]><Invoice xmlns="' + ubl.INVOICE_NS.encode() + b'"/>')
PLAIN_DOCTYPE = b'<?xml version="1.0"?><!DOCTYPE Invoice><Invoice xmlns="' + ubl.INVOICE_NS.encode() + b'"/>'
UTF16_DOCTYPE = ('<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE x [<!ENTITY a "b">]>'
                 f'<Invoice xmlns="{ubl.INVOICE_NS}">&a;</Invoice>').encode("utf-16")


@pytest.mark.parametrize("data", [BILLION_LAUGHS, EXTERNAL_ENTITY, PARAMETER_ENTITY, PLAIN_DOCTYPE, UTF16_DOCTYPE],
                         ids=["billion-laughs", "external-entity", "parameter-entity", "doctype", "utf16-doctype"])
def test_doctype_and_entities_are_rejected(data):
    with pytest.raises(ValueError, match="DOCTYPE"):
        ubl.parse_ubl(data)
    assert ubl.is_ubl(data) is False


@pytest.mark.parametrize("data, message", [
    (b"", "empty"),
    (b"   \n", "empty"),
    (PDF_BYTES, "not well-formed"),
    (b"<Invoice><unclosed></Invoice>", "not well-formed"),
    (b"<Invoice>&undefined;</Invoice>", "not well-formed"),
    (b"<Invoice/>", "not a UBL"),  # right name, no namespace
    (b'<rsm:CrossIndustryInvoice xmlns:rsm="urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100"/>',
     "not a UBL"),  # a CII e-invoice
    (b'<Order xmlns="urn:oasis:names:specification:ubl:schema:xsd:Order-2"/>', "not a UBL"),  # UBL, not an invoice
])
def test_non_ubl_input_is_rejected(data, message):
    with pytest.raises(ValueError, match=message):
        ubl.parse_ubl(data)
    assert ubl.is_ubl(data) is False


def test_is_ubl_accepts_invoices_and_credit_notes():
    assert ubl.is_ubl(ubl.render_ubl(world.DOCUMENT_BY_NO[3]))
    assert ubl.is_ubl(ubl.render_ubl(world.DOCUMENT_BY_NO[4]))
    assert ubl.is_ubl(xml_doc("<cbc:ID>1</cbc:ID>", root="CreditNote"))
    assert ubl.is_ubl(None) is False  # type: ignore[arg-type]  # never raises
