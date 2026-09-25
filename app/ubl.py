"""Structured e-invoices: Peppol BIS Billing 3.0 UBL (Invoice and CreditNote), read without a model call.

- parse_ubl(data) returns a dict shaped exactly like extract.InvoiceExtraction.model_dump(mode="json"):
  confidence 1.0 for every value present in the XML, null with confidence 0.0 for a missing one. Credit-note
  amounts come out negative (the AP sign convention of the extraction schema; UBL writes them positive).
  Amounts and quantities must be in the xs:decimal form UBL uses ("1234.50"): "NaN", "INF", "1e3" or "1,5" come
  out null with confidence 0, so the gate routes the document to review. Payment terms are DueDate - IssueDate
  (BT-9 is the authoritative due date); the terms note is read only when a date is missing, and a cash-discount
  clause ("2 % Skonto innerhalb 8 Tagen") is never taken for the net terms.
- render_ubl(spec) writes a world.DocumentSpec as a Peppol BIS Billing 3.0 Invoice or CreditNote (UTF-8), used
  by the v2 test set (document 11). parse_ubl(render_ubl(spec)) gives back the spec's ground truth for every
  field a UBL invoice carries (notes excepted: they are fixed text here).
- Safe to feed untrusted mail attachments: a document with a DOCTYPE or entity declarations is rejected before
  ElementTree sees it (no entity expansion, no external entities), and any XML that is not a UBL Invoice or
  CreditNote raises ValueError (an unknown or unusable declared encoding included); is_ubl never raises.

Stdlib only and no import of app.extract (which imports this module): the IMAP poller loads it cheaply.
Limitation: UBL carries one order reference per document (BT-13); render_ubl writes the first PO number, and
parse_ubl also reads line-level order references when another sender uses them.
"""
from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET
from datetime import date
from typing import Any, Optional
from xml.parsers import expat

from app import world

UBL_MODEL = "UBL e-invoice (parsed, no model call)"
UBL_NOTES = "Structured e-invoice (UBL, Peppol BIS Billing 3.0)"

INVOICE_NS = "urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"
CREDIT_NOTE_NS = "urn:oasis:names:specification:ubl:schema:xsd:CreditNote-2"
CAC_NS = "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
CBC_NS = "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2"
NS = {"cac": CAC_NS, "cbc": CBC_NS}

CUSTOMIZATION_ID = "urn:cen.eu:en16931:2017#compliant#urn:fdc:peppol.eu:2017:poacc:billing:3.0"
PROFILE_ID = "urn:fdc:peppol.eu:2017:poacc:billing:01:1.0"

# Root element -> (doc_type, line element, quantity element)
_ROOTS = {
    f"{{{INVOICE_NS}}}Invoice": ("invoice", "InvoiceLine", "InvoicedQuantity"),
    f"{{{CREDIT_NOTE_NS}}}CreditNote": ("credit_note", "CreditNoteLine", "CreditedQuantity"),
}

# Field order of extract.InvoiceExtraction (kept here to avoid a circular import; tests check it matches).
FIELDS = ("doc_type", "supplier_name", "supplier_vat_id", "supplier_iban", "supplier_country", "bill_to_name",
          "bill_to_vat_id", "invoice_number", "invoice_date", "due_date", "payment_terms_days", "currency",
          "net_total", "tax_total", "gross_total", "po_numbers", "referenced_invoice_number", "lines", "notes")


# --------------------------------------------------------------------------------------------
# Safe parsing
# --------------------------------------------------------------------------------------------


def _forbid_dtd(*_args: Any) -> None:
    raise ValueError("XML with a DOCTYPE or entity declarations is not accepted")


def _check_no_dtd(data: bytes) -> None:
    """Scan with expat (any encoding it supports) and refuse a DOCTYPE or entity declaration anywhere.
    Without a DTD only the five predefined entities exist, so nothing can expand or be fetched."""
    scanner = expat.ParserCreate()
    scanner.StartDoctypeDeclHandler = _forbid_dtd
    scanner.EntityDeclHandler = _forbid_dtd
    scanner.UnparsedEntityDeclHandler = _forbid_dtd
    scanner.ExternalEntityRefHandler = _forbid_dtd
    try:
        scanner.Parse(data, True)
    except expat.ExpatError as exc:
        raise ValueError(f"not well-formed XML: {exc}") from None
    except (LookupError, UnicodeError) as exc:  # e.g. encoding="x-foo" or "rot13": expat asks the codec registry
        raise ValueError(f"unsupported XML encoding: {exc}") from None


