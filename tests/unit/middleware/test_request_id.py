"""Unit tests for RequestIdMiddleware request-context seeding."""

from __future__ import annotations

import datetime as dt
import re
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

    ``upstream_queue_deadline`` and similar are scoped by the self-unwinding
    ``req_ctx.push`` around the adapter call rather than by this reset, so a
    blanket wipe would be clearing state the middleware has no business owning.
    """
    req_ctx.update({req_ctx.UPSTREAM_QUEUE_DEADLINE: "kept-deadline"})
    captured: dict = {}
    await _drive([], captured)
    assert captured.get(req_ctx.UPSTREAM_QUEUE_DEADLINE) == "kept-deadline"


#: The per-request keys as of this change, spelled out here rather than read from
#: ``req_ctx.REQUEST_SCOPED_KEYS``. Seeding *and* asserting from that tuple would
#: make the clearing test below vacuous in the direction that matters: dropping a
#: key from the tuple would remove it from both halves, so the test would keep
#: passing while the key silently leaked into the next request. Pinning the
#: membership separately means a removal has to be made here too — deliberately,
#: with the leak in view.
_PINNED_REQUEST_SCOPED_KEYS = frozenset(
    {
        "client_user_agent",
        "user_id",
        "user_name",
        "user_role",
        "auth_key_hash",
        "affinity_key",
        "client_error_kind",
        "provider",
        # Published with ``provider`` once routing resolves, for the request
        # log's line; a leftover would name the last request's model.
        "model",
        "pricing_time",
        "pricing_snapshot",
        # Written durably by the Anthropic Messages surface, which has no
        # fallback to publish around and hands its upstream to a background
        # reader task. A leftover value would rank the next request -- a
        # different prompt, possibly on a different endpoint.
        "upstream_priority",
        # The model an auto request's errors must not name. A leftover value
        # would rewrite the next request's error messages.
        "concealed_model",
        # Trusted-probe verdict from the inference handlers; the request-log
        # middleware demotes probe lines from it, so a leftover True would
        # demote the next request on the task to DEBUG.
        "synthetic_probe",
        # Published only for a batch item, in the scope the worker opens for it:
        # the anthropic-version it was created under. Cleared anyway, so no
        # later scope on the task inherits it. (``batch_execution`` is gone: a
        # batch item's adapter constraint is a parameter of the Messages
        # execution now, never in the request scope, so nothing can leak.)
        "anthropic_version",
    }
)


def test_request_scoped_key_set_is_pinned() -> None:
    """``REQUEST_SCOPED_KEYS`` matches the set the clearing test guards.

    Each key is there because some consumer reads "key present" as a fact about
    the current request: ``user_id``/``user_name`` for circuit-breaker
    attribution, ``client_error_kind`` for the 404 split, ``provider`` for the
    401 split, ``user_role`` for access to tier-reserved provider keys (absent
    means "internal caller, unrestricted"), ``auth_key_hash``/``affinity_key``
    for which upstream key and provider a caller sticks to (absent means "no
    caller identity", which shares one binding), ``upstream_priority`` for the
    scheduling rank an sglang backend is told to queue by. Dropping one
    un-clears it and makes the next request inherit it, so the set is not
    something to shrink as a side effect of another change.
    """
    assert set(req_ctx.REQUEST_SCOPED_KEYS) == _PINNED_REQUEST_SCOPED_KEYS


@pytest.mark.asyncio
async def test_every_request_scoped_key_is_cleared() -> None:
    """The reset covers the whole declared key set, not a hand-maintained subset.

    Checks the union of the pinned set and the live tuple: the pinned half keeps
    a key that is dropped from ``REQUEST_SCOPED_KEYS`` under test (it fails here
    rather than leaking), and the live half covers a future key added to the
    tuple without anyone touching this file.
    """
    keys = _PINNED_REQUEST_SCOPED_KEYS | set(req_ctx.REQUEST_SCOPED_KEYS)
    req_ctx.update(dict.fromkeys(keys, "stale"))
    captured: dict = {}
    await _drive([], captured)
    for key in keys:
        if key == req_ctx.PRICING_TIME:
            pricing_time = captured.get(key)
            assert isinstance(pricing_time, dt.datetime)
            assert pricing_time.tzinfo is not None
            assert pricing_time.utcoffset() == dt.timedelta(0)
            continue
        assert captured.get(key) is None, f"{key} leaked from the previous request"


# --- the client's own request id ----------------------------------------------


async def _seen_id(headers: list[tuple[bytes, bytes]]) -> tuple[dict, bytes]:
    seen: dict = {}
    sent: list[dict] = []

    async def app(scope, receive, send):
        seen.update(req_ctx.get())
        seen["state"] = scope["state"]["request_id"]
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "method": "GET", "path": "/", "headers": headers}
    await RequestIdMiddleware(app)(scope, None, send)
    echoed = dict(sent[0]["headers"])[b"x-request-id"]
    return seen, echoed


@pytest.mark.asyncio
async def test_a_short_printable_request_id_is_adopted_and_echoed() -> None:
    seen, echoed = await _seen_id([(b"x-request-id", b"req-support-1234")])
    assert seen["request_id"] == seen["state"] == "req-support-1234"
    assert seen["client_request_id"] == "req-support-1234"
    assert echoed == b"req-support-1234"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value",
    [b"x" * 129, b"has space", b"tab\there", b"line\nbreak", b"\xe9t\xe9", b""],
    ids=["too-long", "space", "tab", "newline", "latin-1", "empty"],
)
async def test_any_other_request_id_is_replaced_by_one_the_gateway_mints(value: bytes) -> None:
    seen, echoed = await _seen_id([(b"x-request-id", value)])
    assert re.fullmatch(r"[0-9a-f]{24}", seen["request_id"])
    assert seen["client_request_id"] is None
    assert echoed == seen["request_id"].encode()


@pytest.mark.asyncio
async def test_a_minted_request_id_is_not_marked_as_the_clients() -> None:
    seen, _ = await _seen_id([])
    assert seen["client_request_id"] is None
