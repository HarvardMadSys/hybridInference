"""The instance reporter's writer loop and its prune of expired registrations.

The loop runs against a simulated database: each report arrives one heartbeat
after the previous one on a fake monotonic clock, and wakes the loop at once
instead of after a real wait, so hours of reporting take a moment.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from serving.config import instances

#: Simulated seconds between two reports.
_BEAT = 10.0
_HOUR = 3600.0


class _Database:
    """A pool whose reports each arrive one simulated heartbeat after the last.

    A report at or before ``down_until`` fails, and a prune raises
    ``prune_error`` when one is given. The run ends with the report at
    ``until``: the loop is no longer woken, and ``finished`` is set.
    """

    def __init__(
        self, *, until: float, down_until: float = 0.0, prune_error: Exception | None = None
    ) -> None:
        self.now = 0.0
        self.until = until
        self.down_until = down_until
        self.prune_error = prune_error
        self.reporter: instances.InstanceReporter | None = None
        self.reports = 0
        self.prunes: list[float] = []
        self.prune_calls: set[tuple[str, tuple[object, ...]]] = set()
        self.finished = asyncio.Event()

    def clock(self) -> float:
        return self.now

    def acquire(self) -> _Connection:
        return _Connection(self)


class _Connection:
    def __init__(self, database: _Database) -> None:
        self.database = database

    async def __aenter__(self) -> _Connection:
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False

    async def fetchval(self, statement: str, *args: object) -> object:
        """A report, one heartbeat after the previous one."""
        db = self.database
        if db.finished.is_set():
            return args[5]  # The final "stopped" report.
        db.now += _BEAT
        db.reports += 1
        if db.now >= db.until:
            db.finished.set()
        else:
            # The heartbeat timer firing, without its real wait.
            db.reporter._wake.set()
        if db.now <= db.down_until:
            raise ConnectionError("database unavailable")
        return args[5]  # The report sequence, as stored.

    async def execute(self, statement: str, *args: object) -> str:
        """A prune."""
        db = self.database
        db.prunes.append(db.now)
        db.prune_calls.add((statement, args))
        if db.prune_error is not None:
            raise db.prune_error
        return "DELETE 0"


async def _run_until_finished(database: _Database) -> None:
    reporter = instances.InstanceReporter(
        database, "prune-test", datetime.now(UTC), {}, clock=database.clock
    )
    database.reporter = reporter
    reporter.set_lifecycle("running")  # The first report goes out at once.
    reporter.start()
    try:
        await asyncio.wait_for(database.finished.wait(), timeout=10)
    finally:
        await reporter.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("draw", "expected"),
    [
        # The draw puts the first prune at a point within the first hour; it
        # runs at the first report from then on. Each later prune runs at the
        # first report a whole hour after the previous one.
        (0.0, [10.0, 3610.0, 7210.0, 10810.0]),
        (0.5, [1800.0, 5400.0, 9000.0, 12600.0]),
        (0.999, [3600.0, 7200.0, 10800.0, 14400.0]),
    ],
    ids=["first-report", "half-past", "end-of-hour"],
)
async def test_the_loop_prunes_at_most_once_an_hour_from_a_random_first_time(
    monkeypatch, draw, expected
):
    monkeypatch.setattr(instances.random, "random", lambda: draw)
    database = _Database(until=4 * _HOUR)

    await _run_until_finished(database)

    assert database.reports == 1440  # A report every heartbeat...
    assert database.prunes == expected  # ...and a prune once an hour.
    assert database.prune_calls == {(instances.PRUNE_SQL, (instances.RETENTION_SECONDS,))}


@pytest.mark.asyncio
async def test_a_prune_due_while_reports_fail_waits_for_the_next_stored_report(monkeypatch):
    monkeypatch.setattr(instances.random, "random", lambda: 0.0)  # Due at the first report.
    # The reports at 10, 20 and 30 seconds fail; the one at 40 is stored.
    database = _Database(until=2 * _HOUR, down_until=30.0)

    await _run_until_finished(database)

    assert database.prunes == [40.0, 3640.0]


@pytest.mark.asyncio
async def test_a_failed_prune_is_logged_and_retried_an_hour_later_while_reports_go_on(
    monkeypatch, caplog
):
    monkeypatch.setattr(instances.random, "random", lambda: 0.0)
    database = _Database(until=2 * _HOUR, prune_error=OSError("SYNTHETIC-DRIVER-TEXT"))

    await _run_until_finished(database)

    assert database.reports == 720
    assert database.prunes == [10.0, 3610.0]
    assert "Configuration instance prune failed (OSError)" in caplog.text
    # The class name only: a driver message can quote the statement.
    assert "SYNTHETIC-DRIVER-TEXT" not in caplog.text