def _parse_root(data: bytes) -> ET.Element:
    """The root element of a UBL Invoice or CreditNote. Raises ValueError for anything else."""
    if not isinstance(data, (bytes, bytearray)) or not data.strip():
        raise ValueError("empty document: expected UBL XML bytes")
    _check_no_dtd(bytes(data))
    try:
        root = ET.fromstring(bytes(data))
    except ET.ParseError as exc:
        raise ValueError(f"not well-formed XML: {exc}") from None
    except (LookupError, UnicodeError) as exc:
        raise ValueError(f"unsupported XML encoding: {exc}") from None
    if root.tag not in _ROOTS:
        raise ValueError(f"not a UBL Invoice or CreditNote (root element {root.tag})")
    return root


def is_ubl(data: bytes) -> bool:
    """True if the bytes are a (safe, well-formed) UBL Invoice or CreditNote. Never raises: it classifies
    untrusted mail attachments, and one odd file must not fail the webhook or the poller's whole message."""
    try:
        _parse_root(data)
    except Exception:  # noqa: BLE001  ValueError, and anything else a hostile document could provoke
        return False
    return True


# --------------------------------------------------------------------------------------------
# Reading values
# --------------------------------------------------------------------------------------------


def _text(element: Optional[ET.Element], path: str) -> Optional[str]:
    """Stripped text at path, None when the element is missing or empty."""
    text = element.findtext(path, namespaces=NS) if element is not None else None
    return (text or "").strip() or None


# xs:decimal, the lexical form of UBL amounts and quantities: no exponent, no NaN / INF, ASCII digits only.
_XS_DECIMAL = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)")


def _float(text: Optional[str]) -> Optional[float]:
    """The number, or None (treated as missing) when the text is not an xs:decimal or overflows a float.
    float() alone takes "NaN", "Infinity" or "1e999", which would reach the ledger with confidence 1.0."""
    if not text or not _XS_DECIMAL.fullmatch(text):
        return None
    value = float(text)
    return value if math.isfinite(value) else None  # a 400-digit amount overflows to inf


def _number(element: Optional[ET.Element], path: str) -> Optional[float]:
    return _float(_text(element, path))


def _signed(value: Optional[float], sign: float) -> Optional[float]:
    return None if value is None else value * sign + 0.0  # + 0.0 turns -0.0 into 0.0


def _iso_date(element: Optional[ET.Element], path: str) -> Optional[str]:
    text = _text(element, path)
    try:
        return date.fromisoformat(text).isoformat() if text else None
    except ValueError:
        return None


def _compact(value: Optional[str]) -> Optional[str]:
    return value.replace(" ", "") if value else value


_IBAN_LIKE = re.compile(r"[A-Z]{2}\d{2}[A-Z0-9 ]{8,}")
_TERMS_DAYS = re.compile(r"(\d{1,3})\s*(?:days?|tagen?|jours?|d[ií]as|dagen)\b", re.IGNORECASE)
_TERMS_CLAUSE = re.compile(r"[,;.]")
_CASH_DISCOUNT = re.compile(r"skonto|discount|escompte|descuento|korting", re.IGNORECASE)
_NET = re.compile(r"\bnet(?:to|o)?\b", re.IGNORECASE)  # net (EN, FR), netto (DE, NL), neto (ES)


def _tax_id(party: Optional[ET.Element]) -> Optional[str]:
    """CompanyID of the party's VAT registration; else of its first tax scheme (e.g. a US EIN)."""
    if party is None:
        return None
    schemes = party.findall("cac:PartyTaxScheme", NS)
    vat = [s for s in schemes if (_text(s, "cac:TaxScheme/cbc:ID") or "").upper() == "VAT"]
    for scheme in vat + schemes:
        company_id = _text(scheme, "cbc:CompanyID")
        if company_id:
            return _compact(company_id)
    return None


def _party_name(party: Optional[ET.Element]) -> Optional[str]:
    return _text(party, "cac:PartyLegalEntity/cbc:RegistrationName") or _text(party, "cac:PartyName/cbc:Name")


