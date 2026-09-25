"""Render the sample documents as realistic files.

v1: world.DOCUMENTS (brief 5.6 plus 2 clean ones) as one-page A4 PDFs in data/invoices/.
v2: world_v2.DOCUMENTS_V2 (docs/TEST_SET_V2.md) in data/invoices_v2/: native PDFs in four languages, a supplier
    statement, a KOPIE copy, two scanned-image PDFs, one Peppol UBL XML (app.ubl) and one email body (.txt).

Four supplier templates, keyed by spec.layout:
  classic  Times fonts, logo-box letterhead top-left, boxed meta table top-right
  banner   full-width colour banner with the supplier name, Helvetica
  modern   accent side bar, right-aligned meta block, Helvetica
  compact  small-business style: small fonts, simple black rules
Labels follow spec.language (en, de, fr, es); numbers and dates follow the supplier's country conventions, so the
extraction has to cope with both. A statement (doc_type "other") lists open items and a balance: no VAT block and
no payment request.
Scans (spec.scan): the native page is rasterised with pypdfium2 at 300 dpi, rotated by the skew, degraded with
noise seeded by the document number, and embedded as a JPEG, the only content of the page: no text layer.
Output is byte-identical across runs (reportlab invariant mode, no timestamps, seeded randomness; scans on the
same pypdfium2 / Pillow versions). Phone and company-registration numbers are fake and derived from the supplier
number.

CLI:  python -m app.invoices_gen          (v1: writes data/invoices/*.pdf)
      python -m app.invoices_gen --v2     (v2: writes data/invoices_v2/)
      python -m app.invoices_gen --all    (both)
"""
from __future__ import annotations

import argparse
import hashlib
import io
import random
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO, Optional, Union

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader, simpleSplit
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen.canvas import Canvas

from app import config
from app.world import DOCUMENTS, DocumentSpec, PartySpec, documents_for, format_iban

if TYPE_CHECKING:
    from PIL.Image import Image as PILImage

PAGE_W, PAGE_H = A4
INK = colors.HexColor("#222222")
MUTED = colors.HexColor("#5F5F5F")
RULE = colors.HexColor("#BDBDBD")
LIGHT = colors.HexColor("#EFEFEF")
WHITE = colors.white
FOOTER_Y = 36  # baseline of the footer line
CONTENT_BOTTOM = 70  # nothing but the footer may go below this


# --------------------------------------------------------------------------------------------
# Labels per document language. "en" reproduces the v1 documents exactly.
# --------------------------------------------------------------------------------------------

