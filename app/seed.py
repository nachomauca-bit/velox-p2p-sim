"""Seed both scenarios from one source of truth (app/world.py).

- `tobe` = the clean world, loaded as defined.
- `asis` = the clean world after explicit corruption rules D1–D6 (documented in docs/ASSUMPTIONS.md).

Also loads a set of sample documents into the simulated mailboxes: the 12 case documents (dataset v1, app/world.py)
or the 26 documents of test set v2 (app/world_v2.py, docs/TEST_SET_V2.md), and prepares the demo (reset_demo: both
scenarios loaded and run offline, FitOut held back for "Receive next email").

CLI:  python -m app.seed            # drop + create tables, seed both scenarios, generate PDFs, reset the demo
                                     # (case documents in both inboxes, both scenarios run; cache / fixtures only)
      python -m app.seed --dataset v2   # test set v2 instead, loaded and run in both scenarios
"""
from __future__ import annotations

import argparse
import hashlib
import re
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Optional

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app import config, sim, world
from app.config import BASE_DIR, INVOICES_DIR, SCENARIOS
from app.models import (
    Contract,
    CreditNoteApplication,
    GateDecision,
    InboundDocument,
    LegalEntity,
    Party,
    PendingVendorInvoice,
    ProductReceipt,
    PurchaseOrder,
    PurchaseOrderLine,
    Run,
    VendorAccount,
)
from app.world import AccountSpec, make_iban

# --------------------------------------------------------------------------------------------
# Dirty-world corruption rules (scenario `asis`). Each rule is a small pure function over the
# list of account specs; parameters are explicit so the result is fully deterministic.
# --------------------------------------------------------------------------------------------

# One-line description per rule (shown as tooltips in the vendor master and PO pages).
RULE_DESCRIPTIONS = {
    "D1": "D1 spelling duplicate: extra account for the same supplier and entity with another spelling, "
          "IBAN and terms; not linked to the party.",
    "D2": "D2 cross-entity spread: account in an entity the supplier does not serve, created by a store user.",
    "D3": "D3 terms drift: payment terms differ from the contract / supplier agreement.",
    "D4": "D4 missing identifiers: tax ID or IBAN left empty.",
    "D5": "D5 inactive leftover: old account under a previous company name.",
    "D6": "D6 weak PO discipline: the requester gave the supplier a PO number, but the PO was never keyed "
          "and approved in the ERP; only 6 of 12 POs exist in as-is.",
}

# D1 — spelling duplicates: suppliers 1, 2, 4, 5, 7, 8 get extra accounts in the same legal entity with a
# different spelling, a different IBAN and different terms. No party link. (V-000129 and V-000130 keep the as-is at
# 28 records for 12 suppliers, the deck's 2.33, after the clean master dropped two per-entity records.)
# (new account, cloned from, display name, IBAN, terms, created_by, created_on, notes)
D1_DUPLICATES = [
    ("V-000117", "V-000101", "NORDWIND LOGISTICS", make_iban("DE", "200505501234987650"), 14,
     "ap.temp", date(2023, 11, 6), "Created to pay an urgent reminder"),
    ("V-000118", "V-000101", "Nordwind Logistik GmbH", make_iban("DE", "760260000987654321"), 60,
     "store.berlin01", date(2024, 8, 19), "Created by the Berlin store for a delivery invoice"),
    ("V-000119", "V-000102", "Bright Agency", make_iban("FR", "30002005500000157841025"), 60,
     "finance.fr", date(2024, 2, 27), "Created from a credit note email"),
    ("V-000120", "V-000104", "Clean Space Facilities B.V.", make_iban("NL", "ABNA0417164300"), 14,
     "store.berlin01", date(2022, 5, 9), "Created by a store for monthly cleaning"),
    ("V-000121", "V-000113", "CLEANSPACE FACILITIES", make_iban("NL", "RABO0301234567"), 45,
     "store.paris02", date(2025, 10, 14), "Created by a Paris store"),
    ("V-000122", "V-000107", "Metro Media", make_iban("DE", "100208900034567812"), 45,
     "ap.temp", date(2023, 3, 1), "Temporary account for a campaign invoice"),
    ("V-000123", "V-000107", "Metro-Media GmbH", make_iban("DE", "100100100765432109"), 60,
     "store.berlin01", date(2024, 11, 25), "Created by the Berlin store"),
    ("V-000129", "V-000105", "SHOPSYS SOFTWARE", "ABA 121000248 ACCT 5520193847", 45,
     "finance.us", date(2024, 6, 3), "Created for a renewal invoice"),
    ("V-000130", "V-000108", "QuickPrint S.A.S.", make_iban("FR", "10107001180001234567893"), 60,
     "store.paris02", date(2025, 3, 17), "Created by a Paris store for leaflets"),
]