def _iban(root: ET.Element) -> Optional[str]:
    account = _text(root, "cac:PaymentMeans/cac:PayeeFinancialAccount/cbc:ID")
    return _compact(account) if account and _IBAN_LIKE.fullmatch(account) else account


def _due_date(root: ET.Element) -> Optional[str]:
    # Invoice: cbc:DueDate. CreditNote (UBL 2.1 has no DueDate): PaymentMeans/PaymentDueDate.
    return _iso_date(root, "cbc:DueDate") or _iso_date(root, "cac:PaymentMeans/cbc:PaymentDueDate")


def _terms_days(root: ET.Element, issue: Optional[str], due: Optional[str]) -> Optional[int]:
    """DueDate - IssueDate (BT-9 is the authoritative due date). Only when a date is missing (or the due date is
    before the issue date) is the payment terms note read, clause by clause (split on , ; .): a cash-discount
    clause ("2 % Skonto bei Zahlung innerhalb 8 Tagen") is skipped, a net clause ("netto 30 Tage") wins, else the
    first remaining "N days". Never the largest number: a late-interest clause ("interest after 60 days") would."""
    if issue and due:
        days = (date.fromisoformat(due) - date.fromisoformat(issue)).days
        if days >= 0:
            return days
    first: Optional[int] = None
    for note in root.findall("cac:PaymentTerms/cbc:Note", NS):
        for clause in _TERMS_CLAUSE.split(note.text or ""):
            match = _TERMS_DAYS.search(clause)
            if match is None or _CASH_DISCOUNT.search(clause):
                continue
            if _NET.search(clause):
                return int(match.group(1))
            if first is None:
                first = int(match.group(1))
    return first


def _tax_total(root: ET.Element, currency: Optional[str]) -> Optional[float]:
    """TaxTotal/TaxAmount in the document currency (a second TaxTotal may be in the tax currency)."""
    amounts = root.findall("cac:TaxTotal/cbc:TaxAmount", NS)
    preferred = [a for a in amounts if currency is None or a.get("currencyID") in (None, currency)]
    first = (preferred or amounts or [None])[0]
    return _float((first.text or "").strip()) if first is not None else None


def _po_numbers(root: ET.Element, line_tag: str) -> list[str]:
    """Document-level OrderReference/ID, then any line-level order references, without duplicates."""
    found = [_text(root, "cac:OrderReference/cbc:ID")]
    found += [_text(line, "cac:OrderLineReference/cac:OrderReference/cbc:ID")
              for line in root.findall(f"cac:{line_tag}", NS)]
    return list(dict.fromkeys(po for po in found if po))


def _line(line: ET.Element, quantity_tag: str, sign: float) -> dict[str, Any]:
    price = _number(line, "cac:Price/cbc:PriceAmount")
    base = _number(line, "cac:Price/cbc:BaseQuantity")
    if price is not None and base not in (None, 0.0, 1.0):
        price = round(price / base, 6)  # the price is per base quantity (e.g. per 100 units)
    return {
        "description": _text(line, "cac:Item/cbc:Name") or _text(line, "cac:Item/cbc:Description"),
        "quantity": _number(line, f"cbc:{quantity_tag}"),
        "unit_price": _signed(price, sign),
        "amount": _signed(_number(line, "cbc:LineExtensionAmount"), sign),
    }


def _field(value: Any) -> dict[str, Any]:
    """Same rule as extract._postprocess: an empty value is null with confidence 0."""
    if value is None or value == "" or value == []:
        return {"value": None, "confidence": 0.0}
    return {"value": value, "confidence": 1.0}


