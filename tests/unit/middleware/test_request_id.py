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


@pytest.mark.asyncio
async def test_client_error_kind_reset_between_requests() -> None:
    # A prior model-not-found request leaves the marker in the context. The
    # middleware must clear it so a later request's upstream 404 isn't logged
    # with the stale tag and wrongly excluded from the failed-request alert.
    req_ctx.mark_model_not_found()
    assert req_ctx.get().get(req_ctx.CLIENT_ERROR_KIND) == req_ctx.MODEL_NOT_FOUND
    captured: dict = {}
    await _drive([], captured)
    assert captured.get(req_ctx.CLIENT_ERROR_KIND) is None


@pytest.mark.asyncio
async def test_provider_reset_between_requests() -> None:
    """A durable upstream attribution must not outlive the request that made it.

    The completions/embeddings error paths publish ``provider`` with
    ``req_ctx.update``, which is not self-unwinding, so the label survives the end
    of the failed request. Both the request log and the failed-request rule read a
    present ``provider`` as "an upstream refused us", so a gateway-issued 401
    seeded in the same task would inherit the label and be counted as an upstream
    outage — turning ordinary client-auth churn into a service-failure signal.
    """
    req_ctx.publish_upstream_provider("diffusiongemma")
    assert req_ctx.get().get(req_ctx.PROVIDER) == "diffusiongemma"
    captured: dict = {}
    await _drive([], captured)
    assert captured.get(req_ctx.PROVIDER) is None


@pytest.mark.asyncio
async def test_clearing_provider_preserves_reader_defaults() -> None:
    """Clearing must drop ``provider``, not set it to ``None``.

    Some readers supply a non-``None`` default — ``_provider_for_error`` falls back
    to the ``"router"`` sentinel and the HTTP retry log to ``"unknown"``. A
    present-but-``None`` value silences the default, so a pre-routing failure
    would be labelled ``None`` instead of ``"router"`` in the DB log row (where
    the provider-performance aggregations filter on the sentinel by name).
    """
    req_ctx.publish_upstream_provider("diffusiongemma")
    captured: dict = {}
    await _drive([], captured)
    assert req_ctx.PROVIDER not in captured
    assert captured.get(req_ctx.PROVIDER, req_ctx.ROUTER_PROVIDER_SENTINEL) == "router"


@pytest.mark.asyncio
async def test_reset_keeps_keys_outside_the_request_scope() -> None:
    """Only per-request keys are cleared; unrelated context is left alone.

    ``model``, affinity keys and similar are written by handlers and read back
    within the same request, so a blanket wipe would break them.
    """
    req_ctx.update({"model": "kept-model", "affinity_key": "kept-key"})
    captured: dict = {}
    await _drive([], captured)
    assert captured.get("model") == "kept-model"
    assert captured.get("affinity_key") == "kept-key"


@pytest.mark.asyncio
async def test_every_request_scoped_key_is_cleared() -> None:
    """The reset covers the whole declared key set, not a hand-maintained subset.

    Guards the invariant rather than one key: a future durable per-request key
    added to ``REQUEST_SCOPED_KEYS`` is cleared automatically, and one dropped
    from it fails here instead of silently leaking into the next request.
    """
    req_ctx.update(dict.fromkeys(req_ctx.REQUEST_SCOPED_KEYS, "stale"))
    captured: dict = {}
    await _drive([], captured)
    for key in req_ctx.REQUEST_SCOPED_KEYS:
        assert captured.get(key) is None, f"{key} leaked from the previous request"
