"""Render the sample documents (world.DOCUMENTS: brief 5.6 plus 2 clean ones) as realistic one-page A4 PDFs.

Four supplier templates, keyed by spec.layout:
  classic  Times fonts, logo-box letterhead top-left, boxed meta table top-right
  banner   full-width colour banner with the supplier name, Helvetica
  modern   accent side bar, right-aligned meta block, Helvetica
  compact  small-business style: small fonts, simple black rules
Numbers and dates follow the supplier's country conventions, so the extraction has to cope with them.
Output is byte-identical across runs (reportlab invariant mode, no timestamps, no randomness).
Phone and company-registration numbers are fake and derived from the supplier number.

CLI:  python -m app.invoices_gen   (writes data/invoices/*.pdf)
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import simpleSplit
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen.canvas import Canvas

from app import config
from app.world import DOCUMENTS, DocumentSpec, PartySpec, format_iban

PAGE_W, PAGE_H = A4
INK = colors.HexColor("#222222")
MUTED = colors.HexColor("#5F5F5F")
RULE = colors.HexColor("#BDBDBD")
LIGHT = colors.HexColor("#EFEFEF")
WHITE = colors.white
FOOTER_Y = 36  # baseline of the footer line
CONTENT_BOTTOM = 70  # nothing but the footer may go below this


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


def vat_line(vat_id: str, country: str) -> str:
    return fmt_vat(vat_id, country) if country == "US" else f"VAT ID: {fmt_vat(vat_id, country)}"


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


def fake_registration(party: PartySpec) -> str:
    """Deterministic fictional company-register entry in the supplier country's usual form."""
    city, num = city_of(party), (party.no * 7331 + 2027) % 900000 + 100000
    if party.country == "FR":  # SIREN = French VAT ID without the country code and key
        siren = party.vat_id[4:]
        return f"RCS {city} {siren[:3]} {siren[3:6]} {siren[6:]}"
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


def money(spec: DocumentSpec, value: float) -> str:
    return fmt_money(value, spec.currency, spec.party.country)


def supplier_lines(spec: DocumentSpec) -> list[str]:
    """Address, phone, e-mail and VAT ID of the supplier (the name is printed separately)."""
    p = spec.party
    return [*p.address, f"Tel. {fake_phone(p)}", spec.sender_email, vat_line(p.vat_id, p.country)]


def bill_to_lines(spec: DocumentSpec) -> list[str]:
    entity = spec.bill_to
    lines = [entity.name]
    if spec.bill_to_attention:
        lines.append(f"Attn: {spec.bill_to_attention}")
    lines += list(spec.bill_to_address or entity.address)
    if spec.print_bill_to_vat:
        lines.append(vat_line(entity.vat_id, entity.country))
    return lines


def meta_rows(spec: DocumentSpec) -> list[tuple[str, str]]:
    country = spec.party.country
    kind = "Credit note" if is_credit(spec) else "Invoice"
    rows = [(f"{kind} no.", spec.invoice_number), (f"{kind} date", fmt_date(spec.invoice_date, country))]
    if spec.due_date:
        rows.append(("Due date", fmt_date(spec.due_date, country)))
    if spec.payment_terms_days:
        rows.append(("Payment terms", f"{spec.payment_terms_days} days net"))
    rows += [("Your PO", po) for po in spec.po_numbers]
    if spec.contract_reference:
        rows.append(("Contract ref.", spec.contract_reference))
    if spec.referenced_invoice_number:
        rows.append(("Relates to invoice", spec.referenced_invoice_number))
    return rows


def totals_rows(spec: DocumentSpec) -> list[tuple[str, str]]:
    return [
        ("Net amount", money(spec, spec.net_total)),
        (spec.tax_label, money(spec, spec.tax_total)),
        ("Total credit" if is_credit(spec) else "Total due", money(spec, spec.gross_total)),
    ]


def payment_block(spec: DocumentSpec) -> tuple[str, list[str]]:
    """Title and lines of the payment block. Credit notes carry no payment instruction."""
    p = spec.party
    if is_credit(spec):
        return "Settlement", [
            "Amount will be credited to your account.",
            f"Reference: {spec.invoice_number}",
            f"Our bank details: {p.bank_name} · {fmt_bank_account(p.bank)}",
        ]
    return "Payment details", [
        f"Please transfer {money(spec, spec.gross_total)} by {fmt_date(spec.due_date, p.country)} to:"
        if spec.due_date else "Please transfer the total due to:",
        f"Account holder: {spec.printed_supplier_name}",
        f"Bank: {p.bank_name}",
        fmt_bank_account(p.bank),
        f"Payment reference: {spec.invoice_number}",
    ]