def parse_ubl(data: bytes) -> dict[str, Any]:
    """Read a UBL Invoice or CreditNote into the extraction schema (dict of {value, confidence}).

    Raises ValueError for malformed XML, XML with a DOCTYPE / entities, or XML that is not UBL.
    """
    root = _parse_root(data)
    doc_type, line_tag, quantity_tag = _ROOTS[root.tag]
    sign = -1.0 if doc_type == "credit_note" else 1.0
    supplier = root.find("cac:AccountingSupplierParty/cac:Party", NS)
    customer = root.find("cac:AccountingCustomerParty/cac:Party", NS)
    currency = _text(root, "cbc:DocumentCurrencyCode")
    issue, due = _iso_date(root, "cbc:IssueDate"), _due_date(root)
    values = {
        "doc_type": doc_type,
        "supplier_name": _party_name(supplier),
        "supplier_vat_id": _tax_id(supplier),
        "supplier_iban": _iban(root),
        "supplier_country": _text(supplier, "cac:PostalAddress/cac:Country/cbc:IdentificationCode"),
        "bill_to_name": _party_name(customer),
        "bill_to_vat_id": _tax_id(customer),
        "invoice_number": _text(root, "cbc:ID"),
        "invoice_date": issue,
        "due_date": due,
        "payment_terms_days": _terms_days(root, issue, due),
        "currency": currency,
        "net_total": _signed(_number(root, "cac:LegalMonetaryTotal/cbc:TaxExclusiveAmount"), sign),
        "tax_total": _signed(_tax_total(root, currency), sign),
        "gross_total": _signed(_number(root, "cac:LegalMonetaryTotal/cbc:TaxInclusiveAmount"), sign),
        "po_numbers": _po_numbers(root, line_tag),
        "referenced_invoice_number": _text(root, "cac:BillingReference/cac:InvoiceDocumentReference/cbc:ID"),
        "lines": [_line(line, quantity_tag, sign) for line in root.findall(f"cac:{line_tag}", NS)],
        "notes": UBL_NOTES,
    }
    return {name: _field(values[name]) for name in FIELDS}


# --------------------------------------------------------------------------------------------
# Rendering a DocumentSpec as Peppol BIS Billing 3.0 (elements in UBL 2.1 schema order)
# --------------------------------------------------------------------------------------------

# Peppol electronic address scheme (EAS) of a VAT number, per country; other parties use an email address.
_EAS_VAT = {"DE": "9930", "FR": "9957", "NL": "9944", "ES": "9920", "GB": "9932", "CH": "9927"}
_POSTAL_FIRST = re.compile(r"^(\d{4,5}(?: [A-Z]{2})?) (.+)$")  # "20457 Hamburg", "1016 DV Amsterdam"
_POSTAL_UK = re.compile(r"^(.+?) ([A-Z]{1,2}\d[A-Z\d]? \d[A-Z]{2})$")  # "Manchester M1 3HE"
_POSTAL_US = re.compile(r"^(.+), ([A-Z]{2}) (\d{5})$")  # "New York, NY 10003"


def _el(parent: ET.Element, tag: str, text: Optional[str] = None, **attrs: str) -> ET.Element:
    """Child element. Tags carry their final prefix ("cbc:ID") and the root declares the namespaces: this tree
    is only serialised, and ElementTree cannot write a default namespace next to unqualified attributes."""
    element = ET.SubElement(parent, tag, attrs)
    if text is not None:
        element.text = text
    return element


def _money(parent: ET.Element, tag: str, amount: float, currency: str) -> None:
    _el(parent, tag, f"{amount + 0.0:.2f}", currencyID=currency)


def _decimal(value: float) -> str:
    """Quantities and unit prices: no trailing zeros beyond two decimals ("10000", "3.50", "0.125")."""
    if float(value).is_integer():
        return str(int(value))
    text = f"{value:.6f}".rstrip("0")
    return text if len(text.split(".")[1]) >= 2 else f"{value:.2f}"


def _address(parent: ET.Element, lines: tuple[str, ...], country: str) -> None:
    """PostalAddress from the printed lines: street, "<postcode> <city>" (or UK / US order), country."""
    address = _el(parent, "cac:PostalAddress")
    street, city_line = (lines[0] if lines else None), (lines[1] if len(lines) > 1 else "")
    city, postal, region = city_line or None, None, None
    if match := _POSTAL_US.match(city_line):
        city, region, postal = match.groups()
    elif match := _POSTAL_FIRST.match(city_line):
        postal, city = match.groups()
    elif match := _POSTAL_UK.match(city_line):
        city, postal = match.groups()
    for tag, value in (("cbc:StreetName", street), ("cbc:CityName", city), ("cbc:PostalZone", postal),
                       ("cbc:CountrySubentity", region)):
        if value:
            _el(address, tag, value)
    _el(_el(address, "cac:Country"), "cbc:IdentificationCode", country)


def _endpoint(parent: ET.Element, country: str, tax_id: Optional[str], email: str) -> None:
    scheme = _EAS_VAT.get(country)
    if scheme and tax_id:
        _el(parent, "cbc:EndpointID", tax_id, schemeID=scheme)
    else:
        _el(parent, "cbc:EndpointID", email, schemeID="EM")


