"""Test set v2 (phase 3): 26 documents, a wider and harder set than the 14 case documents.

Source of truth for docs/TEST_SET_V2.md (the contract) and tests/golden_v2.yaml. It reuses the seed of
app/world.py: same suppliers, accounts, POs, receipts and contracts. Only document 26 comes from a supplier
that is NOT in world.PARTIES (UNKNOWN_SUPPLIERS), so the seed does not know it.
v2 is a separate run on that seed, as if the 14 case documents (world.DOCUMENTS) had not been received: some v2
invoices bill POs and services that a case document also bills, and no v2 document lists a case invoice.
- invoices_gen.generate_all_v2 renders data/invoices_v2/: native and scanned PDFs, one Peppol UBL XML
  (document 11) and one email body (document 14);
- tests/make_fixtures.py writes the ground-truth fixtures in tests/fixtures_v2/.
Invoice dates are October-November 2026; arrival in the mailboxes Mon 2 - Tue 10 Nov 2026.
All companies, people, VAT IDs and bank accounts are fictional.
"""
from __future__ import annotations

from datetime import date, datetime

from app.world import DocumentSpec, InvoiceLineSpec, PartySpec, PARTY_BY_ID, format_iban, make_iban

# --------------------------------------------------------------------------------------------
# The unknown supplier (document 26): deliberately NOT in world.PARTIES
# --------------------------------------------------------------------------------------------

BERLINER_BLUMEN = PartySpec(
    "X-0001", 13, "Berliner Blumen GmbH", "DE", "DE305118442",
    make_iban("DE", "100500000190123456"), "Berliner Volksbank",
    ("Kastanienallee 12", "10435 Berlin", "Germany"),
    "Local florist supplying a Berlin store — not in the vendor master", 14, "berliner-blumen.de", "#AD1457")
UNKNOWN_SUPPLIERS: list[PartySpec] = [BERLINER_BLUMEN]

# Document headings per language and document type (spec.heading; document 2 adds "KOPIE").
HEADINGS: dict[str, dict[str, str]] = {
    "en": {"invoice": "INVOICE", "credit_note": "CREDIT NOTE", "other": "STATEMENT OF ACCOUNT"},
    "de": {"invoice": "RECHNUNG", "credit_note": "GUTSCHRIFT", "other": "KONTOAUSZUG"},
    "fr": {"invoice": "FACTURE", "credit_note": "AVOIR", "other": "RELEVÉ DE COMPTE"},
    "es": {"invoice": "FACTURA", "credit_note": "ABONO", "other": "EXTRACTO DE CUENTA"},
}

# --------------------------------------------------------------------------------------------
# Shared content
# --------------------------------------------------------------------------------------------

_NW_LINES = (
    InvoiceLineSpec("Lager und Fulfilment, Berlin DC — Oktober 2026", 1, 14600.00),
    InvoiceLineSpec("Filialbelieferung Deutschland — Oktober 2026", 1, 9500.00),
)
_NW_NOTE = "Leistungszeitraum: 01.10.2026 – 31.10.2026."
_WD120_ES = "Expositor mural WD-120, acabado roble"
_STORE_B01 = {"bill_to_attention": "Store Berlin 01",
              "bill_to_address": ("Rosenthaler Strasse 40", "10178 Berlin", "Germany"),
              "print_bill_to_vat": False}

_RC_EN = "Reverse charge: VAT to be accounted for by the recipient (Article 196, Directive 2006/112/EC)."
_RC_FR = "Autoliquidation : TVA due par le preneur (article 196 de la directive 2006/112/CE)."
_ICS_ES = ("Entrega intracomunitaria exenta de IVA (artículo 25 de la Ley 37/1992; "
           "artículo 138 de la Directiva 2006/112/CE).")
_ICS_EN = "Intra-Community supply, exempt under Article 138, Directive 2006/112/EC."
_UK_EXPORT = "Export of goods outside the UK: zero-rated."

# Document 8 is forwarded by a store manager; this is his comment in the email body.
_FORWARD_COMMENT_8 = ("Bonjour, facture reçue au magasin la semaine dernière (affiches et flyers de la campagne "
                      "d'automne). Merci de la régler. Luc")

