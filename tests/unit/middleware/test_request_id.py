"""Unit tests for RequestIdMiddleware request-context seeding."""

from __future__ import annotations

from typing import Any

import pytest

from serving.servers.middleware.request_id import RequestIdMiddleware
from serving.utils import context as req_ctx


async def _drive(headers: list[tuple[bytes, bytes]], captured: dict) -> None:
    """Run one HTTP scope through the middleware, capturing the seeded context."""

    async def app(scope: dict, receive: Any, send: Any) -> None:
        captured.clear()
        captured.update(req_ctx.get())
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(_message: dict) -> None:
        return None

    scope = {"type": "http", "headers": headers, "state": {}}
    await RequestIdMiddleware(app)(scope, receive, send)


@pytest.mark.asyncio
async def test_captures_user_agent_into_context() -> None:
    captured: dict = {}
    await _drive([(b"user-agent", b"my-client/9.9")], captured)
    assert captured.get("client_user_agent") == "my-client/9.9"
    assert captured.get("request_id")


@pytest.mark.asyncio
async def test_absent_user_agent_sets_none() -> None:
    captured: dict = {}
    await _drive([], captured)
    assert captured.get("client_user_agent") is None


@pytest.mark.asyncio
async def test_absent_user_agent_overwrites_previous() -> None:
    # Sequential scopes in the same task must not inherit the prior UA.
    captured: dict = {}
    await _drive([(b"user-agent", b"first/1.0")], captured)
    assert captured.get("client_user_agent") == "first/1.0"
    await _drive([], captured)
    assert captured.get("client_user_agent") is None


@pytest.mark.asyncio
async def test_identity_keys_reset_between_requests() -> None:
    # A prior authenticated completion leaves user_id/user_name in the context.
    # The middleware must clear them so a later route that doesn't authenticate
    # (e.g. the admin playground) can't have a circuit-breaker alert
    # misattributed to the earlier caller.
    req_ctx.update({"user_id": "01PREVUSER", "user_name": "prev-user"})
    captured: dict = {}
    await _drive([], captured)
    assert captured.get("user_id") is None
    assert captured.get("user_name") is None