LABELS: dict[str, dict[str, str]] = {
    "en": {
        "tel": "Tel.", "vat": "VAT ID:", "attn": "Attn:",
        "no_invoice": "Invoice no.", "no_credit_note": "Credit note no.", "no_other": "Statement no.",
        "date_invoice": "Invoice date", "date_credit_note": "Credit note date", "date_other": "Statement date",
        "due": "Due date", "terms": "Payment terms", "terms_value": "{days} days net",
        "po": "Your PO", "contract": "Contract ref.", "relates": "Relates to invoice",
        "bill_to": "Bill to", "billed_to": "Billed to", "invoice_to": "Invoice to:", "credit_to": "Credit to:",
        "customer": "Customer", "customer_to": "Customer:",
        "col_no": "#", "col_desc": "Description", "col_qty": "Qty", "col_price": "Unit price",
        "col_amount": "Amount ({currency})", "col_item": "Open item",
        "net": "Net amount", "total_due": "Total due", "total_credit": "Total credit", "balance": "Balance",
        "pay_title": "Payment details", "pay_request": "Please transfer {amount} by {date} to:",
        "pay_request_total": "Please transfer the total due to:", "holder": "Account holder: {name}",
        "bank": "Bank: {bank}", "pay_ref": "Payment reference: {ref}",
        "settle_title": "Settlement", "settle_text": "Amount will be credited to your account.",
        "settle_ref": "Reference: {ref}", "our_bank": "Our bank details: {bank} · {account}",
        "bank_title": "Bank details", "office": "Registered office: {city}", "page": "Page 1 of 1",
    },
    "de": {
        "tel": "Tel.", "vat": "USt-IdNr.", "attn": "z. Hd.",
        "no_invoice": "Rechnungsnr.", "no_credit_note": "Gutschrift Nr.", "no_other": "Auszug Nr.",
        "date_invoice": "Rechnungsdatum", "date_credit_note": "Gutschriftsdatum", "date_other": "Auszugsdatum",
        "due": "Fällig am", "terms": "Zahlungsbedingungen", "terms_value": "{days} Tage netto",
        "po": "Ihre Bestellnr.", "contract": "Vertragsnr.", "relates": "Zu Rechnung",
        "bill_to": "Rechnungsempfänger", "billed_to": "Rechnungsempfänger", "invoice_to": "Rechnung an:",
        "credit_to": "Gutschrift an:", "customer": "Kunde", "customer_to": "Kunde:",
        "col_no": "Pos.", "col_desc": "Bezeichnung", "col_qty": "Menge", "col_price": "Einzelpreis",
        "col_amount": "Betrag ({currency})", "col_item": "Offener Posten",
        "net": "Nettobetrag", "total_due": "Rechnungsbetrag", "total_credit": "Gutschriftsbetrag",
        "balance": "Offener Saldo",
        "pay_title": "Zahlung", "pay_request": "Bitte überweisen Sie {amount} bis zum {date} auf folgendes Konto:",
        "pay_request_total": "Bitte überweisen Sie den Rechnungsbetrag auf folgendes Konto:",
        "holder": "Kontoinhaber: {name}", "bank": "Bank: {bank}", "pay_ref": "Verwendungszweck: {ref}",
        "settle_title": "Verrechnung", "settle_text": "Der Betrag wird Ihrem Kundenkonto gutgeschrieben.",
        "settle_ref": "Referenz: {ref}", "our_bank": "Unsere Bankverbindung: {bank} · {account}",
        "bank_title": "Bankverbindung", "office": "Sitz: {city}", "page": "Seite 1 von 1",
    },
    "fr": {
        "tel": "Tél.", "vat": "N° TVA :", "attn": "À l'attention de :",
        "no_invoice": "Facture n°", "no_credit_note": "Avoir n°", "no_other": "Relevé n°",
        "date_invoice": "Date de facture", "date_credit_note": "Date de l'avoir", "date_other": "Date du relevé",
        "due": "Échéance", "terms": "Conditions de paiement", "terms_value": "{days} jours net",
        "po": "Votre commande n°", "contract": "Réf. contrat", "relates": "Facture d'origine",
        "bill_to": "Facturé à", "billed_to": "Facturé à", "invoice_to": "Facturé à :", "credit_to": "Client :",
        "customer": "Client", "customer_to": "Client :",
        "col_no": "#", "col_desc": "Désignation", "col_qty": "Qté", "col_price": "PU HT",
        "col_amount": "Montant ({currency})", "col_item": "Facture ouverte",
        "net": "Total HT", "total_due": "Total TTC", "total_credit": "Total avoir TTC", "balance": "Solde",
        "pay_title": "Règlement",
        "pay_request": "Merci de régler {amount} avant le {date} par virement sur le compte :",
        "pay_request_total": "Merci de régler le total par virement sur le compte :",
        "holder": "Titulaire du compte : {name}", "bank": "Banque : {bank}",
        "pay_ref": "Référence de paiement : {ref}",
        "settle_title": "Règlement", "settle_text": "Le montant sera porté au crédit de votre compte.",
        "settle_ref": "Référence : {ref}", "our_bank": "Nos coordonnées bancaires : {bank} · {account}",
        "bank_title": "Coordonnées bancaires", "office": "Siège social : {city}", "page": "Page 1 sur 1",
    },
    "es": {
        "tel": "Tel.", "vat": "NIF-IVA:", "attn": "A la atención de:",
        "no_invoice": "Factura n.º", "no_credit_note": "Abono n.º", "no_other": "Extracto n.º",
        "date_invoice": "Fecha de factura", "date_credit_note": "Fecha del abono", "date_other": "Fecha del extracto",
        "due": "Vencimiento", "terms": "Condiciones de pago", "terms_value": "{days} días netos",
        "po": "Su pedido n.º", "contract": "Ref. contrato", "relates": "Factura rectificada",
        "bill_to": "Facturar a", "billed_to": "Facturar a", "invoice_to": "Facturar a:", "credit_to": "Cliente:",
        "customer": "Cliente", "customer_to": "Cliente:",
        "col_no": "#", "col_desc": "Descripción", "col_qty": "Cant.", "col_price": "Precio",
        "col_amount": "Importe ({currency})", "col_item": "Partida abierta",
        "net": "Base imponible", "total_due": "Total factura", "total_credit": "Total abono", "balance": "Saldo",
        "pay_title": "Forma de pago", "pay_request": "Rogamos transfieran {amount} antes del {date} a la cuenta:",
        "pay_request_total": "Rogamos transfieran el total a la cuenta:",
        "holder": "Titular: {name}", "bank": "Banco: {bank}", "pay_ref": "Referencia: {ref}",
        "settle_title": "Liquidación", "settle_text": "El importe se abonará en su cuenta.",
        "settle_ref": "Referencia: {ref}", "our_bank": "Nuestros datos bancarios: {bank} · {account}",
        "bank_title": "Datos bancarios", "office": "Domicilio social: {city}", "page": "Página 1 de 1",
    },
}

# Country names in printed addresses (the world stores them in English).
COUNTRY_NAMES: dict[str, dict[str, str]] = {
    "de": {"Germany": "Deutschland", "France": "Frankreich", "Netherlands": "Niederlande", "Spain": "Spanien",
           "United Kingdom": "Vereinigtes Königreich", "Switzerland": "Schweiz"},
    "fr": {"Germany": "Allemagne", "Netherlands": "Pays-Bas", "Spain": "Espagne", "United Kingdom": "Royaume-Uni",
           "Switzerland": "Suisse", "USA": "États-Unis"},
    "es": {"Germany": "Alemania", "France": "Francia", "Netherlands": "Países Bajos", "Spain": "España",
           "United Kingdom": "Reino Unido", "Switzerland": "Suiza", "USA": "Estados Unidos"},
}


def labels(spec: DocumentSpec) -> dict[str, str]:
    return LABELS[spec.language]


def localise_address(lines: tuple[str, ...], language: str) -> list[str]:
    """Address lines with the country (last line) in the document language."""
    names = COUNTRY_NAMES.get(language, {})
    return [names.get(line, line) if i == len(lines) - 1 else line for i, line in enumerate(lines)]


# --------------------------------------------------------------------------------------------
# Locale formatting (pure functions)
# --------------------------------------------------------------------------------------------

_DECIMAL_COMMA = frozenset({"DE", "NL", "ES", "FR"})  # 23.400,00; every other country 23,400.00
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_VAT_GROUPS = {"DE": (3, 3, 3), "FR": (2, 3, 3, 3), "GB": (3, 4, 2), "NL": (4, 4, 4)}
_CALLING_CODES = {"DE": "49", "FR": "33", "GB": "44", "NL": "31", "ES": "34", "CH": "41", "US": "1"}
_US_BANK = re.compile(r"ABA (\d+) ACCT (\d+)")


def _swap_separators(text: str, country: str) -> str:
    return text.translate(str.maketrans(",.", ".,")) if country in _DECIMAL_COMMA else text


def fmt_amount(value: float, country: str) -> str:
    """Two decimals with the supplier's separators: DE '23.400,00', GB/US '23,400.00'; '-' for negatives."""
    text = _swap_separators(f"{abs(value):,.2f}", country)
    return f"-{text}" if value < 0 else text


def fmt_quantity(value: float, country: str) -> str:
    """No decimals when whole ('150', DE '10.000', GB '10,000'), otherwise two."""
    if value == int(value):
        return _swap_separators(f"{int(value):,}", country)
    return fmt_amount(value, country)