# Kaffee & Co sells food only: coffee beans and milk take the reduced German VAT rate (7 %, Anlage 2 UStG), and the
# delivery charge follows the main supply.
_KAFFEE_VAT = {"tax_rate": 0.07, "tax_label": "MwSt. 7 %", "tax_note": None}
_VOLLMILCH = "Vollmilch 3,5 %, 1 l, Karton à 12"

# Document 14 is the email itself: the invoice is only in this text, there is no attachment.
_EMAIL_BODY_14 = (
    "Guten Tag,\n\n"
    "anbei unsere Rechnung 2026/140 vom 06.11.2026 über 52,43 EUR (netto 49,00 EUR zzgl. 7 % MwSt. 3,43 EUR) "
    "für die Kaffeelieferung KW 45 an die Filiale Berlin 01.\n\n"
    f"Zahlbar innerhalb von 14 Tagen auf IBAN {format_iban(PARTY_BY_ID['P-0009'].bank)} (Kaffee & Co).\n\n"
    "Mit freundlichen Grüßen\n"
    "Kaffee & Co OHG\n"
)


def _nov(day: int, hour: int, minute: int) -> datetime:
    """Arrival time in the mailbox: November 2026."""
    return datetime(2026, 11, day, hour, minute)


# --------------------------------------------------------------------------------------------
# The 26 documents (docs/TEST_SET_V2.md). Suppliers keep their v1 layout.
# --------------------------------------------------------------------------------------------

