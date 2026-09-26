"""Seed data against brief section 5: the clean world (to-be) and the dirty world derived by rules D1–D6 (as-is).

Uses the conftest `session` fixture: a temporary SQLite DB with both scenarios seeded and empty inboxes.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import seed, world
from app.extract import extract_from_fixture
from app.models import (
    Contract,
    Extraction,
    GateDecision,
    InboundDocument,
    LegalEntity,
    Party,
    ProductReceipt,
    PurchaseOrder,
    VendorAccount,
)
from app.normalize import names_match, normalise_iban, normalise_vat

AGREED_TERMS = {p.party_id: p.agreed_terms_days for p in world.PARTIES}
SEEDED_MODELS = (LegalEntity, Party, VendorAccount, Contract, PurchaseOrder, ProductReceipt)


def accounts(session: Session, scenario: str) -> list[VendorAccount]:
    stmt = select(VendorAccount).where(VendorAccount.scenario == scenario).order_by(VendorAccount.account_id)
    return list(session.scalars(stmt))


def count(session: Session, model, scenario: str) -> int:
    return session.scalar(select(func.count()).select_from(model).where(model.scenario == scenario))


def purchase_order(session: Session, scenario: str, po_number: str) -> PurchaseOrder | None:
    stmt = select(PurchaseOrder).where(PurchaseOrder.scenario == scenario, PurchaseOrder.po_number == po_number)
    return session.scalars(stmt).one_or_none()


def receipts(session: Session, scenario: str, po_number: str | None = None) -> list[ProductReceipt]:
    stmt = select(ProductReceipt).where(ProductReceipt.scenario == scenario)
    if po_number is not None:
        stmt = stmt.where(ProductReceipt.po_number == po_number)
    return list(session.scalars(stmt))


def ground_truth(spec: world.DocumentSpec) -> dict:
    """What a perfect extractor reads from the document (tests/fixtures)."""
    return extract_from_fixture(Path(spec.filename)).data


# --------------------------------------------------------------------------------------------
# Legal entities and parties (5.1, 5.2)
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("scenario", ["asis", "tobe"])
def test_three_legal_entities_and_twelve_parties(session: Session, scenario: str) -> None:
    codes = set(session.scalars(select(LegalEntity.code).where(LegalEntity.scenario == scenario)))
    assert codes == {"VDE", "VFR", "VUS"}
    assert count(session, Party, scenario) == 12


# --------------------------------------------------------------------------------------------
# Clean vendor master, scenario to-be (5.3)
# --------------------------------------------------------------------------------------------


def test_tobe_has_14_accounts_all_clean(session: Session) -> None:
    accs = accounts(session, "tobe")
    assert len(accs) == 14  # 12 suppliers, two of them with a legitimate second record (Cleanspace, Atlas: VFR)
    assert round(len(accs) / count(session, Party, "tobe"), 2) == 1.17  # deck slide 11: <= 1.2
    for a in accs:
        assert a.party_id in AGREED_TERMS, a.account_id
        assert a.vat_id and a.iban, a.account_id
        assert a.payment_terms_days == AGREED_TERMS[a.party_id], a.account_id
        assert a.status == "active"
        assert a.corruption_rules == []
        assert a.created_by.startswith("finance."), a.account_id


def test_tobe_has_one_account_per_party_per_entity(session: Session) -> None:
    pairs = [(a.party_id, a.legal_entity_code) for a in accounts(session, "tobe")]
    assert len(set(pairs)) == len(pairs)
    assert {party_id for party_id, _ in pairs} == set(AGREED_TERMS)  # every supplier has an account


# --------------------------------------------------------------------------------------------
# Dirty vendor master, scenario as-is (5.4): rules D1–D5
# --------------------------------------------------------------------------------------------


def test_asis_has_28_accounts_for_12_suppliers(session: Session) -> None:
    n_accounts = len(accounts(session, "asis"))
    assert n_accounts == 28
    assert round(n_accounts / count(session, Party, "asis"), 2) == 2.33  # case: 2,800 / 1,200


def test_d1_spelling_duplicates(session: Session) -> None:
    by_id = {a.account_id: a for a in accounts(session, "asis")}
    extra_per_party: Counter[str] = Counter()
    for new_id, source_id, *_ in seed.D1_DUPLICATES:
        dup, src = by_id[new_id], world.CLEAN_ACCOUNT_BY_ID[source_id]
        extra_per_party[src.party_id] += 1
        assert "D1" in dup.corruption_rules
        assert dup.party_id is None  # no party link
        assert dup.legal_entity_code == src.legal_entity_code  # same legal entity
        assert dup.display_name != src.display_name and names_match(dup.display_name, src.display_name)
        if dup.iban:  # D4 may have emptied it
            assert dup.iban != src.iban
        assert dup.payment_terms_days in {14, 45, 60}
        assert dup.payment_terms_days != AGREED_TERMS[src.party_id]
    assert {world.PARTY_BY_ID[p].no for p in extra_per_party} == {1, 2, 4, 5, 7, 8}
    assert all(1 <= n <= 2 for n in extra_per_party.values())
    assert sum("D1" in a.corruption_rules for a in by_id.values()) == len(seed.D1_DUPLICATES)


def test_d2_nordwind_has_a_vfr_account_created_by_a_store(session: Session) -> None:
    nordwind = world.PARTY_BY_NO[1]
    assert all(a.legal_entity_code != "VFR" for a in world.CLEAN_ACCOUNTS if a.party_id == nordwind.party_id)
    d2 = [a for a in accounts(session, "asis") if "D2" in a.corruption_rules]
    assert len(d2) == 1
    acc = d2[0]
    assert acc.legal_entity_code == "VFR"
    assert acc.payment_terms_days == 45
    assert acc.created_by.startswith("store.")
    assert acc.party_id is None
    assert names_match(acc.display_name, nordwind.canonical_name)


def test_d3_exactly_five_terms_drifts(session: Session) -> None:
    linked = [a for a in accounts(session, "asis") if a.party_id is not None]
    drifted = {a.account_id for a in linked if a.payment_terms_days != AGREED_TERMS[a.party_id]}
    assert drifted == set(seed.D3_TERMS_DRIFT)
    assert len(drifted) == 5
    tagged = {a.account_id for a in accounts(session, "asis") if "D3" in a.corruption_rules}
    assert tagged == drifted


def test_d4_about_30_percent_miss_vat_or_iban(session: Session) -> None:
    accs = accounts(session, "asis")
    missing = [a for a in accs if not a.vat_id or not a.iban]
    assert {a.account_id for a in missing} == set(seed.D4_MISSING)
    assert 0.25 <= len(missing) / len(accs) <= 0.35  # 8 of 28 = 29%
    assert all("D4" in a.corruption_rules for a in missing)
    # D4 is applied last, so it also hits accounts created by D1, D2 and D5.
    other_rules = {rule for a in missing for rule in a.corruption_rules}
    assert {"D1", "D2", "D5"} <= other_rules


def test_d5_four_inactive_leftovers(session: Session) -> None:
    inactive = [a for a in accounts(session, "asis") if a.status == "inactive"]
    assert len(inactive) == 4
    canonical_names = {p.canonical_name for p in world.PARTIES}
    for a in inactive:
        assert "D5" in a.corruption_rules
        assert a.party_id is None
        assert a.display_name not in canonical_names  # old names


def test_asis_accounts_created_by_anyone(session: Session) -> None:
    created_by = {a.created_by for a in accounts(session, "asis")}
    assert {"store.berlin01", "ap.temp", "finance.fr"} <= created_by


def test_deriving_the_dirty_world_is_deterministic_and_leaves_the_clean_world_alone() -> None:
    clean_before = list(world.CLEAN_ACCOUNTS)
    assert seed.derive_dirty_accounts() == seed.derive_dirty_accounts()
    assert world.CLEAN_ACCOUNTS == clean_before
    assert seed.accounts_for("tobe") == clean_before


# --------------------------------------------------------------------------------------------
# Commitments (5.5) and rule D6
# --------------------------------------------------------------------------------------------


def test_d6_asis_has_half_of_the_pos(session: Session) -> None:
    n_tobe, n_asis = count(session, PurchaseOrder, "tobe"), count(session, PurchaseOrder, "asis")
    assert n_tobe == 12
    assert n_asis == 6
    tobe_numbers = set(session.scalars(select(PurchaseOrder.po_number).where(PurchaseOrder.scenario == "tobe")))
    asis_numbers = set(session.scalars(select(PurchaseOrder.po_number).where(PurchaseOrder.scenario == "asis")))
    assert asis_numbers < tobe_numbers


def test_receipts_follow_the_pos_of_each_scenario(session: Session) -> None:
    tobe = receipts(session, "tobe")
    assert len(tobe) == 11  # receipt lines
    assert len({r.receipt_id for r in tobe}) == 9  # receipt documents
    asis_pos = set(session.scalars(select(PurchaseOrder.po_number).where(PurchaseOrder.scenario == "asis")))
    asis = receipts(session, "asis")
    assert len(asis) == 7
    assert {r.po_number for r in asis} <= asis_pos


def test_contracts_are_per_legal_entity(session: Session) -> None:
    rows = [c for c in session.scalars(select(Contract).where(Contract.scenario == "tobe")) if c.recurring]
    assert len(rows) == 4  # 3 supplier contracts; Cleanspace serves VDE and VFR
    by_party = Counter(world.PARTY_BY_ID[c.party_id].no for c in rows)
    assert by_party == {1: 1, 4: 2, 11: 1}
    assert all(c.category != "catalogue" for c in rows)


def test_tobe_has_the_store_catalogue_and_asis_has_none(session: Session) -> None:
    """Card / catalogue for small store purchases (deck slide 9): Kaffee & Co for Store Berlin 01, CHF 500 per invoice."""
    [cat] = session.scalars(select(Contract).where(Contract.scenario == "tobe", Contract.category == "catalogue"))
    assert (cat.contract_id, cat.party_id, cat.legal_entity_code) == ("CAT-2026-001", "P-0009", "VDE")
    assert (cat.recurring, cat.expected_monthly_max, cat.currency, cat.owner_name) == (False, 500.0, "CHF",
                                                                                         "Paul Neumann")
    assert not list(session.scalars(select(Contract).where(Contract.scenario == "asis",
                                                           Contract.category == "catalogue")))


def test_commitments_reference_accounts_in_their_own_entity(session: Session) -> None:
    tobe_accounts = {a.account_id: a for a in accounts(session, "tobe")}
    for po in session.scalars(select(PurchaseOrder).where(PurchaseOrder.scenario == "tobe")):
        assert tobe_accounts[po.vendor_account_id].legal_entity_code == po.legal_entity_code, po.po_number
    served = {(a.party_id, a.legal_entity_code) for a in tobe_accounts.values()}
    for c in session.scalars(select(Contract).where(Contract.scenario == "tobe")):
        assert (c.party_id, c.legal_entity_code) in served, c.contract_id


def test_po_4500123_fitout_milestone_2_has_no_service_confirmation(session: Session) -> None:
    po = purchase_order(session, "tobe", "4500123")
    assert po is not None and po.category == "service" and po.total == 48000.0
    assert receipts(session, "tobe", "4500123") == []


def test_po_4500112_lumen_received_100_of_120(session: Session) -> None:
    """Lumen's PO stays in the master (test set v2 document 15 uses it); its case document was dropped by brief v2."""
    po = purchase_order(session, "tobe", "4500112")
    assert po is not None
    assert sum(line.qty for line in po.lines) == 120
    assert sum(r.qty_received for r in receipts(session, "tobe", "4500112")) == 100
    assert all(d.party_id != "P-0012" for d in world.DOCUMENTS)