def fmt_money(value: float, currency: str, country: str) -> str:
    """Amount with currency code: '23.400,00 EUR' (continental) or 'USD 9,600.00' (GB/US style)."""
    if country in _DECIMAL_COMMA:
        return f"{fmt_amount(value, country)} {currency}"
    return f"{currency} {fmt_amount(value, country)}"


def fmt_date(d: date, country: str) -> str:
    """DE/NL/CH '30.09.2026', FR/ES '30/09/2026', GB '30 Sep 2026', US 'Oct 1, 2026'."""
    if country == "GB":
        return f"{d.day} {_MONTHS[d.month - 1]} {d.year}"
    if country == "US":
        return f"{_MONTHS[d.month - 1]} {d.day}, {d.year}"
    if country in ("FR", "ES"):
        return f"{d.day:02d}/{d.month:02d}/{d.year}"
    return f"{d.day:02d}.{d.month:02d}.{d.year}"


def fmt_vat(vat_id: str, country: str) -> str:
    """Human format: 'DE 281 947 305', 'NL 8593 7461 2B01', 'ES B86419273', 'CHE-419.287.563 MWST', 'EIN 47-3829105'."""
    if country == "US":
        return f"EIN {vat_id}"
    if country == "CH":
        return f"{vat_id} MWST"
    parts, body = [vat_id[:2]], vat_id[2:]
    for size in _VAT_GROUPS.get(country, (len(body),)):
        parts.append(body[:size])
        body = body[size:]
    return " ".join(parts + ([body] if body else []))


def vat_line(vat_id: str, country: str, language: str = "en", display: Optional[str] = None) -> str:
    """'VAT ID: DE 281 947 305' / 'USt-IdNr. DE281947305' (display: the ID exactly as printed); US: 'EIN ...'."""
    shown = display or fmt_vat(vat_id, country)
    return shown if country == "US" else f"{LABELS[language]['vat']} {shown}"


def fmt_bank_account(bank: str) -> str:
    """'IBAN DE12 5001 ...' or, for US suppliers, 'ABA routing 121000248 · Account 4839201756'."""
    match = _US_BANK.fullmatch(bank)
    if match:
        return f"ABA routing {match[1]} · Account {match[2]}"
    return f"IBAN {format_iban(bank)}"


def city_of(party: PartySpec) -> str:
    """City from the second address line: '20457 Hamburg', 'Manchester M1 3HE', 'Newark, NJ 07114'."""
    line = party.address[1]
    if "," in line:
        return line.split(",")[0]
    tokens = line.split()
    if party.country == "GB":
        return " ".join(tokens[:-2])
    if party.country == "NL":
        return " ".join(tokens[2:])
    return " ".join(tokens[1:])


def fake_phone(party: PartySpec) -> str:
    """Deterministic fictional phone number derived from the supplier number."""
    n = party.no
    if party.country == "US":
        return f"+1 555 {100 + n * 37} {1000 + n * 613}"
    return f"+{_CALLING_CODES[party.country]} {20 + n} {n * 397 % 900 + 100} {n * 7919 % 9000 + 1000}"


def fake_registration(party: PartySpec, language: str = "en") -> str:
    """Deterministic fictional company-register entry in the supplier country's usual form (and language)."""
    city, num = city_of(party), (party.no * 7331 + 2027) % 900000 + 100000
    if party.country == "FR":  # SIREN = French VAT ID without the country code and key
        siren = party.vat_id[4:]
        return f"RCS {city} {siren[:3]} {siren[3:6]} {siren[6:]}"
    local = {
        ("de", "DE"): f"Amtsgericht {city}, HRB {num}",
        ("es", "ES"): f"Registro Mercantil de {city}, hoja M-{num}",
        ("fr", "NL"): f"Chambre de commerce (KvK) n° {num:08d}",
    }
    if (language, party.country) in local:
        return local[(language, party.country)]
    return {
        "DE": f"Commercial register: {city} local court, HRB {num}",
        "GB": f"Registered in England and Wales, company no. {num:08d}",
        "NL": f"Chamber of Commerce (KvK) no. {num:08d}",
        "ES": f"Mercantile Register of {city}, sheet M-{num}",
        "CH": f"Commercial register of {city}, UID {party.vat_id}",
        "US": f"Corporate file no. {num}",
    }[party.country]


# --------------------------------------------------------------------------------------------
# Document content (pure functions over DocumentSpec)
# --------------------------------------------------------------------------------------------


def is_credit(spec: DocumentSpec) -> bool:
    return spec.doc_type == "credit_note"


def is_statement(spec: DocumentSpec) -> bool:
    return spec.doc_type == "other"


def money(spec: DocumentSpec, value: float) -> str:
    return fmt_money(value, spec.currency, spec.party.country)


def contact_email(spec: DocumentSpec) -> str:
    """The supplier e-mail printed on the document: the sender, unless someone else forwarded it (v2 no. 8)."""
    domain = spec.party.email_domain
    return spec.sender_email if spec.sender_email.endswith(f"@{domain}") else f"accounts@{domain}"


def supplier_address(spec: DocumentSpec) -> list[str]:
    return localise_address(spec.party.address, spec.language)


def supplier_vat_line(spec: DocumentSpec) -> str:
    p = spec.party
    return vat_line(p.vat_id, p.country, spec.language, spec.vat_display)


def supplier_lines(spec: DocumentSpec) -> list[str]:
    """Address, phone, e-mail and VAT ID of the supplier (the name is printed separately)."""
    return [*supplier_address(spec), f"{labels(spec)['tel']} {fake_phone(spec.party)}", contact_email(spec),
            supplier_vat_line(spec)]