DOCUMENTS_V2: list[DocumentSpec] = [
    DocumentSpec(
        no=1, filename="01_nordwind_rechnung.pdf", channel="ap_mailbox",
        sender_email="billing@nordwind-logistics.de",
        subject="Rechnung NWL-2026-01027 — Logistikleistungen Oktober 2026",
        received_on=_nov(2, 8, 15), doc_type="invoice", party_id="P-0001",
        printed_supplier_name="Nordwind Logistics GmbH", bill_to_entity="VDE",
        invoice_number="NWL-2026-01027", invoice_date=date(2026, 10, 31), payment_terms_days=30,
        currency="EUR", lines=_NW_LINES, tax_rate=0.19, tax_label="USt. 19 %", tax_note=None,
        contract_reference="CT-2025-001", heading=HEADINGS["de"]["invoice"], printed_notes=(_NW_NOTE,),
        layout="banner", language="de", vat_display="DE281947305", dataset="v2",
        designed_to_show="German invoice, contract match. VAT ID printed compact after the German label.",
    ),
    DocumentSpec(
        no=2, filename="02_nordwind_rechnung_kopie.pdf", channel="store_mailbox",
        sender_email="ar@nordwind-logistics.de",
        subject="Kopie: Rechnung NWL-2026-01027",
        received_on=_nov(10, 15, 20), doc_type="invoice", party_id="P-0001",
        printed_supplier_name="Nordwind Logistics GmbH", bill_to_entity="VDE",
        invoice_number="NWL-2026-01027", invoice_date=date(2026, 10, 31), payment_terms_days=30,
        currency="EUR", lines=_NW_LINES, tax_rate=0.19, tax_label="USt. 19 %", tax_note=None,
        contract_reference="CT-2025-001", heading="RECHNUNG — KOPIE", watermark="KOPIE",
        printed_notes=("Kopie unserer Rechnung NWL-2026-01027 vom 31.10.2026.", _NW_NOTE),
        layout="banner", language="de", vat_display="DE281947305", dataset="v2",
        designed_to_show="Duplicate of 1 on the same account: blocked in both scenarios (the naive per-account "
                         "check works when the account is the same).",
    ),
    DocumentSpec(
        no=3, filename="03_nordwind_kontoauszug.pdf", channel="ap_mailbox",
        sender_email="ar@nordwind-logistics.de",
        subject="Kontoauszug KA-2026-11 — offene Posten",
        received_on=_nov(5, 10, 0), doc_type="other", party_id="P-0001",
        printed_supplier_name="Nordwind Logistics GmbH", bill_to_entity="VDE",
        invoice_number="KA-2026-11", invoice_date=date(2026, 11, 5), payment_terms_days=None,
        currency="EUR",
        lines=(InvoiceLineSpec("Rechnung NWL-2026-00871 vom 31.08.2026", 1, 27846.00),
               InvoiceLineSpec("Rechnung NWL-2026-01027 vom 31.10.2026", 1, 28679.00)),
        tax_rate=0.0, tax_label="", tax_note=None, heading=HEADINGS["de"]["other"],
        printed_notes=("Offene Posten zum 05.11.2026. Bitte gleichen Sie die Posten mit Ihrer Buchhaltung ab.",
                       "Dieser Kontoauszug ist keine Rechnung. Rückfragen bitte an ar@nordwind-logistics.de."),
        layout="banner", language="de", dataset="v2",
        designed_to_show="Not an invoice (a statement of open items): to-be files it; the as-is tool posts it "
                         "as an invoice.",
    ),
    DocumentSpec(
        no=4, filename="04_cleanspace_invoice_vde.pdf", channel="ap_mailbox",
        sender_email="invoicing@cleanspace.nl",
        subject="Invoice CSF-26-11412 — cleaning services Germany, October 2026",
        received_on=_nov(2, 9, 40), doc_type="invoice", party_id="P-0004",
        printed_supplier_name="Cleanspace Facilities BV", bill_to_entity="VDE",
        invoice_number="CSF-26-11412", invoice_date=date(2026, 10, 31), payment_terms_days=30,
        currency="EUR",
        lines=(InvoiceLineSpec("Store cleaning services Germany — October 2026 (9 stores)", 1, 3150.00),),
        tax_rate=0.0, tax_label="VAT 0%", tax_note=_RC_EN, contract_reference="CT-2025-002",
        layout="classic", language="en", dataset="v2",
        designed_to_show="Supplier with two legitimate accounts (VDE and VFR): this one is the VDE account.",
    ),
    DocumentSpec(
        no=5, filename="05_cleanspace_facture_vfr.pdf", channel="ap_mailbox",
        sender_email="invoicing@cleanspace.nl",
        subject="Facture CSF-26-11413 — nettoyage des magasins France, octobre 2026",
        received_on=_nov(2, 9, 41), doc_type="invoice", party_id="P-0004",
        printed_supplier_name="Cleanspace Facilities BV", bill_to_entity="VFR",
        invoice_number="CSF-26-11413", invoice_date=date(2026, 10, 31), payment_terms_days=30,
        currency="EUR",
        lines=(InvoiceLineSpec("Nettoyage des magasins France — octobre 2026 (6 magasins)", 1, 3300.00),),
        tax_rate=0.0, tax_label="TVA 0 %", tax_note=_RC_FR, contract_reference="CT-2025-003",
        heading=HEADINGS["fr"]["invoice"], printed_notes=("Période de prestation : octobre 2026.",),
        layout="classic", language="fr", dataset="v2",
        designed_to_show="Two legitimate accounts: to-be picks the VFR account; as-is posts to the VDE account.",
    ),
    DocumentSpec(
        no=6, filename="06_bright_agency_facture_q4.pdf", channel="ap_mailbox",
        sender_email="billing@bright-agency.fr",
        subject="Facture INV-2026-0512 — campagne réseaux sociaux T4 — commande 4500121",
        received_on=_nov(3, 11, 5), doc_type="invoice", party_id="P-0002",
        printed_supplier_name="Bright Agency SARL", bill_to_entity="VFR",
        invoice_number="INV-2026-0512", invoice_date=date(2026, 10, 30), payment_terms_days=45,
        currency="EUR",
        lines=(InvoiceLineSpec("Campagne réseaux sociaux T4 — contenu et animation (octobre)", 1, 6000.00),),
        tax_rate=0.20, tax_label="TVA 20 %", tax_note=None, po_numbers=("4500121",),
        heading=HEADINGS["fr"]["invoice"], layout="modern", language="fr", dataset="v2",
        designed_to_show="French invoice on a PO whose service is not confirmed yet.",
    ),
    DocumentSpec(
        no=7, filename="07_quickprint_avoir.pdf", channel="ap_mailbox",
        sender_email="accounts@quickprint.fr",
        subject="Avoir AV-26-0088 — remise commerciale 2026",
        received_on=_nov(4, 14, 30), doc_type="credit_note", party_id="P-0008",
        printed_supplier_name="QuickPrint SAS", bill_to_entity="VFR",
        invoice_number="AV-26-0088", invoice_date=date(2026, 11, 4), payment_terms_days=None,
        currency="EUR",
        lines=(InvoiceLineSpec("Remise commerciale — volume 2026", 1, -200.00),),
        tax_rate=0.20, tax_label="TVA 20 %", tax_note=None, heading=HEADINGS["fr"]["credit_note"],
        printed_notes=("Remise commerciale accordée sur votre volume d'achats 2026.",),
        layout="compact", language="fr", dataset="v2",
        designed_to_show="Credit note without a reference to any invoice.",
    ),
    DocumentSpec(
        no=8, filename="08_quickprint_facture_forwarded.pdf", channel="ap_mailbox",
        sender_email="luc.bernard@velox.com",
        subject="TR: Facture QP-26-1107",
        received_on=_nov(6, 16, 45), doc_type="invoice", party_id="P-0008",
        printed_supplier_name="QuickPrint SAS", bill_to_entity="VFR",
        invoice_number="QP-26-1107", invoice_date=date(2026, 10, 28), payment_terms_days=30,
        currency="EUR",
        lines=(InvoiceLineSpec("Affiches A2 en magasin, quadrichromie", 400, 3.50),
               InvoiceLineSpec("Flyers A5 recto-verso", 10000, 0.10)),
        tax_rate=0.20, tax_label="TVA 20 %", tax_note=None, po_numbers=("4500114",),
        heading=HEADINGS["fr"]["invoice"], layout="compact", language="fr",
        email_body=_FORWARD_COMMENT_8, dataset="v2",
        designed_to_show="Forwarded by a store manager with a comment; French line texts against English PO lines.",
    ),
    DocumentSpec(
        no=9, filename="09_atlas_displays_factura_vfr.pdf", channel="ap_mailbox",
        sender_email="invoices@atlasdisplays.es",
        subject="Factura AD-2026/0861 — pedido 4500105",
        received_on=_nov(3, 9, 12), doc_type="invoice", party_id="P-0006",
        printed_supplier_name="Atlas Displays SL", bill_to_entity="VFR",
        invoice_number="AD-2026/0861", invoice_date=date(2026, 10, 27), payment_terms_days=60,
        currency="EUR", lines=(InvoiceLineSpec(_WD120_ES, 80, 42.00),),
        tax_rate=0.0, tax_label="IVA 0 %", tax_note=_ICS_ES, po_numbers=("4500105",),
        heading=HEADINGS["es"]["invoice"], layout="classic", language="es", vat_display="ES-B86419273",
        dataset="v2",
        designed_to_show="Two legitimate accounts: as-is takes the VDE account, the PO does not match, "
                         "wrong-entity posting. VAT ID printed with a hyphen.",
    ),
    DocumentSpec(
        no=10, filename="10_atlas_displays_factura_vde.pdf", channel="ap_mailbox",
        sender_email="invoices@atlasdisplays.es",
        subject="Factura AD-2026/0862 — pedido 4500109",
        received_on=_nov(3, 9, 13), doc_type="invoice", party_id="P-0006",
        printed_supplier_name="Atlas Displays SL", bill_to_entity="VDE",
        invoice_number="AD-2026/0862", invoice_date=date(2026, 10, 27), payment_terms_days=60,
        currency="EUR", lines=(InvoiceLineSpec(_WD120_ES, 150, 42.00),),
        tax_rate=0.0, tax_label="IVA 0 %", tax_note=_ICS_ES, po_numbers=("4500109",),
        heading=HEADINGS["es"]["invoice"], layout="classic", language="es", dataset="v2",
        designed_to_show="Spanish invoice, clean 3-way match.",
    ),
    DocumentSpec(
        no=11, filename="11_metro_media_einvoice.xml", channel="ap_mailbox",
        sender_email="einvoice@metromedia.de",
        subject="E-Rechnung MM-2026-248 (Peppol BIS Billing 3.0) — Bestellung 4500126",
        received_on=_nov(2, 7, 30), doc_type="invoice", party_id="P-0007",
        printed_supplier_name="Metro Media GmbH", bill_to_entity="VDE",
        invoice_number="MM-2026-248", invoice_date=date(2026, 10, 30), payment_terms_days=30,
        currency="EUR",
        lines=(InvoiceLineSpec("Berlin Außenwerbung September 2026 — 40 Plakatflächen", 1, 7500.00),),
        tax_rate=0.19, tax_label="USt. 19 %", tax_note=None, po_numbers=("4500126",),
        heading=HEADINGS["de"]["invoice"], layout="banner", language="de", content="ubl_xml", dataset="v2",
        designed_to_show="Structured e-invoice (Peppol BIS Billing 3.0 UBL) parsed directly, no model call.",
    ),
    DocumentSpec(
        no=12, filename="12_fitout_partners_scan.pdf", channel="ap_mailbox",
        sender_email="accounts@fitoutpartners.co.uk",
        subject="Invoice 2026-104 (scan) — Hamburg milestone 1 — PO 4500101",
        received_on=_nov(4, 10, 20), doc_type="invoice", party_id="P-0003",
        printed_supplier_name="FitOut Partners Ltd", bill_to_entity="VDE",
        invoice_number="2026-104", invoice_date=date(2026, 10, 30), payment_terms_days=30,
        currency="EUR",
        lines=(InvoiceLineSpec("Store fit-out Hamburg — milestone 1: demolition and electrical works", 1, 40000.00),),
        tax_rate=0.0, tax_label="VAT 0%",
        tax_note="Reverse charge: customer to account for VAT (Article 196, Directive 2006/112/EC).",
        po_numbers=("4500101",), layout="classic", language="en", scan="clean", vat_display="GB293 8475 61",
        dataset="v2",
        designed_to_show="Clean scan (300 dpi, 1.5° skew, no text layer) read with high confidence.",
    ),
    DocumentSpec(
        no=13, filename="13_kaffee_scan_low.pdf", channel="store_mailbox",
        sender_email="info@kaffee-und-co.de",
        subject="Rechnung 2026/139 (Scan)",
        received_on=_nov(2, 12, 0), doc_type="invoice", party_id="P-0009",
        printed_supplier_name="Kaffee & Co OHG", bill_to_entity="VDE",
        invoice_number="2026/139", invoice_date=date(2026, 10, 30), payment_terms_days=14,
        currency="EUR",
        lines=(InvoiceLineSpec("Kaffeebohnen Espresso 1 kg", 3, 18.50),
               InvoiceLineSpec(_VOLLMILCH, 1, 14.98),
               InvoiceLineSpec("Lieferung", 1, 10.30)),
        **_KAFFEE_VAT, heading=HEADINGS["de"]["invoice"],
        printed_notes=("Vielen Dank für Ihre Bestellung!",),
        layout="compact", language="de", scan="low", dataset="v2", **_STORE_B01,
        designed_to_show="Poor scan (3° skew, noise, smudged number and total): low confidence on the gross "
                         "total and the invoice number -> human review (to-be).",
    ),
    DocumentSpec(
        no=14, filename="14_kaffee_email_body.txt", channel="store_mailbox",
        sender_email="info@kaffee-und-co.de",
        subject="Rechnung 2026/140",
        received_on=_nov(6, 9, 30), doc_type="invoice", party_id="P-0009",
        printed_supplier_name="Kaffee & Co OHG", bill_to_entity="VDE",
        invoice_number="2026/140", invoice_date=date(2026, 11, 6), payment_terms_days=14,
        currency="EUR", lines=(InvoiceLineSpec("Kaffeelieferung KW 45", 1, 49.00),),
        **_KAFFEE_VAT, heading=HEADINGS["de"]["invoice"],
        layout="compact", language="de", content="email_body", email_body=_EMAIL_BODY_14, dataset="v2",
        **_STORE_B01,
        designed_to_show="Invoice only in the email body, no attachment: registered as 'unknown' and routed to "
                         "human review.",
    ),
    DocumentSpec(
        no=15, filename="15_lumen_partial_delivery.pdf", channel="ap_mailbox",
        sender_email="accounts@lumenlighting.co.uk",
        subject="Invoice LSL-INV-5602 — PO 4500112 (partial delivery)",
        received_on=_nov(4, 8, 5), doc_type="invoice", party_id="P-0012",
        printed_supplier_name="Lumen Store Lighting Ltd", bill_to_entity="VDE",
        invoice_number="LSL-INV-5602", invoice_date=date(2026, 10, 29), payment_terms_days=30,
        currency="EUR",
        lines=(InvoiceLineSpec("LED track spotlight 30W", 80, 40.00),
               InvoiceLineSpec("LED panel 600x600 40W — partial delivery, 20 of 40", 20, 50.00)),
        tax_rate=0.0, tax_label="VAT 0%", tax_note=_UK_EXPORT, po_numbers=("4500112",),
        printed_notes=("Partial delivery: the remaining 20 LED panels follow and will be invoiced on delivery.",),
        layout="modern", language="en", dataset="v2",
        designed_to_show="Partial delivery invoiced correctly (only what was received): touchless.",
    ),
    DocumentSpec(
        no=16, filename="16_securenet_multi_po.pdf", channel="ap_mailbox",
        sender_email="billing@securenet.ch",
        subject="Invoice SN-2026-3391 — POs 4500128 and 4500130",
        received_on=_nov(5, 13, 10), doc_type="invoice", party_id="P-0010",
        printed_supplier_name="SecureNet AG", bill_to_entity="VDE",
        invoice_number="SN-2026-3391", invoice_date=date(2026, 10, 31), payment_terms_days=30,
        currency="EUR",
        lines=(InvoiceLineSpec("Managed firewall service Q3 2026 (PO 4500128)", 1, 4800.00),
               InvoiceLineSpec("Penetration test — e-commerce platform (PO 4500130)", 1, 3500.00)),
        tax_rate=0.0, tax_label="VAT 0%",
        tax_note="Reverse charge: VAT to be accounted for by the recipient (services supplied from Switzerland).",
        po_numbers=("4500128", "4500130"),
        printed_notes=("This invoice covers two purchase orders; the PO is shown on each line.",),
        layout="classic", language="en", dataset="v2",
        designed_to_show="Multi-PO invoice: the gate pinpoints the line whose service is not confirmed.",
    ),
    DocumentSpec(
        no=17, filename="17_shopsys_eur_invoice.pdf", channel="ap_mailbox",
        sender_email="billing@shopsys.io",
        subject="Shopsys invoice SS-100517 (PO 4500131)",
        received_on=_nov(2, 7, 50), doc_type="invoice", party_id="P-0005",
        printed_supplier_name="Shopsys Software Inc.", bill_to_entity="VUS",
        invoice_number="SS-100517", invoice_date=date(2026, 11, 1), payment_terms_days=30,
        currency="EUR",
        lines=(InvoiceLineSpec("Shopsys Commerce Cloud — annual subscription (Oct 2026 – Sep 2027)", 1, 9600.00),),
        tax_rate=0.0, tax_label="Sales tax", tax_note="Sales tax: not applicable to this service.",
        po_numbers=("4500131",), printed_notes=("All amounts are in euro (EUR).",),
        layout="modern", language="en", dataset="v2",
        designed_to_show="Invoice currency (EUR) different from the PO currency (USD).",
    ),
    DocumentSpec(
        no=18, filename="18_harbor_freight_invoice.pdf", channel="ap_mailbox",
        sender_email="billing@harborff.com",
        subject="Invoice HFF-2026-1031 — October 2026 freight services",
        received_on=_nov(2, 16, 30), doc_type="invoice", party_id="P-0011",
        printed_supplier_name="Harbor Freight Forwarders Inc.", bill_to_entity="VUS",
        invoice_number="HFF-2026-1031", invoice_date=date(2026, 10, 31), payment_terms_days=30,
        currency="USD",
        lines=(InvoiceLineSpec("Ocean freight forwarding — October 2026", 1, 11100.00),
               InvoiceLineSpec("Customs brokerage and drayage — October 2026", 1, 5700.00)),
        tax_rate=0.0, tax_label="Sales tax", tax_note="Freight and customs services: no sales tax charged.",
        contract_reference="CT-2025-004", layout="banner", language="en", dataset="v2",
        designed_to_show="US contract invoice inside the monthly range.",
    ),
    DocumentSpec(
        no=19, filename="19_harbor_freight_credit_note.pdf", channel="ap_mailbox",
        sender_email="billing@harborff.com",
        subject="Credit note HFF-CN-2026-017 for invoice HFF-2026-1031",
        received_on=_nov(6, 17, 0), doc_type="credit_note", party_id="P-0011",
        printed_supplier_name="Harbor Freight Forwarders Inc.", bill_to_entity="VUS",
        invoice_number="HFF-CN-2026-017", invoice_date=date(2026, 11, 6), payment_terms_days=None,
        currency="USD",
        lines=(InvoiceLineSpec("Credit: demurrage charge waived — October 2026", 1, -450.00),),
        tax_rate=0.0, tax_label="Sales tax", tax_note=None, referenced_invoice_number="HFF-2026-1031",
        heading=HEADINGS["en"]["credit_note"],
        printed_notes=("Credit note relating to invoice HFF-2026-1031 of Oct 31, 2026.",
                       "Demurrage charge waived as agreed. The amount is credited to your account."),
        layout="banner", language="en", dataset="v2",
        designed_to_show="Credit note on the same account as its invoice: applied in both scenarios.",
    ),
    DocumentSpec(
        no=20, filename="20_shopsys_invoice_vde.pdf", channel="ap_mailbox",
        sender_email="billing@shopsys.io",
        subject="Shopsys invoice SS-100522 (PO 4500107)",
        received_on=_nov(3, 8, 40), doc_type="invoice", party_id="P-0005",
        printed_supplier_name="Shopsys Software Inc.", bill_to_entity="VDE",
        invoice_number="SS-100522", invoice_date=date(2026, 11, 2), payment_terms_days=30,
        currency="EUR",
        lines=(InvoiceLineSpec("POS integration add-on — annual licence (Germany)", 1, 2400.00),),
        tax_rate=0.0, tax_label="VAT 0%",
        tax_note="Reverse charge: VAT to be accounted for by the recipient (services supplied from the USA).",
        po_numbers=("4500107",), layout="modern", language="en", dataset="v2",
        designed_to_show="Two legitimate accounts (VUS, VDE): as-is posts to the VUS account.",
    ),
    DocumentSpec(
        no=21, filename="21_bright_agency_facture_automne.pdf", channel="ap_mailbox",
        sender_email="billing@bright-agency.fr",
        subject="Facture INV-2026-0530 — campagne d'automne 2026 — commande 4500117",
        received_on=_nov(3, 11, 0), doc_type="invoice", party_id="P-0002",
        printed_supplier_name="Bright Agency SARL", bill_to_entity="VFR",
        invoice_number="INV-2026-0530", invoice_date=date(2026, 11, 3), payment_terms_days=45,
        currency="EUR",
        lines=(InvoiceLineSpec("Campagne d'automne 2026 — conception, production et achat média", 1, 12000.00),),
        tax_rate=0.20, tax_label="TVA 20 %", tax_note=None, po_numbers=("4500117",),
        heading=HEADINGS["fr"]["invoice"], layout="modern", language="fr", vat_display="FR 62 512 345 678",
        dataset="v2",
        designed_to_show="French invoice on a confirmed PO: touchless.",
    ),
    DocumentSpec(
        no=22, filename="22_kaffee_rechnung.pdf", channel="store_mailbox",
        sender_email="info@kaffee-und-co.de",
        subject="Rechnung 2026/136 — Kaffeelieferung",
        received_on=_nov(2, 12, 5), doc_type="invoice", party_id="P-0009",
        printed_supplier_name="Kaffee & Co OHG", bill_to_entity="VDE",
        invoice_number="2026/136", invoice_date=date(2026, 10, 30), payment_terms_days=14,
        currency="EUR",
        lines=(InvoiceLineSpec("Kaffeebohnen Espresso 1 kg", 8, 18.50),
               InvoiceLineSpec(_VOLLMILCH, 2, 14.98),
               InvoiceLineSpec("Lieferung", 1, 10.30)),
        **_KAFFEE_VAT, heading=HEADINGS["de"]["invoice"],
        printed_notes=("Vielen Dank für Ihre Bestellung!",),
        layout="compact", language="de", dataset="v2", **_STORE_B01,
        designed_to_show="Low-value non-PO invoice: DoA auto-approval by the store manager's threshold.",
    ),
    DocumentSpec(
        no=23, filename="23_nordwind_sondertransporte.pdf", channel="ap_mailbox",
        sender_email="billing@nordwind-logistics.de",
        subject="Rechnung NWL-2026-01064 — Sondertransporte Weihnachtsgeschäft",
        received_on=_nov(9, 8, 30), doc_type="invoice", party_id="P-0001",
        printed_supplier_name="Nordwind Logistics GmbH", bill_to_entity="VDE",
        invoice_number="NWL-2026-01064", invoice_date=date(2026, 11, 6), payment_terms_days=30,
        currency="EUR",
        lines=(InvoiceLineSpec("Sondertransporte Weihnachtsgeschäft — Vorlauf", 1, 7400.00),),
        tax_rate=0.19, tax_label="USt. 19 %", tax_note=None, contract_reference="CT-2025-001",
        heading=HEADINGS["de"]["invoice"],
        printed_notes=("Leistungszeitraum: 02.11.2026 – 06.11.2026.",
                       "Zusätzliche Sondertransporte auf Ihre Anforderung, nicht im monatlichen Leistungsumfang "
                       "enthalten."),
        layout="banner", language="de", dataset="v2",
        designed_to_show="Extra services beyond the contract: the contract is printed but the amount is outside "
                         "its monthly range -> no_po to the contract owner.",
    ),
    DocumentSpec(
        no=24, filename="24_lumen_unknown_po.pdf", channel="ap_mailbox",
        sender_email="accounts@lumenlighting.co.uk",
        subject="Invoice LSL-INV-5611 — PO 4500999",
        received_on=_nov(5, 9, 0), doc_type="invoice", party_id="P-0012",
        printed_supplier_name="Lumen Store Lighting Ltd", bill_to_entity="VDE",
        invoice_number="LSL-INV-5611", invoice_date=date(2026, 11, 2), payment_terms_days=30,
        currency="EUR", lines=(InvoiceLineSpec("LED strip 5m, warm white", 30, 22.00),),
        tax_rate=0.0, tax_label="VAT 0%", tax_note=_UK_EXPORT, po_numbers=("4500999",),
        layout="modern", language="en", dataset="v2",
        designed_to_show="PO number printed on the invoice does not exist.",
    ),
    DocumentSpec(
        no=25, filename="25_quickprint_invoice_vde.pdf", channel="ap_mailbox",
        sender_email="accounts@quickprint.fr",
        subject="QuickPrint invoice QP-26-1119 — PO 4500119",
        received_on=_nov(5, 11, 15), doc_type="invoice", party_id="P-0008",
        printed_supplier_name="QuickPrint SAS", bill_to_entity="VDE",
        invoice_number="QP-26-1119", invoice_date=date(2026, 11, 4), payment_terms_days=30,
        currency="EUR", lines=(InvoiceLineSpec("Window stickers, die-cut", 500, 2.20),),
        tax_rate=0.0, tax_label="VAT 0%", tax_note=_ICS_EN, po_numbers=("4500119",),
        layout="compact", language="en", dataset="v2",
        designed_to_show="Two legitimate accounts (VFR, VDE); goods not received yet.",
    ),
    DocumentSpec(
        no=26, filename="26_berliner_blumen_rechnung.pdf", channel="store_mailbox",
        sender_email="rechnung@berliner-blumen.de",
        subject="Rechnung BB-26-318 — Schaufensterdekoration November",
        received_on=_nov(5, 15, 40), doc_type="invoice", party_id=BERLINER_BLUMEN.party_id,
        printed_supplier_name="Berliner Blumen GmbH", bill_to_entity="VDE",
        invoice_number="BB-26-318", invoice_date=date(2026, 11, 5), payment_terms_days=14,
        currency="EUR", lines=(InvoiceLineSpec("Blumendekoration Schaufenster — November", 1, 268.91),),
        tax_rate=0.07, tax_label="USt. 7 %", tax_note=None, heading=HEADINGS["de"]["invoice"],
        printed_notes=("Leistungsdatum: 05.11.2026.",),
        layout="classic", language="de", vat_display="DE 305 118 442", supplier=BERLINER_BLUMEN,
        dataset="v2", **_STORE_B01,
        designed_to_show="Unknown supplier (not in the vendor master): onboarding (to-be) vs an account created "
                         "on the fly (as-is).",
    ),
]
DOCUMENT_V2_BY_NO = {d.no: d for d in DOCUMENTS_V2}
