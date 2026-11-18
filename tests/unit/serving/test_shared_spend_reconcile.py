"""Which day a reconciliation counts a recovered charge on.

A reservation's own settlement counts each cost on the UTC day its billing
point recorded it. The reconciliation of a process that died counted it on the
day the request's log row was written, which is later: the next day's budget
for a cost recorded just before midnight, and the next month's at a month's
end. The row now carries the days, and the reconciliation uses them.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from serving.shared_spend import reconcile

pytestmark = pytest.mark.asyncio

USER = "u-reconcile"
ADMITTED = datetime(2031, 3, 31, 23, 59, 50, tzinfo=UTC)


class _Backend:
    """One active reservation of a gone process, and the log rows written under it."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    async def reservations_of(self, instance_id, *, limit, exclude=()):
        return [
            {
                "reservation_id": "sr_1",
                "user_id": USER,
                "api_key_id": 7,
                "project_id": "p-1",
                "amount": Decimal("0.300000"),
                "operation": "request",
                "created_at": ADMITTED,
            }
        ]

    async def request_log_rows(self, reservations):
        return {"sr_1": self.rows}


def _row(charged: str | None, days: Any = None, *, written: str = "2031-04-01", **more) -> dict:
    return {
        "request_id": more.get("request_id", "req_1"),
        "day": written,
        "cost_amount": Decimal("0.2"),
        "charged_amount": charged,
        "charged_days": days,
        "status_code": 200,
    }


async def _lines(rows: list[dict[str, Any]]) -> list[tuple[str, str, int]]:
    (entry,) = await reconcile.plan(_Backend(rows), "inst-gone")
    assert all(
        (line.user_id, line.api_key_id, line.project_id) == (USER, 7, "p-1")
        for line in entry.member.costs
    )
    return [(line.day, str(line.cost_amount), line.requests) for line in entry.member.costs]


async def test_a_charge_recorded_before_midnight_is_counted_on_its_own_day():
    # Charged $0.20 on 31 March, logged on 1 April: March's budget, not April's.
    days = [{"day": "2031-03-31", "amount": "0.200000", "requests": 1}]
    rows = [_row("0.200000", days)]
    assert await _lines(rows) == [("2031-03-31", "0.200000", 1)]

    (entry,) = await reconcile.plan(_Backend(rows), "inst-gone")
    assert entry.evidence[0]["day"] == "2031-04-01"  # when the row was written
    assert entry.evidence[0]["charged_days"] == {"2031-03-31": "0.200000"}
    shown = reconcile.describe(entry)
    assert shown["charge_amount"] == "0.200000"
    assert shown["charge_days"] == {"2031-03-31": "0.200000"}


async def test_a_request_billed_on_both_sides_of_midnight_is_counted_on_both_days():
    days = [
        {"day": "2031-03-31", "amount": "0.120000", "requests": 2},
        {"day": "2031-04-01", "amount": "0.050000", "requests": 1},
    ]
    assert await _lines([_row("0.170000", days)]) == [
        ("2031-03-31", "0.120000", 2),
        ("2031-04-01", "0.050000", 1),
    ]


@pytest.mark.parametrize(
    "days",
    [
        None,  # a row that does not say
        [],
        "2031-03-31",
        [["2031-03-31", "0.200000", 1]],
        # Costs that are not the row's charge.
        [{"day": "2031-03-31", "amount": "0.150000", "requests": 1}],
        [{"day": "2031-03-31", "amount": "0", "requests": 1}],
        [{"day": "2031-03-31", "amount": "-0.200000", "requests": 1}],
        [{"day": "2031-03-31", "amount": "a lot", "requests": 1}],
        # A day that is not one, or is there twice.
        [{"day": "31 March", "amount": "0.200000", "requests": 1}],
        [{"day": "2031-3-31", "amount": "0.200000", "requests": 1}],
        [{"day": 20310331, "amount": "0.200000", "requests": 1}],
        [
            {"day": "2031-03-31", "amount": "0.100000", "requests": 1},
            {"day": "2031-03-31", "amount": "0.100000", "requests": 1},
        ],
        # A day outside the reservation's life: admitted 31 March, logged 1 April.
        [{"day": "2031-03-29", "amount": "0.200000", "requests": 1}],
        [{"day": "2031-04-03", "amount": "0.200000", "requests": 1}],
        [{"day": "2030-03-31", "amount": "0.200000", "requests": 1}],
        # Requests that are not a count.
        [{"day": "2031-03-31", "amount": "0.200000", "requests": True}],
        [{"day": "2031-03-31", "amount": "0.200000", "requests": -1}],
        [{"day": "2031-03-31", "amount": "0.200000"}],
    ],
)
async def test_a_row_that_does_not_say_soundly_is_counted_on_the_day_it_was_written(days):
    rows = [_row("0.200000", days)]
    assert await _lines(rows) == [("2031-04-01", "0.200000", 1)]
    (entry,) = await reconcile.plan(_Backend(rows), "inst-gone")
    assert entry.evidence[0]["charged_days"] is None


async def test_clocks_a_little_apart_do_not_lose_the_day():
    # The gateway's clock a moment ahead of the database's, or behind it: a
    # charge dated the day after the row, or the day before the admission.
    for day in ("2031-03-30", "2031-04-02"):
        days = [{"day": day, "amount": "0.200000", "requests": 1}]
        assert await _lines([_row("0.200000", days)]) == [(day, "0.200000", 1)]


async def test_the_largest_charge_brings_its_own_days_and_of_equals_the_last_row_does():
    early = [{"day": "2031-03-31", "amount": "0.100000", "requests": 1}]
    late = [
        {"day": "2031-03-31", "amount": "0.100000", "requests": 1},
        {"day": "2031-04-01", "amount": "0.100000", "requests": 1},
    ]
    # Each row carries the total when it was written: the larger is the charge.
    rows = [_row("0.100000", early), _row("0.200000", late, request_id="req_2")]
    assert await _lines(rows) == [("2031-03-31", "0.100000", 1), ("2031-04-01", "0.100000", 1)]
    assert await _lines(rows[::-1]) == await _lines(rows)
    # Two rows of one charge: the one written last.
    said = [{"day": "2031-03-31", "amount": "0.200000", "requests": 1}]
    assert await _lines([_row("0.200000", None), _row("0.200000", said)]) == [
        ("2031-03-31", "0.200000", 1)
    ]
    assert await _lines([_row("0.200000", said), _row("0.200000", None)]) == [
        ("2031-04-01", "0.200000", 1)
    ]


async def test_a_reservation_charged_nothing_is_given_back_whatever_its_rows_say():
    days = [{"day": "2031-03-31", "amount": "0.200000", "requests": 1}]
    for rows in ([], [_row(None, days)], [_row("0", days)], [_row("0.000000", [])]):
        assert await _lines(rows) == []
