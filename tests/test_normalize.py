"""Normalisation helpers used by the vendor-master view and the control gate (app/normalize.py)."""
from __future__ import annotations

from collections import Counter
from itertools import combinations

import pytest

from app import seed, world
from app.normalize import (
    LEGAL_SUFFIXES,
    NAME_SIMILARITY_THRESHOLD,
    name_similarity,
    names_match,
    normalise_iban,
    normalise_invoice_number,
    normalise_name,
    normalise_vat,
)

# --------------------------------------------------------------------------------------------
# Names
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("raw, expected", [
    ("Nordwind Logistics GmbH", "nordwind logistics"),
    ("NORDWIND LOGISTICS", "nordwind logistics"),
    ("  Nordwind   Logistics  ", "nordwind logistics"),
    ("Clean Space Facilities B.V.", "clean space facilities"),  # "B.V." -> "bv" -> stripped
    ("Cleanspace Facilities BV", "cleanspace facilities"),
    ("Bright Agency SARL", "bright agency"),  # "ag" inside "agency" is not a suffix
    ("Shopsys Software Inc.", "shopsys software"),
    ("Kaffee & Co OHG", "kaffee co"),
    ("Metro-Media GmbH", "metro media"),
    ("QuickPrint S.A.S.", "quickprint"),
    ("Lumen Store Lighting Limited", "lumen store lighting"),
    ("SecureNet AG", "securenet"),
    ("Atlas Displays SL", "atlas displays"),
    (None, ""),
    ("", ""),
])
def test_normalise_name(raw, expected: str) -> None:
    assert normalise_name(raw) == expected


def test_legal_suffixes_cover_the_brief_list() -> None:
    assert {"gmbh", "sarl", "ltd", "inc", "sl", "bv", "ag", "ohg"} <= LEGAL_SUFFIXES
    assert NAME_SIMILARITY_THRESHOLD == 90


@pytest.mark.parametrize("dup", seed.D1_DUPLICATES, ids=lambda d: d[0])
def test_d1_spelling_duplicates_match_their_canonical_name(dup) -> None:
    new_id, source_id, display_name, *_ = dup
    canonical = world.CLEAN_ACCOUNT_BY_ID[source_id].display_name
    assert names_match(display_name, canonical), (new_id, name_similarity(display_name, canonical))


@pytest.mark.parametrize("a, b", [
    ("Nordwind Logistics GmbH", "NORDWIND LOGISTICS"),
    ("Nordwind Logistics GmbH", "Nordwind Logistik GmbH"),
    ("Cleanspace Facilities BV", "Clean Space Facilities B.V."),
    ("Metro Media GmbH", "Metro-Media GmbH"),
    ("Bright Agency SARL", "Bright Agency"),
])
def test_names_match_on_seed_duplicate_pairs(a: str, b: str) -> None:
    assert names_match(a, b)
    assert name_similarity(a, b) == name_similarity(b, a)


@pytest.mark.parametrize("a, b", [
    ("Metro Media GmbH", "Nordwind Logistics GmbH"),
    ("Bright Agency SARL", "Metro Media GmbH"),
    ("QuickPrint SAS", "Kaffee & Co OHG"),
])
def test_different_suppliers_do_not_match(a: str, b: str) -> None:
    assert not names_match(a, b)


def test_no_two_parties_match_by_name() -> None:
    for p, q in combinations(world.PARTIES, 2):
        assert not names_match(p.canonical_name, q.canonical_name), (p.canonical_name, q.canonical_name)


@pytest.mark.parametrize("spec", world.DOCUMENTS, ids=lambda d: f"doc{d.no:02d}")
def test_printed_supplier_name_matches_its_party(spec: world.DocumentSpec) -> None:
    """Includes the traps: doc 2 'NORDWIND LOGISTICS' and doc 4 'Bright Agency'."""
    assert names_match(spec.printed_supplier_name, spec.party.canonical_name)


def test_empty_names_never_match() -> None:
    assert name_similarity(None, "Nordwind Logistics GmbH") == 0.0
    assert name_similarity("GmbH", "GmbH") == 0.0  # nothing left after stripping the suffix
    assert not names_match("", "")


def test_names_match_threshold_is_a_parameter() -> None:
    score = name_similarity("Nordwind Logistics GmbH", "Nordwind Logistik GmbH")
    assert 90 <= score < 100
    assert not names_match("Nordwind Logistics GmbH", "Nordwind Logistik GmbH", threshold=100)