# D2 — cross-entity spread: supplier 1 also has an account in VFR with terms 45, created by a store user.
D2_CROSS_ENTITY = ("V-000124", "V-000101", "VFR", 45, "store.paris02", date(2025, 2, 11),
                   "Created by a Paris store to pay a delivery")

# D3 — terms drift: five accounts whose terms differ from the agreed terms (contract / supplier agreement).
# Fixed list (spread across entities) for reproducibility.
D3_TERMS_DRIFT = {"V-000103": 60, "V-000105": 45, "V-000106": 30, "V-000110": 14, "V-000113": 60}

# D5 — inactive leftovers with old names. No party link.
# (account, party it belonged to, entity, old name, VAT, bank, terms, created_by, created_on, notes)
D5_INACTIVE = [
    ("V-000125", "P-0001", "VDE", "Nordwind Spedition GmbH", "DE281947305", make_iban("DE", "200400000612345678"),
     30, "finance.de", date(2016, 5, 2), "Old name before the 2019 rebrand"),
    ("V-000126", "P-0006", "VDE", "Atlas Display Systems SL", "ESB86419273", make_iban("ES", "00491500051234567892"),
     60, "finance.de", date(2018, 3, 12), "Old company name"),
    ("V-000127", "P-0005", "VUS", "Shopsys Inc", "47-3829105", "ABA 121000248 ACCT 1100456789",
     30, "finance.us", date(2017, 10, 1), "Replaced by a newer account"),
    ("V-000128", "P-0012", "VDE", "Lumen Lighting UK Ltd", "GB618273940", make_iban("GB", "LOYD30963499887766"),
     30, "finance.de", date(2019, 7, 15), "Old trading name"),
]

# D4 — missing identifiers: ~30% of all accounts (8 of 28) have an empty VAT ID (tax ID) or IBAN.
# Applied last so it covers accounts created by D1, D2 and D5.
D4_MISSING = {
    "V-000109": "vat_id", "V-000117": "vat_id", "V-000119": "vat_id", "V-000120": "iban",
    "V-000122": "vat_id", "V-000124": "vat_id", "V-000125": "iban", "V-000127": "vat_id",
}


def _tag(acc: AccountSpec, rule: str, **changes) -> AccountSpec:
    return replace(acc, rules=acc.rules + (rule,), **changes)


def rule_d1_spelling_duplicates(accounts: list[AccountSpec]) -> list[AccountSpec]:
    by_id = {a.account_id: a for a in accounts}
    out = list(accounts)
    for new_id, source_id, name, iban, terms, created_by, created_on, notes in D1_DUPLICATES:
        src = by_id[source_id]
        out.append(AccountSpec(new_id, None, src.legal_entity_code, name, src.vat_id, iban, terms,
                               created_by, created_on, "active", notes, ("D1",)))
    return out


def rule_d2_cross_entity_spread(accounts: list[AccountSpec]) -> list[AccountSpec]:
    by_id = {a.account_id: a for a in accounts}
    new_id, source_id, entity, terms, created_by, created_on, notes = D2_CROSS_ENTITY
    src = by_id[source_id]
    return accounts + [AccountSpec(new_id, None, entity, src.display_name, src.vat_id, src.iban, terms,
                                   created_by, created_on, "active", notes, ("D2",))]


