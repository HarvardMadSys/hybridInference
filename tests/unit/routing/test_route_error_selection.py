"""Which failed route's error a fully-failed request is reported as.

Regression coverage for a production incident on ``deepseek-v4-flash``: one
client conversation carried a malformed historical tool call, and the model had
six configured routes but only one live one. When the weighted pick landed on a
dead endpoint, the user was told ``{"code": 500, "message": "Internal server
error"}`` -- a retryable status -- even though the fallback route had already
answered with the 400 naming the malformed payload. The same request produced
one of two different answers depending on a coin flip at selection time.

The rule under test is deliberately narrow: only statuses that describe the
*request* may displace the primary's error. A fallback's 403 ("you've reached
your concurrent request limit") or 429 must not, or a transient capacity blip on
one route becomes a terminal, non-retryable client error.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from routing.routers import (
    FixedRouter,
    _describes_request,
    _RouteAttempt,
    _select_surfaced_error,
)
from serving.adapters.base import BaseAdapter, ModelConfig

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

_MESSAGES = [{"role": "user", "content": "hi"}]

# The exact upstream body that wedged the production conversation.
_POISON_400 = "Assistant tool call function.arguments must be valid JSON."


def _cfg(provider: str) -> ModelConfig:
    return ModelConfig(
        id="m",
        name="m",
        provider=provider,
        base_url="http://test",
        context_length=8192,
        max_output_length=4096,
    )


class _UpstreamStatusError(RuntimeError):
    """An upstream rejection carrying its HTTP status, as adapters raise them."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


class _TransportError(RuntimeError):
    """A connection failure to a dead endpoint: no HTTP status anywhere on it.

    Stands in for aiohttp's ``ClientConnectorError``, which is what the
    classifier in ``completions_stream`` defaults to 500 for want of a status.
    """


class _RaisingAdapter(BaseAdapter):
    """Adapter that fails every call with one preset exception."""

    def __init__(self, config: ModelConfig, error: BaseException) -> None:
        super().__init__(config)
        self.error = error

    async def chat_completion(self, messages: list[dict[str, Any]], **params) -> dict[str, Any]:
        raise self.error

    async def stream_chat_completion(
        self, messages: list[dict[str, Any]], **params
    ) -> AsyncGenerator[str, None]:
        raise self.error
        yield  # make this an async generator  # pragma: no cover


def _router(primary: BaseAdapter, *backups: BaseAdapter) -> FixedRouter:
    """Route ``m`` over every adapter, with selection pinned to ``primary``.

    Backups are tried in the order given: the fallback loop walks the route's
    registered adapters, and with no weight-override resolver installed that is
    registration order.
    """
    router = FixedRouter()
    router.register_route("m", [(primary, 0.9), *((backup, 0.1) for backup in backups)])
    # Defeat the weighted coin flip: this suite is about what happens *after*
    # the primary fails, so which adapter is primary must not be random.
    router._select_adapter = lambda model_id, **kw: primary  # type: ignore[assignment]
    return router


async def _drain_stream(router: FixedRouter) -> None:
    async for _ in router.stream_chat_completion("m", messages=_MESSAGES):
        pass


# ---------------------------------------------------------------------------
# The whitelist itself
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("status", [400, 404, 413, 422])
def test_request_describing_statuses_are_eligible(status):
    assert _describes_request(_UpstreamStatusError(status, "bad request"))


@pytest.mark.unit
@pytest.mark.parametrize("status", [401, 403, 407, 408, 429, 500, 502, 503])
def test_capacity_and_credential_statuses_are_not_eligible(status):
    """A fallback's auth challenge or overload signal must never be promoted.

    403 in particular is what several providers return for "you've reached your
    concurrent request limit" -- surfacing it in place of the primary's error
    would tell a user their key was revoked during a capacity blip.
    """
    assert not _describes_request(_UpstreamStatusError(status, "not about the request"))


