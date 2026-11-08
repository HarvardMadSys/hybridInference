"""Requests admitted together cannot overshoot a quota, and every exit gives back its share.

The quota gate read spend from the counters without counting requests still
running, so a caller's concurrent requests could overshoot a limit by up to
the concurrency limit times what one request costs. Admission now reserves
each request's estimated cost against the account's daily and monthly quota
and the key's budgets, in one step with the check, and the reservation is
released when the request ends -- success, failure, stream end, disconnect.
"""

from __future__ import annotations

import asyncio
import types
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from httpx import ASGITransport, AsyncClient
from starlette.requests import Request

from serving.servers.concurrency import (
    UserConcurrencyLimiter,
    enforce_user_concurrency,
    static_limits_provider,
)
from serving.servers.deps import (
    get_embedding_adapters,
    get_log_store,
    get_model_concurrency_resolver,
    get_operational_store,
    get_router,
    get_user_concurrency_limiter,
)
from serving.spend_ledger import SPEND_CHECK_KEY, SpendCheck, ledger_for
from serving.storage.base import OperationalStore

USER = "user-r"
KEY_ID = 31
LIMITS = {"free": 50, "pro": 50, "internal": 50, "admin": 50}


@pytest.fixture(autouse=True)
def _auth_on(monkeypatch):
    monkeypatch.setenv("USER_AUTH_ENABLED", "1")
    monkeypatch.setenv("API_KEY_SECRET", "test-secret")


def _store(*, quota_usd: float, spent_usd: float = 0.0, **row: Any) -> MagicMock:
    store = MagicMock(spec=OperationalStore)
    store.get_auth_context_with_spend = AsyncMock(
        return_value={
            "id": KEY_ID,
            "user_id": USER,
            "quota_daily_cost_usd": quota_usd,
            "role": "free",
            "email": "r@example.com",
            "email_verified": True,
            "cost_usd": spent_usd,
            **row,
        }
    )
    store.update_key_last_used = AsyncMock()
    store.increment_user_cost = AsyncMock()
    return store


def _app(store: MagicMock, limiter: UserConcurrencyLimiter) -> FastAPI:
    app = FastAPI()
    app.state.gate = asyncio.Event()
    app.dependency_overrides[get_operational_store] = lambda: store
    app.dependency_overrides[get_log_store] = lambda: None
    app.dependency_overrides[get_user_concurrency_limiter] = lambda: limiter
    app.dependency_overrides[get_router] = lambda: None
    app.dependency_overrides[get_model_concurrency_resolver] = lambda: None
    app.dependency_overrides[get_embedding_adapters] = lambda: {}

    @app.post("/hold", dependencies=[Depends(enforce_user_concurrency)])
    async def hold() -> dict[str, bool]:
        await app.state.gate.wait()
        return {"ok": True}

    async def body():
        yield b"one\n"
        await app.state.gate.wait()
        yield b"two\n"

    @app.post("/stream", dependencies=[Depends(enforce_user_concurrency)])
    async def stream() -> StreamingResponse:
        return StreamingResponse(body(), media_type="text/plain")

    @app.post("/raise", dependencies=[Depends(enforce_user_concurrency)])
    async def boom() -> None:
        raise RuntimeError("boom")

    return app


def _client(app: FastAPI) -> AsyncClient:
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    return AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": "Bearer hyi-reserve"},
    )


def _in_flight(store: MagicMock) -> int:
    entry = ledger_for(store)._reserved_users.get(USER)
    return int(entry[1]) if entry else 0


async def _until(predicate, timeout: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.005)


@pytest.mark.asyncio
async def test_requests_in_flight_count_against_the_quota_until_they_end():
    # $0.035 left and a $0.01 estimate: three fit, the fourth does not.
    store = _store(quota_usd=0.035)
    limiter = UserConcurrencyLimiter(static_limits_provider(LIMITS))
    app = _app(store, limiter)

    async with _client(app) as client:
        held = [asyncio.create_task(client.post("/hold")) for _ in range(3)]
        await _until(lambda: _in_flight(store) == 3)

        refused = await client.post("/hold")
        assert refused.status_code == 429
        body = refused.json()["detail"]
        assert (body["code"], body["scope"], body["window"]) == (
            "budget_exceeded",
            "account",
            "day",
        )
        assert body["spent_usd"] == pytest.approx(0.03)

        app.state.gate.set()
        assert [r.status_code for r in await asyncio.gather(*held)] == [200, 200, 200]
        assert _in_flight(store) == 0
        assert (await client.post("/hold")).status_code == 200
    assert _in_flight(store) == 0
    assert limiter._slots[USER].in_use == 0


@pytest.mark.asyncio
async def test_two_requests_that_passed_the_gate_together_are_admitted_one_at_a_time():
    """Both read the counters before either reserved: admission checks again."""
    store = _store(quota_usd=0.015)  # room for one
    both_reading = asyncio.Event()
    calls = 0
    row = await store.get_auth_context_with_spend("x")

    async def read_together(_key_hash):
        nonlocal calls
        calls += 1
        if calls == 2:
            both_reading.set()
        await both_reading.wait()
        return dict(row)

    store.get_auth_context_with_spend = AsyncMock(side_effect=read_together)
    limiter = UserConcurrencyLimiter(static_limits_provider(LIMITS))
    app = _app(store, limiter)

    async with _client(app) as client:
        first = asyncio.create_task(client.post("/hold"))
        second = asyncio.create_task(client.post("/hold"))
        await _until(lambda: _in_flight(store) == 1 and (first.done() or second.done()))
        app.state.gate.set()
        statuses = sorted(r.status_code for r in await asyncio.gather(first, second))

    assert statuses == [200, 429]
    assert _in_flight(store) == 0
    # The refused one gave its concurrency slot back too.
    assert limiter._slots[USER].in_use == 0