def rule_d3_terms_drift(accounts: list[AccountSpec]) -> list[AccountSpec]:
    return [_tag(a, "D3", payment_terms_days=D3_TERMS_DRIFT[a.account_id]) if a.account_id in D3_TERMS_DRIFT else a
            for a in accounts]


def rule_d5_inactive_leftovers(accounts: list[AccountSpec]) -> list[AccountSpec]:
    extra = [AccountSpec(acc_id, None, entity, name, vat, bank, terms, created_by, created_on, "inactive",
                         f"{notes} (belonged to {world.PARTY_BY_ID[party_id].canonical_name})", ("D5",))
             for acc_id, party_id, entity, name, vat, bank, terms, created_by, created_on, notes in D5_INACTIVE]
    return accounts + extra


def rule_d4_missing_identifiers(accounts: list[AccountSpec]) -> list[AccountSpec]:
    return [_tag(a, "D4", **{D4_MISSING[a.account_id]: None}) if a.account_id in D4_MISSING else a
            for a in accounts]


def rule_d6_po_discipline(pos: list[world.POSpec]) -> list[world.POSpec]:
    """D6 — weak PO discipline: POs promised to suppliers but never keyed in the ERP are missing in as-is (~60%)."""
    return [po for po in pos if po.in_asis]


def derive_dirty_accounts(clean: list[AccountSpec] | None = None) -> list[AccountSpec]:
    accounts = list(clean if clean is not None else world.CLEAN_ACCOUNTS)
    for rule in (rule_d1_spelling_duplicates, rule_d2_cross_entity_spread, rule_d3_terms_drift,
                 rule_d5_inactive_leftovers, rule_d4_missing_identifiers):
        accounts = rule(accounts)
    return sorted(accounts, key=lambda a: a.account_id)


def accounts_for(scenario: str) -> list[AccountSpec]:
    return derive_dirty_accounts() if scenario == "asis" else list(world.CLEAN_ACCOUNTS)


def purchase_orders_for(scenario: str) -> list[world.POSpec]:
    return rule_d6_po_discipline(world.PURCHASE_ORDERS) if scenario == "asis" else list(world.PURCHASE_ORDERS)


def receipts_for(scenario: str) -> list[world.ReceiptSpec]:
    po_numbers = {po.po_number for po in purchase_orders_for(scenario)}
    return [r for r in world.RECEIPTS if r.po_number in po_numbers]


# --------------------------------------------------------------------------------------------
# Loading into the database
# --------------------------------------------------------------------------------------------

ERP_TABLES = (LegalEntity, Party, VendorAccount, Contract, ProductReceipt, PendingVendorInvoice,
              CreditNoteApplication)


def clear_scenario(session: Session, scenario: str) -> None:
    """Delete every row of one scenario (mock ERP + simulator tables)."""
    for doc in session.scalars(select(InboundDocument).where(InboundDocument.scenario == scenario)):
        session.delete(doc)  # cascades to extraction
    for po in session.scalars(select(PurchaseOrder).where(PurchaseOrder.scenario == scenario)):
        session.delete(po)  # cascades to lines
    for model in ERP_TABLES + (GateDecision, Run):
        session.execute(delete(model).where(model.scenario == scenario))
    session.flush()