def test_po_4500109_atlas_price_42_but_invoice_44(session: Session) -> None:
    po = purchase_order(session, "tobe", "4500109")
    assert po is not None
    [po_line] = po.lines
    [inv_line] = world.DOCUMENT_BY_NO[6].lines
    assert (po_line.qty, po_line.unit_price) == (150, 42.0)
    assert (inv_line.quantity, inv_line.unit_price) == (150, 44.0)


# --------------------------------------------------------------------------------------------
# The 12 sample documents against the seed (5.6)
# --------------------------------------------------------------------------------------------


def test_every_po_printed_on_a_document_exists_in_tobe(session: Session) -> None:
    printed = {po for d in world.DOCUMENTS for po in d.po_numbers}
    assert printed  # sanity: several documents carry a PO
    for po_number in printed:
        assert purchase_order(session, "tobe", po_number) is not None, po_number


@pytest.mark.parametrize("spec", world.DOCUMENTS, ids=lambda d: f"doc{d.no:02d}")
def test_document_supplier_identifiers_match_a_clean_account(session: Session, spec: world.DocumentSpec) -> None:
    data = ground_truth(spec)
    party_accounts = [a for a in accounts(session, "tobe") if a.party_id == spec.party_id]
    assert party_accounts
    vat = normalise_vat(data["supplier_vat_id"]["value"])
    iban = normalise_iban(data["supplier_iban"]["value"])
    assert any(normalise_vat(a.vat_id) == vat for a in party_accounts)
    assert any(normalise_iban(a.iban) == iban for a in party_accounts)


