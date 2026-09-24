"""Simulated durations (business days). Every number here is declared in docs/ASSUMPTIONS.md.

A lookup table plus a seeded random for the as-is email loop; nothing else.
"""
from __future__ import annotations

import random
from datetime import date, datetime, timedelta
from typing import TypeVar

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
