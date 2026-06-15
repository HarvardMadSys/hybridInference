"""Tests for the enforce_user_concurrency FastAPI dependency.

We mount a tiny ad-hoc FastAPI app and override verify_api_key /
get_user_concurrency_limiter so we can exercise the dependency in
isolation, including streaming-hold / disconnect / exception cleanup.
"""

from __future__ import annotations

import asyncio
from typing import Any, ClassVar

import pytest
from fastapi import Depends, FastAPI
from fastapi.responses import StreamingResponse
from httpx import ASGITransport, AsyncClient

from serving.servers.auth import verify_api_key
from serving.servers.concurrency import (
    UserConcurrencyLimiter,
    enforce_user_concurrency,
    static_limits_provider,
)
from serving.servers.deps import (
    get_model_concurrency_resolver,
    get_router,
    get_user_concurrency_limiter,
)

LIMITS = {"free": 1, "pro": 3, "internal": 10, "admin": 10}


def _make_app(user: dict[str, Any], limiter: UserConcurrencyLimiter | None) -> FastAPI:
    app = FastAPI()

    async def fake_verify_api_key() -> dict[str, Any]:
        return user

    def fake_get_limiter() -> UserConcurrencyLimiter:
        return limiter

    app.dependency_overrides[verify_api_key] = fake_verify_api_key
    app.dependency_overrides[get_user_concurrency_limiter] = fake_get_limiter
    # These tests exercise the per-user gate in isolation, so disable the
    # model-exemption short-circuit by returning no router / resolver.
    app.dependency_overrides[get_router] = lambda: None
    app.dependency_overrides[get_model_concurrency_resolver] = lambda: None

    # Unary endpoint
    app.unary_event = asyncio.Event()  # type: ignore[attr-defined]

    @app.get("/probe", dependencies=[Depends(enforce_user_concurrency)])
    async def probe():
        await app.unary_event.wait()  # holds the slot until the test releases
        return {"ok": True}

    # Streaming endpoint
    app.stream_event = asyncio.Event()  # type: ignore[attr-defined]

    async def stream_body():
        yield b"chunk1\n"
        await app.stream_event.wait()
        yield b"chunk2\n"

    @app.get("/probe_stream", dependencies=[Depends(enforce_user_concurrency)])
    async def probe_stream():
        return StreamingResponse(stream_body(), media_type="text/plain")

    # Endpoint that always raises
    @app.get("/probe_raise", dependencies=[Depends(enforce_user_concurrency)])
    async def probe_raise():
        raise RuntimeError("boom")

    return app


async def _wait_until_in_use(
    limiter: UserConcurrencyLimiter, user_id: str, n: int, timeout: float = 5.0
) -> None:
    """Poll the event loop until *user_id* holds at least *n* slots.

    The held-slot handlers block on an ``asyncio.Event``, so an over-limit
    request sent before the saturating requests have acquired their slots would
    itself grab a free slot and then wait on that Event forever, deadlocking the
    test until pytest-timeout kills the whole shard. A fixed ``asyncio.sleep``
    raced this on loaded CI hosts; polling the live ``in_use`` count is
    deterministic and fails fast if saturation never happens.

    Poll with a short *real* ``asyncio.sleep`` against a wall-clock deadline
    rather than a fixed number of bare ``await asyncio.sleep(0)`` yields. A bare
    yield drains only the currently-ready callbacks; the streaming path
    (``httpx.ASGITransport`` + ``StreamingResponse``) schedules work through
    several anyio hops that can need real loop time to reach slot acquisition,
    so a fixed yield budget raced on loaded CI hosts. Sleeping a few ms between
    checks lets every pending callback drain each iteration and gives competing
    tasks real time to make progress.
    """
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while True:
        slot = limiter._slots.get(user_id)
        if slot is not None and slot.in_use >= n:
            return
        if loop.time() >= deadline:
            break
        await asyncio.sleep(0.005)
    in_use = getattr(limiter._slots.get(user_id), "in_use", 0)
    raise AssertionError(f"{user_id}: only {in_use}/{n} slots acquired")