def footer_line(spec: DocumentSpec) -> str:
    """Registration line; uses the printed supplier name only (docs #2 and #4 print a short name)."""
    p = spec.party
    return f"{spec.printed_supplier_name} · Registered office: {city_of(p)} · {fake_registration(p)}"


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
    return _bill_to(ctx, ctx.left, y, "Bill to", ctx.accent_text) - 12


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
    y_bill = _bill_to(ctx, ctx.left, y, "BILL TO", MUTED)
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
    contacts = _wrap([", ".join(p.address), f"Tel. {fake_phone(p)} · {s.sender_email}", vat_line(p.vat_id, p.country)],
                     L.font, L.small, text_w)
    y_supplier = _lines(c, ctx.left, top - 18, contacts, L.font, L.small, MUTED)

    _text(c, ctx.right, top, s.heading, L.bold, _fit(s.heading, L.bold, 24, meta_w), INK, "right")
    y_meta = _meta(ctx, ctx.right - 105, top - 24, meta_rows(s), L.size - 0.5, label_align="right")

    y = min(y_supplier, y_meta) - 4
    _rule(c, ctx.left, ctx.right, y, ctx.accent, 1.2)
    return _bill_to(ctx, ctx.left, y - 24, "BILLED TO", ctx.accent_text) - 10


def _header_compact(ctx: Ctx) -> float:
    c, s, L = ctx.c, ctx.spec, ctx.look
    p = s.party
    top = PAGE_H - 48
    _text(c, ctx.right, top, s.heading, L.bold, 14, INK, "right")
    heading_w = stringWidth(s.heading, L.bold, 14)
    _text(c, ctx.left, top, s.printed_supplier_name, L.bold,
          _fit(s.printed_supplier_name, L.bold, 14, ctx.width - heading_w - 20), ctx.accent_text)
    contacts = _wrap([" · ".join(p.address),
                      f"Tel. {fake_phone(p)} · {s.sender_email} · {vat_line(p.vat_id, p.country)}"],
                     L.font, L.small, ctx.width)
    y = _lines(c, ctx.left, top - 14, contacts, L.font, L.small, MUTED) + 2
    _rule(c, ctx.left, ctx.right, y, INK)

    y -= 18
    y_bill = _bill_to(ctx, ctx.left, y, "Invoice to:" if not is_credit(s) else "Credit to:", INK)
    y_meta = _meta(ctx, ctx.right - 190, y, meta_rows(s), L.size)
    y = min(y_bill, y_meta) + 2
    _rule(c, ctx.left, ctx.right, y, INK)
    return y - 14


HEADERS = {"classic": _header_classic, "banner": _header_banner, "modern": _header_modern,
           "compact": _header_compact}


# --------------------------------------------------------------------------------------------
# Shared body: lines table, totals, notes, payment block, footer, watermark
# --------------------------------------------------------------------------------------------


def _draw_table(ctx: Ctx, y: float) -> float:
    c, s, L = ctx.c, ctx.spec, ctx.look
    country, x0, x1 = s.party.country, ctx.left, ctx.right
    col_amount, col_price, col_qty, col_desc = x1 - 6, x1 - 86, x1 - 146, x0 + 26
    desc_w = col_qty - 34 - col_desc

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
    for label, x, align in (("#", x0 + 6, "left"), ("Description", col_desc, "left"), ("Qty", col_qty, "right"),
                            ("Unit price", col_price, "right"), (f"Amount ({s.currency})", col_amount, "right")):
        _text(c, x, base, label, L.bold, L.size - 0.5, header_colour, align)

    y -= header_h
    for no, line in enumerate(s.lines, 1):
        desc = _wrap([line.description], L.font, L.size, desc_w)
        base = y - 6 - L.size
        _text(c, x0 + 6, base, str(no), L.font, L.size)
        _lines(c, col_desc, base, desc, L.font, L.size)
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
    _text(c, ctx.right, FOOTER_Y, "Page 1 of 1", L.font, size, MUTED, "right")


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


# --------------------------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------------------------


def render_document(spec: DocumentSpec, path: Path) -> Path:
    """Render one document as a one-page A4 PDF at path (byte-identical on every run)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    look = LOOKS[spec.layout]
    c = Canvas(str(path), pagesize=A4, invariant=1)
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
    return path


def generate_all(out_dir: Path = config.INVOICES_DIR) -> list[Path]:
    """Render every world.DOCUMENTS entry to out_dir / spec.filename."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    return [render_document(spec, out_dir / spec.filename) for spec in DOCUMENTS]


def main() -> None:
    for path in generate_all(config.INVOICES_DIR):
        data = path.read_bytes()
        print(f"{path.name:<34} {len(data):>6} bytes  sha256 {hashlib.sha256(data).hexdigest()[:12]}")


if __name__ == "__main__":
    main()
