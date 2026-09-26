"""Simulated calendar and durations (brief sections 6 and 10; app/sim.py)."""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from app import sim, world

# 2026-10-01 is a Thursday.
THU = date(2026, 10, 1)
FRI = date(2026, 10, 2)
SAT = date(2026, 10, 3)
SUN = date(2026, 10, 4)
MON = date(2026, 10, 5)


# --------------------------------------------------------------------------------------------
# Business-day calendar (Mon–Fri, no public holidays)
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("start, days, expected", [
    (THU, 0, THU),
    (THU, 1, FRI),
    (FRI, 1, MON),  # Fri + 1 = Mon
    (SAT, 1, MON),  # weekend start: the next business day
    (SUN, 1, MON),
    (FRI, 5, date(2026, 10, 9)),  # one full week
    (THU, 7, date(2026, 10, 12)),  # the store-forwarding delay
    (MON, 10, date(2026, 10, 19)),
])
def test_add_business_days_on_dates(start: date, days: int, expected: date) -> None:
    assert sim.add_business_days(start, days) == expected


def test_add_business_days_keeps_datetime_and_time_of_day() -> None:
    received = datetime(2026, 10, 2, 16, 5)  # Friday afternoon
    result = sim.add_business_days(received, 1)
    assert isinstance(result, datetime)
    assert result == datetime(2026, 10, 5, 16, 5)  # Monday, same time


def test_add_business_days_never_lands_on_a_weekend() -> None:
    for offset in range(14):
        start = THU + timedelta(days=offset)
        for days in range(1, 12):
            assert sim.add_business_days(start, days).weekday() < 5


@pytest.mark.parametrize("start, end, expected", [
    (THU, THU, 0),
    (FRI, THU, 0),  # end before start
    (THU, FRI, 1),
    (FRI, MON, 1),  # the weekend does not count
    (FRI, SUN, 0),
    (MON, date(2026, 10, 12), 5),
    (datetime(2026, 10, 2, 17, 0), datetime(2026, 10, 5, 8, 0), 1),  # datetimes compare by date
])
def test_business_days_between(start, end, expected: int) -> None:
    assert sim.business_days_between(start, end) == expected


def test_business_days_between_inverts_add_business_days() -> None:
    for offset in range(14):
        start = THU + timedelta(days=offset)
        for days in range(0, 12):
            assert sim.business_days_between(start, sim.add_business_days(start, days)) == days


# --------------------------------------------------------------------------------------------
# Registration per scenario and channel (brief section 6)
# --------------------------------------------------------------------------------------------

# Expected as-is registration of the 12 case documents: ap@ +1 business day, store mailbox +7.
ASIS_REGISTERED_ON = {
    1: datetime(2026, 10, 2, 8, 42),
    2: datetime(2026, 10, 13, 8, 15),  # store mailbox, received Fri 2 Oct
    3: datetime(2026, 10, 2, 9, 15),
    4: datetime(2026, 10, 5, 11, 30),  # received Fri -> Mon
    5: datetime(2026, 10, 5, 14, 2),  # received Fri 2 Oct (the demo's "Receive next email")
    6: datetime(2026, 9, 29, 10, 21),  # received Mon 28 Sep
    7: datetime(2026, 10, 2, 7, 55),
    8: datetime(2026, 10, 2, 10, 33),
    9: datetime(2026, 10, 12, 12, 10),  # store mailbox, received Thu 1 Oct
    10: datetime(2026, 10, 5, 9, 5),  # received Fri -> Mon
    11: datetime(2026, 10, 5, 14, 20),  # received Fri -> Mon
    12: datetime(2026, 10, 2, 16, 40),
}


def test_registration_delay_days() -> None:
    assert sim.registration_delay_days("tobe", "ap_mailbox") == 0
    assert sim.registration_delay_days("tobe", "store_mailbox") == 0
    assert sim.registration_delay_days("asis", "ap_mailbox") == 1
    assert sim.registration_delay_days("asis", "store_mailbox") == 7


@pytest.mark.parametrize("spec", world.DOCUMENTS, ids=lambda d: f"doc{d.no:02d}")
def test_tobe_registers_every_document_on_arrival(spec: world.DocumentSpec) -> None:
    assert sim.registration_date("tobe", spec.channel, spec.received_on) == spec.received_on


@pytest.mark.parametrize("spec", world.DOCUMENTS, ids=lambda d: f"doc{d.no:02d}")
def test_asis_registration_date(spec: world.DocumentSpec) -> None:
    registered = sim.registration_date("asis", spec.channel, spec.received_on)
    assert registered == ASIS_REGISTERED_ON[spec.no]
    delay = 7 if spec.channel == "store_mailbox" else 1
    assert sim.business_days_between(spec.received_on, registered) == delay


def test_expected_dates_cover_all_documents_and_both_mailboxes() -> None:
    assert set(ASIS_REGISTERED_ON) == {d.no for d in world.DOCUMENTS}
    assert {d.no for d in world.DOCUMENTS if d.channel == "store_mailbox"} == {2, 9}


# --------------------------------------------------------------------------------------------
# Durations and the seeded email loop (brief section 10)
# --------------------------------------------------------------------------------------------


def test_asis_store_non_po_cycle_is_about_25_days() -> None:
    """Store forwarding + AP keying + mean email loop + approval + posting = 7 + 1 + 12 + 4 + 1."""
    asis = sim.DURATIONS["asis"]
    mean_loop = (asis["email_loop_min"] + asis["email_loop_max"]) / 2
    total = asis["store_forwarding"] + asis["ap_open_and_key"] + mean_loop + asis["email_approval"] + asis["posting"]
    assert mean_loop == 12
    assert total == 25


def test_tobe_touchless_path_takes_at_most_one_day() -> None:
    assert sum(sim.DURATIONS["tobe"].values()) <= 1


def test_email_loop_days_is_deterministic_per_key() -> None:
    for key in ("A-01", "A-02", "A-05", "A-10"):
        assert sim.email_loop_days(key) == sim.email_loop_days(key)


def test_email_loop_days_stays_within_8_to_16() -> None:
    values = [sim.email_loop_days(f"doc-{i}") for i in range(500)]
    assert min(values) >= 8 and max(values) <= 16
    assert len(set(values)) > 1  # it does vary between documents


def test_email_loop_days_mean_is_about_12() -> None:
    values = [sim.email_loop_days(f"doc-{i}") for i in range(500)]
    assert 11 <= sum(values) / len(values) <= 13


def test_cockpit_snapshot_is_the_close_of_the_last_registration_day() -> None:
    registered = [datetime(2026, 10, 1, 14, 2), None, datetime(2026, 10, 5, 8, 30), datetime(2026, 10, 2, 10, 21)]
    assert sim.cockpit_as_of(registered) == datetime(2026, 10, 5, 17, 0)
    assert sim.cockpit_as_of([]) is None