@pytest.mark.asyncio
async def test_grant_then_reject_for_free_user():
    user = {"user_id": "u1", "role": "free", "is_admin": False}
    limiter = UserConcurrencyLimiter(static_limits_provider(LIMITS))
    app = _make_app(user, limiter)

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Start request 1, hold it open
        task1 = asyncio.create_task(client.get("/probe"))
        # Wait until it actually holds the slot before sending the over-limit
        # request (a fixed sleep races on a loaded host and would deadlock).
        await _wait_until_in_use(limiter, "u1", 1)
        # Request 2 must be rejected with 429
        resp2 = await client.get("/probe")
        assert resp2.status_code == 429
        body = resp2.json()
        # Note: response shape is {"detail": {"error": ...}} in this ad-hoc app
        # because we don't include the production error middleware that unwraps
        # the inner "error" key. See test/servers/test_concurrency_endpoint.py
        # for the production shape ({"error": ...}).
        assert body["detail"]["error"]["code"] == "concurrency_limit_exceeded"
        assert body["detail"]["error"]["limit"] == 1
        assert body["detail"]["error"]["role"] == "free"
        assert resp2.headers.get("Retry-After") == "1"
        # Release request 1
        app.unary_event.set()  # type: ignore[attr-defined]
        resp1 = await task1
        assert resp1.status_code == 200


@pytest.mark.asyncio
async def test_release_after_handler_returns_unblocks_next_request():
    user = {"user_id": "u1", "role": "free", "is_admin": False}
    limiter = UserConcurrencyLimiter(static_limits_provider(LIMITS))
    app = _make_app(user, limiter)
    app.unary_event.set()  # type: ignore[attr-defined]  # do not block handler

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Two sequential requests — both should succeed
        r1 = await client.get("/probe")
        r2 = await client.get("/probe")
        assert r1.status_code == 200
        assert r2.status_code == 200


@pytest.mark.asyncio
async def test_streaming_response_holds_slot_until_drained():
    """While a stream is mid-body, a second request must be rejected.

    Note: ``httpx.ASGITransport`` buffers the entire response body before
    returning the response object, so ``client.stream(...)`` cannot be used
    here — it would deadlock (the stream generator is paused on
    ``stream_event.wait()`` and would never unblock before we enter the
    context manager body).  Instead we kick off the streaming request as a
    task (plain ``client.get``); the ASGI app body generator pauses on the
    event *inside* the transport, which is sufficient to hold the slot while
    we fire a competing request.
    """
    user = {"user_id": "u1", "role": "free", "is_admin": False}
    limiter = UserConcurrencyLimiter(static_limits_provider(LIMITS))
    app = _make_app(user, limiter)

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Start streaming request — the body generator will pause on stream_event
        stream_task = asyncio.create_task(client.get("/probe_stream"))
        # Wait until the streaming request holds the slot before competing.
        await _wait_until_in_use(limiter, "u1", 1)

        # Slot should still be held — second request gets 429
        resp2 = await client.get("/probe")
        assert resp2.status_code == 429

        # Let the stream finish draining
        app.stream_event.set()  # type: ignore[attr-defined]
        stream_resp = await stream_task
        assert stream_resp.status_code == 200

        # After stream is fully drained, slot must be released — sequential
        # request should succeed.
        app.unary_event.set()  # type: ignore[attr-defined]
        resp3 = await client.get("/probe")
        assert resp3.status_code == 200


@pytest.mark.asyncio
async def test_handler_exception_releases_slot():
    user = {"user_id": "u1", "role": "free", "is_admin": False}
    limiter = UserConcurrencyLimiter(static_limits_provider(LIMITS))
    app = _make_app(user, limiter)
    app.unary_event.set()  # type: ignore[attr-defined]

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # First request raises -> 500 (raise_app_exceptions=False)
        r1 = await client.get("/probe_raise")
        assert r1.status_code == 500
        # Second request must succeed — slot was released in finally
        r2 = await client.get("/probe")
        assert r2.status_code == 200