@pytest.mark.asyncio
async def test_a_stream_holds_its_reservation_until_its_body_ends():
    store = _store(quota_usd=1.0)
    app = _app(store, UserConcurrencyLimiter(static_limits_provider(LIMITS)))

    async with _client(app) as client:
        streaming = asyncio.create_task(client.post("/stream"))
        await _until(lambda: _in_flight(store) == 1)
        await asyncio.sleep(0.02)
        assert _in_flight(store) == 1  # the body is still running

        app.state.gate.set()
        response = await streaming
        assert response.status_code == 200
        assert response.text == "one\ntwo\n"
    assert _in_flight(store) == 0


@pytest.mark.asyncio
async def test_a_client_that_goes_away_mid_stream_gives_its_reservation_back():
    store = _store(quota_usd=1.0)
    app = _app(store, UserConcurrencyLimiter(static_limits_provider(LIMITS)))

    async with _client(app) as client:
        streaming = asyncio.create_task(client.post("/stream"))
        await _until(lambda: _in_flight(store) == 1)
        streaming.cancel()
        with pytest.raises(asyncio.CancelledError):
            await streaming
        await _until(lambda: _in_flight(store) == 0)


@pytest.mark.asyncio
async def test_a_failing_handler_gives_its_reservation_back():
    store = _store(quota_usd=1.0)
    app = _app(store, UserConcurrencyLimiter(static_limits_provider(LIMITS)))

    async with _client(app) as client:
        response = await client.post("/raise")

    assert response.status_code == 500
    assert _in_flight(store) == 0


@pytest.mark.asyncio
async def test_in_flight_requests_count_against_the_keys_budget():
    store = _store(quota_usd=100.0, budget_daily_usd=0.015, key_spent_today_usd=0.0)
    app = _app(store, UserConcurrencyLimiter(static_limits_provider(LIMITS)))

    async with _client(app) as client:
        held = asyncio.create_task(client.post("/hold"))
        await _until(lambda: _in_flight(store) == 1)
        refused = await client.post("/hold")
        app.state.gate.set()
        assert (await held).status_code == 200

    assert refused.status_code == 429
    assert refused.json()["detail"]["scope"] == "key"
    assert _in_flight(store) == 0


@pytest.mark.asyncio
async def test_without_a_limiter_the_reservation_is_still_taken_and_released():
    store = _store(quota_usd=0.015)
    app = _app(store, None)  # fail-open admission

    async with _client(app) as client:
        held = asyncio.create_task(client.post("/hold"))
        await _until(lambda: _in_flight(store) == 1)
        refused = await client.post("/hold")
        app.state.gate.set()
        assert (await held).status_code == 200

    assert refused.status_code == 429
    assert _in_flight(store) == 0


@pytest.mark.asyncio
async def test_a_refusal_at_admission_releases_the_slot_it_took():
    """Driven directly: the check finds the budget gone between the gate and admission."""
    store = _store(quota_usd=0.015)
    ledger = ledger_for(store)
    check = SpendCheck(
        ledger=ledger,
        user_id=USER,
        key_id=None,
        read_at=0.0,
        account_quota_usd=0.015,
        account_spent_usd=0.0,
    )
    taken, _ = check.admit()  # someone else got there first
    limiter = UserConcurrencyLimiter(static_limits_provider(LIMITS))
    request = MagicMock(spec=Request)
    request.url = types.SimpleNamespace(path="/v1/chat/completions")
    user = {"user_id": USER, "role": "free", "is_admin": False, SPEND_CHECK_KEY: check}

    admission = enforce_user_concurrency(
        request,
        user=user,
        limiter=limiter,
        router=None,
        concurrency_resolver=None,
        embedding_adapters={},
    )
    with pytest.raises(HTTPException) as refused:
        await admission.__anext__()

    assert refused.value.status_code == 429
    assert limiter._slots[USER].in_use == 0
    taken.release()
    assert ledger._reserved_users == {}


@pytest.mark.asyncio
async def test_a_caller_without_a_check_is_admitted_without_reserving():
    """A replaced gate (tests) or auth turned off: nothing to reserve against."""
    limiter = UserConcurrencyLimiter(static_limits_provider(LIMITS))
    request = MagicMock(spec=Request)
    admission = enforce_user_concurrency(
        request,
        user={"user_id": USER, "role": "free", "is_admin": False},
        limiter=limiter,
        router=None,
        concurrency_resolver=None,
        embedding_adapters={},
    )

    await admission.__anext__()
    with pytest.raises(StopAsyncIteration):
        await admission.__anext__()
    assert limiter._slots[USER].in_use == 0


@pytest.mark.asyncio
async def test_a_cost_not_yet_written_to_the_counters_counts_at_the_gate():
    """A slow database delays the counters, never the quota."""
    store = _store(quota_usd=0.5)  # the counters read $0 spent
    landed = asyncio.Event()

    async def slow_write(*_args, **_kwargs):
        await landed.wait()

    store.increment_user_cost = AsyncMock(side_effect=slow_write)
    ledger = ledger_for(store)
    ledger.record(USER, 0.495, api_key_id=KEY_ID)  # the last request's cost, being written
    app = _app(store, UserConcurrencyLimiter(static_limits_provider(LIMITS)))
    app.state.gate.set()

    async with _client(app) as client:
        refused = await client.post("/hold")
        landed.set()
        assert await ledger.flush()

    assert refused.status_code == 429
    assert refused.json()["detail"]["spent_usd"] == pytest.approx(0.495)
    store.increment_user_cost.assert_awaited_once()