def bill_to_lines(spec: DocumentSpec) -> list[str]:
    entity, L = spec.bill_to, labels(spec)
    lines = [entity.name]
    if spec.bill_to_attention:
        lines.append(f"{L['attn']} {spec.bill_to_attention}")
    lines += localise_address(spec.bill_to_address or entity.address, spec.language)
    if spec.print_bill_to_vat:
        lines.append(vat_line(entity.vat_id, entity.country, spec.language))
    return lines


def bill_to_title(spec: DocumentSpec, layout: str) -> str:
    """Title of the bill-to block: 'Bill to' (classic), 'BILL TO' (banner), 'BILLED TO' (modern), 'Invoice to:'."""
    L = labels(spec)
    if is_statement(spec):
        return {"classic": L["customer"], "compact": L["customer_to"]}.get(layout, L["customer"].upper())
    if layout == "classic":
        return L["bill_to"]
    if layout == "banner":
        return L["bill_to"].upper()
    if layout == "modern":
        return L["billed_to"].upper()
    return L["credit_to"] if is_credit(spec) else L["invoice_to"]


def meta_rows(spec: DocumentSpec) -> list[tuple[str, str]]:
    country, L = spec.party.country, labels(spec)
    rows = [(L[f"no_{spec.doc_type}"], spec.invoice_number),
            (L[f"date_{spec.doc_type}"], fmt_date(spec.invoice_date, country))]
    if spec.due_date:
        rows.append((L["due"], fmt_date(spec.due_date, country)))
    if spec.payment_terms_days:
        rows.append((L["terms"], L["terms_value"].format(days=spec.payment_terms_days)))
    rows += [(L["po"], po) for po in spec.po_numbers]
    if spec.contract_reference:
        rows.append((L["contract"], spec.contract_reference))
    if spec.referenced_invoice_number:
        rows.append((L["relates"], spec.referenced_invoice_number))
    return rows


def totals_rows(spec: DocumentSpec) -> list[tuple[str, str]]:
    """Net, tax and total rows; a statement has only its balance (no VAT block)."""
    L = labels(spec)
    if is_statement(spec):
        return [(L["balance"], money(spec, spec.gross_total))]
    return [
        (L["net"], money(spec, spec.net_total)),
        (spec.tax_label, money(spec, spec.tax_total)),
        (L["total_credit"] if is_credit(spec) else L["total_due"], money(spec, spec.gross_total)),
    ]


def payment_block(spec: DocumentSpec) -> tuple[str, list[str]]:
    """Title and lines of the payment block. Credit notes and statements carry no payment instruction."""
    p, L = spec.party, labels(spec)
    if is_credit(spec):
        return L["settle_title"], [
            L["settle_text"],
            L["settle_ref"].format(ref=spec.invoice_number),
            L["our_bank"].format(bank=p.bank_name, account=fmt_bank_account(p.bank)),
        ]
    holder = [L["holder"].format(name=spec.printed_supplier_name), L["bank"].format(bank=p.bank_name),
              fmt_bank_account(p.bank)]
    if is_statement(spec):
        return L["bank_title"], holder
    request = (L["pay_request"].format(amount=money(spec, spec.gross_total), date=fmt_date(spec.due_date, p.country))
               if spec.due_date else L["pay_request_total"])
    return L["pay_title"], [request, *holder, L["pay_ref"].format(ref=spec.invoice_number)]


def footer_line(spec: DocumentSpec) -> str:
    """Registration line; uses the printed supplier name only (v1 docs #2 and #4 print a short name)."""
    p, L = spec.party, labels(spec)
    return (f"{spec.printed_supplier_name} · {L['office'].format(city=city_of(p))} · "
            f"{fake_registration(p, spec.language)}")


def initials(name: str) -> str:
    return "".join(word[0] for word in re.split(r"[\s&]+", name) if word)[:2].upper()


# --------------------------------------------------------------------------------------------
# Drawing primitives
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Look:
    font: str
    bold: str
    italic: str
    size: float  # body text
    small: float  # contact lines, notes, footer
    margin_left: float
    margin_right: float
    table_header: str  # accent_rules | accent_fill | light_fill | ink_rules


LOOKS = {
    "classic": Look("Times-Roman", "Times-Bold", "Times-Italic", 10.5, 9, 56, 56, "accent_rules"),
    "banner": Look("Helvetica", "Helvetica-Bold", "Helvetica-Oblique", 9.5, 8, 48, 48, "accent_fill"),
    "modern": Look("Helvetica", "Helvetica-Bold", "Helvetica-Oblique", 9.5, 8, 64, 50, "light_fill"),
    "compact": Look("Helvetica", "Helvetica-Bold", "Helvetica-Oblique", 8.5, 7.5, 40, 40, "ink_rules"),
}


@dataclass(frozen=True)
class Ctx:
    c: Canvas
    spec: DocumentSpec
    look: Look
    accent: colors.Color

    @property
    def left(self) -> float:
        return self.look.margin_left

    @property
    def right(self) -> float:
        return PAGE_W - self.look.margin_right

    @property
    def width(self) -> float:
        return self.right - self.left

    @property
    def accent_text(self) -> colors.Color:
        """The accent colour, darkened when too light to read as text on white (e.g. yellow)."""
        a = self.accent
        if 0.299 * a.red + 0.587 * a.green + 0.114 * a.blue > 0.6:
            return colors.Color(a.red * 0.65, a.green * 0.65, a.blue * 0.65)
        return a


def check_glyphs(text: str) -> None:
    """The built-in fonts use WinAnsi (cp1252); anything outside it would print as a black box."""
    try:
        text.encode("cp1252")
    except UnicodeEncodeError as exc:
        raise ValueError(f"not printable with the built-in PDF fonts: {text!r}") from exc


def _text(c: Canvas, x: float, y: float, text: str, font: str, size: float,
          colour: colors.Color = INK, align: str = "left") -> None:
    check_glyphs(text)
    c.setFont(font, size)
    c.setFillColor(colour)
    {"left": c.drawString, "right": c.drawRightString, "center": c.drawCentredString}[align](x, y, text)


