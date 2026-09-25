"""Simulated durations (business days). Every number here is declared in docs/ASSUMPTIONS.md.

A lookup table plus a seeded random for the as-is email loop; nothing else.
"""
from __future__ import annotations

import random
from datetime import date, datetime, timedelta
from typing import Iterable, Optional, TypeVar

# Business days per activity. Source: brief section 10 (as-is values calibrated to the case's
# 26-day average cycle; to-be values assume the control gate and SLAs described in section 9).
DURATIONS: dict[str, dict[str, int]] = {
    "asis": {
        "store_forwarding": 7,  # store mailbox -> forwarded to AP
        "ap_open_and_key": 1,  # AP opens ap@ and keys the invoice
        "email_loop_min": 8,  # untracked back-and-forth, uniform 8..16 (mean 12)
        "email_loop_max": 16,
        "email_approval": 4,
        "posting": 1,
    },
    "tobe": {
        "registration": 0,  # registered on arrival
        "extraction_and_gate": 0,  # minutes
        "workflow_approval": 1,
        "posting": 0,
    },
}

# Seed for the as-is email-loop duration (documented in ASSUMPTIONS.md).
EMAIL_LOOP_SEED = 2026

# The exception cockpit is a snapshot taken at the close of the business day on which the last item of the
# queue was registered (to-be: Mon 5 Oct 2026). Ages are counted to it, so the queue reads as it would that
# evening; the simulation itself assumes every SLA is met.
COCKPIT_CLOSE_HOUR = 17

D = TypeVar("D", date, datetime)


def add_business_days(start: D, days: int) -> D:
    """Add `days` business days (Mon–Fri; no public holidays) to a date or datetime."""
    current = start
    remaining = days
    while remaining > 0:
        current = current + timedelta(days=1)
        if current.weekday() < 5:
            remaining -= 1
    return current


def business_days_between(start: date | datetime, end: date | datetime) -> int:
    """Number of business days from start (exclusive) to end (inclusive). 0 if end <= start."""
    s = start.date() if isinstance(start, datetime) else start
    e = end.date() if isinstance(end, datetime) else end
    count = 0
    while s < e:
        s += timedelta(days=1)
        if s.weekday() < 5:
            count += 1
    return count


def cockpit_as_of(registered: Iterable[Optional[datetime]]) -> Optional[datetime]:
    """Close of the business day of the latest registration in the queue (None for an empty queue)."""
    dates = [d for d in registered if d is not None]
    if not dates:
        return None
    return max(dates).replace(hour=COCKPIT_CLOSE_HOUR, minute=0, second=0, microsecond=0)


def registration_delay_days(scenario: str, channel: str) -> int:
    """Business days between receipt and registration (brief section 6)."""
    if scenario == "tobe":
        return DURATIONS["tobe"]["registration"]
    if channel == "store_mailbox":
        return DURATIONS["asis"]["store_forwarding"]
    return DURATIONS["asis"]["ap_open_and_key"]


def registration_date(scenario: str, channel: str, received_on: datetime) -> datetime:
    return add_business_days(received_on, registration_delay_days(scenario, channel))


def email_loop_days(doc_key: str) -> int:
    """Seeded, repeatable duration of the as-is email loop for one document (8..16 business days)."""
    rng = random.Random(f"{EMAIL_LOOP_SEED}:{doc_key}")
    return rng.randint(DURATIONS["asis"]["email_loop_min"], DURATIONS["asis"]["email_loop_max"])


# --------------------------------------------------------------------------------------------
# Simulated cycle time per document (business days from receipt to posting / resolution)
# --------------------------------------------------------------------------------------------

# Paths a document can take through the process.
#   as-is: "matched" (posted by the quick-fix tool), "email_loop" (untracked follow-up, then posted)
#   to-be: "touchless" (posted, blocked duplicate or applied credit with no human step),
#          "exception" (resolved by the owner within the SLA, assumed met)
PATHS = ("matched", "email_loop", "touchless", "exception")


def cycle_breakdown(scenario: str, path: str, *, channel: str, doc_key: str, sla_days: int = 0,
                    approval: bool = False) -> dict[str, int]:
    """Business days per activity for one document. Sum of the values = the simulated cycle time.

    as-is: store forwarding 7 (store mailbox only) + AP opening and keying 1, then either posting 1
           (matched) or the seeded email loop (8..16) + email approval 4 + posting 1.
    to-be: registration, extraction, gate and posting take 0; an exception adds its SLA (assumed met)
           plus a workflow approval of 1 day when the resolution needs one.
    """
    if path not in PATHS:
        raise ValueError(f"unknown path {path!r}")
    if scenario == "asis":
        d = DURATIONS["asis"]
        steps: dict[str, int] = {}
        if channel == "store_mailbox":
            steps["store_forwarding"] = d["store_forwarding"]
        steps["ap_open_and_key"] = d["ap_open_and_key"]
        if path == "email_loop":
            steps["email_loop"] = email_loop_days(doc_key)
            steps["email_approval"] = d["email_approval"]
        steps["posting"] = d["posting"]
        return steps
    d = DURATIONS["tobe"]
    steps = {"registration": d["registration"], "extraction_and_gate": d["extraction_and_gate"]}
    if path == "exception":
        steps["exception_sla"] = sla_days
        if approval:
            steps["workflow_approval"] = d["workflow_approval"]
    steps["posting"] = d["posting"]
    return steps


def cycle_days(scenario: str, path: str, *, channel: str, doc_key: str, sla_days: int = 0,
               approval: bool = False) -> int:
    return sum(cycle_breakdown(scenario, path, channel=channel, doc_key=doc_key, sla_days=sla_days,
                               approval=approval).values())