@pytest.mark.asyncio
async def test_two_users_have_independent_budgets_via_dependency():
    """Two separate users each get their own slot."""
    limiter = UserConcurrencyLimiter(static_limits_provider(LIMITS))

    # Build two apps, one per user, but sharing the same limiter
    app_a = _make_app({"user_id": "user-A", "role": "free", "is_admin": False}, limiter)
    app_b = _make_app({"user_id": "user-B", "role": "free", "is_admin": False}, limiter)

    transport_a = ASGITransport(app=app_a, raise_app_exceptions=False)
    transport_b = ASGITransport(app=app_b, raise_app_exceptions=False)

    async with (
        AsyncClient(transport=transport_a, base_url="http://test-a") as client_a,
        AsyncClient(transport=transport_b, base_url="http://test-b") as client_b,
    ):
        task_a = asyncio.create_task(client_a.get("/probe"))
        await _wait_until_in_use(limiter, "user-A", 1)
        # User B must succeed even while user A is holding
        app_b.unary_event.set()  # type: ignore[attr-defined]
        r_b = await client_b.get("/probe")
        assert r_b.status_code == 200
        # Release user A
        app_a.unary_event.set()  # type: ignore[attr-defined]
        r_a = await task_a
        assert r_a.status_code == 200


@pytest.mark.asyncio
async def test_pro_user_three_slots_via_dependency():
    user = {"user_id": "pro-1", "role": "pro", "is_admin": False}
    limiter = UserConcurrencyLimiter(static_limits_provider(LIMITS))
    app = _make_app(user, limiter)

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Hold three concurrent requests
        tasks = [asyncio.create_task(client.get("/probe")) for _ in range(3)]
        await _wait_until_in_use(limiter, "pro-1", 3)
        # Fourth gets 429
        resp4 = await client.get("/probe")
        assert resp4.status_code == 429
        assert resp4.json()["detail"]["error"]["limit"] == 3
        # Release
        app.unary_event.set()  # type: ignore[attr-defined]
        for t in tasks:
            r = await t
            assert r.status_code == 200


@pytest.mark.asyncio
async def test_admin_flag_yields_admin_role_in_response_body():
    user = {"user_id": "adm-1", "role": "free", "is_admin": True}
    limiter = UserConcurrencyLimiter(static_limits_provider(LIMITS))
    app = _make_app(user, limiter)

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Saturate 10 admin slots
        tasks = [asyncio.create_task(client.get("/probe")) for _ in range(10)]
        await _wait_until_in_use(limiter, "adm-1", 10)
        resp11 = await client.get("/probe")
        assert resp11.status_code == 429
        body = resp11.json()
        assert body["detail"]["error"]["limit"] == 10
        assert body["detail"]["error"]["role"] == "admin"
        app.unary_event.set()  # type: ignore[attr-defined]
        for t in tasks:
            r = await t
            assert r.status_code == 200


@pytest.mark.asyncio
async def test_fail_open_when_limiter_is_none():
    """If the limiter isn't configured, the dependency must yield without
    blocking — never break requests when the gate itself is missing."""
    user = {"user_id": "u1", "role": "free", "is_admin": False}
    app = _make_app(user, None)
    app.unary_event.set()  # type: ignore[attr-defined]  # do not block handler

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Many sequential requests must succeed even though there's no limiter
        for _ in range(5):
            r = await client.get("/probe")
            assert r.status_code == 200


def _make_exempt_app(
    user: dict[str, Any],
    limiter: UserConcurrencyLimiter | None,
    exempt_models: set[str],
) -> FastAPI:
    """Build an app whose POST endpoint reads ``model`` from the JSON body.

    Wires a stub router (canonical id == model) and a stub concurrency
    resolver that reports the given models as exempt.
    """
    app = FastAPI()

    async def fake_verify_api_key() -> dict[str, Any]:
        return user

    def fake_get_limiter() -> UserConcurrencyLimiter | None:
        return limiter

    class _StubConfig:
        def __init__(self, model_id: str) -> None:
            self.id = model_id

    class _StubAdapter:
        def __init__(self, model_id: str) -> None:
            self.config = _StubConfig(model_id)

    class _StubRoute:
        def __init__(self, model_id: str) -> None:
            self.adapters = [(_StubAdapter(model_id), None)]

    class _AllRoutes:
        """Pretend every model id is a registered canonical route.

        The gate only checks exemptions for models the router recognizes, so
        the stub returns a route (with ``config.id == model``) for any lookup.
        """

        def get(self, model_id: str, default: Any = None) -> Any:
            return _StubRoute(model_id)

    class _StubRouter:
        routes: ClassVar[Any] = _AllRoutes()

    class _StubResolver:
        async def is_exempt(self, model_id: str) -> bool:
            return model_id in exempt_models

    app.dependency_overrides[verify_api_key] = fake_verify_api_key
    app.dependency_overrides[get_user_concurrency_limiter] = fake_get_limiter
    app.dependency_overrides[get_router] = lambda: _StubRouter()
    app.dependency_overrides[get_model_concurrency_resolver] = lambda: _StubResolver()

    app.unary_event = asyncio.Event()  # type: ignore[attr-defined]

    # No body param on the handler: the gate dependency reads the JSON body
    # itself, and re-reading it for the handler is exercised separately. A
    # parameterless handler matches the pattern used by the other tests here
    # and avoids FastAPI re-parsing a body stream the gate already consumed.
    @app.post("/probe", dependencies=[Depends(enforce_user_concurrency)])
    async def probe():
        await app.unary_event.wait()
        return {"ok": True}

    return app