def _lines(c: Canvas, x: float, y: float, lines: list[str], font: str, size: float,
           colour: colors.Color = INK, align: str = "left", first_font: str | None = None) -> float:
    """Draw lines top-down from baseline y; return the baseline of the next line."""
    for i, line in enumerate(lines):
        _text(c, x, y, line, first_font if (first_font and i == 0) else font, size, colour, align)
        y -= size * 1.35
    return y


def _wrap(lines: list[str], font: str, size: float, width: float) -> list[str]:
    return [part for line in lines for part in (simpleSplit(line, font, size, width) or [""])]


def _fit(text: str, font: str, size: float, width: float) -> float:
    """Largest font size (<= size) at which text fits in width."""
    while size > 6 and stringWidth(text, font, size) > width:
        size -= 0.5
    return size


def _rule(c: Canvas, x0: float, x1: float, y: float, colour: colors.Color, width: float = 0.5) -> None:
    c.setStrokeColor(colour)
    c.setLineWidth(width)
    c.line(x0, y, x1, y)


def _meta(ctx: Ctx, x_label: float, y: float, rows: list[tuple[str, str]], size: float,
          label_align: str = "left") -> float:
    """Label/value rows with values right-aligned at the right margin; returns the next baseline."""
    for label, value in rows:
        _text(ctx.c, x_label, y, label, ctx.look.font, size, MUTED, label_align)
        _text(ctx.c, ctx.right, y, value, ctx.look.bold, size, INK, "right")
        y -= size * 1.5
    return y


def _bill_to(ctx: Ctx, x: float, y: float, title: str, colour: colors.Color) -> float:
    L = ctx.look
    _text(ctx.c, x, y, title, L.bold, L.small, colour)
    return _lines(ctx.c, x, y - L.size * 1.5, bill_to_lines(ctx.spec), L.font, L.size, first_font=L.bold)


# --------------------------------------------------------------------------------------------
# Letterheads: each draws supplier, heading, meta and bill-to; returns the y where the table starts
# --------------------------------------------------------------------------------------------


def _header_classic(ctx: Ctx) -> float:
    c, s, L = ctx.c, ctx.spec, ctx.look
    top, box, meta_w = PAGE_H - 50, 54, 215
    meta_x = ctx.right - meta_w
    c.setFillColor(ctx.accent)
    c.rect(ctx.left, top - box, box, box, stroke=0, fill=1)
    _text(c, ctx.left + box / 2, top - box / 2 - 8, initials(s.printed_supplier_name), L.bold, 23, WHITE, "center")

    x = ctx.left + box + 12
    name_size = _fit(s.printed_supplier_name, L.bold, 16, meta_x - x - 12)
    _text(c, x, top - 13, s.printed_supplier_name, L.bold, name_size, ctx.accent_text)
    contacts = _wrap(supplier_lines(s), L.font, L.small, meta_x - x - 12)
    y_supplier = _lines(c, x, top - 28, contacts, L.font, L.small, MUTED)

    _text(c, ctx.right, top - 16, s.heading, L.bold, _fit(s.heading, L.bold, 22, meta_w), INK, "right")
    rows, row_h, size = meta_rows(s), 15, L.size - 1
    y0 = top - 28
    c.setStrokeColor(RULE)
    c.setLineWidth(0.6)
    c.rect(meta_x, y0 - row_h * len(rows), meta_w, row_h * len(rows), stroke=1, fill=0)
    for i, (label, value) in enumerate(rows):
        base = y0 - row_h * (i + 1)
        if i:
            c.line(meta_x, base + row_h, ctx.right, base + row_h)
        _text(c, meta_x + 6, base + 4.5, label, L.italic, size, MUTED)
        _text(c, ctx.right - 6, base + 4.5, value, L.bold, size, INK, "right")
    y_meta = y0 - row_h * len(rows)

    y = min(y_supplier, y_meta, top - box) - 26
    _rule(c, ctx.left, ctx.left + 230, y - 4, ctx.accent, 0.8)
    return _bill_to(ctx, ctx.left, y, bill_to_title(s, "classic"), ctx.accent_text) - 12


def _header_banner(ctx: Ctx) -> float:
    c, s, L = ctx.c, ctx.spec, ctx.look
    band = 112
    c.setFillColor(ctx.accent)
    c.rect(0, PAGE_H - band, PAGE_W, band, stroke=0, fill=1)
    contacts = supplier_lines(s)
    contact_w = max(stringWidth(line, L.font, L.small) for line in contacts)
    name_size = _fit(s.printed_supplier_name, L.bold, 24, ctx.width - contact_w - 24)
    _text(c, ctx.left, PAGE_H - 60, s.printed_supplier_name, L.bold, name_size, WHITE)
    _text(c, ctx.left, PAGE_H - 78, f"www.{s.party.email_domain}", L.font, L.small, WHITE)
    _lines(c, ctx.right, PAGE_H - 28, contacts, L.font, L.small, WHITE, "right")

    y = PAGE_H - band - 38
    _text(c, ctx.left, y, s.heading, L.bold, _fit(s.heading, L.bold, 18, ctx.width), ctx.accent_text)
    y -= 32
    y_bill = _bill_to(ctx, ctx.left, y, bill_to_title(s, "banner"), MUTED)
    y_meta = _meta(ctx, ctx.right - 220, y, meta_rows(s), L.size)
    return min(y_bill, y_meta) - 10