@pytest.mark.unit
def test_statusless_exception_is_not_eligible():
    assert not _describes_request(_TransportError("Cannot connect to host 127.0.0.1:8004"))


@pytest.mark.unit
def test_selection_prefers_primary_when_both_describe_the_request():
    """Ties go to the primary: it is the route the request was actually sent to."""
    primary = _RouteAttempt(
        _RaisingAdapter(_cfg("a"), RuntimeError()), _UpstreamStatusError(400, "primary")
    )
    backup = _RouteAttempt(
        _RaisingAdapter(_cfg("b"), RuntimeError()), _UpstreamStatusError(400, "backup")
    )

    assert _select_surfaced_error([primary, backup]) is primary


@pytest.mark.unit
def test_selection_takes_the_first_eligible_fallback():
    primary = _RouteAttempt(_RaisingAdapter(_cfg("a"), RuntimeError()), _TransportError("dead"))
    first = _RouteAttempt(
        _RaisingAdapter(_cfg("b"), RuntimeError()), _UpstreamStatusError(429, "busy")
    )
    second = _RouteAttempt(
        _RaisingAdapter(_cfg("c"), RuntimeError()), _UpstreamStatusError(422, "bad")
    )
    third = _RouteAttempt(
        _RaisingAdapter(_cfg("d"), RuntimeError()), _UpstreamStatusError(400, "also bad")
    )

    assert _select_surfaced_error([primary, first, second, third]) is second