def seed_scenario(session: Session, scenario: str) -> None:
    """Load the mock ERP tables for one scenario (does not touch the inbox)."""
    for le in world.LEGAL_ENTITIES:
        session.add(LegalEntity(scenario=scenario, code=le.code, name=le.name, country=le.country,
                                currency=le.currency, vat_id=le.vat_id, address=", ".join(le.address)))
    for p in world.PARTIES:
        session.add(Party(scenario=scenario, party_id=p.party_id, canonical_name=p.canonical_name,
                          vat_id=p.vat_id, country=p.country, supplier_type=p.supplier_type,
                          agreed_terms_days=p.agreed_terms_days))
    for a in accounts_for(scenario):
        notes = a.notes
        if scenario == "tobe" and notes is None:
            notes = "Validated through the vendor request workflow; linked to its supplier."
        session.add(VendorAccount(scenario=scenario, account_id=a.account_id,
                                  legal_entity_code=a.legal_entity_code, party_id=a.party_id,
                                  display_name=a.display_name, vat_id=a.vat_id, iban=a.iban,
                                  payment_terms_days=a.payment_terms_days, created_by=a.created_by,
                                  created_on=a.created_on, status=a.status, notes=notes,
                                  corruption_rules=list(a.rules)))
    for c in world.CONTRACTS:
        owner = world.PEOPLE[c.owner]
        session.add(Contract(scenario=scenario, contract_id=c.contract_id, party_id=c.party_id,
                             legal_entity_code=c.legal_entity_code, description=c.description,
                             payment_terms_days=c.payment_terms_days, recurring=c.recurring,
                             expected_monthly_min=c.expected_monthly_min,
                             expected_monthly_max=c.expected_monthly_max, currency=c.currency,
                             category=c.category, owner_name=owner.name, owner_email=owner.email))
    if scenario == "tobe":  # card / catalogue commitments for small store purchases (none in the as-is)
        for cat in world.CATALOGUES:
            owner = world.PEOPLE[cat.owner]
            session.add(Contract(scenario=scenario, contract_id=cat.catalogue_id, party_id=cat.party_id,
                                 legal_entity_code=cat.legal_entity_code, description=cat.description,
                                 payment_terms_days=world.PARTY_BY_ID[cat.party_id].agreed_terms_days, recurring=False,
                                 expected_monthly_min=0.0, expected_monthly_max=cat.limit_chf,
                                 currency=world.GROUP_CURRENCY, category="catalogue", owner_name=owner.name,
                                 owner_email=owner.email))
    for po in purchase_orders_for(scenario):
        requester, buyer = world.PEOPLE[po.requester], world.PEOPLE[po.buyer]
        session.add(PurchaseOrder(
            scenario=scenario, po_number=po.po_number, legal_entity_code=po.legal_entity_code,
            vendor_account_id=po.vendor_account_id, requester_name=requester.name,
            requester_email=requester.email, cost_centre=po.cost_centre, buyer_name=buyer.name,
            buyer_email=buyer.email, category=po.category, status="confirmed", order_date=po.order_date,
            description=po.description, total=po.total, currency=po.currency,
            lines=[PurchaseOrderLine(line_no=ln.line_no, description=ln.description, qty=ln.qty,
                                     unit_price=ln.unit_price, amount=ln.amount,
                                     receipt_required=ln.receipt_required) for ln in po.lines]))
    for r in receipts_for(scenario):
        session.add(ProductReceipt(scenario=scenario, receipt_id=r.receipt_id, po_number=r.po_number,
                                   line_no=r.line_no, qty_received=r.qty_received, received_on=r.received_on,
                                   received_by=world.PEOPLE[r.received_by].name, kind=r.kind))
    session.flush()


def seed_all(session: Session) -> None:
    for scenario in SCENARIOS:
        clear_scenario(session, scenario)
        seed_scenario(session, scenario)
    session.commit()


def reset_scenario(session: Session, scenario: str) -> Optional[str]:
    """Restore one scenario to its seeded state (empty inbox, no decisions, no postings). Returns the dataset of
    the sample documents that were loaded (None if none), so the caller can reload the same set."""
    dataset = loaded_dataset(session, scenario)
    clear_scenario(session, scenario)
    seed_scenario(session, scenario)
    session.commit()
    return dataset


# --------------------------------------------------------------------------------------------
# Intake: load a set of sample documents into the simulated mailboxes
# --------------------------------------------------------------------------------------------

DATASETS = ("v1", "v2")  # v1 = the 12 case documents; v2 = test set v2 (26 documents)
DEMO_HELD_BACK = 5  # the case document that arrives live in the demo ("Receive next email"): FitOut, deck A5
LIVE_DATASET = "live"  # documents received through the intake webhook
DATASET_LABELS = {"v1": "case documents", "v2": "test set v2", LIVE_DATASET: "live intake"}


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