def _header_modern(ctx: Ctx) -> float:
    c, s, L = ctx.c, ctx.spec, ctx.look
    c.setFillColor(ctx.accent)
    c.rect(0, 0, 18, PAGE_H, stroke=0, fill=1)
    top, meta_w = PAGE_H - 62, 215
    text_w = ctx.width - meta_w - 16
    p = s.party
    _text(c, ctx.left, top, s.printed_supplier_name, L.bold,
          _fit(s.printed_supplier_name, L.bold, 20, text_w), ctx.accent_text)
    contacts = _wrap([", ".join(supplier_address(s)),
                      f"{labels(s)['tel']} {fake_phone(p)} · {contact_email(s)}", supplier_vat_line(s)],
                     L.font, L.small, text_w)
    y_supplier = _lines(c, ctx.left, top - 18, contacts, L.font, L.small, MUTED)

    _text(c, ctx.right, top, s.heading, L.bold, _fit(s.heading, L.bold, 24, meta_w), INK, "right")
    y_meta = _meta(ctx, ctx.right - 105, top - 24, meta_rows(s), L.size - 0.5, label_align="right")

    y = min(y_supplier, y_meta) - 4
    _rule(c, ctx.left, ctx.right, y, ctx.accent, 1.2)
    return _bill_to(ctx, ctx.left, y - 24, bill_to_title(s, "modern"), ctx.accent_text) - 10


def _header_compact(ctx: Ctx) -> float:
    c, s, L = ctx.c, ctx.spec, ctx.look
    p = s.party
    top = PAGE_H - 48
    _text(c, ctx.right, top, s.heading, L.bold, 14, INK, "right")
    heading_w = stringWidth(s.heading, L.bold, 14)
    _text(c, ctx.left, top, s.printed_supplier_name, L.bold,
          _fit(s.printed_supplier_name, L.bold, 14, ctx.width - heading_w - 20), ctx.accent_text)
    contacts = _wrap([" · ".join(supplier_address(s)),
                      f"{labels(s)['tel']} {fake_phone(p)} · {contact_email(s)} · {supplier_vat_line(s)}"],
                     L.font, L.small, ctx.width)
    y = _lines(c, ctx.left, top - 14, contacts, L.font, L.small, MUTED) + 2
    _rule(c, ctx.left, ctx.right, y, INK)

    y -= 18
    y_bill = _bill_to(ctx, ctx.left, y, bill_to_title(s, "compact"), INK)
    y_meta = _meta(ctx, ctx.right - 190, y, meta_rows(s), L.size)
    y = min(y_bill, y_meta) + 2
    _rule(c, ctx.left, ctx.right, y, INK)
    return y - 14


HEADERS = {"classic": _header_classic, "banner": _header_banner, "modern": _header_modern,
           "compact": _header_compact}


# --------------------------------------------------------------------------------------------
# Shared body: lines table, totals, notes, payment block, footer, watermark
# --------------------------------------------------------------------------------------------


def _table_header(ctx: Ctx, y: float, columns: list[tuple[str, float, str]]) -> float:
    """Draw the table header row in the layout's style; returns the y of its bottom edge."""
    c, L = ctx.c, ctx.look
    x0, x1 = ctx.left, ctx.right
    header_h = L.size + 10
    header_colour = INK
    if L.table_header == "accent_fill":
        c.setFillColor(ctx.accent)
        c.rect(x0, y - header_h, ctx.width, header_h, stroke=0, fill=1)
        header_colour = WHITE
    elif L.table_header == "light_fill":
        c.setFillColor(LIGHT)
        c.rect(x0, y - header_h, ctx.width, header_h, stroke=0, fill=1)
    else:
        rule_colour = ctx.accent if L.table_header == "accent_rules" else INK
        _rule(c, x0, x1, y, rule_colour, 1)
        _rule(c, x0, x1, y - header_h, rule_colour, 0.6)
    base = y - header_h + 3.5 + L.size * 0.1
    for label, x, align in columns:
        _text(c, x, base, label, L.bold, L.size - 0.5, header_colour, align)
    return y - header_h


def _draw_table(ctx: Ctx, y: float) -> float:
    c, s, L = ctx.c, ctx.spec, ctx.look
    country, x0, x1, T = s.party.country, ctx.left, ctx.right, labels(s)
    col_amount, col_price, col_qty = x1 - 6, x1 - 86, x1 - 146
    col_desc = max(x0 + 26, x0 + 12 + stringWidth(T["col_no"], L.bold, L.size - 0.5))  # room for "Pos."
    statement = is_statement(s)  # open items: description and amount only
    desc_w = (col_price if statement else col_qty - 34) - col_desc
    amount_header = T["col_amount"].format(currency=s.currency)
    if statement:
        columns = [(T["col_no"], x0 + 6, "left"), (T["col_item"], col_desc, "left"),
                   (amount_header, col_amount, "right")]
    else:
        columns = [(T["col_no"], x0 + 6, "left"), (T["col_desc"], col_desc, "left"),
                   (T["col_qty"], col_qty, "right"), (T["col_price"], col_price, "right"),
                   (amount_header, col_amount, "right")]
    y = _table_header(ctx, y, columns)

    for no, line in enumerate(s.lines, 1):
        desc = _wrap([line.description], L.font, L.size, desc_w)
        base = y - 6 - L.size
        _text(c, x0 + 6, base, str(no), L.font, L.size)
        _lines(c, col_desc, base, desc, L.font, L.size)
        if not statement:
            _text(c, col_qty, base, fmt_quantity(line.quantity, country), L.font, L.size, INK, "right")
            _text(c, col_price, base, fmt_amount(line.unit_price, country), L.font, L.size, INK, "right")
        _text(c, col_amount, base, fmt_amount(line.amount, country), L.font, L.size, INK, "right")
        y -= len(desc) * L.size * 1.35 + 10
        _rule(c, x0, x1, y, RULE, 0.4)
    _rule(c, x0, x1, y, ctx.accent if L.table_header != "ink_rules" else INK, 0.8)
    return y


def _draw_totals(ctx: Ctx, y: float) -> float:
    c, L = ctx.c, ctx.look
    rows = totals_rows(ctx.spec)
    x_label = ctx.right - 240
    y -= L.size + 8
    for i, (label, value) in enumerate(rows):
        if i == len(rows) - 1:
            y -= 4
            _rule(c, x_label, ctx.right, y + L.size + 4, INK, 0.8)
            size, colour = L.size + 1.5, (INK if L.table_header == "ink_rules" else ctx.accent_text)
            _text(c, x_label, y, label, L.bold, size, colour)
            _text(c, ctx.right - 6, y, value, L.bold, size, colour, "right")
            _rule(c, x_label, ctx.right, y - 6, INK, 0.8)
        else:
            _text(c, x_label, y, label, L.font, L.size)
            _text(c, ctx.right - 6, y, value, L.font, L.size, INK, "right")
        y -= L.size * 1.6
    return y


