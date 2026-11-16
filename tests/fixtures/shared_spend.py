"""An in-memory stand-in for :class:`serving.shared_spend.store.SharedSpendStore`.

For unit and API tests of the shared ledger's own logic and of its call sites.
It keeps the protocol's outcomes (admitted / refused, settled / released /
missing) but none of its concurrency: what the real statements
guarantee is tested against PostgreSQL in
``tests/integration/storage/test_shared_spend_postgres.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
from decimal import Decimal
from typing import Any

from serving.shared_spend.store import AdmissionOutcome, SettlementResult, SpendSnapshot


class FakePool:
    """A pool whose connections are nothing: the fake backend ignores them."""

    @contextlib.asynccontextmanager
    async def acquire(self, *, timeout: float | None = None):
        yield None


class FakeSharedBackend:
    """Reservations and counters in dicts; failures and holds injectable."""

    def __init__(self) -> None:
        self.pool = FakePool()
        self.request_pool = FakePool()
        self.fence_secret = None
        self.rows: dict[str, tuple[Any, Decimal]] = {}
        self.counters: dict[str, Decimal] = {}
        self.batches: list[Any] = []
        self.fail_admit: BaseException | None = None
        self.fail_settle: BaseException | None = None
        self.settle_gate: asyncio.Event | None = None
        self.settling = asyncio.Event()
        self._xid = 1000
        # Closures recorded (instance id -> by whom), each purge asked for
        # (instance id, the batch ids it kept), the answers the next purges
        # give, and the rows the survey reads.
        self.closures: dict[str, str] = {}
        self.fail_close: BaseException | None = None
        self.purges: list[tuple[str, list[str]]] = []
        self.purge_answers: list[tuple[int, int]] = []
        self.open_rows: list[dict[str, Any]] = []
        self.fail_summary: BaseException | None = None

    def held(self, user_id: str) -> Decimal:
        return sum((a for r, a in self.rows.values() if r.user_id == user_id), Decimal(0))

    async def admit_on(self, conn, request, decide, *, budget_ms, on_xid, pause=None):
        if self.fail_admit is not None:
            raise self.fail_admit
        self._xid += 1
        on_xid(self._xid)
        zero = Decimal(0)
        held = self.held(request.user_id)
        snapshot = SpendSnapshot(
            account_day=self.counters.get(request.user_id, zero),
            account_month=self.counters.get(request.user_id, zero),
            key_day=zero,
            key_month=zero,
            project_day=zero,
            project_month=zero,
            reserved_account_day=held,
            reserved_account_month=held,
            reserved_key_day=zero,
            reserved_key_month=zero,
            reserved_project_day=zero,
            reserved_project_month=zero,
        )
        amount = decide(snapshot)
        if amount is None:
            return AdmissionOutcome("refused", snapshot=snapshot)
        self.rows[request.reservation_id] = (request, amount)
        return AdmissionOutcome("admitted", reserved_usd=amount, snapshot=snapshot)

    async def transaction_status(self, xids):
        return dict.fromkeys(xids, "aborted")

    async def settle(self, batch, *, pause=None):
        self.settling.set()
        if self.settle_gate is not None:
            await self.settle_gate.wait()
        if self.fail_settle is not None:
            raise self.fail_settle
        result = SettlementResult(batch.batch_id, "applied")
        costs = list(batch.increments)
        for member in batch.members:
            if self.rows.pop(member.reservation_id, None) is None:
                result.add("missing", member.reservation_id)
                continue
            result.add("settled" if member.costs else "released", member.reservation_id)
            costs.extend(member.costs)
        for line in costs:
            self.counters[line.user_id] = self.counters.get(line.user_id, Decimal(0)) + (
                line.cost_usd
            )
        self.batches.append(batch)
        return result

    async def quarantine(self, member, instance_id, *, day, note):
        return True

    # -- closure, receipt retention and the survey: what was asked, and canned answers --

    async def close_instance(self, instance_id, *, closed_by, operator=None, reason=None):
        if self.fail_close is not None:
            raise self.fail_close
        new = instance_id not in self.closures
        self.closures.setdefault(instance_id, closed_by)
        return new

    async def purge_receipts(self, instance_id, unanswered, **kwargs):
        self.purges.append((instance_id, sorted(unanswered)))
        return self.purge_answers.pop(0) if self.purge_answers else (0, 0)

    async def purge_closures(self, **kwargs):
        return 0

    async def summary(self):
        if self.fail_summary is not None:
            raise self.fail_summary
        return {"reservations": list(self.open_rows), "receipts": {"batches": 0, "members": 0}}
