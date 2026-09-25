"""Normalisation helpers shared by the vendor-master view and the control gate. Pure functions."""
from __future__ import annotations

import re
import unicodedata
from typing import Optional

from rapidfuzz import fuzz

# Legal-form suffixes stripped before name comparison (brief section 8, step 3; plus SAS/SA/LLC).
LEGAL_SUFFIXES = frozenset({"gmbh", "sarl", "ltd", "limited", "inc", "sl", "bv", "ag", "ohg", "sas", "sa", "llc"})

NAME_SIMILARITY_THRESHOLD = 90  # rapidfuzz token_set_ratio on normalised names

_COPY_WORDS = re.compile(r"\b(COPY|DUPLICATE|REMINDER|KOPIE|DUPLICATA|COPIE|COPIA)\b")
# An explicit leading label ('Invoice no.', 'Rechnung Nr.', 'Facture N°', 'Copy of invoice #'). The label word is
# required, so numbers such as 'INV-2026-0457' are left alone.
_INVOICE_LABEL = re.compile(r"^(?:OF\s+)?(?:INVOICE|RECHNUNG|FACTURE|FACTURA)\b\s*"
                            r"(?:(?:NUMBER|NUM|NO|NR)(?![A-Z])|N°|Nº|#)?\W*")
# A leading purchase-order label ('PO', 'P.O.', 'Your PO', 'Purchase order no.', 'Order #') and number words.
_PO_LABEL = re.compile(r"^\W*(?:(?:YOUR|OUR)\s+)?(?:PURCHASE\s*ORDER|P\s*O|ORDER)(?![A-Z])\W*")
_NUMBER_LABEL = re.compile(r"^(?:(?:NUMBER|NUM|NO|NR)(?![A-Z])|N°|Nº|#)\W*")
CURRENCY_SYMBOLS = {"€": "EUR", "$": "USD", "US$": "USD", "£": "GBP"}


def normalise_name(name: Optional[str]) -> str:
    """Fold accents, lowercase, drop punctuation and legal suffixes:
    'Clean Space Facilities B.V.' -> 'clean space facilities'."""
    if not name:
        return ""
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower().replace(".", "")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    tokens = [t for t in s.split() if t not in LEGAL_SUFFIXES]
    return " ".join(tokens)


def name_similarity(a: Optional[str], b: Optional[str]) -> float:
    na, nb = normalise_name(a), normalise_name(b)
    if not na or not nb:
        return 0.0
    return float(fuzz.token_set_ratio(na, nb))


def names_match(a: Optional[str], b: Optional[str], threshold: int = NAME_SIMILARITY_THRESHOLD) -> bool:
    return name_similarity(a, b) >= threshold


def normalise_vat(vat: Optional[str]) -> str:
    """Uppercase alphanumerics; drop 'EIN' prefix and Swiss 'MWST/TVA/IVA' suffix: 'DE 281 947 305' -> 'DE281947305'."""
    if not vat:
        return ""
    s = re.sub(r"[^A-Z0-9]", "", vat.upper())
    if s.startswith("EIN"):
        s = s[3:]
    for suffix in ("MWST", "TVA", "IVA"):
        if s.startswith("CHE") and s.endswith(suffix):
            s = s[: -len(suffix)]
    return s


def normalise_iban(iban: Optional[str]) -> str:
    """Uppercase alphanumerics only. US bank details in any wording ('ABA routing 121000248 · Account
    4839201756', 'ABA 121000248 ACCT 4839201756') map to one canonical form 'ABA121000248ACCT4839201756'."""
    if not iban:
        return ""
    aba = re.search(r"ABA\D*(\d{9})\D+(\d+)", iban.upper())
    if aba:
        return f"ABA{aba[1]}ACCT{aba[2]}"
    return re.sub(r"[^A-Z0-9]", "", iban.upper())


def normalise_invoice_number(number: Optional[str]) -> str:
    """Strip 'copy'/'reminder' words, an explicit leading label, spaces/punctuation and leading zeros of digit runs.

    'NWL-2026-00913' -> 'NWL2026913';  'NWL 2026 913 (COPY)' -> 'NWL2026913';  'Invoice no. NWL-2026-00913' ->
    'NWL2026913'.
    """
    if not number:
        return ""
    s = _COPY_WORDS.sub(" ", str(number).upper()).strip()
    s = _INVOICE_LABEL.sub("", s, count=1)
    parts = re.findall(r"[A-Z]+|\d+", s)
    return "".join(p.lstrip("0") or "0" if p.isdigit() else p for p in parts)


def normalise_po_number(number: object) -> str:
    """Strip a leading PO label ('PO', 'P.O.', 'Your PO', 'Purchase order no.', '#') and every separator; the
    digits of a purely numeric PO are kept as printed (no zero stripping).

    'PO 4500117', 'P.O. #4500117', 'po-4500117', ' 4500 117 ' -> '4500117'.
    """
    if number is None:
        return ""
    s = unicodedata.normalize("NFKC", str(number)).upper().replace(".", " ").strip()
    s = _NUMBER_LABEL.sub("", _PO_LABEL.sub("", s, count=1), count=1)
    return re.sub(r"[^A-Z0-9]", "", s)


def normalise_currency(currency: Optional[str]) -> Optional[str]:
    """ISO 4217 code from an extracted currency: trimmed, uppercase, symbols mapped ('€' -> 'EUR', '$' -> 'USD',
    '£' -> 'GBP'); None unless the result is a 3-letter code."""
    if not currency:
        return None
    s = str(currency).strip().upper()
    s = CURRENCY_SYMBOLS.get(s, s)
    return s if re.fullmatch(r"[A-Z]{3}", s) else None
