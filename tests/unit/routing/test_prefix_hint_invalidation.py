"""Which failures forget an endpoint's warm-prefix hints, and what forgetting costs.

A hint says "this caller's last prompt is resident in this endpoint's radix
cache". A failure that can mean the endpoint restarted -- a 5xx, or no answer at
all -- has to drop the hints, or a cold continuation is stamped with the
preempting interactive tier. Every other failure must leave them: an endpoint
that answered a 4xx is up with its cache intact, and a request refused before it
was sent never reached the endpoint at all. Forgetting on those let any caller
wipe every other caller's hints for an endpoint with one malformed request.
"""

from __future__ import annotations

from collections import OrderedDict
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import aiohttp
import pytest

from routing import prefill_load
from routing.endpoint_health import failure_may_lose_prefix_cache
from routing.engine_wait import EngineWaitExpired
from routing.prefill_load import PrefillLoadTracker
from routing.routers import FixedRouter
from serving.adapters.base import BaseAdapter, ModelConfig
from serving.adapters.key_pool import KeyPoolExhausted, KeyPoolRoleRestricted
from serving.adapters.upstream_limiter import UpstreamQueueWaitExpired, UpstreamSaturated
from serving.exceptions import UpstreamModelQueued
from serving.utils import context as req_ctx

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

_PROMPT = [{"role": "user", "content": "x" * (4 * 300_000)}]


class _StatusError(RuntimeError):
    """An upstream rejection carrying its HTTP status, as adapters raise them."""

    def __init__(self, status_code: int) -> None:
        super().__init__(f"upstream answered {status_code}")
        self.status_code = status_code


# ``ClientResponseError.__str__`` reads ``request_info.real_url``.
_REQUEST_INFO = SimpleNamespace(real_url="http://upstream/v1/chat/completions")


def _response_error(status: int) -> aiohttp.ClientResponseError:
    return aiohttp.ClientResponseError(
        request_info=_REQUEST_INFO, history=(), status=status, message="x"
    )


def _model_queued() -> UpstreamModelQueued:
    return UpstreamModelQueued(request_info=_REQUEST_INFO, history=(), status=503, message="queued")


def _cfg(provider: str) -> ModelConfig:
    return ModelConfig(
        id="m",
        name="m",
        provider=provider,
        base_url=f"http://{provider}",
        context_length=8192,
        max_output_length=4096,
    )


class _RaisingAdapter(BaseAdapter):
    def __init__(self, config: ModelConfig, error: BaseException) -> None:
        super().__init__(config)
        self.error = error

    async def chat_completion(self, messages: list[dict[str, Any]], **params) -> dict[str, Any]:
        raise self.error

    async def stream_chat_completion(
        self, messages: list[dict[str, Any]], **params
    ) -> AsyncGenerator[str, None]:
        raise self.error
        yield  # pragma: no cover


def _warm(tracker: PrefillLoadTracker, endpoint_id: str, caller: str = "u1") -> None:
    """Leave ``caller``'s confirmed prompt behind on ``endpoint_id``."""
    tracker.release(
        tracker.acquire(
            endpoint_id,
            prefill_load.estimate_prefill_tokens(_PROMPT),
            affinity_key=caller,
            fingerprint=prefill_load.conversation_fingerprint(_PROMPT),
            anchor=prefill_load.prompt_anchor(_PROMPT),
        ),
        prefill_confirmed=True,
    )


def _is_warm(tracker: PrefillLoadTracker, endpoint_id: str, caller: str = "u1") -> bool:
    size = prefill_load.estimate_prefill_tokens(_PROMPT)
    return tracker.uncached_estimate(endpoint_id, size, caller) < size


# ---------------------------------------------------------------------------
# The classification
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "exc",
    [
        None,
        RuntimeError("no status anywhere"),
        TimeoutError("read timed out"),
        ConnectionResetError("reset by peer"),
        aiohttp.ServerDisconnectedError(),
        aiohttp.ClientConnectionError("cannot connect"),
        _StatusError(500),
        _StatusError(502),
        _response_error(503),
        _response_error(504),
        _model_queued(),
    ],
    ids=repr,
)
def test_failures_that_can_mean_a_restart_lose_the_cache(exc):
    assert failure_may_lose_prefix_cache(exc)


@pytest.mark.unit
@pytest.mark.parametrize(
    "exc",
    [
        _StatusError(400),
        _StatusError(404),
        _StatusError(413),
        _response_error(422),
        # Counted against the endpoint by the breaker, but answered all the same.
        _StatusError(401),
        _StatusError(403),
        _StatusError(407),
        _StatusError(408),
        _response_error(429),
        # Never sent, or the engine was only busy.
        KeyPoolExhausted("all keys muted"),
        KeyPoolRoleRestricted("no key for this tier"),
        UpstreamSaturated("gateway limiter"),
        UpstreamQueueWaitExpired("queue wait over"),
        EngineWaitExpired("m:local-1", 5.0),
    ],
    ids=repr,
)
def test_failures_the_endpoint_survives_keep_the_cache(exc):
    assert not failure_may_lose_prefix_cache(exc)


