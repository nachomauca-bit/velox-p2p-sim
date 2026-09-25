"""Exception taxonomy: types, plain-English labels, owner roles and SLAs (brief section 9).

Single source of truth for the gate, the exception cockpit, the KPIs and docs/ASSUMPTIONS.md.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class ExceptionType:
    key: str
    label: str
    owner_role: str
    sla_days: Optional[int]  # business days; None = no SLA (as-is email loop, info flags)
    resolution: str
    blocking: bool = True  # False = info task: the document still posts


EXCEPTION_TYPES: list[ExceptionType] = [
    ExceptionType("no_po", "Invoice without purchase order", "Requester, then cost-centre owner approval", 2,
                  "Confirm purchase, raise PO or approve as non-PO under DoA"),
    ExceptionType("po_no_receipt", "PO exists, no receipt or service confirmation", "Receiver / requester", 2,
                  "Confirm receipt or service delivery"),
    ExceptionType("price_qty_mismatch", "Price or quantity outside tolerance", "Buyer", 2,
                  "Agree correction with supplier or approve variance"),
    ExceptionType("po_not_found", "PO number not found or wrong vendor", "Requester", 2, "Provide correct PO"),
    ExceptionType("duplicate_vendor_account", "Supplier has more than one account (info)", "Master Data owner", 5,
                  "Merge or deactivate", blocking=False),
    ExceptionType("unknown_vendor", "Supplier not in master", "Master Data owner", 2,
                  "Onboard through the vendor request workflow"),
    ExceptionType("wrong_legal_entity", "Billed to the wrong Velox entity", "AP specialist", 1,
                  "Ask supplier to re-issue, or re-assign"),
    ExceptionType("duplicate_invoice", "Same invoice already registered or posted", "AP specialist", 0,
                  "Reply to supplier with status"),
    ExceptionType("credit_note_without_invoice", "Credit note references no known invoice", "AP specialist", 2,
                  "Identify original invoice"),
    ExceptionType("human_review", "Low extraction confidence", "AP specialist", 1,
                  "Verify fields against the document"),
    # Document-type check of the to-be gate (e.g. a supplier statement): nothing is posted.
    ExceptionType("not_an_invoice", "Document is not an invoice", "AP specialist", 1,
                  "File the statement or reconcile it with the open items"),
    # Non-blocking info flag of gate step 8 (terms taken from the master; the variance is reported).
    ExceptionType("terms_variance", "Invoice payment terms differ from the master (info)", "AP specialist", None,
                  "Confirm terms with the supplier; the master terms apply", blocking=False),
    # As-is only: nothing is tracked by type, nobody owns it.
    ExceptionType("email_loop", "Untracked manual follow-up", "—", None, "—"),
]

BY_KEY: dict[str, ExceptionType] = {t.key: t for t in EXCEPTION_TYPES}
INFO_TYPES = frozenset(t.key for t in EXCEPTION_TYPES if not t.blocking)


def get(key: str) -> ExceptionType:
    return BY_KEY[key]


def label(key: Optional[str]) -> str:
    return BY_KEY[key].label if key in BY_KEY else (key or "")
