"""The clean world: single source of truth for seed data and for the 14 sample documents.

Everything else is derived from this module:
- seed.py loads it as scenario `tobe` and derives scenario `asis` by explicit corruption rules;
- invoices_gen.py renders DOCUMENTS (the 12 of the brief plus 2 clean ones) as PDFs;
- tests/fixtures/ ground-truth extraction JSON is built from DOCUMENTS.

All companies, people, VAT IDs and bank accounts are fictional.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional


# --------------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------------


def make_iban(country: str, bban: str) -> str:
    """Build an IBAN with valid ISO 13616 check digits from a country code and BBAN."""
    rearranged = (bban + country + "00").upper()
    numeric = "".join(str(int(ch, 36)) for ch in rearranged)
    check = 98 - int(numeric) % 97
    return f"{country}{check:02d}{bban}"


def format_iban(iban: str) -> str:
    """Group an IBAN in blocks of four for printing. Non-IBAN bank strings are returned as-is."""
    if not iban[:2].isalpha() or not iban[2:4].isdigit():
        return iban
    return " ".join(iban[i : i + 4] for i in range(0, len(iban), 4))


# --------------------------------------------------------------------------------------------
# People (owners used by the gate and shown on POs / contracts)
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Person:
    name: str
    email: str
    role: str
    cost_centre: Optional[str] = None


PEOPLE: dict[str, Person] = {
    "lena": Person("Lena Fischer", "lena.fischer@velox.com", "Master Data owner"),
    "marco": Person("Marco Ruiz", "marco.ruiz@velox.com", "AP specialist"),
    "sofia": Person("Sofia Brandt", "sofia.brandt@velox.com", "Buyer, Procurement DE"),
    "julien": Person("Julien Moreau", "julien.moreau@velox.com", "Buyer, Procurement FR"),
    "daniel": Person("Daniel Price", "daniel.price@velox.com", "Buyer, Procurement US"),
    "camille": Person("Camille Martin", "camille.martin@velox.com", "Marketing manager FR", "FR-MKT-210"),
    "anna": Person("Anna Schulz", "anna.schulz@velox.com", "Marketing manager DE", "DE-MKT-210"),
    "jonas": Person("Jonas Weber", "jonas.weber@velox.com", "Store development lead DE", "DE-STD-310"),
    "tim": Person("Tim Koch", "tim.koch@velox.com", "Warehouse lead, Berlin DC", "DE-LOG-120"),
    "luc": Person("Luc Bernard", "luc.bernard@velox.com", "Store operations FR", "FR-STR-100"),
    "emily": Person("Emily Carter", "emily.carter@velox.com", "IT manager US", "US-IT-410"),
    "felix": Person("Felix Braun", "felix.braun@velox.com", "IT manager DE", "DE-IT-410"),
    "paul": Person("Paul Neumann", "paul.neumann@velox.com", "Store manager, Berlin 01", "DE-STR-B01"),
    "nina": Person("Nina Hoffmann", "nina.hoffmann@velox.com", "Logistics manager DE", "DE-LOG-120"),
    "katrin": Person("Katrin Lange", "katrin.lange@velox.com", "Facilities manager DE", "DE-FAC-150"),
    "claire": Person("Claire Dubois", "claire.dubois@velox.com", "Facilities manager FR", "FR-FAC-150"),
    "ryan": Person("Ryan Brooks", "ryan.brooks@velox.com", "Logistics manager US", "US-LOG-120"),
}

MASTER_DATA_OWNER = PEOPLE["lena"]
AP_SPECIALIST = PEOPLE["marco"]

# Requester (and cost-centre owner) for non-PO spend without a contract, per supplier and legal entity.
# Kaffee & Co supplies the Berlin 01 store; its manager owns the store cost centre.
NON_PO_REQUESTERS: dict[tuple[str, str], str] = {("P-0009", "VDE"): "paul"}


# --------------------------------------------------------------------------------------------
# Legal entities
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class LegalEntitySpec:
    code: str
    name: str
    country: str
    currency: str
    vat_id: str  # stored compact; US entity stores its EIN
    address: tuple[str, ...]


LEGAL_ENTITIES: list[LegalEntitySpec] = [
    LegalEntitySpec("VDE", "Velox Retail GmbH", "DE", "EUR", "DE298765431",
                    ("Torstrasse 140", "10119 Berlin", "Germany")),
    LegalEntitySpec("VFR", "Velox Retail SAS", "FR", "EUR", "FR48823456781",
                    ("25 rue de la Chaussee d'Antin", "75009 Paris", "France")),
    LegalEntitySpec("VUS", "Velox Retail Inc.", "US", "USD", "84-2716453",
                    ("250 Park Avenue South", "New York, NY 10003", "USA")),
]
LEGAL_ENTITY_BY_CODE = {le.code: le for le in LEGAL_ENTITIES}


# --------------------------------------------------------------------------------------------
# Parties (one per real supplier) — section 5.2 of the brief
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class PartySpec:
    party_id: str
    no: int  # supplier number 1..12 as listed in the brief
    canonical_name: str
    country: str
    vat_id: str  # compact form (EU VAT ID, UK VAT, Swiss UID, or US EIN)
    bank: str  # IBAN (compact) or, for US suppliers, "ABA <routing> ACCT <account>"
    bank_name: str
    address: tuple[str, ...]
    supplier_type: str
    agreed_terms_days: int
    email_domain: str
    brand_colour: str  # used by invoices_gen for per-supplier layouts


PARTIES: list[PartySpec] = [
    PartySpec("P-0001", 1, "Nordwind Logistics GmbH", "DE", "DE281947305",
              make_iban("DE", "500105175407324931"), "Hanseatic Commerzbank",
              ("Hafenstrasse 12", "20457 Hamburg", "Germany"),
              "3PL carrier — recurring contract", 30, "nordwind-logistics.de", "#1F3A5F"),
    PartySpec("P-0002", 2, "Bright Agency SARL", "FR", "FR62512345678",
              make_iban("FR", "30004005500001234567890"), "BNP Paribas",
              ("18 rue du Faubourg Saint-Antoine", "75011 Paris", "France"),
              "Marketing agency — services with PO", 45, "bright-agency.fr", "#C2185B"),
    PartySpec("P-0003", 3, "FitOut Partners Ltd", "GB", "GB293847561",
              make_iban("GB", "BARC20038412345678"), "Barclays Bank",
              ("14 Canal Street", "Manchester M1 3HE", "United Kingdom"),
              "Store fit-out contractor — milestone invoices with PO", 30, "fitoutpartners.co.uk", "#2E7D32"),
    PartySpec("P-0004", 4, "Cleanspace Facilities BV", "NL", "NL859374612B01",
              make_iban("NL", "INGB0007364521"), "ING Bank",
              ("Keizersgracht 221", "1016 DV Amsterdam", "Netherlands"),
              "Facilities management — recurring contract", 30, "cleanspace.nl", "#00897B"),
    PartySpec("P-0005", 5, "Shopsys Software Inc.", "US", "47-3829105",
              "ABA 121000248 ACCT 4839201756", "Pacific Western Bank",
              ("500 Howard Street", "San Francisco, CA 94105", "USA"),
              "IT SaaS — annual subscription with PO", 30, "shopsys.io", "#5E35B1"),
    PartySpec("P-0006", 6, "Atlas Displays SL", "ES", "ESB86419273",
              make_iban("ES", "20852066623456789011"), "Banco Sabadell",
              ("Calle de Alcala 145", "28009 Madrid", "Spain"),
              "Store fixtures (goods) — goods with PO and receipt", 60, "atlasdisplays.es", "#E65100"),
    PartySpec("P-0007", 7, "Metro Media GmbH", "DE", "DE318273645",
              make_iban("DE", "100700240912345678"), "Deutsche Bank",
              ("Friedrichstrasse 68", "10117 Berlin", "Germany"),
              "Regional marketing — services with PO", 30, "metromedia.de", "#C62828"),
    PartySpec("P-0008", 8, "QuickPrint SAS", "FR", "FR73491234567",
              make_iban("FR", "10107001180009876543221"), "Credit Lyonnais",
              ("42 avenue Jean Jaures", "69007 Lyon", "France"),
              "Marketing collateral (goods) — goods with PO", 30, "quickprint.fr", "#0277BD"),
    PartySpec("P-0009", 9, "Kaffee & Co OHG", "DE", "DE274619385",
              make_iban("DE", "120300001020304050"), "Berliner Sparkasse",
              ("Oranienstrasse 25", "10999 Berlin", "Germany"),
              "Small local supplier to a Berlin store — no PO", 14, "kaffee-und-co.de", "#6D4C41"),
    PartySpec("P-0010", 10, "SecureNet AG", "CH", "CHE-419.287.563",
              make_iban("CH", "00700110008765432"), "Zuercher Kantonalbank",
              ("Bahnhofstrasse 10", "8001 Zurich", "Switzerland"),
              "IT vendor — service with PO", 30, "securenet.ch", "#455A64"),
    PartySpec("P-0011", 11, "Harbor Freight Forwarders Inc.", "US", "36-4817290",
              "ABA 021000021 ACCT 7730045918", "Hudson Commercial Bank",
              ("1 Marine Terminal Road", "Newark, NJ 07114", "USA"),
              "3PL — recurring contract", 30, "harborff.com", "#37474F"),
    PartySpec("P-0012", 12, "Lumen Store Lighting Ltd", "GB", "GB618273940",
              make_iban("GB", "LOYD30963412345678"), "Lloyds Bank",
              ("7 Brindley Place", "Birmingham B1 2JB", "United Kingdom"),
              "Store lighting (goods) — goods with PO", 30, "lumenlighting.co.uk", "#F9A825"),
]
PARTY_BY_ID = {p.party_id: p for p in PARTIES}
PARTY_BY_NO = {p.no: p for p in PARTIES}


# --------------------------------------------------------------------------------------------
# Clean vendor master (scenario `tobe`) — one account per party per legal entity it serves
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class AccountSpec:
    account_id: str
    party_id: Optional[str]
    legal_entity_code: str
    display_name: str
    vat_id: Optional[str]
    iban: Optional[str]
    payment_terms_days: int
    created_by: str
    created_on: date
    status: str = "active"
    notes: Optional[str] = None
    rules: tuple[str, ...] = ()  # dirty-world rules applied (asis only)


def _clean_account(account_id: str, party_id: str, entity: str, created_on: date) -> AccountSpec:
    p = PARTY_BY_ID[party_id]
    creator = {"VDE": "finance.de", "VFR": "finance.fr", "VUS": "finance.us"}[entity]
    return AccountSpec(
        account_id=account_id,
        party_id=party_id,
        legal_entity_code=entity,
        display_name=p.canonical_name,
        vat_id=p.vat_id,
        iban=p.bank,
        payment_terms_days=p.agreed_terms_days,
        created_by=creator,
        created_on=created_on,
        notes=None,
    )


CLEAN_ACCOUNTS: list[AccountSpec] = [
    _clean_account("V-000101", "P-0001", "VDE", date(2019, 3, 4)),
    _clean_account("V-000102", "P-0002", "VFR", date(2020, 6, 15)),
    _clean_account("V-000103", "P-0003", "VDE", date(2025, 11, 3)),
    _clean_account("V-000104", "P-0004", "VDE", date(2021, 1, 20)),
    _clean_account("V-000105", "P-0005", "VUS", date(2022, 9, 1)),
    _clean_account("V-000106", "P-0006", "VDE", date(2023, 2, 14)),
    _clean_account("V-000107", "P-0007", "VDE", date(2022, 4, 5)),
    _clean_account("V-000108", "P-0008", "VFR", date(2021, 10, 12)),
    _clean_account("V-000109", "P-0009", "VDE", date(2024, 5, 21)),
    _clean_account("V-000110", "P-0010", "VDE", date(2023, 8, 30)),
    _clean_account("V-000111", "P-0011", "VUS", date(2022, 7, 18)),
    _clean_account("V-000112", "P-0012", "VDE", date(2025, 3, 10)),
    _clean_account("V-000113", "P-0004", "VFR", date(2025, 9, 1)),
    _clean_account("V-000114", "P-0005", "VDE", date(2024, 1, 8)),
    _clean_account("V-000115", "P-0006", "VFR", date(2025, 6, 2)),
    _clean_account("V-000116", "P-0008", "VDE", date(2025, 4, 22)),
]
CLEAN_ACCOUNT_BY_ID = {a.account_id: a for a in CLEAN_ACCOUNTS}


# --------------------------------------------------------------------------------------------
# Contracts (recurring). Contracts are per legal entity, so Cleanspace (VDE and VFR) has two rows:
# 3 contracts with suppliers -> 4 contract rows.
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ContractSpec:
    contract_id: str
    party_id: str
    legal_entity_code: str
    description: str
    payment_terms_days: int
    recurring: bool
    expected_monthly_min: float
    expected_monthly_max: float
    currency: str
    category: str
    owner: str  # key into PEOPLE


CONTRACTS: list[ContractSpec] = [
    ContractSpec("CT-2025-001", "P-0001", "VDE", "3PL warehousing and store replenishment transport, Germany",
                 30, True, 20000.0, 26000.0, "EUR", "logistics", "nina"),
    ContractSpec("CT-2025-002", "P-0004", "VDE", "Store cleaning and facilities services, Germany",
                 30, True, 3000.0, 3400.0, "EUR", "facilities", "katrin"),
    ContractSpec("CT-2025-003", "P-0004", "VFR", "Store cleaning and facilities services, France",
                 30, True, 3000.0, 3400.0, "EUR", "facilities", "claire"),
    ContractSpec("CT-2025-004", "P-0011", "VUS", "Freight forwarding and customs brokerage, USA",
                 30, True, 15000.0, 19000.0, "USD", "logistics", "ryan"),
]


# --------------------------------------------------------------------------------------------
# Purchase orders and receipts. `in_asis` models weak PO discipline: only ~40% of the POs were
# raised in the ERP before the invoice arrived in the as-is world (rule D6 in ASSUMPTIONS.md).
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class POLineSpec:
    line_no: int
    description: str
    qty: float
    unit_price: float
    receipt_required: bool = True

    @property
    def amount(self) -> float:
        return round(self.qty * self.unit_price, 2)


@dataclass(frozen=True)
class POSpec:
    po_number: str
    legal_entity_code: str
    vendor_account_id: str
    category: str  # goods | service
    description: str
    requester: str  # key into PEOPLE
    buyer: str  # key into PEOPLE
    order_date: date
    currency: str
    lines: tuple[POLineSpec, ...]
    in_asis: bool

    @property
    def party_id(self) -> str:
        return CLEAN_ACCOUNT_BY_ID[self.vendor_account_id].party_id  # type: ignore[return-value]

    @property
    def total(self) -> float:
        return round(sum(line.amount for line in self.lines), 2)

    @property
    def cost_centre(self) -> str:
        return PEOPLE[self.requester].cost_centre or ""


PURCHASE_ORDERS: list[POSpec] = [
    POSpec("4500101", "VDE", "V-000103", "service", "Store fit-out Hamburg — milestone 1 (demolition and electrical)",
           "jonas", "sofia", date(2026, 6, 15), "EUR",
           (POLineSpec(1, "Store fit-out Hamburg — milestone 1: demolition and electrical works", 1, 40000.00),), True),
    POSpec("4500105", "VFR", "V-000115", "goods", "Wall display units for Paris stores",
           "luc", "julien", date(2026, 8, 20), "EUR",
           (POLineSpec(1, "Wall display unit WD-120, oak finish", 80, 42.00),), True),
    POSpec("4500107", "VDE", "V-000114", "service", "POS integration add-on, annual licence (Germany)",
           "felix", "sofia", date(2026, 9, 1), "EUR",
           (POLineSpec(1, "POS integration add-on — annual licence (Germany)", 1, 2400.00),), False),
    POSpec("4500109", "VDE", "V-000106", "goods", "Wall display units for German stores",
           "jonas", "sofia", date(2026, 9, 1), "EUR",
           (POLineSpec(1, "Wall display unit WD-120, oak finish", 150, 42.00),), False),
    POSpec("4500112", "VDE", "V-000112", "goods", "Store lighting refresh, Hamburg",
           "jonas", "sofia", date(2026, 9, 3), "EUR",
           (POLineSpec(1, "LED track spotlight 30W", 80, 40.00),
            POLineSpec(2, "LED panel 600x600 40W", 40, 50.00)), False),
    POSpec("4500114", "VFR", "V-000108", "goods", "In-store collateral, autumn campaign",
           "camille", "julien", date(2026, 9, 7), "EUR",
           (POLineSpec(1, "A2 in-store posters, 4-colour", 400, 3.50),
            POLineSpec(2, "A5 leaflets, double-sided", 10000, 0.10)), True),
    POSpec("4500117", "VFR", "V-000102", "service", "Autumn campaign 2026 (France)",
           "camille", "julien", date(2026, 8, 25), "EUR",
           (POLineSpec(1, "Autumn campaign 2026 — creative concept, production and media planning", 1, 12000.00),), True),
    POSpec("4500119", "VDE", "V-000116", "goods", "Window stickers for German stores",
           "anna", "sofia", date(2026, 9, 15), "EUR",
           (POLineSpec(1, "Window stickers, die-cut", 500, 2.20),), False),
    POSpec("4500121", "VFR", "V-000102", "service", "Q4 social media campaign (France)",
           "camille", "julien", date(2026, 9, 18), "EUR",
           (POLineSpec(1, "Q4 social media campaign — content and community management", 1, 6000.00),), False),
    POSpec("4500123", "VDE", "V-000103", "service", "Store fit-out Hamburg — milestone 2 (shopfitting completion)",
           "jonas", "sofia", date(2026, 6, 15), "EUR",
           (POLineSpec(1, "Store fit-out Hamburg — milestone 2: shopfitting and fixtures installation", 1, 48000.00),), False),
    POSpec("4500126", "VDE", "V-000107", "service", "Berlin out-of-home campaign, September 2026",
           "anna", "sofia", date(2026, 8, 10), "EUR",
           (POLineSpec(1, "Berlin out-of-home campaign September 2026 — 40 billboard sites", 1, 7500.00),), True),
    POSpec("4500128", "VDE", "V-000110", "service", "Managed firewall service Q3 2026",
           "felix", "sofia", date(2026, 7, 1), "EUR",
           (POLineSpec(1, "Managed firewall service Q3 2026", 1, 4800.00),), False),
    POSpec("4500130", "VDE", "V-000110", "service", "Penetration test, e-commerce platform",
           "felix", "sofia", date(2026, 9, 21), "EUR",
           (POLineSpec(1, "Penetration test — e-commerce platform", 1, 3500.00),), False),
    POSpec("4500131", "VUS", "V-000105", "service", "Shopsys Commerce Cloud subscription Oct 2026 – Sep 2027",
           "emily", "daniel", date(2026, 9, 10), "USD",
           (POLineSpec(1, "Shopsys Commerce Cloud — annual subscription (Oct 2026 – Sep 2027)", 1, 9600.00),), True),
]
PO_BY_NUMBER = {po.po_number: po for po in PURCHASE_ORDERS}


@dataclass(frozen=True)
class ReceiptSpec:
    receipt_id: str
    po_number: str
    line_no: int
    qty_received: float
    received_on: date
    received_by: str  # key into PEOPLE
    kind: str  # product_receipt | service_confirmation


RECEIPTS: list[ReceiptSpec] = [
    ReceiptSpec("PR-26-0412", "4500101", 1, 1, date(2026, 8, 14), "jonas", "service_confirmation"),
    ReceiptSpec("PR-26-0433", "4500105", 1, 80, date(2026, 9, 10), "luc", "product_receipt"),
    ReceiptSpec("PR-26-0451", "4500109", 1, 150, date(2026, 9, 24), "tim", "product_receipt"),
    ReceiptSpec("PR-26-0456", "4500112", 1, 80, date(2026, 9, 29), "tim", "product_receipt"),
    ReceiptSpec("PR-26-0456", "4500112", 2, 20, date(2026, 9, 29), "tim", "product_receipt"),
    ReceiptSpec("PR-26-0447", "4500114", 1, 400, date(2026, 9, 25), "luc", "product_receipt"),
    ReceiptSpec("PR-26-0447", "4500114", 2, 10000, date(2026, 9, 25), "luc", "product_receipt"),
    ReceiptSpec("PR-26-0449", "4500117", 1, 1, date(2026, 9, 26), "camille", "service_confirmation"),
    ReceiptSpec("PR-26-0458", "4500126", 1, 1, date(2026, 9, 30), "anna", "service_confirmation"),
    ReceiptSpec("PR-26-0459", "4500128", 1, 1, date(2026, 9, 30), "felix", "service_confirmation"),
    ReceiptSpec("PR-26-0461", "4500131", 1, 1, date(2026, 10, 1), "emily", "service_confirmation"),
]
# Note: PO 4500123 (FitOut milestone 2) deliberately has NO service confirmation.


# --------------------------------------------------------------------------------------------
# The inbound documents: the 12 of brief section 5.6 plus two clean ones (13, 14)
# --------------------------------------------------------------------------------------------

AP_MAILBOX = "ap@velox.com"
STORE_MAILBOX = "store.berlin01@velox.com"
MAILBOX_BY_CHANNEL = {"ap_mailbox": AP_MAILBOX, "store_mailbox": STORE_MAILBOX}


@dataclass(frozen=True)
class InvoiceLineSpec:
    description: str
    quantity: float
    unit_price: float

    @property
    def amount(self) -> float:
        return round(self.quantity * self.unit_price, 2)


@dataclass(frozen=True)
class DocumentSpec:
    no: int
    filename: str
    channel: str  # ap_mailbox | store_mailbox
    sender_email: str
    subject: str
    received_on: datetime
    doc_type: str  # invoice | credit_note
    party_id: str
    printed_supplier_name: str  # the name as printed on this document (may differ from canonical)
    bill_to_entity: str  # legal entity code printed in the bill-to block
    invoice_number: str
    invoice_date: date
    payment_terms_days: Optional[int]  # as printed on the document (None for credit notes)
    currency: str
    lines: tuple[InvoiceLineSpec, ...]
    tax_rate: float
    tax_label: str  # e.g. "VAT 19%" or "VAT 0% — reverse charge"
    tax_note: Optional[str]  # legal mention printed under totals
    po_numbers: tuple[str, ...] = ()
    contract_reference: Optional[str] = None
    referenced_invoice_number: Optional[str] = None
    bill_to_attention: Optional[str] = None  # e.g. "Store Berlin 01" with a different address
    bill_to_address: Optional[tuple[str, ...]] = None  # overrides the entity address
    print_bill_to_vat: bool = True
    heading: str = "INVOICE"
    watermark: Optional[str] = None  # e.g. "COPY"
    printed_notes: tuple[str, ...] = ()  # free text printed on the document
    layout: str = "classic"  # classic | banner | modern | compact (see invoices_gen.py)
    designed_to_show: str = ""

    @property
    def mailbox(self) -> str:
        return MAILBOX_BY_CHANNEL[self.channel]

    @property
    def party(self) -> PartySpec:
        return PARTY_BY_ID[self.party_id]

    @property
    def bill_to(self) -> LegalEntitySpec:
        return LEGAL_ENTITY_BY_CODE[self.bill_to_entity]

    @property
    def net_total(self) -> float:
        return round(sum(line.amount for line in self.lines), 2)

    @property
    def tax_total(self) -> float:
        return round(self.net_total * self.tax_rate, 2)

    @property
    def gross_total(self) -> float:
        return round(self.net_total + self.tax_total, 2)

    @property
    def due_date(self) -> Optional[date]:
        if self.payment_terms_days is None:
            return None
        return self.invoice_date + timedelta(days=self.payment_terms_days)


_NW_LINES = (
    InvoiceLineSpec("Warehousing and fulfilment, Berlin DC — September 2026", 1, 14200.00),
    InvoiceLineSpec("Store replenishment transport, Germany — September 2026", 1, 9200.00),
)

DOCUMENTS: list[DocumentSpec] = [
    DocumentSpec(
        no=1, filename="01_nordwind_invoice.pdf", channel="ap_mailbox",
        sender_email="billing@nordwind-logistics.de",
        subject="Invoice NWL-2026-00913 — September 2026 logistics services",
        received_on=datetime(2026, 10, 1, 8, 42), doc_type="invoice", party_id="P-0001",
        printed_supplier_name="Nordwind Logistics GmbH", bill_to_entity="VDE",
        invoice_number="NWL-2026-00913", invoice_date=date(2026, 9, 30), payment_terms_days=14,
        currency="EUR", lines=_NW_LINES, tax_rate=0.19, tax_label="VAT 19%", tax_note=None,
        contract_reference="CT-2025-001", layout="banner",
        designed_to_show="Recurring contract match (to-be) vs 'no PO' email loop (as-is). "
                         "Invoice terms 14 days differ from the contract (30).",
    ),
    DocumentSpec(
        no=2, filename="02_nordwind_reminder_copy.pdf", channel="store_mailbox",
        sender_email="ar@nordwind-logistics.de",
        subject="Reminder — copy of invoice NWL-2026-00913",
        received_on=datetime(2026, 10, 15, 16, 5), doc_type="invoice", party_id="P-0001",
        printed_supplier_name="NORDWIND LOGISTICS", bill_to_entity="VDE",
        invoice_number="NWL-2026-00913", invoice_date=date(2026, 9, 30), payment_terms_days=14,
        currency="EUR", lines=_NW_LINES, tax_rate=0.19, tax_label="VAT 19%", tax_note=None,
        contract_reference="CT-2025-001", heading="PAYMENT REMINDER — COPY OF INVOICE", watermark="COPY",
        printed_notes=("This is a copy of invoice NWL-2026-00913 issued on 30 September 2026.",
                       "Our records show it as unpaid. Please arrange payment or contact our accounts receivable team."),
        layout="banner",
        designed_to_show="Duplicate blocked (to-be) vs posted twice on another vendor account (as-is). "
                         "Sent from the receivables system with the short name 'NORDWIND LOGISTICS'.",
    ),
    DocumentSpec(
        no=3, filename="03_bright_agency_invoice.pdf", channel="ap_mailbox",
        sender_email="billing@bright-agency.fr",
        subject="Invoice INV-2026-0457 — Autumn campaign — PO 4500117",
        received_on=datetime(2026, 10, 1, 9, 15), doc_type="invoice", party_id="P-0002",
        printed_supplier_name="Bright Agency SARL", bill_to_entity="VFR",
        invoice_number="INV-2026-0457", invoice_date=date(2026, 9, 29), payment_terms_days=45,
        currency="EUR",
        lines=(InvoiceLineSpec("Autumn campaign 2026 — creative concept, production and media planning", 1, 12000.00),),
        tax_rate=0.20, tax_label="VAT 20%", tax_note=None, po_numbers=("4500117",), layout="modern",
        designed_to_show="Clean PO + service confirmation -> touchless.",
    ),
    DocumentSpec(
        no=4, filename="04_bright_agency_credit_note.pdf", channel="ap_mailbox",
        sender_email="billing@bright-agency.fr",
        subject="Credit note CN-2026-0031 for invoice INV-2026-0457",
        received_on=datetime(2026, 10, 2, 11, 30), doc_type="credit_note", party_id="P-0002",
        printed_supplier_name="Bright Agency", bill_to_entity="VFR",
        invoice_number="CN-2026-0031", invoice_date=date(2026, 10, 2), payment_terms_days=None,
        currency="EUR",
        lines=(InvoiceLineSpec("Agreed discount on Autumn campaign 2026 (invoice INV-2026-0457)", 1, -1500.00),),
        tax_rate=0.20, tax_label="VAT 20%", tax_note=None, referenced_invoice_number="INV-2026-0457",
        heading="CREDIT NOTE",
        printed_notes=("Credit note relating to invoice INV-2026-0457 of 29 September 2026.",
                       "Discount agreed for the delayed campaign launch. Amounts are credited to your account."),
        layout="modern",
        designed_to_show="Credit applied to invoice #3 (to-be) vs unapplied on a different vendor account (as-is). "
                         "Issued from a different template with the short name 'Bright Agency'.",
    ),
    DocumentSpec(
        no=5, filename="05_fitout_partners_invoice.pdf", channel="ap_mailbox",
        sender_email="accounts@fitoutpartners.co.uk",
        subject="Invoice 2026-091 — Hamburg milestone 2 — PO 4500123",
        received_on=datetime(2026, 10, 1, 14, 2), doc_type="invoice", party_id="P-0003",
        printed_supplier_name="FitOut Partners Ltd", bill_to_entity="VDE",
        invoice_number="2026-091", invoice_date=date(2026, 9, 30), payment_terms_days=30,
        currency="EUR",
        lines=(InvoiceLineSpec("Store fit-out Hamburg — milestone 2: shopfitting and fixtures installation", 1, 48000.00),),
        tax_rate=0.0, tax_label="VAT 0%",
        tax_note="Reverse charge: customer to account for VAT (Article 196, Directive 2006/112/EC).",
        po_numbers=("4500123",), layout="classic",
        designed_to_show="PO exists but no service confirmation -> exception to the receiver with SLA (to-be) "
                         "vs email loop (as-is).",
    ),
    DocumentSpec(
        no=6, filename="06_atlas_displays_invoice.pdf", channel="ap_mailbox",
        sender_email="invoices@atlasdisplays.es",
        subject="Invoice AD-2026/0788 — PO 4500109",
        received_on=datetime(2026, 10, 2, 10, 21), doc_type="invoice", party_id="P-0006",
        printed_supplier_name="Atlas Displays SL", bill_to_entity="VDE",
        invoice_number="AD-2026/0788", invoice_date=date(2026, 9, 28), payment_terms_days=30,
        currency="EUR",
        lines=(InvoiceLineSpec("Wall display unit WD-120, oak finish", 150, 44.00),),
        tax_rate=0.0, tax_label="VAT 0%",
        tax_note="Intra-Community supply, exempt under Article 138, Directive 2006/112/EC.",
        po_numbers=("4500109",), layout="classic",
        designed_to_show="Unit price 44.00 vs PO 42.00: price mismatch outside tolerance -> exception to the buyer. "
                         "Invoice terms 30 days differ from the master (60).",
    ),
    DocumentSpec(
        no=7, filename="07_shopsys_invoice.pdf", channel="ap_mailbox",
        sender_email="billing@shopsys.io",
        subject="Shopsys invoice SS-100482 (PO 4500131)",
        received_on=datetime(2026, 10, 1, 7, 55), doc_type="invoice", party_id="P-0005",
        printed_supplier_name="Shopsys Software Inc.", bill_to_entity="VUS",
        invoice_number="SS-100482", invoice_date=date(2026, 10, 1), payment_terms_days=30,
        currency="USD",
        lines=(InvoiceLineSpec("Shopsys Commerce Cloud — annual subscription (Oct 2026 – Sep 2027)", 1, 9600.00),),
        tax_rate=0.0, tax_label="Sales tax", tax_note="Sales tax: not applicable to this service.",
        po_numbers=("4500131",), layout="modern",
        designed_to_show="Touchless (PO + service confirmed).",
    ),
    DocumentSpec(
        no=8, filename="08_metro_media_invoice.pdf", channel="store_mailbox",
        sender_email="invoices@metromedia.de",
        subject="Invoice MM-2026-212 (PO 4500126)",
        received_on=datetime(2026, 10, 2, 13, 47), doc_type="invoice", party_id="P-0007",
        printed_supplier_name="Metro Media GmbH", bill_to_entity="VFR",
        invoice_number="MM-2026-212", invoice_date=date(2026, 9, 30), payment_terms_days=30,
        currency="EUR",
        lines=(InvoiceLineSpec("Berlin out-of-home campaign September 2026 — 40 billboard sites", 1, 7500.00),),
        tax_rate=0.0, tax_label="VAT 0%",
        tax_note="Reverse charge: VAT to be accounted for by the recipient (Article 196, Directive 2006/112/EC).",
        po_numbers=("4500126",), layout="banner",
        designed_to_show="Billed to Velox Retail SAS (VFR) but PO 4500126 belongs to VDE: wrong legal entity "
                         "-> exception (to-be) vs silent wrong posting (as-is).",
    ),
    DocumentSpec(
        no=9, filename="09_quickprint_invoice.pdf", channel="ap_mailbox",
        sender_email="accounts@quickprint.fr",
        subject="QuickPrint invoice QP-26-1043",
        received_on=datetime(2026, 10, 1, 10, 33), doc_type="invoice", party_id="P-0008",
        printed_supplier_name="QuickPrint SAS", bill_to_entity="VFR",
        invoice_number="QP-26-1043", invoice_date=date(2026, 9, 29), payment_terms_days=30,
        currency="EUR",
        lines=(InvoiceLineSpec("A2 in-store posters, 4-colour", 400, 3.50),
               InvoiceLineSpec("A5 leaflets, double-sided", 10000, 0.10)),
        tax_rate=0.20, tax_label="VAT 20%", tax_note=None, po_numbers=("4500114",), layout="compact",
        designed_to_show="Touchless goods 3-way match (PO, receipt, invoice).",
    ),
    DocumentSpec(
        no=10, filename="10_kaffee_und_co_invoice.pdf", channel="store_mailbox",
        sender_email="info@kaffee-und-co.de",
        subject="Invoice 2026/117 — coffee supply September",
        received_on=datetime(2026, 10, 1, 12, 10), doc_type="invoice", party_id="P-0009",
        printed_supplier_name="Kaffee & Co OHG", bill_to_entity="VDE",
        invoice_number="2026/117", invoice_date=date(2026, 9, 30), payment_terms_days=14,
        currency="EUR",
        lines=(InvoiceLineSpec("Coffee beans, espresso blend 1 kg", 6, 18.50),
               InvoiceLineSpec("Oat milk 1 l, carton of 12", 2, 14.98),
               InvoiceLineSpec("Delivery", 1, 10.30)),
        tax_rate=0.19, tax_label="VAT 19%", tax_note=None,
        bill_to_attention="Store Berlin 01",
        bill_to_address=("Rosenthaler Strasse 40", "10178 Berlin", "Germany"),
        print_bill_to_vat=False, layout="compact",
        designed_to_show="Low-value non-PO invoice sent to the store: to-be auto-approves under the DoA threshold; "
                         "as-is waits 7+ days in the store mailbox.",
    ),
    DocumentSpec(
        no=11, filename="11_cleanspace_invoice.pdf", channel="ap_mailbox",
        sender_email="invoicing@cleanspace.nl",
        subject="Invoice CSF-26-10355 — cleaning services France, September 2026",
        received_on=datetime(2026, 10, 2, 9, 5), doc_type="invoice", party_id="P-0004",
        printed_supplier_name="Cleanspace Facilities BV", bill_to_entity="VFR",
        invoice_number="CSF-26-10355", invoice_date=date(2026, 9, 30), payment_terms_days=30,
        currency="EUR",
        lines=(InvoiceLineSpec("Store cleaning services France — September 2026 (6 stores)", 1, 3200.00),),
        tax_rate=0.0, tax_label="VAT 0%",
        tax_note="Reverse charge: VAT to be accounted for by the recipient (Article 196, Directive 2006/112/EC).",
        contract_reference="CT-2025-003", layout="classic",
        designed_to_show="No PO, recurring contract match (amount inside the expected monthly range).",
    ),
    DocumentSpec(
        no=12, filename="12_lumen_lighting_invoice.pdf", channel="ap_mailbox",
        sender_email="accounts@lumenlighting.co.uk",
        subject="Invoice LSL-INV-5521 — PO 4500112",
        received_on=datetime(2026, 10, 5, 8, 30), doc_type="invoice", party_id="P-0012",
        printed_supplier_name="Lumen Store Lighting Ltd", bill_to_entity="VDE",
        invoice_number="LSL-INV-5521", invoice_date=date(2026, 9, 30), payment_terms_days=30,
        currency="EUR",
        lines=(InvoiceLineSpec("LED track spotlight 30W", 80, 40.00),
               InvoiceLineSpec("LED panel 600x600 40W", 40, 50.00)),
        tax_rate=0.0, tax_label="VAT 0%", tax_note="Export of goods outside the UK: zero-rated.",
        po_numbers=("4500112",), layout="modern",
        designed_to_show="120 units invoiced, only 100 received (line 2: 40 invoiced, 20 received): "
                         "quantity mismatch -> exception to the receiver, then the buyer; partial.",
    ),
    # Documents 13 and 14 are not in the brief's table: two clean, everyday invoices added so that the
    # sample is not made of difficult cases only (decision of 24 Sep 2026; see docs/ASSUMPTIONS.md).
    DocumentSpec(
        no=13, filename="13_securenet_invoice.pdf", channel="ap_mailbox",
        sender_email="billing@securenet.ch",
        subject="Invoice SN-2026-3307 — PO 4500128",
        received_on=datetime(2026, 10, 2, 14, 20), doc_type="invoice", party_id="P-0010",
        printed_supplier_name="SecureNet AG", bill_to_entity="VDE",
        invoice_number="SN-2026-3307", invoice_date=date(2026, 10, 1), payment_terms_days=30,
        currency="EUR",
        lines=(InvoiceLineSpec("Managed firewall service Q3 2026", 1, 4800.00),),
        tax_rate=0.0, tax_label="VAT 0%",
        tax_note="Reverse charge: VAT to be accounted for by the recipient (services supplied from Switzerland).",
        po_numbers=("4500128",), layout="classic",
        designed_to_show="Clean PO + service confirmation -> touchless (to-be); the PO was never keyed in the "
                         "as-is ERP -> email loop.",
    ),
    DocumentSpec(
        no=14, filename="14_harbor_freight_invoice.pdf", channel="ap_mailbox",
        sender_email="billing@harborff.com",
        subject="Invoice HFF-2026-0930 — September 2026 freight services",
        received_on=datetime(2026, 10, 1, 16, 40), doc_type="invoice", party_id="P-0011",
        printed_supplier_name="Harbor Freight Forwarders Inc.", bill_to_entity="VUS",
        invoice_number="HFF-2026-0930", invoice_date=date(2026, 9, 30), payment_terms_days=30,
        currency="USD",
        lines=(InvoiceLineSpec("Ocean freight forwarding — September 2026", 1, 11400.00),
               InvoiceLineSpec("Customs brokerage and drayage — September 2026", 1, 5850.00)),
        tax_rate=0.0, tax_label="Sales tax", tax_note="Freight and customs services: no sales tax charged.",
        contract_reference="CT-2025-004", layout="banner",
        designed_to_show="No PO, recurring contract match inside the monthly range -> touchless (to-be) vs "
                         "email loop (as-is).",
    ),
]
DOCUMENT_BY_NO = {d.no: d for d in DOCUMENTS}