SCENARIO_LETTER = {"asis": "A", "tobe": "B"}  # prefix of document IDs, e.g. A-01 / B-01
_DOC_ID = re.compile(r"^[AB](2?)-(\d+)$")  # a sample document's id: A-01 (v1) / B2-01 (v2)


def check_dataset(dataset: str) -> str:
    if dataset not in DATASETS:
        raise ValueError(f"unknown dataset {dataset!r}: use one of {DATASETS}")
    return dataset


def doc_id_for(scenario: str, sample_no: int, dataset: str = "v1") -> str:
    """'B-01' for a case document (v1), 'B2-01' for a document of test set v2."""
    suffix = "" if check_dataset(dataset) == "v1" else "2"
    return f"{SCENARIO_LETTER[scenario]}{suffix}-{sample_no:02d}"


def dataset_of_doc_id(doc_id: str) -> Optional[str]:
    """The sample dataset a document id belongs to ('B2-01' -> 'v2'); None for a webhook id ('B-W01')."""
    m = _DOC_ID.match(doc_id or "")
    return None if m is None else ("v2" if m.group(1) else "v1")


def dataset_dir(dataset: str) -> Path:
    return INVOICES_DIR if check_dataset(dataset) == "v1" else config.INVOICES_V2_DIR


def documents_for(dataset: str) -> list[world.DocumentSpec]:
    return world.DOCUMENTS if check_dataset(dataset) == "v1" else world.documents_for(dataset)


def spec_for(dataset: Optional[str], sample_no: Optional[int]) -> Optional[world.DocumentSpec]:
    """The DocumentSpec of a sample document; None for a live document or an unknown number."""
    if dataset not in DATASETS or not sample_no:
        return None
    return next((s for s in documents_for(dataset) if s.no == sample_no), None)