def _tax_scheme(parent: ET.Element, company_id: str, country: str) -> None:
    scheme = _el(parent, "cac:PartyTaxScheme")
    _el(scheme, "cbc:CompanyID", company_id)
    _el(_el(scheme, "cac:TaxScheme"), "cbc:ID", "TAX" if country == "US" else "VAT")  # a US EIN is not a VAT ID


def _supplier(root: ET.Element, spec: world.DocumentSpec) -> None:
    party_spec = spec.party
    party = _el(_el(root, "cac:AccountingSupplierParty"), "cac:Party")
    _endpoint(party, party_spec.country, party_spec.vat_id, spec.sender_email)
    _el(_el(party, "cac:PartyName"), "cbc:Name", spec.printed_supplier_name)
    _address(party, party_spec.address, party_spec.country)
    _tax_scheme(party, party_spec.vat_id, party_spec.country)
    _el(_el(party, "cac:PartyLegalEntity"), "cbc:RegistrationName", spec.printed_supplier_name)


def _customer(root: ET.Element, spec: world.DocumentSpec) -> None:
    entity = spec.bill_to
    party = _el(_el(root, "cac:AccountingCustomerParty"), "cac:Party")
    _endpoint(party, entity.country, entity.vat_id, world.AP_MAILBOX)
    _el(_el(party, "cac:PartyName"), "cbc:Name", entity.name)
    _address(party, spec.bill_to_address or entity.address, entity.country)
    if spec.print_bill_to_vat:
        _tax_scheme(party, entity.vat_id, entity.country)
    _el(_el(party, "cac:PartyLegalEntity"), "cbc:RegistrationName", entity.name)
    if spec.bill_to_attention:
        _el(_el(party, "cac:Contact"), "cbc:Name", spec.bill_to_attention)


def _tax_category(spec: world.DocumentSpec) -> tuple[str, Optional[float], Optional[str]]:
    """(UNCL5305 category, percent or None, exemption reason) from the rate and the printed legal mention."""
    if spec.tax_rate > 0:
        return "S", round(spec.tax_rate * 100, 2), None
    mention = f"{spec.tax_note or ''} {spec.tax_label}".lower()
    reason = spec.tax_note or spec.tax_label
    if any(key in mention for key in ("reverse charge", "autoliquidation", "article 196", "steuerschuldnerschaft")):
        return "AE", 0.0, reason
    if any(key in mention for key in ("intra-community", "intra-eu", "intracomunitaria", "article 138")):
        return "K", 0.0, reason
    if "export" in mention:
        return "G", 0.0, reason
    if "sales tax" in mention or spec.party.country == "US":
        return "O", None, reason  # outside the scope of VAT: no rate
    return "Z", 0.0, None


def _category(parent: ET.Element, tag: str, spec: world.DocumentSpec, with_reason: bool) -> None:
    code, percent, reason = _tax_category(spec)
    category = _el(parent, tag)
    _el(category, "cbc:ID", code)
    if percent is not None:
        _el(category, "cbc:Percent", _decimal(percent))
    if with_reason and reason:
        _el(category, "cbc:TaxExemptionReason", reason)
    _el(_el(category, "cac:TaxScheme"), "cbc:ID", "VAT")


def _payment(root: ET.Element, spec: world.DocumentSpec, credit: bool) -> None:
    bank = spec.party.bank
    means = _el(root, "cac:PaymentMeans")
    sepa = spec.currency == "EUR" and bool(_IBAN_LIKE.fullmatch(bank))
    _el(means, "cbc:PaymentMeansCode", "58" if sepa else "30")  # SEPA credit transfer / credit transfer
    if credit and spec.due_date:
        _el(means, "cbc:PaymentDueDate", spec.due_date.isoformat())
    _el(means, "cbc:PaymentID", spec.invoice_number)
    account = _el(means, "cac:PayeeFinancialAccount")
    _el(account, "cbc:ID", bank)
    _el(account, "cbc:Name", spec.party.canonical_name)
    if spec.payment_terms_days is not None:
        _el(_el(root, "cac:PaymentTerms"), "cbc:Note", f"{spec.payment_terms_days} days net")