def _draw_notes(ctx: Ctx, y: float) -> float:
    c, s, L = ctx.c, ctx.spec, ctx.look
    y -= 6
    if s.tax_note:
        y = _lines(c, ctx.left, y, _wrap([s.tax_note], L.italic, L.small, ctx.width), L.italic, L.small) - 4
    if s.printed_notes:
        y = _lines(c, ctx.left, y - 4, _wrap(list(s.printed_notes), L.font, L.size, ctx.width), L.font, L.size)
    return y


def _draw_payment(ctx: Ctx, y: float) -> float:
    """Payment block, anchored above the footer unless the content above already reaches that far."""
    c, L = ctx.c, ctx.look
    title, lines = payment_block(ctx.spec)
    lines = _wrap(lines, L.font, L.size, ctx.width - 20)
    height = (len(lines) + 1) * L.size * 1.35 + 12
    top = min(y - 16, CONTENT_BOTTOM + height)
    _rule(c, ctx.left, ctx.left + 230, top + 4, ctx.accent if L.table_header != "ink_rules" else INK, 0.8)
    _text(c, ctx.left, top - L.size - 2, title, L.bold, L.size, ctx.accent_text)
    return _lines(c, ctx.left, top - L.size * 2.5 - 2, lines, L.font, L.size)


def _draw_footer(ctx: Ctx) -> None:
    c, L = ctx.c, ctx.look
    size = L.small - 0.5
    _rule(c, ctx.left, ctx.right, FOOTER_Y + size + 5, RULE)
    lines = _wrap([footer_line(ctx.spec)], L.font, size, ctx.width - 70)
    _lines(c, ctx.left, FOOTER_Y, lines, L.font, size, MUTED)
    _text(c, ctx.right, FOOTER_Y, labels(ctx.spec)["page"], L.font, size, MUTED, "right")


def _draw_watermark(c: Canvas, text: str) -> None:
    """Large, light, diagonal stamp drawn over the content (translucent, so the content stays readable)."""
    check_glyphs(text)
    c.saveState()
    c.setFillColorRGB(0.78, 0.1, 0.1)
    c.setFillAlpha(0.14)
    c.translate(PAGE_W / 2, PAGE_H / 2 - 40)
    c.rotate(38)
    size = _fit(text, "Helvetica-Bold", 170, PAGE_H * 0.7)
    c.setFont("Helvetica-Bold", size)
    c.drawCentredString(0, -size * 0.35, text)
    c.restoreState()


def _draw_native(spec: DocumentSpec, target: Union[str, BinaryIO]) -> None:
    """Draw the native (text) page of spec onto target: a file path (str) or a binary file object."""
    look = LOOKS[spec.layout]
    c = Canvas(target, pagesize=A4, invariant=1)
    c.setTitle(f"{spec.heading} {spec.invoice_number}")
    c.setAuthor(spec.printed_supplier_name)
    c.setCreator("velox-p2p-sim invoices_gen (fictional sample document)")
    ctx = Ctx(c, spec, look, colors.HexColor(spec.party.brand_colour))

    y = HEADERS[spec.layout](ctx)
    y = _draw_table(ctx, y)
    y = _draw_totals(ctx, y)
    y = _draw_notes(ctx, y)
    y = _draw_payment(ctx, y)
    if y < CONTENT_BOTTOM - 16:
        raise ValueError(f"document {spec.no} does not fit on one page")
    _draw_footer(ctx)
    if spec.watermark:
        _draw_watermark(c, spec.watermark)
    c.showPage()
    c.save()


# --------------------------------------------------------------------------------------------
# Scans: rasterise the native page, skew it, add seeded noise, embed it as the only page content
# --------------------------------------------------------------------------------------------

SCAN_DPI = 300


@dataclass(frozen=True)
class ScanProfile:
    skew: float  # degrees, counter-clockwise
    greyscale: bool
    paper: int  # brightest grey level of the paper (255 = white)
    contrast: float  # 1.0 = unchanged
    blur: float  # Gaussian radius in pixels (0 = none)
    speckles: int  # number of grey dust specks
    smudge: bool  # smudge the invoice number and the gross total (the low-confidence fields)
    border: int  # grey level of the scanner lid around the rotated page
    jpeg_quality: int = 70


SCAN_PROFILES = {
    "clean": ScanProfile(skew=1.5, greyscale=False, paper=250, contrast=1.0, blur=0.0, speckles=250,
                         smudge=False, border=255),
    "low": ScanProfile(skew=3.0, greyscale=True, paper=236, contrast=0.72, blur=1.1, speckles=5000,
                       smudge=True, border=214),
}


def _smudge_targets(spec: DocumentSpec) -> list[str]:
    """Texts a low-quality scan smudges: the invoice number and the gross total (every occurrence)."""
    return [spec.invoice_number, money(spec, spec.gross_total)]


Box = tuple[float, float, float, float]  # left, top, right, bottom in pixels


def _rasterise(pdf_bytes: bytes, targets: list[str]) -> tuple[PILImage, list[Box]]:
    """Render page 1 at SCAN_DPI; return the RGB image and the pixel boxes of every occurrence of targets."""
    import pypdfium2 as pdfium  # lazy: only scans need it

    scale = SCAN_DPI / 72
    pdf = pdfium.PdfDocument(pdf_bytes)
    try:
        page = pdf[0]
        image = page.render(scale=scale).to_pil().convert("RGB")
        boxes: list[Box] = []
        textpage = page.get_textpage()
        for text in targets:
            searcher = textpage.search(text, match_case=True)
            while (found := searcher.get_next()) is not None:
                start, count = found
                first, last = textpage.get_charbox(start), textpage.get_charbox(start + count - 1)
                left, right = first[0], last[2]
                bottom, top = min(first[1], last[1]), max(first[3], last[3])
                boxes.append((left * scale, (PAGE_H - top) * scale, right * scale, (PAGE_H - bottom) * scale))
    finally:
        pdf.close()
    return image, boxes