# --------------------------------------------------------------------------------------------
# VAT IDs and bank accounts
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("raw, expected", [
    ("DE 281 947 305", "DE281947305"),
    ("de281947305", "DE281947305"),
    ("FR 62 512 345 678", "FR62512345678"),
    ("NL859374612B01", "NL859374612B01"),
    ("ESB86419273", "ESB86419273"),
    ("47-3829105", "473829105"),
    ("EIN 47-3829105", "473829105"),  # US EIN with prefix
    ("CHE-419.287.563", "CHE419287563"),
    ("CHE-419.287.563 MWST", "CHE419287563"),  # Swiss UID with VAT suffix
    ("CHE-419.287.563 TVA", "CHE419287563"),
    ("CHE-419.287.563 IVA", "CHE419287563"),
    (None, ""),
    ("", ""),
])
def test_normalise_vat(raw, expected: str) -> None:
    assert normalise_vat(raw) == expected


@pytest.mark.parametrize("raw, expected", [
    ("DE44 5001 0517 5407 3249 31", "DE44500105175407324931"),
    ("de44500105175407324931", "DE44500105175407324931"),
    ("ABA 121000248 ACCT 4839201756", "ABA121000248ACCT4839201756"),  # US bank string
    (None, ""),
    ("", ""),
])
def test_normalise_iban(raw, expected: str) -> None:
    assert normalise_iban(raw) == expected


@pytest.mark.parametrize("party", world.PARTIES, ids=lambda p: p.party_id)
def test_printed_bank_details_normalise_to_the_stored_value(party: world.PartySpec) -> None:
    """IBANs are printed in blocks of four; the master stores them compact."""
    assert normalise_iban(world.format_iban(party.bank)) == normalise_iban(party.bank)


# --------------------------------------------------------------------------------------------
# Invoice numbers (duplicate check)
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("raw, expected", [
    ("NWL-2026-00913", "NWL2026913"),
    ("NWL 2026 913", "NWL2026913"),  # separators and leading zeros
    ("NWL/2026/0913", "NWL2026913"),
    ("NWL-2026-00913 (COPY)", "NWL2026913"),  # copy / reminder wording
    ("Reminder NWL-2026-00913", "NWL2026913"),
    ("nwl-2026-00913 copy", "NWL2026913"),
    ("DUPLICATE NWL-2026-00913", "NWL2026913"),
    ("KOPIE NWL-2026-00913", "NWL2026913"),
    ("INV-2026-0457", "INV2026457"),
    ("2026/117", "2026117"),
    ("AD-2026/0788", "AD2026788"),
    ("0000", "0"),
    (None, ""),
    ("", ""),
])
def test_normalise_invoice_number(raw, expected: str) -> None:
    assert normalise_invoice_number(raw) == expected


def test_only_the_nordwind_resend_shares_an_invoice_number() -> None:
    """Among the 12 sample documents, documents 1 and 2 are the only normalised duplicate."""
    counts = Counter(normalise_invoice_number(d.invoice_number) for d in world.DOCUMENTS)
    duplicates = {number for number, n in counts.items() if n > 1}
    assert duplicates == {normalise_invoice_number(world.DOCUMENT_BY_NO[1].invoice_number)}
    assert world.DOCUMENT_BY_NO[1].invoice_number == world.DOCUMENT_BY_NO[2].invoice_number


def test_credit_note_number_differs_from_the_invoice_it_references() -> None:
    credit_note = world.DOCUMENT_BY_NO[4]
    assert normalise_invoice_number(credit_note.invoice_number) != \
        normalise_invoice_number(credit_note.referenced_invoice_number)


@pytest.mark.parametrize("printed", [
    "ABA routing 121000248 · Account 4839201756",  # as printed on the Shopsys PDF
    "ABA 121000248 ACCT 4839201756",  # as stored in the vendor master
    "aba: 121000248 / acct 4839201756",
])
def test_normalise_iban_maps_us_bank_details_to_one_form(printed):
    assert normalise_iban(printed) == "ABA121000248ACCT4839201756"


def test_normalise_name_folds_accents():
    assert normalise_name("Zürcher Kantonalbank AG") == "zurcher kantonalbank"
    assert normalise_name("Société Générale SA") == "societe generale"