def stored_path(path: Path) -> str:
    """How InboundDocument.file_path stores a file: relative to the project root, or absolute (POSIX form) when the
    file lies outside it (e.g. INBOUND_DIR on a mounted Cloud Storage bucket)."""
    path = Path(path).resolve()
    try:
        return path.relative_to(Path(config.BASE_DIR).resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def ensure_pdfs() -> None:
    missing = [d for d in world.DOCUMENTS if not (INVOICES_DIR / d.filename).exists()]
    if missing:
        from app import invoices_gen

        invoices_gen.generate_all(INVOICES_DIR)


def ensure_files(dataset: str) -> None:
    """Generate the files of a dataset when any is missing (v1: the 12 PDFs; v2: PDFs, the UBL XML, the email)."""
    if check_dataset(dataset) == "v1":
        ensure_pdfs()
        return
    out_dir = config.INVOICES_V2_DIR
    if any(not (out_dir / d.filename).exists() for d in documents_for(dataset)):
        from app import invoices_gen

        invoices_gen.generate_all_v2(out_dir)


def loaded_dataset(session: Session, scenario: str) -> Optional[str]:
    """Dataset of the sample documents in the scenario's mailboxes (v1 or v2), None when none is loaded."""
    datasets = session.scalars(select(InboundDocument.dataset).where(
        InboundDocument.scenario == scenario, InboundDocument.sample_no > 0).distinct()).all()
    known = [d for d in DATASETS if d in datasets]
    return known[0] if known else None


def _new_document(scenario: str, spec: world.DocumentSpec, dataset: str) -> InboundDocument:
    """A sample document as it arrives. to-be: one intake address (ap@), registered on arrival. as-is: the mailbox the
    supplier used, unregistered until the scenario runs (ap@ +1 business day, store mailbox +7 business days)."""
    path = dataset_dir(dataset) / spec.filename
    registered = scenario == "tobe"
    channel, mailbox = ("ap_mailbox", world.AP_MAILBOX) if registered else (spec.channel, spec.mailbox)
    return InboundDocument(
        doc_id=doc_id_for(scenario, spec.no, dataset), scenario=scenario, sample_no=spec.no,
        channel=channel, mailbox=mailbox, received_on=spec.received_on,
        file_path=stored_path(path), file_hash=file_sha256(path),
        sender_email=spec.sender_email, subject=spec.subject, registered=registered,
        registered_on=sim.registration_date(scenario, channel, spec.received_on) if registered else None,
        doc_type="unknown", dataset=dataset, content_type=getattr(spec, "content", "pdf") or "pdf",
        email_body=getattr(spec, "email_body", None),
    )


def load_sample_documents(session: Session, scenario: str, dataset: str = "v1", *,
                          hold_back: tuple[int, ...] = ()) -> list[InboundDocument]:
    """(Re)load a dataset's sample documents into the mailboxes of one scenario (every document of the scenario,
    webhook uploads included, is replaced). Documents whose number is in `hold_back` are not loaded yet: they arrive
    later with receive_document (the demo's "Receive next email")."""
    specs = documents_for(dataset)
    ensure_files(dataset)
    for doc in session.scalars(select(InboundDocument).where(InboundDocument.scenario == scenario)):
        session.delete(doc)
    session.flush()
    docs = [_new_document(scenario, spec, dataset) for spec in specs if spec.no not in hold_back]
    session.add_all(docs)
    session.commit()
    return docs


def held_back_documents(session: Session, scenario: str = "tobe") -> list[world.DocumentSpec]:
    """Case documents not in the scenario's mailbox yet while the case documents are loaded (the demo's next email),
    in order of arrival."""
    if loaded_dataset(session, scenario) != "v1":
        return []
    present = set(session.scalars(select(InboundDocument.sample_no).where(
        InboundDocument.scenario == scenario, InboundDocument.dataset == "v1")))
    return sorted((s for s in world.DOCUMENTS if s.no not in present), key=lambda s: s.received_on)


def receive_document(session: Session, scenario: str, spec: world.DocumentSpec) -> InboundDocument:
    """One case document arrives: registered on arrival (to-be), not read and not processed yet."""
    doc = _new_document(scenario, spec, "v1")
    session.add(doc)
    session.commit()
    return doc


def reset_demo(session: Session, dataset: str = "v1", *, log=None) -> None:
    """The demo's starting point (brief v2 section 7): both scenarios re-seeded, the sample documents loaded and run
    offline (extraction cache or fixtures, never the API), the case document DEMO_HELD_BACK not arrived yet in to-be."""
    from app import extract, gate

    for scenario in SCENARIOS:
        clear_scenario(session, scenario)
        seed_scenario(session, scenario)
        session.commit()
        hold = (DEMO_HELD_BACK,) if scenario == "tobe" and dataset == "v1" else ()
        docs = load_sample_documents(session, scenario, dataset, hold_back=hold)
        extract.extract_documents(session, docs, allow_api=False)
        gate.run_scenario(session, scenario, allow_api=False, log=log or (lambda line: None))


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed the Velox P2P simulator database.")
    parser.add_argument("--no-documents", action="store_true", help="do not load the sample documents")
    parser.add_argument("--dataset", choices=DATASETS, default="v1",
                        help="v1: the case documents (default); v2: test set v2")
    args = parser.parse_args()

    from app.db import SessionLocal, init_db

    init_db(drop=True)
    with SessionLocal() as session:
        seed_all(session)
        for scenario in SCENARIOS:
            n_acc = len(accounts_for(scenario))
            n_po = len(purchase_orders_for(scenario))
            print(f"[seed] scenario={scenario} accounts={n_acc} parties={len(world.PARTIES)} "
                  f"ratio={n_acc / len(world.PARTIES):.2f} POs={n_po} receipts={len(receipts_for(scenario))} "
                  f"contracts={len(world.CONTRACTS)}")
        if not args.no_documents:
            reset_demo(session, args.dataset)  # cache / fixtures only: seeding never calls the Gemini API
            for scenario in SCENARIOS:
                n = len(session.scalars(select(InboundDocument).where(InboundDocument.scenario == scenario)).all())
                print(f"[seed] scenario={scenario} {n} {DATASET_LABELS[args.dataset]} loaded and run")


if __name__ == "__main__":
    main()
