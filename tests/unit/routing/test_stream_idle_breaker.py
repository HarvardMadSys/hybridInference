"""A stalled upstream must cost the endpoint its health, not just the request.

Detecting the stall is only half the fix. The reason it was worth detecting at
all is that a wedged sglang replica kept being *selected* for the whole 4-7
minutes it was dead -- every new stream committed to it became another user
getting a partial answer and a generic error. So the idle timeout has to reach
the circuit breaker, and it has to do that without being mistaken for a client
error (which is breaker-exempt) or for a client disconnect (which must never be
charged to an upstream at all).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import pytest

from routing.endpoint_health import EndpointHealthRegistry, _CircuitState, _is_client_error
from routing.routers import AllCircuitsOpenError, FixedRouter
from serving.adapters.base import BaseAdapter, ModelConfig
from serving.exceptions import UpstreamStreamIdleError

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

pytestmark = pytest.mark.unit


def _cfg() -> ModelConfig:
    return ModelConfig(
        id="deepseek-v4-flash",
        name="DeepSeek-V4-Flash",
        provider="sglang",
        base_url="http://h200a.local:8003/v1",
        context_length=1048576,
        max_output_length=393216,
    )


class _StallingAdapter(BaseAdapter):
    """Delivers a token, then goes silent long enough for the detector to fire."""

    async def chat_completion(self, messages: list[dict[str, Any]], **params) -> dict[str, Any]:
        raise NotImplementedError

    async def stream_chat_completion(
        self, messages: list[dict[str, Any]], **params
    ) -> AsyncGenerator[str, None]:
        yield self.format_stream_chunk(model=self.config.id, content="par")
        raise UpstreamStreamIdleError(180.0, endpoint_id="sglang:h200a.local:8003", frames=124)


class _DisconnectingAdapter(BaseAdapter):
    """Stands in for the client hanging up mid-stream."""

    async def chat_completion(self, messages: list[dict[str, Any]], **params) -> dict[str, Any]:
        raise NotImplementedError

    async def stream_chat_completion(
        self, messages: list[dict[str, Any]], **params
    ) -> AsyncGenerator[str, None]:
        yield self.format_stream_chunk(model=self.config.id, content="par")
        raise asyncio.CancelledError


def test_idle_timeout_is_not_breaker_exempt() -> None:
    """It carries no HTTP status, so the client-error exemption cannot swallow it."""
    exc = UpstreamStreamIdleError(180.0, endpoint_id="sglang:h200a.local:8003", frames=124)
    assert _is_client_error(exc) is False


def test_idle_timeout_marks_the_endpoint_unhealthy_and_stops_dispatch(monkeypatch) -> None:
    """A stall costs the endpoint its availability, opens its circuit, and --
    the part users actually feel -- stops the next stream being sent there."""
    monkeypatch.setenv("CIRCUIT_FAILURE_THRESHOLD", "1")
    monkeypatch.setenv("CIRCUIT_COOLDOWN_SECONDS", "300")

    registry = EndpointHealthRegistry()
    router = FixedRouter(health_registry=registry)
    adapter = _StallingAdapter(_cfg())
    router.register_route("deepseek-v4-flash", [(adapter, 1.0)])

    async def _drive() -> None:
        with pytest.raises(UpstreamStreamIdleError):
            async for _chunk in router.stream_chat_completion(
                "deepseek-v4-flash", [{"role": "user", "content": "hi"}]
            ):
                pass

        # The next caller is turned away here rather than being committed to a
        # replica that is already dead and then handed a truncated answer.
        with pytest.raises(AllCircuitsOpenError):
            async for _chunk in router.stream_chat_completion(
                "deepseek-v4-flash", [{"role": "user", "content": "hi"}]
            ):
                pass

    asyncio.run(_drive())

    endpoint_id = next(iter(registry.snapshot()))
    snapshot = registry.snapshot()[endpoint_id]
    assert snapshot["availability"] < 1.0
    assert snapshot["circuit_state"] == _CircuitState.OPEN


def test_client_disconnect_leaves_the_endpoint_alone(monkeypatch) -> None:
    """The companion invariant: a hang-up is a BaseException and is not charged.

    Same mid-stream position, same route -- only the exception class differs,
    which is what keeps the detector from being a new way for flaky client
    networks to open a circuit.
    """
    monkeypatch.setenv("CIRCUIT_FAILURE_THRESHOLD", "2")

    registry = EndpointHealthRegistry()
    router = FixedRouter(health_registry=registry)
    adapter = _DisconnectingAdapter(_cfg())
    router.register_route("deepseek-v4-flash", [(adapter, 1.0)])

    failures: list[str] = []
    monkeypatch.setattr(registry, "record_failure", lambda eid, **kw: failures.append(eid))

    async def _drive() -> None:
        with pytest.raises(asyncio.CancelledError):
            async for _chunk in router.stream_chat_completion(
                "deepseek-v4-flash", [{"role": "user", "content": "hi"}]
            ):
                pass

    asyncio.run(_drive())

    assert failures == []
