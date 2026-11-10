"""How far fallback walks a request that routes refuse as malformed.

A 400 is usually the answering server's own verdict -- one SGLang build rejects
a tool schema another accepts -- so FixedRouter tries the next route after one.
Without a bound it tried every weighted route: a malformed request reached each
fallback weighted at 1%, the staging canary and any other gateway on the route,
each paying a full upload and tokenization to refuse it again.

The bound under test: once two routes have refused the request itself (a 400 or
a 422 other than a context-window refusal), the walk stops. Context-window
refusals keep their own rule, and every other failure (5xx, 429, transport, and
the route-specific 404 and 413) walks on as before.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import pytest

from routing.hybrid import HybridRouter
from routing.model_router_registry import ModelRouterRegistry
from routing.prefill_load import PrefillLoadTracker
from routing.routers import FixedRouter, request_refusals_end_fallback
from serving.adapters.base import BaseAdapter, ModelConfig
from serving.servers.hybrid_composition import HybridFixedRouterFactory

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

_MESSAGES = [{"role": "user", "content": "hi"}]
_CONTENT_CHUNK = 'data: {"choices": [{"index": 0, "delta": {"content": "ok"}}]}\n\n'
_TOOL_SCHEMA_400 = "Tool 0 function has invalid 'parameters' schema"


class _UpstreamStatusError(RuntimeError):
    """An upstream rejection carrying its HTTP status, as adapters raise them."""

    def __init__(self, status_code: int, message: str = _TOOL_SCHEMA_400) -> None:
        super().__init__(message)
        self.status_code = status_code


class _TransportError(RuntimeError):
    """A connection failure: no HTTP status anywhere on it."""


def _window_refusal(window: int) -> _UpstreamStatusError:
    return _UpstreamStatusError(
        400,
        f"The input (605732 tokens) is longer than the model's context length ({window} tokens).",
    )


def _cfg(
    provider: str,
    *,
    context_length: int = 262144,
    base_url: str | None = None,
    endpoint_id: str | None = None,
) -> ModelConfig:
    return ModelConfig(
        id="m",
        name="m",
        provider=provider,
        base_url=base_url or f"http://{provider}",
        context_length=context_length,
        max_output_length=4096,
        endpoint_id=endpoint_id,
    )


class _Adapter(BaseAdapter):
    """Adapter that fails with ``error`` when given one and answers otherwise."""

    def __init__(self, config: ModelConfig, error: BaseException | None = None) -> None:
        super().__init__(config)
        self.error = error
        self.calls = 0

    async def chat_completion(self, messages: list[dict[str, Any]], **params) -> dict[str, Any]:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return {"choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}}]}

    async def stream_chat_completion(
        self, messages: list[dict[str, Any]], **params
    ) -> AsyncGenerator[str, None]:
        self.calls += 1
        if self.error is not None:
            raise self.error
        yield _CONTENT_CHUNK


def _router(primary: BaseAdapter, *backups: BaseAdapter) -> FixedRouter:
    """Route ``m`` over every adapter, with selection pinned to ``primary``.

    Backups are tried in the order given, with the 1% weight a fallback or a
    canary carries in production.
    """
    router = FixedRouter()
    router.register_route("m", [(primary, 1.0), *((backup, 0.01) for backup in backups)])
    router._select_adapter = lambda model_id, **kw: primary  # type: ignore[assignment]
    return router


async def _run(router: Any, *, stream: bool, model_id: str = "m") -> Any:
    if stream:
        return [chunk async for chunk in router.stream_chat_completion(model_id, _MESSAGES)]
    return await router.chat_completion(model_id, _MESSAGES)


_PATHS = pytest.mark.parametrize("stream", [False, True], ids=["chat", "stream"])


# ---------------------------------------------------------------------------
# The bound itself
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_two_request_refusals_end_the_walk():
    assert not request_refusals_end_fallback("m", [("a", _UpstreamStatusError(400))])
    assert request_refusals_end_fallback(
        "m", [("a", _UpstreamStatusError(400)), ("b", _UpstreamStatusError(422))]
    )


@pytest.mark.unit
@pytest.mark.parametrize("status", [400, 422])
def test_every_refusal_of_the_payload_counts(status):
    assert request_refusals_end_fallback(
        "m", [("a", _UpstreamStatusError(400)), ("b", _UpstreamStatusError(status))]
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "error",
    [
        _UpstreamStatusError(429),
        _UpstreamStatusError(403),
        _UpstreamStatusError(401),
        _UpstreamStatusError(503),
        _UpstreamStatusError(404, "The model `m` does not exist."),
        _UpstreamStatusError(413, "Request Entity Too Large"),
        _TransportError("Cannot connect"),
        _window_refusal(262144),
    ],
    ids=["429", "403", "401", "503", "404", "413", "transport", "context-window"],
)
def test_other_failures_do_not_count(error):
    assert not request_refusals_end_fallback(
        "m", [("a", _UpstreamStatusError(400)), ("b", error), ("c", error)]
    )


# ---------------------------------------------------------------------------
# FixedRouter's walk, streaming and not
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
@_PATHS
async def test_a_malformed_request_reaches_two_routes_not_every_route(stream):
    """The finding in one test: the 1% routes after the second refusal are never asked."""
    primary_error = _UpstreamStatusError(400, "primary says no")
    primary = _Adapter(_cfg("zai"), primary_error)
    fallback = _Adapter(_cfg("ollama"), _UpstreamStatusError(400, "ollama says no"))
    chutes = _Adapter(_cfg("chutes"), _UpstreamStatusError(400))
    canary = _Adapter(_cfg("staging"), _UpstreamStatusError(400))
    router = _router(primary, fallback, chutes, canary)

    with pytest.raises(_UpstreamStatusError) as exc_info:
        await _run(router, stream=stream)

    assert (primary.calls, fallback.calls, chutes.calls, canary.calls) == (1, 1, 0, 0)
    # Reported as before: the primary's own request-describing error.
    assert exc_info.value is primary_error
    assert [a["provider"] for a in exc_info.value._routing["failed_attempts"]] == [
        "zai",
        "ollama",
    ]


@pytest.mark.unit
@pytest.mark.asyncio
@_PATHS
async def test_one_refusal_still_falls_back(stream):
    """The reason a 400 is retried at all: another server may well accept it."""
    primary = _Adapter(_cfg("local-a"), _UpstreamStatusError(400))
    backup = _Adapter(_cfg("remote-b"))
    router = _router(primary, backup)

    result = await _run(router, stream=stream)

    assert backup.calls == 1
    if stream:
        assert _CONTENT_CHUNK in result
    else:
        assert result["_routing"]["endpoint_id"] == "remote-b"


@pytest.mark.unit
@pytest.mark.asyncio
@_PATHS
async def test_failures_that_say_nothing_about_the_request_walk_on(stream):
    primary = _Adapter(_cfg("a"), _UpstreamStatusError(400))
    unavailable = _Adapter(_cfg("b"), _UpstreamStatusError(503, "unavailable"))
    busy = _Adapter(_cfg("c"), _UpstreamStatusError(429, "rate limited"))
    dead = _Adapter(_cfg("d"), _TransportError("Cannot connect"))
    healthy = _Adapter(_cfg("e"))
    router = _router(primary, unavailable, busy, dead, healthy)

    await _run(router, stream=stream)

    assert healthy.calls == 1


@pytest.mark.unit
@pytest.mark.asyncio
@_PATHS
@pytest.mark.parametrize(
    ("status", "message"),
    [(404, "The model `m` does not exist."), (413, "Request Entity Too Large")],
    ids=["404", "413"],
)
async def test_route_specific_refusals_do_not_end_the_walk(stream, status, message):
    """Two routes that do not serve the model, or cap the body, before a healthy one.

    A 404 from a server that does not serve the model (an on-demand deployment
    not loaded, a ``provider_model_id`` set wrong for the route) and a 413 from
    one route's proxy describe the route, not the request, and are never charged
    to the breaker: counted as refusals they ended the walk before the healthy
    route, on every request.
    """
    first = _Adapter(_cfg("vllm-a"), _UpstreamStatusError(status, message))
    second = _Adapter(_cfg("vllm-b"), _UpstreamStatusError(status, message))
    healthy = _Adapter(_cfg("cloud"))
    router = _router(first, second, healthy)

    result = await _run(router, stream=stream)

    assert (first.calls, second.calls, healthy.calls) == (1, 1, 1)
    if stream:
        assert _CONTENT_CHUNK in result
    else:
        assert result["_routing"]["endpoint_id"] == "cloud"
        assert [a["provider"] for a in result["_routing"]["failed_attempts"]] == [
            "vllm-a",
            "vllm-b",
        ]


@pytest.mark.unit
@pytest.mark.asyncio
@_PATHS
async def test_a_route_specific_404_does_not_use_up_the_bound(stream):
    """A payload refusal on either side of a 404 still needs a second one to stop."""
    refused = _Adapter(_cfg("first"), _UpstreamStatusError(400, "first refusal"))
    missing = _Adapter(_cfg("missing"), _UpstreamStatusError(404, "model not found"))
    healthy = _Adapter(_cfg("healthy"))
    router = _router(refused, missing, healthy)

    await _run(router, stream=stream)

    assert healthy.calls == 1


@pytest.mark.unit
@pytest.mark.asyncio
@_PATHS
async def test_a_dead_primary_does_not_use_up_the_bound(stream):
    """Two refusals end the walk wherever they come; a transport failure is not one."""
    dead = _Adapter(_cfg("dead"), _TransportError("Cannot connect"))
    first = _Adapter(_cfg("first"), _UpstreamStatusError(400, "first refusal"))
    second = _Adapter(_cfg("second"), _UpstreamStatusError(422, "second refusal"))
    never = _Adapter(_cfg("never"))
    router = _router(dead, first, second, never)

    with pytest.raises(_UpstreamStatusError) as exc_info:
        await _run(router, stream=stream)

    assert (first.calls, second.calls, never.calls) == (1, 1, 0)
    # The first route that described the request is the one reported.
    assert str(exc_info.value) == "first refusal"
    assert exc_info.value._routing["provider"] == "first"


@pytest.mark.unit
@pytest.mark.asyncio
@_PATHS
async def test_context_window_refusals_keep_their_own_rule(stream):
    """Two windows too narrow for the prompt do not stop a wider one from being tried."""
    narrow = _Adapter(_cfg("narrow", context_length=131072), _window_refusal(131072))
    wider = _Adapter(_cfg("wider", context_length=262144), _window_refusal(262144))
    same = _Adapter(_cfg("same", context_length=262144))
    widest = _Adapter(_cfg("widest", context_length=1048576))
    router = _router(narrow, wider, same, widest)

    result = await _run(router, stream=stream)

    # ``same`` is skipped by the window rule, as before; ``widest`` still answers.
    assert (wider.calls, same.calls, widest.calls) == (1, 0, 1)
    if stream:
        assert _CONTENT_CHUNK in result
    else:
        assert result["_routing"]["endpoint_id"] == "widest"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stopping_is_logged_with_the_routes_that_refused(caplog):
    primary = _Adapter(_cfg("zai"), _UpstreamStatusError(400))
    fallback = _Adapter(_cfg("ollama"), _UpstreamStatusError(400))
    canary = _Adapter(_cfg("staging"), _UpstreamStatusError(400))
    router = _router(primary, fallback, canary)

    with (
        caplog.at_level(logging.INFO, logger="routing.routers"),
        pytest.raises(_UpstreamStatusError),
    ):
        await _run(router, stream=False)

    (record,) = [
        r for r in caplog.records if getattr(r, "event", None) == "request_refusal_fallback_stopped"
    ]
    assert record.model_id == "m"
    assert record.endpoint_ids == ["zai", "ollama"]


# ---------------------------------------------------------------------------
# The opt-in hybrid composition walks its plan under the same bound
# ---------------------------------------------------------------------------


def _hybrid(*adapters: BaseAdapter) -> HybridRouter:
    shared = FixedRouter()
    shared.register_route("m", [(adapter, 1.0) for adapter in adapters])
    registry = ModelRouterRegistry(
        models_config={"m": {"router": "fixed", "router_params": {"hybrid_composition": True}}},
        default_router_name="fixed",
        shared_fixed_router=shared,
    )
    registry.set_hybrid_router_factory(
        HybridFixedRouterFactory(registry=registry, health_registry=shared.endpoint_health_registry)
    )
    router = registry.get_router("m")
    assert isinstance(router, HybridRouter)
    return router


@pytest.mark.unit
@pytest.mark.asyncio
@_PATHS
async def test_the_hybrid_plan_stops_after_two_refusals(monkeypatch, stream):
    monkeypatch.setattr(PrefillLoadTracker, "select_index", lambda self, *a, **k: 0)
    local = _Adapter(
        _cfg("local", base_url="http://localhost:11434/v1", endpoint_id="m:local-11434"),
        _UpstreamStatusError(400),
    )
    zai = _Adapter(
        _cfg("zai", base_url="https://api.zai.example/v1", endpoint_id="m:zai-api"),
        _UpstreamStatusError(400),
    )
    staging = _Adapter(
        _cfg("staging", base_url="https://staging.example/v1", endpoint_id="m:staging-api"),
        _UpstreamStatusError(400),
    )
    router = _hybrid(local, zai, staging)

    with pytest.raises(_UpstreamStatusError):
        await _run(router, stream=stream)

    assert (local.calls, zai.calls, staging.calls) == (1, 1, 0)
