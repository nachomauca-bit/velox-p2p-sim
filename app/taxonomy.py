"""Exception taxonomy: the twelve types of the deck's appendix A3, with owner roles and SLAs (brief v2 section 3).

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
    sla_days: Optional[int]  # business days; None = no SLA ("—" in A3)
    resolution: str
    blocking: bool = True  # False = info task: the document still posts
    outcome: str = "exception"  # the gate outcome: exception | human_review | blocked_duplicate


EXCEPTION_TYPES: list[ExceptionType] = [
    ExceptionType("no_po", "No PO", "Requester, then budget owner", 2,
                  "Confirm the purchase and raise the commitment; the budget owner approves"),
    ExceptionType("po_no_receipt", "PO exists, no receipt or confirmation", "Receiver / requester", 2,
                  "Confirm receipt or service delivery"),
    ExceptionType("price_qty_mismatch", "Price or quantity mismatch", "Buyer", 2,
                  "Agree a correction with the supplier or approve the variance"),
    ExceptionType("po_not_found", "PO not found or wrong vendor", "Requester", 2, "Provide the correct PO"),
    ExceptionType("duplicate_vendor_account", "Duplicate vendor record (info)", "Master data owner", 5,
                  "Merge or deactivate the duplicate record", blocking=False),
    ExceptionType("unknown_vendor", "Unknown vendor", "Master data owner", 2,
                  "Onboard the supplier through the vendor request workflow"),
    ExceptionType("wrong_legal_entity", "Wrong legal entity", "AP", 1,
                  "Ask the supplier to re-issue the invoice, or re-assign it"),
    ExceptionType("duplicate_invoice", "Duplicate invoice", "Blocked; AP replies with status", None,
                  "Blocked before posting; AP replies to the supplier with the status", outcome="blocked_duplicate"),
    ExceptionType("credit_note_without_invoice", "Credit note without invoice", "AP", 2,
                  "Identify the original invoice"),
    ExceptionType("human_review", "Low extraction confidence", "AP review", 1,
                  "Verify the fields against the document", outcome="human_review"),
    ExceptionType("payment_status_query", "Supplier payment-status query", "Agent drafts, AP approves", None,
                  "Reply with the payment status: the agent drafts, AP approves before anything is sent"),
    ExceptionType("amount_above_approval_limit", "Amount above approval limit", "Next approver in the matrix", 2,
                  "The next approver decides in the approval workflow", outcome="human_review"),
]

# As-is status, not a type of the taxonomy: nothing is tracked by type and nobody owns it.
EMAIL_LOOP = ExceptionType("email_loop", "Email loop — untracked", "—", None, "—")

BY_KEY: dict[str, ExceptionType] = {t.key: t for t in [*EXCEPTION_TYPES, EMAIL_LOOP]}
INFO_TYPES = frozenset(t.key for t in EXCEPTION_TYPES if not t.blocking)


def get(key: str) -> ExceptionType:
    return BY_KEY[key]


def label(key: Optional[str]) -> str:
    return BY_KEY[key].label if key in BY_KEY else (key or "")