# ---------------------------------------------------------------------------
# FixedRouter
# ---------------------------------------------------------------------------


async def _dispatch(router: FixedRouter, *, stream: bool) -> None:
    if stream:
        async for _ in router.stream_chat_completion("m", _PROMPT):
            pass
    else:
        await router.chat_completion("m", _PROMPT)


_PATHS = pytest.mark.parametrize("stream", [False, True], ids=["chat", "stream"])


@pytest.mark.unit
@pytest.mark.asyncio
@_PATHS
@pytest.mark.parametrize(
    "error",
    [_StatusError(400), _response_error(429), KeyPoolExhausted("all keys muted")],
    ids=["client-400", "rate-limited", "key-pool-exhausted"],
)
async def test_a_failure_the_endpoint_survives_leaves_every_callers_hints(stream, error):
    router = FixedRouter()
    router.register_route("m", [(_RaisingAdapter(_cfg("bad"), error), 1.0)])
    endpoint_id = "bad"
    _warm(router.prefill_load, endpoint_id, caller="someone-else")
    req_ctx.set({"affinity_key": "attacker"})

    with pytest.raises(type(error)):
        await _dispatch(router, stream=stream)

    assert _is_warm(router.prefill_load, endpoint_id, caller="someone-else")


@pytest.mark.unit
@pytest.mark.asyncio
@_PATHS
@pytest.mark.parametrize(
    "error",
    [_StatusError(503), aiohttp.ServerDisconnectedError(), _model_queued()],
    ids=["503", "disconnected", "model-queued"],
)
async def test_a_failure_that_can_mean_a_restart_forgets_the_hints(stream, error):
    router = FixedRouter()
    router.register_route("m", [(_RaisingAdapter(_cfg("bad"), error), 1.0)])
    endpoint_id = "bad"
    _warm(router.prefill_load, endpoint_id, caller="someone-else")

    with pytest.raises(type(error)):
        await _dispatch(router, stream=stream)

    assert not _is_warm(router.prefill_load, endpoint_id, caller="someone-else")


# ---------------------------------------------------------------------------
# The cost of forgetting
# ---------------------------------------------------------------------------


class _NoScanHints(OrderedDict):
    """Hint storage that refuses to be walked, so a full scan fails the test."""

    def __iter__(self):
        raise AssertionError("forget_endpoint walked every hint")

    def keys(self):
        raise AssertionError("forget_endpoint walked every hint")

    def items(self):
        raise AssertionError("forget_endpoint walked every hint")


@pytest.mark.unit
def test_forgetting_an_endpoint_reads_only_that_endpoints_hints():
    tracker = PrefillLoadTracker()
    tracker._prefix_hints = _NoScanHints()
    for caller in range(200):
        _warm(tracker, "busy", caller=f"c{caller}")
    _warm(tracker, "failed", caller="c0")
    _warm(tracker, "failed", caller="c1")

    tracker.forget_endpoint("failed")

    assert not _is_warm(tracker, "failed", caller="c0")
    assert not _is_warm(tracker, "failed", caller="c1")
    assert all(_is_warm(tracker, "busy", caller=f"c{caller}") for caller in range(200))
    assert "failed" not in tracker._hint_callers


@pytest.mark.unit
def test_forgetting_twice_or_an_unknown_endpoint_is_harmless():
    tracker = PrefillLoadTracker()
    _warm(tracker, "a")

    tracker.forget_endpoint("never-seen")
    tracker.forget_endpoint("a")
    tracker.forget_endpoint("a")

    assert not _is_warm(tracker, "a")
    assert tracker._hint_callers == {}


@pytest.mark.unit
def test_the_endpoint_index_follows_eviction(monkeypatch):
    """A hint evicted for space leaves the index too, so neither grows past the cap."""
    monkeypatch.setattr(prefill_load, "_PREFIX_HINT_MAX_ENTRIES", 3)
    tracker = PrefillLoadTracker()
    for caller in ("c1", "c2", "c3", "c4", "c5"):
        _warm(tracker, "a" if caller in ("c1", "c2") else "b", caller=caller)

    # c1 and c2 were the oldest and were evicted: "a" holds nothing any more.
    assert list(tracker._prefix_hints) == [("c3", "b"), ("c4", "b"), ("c5", "b")]
    assert tracker._hint_callers == {"b": {"c3", "c4", "c5"}}

    tracker.forget_endpoint("b")

    assert len(tracker._prefix_hints) == 0
    assert tracker._hint_callers == {}


@pytest.mark.unit
def test_a_rewritten_hint_is_indexed_once():
    tracker = PrefillLoadTracker()
    _warm(tracker, "a")
    _warm(tracker, "a")

    assert tracker._hint_callers == {"a": {"u1"}}
    tracker.forget_endpoint("a")
    assert len(tracker._prefix_hints) == 0