def test_every_case_document_is_billed_to_an_entity_its_supplier_serves(session: Session) -> None:
    """Brief v2 dropped the wrong-entity case document (Metro Media): every one of the twelve is billed to an entity
    where its supplier has a record; the POs quoted belong to that entity."""
    served = {(a.party_id, a.legal_entity_code) for a in accounts(session, "tobe")}
    assert [d.no for d in world.DOCUMENTS if (d.party_id, d.bill_to_entity) not in served] == []
    for d in world.DOCUMENTS:
        for number in d.po_numbers:
            assert purchase_order(session, "tobe", number).legal_entity_code == d.bill_to_entity, d.no


def test_bright_credit_note_references_document_3() -> None:
    invoice, credit_note = world.DOCUMENT_BY_NO[3], world.DOCUMENT_BY_NO[4]
    assert credit_note.doc_type == "credit_note"
    assert credit_note.referenced_invoice_number == invoice.invoice_number
    assert credit_note.party_id == invoice.party_id
    assert credit_note.bill_to_entity == invoice.bill_to_entity
    assert credit_note.gross_total < 0


def test_contract_invoices_fall_inside_the_expected_range_on_net_amounts(session: Session) -> None:
    """Contract ranges are net: document 1 is 23,400 net (inside 20,000–26,000) but 27,846 gross."""
    for no in (1, 10, 12):
        doc = world.DOCUMENT_BY_NO[no]
        contract = session.scalars(select(Contract).where(
            Contract.scenario == "tobe", Contract.contract_id == doc.contract_reference)).one()
        assert (contract.party_id, contract.legal_entity_code) == (doc.party_id, doc.bill_to_entity)
        assert contract.expected_monthly_min <= doc.net_total <= contract.expected_monthly_max
    assert world.DOCUMENT_BY_NO[1].gross_total > 26000


