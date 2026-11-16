"""Two gateways on one database, each through the whole request path.

``tests/integration/storage/test_shared_spend_postgres.py`` drives the shared
ledger directly. Here each "gateway" is an application with the real request
dependencies -- the key's gate (``verify_api_key``, which refuses early on what
it read plus what its own process holds), then ``enforce_user_concurrency``
(the admission in the database) -- its own shared ledger and instance id, and
its own connection pool, on the same PostgreSQL tables.

What one gateway answers a caller, the other must answer too: the two can
differ only in *where* they refuse (the gate of the process that holds a
liability knows it before the database is asked), never in *whether*. And a
request the upstream failed, charged nothing, leaves nothing that either
gateway counts afterwards.

Only the identity lookup is a stand-in (``_store``): it answers the gate's read
of the key and of today's spend, which the tests keep equal to the counter the
admission reads.
"""

from __future__ import annotations

import asyncio
import contextlib
import weakref
from decimal import Decimal
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import Depends, HTTPException

from serving.servers.concurrency import (
    UserConcurrencyLimiter,
    enforce_user_concurrency,
    static_limits_provider,
)
from serving.servers.middleware.exception_handler import install_exception_handlers
from serving.shared_spend import ledger as shared_ledger, mode as spend_mode
from serving.shared_spend.ledger import SharedSpendLedger
from serving.spend_ledger import ledger_for
from serving.storage.postgres_operational import PostgresOperationalStore
from tests.fixtures import postgres
from tests.integration.storage import test_shared_spend_postgres as _base
from tests.servers.test_spend_reservation import KEY_ID, LIMITS, _app, _client, _store

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

USER = _base.PREFIX + "gateways"  # an account the base module's wipe covers


@pytest.fixture
def clock(monkeypatch):
    """The UTC day every ledger reading sees: ``clock.day``, changed by the test."""
    state = SimpleNamespace(day="2031-03-15")
    monkeypatch.setattr("serving.spend_ledger.utc_day", lambda now=None: state.day)
    return state


@pytest_asyncio.fixture
async def gateways(monkeypatch):
    """A factory of two gateways, ``a`` and ``b``, on the test database."""
    monkeypatch.setenv("USER_AUTH_ENABLED", "1")
    monkeypatch.setenv("API_KEY_SECRET", "test-secret")
    monkeypatch.setattr(spend_mode, "_mode", "shared")
    if shared_ledger._LEDGERS is None:
        monkeypatch.setattr(shared_ledger, "_LEDGERS", weakref.WeakKeyDictionary())
    made: list[SimpleNamespace] = []

    async def make(*, quota_usd: float, spent_usd: float = 0.0) -> tuple[SimpleNamespace, ...]:
        for name in ("a", "b"):
            pool = await postgres.create_pool(min_size=1, max_size=4)
            if not made:
                await _base._wipe(pool)
            backend = PostgresOperationalStore(
                pool, fence_secret=postgres.FENCE_SECRET
            ).shared_spend_backend()
            store = _store(quota_usd=quota_usd, spent_usd=spent_usd, user_id=USER)
            store.shared_spend_backend = lambda backend=backend: backend
            store.shared_spend_root = lambda store=store: store
            # Its own instance id, as each gateway process has.
            ledger = SharedSpendLedger(backend, instance_id=f"gw-{name}")
            shared_ledger._LEDGERS[store] = ledger
            app = _app(store, UserConcurrencyLimiter(static_limits_provider(LIMITS)))
            install_exception_handlers(app)
            _routes(app, store)
            made.append(SimpleNamespace(app=app, store=store, ledger=ledger, pool=pool))
        return tuple(made)

    try:
        yield make
    finally:
        for gateway in made:
            if gateway.ledger._writer is not None:
                gateway.ledger._writer.cancel()
                with contextlib.suppress(BaseException):
                    await gateway.ledger._writer
            if gateway.ledger._retry is not None:
                gateway.ledger._retry.cancel()
        try:
            if made:
                async with asyncio.timeout(30):
                    await _base._wipe(made[0].pool)
        finally:
            for gateway in made:
                await _base.close_pool(gateway.pool)