# ---------------------------------------------------------------------------
# Streaming path
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stream_surfaces_fallback_400_over_primary_transport_error():
    """The incident in one test: a dead primary must not mask a live route's 400."""
    primary = _RaisingAdapter(_cfg("dead-local"), _TransportError("Cannot connect to host"))
    backup = _RaisingAdapter(_cfg("live-local"), _UpstreamStatusError(400, _POISON_400))
    router = _router(primary, backup)

    with pytest.raises(_UpstreamStatusError) as exc_info:
        await _drain_stream(router)

    assert exc_info.value.status_code == 400
    assert str(exc_info.value) == _POISON_400


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stream_surfaced_fallback_error_carries_routing():
    """``_routing`` must ride along onto whichever error is surfaced.

    Without it the error-log path falls back to the provider it last saw on the
    wire, and api_logs attribution plus cost accounting break gateway-wide --
    the very failure the primary-error ``_routing`` attach exists to prevent.
    """
    primary = _RaisingAdapter(_cfg("dead-local"), _TransportError("Cannot connect to host"))
    backup = _RaisingAdapter(_cfg("live-local"), _UpstreamStatusError(400, _POISON_400))
    router = _router(primary, backup)

    with pytest.raises(_UpstreamStatusError) as exc_info:
        await _drain_stream(router)

    routing = getattr(exc_info.value, "_routing", None)
    assert routing is not None
    assert routing["provider"] == "live-local"
    assert routing["endpoint_id"] == "live-local"
    # The provider named here is not where the request was routed, so the row
    # has to say so -- same marker the success path puts on a fallback response.
    assert routing["fallback"] is True
    # The chain is telemetry for both attempts regardless of which one is shown.
    assert [a["provider"] for a in routing["failed_attempts"]] == ["dead-local", "live-local"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stream_surfaces_the_first_eligible_fallback_not_the_last_attempted():
    """Attribution must follow the error that is reported, not the last route tried.

    The generator emits a synthetic routing chunk before *every* fallback
    attempt and the consumer keeps overwriting its provider with the latest one
    seen, so without ``_routing`` on the surfaced error this row would be
    attributed to ``busy-remote`` -- a route whose 429 nobody is being shown.
    """
    primary = _RaisingAdapter(_cfg("dead-local"), _TransportError("Cannot connect to host"))
    first = _RaisingAdapter(_cfg("live-local"), _UpstreamStatusError(400, _POISON_400))
    last = _RaisingAdapter(_cfg("busy-remote"), _UpstreamStatusError(429, "rate limited"))
    router = _router(primary, first, last)

    with pytest.raises(_UpstreamStatusError) as exc_info:
        await _drain_stream(router)

    assert exc_info.value.status_code == 400
    assert exc_info.value._routing["provider"] == "live-local"
    assert [a["provider"] for a in exc_info.value._routing["failed_attempts"]] == [
        "dead-local",
        "live-local",
        "busy-remote",
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stream_keeps_primary_error_when_fallback_is_only_rate_limited():
    """429 is excluded, so the primary's error stands and stays retryable."""
    primary_error = _TransportError("Cannot connect to host")
    primary = _RaisingAdapter(_cfg("dead-local"), primary_error)
    backup = _RaisingAdapter(_cfg("busy-remote"), _UpstreamStatusError(429, "rate limited"))
    router = _router(primary, backup)

    with pytest.raises(_TransportError) as exc_info:
        await _drain_stream(router)

    assert exc_info.value is primary_error
    routing = getattr(exc_info.value, "_routing", None)
    assert routing is not None
    assert routing["provider"] == "dead-local"
    # The request really was routed here, so the fallback marker stays off.
    assert "fallback" not in routing
    assert [a["provider"] for a in routing["failed_attempts"]] == ["dead-local", "busy-remote"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stream_keeps_primary_error_when_fallback_hits_concurrency_403():
    """The case that got the naive "any 4xx wins" rule rejected in review."""
    primary_error = _TransportError("Cannot connect to host")
    primary = _RaisingAdapter(_cfg("dead-local"), primary_error)
    backup = _RaisingAdapter(
        _cfg("kimi_coding-api"),
        _UpstreamStatusError(403, "You've reached your concurrent request limit"),
    )
    router = _router(primary, backup)

    with pytest.raises(_TransportError) as exc_info:
        await _drain_stream(router)

    assert exc_info.value is primary_error


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stream_prefers_primary_when_both_return_400():
    primary_error = _UpstreamStatusError(400, _POISON_400)
    primary = _RaisingAdapter(_cfg("local-a"), primary_error)
    backup = _RaisingAdapter(_cfg("local-b"), _UpstreamStatusError(400, "a different complaint"))
    router = _router(primary, backup)

    with pytest.raises(_UpstreamStatusError) as exc_info:
        await _drain_stream(router)

    assert exc_info.value is primary_error
    assert exc_info.value._routing["provider"] == "local-a"


# ---------------------------------------------------------------------------
# Non-streaming path -- same rule, same terminal raise
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_chat_surfaces_fallback_400_over_primary_transport_error():
    primary = _RaisingAdapter(_cfg("dead-local"), _TransportError("Cannot connect to host"))
    backup = _RaisingAdapter(_cfg("live-local"), _UpstreamStatusError(400, _POISON_400))
    router = _router(primary, backup)

    with pytest.raises(_UpstreamStatusError) as exc_info:
        await router.chat_completion("m", messages=_MESSAGES)

    assert exc_info.value.status_code == 400
    routing = getattr(exc_info.value, "_routing", None)
    assert routing is not None
    assert routing["provider"] == "live-local"
    assert [a["provider"] for a in routing["failed_attempts"]] == ["dead-local", "live-local"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_chat_keeps_primary_error_when_fallback_is_only_rate_limited():
    primary_error = _TransportError("Cannot connect to host")
    primary = _RaisingAdapter(_cfg("dead-local"), primary_error)
    backup = _RaisingAdapter(_cfg("busy-remote"), _UpstreamStatusError(429, "rate limited"))
    router = _router(primary, backup)

    with pytest.raises(_TransportError) as exc_info:
        await router.chat_completion("m", messages=_MESSAGES)

    assert exc_info.value is primary_error
    assert exc_info.value._routing["provider"] == "dead-local"