# --------------------------------------------------------------------------------------------
# Idempotency and reset
# --------------------------------------------------------------------------------------------


def _counts(session: Session) -> dict[tuple[str, str], int]:
    return {(model.__name__, scenario): count(session, model, scenario)
            for model in SEEDED_MODELS for scenario in ("asis", "tobe")}


def test_seed_all_twice_gives_the_same_counts(session: Session) -> None:
    before = _counts(session)
    seed.seed_all(session)
    assert _counts(session) == before
    assert before[("VendorAccount", "asis")] == 28 and before[("VendorAccount", "tobe")] == 14


def _add_inbox_document(session: Session, scenario: str, spec: world.DocumentSpec) -> None:
    """A hand-made inbox row plus its extraction (no PDF generation, no extractor)."""
    doc_id = seed.doc_id_for(scenario, spec.no)
    session.add(InboundDocument(
        doc_id=doc_id, scenario=scenario, sample_no=spec.no, channel=spec.channel, mailbox=spec.mailbox,
        received_on=spec.received_on, file_path=f"data/invoices/{spec.filename}", file_hash="0" * 64,
        sender_email=spec.sender_email, subject=spec.subject, registered=False, doc_type="unknown"))
    session.add(Extraction(doc_id=doc_id, model="test", json={}, created_on=datetime(2026, 10, 1)))
    session.add(GateDecision(doc_id=doc_id, scenario=scenario, steps=[], outcome="posted"))


