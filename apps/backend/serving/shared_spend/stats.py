"""What one process observed of its own shared spend accounting, since it started.

Plain counters and one fixed-bucket histogram, kept by the process's
:class:`~serving.shared_spend.ledger.SharedSpendLedger` and read through its
``stats()`` by ``/health/deep`` (for an administrator). They answer, for this
process: how long an admission takes, how many were refused for a database that
did not answer, and whether settlements are being written.

They are this process's alone and start again with it. What every process
holds in the database is read from the database: by ``python -m
serving.shared_spend status``, and once an hour by each process's survey
(:func:`survey`), whose last result ``stats()`` carries.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from serving.shared_spend.store import OWNER_GONE

#: Upper bounds, in seconds, of the admission histogram's buckets. One more
#: bucket takes everything slower.
LATENCY_BUCKETS = (0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5)


@dataclass(slots=True)
class Latency:
    """How long something took, each time: a count per bucket, the total and the longest."""

    counts: list[int] = field(default_factory=lambda: [0] * (len(LATENCY_BUCKETS) + 1))
    count: int = 0
    total_seconds: float = 0.0
    longest_seconds: float = 0.0

    def observe(self, seconds: float) -> None:
        """Count one observation of *seconds*."""
        seconds = max(0.0, seconds)
        self.counts[bisect.bisect_left(LATENCY_BUCKETS, seconds)] += 1
        self.count += 1
        self.total_seconds += seconds
        self.longest_seconds = max(self.longest_seconds, seconds)

    def at_most(self, quantile: float) -> float | None:
        """A duration that *quantile* of the observations were within; None with none.

        An upper bound, only as fine as the buckets: the bound of the bucket
        the quantile falls in, or the longest observation when that is the
        last, open bucket.
        """
        if not self.count:
            return None
        rank = quantile * self.count
        seen = 0
        for bound, n in zip(LATENCY_BUCKETS, self.counts, strict=False):
            seen += n
            if seen >= rank:
                return bound
        return round(self.longest_seconds, 6)

    def snapshot(self) -> dict[str, Any]:
        """The histogram and what is read off it."""
        buckets = {f"{bound:g}": n for bound, n in zip(LATENCY_BUCKETS, self.counts, strict=False)}
        buckets["more"] = self.counts[-1]
        return {
            "count": self.count,
            "mean_seconds": round(self.total_seconds / self.count, 6) if self.count else None,
            "p50_at_most_seconds": self.at_most(0.5),
            "p95_at_most_seconds": self.at_most(0.95),
            "p99_at_most_seconds": self.at_most(0.99),
            "longest_seconds": round(self.longest_seconds, 6),
            "buckets_seconds": buckets,
        }


@dataclass(slots=True)
class LedgerStats:
    """One process's counts: admissions by how they ended, and how settlements went."""

    #: ``admitted``, ``refused`` (the budget), ``unavailable`` (the database
    #: did not answer in time: a 503), ``erased``, ``cancelled``, ``failed``.
    admissions: dict[str, int] = field(default_factory=dict)
    #: Every admission's duration, however it ended.
    admission: Latency = field(default_factory=Latency)
    #: Batches the database answered.
    batches_answered: int = 0
    #: Writes that got no answer and were sent again (or will be).
    write_failures: int = 0
    #: Entries the counters refused for their own values, set aside.
    entries_refused: int = 0
    #: Reservations a settlement did not find held as recorded.
    members_unaccounted: int = 0
    #: Reservations with costs that another batch had retired first (an
    #: operator's reconciliation of this process while it ran): costs dropped.
    members_duplicate: int = 0
    #: Batch receipts this process's upkeep deleted.
    receipts_purged: int = 0

    def admitted(self, outcome: str, seconds: float) -> None:
        """Count one admission that ended as *outcome* after *seconds*."""
        self.admissions[outcome] = self.admissions.get(outcome, 0) + 1
        self.admission.observe(seconds)


def stranded(reservations: list[dict[str, Any]]) -> dict[str, Any]:
    """The active reservations whose owner may never settle them, added up.

    *reservations* are the status summary's rows (by instance and state, each
    with its ``owner``). Nothing settles these until their owner does, and a
    process that was killed never will: they count against their account's
    current day and month for as long as they stay. Not hearing from an owner
    does not prove it gone (it may be cut off from the database), so this is
    what an operator looks at first, not a verdict; ``python -m
    serving.shared_spend reconcile`` retires them once someone has checked.
    """
    rows = [row for row in reservations if row["state"] == "active" and row["owner"] in OWNER_GONE]
    return {
        "reservations": sum(int(row["n"]) for row in rows),
        "amount_usd": sum((row["amount_usd"] or 0 for row in rows), Decimal(0)),
        "oldest_age_seconds": max((row["oldest_age_seconds"] or 0 for row in rows), default=None),
        "instances": sorted({row["instance_id"] for row in rows}),
    }


def survey(summary: dict[str, Any]) -> dict[str, Any]:
    """What every process holds, from the status summary: totals by state, and the stranded."""
    rows = summary["reservations"]

    def total(state: str) -> dict[str, Any]:
        of_state = [row for row in rows if row["state"] == state]
        return {
            "reservations": sum(int(row["n"]) for row in of_state),
            "amount_usd": sum((row["amount_usd"] or 0 for row in of_state), Decimal(0)),
            "oldest_age_seconds": max(
                (row["oldest_age_seconds"] or 0 for row in of_state), default=None
            ),
        }

    return {
        "active": total("active"),
        "quarantined": total("quarantined"),
        "stranded": stranded(rows),
        "receipts": summary.get("receipts"),
    }