def _routes(app, store) -> None:
    @app.post("/ok", dependencies=[Depends(enforce_user_concurrency)])
    async def ok() -> dict[str, bool]:
        return {"ok": True}

    @app.post("/upstream/{status}", dependencies=[Depends(enforce_user_concurrency)])
    async def upstream_error(status: int) -> None:
        raise HTTPException(status, "the upstream answered with an error")

    @app.post("/spend/{cents}", dependencies=[Depends(enforce_user_concurrency)])
    async def spend(cents: int) -> dict[str, bool]:
        # What a handler's billing point does: record against the store's ledger.
        ledger_for(store).record(USER, cents / 100, api_key_id=KEY_ID)
        return {"ok": True}


async def _answers(*gateways: SimpleNamespace, path: str = "/ok") -> list[int]:
    """What each gateway answers the caller's next request, each settled before the next."""
    statuses = []
    for gateway in gateways:
        async with _client(gateway.app) as client:
            response = await client.post(path)
        statuses.append(response.status_code)
        if response.status_code == 429:
            assert response.json()["detail"]["code"] == "budget_exceeded"
        assert await gateway.ledger.flush(5)
    return statuses


async def _set_counter(pool, day: str, cost: str) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO user_daily_cost (user_id, day, cost_usd, requests) VALUES ($1, $2, $3, 1) "
            "ON CONFLICT (user_id, day) DO UPDATE SET cost_usd = EXCLUDED.cost_usd",
            USER,
            day,
            Decimal(cost),
        )


async def test_requests_the_upstream_fails_leave_both_gateways_admitting_the_caller(gateways):
    # Room for three $0.01 estimates. Ten requests in a row that the upstream
    # fails: each is admitted (the upstream's 502, never the quota's 429),
    # nothing is charged, and nothing is left for either gateway to count.
    a, b = await gateways(quota_usd=0.035)
    for status in (500, 502, 504, 502, 502, 500, 504, 502, 502, 502):
        assert await _answers(a, path=f"/upstream/{status}") == [status]
    assert await _base._rows(a.pool, USER) == []
    assert await _base._account(a.pool, USER) == 0
    assert a.ledger.fully_settled()
    assert await _answers(a, b) == [200, 200]
    assert await _answers(b, a) == [200, 200]


async def test_what_a_request_was_charged_is_what_both_gateways_then_count(gateways):
    # $0.055 of quota. A request that cost $0.03 through one gateway leaves
    # $0.025: two more $0.01 requests fit through the other, the third does
    # not, through either.
    a, b = await gateways(quota_usd=0.055)
    assert await _answers(a, path="/spend/3") == [200]
    assert await _base._account(a.pool, USER) == Decimal("0.03")
    assert await _answers(b, path="/spend/1") == [200]
    assert await _answers(b, path="/spend/1") == [200]
    assert await _answers(a, b) == [429, 429]


async def test_a_quarantined_liability_is_refused_alike_on_its_day_and_admitted_alike_after(
    gateways, clock
):
    """The gate of the gateway that holds a liability and the other gateway agree, every day.

    A request through gateway ``a`` costs $0.50 and the counters refuse the
    cost: its reservation is quarantined, counted against the day it ended on.
    The account has spent $0.55 of $1.00 on each of the two days.

    That day both gateways refuse the caller: ``a`` at its gate (it knows the
    liability), ``b`` at the admission (the database holds it). The next day
    both admit: the liability is the day before's. Counted against "today" in
    ``a``'s memory, ``a`` alone kept refusing until it restarted.
    """
    day_one, day_two = "2031-03-15", "2031-03-16"
    a, b = await gateways(quota_usd=1.0, spent_usd=0.55)
    for day in (day_one, day_two):
        await _set_counter(a.pool, day, "0.55")

    clock.day = day_one
    real, a.ledger._backend = a.ledger._backend, _base.RefusesCosts(a.ledger._backend)
    try:
        assert await _answers(a, path="/spend/50") == [200]
    finally:
        a.ledger._backend = real
    rows = await _base._rows(a.pool, USER)
    assert [(r["state"], r["liability_day"], r["cost_usd"]) for r in rows] == [
        ("quarantined", day_one, Decimal("0.5"))
    ]
    assert a.ledger.holdings()["quarantined"] == 1

    assert await _answers(a, b) == [429, 429]

    clock.day = day_two
    assert await _answers(a, b) == [200, 200]
    assert await _answers(b, a) == [200, 200]
    # Not forgotten: still held in the database and reported by its holder.
    assert len(await _base._rows(a.pool, USER)) == 1
    assert a.ledger.holdings()["quarantined"] == 1