def test_reset_scenario_empties_only_that_inbox(session: Session) -> None:
    for scenario in ("asis", "tobe"):
        for spec in world.DOCUMENTS[:3]:
            _add_inbox_document(session, scenario, spec)
    session.commit()
    before = _counts(session)

    seed.reset_scenario(session, "asis")

    assert count(session, InboundDocument, "asis") == 0
    assert count(session, InboundDocument, "tobe") == 3
    assert count(session, GateDecision, "asis") == 0
    assert count(session, GateDecision, "tobe") == 3
    extraction_ids = set(session.scalars(select(Extraction.doc_id)))
    assert extraction_ids == {seed.doc_id_for("tobe", spec.no) for spec in world.DOCUMENTS[:3]}
    assert _counts(session) == before  # mock ERP tables restored, the other scenario untouched


# --------------------------------------------------------------------------------------------
# Data contract for the phase-2 demo story: the vendor-resolution rules documented in
# docs/ASSUMPTIONS.md section 8, applied to the printed supplier data of the 12 documents.
# --------------------------------------------------------------------------------------------


def _naive_asis_lookup(printed_name: str, accounts: list[world.AccountSpec]) -> world.AccountSpec | None:
    """As-is 'first name hit': exact display name (case-insensitive, trimmed), else fuzzy; lowest account ID."""
    active = sorted((a for a in accounts if a.status == "active"), key=lambda a: a.account_id)
    target = printed_name.strip().casefold()
    exact = [a for a in active if a.display_name.strip().casefold() == target]
    if exact:
        return exact[0]
    return next((a for a in active if names_match(a.display_name, printed_name)), None)


ASIS_EXPECTED_ACCOUNT = {1: "V-000101", 2: "V-000117", 3: "V-000102", 4: "V-000119", 5: "V-000103", 6: "V-000106",
                         7: "V-000105", 8: "V-000108", 9: "V-000109", 10: "V-000104", 11: "V-000110", 12: "V-000111"}


@pytest.mark.parametrize("spec", world.DOCUMENTS, ids=lambda d: f"doc{d.no:02d}")
def test_naive_asis_lookup_lands_each_document_on_the_documented_account(spec):
    account = _naive_asis_lookup(spec.printed_supplier_name, seed.accounts_for("asis"))
    assert account is not None and account.account_id == ASIS_EXPECTED_ACCOUNT[spec.no]


def test_asis_traps_follow_from_the_data():
    asis = seed.accounts_for("asis")
    hit = {d.no: _naive_asis_lookup(d.printed_supplier_name, asis) for d in world.DOCUMENTS}
    # Doc 2 (resent copy) lands on another account than doc 1, so a per-account duplicate check misses it.
    assert hit[2].account_id != hit[1].account_id
    # The credit note (doc 4) lands on another account than the invoice it credits (doc 3): unapplied.
    assert hit[4].account_id != hit[3].account_id
    # Doc 10 (Cleanspace, billed to VFR) lands on the VDE account: a wrong-entity posting.
    wrong_entity = [d.no for d in world.DOCUMENTS if hit[d.no].legal_entity_code != d.bill_to_entity]
    assert wrong_entity == [10]


def test_tobe_identifiers_resolve_resends_and_credit_notes_to_the_same_party():
    by_vat = {normalise_vat(a.vat_id): a.party_id for a in world.CLEAN_ACCOUNTS}
    party = {d.no: by_vat[normalise_vat(d.party.vat_id)] for d in world.DOCUMENTS}
    assert party[1] == party[2] == "P-0001"
    assert party[3] == party[4] == "P-0002"


# --------------------------------------------------------------------------------------------
# Datasets (phase 3): the case documents (v1) or test set v2, and the columns they fill
# --------------------------------------------------------------------------------------------


