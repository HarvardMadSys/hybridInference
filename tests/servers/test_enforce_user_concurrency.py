"""Tests for the enforce_user_concurrency FastAPI dependency.

We mount a tiny ad-hoc FastAPI app and override verify_api_key /
get_user_concurrency_limiter so we can exercise the dependency in
isolation, including streaming-hold / disconnect / exception cleanup.
"""

from __future__ import annotations

import asyncio
from typing import Any

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
from serving.servers.deps import get_user_concurrency_limiter

LIMITS = {"free": 1, "pro": 3, "internal": 10, "admin": 10}


def _make_app(user: dict[str, Any], limiter: UserConcurrencyLimiter | None) -> FastAPI:
    app = FastAPI()

    async def fake_verify_api_key() -> dict[str, Any]:
        return user

    def fake_get_limiter() -> UserConcurrencyLimiter:
        return limiter

    app.dependency_overrides[verify_api_key] = fake_verify_api_key
    app.dependency_overrides[get_user_concurrency_limiter] = fake_get_limiter

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


@pytest.mark.asyncio
async def test_grant_then_reject_for_free_user():
    user = {"user_id": "u1", "role": "free", "is_admin": False}
    limiter = UserConcurrencyLimiter(static_limits_provider(LIMITS))
    app = _make_app(user, limiter)

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Start request 1, hold it open
        task1 = asyncio.create_task(client.get("/probe"))
        # Give it a moment to enter the dependency
        await asyncio.sleep(0.1)
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
        # Give the task time to enter the dependency and start streaming
        await asyncio.sleep(0.1)

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
        await asyncio.sleep(0.1)
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
        await asyncio.sleep(0.1)
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
        await asyncio.sleep(0.1)
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
