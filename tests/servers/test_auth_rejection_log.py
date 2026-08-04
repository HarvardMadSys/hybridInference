"""Tests verifying verify_api_key fires log_rejection at its rejection sites."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from serving.servers.auth import verify_api_key


def _build_app(monkeypatch, *, op_store_user: dict[str, Any] | None) -> tuple[FastAPI, list[dict]]:
    """Wire up a tiny app whose only endpoint depends on verify_api_key.

    Returns the app plus the list that captures log_rejection invocations.
    """
    log_calls: list[dict] = []

    async def fake_log_rejection(**kwargs):
        log_calls.append(kwargs)

    monkeypatch.setattr(
        "serving.servers.auth.log_rejection",
        fake_log_rejection,
    )

    app = FastAPI()

    async def fake_op_store_dep():
        op = MagicMock()
        op.get_auth_context_by_key_hash = AsyncMock(return_value=op_store_user)
        op.get_user_cost_today = AsyncMock(return_value=0.0)
        op.update_key_last_used = AsyncMock()
        return op

    async def fake_log_store_dep():
        return MagicMock()

    from serving.servers.deps import get_log_store, get_operational_store

    app.dependency_overrides[get_operational_store] = fake_op_store_dep
    app.dependency_overrides[get_log_store] = fake_log_store_dep

    @app.get("/v1/chat/completions")
    async def hit(user: dict = pytest.importorskip("fastapi").Depends(verify_api_key)):
        return {"ok": True}

    # Stub services so the helper can read log_store / runtime_settings.
    app.state.services = type("S", (), {})()
    app.state.services.log_store = MagicMock()
    app.state.services.runtime_settings = MagicMock()
    return app, log_calls


def _recording_queue(sink: list[dict]):
    """A ``queue_rejection_log`` stand-in: sync, records kwargs, returns a coro."""

    def fake_queue_rejection_log(**kwargs):
        sink.append(kwargs)

        async def _noop() -> None:
            return None

        return _noop()

    return fake_queue_rejection_log


def _build_blocked_app(
    monkeypatch,
    *,
    lightweight_user: dict[str, Any] | None,
    logging_on: bool,
) -> tuple[FastAPI, list[dict], MagicMock]:
    """App with a POST inference route, for the ip_blocked rejection path.

    POST (not GET) because the point of these tests is the request *body*:
    ``ip_blocked`` is refused in a dependency, before any handler has parsed it.
    Returns the app, the captured rejection-log calls, and the op_store mock so
    a test can assert whether the identity lookup was attempted at all.

    Patches ``queue_rejection_log`` — the seam the blocked path actually uses,
    since the prompt-retention bound has to be applied before the task exists.
    It is sync and returns the coroutine to schedule, so the fake matches.
    """
    log_calls: list[dict] = []

    monkeypatch.setattr("serving.servers.auth.queue_rejection_log", _recording_queue(log_calls))

    op = MagicMock()
    op.get_auth_context_lightweight = AsyncMock(return_value=lightweight_user)

    async def fake_op_store_dep():
        return op

    async def fake_log_store_dep():
        return MagicMock()

    from serving.servers.deps import get_log_store, get_operational_store

    app = FastAPI()
    app.dependency_overrides[get_operational_store] = fake_op_store_dep
    app.dependency_overrides[get_log_store] = fake_log_store_dep

    from fastapi import Depends

    @app.post("/v1/chat/completions")
    async def hit(user: dict = Depends(verify_api_key)):
        return {"ok": True}

    app.state.services = type("S", (), {})()
    app.state.services.log_store = MagicMock()
    rs = MagicMock()
    rs.get_bool = AsyncMock(return_value=logging_on)
    app.state.services.runtime_settings = rs
    return app, log_calls, op


@pytest.fixture
def blocked_localhost(monkeypatch):
    """Yield a coroutine that trips the auth-failure block for a given IP.

    Threshold 1 so a single recorded failure blocks the ASGI client's peer.
    The blocklist is per-process module state, so it is wiped either side.
    """
    from serving.config.settings import settings
    from serving.utils.auth_failure_blocklist import (
        record_auth_failure,
        reset_auth_failure_block_state,
    )

    reset_auth_failure_block_state()
    monkeypatch.setattr(settings, "auth_failure_block_enabled", True)
    monkeypatch.setattr(settings, "auth_failure_block_threshold", 1)
    monkeypatch.setattr(settings, "auth_failure_block_window_sec", 100)
    monkeypatch.setattr(settings, "auth_failure_block_duration_sec", 1000)
    yield record_auth_failure
    reset_auth_failure_block_state()


@pytest.mark.asyncio
async def test_blocked_ip_rejection_logs_prompt_and_user(monkeypatch, blocked_localhost):
    """An ip_blocked row carries the request prompt and the caller's identity.

    Both are what the admin dashboard renders per row, and both were null on
    this path before: the refusal happens in a dependency, so nothing had read
    the body or resolved the presented key.
    """
    user_row = {"user_id": "u1", "role": "pro", "email": None, "email_verified": True}
    app, log_calls, _op = _build_blocked_app(
        monkeypatch, lightweight_user=user_row, logging_on=True
    )
    await blocked_localhost("127.0.0.1")

    messages = [{"role": "user", "content": "who am I"}]
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer hyi-valid"},
            json={"model": "gpt-4", "messages": messages},
        )
        await asyncio.sleep(0)

    assert resp.status_code == 429
    assert len(log_calls) == 1
    assert log_calls[0]["error_code"] == "ip_blocked"
    assert log_calls[0]["prompt"] == messages
    assert log_calls[0]["user"] == {"user_id": "u1", "role": "pro"}


@pytest.mark.asyncio
async def test_blocked_ip_rejection_without_a_valid_key_has_no_user(monkeypatch, blocked_localhost):
    """A scanner with no resolvable key still logs its prompt, with a null user."""
    app, log_calls, _op = _build_blocked_app(monkeypatch, lightweight_user=None, logging_on=True)
    await blocked_localhost("127.0.0.1")

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/chat/completions",
            json={"model": "gpt-4", "messages": [{"role": "user", "content": "probe"}]},
        )
        await asyncio.sleep(0)

    assert resp.status_code == 429
    assert log_calls[0]["user"] is None
    assert log_calls[0]["prompt"] == [{"role": "user", "content": "probe"}]


@pytest.mark.asyncio
async def test_blocked_ip_treats_a_row_without_a_user_id_as_unresolved(
    monkeypatch, blocked_localhost
):
    """A partial lookup row logs no user rather than a null-id "free" account.

    Passing ``{"user_id": None, "role": "free"}`` through would render in the
    dashboard as an identified free user, which is worse than an honest blank.
    """
    app, log_calls, _op = _build_blocked_app(
        monkeypatch, lightweight_user={"role": "free"}, logging_on=True
    )
    await blocked_localhost("127.0.0.1")

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer hyi-valid"},
            json={"model": "gpt-4", "messages": [{"role": "user", "content": "hi"}]},
        )
        await asyncio.sleep(0)

    assert resp.status_code == 429
    assert log_calls[0]["user"] is None


@pytest.mark.asyncio
async def test_blocked_ip_skips_enrichment_when_rejection_logging_is_off(
    monkeypatch, blocked_localhost
):
    """With the toggle off, the block costs no body read and no identity lookup.

    The shed path stays cheap: enrichment is only worth paying for when the row
    it enriches will actually be written.
    """
    user_row = {"user_id": "u1", "role": "pro"}
    app, log_calls, op = _build_blocked_app(
        monkeypatch, lightweight_user=user_row, logging_on=False
    )
    await blocked_localhost("127.0.0.1")

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer hyi-valid"},
            json={"model": "gpt-4", "messages": [{"role": "user", "content": "hi"}]},
        )
        await asyncio.sleep(0)

    assert resp.status_code == 429
    assert log_calls[0]["prompt"] == ""
    assert log_calls[0]["user"] is None
    op.get_auth_context_lightweight.assert_not_called()


@pytest.mark.asyncio
async def test_blocked_ip_identity_lookup_is_skipped_when_the_budget_is_spent(
    monkeypatch, blocked_localhost
):
    """A spent enrichment budget keeps the block off the database entirely.

    Unsuccessful auth lookups are not cached, so a blocked source spraying fresh
    random tokens would otherwise reach the shared Postgres pool once per
    request — restoring exactly the cost the IP block exists to eliminate.
    """
    import serving.observability.rejection_log as mod

    app, log_calls, op = _build_blocked_app(
        monkeypatch, lightweight_user={"user_id": "u1", "role": "pro"}, logging_on=True
    )
    await blocked_localhost("127.0.0.1")

    exhausted = asyncio.Semaphore(1)
    await exhausted.acquire()
    monkeypatch.setattr(mod, "_enrichment_slots", exhausted)

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer hyi-valid"},
            json={"model": "gpt-4", "messages": [{"role": "user", "content": "hi"}]},
        )
        await asyncio.sleep(0)

    assert resp.status_code == 429
    assert log_calls[0]["user"] is None
    assert log_calls[0]["prompt"] == ""
    op.get_auth_context_lightweight.assert_not_awaited()


@pytest.mark.asyncio
async def test_blocked_ip_on_a_typed_body_route_bounds_an_oversized_prompt(
    monkeypatch, blocked_localhost
):
    """End-to-end over the path that actually pre-parses the body.

    FastAPI parses a declared body model *before* solving dependencies, so on a
    typed route (``/v1/embeddings`` takes ``EmbeddingRequest``) the gate sees a
    body already on ``request._json``. The size cap has to hold there too, or a
    blocked caller — under no quota, auth, or concurrency limit — could write
    arbitrarily large prompts into api_logs.
    """
    from pydantic import BaseModel

    from serving.observability.rejection_log import REJECTED_PROMPT_MAX_BODY_BYTES

    log_calls: list[dict] = []
    monkeypatch.setattr("serving.servers.auth.queue_rejection_log", _recording_queue(log_calls))

    op = MagicMock()
    op.get_auth_context_lightweight = AsyncMock(return_value=None)

    from fastapi import Depends

    from serving.servers.deps import get_log_store, get_operational_store

    class EmbedBody(BaseModel):
        model: str
        input: str

    app = FastAPI()
    app.dependency_overrides[get_operational_store] = lambda: op
    app.dependency_overrides[get_log_store] = lambda: MagicMock()

    @app.post("/v1/embeddings")
    async def embed(body: EmbedBody, _u: dict = Depends(verify_api_key)):
        return {"ok": True}

    app.state.services = type("S", (), {})()
    app.state.services.log_store = MagicMock()
    rs = MagicMock()
    rs.get_bool = AsyncMock(return_value=True)
    app.state.services.runtime_settings = rs

    await blocked_localhost("127.0.0.1")

    # A real oversized payload against the real constant — no patched cap, so
    # this exercises the bound that actually ships.
    oversized = "x" * (REJECTED_PROMPT_MAX_BODY_BYTES + 1024)
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        big = await client.post("/v1/embeddings", json={"model": "m", "input": oversized})
        await asyncio.sleep(0)
        small = await client.post("/v1/embeddings", json={"model": "m", "input": "hi"})
        await asyncio.sleep(0)

    assert big.status_code == 429
    assert small.status_code == 429
    # Oversized declined; a small one on the same pre-parsed path still captured,
    # so the decline above is the cap and not a broken cached-body path.
    assert log_calls[0]["prompt"] == ""
    assert log_calls[1]["prompt"] == "hi"


@pytest.mark.asyncio
async def test_blocked_ip_queued_log_retains_no_body_bytes(monkeypatch, blocked_localhost):
    """The queued task must not keep the request's cached body alive.

    Capping the prompt is not enough on its own: the task retains the *request*,
    and ``capture_rejected_prompt`` caused Starlette to cache the raw bytes on
    it. Dropping only the prompt would free one of two references to the same
    megabyte, leaving the queue unbounded in practice.
    """
    app, log_calls, _op = _build_blocked_app(monkeypatch, lightweight_user=None, logging_on=True)
    await blocked_localhost("127.0.0.1")

    body = {"model": "gpt-4", "messages": [{"role": "user", "content": "z" * 3000}]}
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer hyi-valid"},
            json=body,
        )
        await asyncio.sleep(0)

    assert resp.status_code == 429
    # The prompt still made it through — releasing the body must not cost the row
    # the thing this whole change exists to record.
    assert log_calls[0]["prompt"] == body["messages"]
    # But the request handed to the queued task carries no body bytes.
    queued_request = log_calls[0]["request"]
    assert not getattr(queued_request, "_body", b"")
    assert getattr(queued_request, "_json", None) is None


@pytest.mark.asyncio
async def test_blocked_ip_settings_read_is_bounded_too(monkeypatch, blocked_localhost):
    """A spent budget skips even the toggle read, not just the lookups.

    ``RuntimeSettings._get`` has no single-flight, so an expired 30 s TTL under a
    flood would otherwise turn one expiry into a query per arriving request.
    """
    import serving.observability.rejection_log as mod

    app, _log_calls, op = _build_blocked_app(
        monkeypatch, lightweight_user={"user_id": "u1", "role": "pro"}, logging_on=True
    )
    rs = app.state.services.runtime_settings
    await blocked_localhost("127.0.0.1")

    exhausted = asyncio.Semaphore(1)
    await exhausted.acquire()
    monkeypatch.setattr(mod, "_enrichment_slots", exhausted)

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer hyi-valid"},
            json={"model": "gpt-4", "messages": [{"role": "user", "content": "hi"}]},
        )
        await asyncio.sleep(0)

    assert resp.status_code == 429
    # Not consulted at all on the inline path. log_rejection is faked out here,
    # so this asserts the enrichment gate specifically.
    rs.get_bool.assert_not_awaited()
    op.get_auth_context_lightweight.assert_not_awaited()


@pytest.mark.asyncio
async def test_blocked_ip_identity_lookup_failure_still_returns_429(monkeypatch, blocked_localhost):
    """A broken identity lookup degrades the log row, never the response."""
    app, log_calls, op = _build_blocked_app(monkeypatch, lightweight_user=None, logging_on=True)
    op.get_auth_context_lightweight = AsyncMock(side_effect=RuntimeError("db down"))
    await blocked_localhost("127.0.0.1")

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer hyi-valid"},
            json={"model": "gpt-4", "messages": [{"role": "user", "content": "hi"}]},
        )
        await asyncio.sleep(0)

    assert resp.status_code == 429
    assert resp.headers["Retry-After"] == "1000"
    assert log_calls[0]["user"] is None
    # The prompt is captured independently, so it survives the failed lookup.
    assert log_calls[0]["prompt"] == [{"role": "user", "content": "hi"}]


@pytest.mark.asyncio
async def test_missing_api_key_logs_rejection(monkeypatch):
    """No Authorization header -> 401 + log_rejection(error_code='auth_missing')."""
    app, log_calls = _build_app(monkeypatch, op_store_user=None)
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/v1/chat/completions")
        await asyncio.sleep(0)
    assert resp.status_code == 401
    assert len(log_calls) == 1
    assert log_calls[0]["error_code"] == "auth_missing"
    assert log_calls[0]["status_code"] == 401
    assert log_calls[0]["user"] is None


@pytest.mark.asyncio
async def test_invalid_api_key_logs_rejection(monkeypatch):
    """Unknown key -> 401 + log_rejection(error_code='auth_invalid')."""
    app, log_calls = _build_app(monkeypatch, op_store_user=None)
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer hyi-bogus"},
        )
        await asyncio.sleep(0)
    assert resp.status_code == 401
    assert len(log_calls) == 1
    assert log_calls[0]["error_code"] == "auth_invalid"


@pytest.mark.asyncio
async def test_quota_exceeded_logs_rejection(monkeypatch):
    """Authenticated user over quota -> 429 + log_rejection(error_code='quota_exceeded')."""
    user_row = {
        "id": 1,
        "user_id": "u1",
        "user_name": "Test",
        "role": "free",
        "email": None,
        "email_verified": True,
        "quota_daily_cost_usd": 0.001,  # very low
    }
    app, log_calls = _build_app(monkeypatch, op_store_user=user_row)

    # Override get_user_cost_today to push us over the quota.
    async def fake_op_store_dep():
        op = MagicMock()
        op.get_auth_context_by_key_hash = AsyncMock(return_value=user_row)
        op.get_user_cost_today = AsyncMock(return_value=10.0)
        op.update_key_last_used = AsyncMock()
        return op

    from serving.servers.deps import get_operational_store

    app.dependency_overrides[get_operational_store] = fake_op_store_dep

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer hyi-valid"},
        )
        await asyncio.sleep(0)
    assert resp.status_code == 429
    assert len(log_calls) == 1
    assert log_calls[0]["error_code"] == "quota_exceeded"
    assert log_calls[0]["user"]["user_id"] == "u1"