def test_doc_ids_encode_scenario_dataset_and_number() -> None:
    assert (seed.doc_id_for("tobe", 1), seed.doc_id_for("asis", 12, "v1")) == ("B-01", "A-12")
    assert (seed.doc_id_for("tobe", 1, "v2"), seed.doc_id_for("asis", 26, "v2")) == ("B2-01", "A2-26")
    assert [seed.dataset_of_doc_id(i) for i in ("B-01", "A2-26", "B-W01", "")] == ["v1", "v2", None, None]
    with pytest.raises(ValueError):
        seed.doc_id_for("tobe", 1, "v3")


def test_load_test_set_v2_fills_dataset_content_type_and_email_body(session: Session) -> None:
    docs = seed.load_sample_documents(session, "tobe", "v2")
    specs = world.documents_for("v2")
    assert len(docs) == len(specs) == 26 and seed.loaded_dataset(session, "tobe") == "v2"
    assert seed.loaded_dataset(session, "asis") is None
    by_no = {d.sample_no: d for d in docs}
    for spec in specs:
        doc = by_no[spec.no]
        assert (doc.doc_id, doc.dataset, doc.content_type, doc.email_body) == (
            seed.doc_id_for("tobe", spec.no, "v2"), "v2", spec.content, spec.email_body)
        assert doc.file_path == f"data/invoices_v2/{spec.filename}" and doc.registered is True
    assert (by_no[11].content_type, by_no[14].content_type) == ("ubl_xml", "email_body")
    assert by_no[8].email_body and by_no[8].content_type == "pdf"  # the store manager's forwarding comment
    assert all(d.content_type == "pdf" and d.email_body is None for d in seed.load_sample_documents(session, "tobe"))
    assert seed.loaded_dataset(session, "tobe") == "v1"


def test_reset_scenario_returns_the_dataset_that_was_loaded(session: Session) -> None:
    seed.load_sample_documents(session, "asis", "v2")
    assert seed.reset_scenario(session, "asis") == "v2" and count(session, InboundDocument, "asis") == 0
    assert seed.reset_scenario(session, "asis") is None


def test_spec_for_is_dataset_aware() -> None:
    assert seed.spec_for("v1", 12).filename == world.DOCUMENT_BY_NO[12].filename
    assert seed.spec_for("v1", 14) is None  # twelve case documents
    assert seed.spec_for("v2", 14).content == "email_body"
    assert seed.spec_for("v2", 26).party.canonical_name == "Berliner Blumen GmbH"
    assert seed.spec_for("live", 0) is None and seed.spec_for("v1", 99) is None


def test_stored_path_is_relative_inside_the_project_and_absolute_outside(tmp_path) -> None:
    from app import config

    assert seed.stored_path(config.BASE_DIR / "data" / "invoices" / "x.pdf") == "data/invoices/x.pdf"
    outside = tmp_path / "bucket" / "inbound" / "x.pdf"
    assert seed.stored_path(outside) == outside.resolve().as_posix()


def test_add_missing_columns_upgrades_an_older_database(tmp_path) -> None:
    """A database created before phase 3 (inbound_document without the new columns) gets them, with defaults."""
    from sqlalchemy import create_engine, inspect, text

    from app import models

    engine = create_engine(f"sqlite:///{(tmp_path / 'old.db').as_posix()}")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE inbound_document (id INTEGER PRIMARY KEY, doc_id VARCHAR(20), "
                          "scenario VARCHAR(8), doc_type VARCHAR(12))"))
        conn.execute(text("INSERT INTO inbound_document (doc_id, scenario, doc_type) "
                          "VALUES ('B-01', 'tobe', 'invoice')"))
    added = models.add_missing_columns(engine)
    assert {"inbound_document.dataset", "inbound_document.content_type", "inbound_document.email_body",
            "inbound_document.message_id", "inbound_document.source_name"} <= set(added)
    assert all(a.startswith("inbound_document.") for a in added)  # tables that do not exist are left alone
    with engine.connect() as conn:
        row = conn.execute(text("SELECT dataset, content_type, email_body FROM inbound_document")).one()
    assert tuple(row) == ("v1", "pdf", None)
    assert models.add_missing_columns(engine) == []  # idempotent
    assert "message_id" in {c["name"] for c in inspect(engine).get_columns("inbound_document")}
