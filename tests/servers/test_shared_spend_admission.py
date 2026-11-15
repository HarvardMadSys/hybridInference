"""The API admission in the shared accounting mode, on an in-memory shared backend.

The same app as ``test_spend_reservation.py``: the gate, then
``enforce_user_concurrency``. In the shared mode the reservation is made through
the shared ledger, published to the request, and the costs the handler records
are settled with it; the concurrency slot is given back when the request ends,
while the money waits for its settlement. A request is charged what its
billing point recorded and nothing else, however it ended: one the upstream
failed, or that was cancelled, gives its whole reservation back. A database
that cannot admit answers the request path's 503; requests already admitted
keep running.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

import asyncpg
import pytest
from fastapi import Depends, HTTPException
from fastapi.responses import StreamingResponse

from serving.adapters.dispatch_watch import report_sent
from serving.batch.attempts import observe_billing
from serving.servers.concurrency import (
    UserConcurrencyLimiter,
    enforce_user_concurrency,
    static_limits_provider,
)
from serving.servers.middleware.exception_handler import install_exception_handlers
from serving.shared_spend import ledger as shared_ledger, mode as spend_mode
from serving.spend_ledger import ledger_for
from serving.utils import context as req_ctx
from tests.fixtures.shared_spend import FakeSharedBackend
from tests.servers.test_spend_reservation import (
    KEY_ID,
    LIMITS,
    USER,
    _app,
    _client,
    _store,
    _until,
)


@pytest.fixture(autouse=True)
def _shared(monkeypatch):
    monkeypatch.setenv("USER_AUTH_ENABLED", "1")
    monkeypatch.setenv("API_KEY_SECRET", "test-secret")
    monkeypatch.setattr(spend_mode, "_mode", "shared")


def _shared_app(quota_usd: float = 1.0):
    store = _store(quota_usd=quota_usd)
    backend = FakeSharedBackend()
    store.shared_spend_backend = lambda: backend
    store.shared_spend_root = lambda: store
    limiter = UserConcurrencyLimiter(static_limits_provider(LIMITS))
    app = _app(store, limiter)
    # The application's handlers: DatabaseBusyError is its 503 + Retry-After.
    install_exception_handlers(app)

    @app.post("/cost", dependencies=[Depends(enforce_user_concurrency)])
    async def cost() -> dict[str, bool]:
        # What a handler's billing point does: record against the store's ledger.
        assert req_ctx.get().get(req_ctx.SPEND_RESERVATION) is not None
        ledger_for(store).record(USER, 0.25, api_key_id=KEY_ID)
        ledger_for(store).record(USER, 0.05, api_key_id=KEY_ID)
        await app.state.gate.wait()
        return {"ok": True}

    @app.post("/reject", dependencies=[Depends(enforce_user_concurrency)])
    async def reject() -> None:
        raise HTTPException(400, "invalid request, refused before any upstream call")

    @app.post("/free", dependencies=[Depends(enforce_user_concurrency)])
    async def free() -> dict[str, bool]:
        report_sent()
        observe_billing(cost_usd=0.0)  # a priced, free model
        return {"ok": True}

    @app.post("/unpriced", dependencies=[Depends(enforce_user_concurrency)])
    async def unpriced() -> dict[str, bool]:
        report_sent()
        observe_billing(cost_usd=None)  # what CostTracker reports without pricing
        return {"ok": True}

    @app.post("/sent-unbilled", dependencies=[Depends(enforce_user_concurrency)])
    async def sent_unbilled() -> dict[str, bool]:
        report_sent()
        return {"ok": True}

    @app.post("/upstream/{status}", dependencies=[Depends(enforce_user_concurrency)])
    async def upstream_error(status: int) -> None:
        report_sent()
        raise HTTPException(status, "upstream answered with an error")

    @app.post("/sent-wait", dependencies=[Depends(enforce_user_concurrency)])
    async def sent_wait() -> dict[str, bool]:
        report_sent()
        await app.state.gate.wait()
        return {"ok": True}

    async def billed_body():
        report_sent()
        try:
            yield b"one\n"
            await app.state.gate.wait()
            yield b"two\n"
        finally:
            # The tail: a stream bills what it used as it ends, cut off or not.
            ledger_for(store).record(USER, 0.07, api_key_id=KEY_ID)
            observe_billing(cost_usd=0.07)

    @app.post("/billed-stream", dependencies=[Depends(enforce_user_concurrency)])
    async def billed_stream() -> StreamingResponse:
        return StreamingResponse(billed_body(), media_type="text/plain")

    return app, store, backend, limiter


async def _kinds(store, backend) -> list[str]:
    assert await ledger_for(store).flush(5)
    return [m.kind for b in backend.batches for m in b.members]


@pytest.mark.asyncio
async def test_costs_recorded_by_the_request_are_settled_with_its_reservation():
    app, store, backend, limiter = _shared_app()
    ledger = ledger_for(store)
    assert isinstance(ledger, shared_ledger.SharedSpendLedger)
    backend.settle_gate = asyncio.Event()
    app.state.gate.set()
    async with _client(app) as client:
        response = await client.post("/cost")
    assert response.status_code == 200
    # The slot is back at once; the money waits for its settlement.
    assert limiter._slots[USER].in_use == 0
    assert ledger.holdings()["queued"] == 1
    backend.settle_gate.set()
    assert await ledger.flush(5)
    (batch,) = backend.batches
    assert batch.increments == ()
    (member,) = batch.members
    assert [line.cost_usd for line in member.costs] == [Decimal("0.25"), Decimal("0.05")]
    assert backend.counters[USER] == Decimal("0.30")
    assert backend.rows == {}
    assert ledger.fully_settled()


@pytest.mark.asyncio
async def test_a_database_that_cannot_admit_answers_503_and_spares_requests_already_running():
    app, store, backend, _ = _shared_app()
    async with _client(app) as client:
        running = asyncio.create_task(client.post("/stream"))
        await _until(lambda: len(backend.rows) == 1)

        backend.fail_admit = asyncpg.exceptions.AdminShutdownError("terminating")
        refused = await client.post("/hold")
        assert refused.status_code == 503
        assert refused.headers["Retry-After"] == "1"

        backend.fail_settle = OSError("database down")
        app.state.gate.set()
        response = await running
        assert response.status_code == 200 and response.text == "one\ntwo\n"
    ledger = ledger_for(store)
    # Not written, not forgotten: the reservation stays queued for its batch.
    await _until(lambda: ledger._writer is None)
    assert ledger.holdings()["queued"] == 1
    assert len(backend.rows) == 1
    backend.fail_settle = None
    assert await ledger.flush(5)
    assert backend.rows == {}


@pytest.mark.asyncio
async def test_an_admission_that_cannot_finish_in_time_answers_503_within_its_bound(monkeypatch):
    monkeypatch.setattr(shared_ledger, "ADMISSION_BUDGET_SECONDS", 0.2)
    app, _, backend, _ = _shared_app()
    backend.fail_admit = ConnectionRefusedError("no database")  # retried until the bound
    loop = asyncio.get_running_loop()
    async with _client(app) as client:
        started = loop.time()
        refused = await client.post("/hold")
        elapsed = loop.time() - started
    assert refused.status_code == 503
    assert elapsed < 2.0


@pytest.mark.asyncio
async def test_the_shared_admission_applies_the_same_partial_headroom_policy():
    app, store, backend, _ = _shared_app(quota_usd=0.035)
    async with _client(app) as client:
        held = [asyncio.create_task(client.post("/hold")) for _ in range(3)]
        await _until(lambda: len(backend.rows) == 3)
        refused = await client.post("/hold")
        assert refused.status_code == 429
        assert refused.json()["detail"]["code"] == "budget_exceeded"
        app.state.gate.set()
        assert [r.status_code for r in await asyncio.gather(*held)] == [200, 200, 200]
    assert await ledger_for(store).flush(5)
    assert backend.rows == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "status"),
    [
        # Refused before any upstream call.
        ("/reject", 400),
        # Priced, and free: a known zero.
        ("/free", 200),
        # Sent, and the billing point could not price it: it records nothing,
        # as in the per-process mode.
        ("/unpriced", 200),
        # Sent, ended normally, and nothing billed it.
        ("/sent-unbilled", 200),
        # Sent, and the upstream answered with an error, whichever it was.
        ("/upstream/400", 400),
        ("/upstream/429", 429),
        ("/upstream/500", 500),
        ("/upstream/502", 502),
        ("/upstream/503", 503),
        ("/upstream/504", 504),
    ],
)
async def test_a_request_nothing_was_charged_for_gives_its_whole_reservation_back(path, status):
    app, store, backend, _ = _shared_app()
    async with _client(app) as client:
        response = await client.post(path)
    assert response.status_code == status
    assert await _kinds(store, backend) == ["settle"]
    # Nothing is held afterwards, here or in the database, and an estimate is
    # never charged: no cost was recorded, none is added.
    assert backend.rows == {}
    assert ledger_for(store).fully_settled()
    assert backend.counters.get(USER, Decimal(0)) == 0


@pytest.mark.asyncio
async def test_requests_the_upstream_fails_do_not_use_up_the_quota():
    # Room for three $0.01 estimates. Ten requests the upstream fails, one after
    # another, are each admitted (they answer the upstream's 502, never the
    # quota's 429), and so is the next request: a failure its billing point
    # charged nothing for holds nothing once it has ended.
    app, store, backend, _ = _shared_app(quota_usd=0.035)
    async with _client(app) as client:
        for _ in range(10):
            failed = await client.post("/upstream/502")
            assert failed.status_code == 502
            assert await ledger_for(store).flush(5)
        assert (await client.post("/free")).status_code == 200
    assert await ledger_for(store).flush(5)
    assert backend.rows == {}
    assert backend.counters.get(USER, Decimal(0)) == 0


@pytest.mark.asyncio
async def test_a_request_cancelled_after_it_was_sent_and_never_billed_is_given_back():
    app, store, backend, _ = _shared_app()
    async with _client(app) as client:
        call = asyncio.create_task(client.post("/sent-wait"))
        await _until(lambda: bool(ledger_for(store)._active))
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call
        await _until(lambda: not ledger_for(store)._active)
    assert await _kinds(store, backend) == ["settle"]
    assert backend.rows == {}
    assert ledger_for(store).fully_settled()


@pytest.mark.asyncio
async def test_a_cut_off_stream_settles_the_tail_its_billing_point_charged():
    app, store, backend, _ = _shared_app()
    async with _client(app) as client:
        streaming = asyncio.create_task(client.post("/billed-stream"))
        await _until(lambda: bool(ledger_for(store)._active))
        streaming.cancel()
        with pytest.raises(asyncio.CancelledError):
            await streaming
        await _until(lambda: not ledger_for(store)._active)
    assert await _kinds(store, backend) == ["settle"]
    (member,) = backend.batches[0].members
    assert [line.cost_usd for line in member.costs] == [Decimal("0.07")]
    assert backend.counters[USER] == Decimal("0.07")
