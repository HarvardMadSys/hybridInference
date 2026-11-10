"""How far fallback walks a request that the endpoints it reaches fail on.

A 5xx, or a connection dropped once open, after the request left the gateway is
what an engine that crashed, ran out of memory or restarted on the request looks
like -- and such a request does the same to the next engine. Walked on without a
bound it was replayed on every route of its model and charged to every breaker,
so three of them opened every circuit for a cooldown and every other caller got
a 503.

The rules under test: once an endpoint has failed on the request, at most two
more routes are tried; a request charges at most one breaker for failing that
way; and every other failure -- a route that refuses connections, a request
that never left the gateway -- walks on and is charged as before.

The fake engines take their outbound slot from the process-wide limiter, as the
real adapters do: that is what tells the router the request was sent.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import aiohttp
import pytest
from aiohttp.client_reqrep import ConnectionKey

from routing.endpoint_health import failed_handling_request
from routing.engine_wait import EngineWaitExpired
from routing.routers import AllCircuitsOpenError, FixedRouter
from serving.adapters.base import BaseAdapter, ModelConfig
from serving.adapters.key_pool import KeyPoolExhausted
from serving.adapters.upstream_limiter import (
    UpstreamConcurrencyLimiter,
    UpstreamSaturated,
    reset_upstream_limiter,
    upstream_slot,
)
from serving.exceptions import UpstreamModelQueued, UpstreamStreamIdleError

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

MODEL = "m"
KEY = "k"
_MESSAGES = [{"role": "user", "content": "hi"}]
_POISON = [{"role": "user", "content": "poison"}]
_CONTENT_CHUNK = 'data: {"choices": [{"index": 0, "delta": {"content": "ok"}}]}\n\n'
_PATHS = pytest.mark.parametrize("stream", [False, True], ids=["chat", "stream"])

# ``ClientResponseError.__str__`` reads ``request_info.real_url``.
_REQUEST_INFO = SimpleNamespace(real_url="http://upstream/v1/chat/completions")


class _StatusError(RuntimeError):
    """An upstream rejection carrying its HTTP status, as adapters raise them."""

    def __init__(self, status_code: int, message: str = "engine crashed") -> None:
        super().__init__(message)
        self.status_code = status_code


def _refused() -> aiohttp.ClientConnectorError:
    key = ConnectionKey("engine", 8000, False, True, None, None, None, None)
    return aiohttp.ClientConnectorError(key, ConnectionRefusedError(111, "Connection refused"))


class _Engine(BaseAdapter):
    """An engine that fails with ``error`` once the request is sent, else answers.

    ``on_poison_only`` fails only the poison prompt, as an engine that crashes on
    one request does. ``before_send`` raises before the outbound slot is taken,
    so the request never leaves the gateway.
    """

    def __init__(
        self,
        name: str,
        error: BaseException | None = None,
        *,
        on_poison_only: bool = False,
        before_send: bool = False,
    ) -> None:
        super().__init__(
            ModelConfig(
                id=MODEL,
                name=MODEL,
                provider=name,
                base_url=f"https://{name}.example/v1",
                endpoint_id=name,
                context_length=262_144,
                max_output_length=4096,
            )
        )
        self.error = error
        self.on_poison_only = on_poison_only
        self.before_send = before_send
        self.calls = 0

    async def _serve(self, messages: list[dict[str, Any]]) -> None:
        self.calls += 1
        fails = self.error is not None and (
            not self.on_poison_only or messages[-1]["content"] == "poison"
        )
        if fails and self.before_send:
            raise self.error
        async with upstream_slot(self.config.provider, KEY, base_url=self.config.base_url):
            if fails:
                raise self.error

    async def chat_completion(self, messages: list[dict[str, Any]], **params) -> dict[str, Any]:
        await self._serve(messages)
        return {"choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}}]}

    async def stream_chat_completion(
        self, messages: list[dict[str, Any]], **params
    ) -> AsyncGenerator[str, None]:
        await self._serve(messages)
        yield _CONTENT_CHUNK


@pytest.fixture(autouse=True)
def _limiter():
    """A fresh process-wide limiter with room to spare, so nothing queues by accident."""
    reset_upstream_limiter(
        UpstreamConcurrencyLimiter(initial_limit=8, max_limit=8, acquire_timeout=10.0)
    )
    yield
    reset_upstream_limiter()


def _router(primary: _Engine, *backups: _Engine) -> FixedRouter:
    """Route ``m`` over every engine, with selection pinned to ``primary``."""
    router = FixedRouter()
    router.register_route(MODEL, [(primary, 1.0), *((backup, 0.01) for backup in backups)])
    router._select_adapter = lambda model_id, **kw: primary  # type: ignore[assignment]
    return router


async def _run(router: FixedRouter, *, stream: bool, messages: list[dict[str, Any]]) -> Any:
    if stream:
        return [chunk async for chunk in router.stream_chat_completion(MODEL, messages)]
    return await router.chat_completion(MODEL, messages)


def _charged(router: FixedRouter, endpoint_id: str) -> bool:
    """Whether the endpoint's breaker has recorded a failure."""
    snapshot = router.endpoint_health_registry.snapshot()
    return endpoint_id in snapshot and snapshot[endpoint_id]["availability"] < 1.0