@pytest.mark.asyncio
async def test_exempt_model_bypasses_full_user_slot():
    """An exempt model must never be rejected even when the user's slot is full."""
    user = {"user_id": "u1", "role": "free", "is_admin": False}
    limiter = UserConcurrencyLimiter(static_limits_provider(LIMITS))
    app = _make_exempt_app(user, limiter, exempt_models={"exempt-model"})
    app.unary_event.set()  # type: ignore[attr-defined]  # don't block handler

    # Saturate the user's single free slot up-front.
    granted, _, _ = await limiter.try_acquire("u1", "free", False)
    assert granted

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Non-exempt model is rejected because the slot is full.
        resp_blocked = await client.post("/probe", json={"model": "normal-model"})
        assert resp_blocked.status_code == 429

        # Exempt model bypasses the gate entirely -> 200 despite the full slot.
        resp_exempt = await client.post("/probe", json={"model": "exempt-model"})
        assert resp_exempt.status_code == 200
        assert resp_exempt.json()["ok"] is True


@pytest.mark.asyncio
async def test_exempt_model_consumes_no_slot():
    """Many concurrent exempt requests succeed without consuming per-user slots."""
    user = {"user_id": "u1", "role": "free", "is_admin": False}
    limiter = UserConcurrencyLimiter(static_limits_provider(LIMITS))
    app = _make_exempt_app(user, limiter, exempt_models={"exempt-model"})
    app.unary_event.set()  # type: ignore[attr-defined]

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # free limit is 1, but exempt model should allow many sequential 200s.
        for _ in range(5):
            r = await client.post("/probe", json={"model": "exempt-model"})
            assert r.status_code == 200

    # The exempt path must not have touched the user's slot.
    assert limiter.role_for("u1") is None


@pytest.mark.asyncio
async def test_concurrency_429_calls_log_rejection(monkeypatch):
    """When a request is rejected with 429, log_rejection is fired off."""
    from unittest.mock import AsyncMock

    log_calls: list[dict] = []

    async def fake_log_rejection(**kwargs):
        log_calls.append(kwargs)

    monkeypatch.setattr(
        "serving.servers.concurrency.log_rejection",
        fake_log_rejection,
    )

    user = {"user_id": "u1", "role": "free", "is_admin": False}
    limiter = UserConcurrencyLimiter(static_limits_provider(LIMITS))
    app = _make_app(user, limiter)

    # Stash fake services on app.state so the wired call can find them.
    app.state.services = type("S", (), {})()
    app.state.services.log_store = AsyncMock()
    app.state.services.runtime_settings = AsyncMock()

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Saturate the user's slot first.
        granted, _, _ = await limiter.try_acquire(user["user_id"], "free", False)
        assert granted

        resp = await client.get("/probe")
        assert resp.status_code == 429
        # Yield to the event loop so the fire-and-forget create_task runs.
        await asyncio.sleep(0)

    # Helper should have been invoked exactly once with concurrency error code.
    assert len(log_calls) == 1
    call = log_calls[0]
    assert call["status_code"] == 429
    assert call["error_code"] == "concurrency_limit_exceeded"
    assert call["user"]["user_id"] == "u1"