def _degrade(image: PILImage, boxes: list[Box], profile: ScanProfile, rng: random.Random) -> PILImage:
    """Paper tone, smudges, blur, lower contrast, dust and skew; deterministic for a given rng."""
    from PIL import Image, ImageDraw, ImageEnhance, ImageFilter  # lazy: only scans need Pillow directly

    if profile.greyscale:
        image = image.convert("L")
    image = image.point(lambda v: v * profile.paper // 255)  # paper is never pure white
    if profile.smudge:  # partially legible: blurred and stained, not erased
        for left, top, right, bottom in boxes:
            pad = 18
            area = tuple(int(v) for v in (left - pad, top - pad, right + pad, bottom + pad))
            image.paste(image.crop(area).filter(ImageFilter.GaussianBlur(2.2)), area[:2])
            stain = Image.new("L", image.size, 0)
            ImageDraw.Draw(stain).ellipse(area, fill=70)
            image = Image.composite(Image.new(image.mode, image.size, 150 if profile.greyscale else (150,) * 3),
                                    image, stain.filter(ImageFilter.GaussianBlur(10)))
    if profile.blur:
        image = image.filter(ImageFilter.GaussianBlur(profile.blur))
    if profile.contrast != 1.0:
        image = ImageEnhance.Contrast(image).enhance(profile.contrast)
    draw = ImageDraw.Draw(image)
    width, height = image.size
    for _ in range(profile.speckles):
        x, y, r = rng.randrange(width), rng.randrange(height), rng.choice((1, 1, 1, 2, 2, 3))
        grey = rng.randrange(60, 200)
        draw.ellipse((x - r, y - r, x + r, y + r), fill=grey if profile.greyscale else (grey,) * 3)
    border = profile.border if profile.greyscale else (profile.border,) * 3
    return image.rotate(profile.skew, resample=Image.BICUBIC, expand=True, fillcolor=border)


def render_scan(spec: DocumentSpec, path: Path) -> Path:
    """Render spec as a scanned-image PDF: one JPEG on an A4 page, no text layer."""
    profile = SCAN_PROFILES[spec.scan]
    native = io.BytesIO()
    _draw_native(spec, native)
    image, boxes = _rasterise(native.getvalue(), _smudge_targets(spec) if profile.smudge else [])
    image = _degrade(image, boxes, profile, random.Random(spec.no))
    jpeg = io.BytesIO()
    image.save(jpeg, "JPEG", quality=profile.jpeg_quality, optimize=False, progressive=False)

    width_pt, height_pt = (px * 72 / SCAN_DPI for px in image.size)
    scale = min(PAGE_W / width_pt, PAGE_H / height_pt)
    w, h = width_pt * scale, height_pt * scale
    c = Canvas(str(path), pagesize=A4, invariant=1)
    c.setTitle(f"SCAN_{spec.received_on:%Y%m%d_%H%M}")
    c.setCreator("velox-p2p-sim invoices_gen (fictional sample document, simulated scan)")
    grey = profile.border / 255
    c.setFillColorRGB(grey, grey, grey)
    c.rect(0, 0, PAGE_W, PAGE_H, stroke=0, fill=1)
    c.drawImage(ImageReader(io.BytesIO(jpeg.getvalue())), (PAGE_W - w) / 2, (PAGE_H - h) / 2, width=w, height=h)
    c.showPage()
    c.save()
    return path


# --------------------------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------------------------


def render_document(spec: DocumentSpec, path: Path) -> Path:
    """Render one document as a one-page A4 PDF at path (byte-identical on every run); scans are image-only."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if spec.scan:
        return render_scan(spec, path)
    _draw_native(spec, str(path))
    return path


def write_document(spec: DocumentSpec, path: Path) -> Path:
    """Write one document in its format: PDF (native or scan), UBL XML (app.ubl) or the email body (UTF-8)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if spec.content == "ubl_xml":
        from app import ubl  # lazy: the v1 documents never need it

        path.write_bytes(ubl.render_ubl(spec))
    elif spec.content == "email_body":
        path.write_bytes((spec.email_body or "").encode("utf-8"))  # bytes: LF on every OS
    else:
        render_document(spec, path)
    return path


def generate_all(out_dir: Path = config.INVOICES_DIR) -> list[Path]:
    """Render every world.DOCUMENTS entry to out_dir / spec.filename."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    return [render_document(spec, out_dir / spec.filename) for spec in DOCUMENTS]


def generate_all_v2(out_dir: Optional[Path] = None) -> list[Path]:
    """Write every test set v2 document to out_dir (default config.INVOICES_V2_DIR) / spec.filename.

    The UBL e-invoice is written last, so the other files exist even if app.ubl fails. Returns the paths in
    document order.
    """
    out_dir = Path(out_dir or config.INVOICES_V2_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    specs = documents_for("v2")
    for spec in sorted(specs, key=lambda s: s.content == "ubl_xml"):
        write_document(spec, out_dir / spec.filename)
    return [out_dir / spec.filename for spec in specs]


def _report(paths: list[Path]) -> None:
    for path in paths:
        data = path.read_bytes()
        print(f"{path.name:<40} {len(data):>8} bytes  sha256 {hashlib.sha256(data).hexdigest()[:12]}")


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="Generate the sample documents (deterministic).")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--v2", action="store_true", help="test set v2 only (data/invoices_v2/)")
    group.add_argument("--all", action="store_true", help="v1 and test set v2")
    args = parser.parse_args(argv)
    if not args.v2:
        _report(generate_all(config.INVOICES_DIR))
    if args.v2 or args.all:
        _report(generate_all_v2(config.INVOICES_V2_DIR))


if __name__ == "__main__":
    main()