# ---------------------------------------------------------------------------
# The classification
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "exc",
    [
        _StatusError(500),
        _StatusError(502),
        aiohttp.ClientResponseError(request_info=_REQUEST_INFO, history=(), status=503),
        aiohttp.ServerDisconnectedError(),
        aiohttp.ClientPayloadError("Response payload is not completed"),
        ConnectionResetError("reset by peer"),
        RuntimeError("no status anywhere"),
    ],
    ids=repr,
)
def test_what_reads_as_an_endpoint_failing_on_the_request(exc):
    assert failed_handling_request(exc)


@pytest.mark.unit
@pytest.mark.parametrize(
    "exc",
    [
        _StatusError(400),
        _StatusError(404),
        _StatusError(429),
        _refused(),
        TimeoutError("read timed out"),
        UpstreamStreamIdleError(30.0),
        UpstreamModelQueued(request_info=_REQUEST_INFO, history=(), status=503),
        UpstreamSaturated("no slot"),
        KeyPoolExhausted("every key muted"),
        EngineWaitExpired("m:engine", 5.0),
    ],
    ids=repr,
)
def test_what_does_not(exc):
    assert not failed_handling_request(exc)


# ---------------------------------------------------------------------------
# FixedRouter's walk, streaming and not
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
@_PATHS
async def test_a_request_every_engine_fails_on_reaches_three_routes_and_one_breaker(stream):
    """The finding in one test: the walk stops two routes after the first fault."""
    engines = [_Engine(f"e{i}", _StatusError(500)) for i in range(5)]
    router = _router(*engines)

    with pytest.raises(_StatusError):
        await _run(router, stream=stream, messages=_POISON)

    assert [engine.calls for engine in engines] == [1, 1, 1, 0, 0]
    assert [_charged(router, engine.config.provider) for engine in engines] == [
        True,
        False,
        False,
        False,
        False,
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_three_poison_requests_no_longer_open_every_circuit():
    """The reviewer's repro: other callers were refused for a cooldown."""
    routes = [
        _Engine(name, _StatusError(500), on_poison_only=True)
        for name in ("vllm-spark", "sglang-local", "staging")
    ]
    router = FixedRouter()
    router.register_route(MODEL, [(routes[0], 1.0), (routes[1], 1.0), (routes[2], 0.01)])
    health = router.endpoint_health_registry
    charged: list[str] = []
    record_failure = health.record_failure

    def _record(endpoint_id: str, **kwargs: Any) -> None:
        charged.append(endpoint_id)
        record_failure(endpoint_id, **kwargs)

    health.record_failure = _record  # type: ignore[method-assign]

    for _ in range(3):
        with pytest.raises(_StatusError):
            await router.chat_completion(MODEL, _POISON)

    # Every route still saw each request (three routes: the primary and two
    # more), but only one breaker was charged per request...
    assert [route.calls for route in routes] == [3, 3, 3]
    assert len(charged) == 3
    # ...so at most one circuit can have opened, and an innocent caller is served.
    response = await router.chat_completion(MODEL, _MESSAGES)
    assert response["choices"][0]["message"]["content"] == "ok"


@pytest.mark.unit
@pytest.mark.asyncio
@_PATHS
async def test_the_ordinary_failover_is_unchanged(stream):
    """Two routes down still reach a healthy third."""
    down = _Engine("down", _StatusError(500))
    unavailable = _Engine("unavailable", _StatusError(503, "Service Unavailable"))
    healthy = _Engine("healthy")
    router = _router(down, unavailable, healthy)

    result = await _run(router, stream=stream, messages=_MESSAGES)

    assert healthy.calls == 1
    if stream:
        assert _CONTENT_CHUNK in result
    else:
        assert result["_routing"]["endpoint_id"] == "healthy"


@pytest.mark.unit
@pytest.mark.asyncio
@_PATHS
async def test_routes_that_refuse_connections_neither_start_the_bound_nor_escape_their_breaker(
    stream,
):
    """A route that is simply down never saw the request, and is charged as before."""
    refusing = [_Engine(f"refusing{i}", _refused()) for i in range(3)]
    healthy = _Engine("healthy")
    router = _router(*refusing, healthy)

    await _run(router, stream=stream, messages=_MESSAGES)

    assert healthy.calls == 1
    assert all(_charged(router, engine.config.provider) for engine in refusing)


@pytest.mark.unit
@pytest.mark.asyncio
@_PATHS
async def test_a_request_that_never_left_the_gateway_starts_no_bound(stream):
    failing = [_Engine(f"local{i}", RuntimeError("bad body"), before_send=True) for i in range(3)]
    healthy = _Engine("healthy")
    router = _router(*failing, healthy)

    await _run(router, stream=stream, messages=_MESSAGES)

    assert healthy.calls == 1


@pytest.mark.unit
@pytest.mark.asyncio
@_PATHS
async def test_only_the_first_fault_is_charged_but_a_refused_route_still_is(stream):
    first = _Engine("first", _StatusError(500))
    refusing = _Engine("refusing", _refused())
    second = _Engine("second", _StatusError(500))
    never = _Engine("never")
    router = _router(first, refusing, second, never)

    with pytest.raises(_StatusError):
        await _run(router, stream=stream, messages=_POISON)

    # ``never`` is the third route after the first fault.
    assert (first.calls, refusing.calls, second.calls, never.calls) == (1, 1, 1, 0)
    assert _charged(router, "first")
    assert _charged(router, "refusing")
    assert not _charged(router, "second")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stopping_and_the_skipped_charge_are_logged(caplog):
    engines = [_Engine(f"e{i}", _StatusError(500)) for i in range(4)]
    router = _router(*engines)

    with (
        caplog.at_level(logging.INFO, logger="routing.routers"),
        pytest.raises(_StatusError),
    ):
        await _run(router, stream=False, messages=_POISON)

    (stopped,) = [
        r for r in caplog.records if getattr(r, "event", None) == "endpoint_fault_fallback_stopped"
    ]
    assert stopped.model_id == MODEL
    assert stopped.fault_endpoint_id == "e0"
    assert stopped.endpoint_ids == ["e0", "e1", "e2"]
    skipped = [
        r.endpoint_id
        for r in caplog.records
        if getattr(r, "event", None) == "repeat_fault_skip_breaker"
    ]
    assert skipped == ["e1", "e2"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_all_circuits_open_only_after_their_own_failures():
    """A route charged as a primary still opens its circuit, as before."""
    broken = _Engine("broken", _StatusError(500))
    router = FixedRouter()
    router.register_route(MODEL, [(broken, 1.0)])

    for _ in range(3):
        with pytest.raises(_StatusError):
            await router.chat_completion(MODEL, _MESSAGES)

    with pytest.raises(AllCircuitsOpenError):
        await router.chat_completion(MODEL, _MESSAGES)