def _header(root: ET.Element, spec: world.DocumentSpec, credit: bool) -> None:
    _el(root, "cbc:CustomizationID", CUSTOMIZATION_ID)
    _el(root, "cbc:ProfileID", PROFILE_ID)
    _el(root, "cbc:ID", spec.invoice_number)
    _el(root, "cbc:IssueDate", spec.invoice_date.isoformat())
    if not credit and spec.due_date:
        _el(root, "cbc:DueDate", spec.due_date.isoformat())
    _el(root, "cbc:CreditNoteTypeCode" if credit else "cbc:InvoiceTypeCode", "381" if credit else "380")
    if spec.printed_notes:
        _el(root, "cbc:Note", " ".join(spec.printed_notes))
    _el(root, "cbc:DocumentCurrencyCode", spec.currency)
    if not spec.po_numbers:  # Peppol needs a buyer reference or an order reference
        _el(root, "cbc:BuyerReference", spec.bill_to_attention or spec.bill_to_entity)
    if spec.po_numbers:
        _el(_el(root, "cac:OrderReference"), "cbc:ID", spec.po_numbers[0])
    if spec.referenced_invoice_number:
        _el(_el(_el(root, "cac:BillingReference"), "cac:InvoiceDocumentReference"), "cbc:ID",
            spec.referenced_invoice_number)
    if spec.contract_reference:
        _el(_el(root, "cac:ContractDocumentReference"), "cbc:ID", spec.contract_reference)


def _totals(root: ET.Element, spec: world.DocumentSpec, sign: float) -> None:
    net, tax, gross = spec.net_total * sign, spec.tax_total * sign, spec.gross_total * sign
    tax_total = _el(root, "cac:TaxTotal")
    _money(tax_total, "cbc:TaxAmount", tax, spec.currency)
    subtotal = _el(tax_total, "cac:TaxSubtotal")
    _money(subtotal, "cbc:TaxableAmount", net, spec.currency)
    _money(subtotal, "cbc:TaxAmount", tax, spec.currency)
    _category(subtotal, "cac:TaxCategory", spec, with_reason=True)
    monetary = _el(root, "cac:LegalMonetaryTotal")
    for tag, amount in (("LineExtensionAmount", net), ("TaxExclusiveAmount", net),
                        ("TaxInclusiveAmount", gross), ("PayableAmount", gross)):
        _money(monetary, f"cbc:{tag}", amount, spec.currency)


def _lines(root: ET.Element, spec: world.DocumentSpec, credit: bool, sign: float) -> None:
    for no, spec_line in enumerate(spec.lines, start=1):
        line = _el(root, "cac:CreditNoteLine" if credit else "cac:InvoiceLine")
        _el(line, "cbc:ID", str(no))
        _el(line, "cbc:CreditedQuantity" if credit else "cbc:InvoicedQuantity", _decimal(spec_line.quantity),
            unitCode="C62")
        _money(line, "cbc:LineExtensionAmount", spec_line.amount * sign, spec.currency)
        item = _el(line, "cac:Item")
        _el(item, "cbc:Name", spec_line.description)
        _category(item, "cac:ClassifiedTaxCategory", spec, with_reason=False)
        _el(_el(line, "cac:Price"), "cbc:PriceAmount", _decimal(spec_line.unit_price * sign + 0.0),
            currencyID=spec.currency)


def render_ubl(spec: world.DocumentSpec) -> bytes:
    """A Peppol BIS Billing 3.0 Invoice (or CreditNote for doc_type credit_note) as UTF-8 bytes.

    Credit-note amounts are written positive, as UBL expects (the document type carries the sign).
    Deterministic: the same spec always gives the same bytes.
    """
    credit = spec.doc_type == "credit_note"
    sign = -1.0 if credit else 1.0
    root = ET.Element("CreditNote" if credit else "Invoice",
                      {"xmlns": CREDIT_NOTE_NS if credit else INVOICE_NS, "xmlns:cac": CAC_NS, "xmlns:cbc": CBC_NS})
    _header(root, spec, credit)
    _supplier(root, spec)
    _customer(root, spec)
    _payment(root, spec, credit)
    _totals(root, spec, sign)
    _lines(root, spec, credit, sign)
    ET.indent(root, space="  ")
    body = ET.tostring(root, encoding="unicode")
    return f'<?xml version="1.0" encoding="UTF-8"?>\n{body}\n'.encode("utf-8")
