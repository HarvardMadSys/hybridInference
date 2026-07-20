"""Client (4xx) upstream errors must not trip the circuit breaker.

Regression for the cascade where one user's bad request (e.g. vLLM's 400
"max context length exceeded", surfaced by the local_deployment_proxy) opened
the circuit for every user of the model. A 4xx means the upstream is healthy and
correctly rejected the request, so availability/the breaker must be left alone.
Genuine faults (5xx, timeout/connection errors with no status, and the
overload-signalling 408/429) must still count.
"""

import asyncio
import threading
import types
from unittest.mock import AsyncMock, patch

import pytest

from routing.endpoint_health import (
    EndpointHealthRegistry,
    _CircuitState,
    _http_status_of,
    _is_client_error,
)
from routing.routewise.router import RouteWiseRouter


class _StatusError(Exception):
    """Stand-in for an adapter exception carrying an HTTP status (aiohttp uses .status)."""

    def __init__(self, status: int) -> None:
        super().__init__(f"HTTP {status}")
        self.status = status


def test_http_status_of_reads_common_attributes():
    assert _http_status_of(_StatusError(400)) == 400

    class _CodeError(Exception):
        status_code = 422

    assert _http_status_of(_CodeError()) == 422

    class _ResponseStatusError(Exception):
        class _Resp:
            status_code = 403

        response = _Resp()

    assert _http_status_of(_ResponseStatusError()) == 403
    assert _http_status_of(Exception("no status")) is None
    assert _http_status_of(TimeoutError()) is None


def test_client_errors_are_breaker_exempt(monkeypatch):
    monkeypatch.setenv("CIRCUIT_FAILURE_THRESHOLD", "999")
    monkeypatch.setenv("CIRCUIT_MIN_AVAILABILITY", "0.0")

    # Request/config errors: healthy upstream rejecting a bad request.
    for code in (400, 401, 403, 404, 413, 422):
        assert _is_client_error(_StatusError(code)) is True, code
        registry = EndpointHealthRegistry()
        endpoint_id = f"client-error-{code}"
        registry.record_success(endpoint_id)
        baseline = registry.snapshot()[endpoint_id]["availability"]

        registry.record_failure(
            endpoint_id,
            reason="stream_exception",
            exc=_StatusError(code),
        )

        status = registry.snapshot()[endpoint_id]
        assert status["circuit_state"] == _CircuitState.CLOSED, code
        assert status["availability"] == baseline, code


def test_overload_and_server_errors_still_count(monkeypatch):
    monkeypatch.setenv("CIRCUIT_FAILURE_THRESHOLD", "999")
    monkeypatch.setenv("CIRCUIT_MIN_AVAILABILITY", "0.0")

    # 408/429 signal overload; 5xx is a fault; no status is a connection/timeout fault.
    for code in (408, 429, 500, 502, 503):
        assert _is_client_error(_StatusError(code)) is False, code
        registry = EndpointHealthRegistry()
        endpoint_id = f"upstream-error-{code}"
        registry.record_success(endpoint_id)
        baseline = registry.snapshot()[endpoint_id]["availability"]

        registry.record_failure(
            endpoint_id,
            reason="stream_exception",
            exc=_StatusError(code),
        )

        status = registry.snapshot()[endpoint_id]
        assert status["circuit_state"] == _CircuitState.CLOSED, code
        assert status["availability"] < baseline, code
    assert _is_client_error(Exception("connection refused")) is False
    assert _is_client_error(TimeoutError()) is False


def test_repeated_4xx_never_opens_circuit(monkeypatch):
    monkeypatch.delenv("CIRCUIT_FAILURE_THRESHOLD", raising=False)
    monkeypatch.delenv("CIRCUIT_MIN_AVAILABILITY", raising=False)
    registry = EndpointHealthRegistry()
    endpoint_id = "qwen3.6-35b:local-8001"

    # Register the endpoint with a healthy baseline so we can prove the 4xx
    # failures leave its state (and availability) untouched, rather than the
    # endpoint simply never appearing.
    registry.record_success(endpoint_id)
    baseline = registry.snapshot()[endpoint_id]["availability"]

    # Far more 400s than the failure threshold — the breaker must stay closed
    # and availability must not drop.
    for _ in range(10):
        registry.record_failure(endpoint_id, reason="stream_exception", exc=_StatusError(400))

    status = registry.snapshot()[endpoint_id]
    assert status["circuit_state"] == _CircuitState.CLOSED
    assert status["availability"] == baseline


async def test_5xx_still_opens_circuit(monkeypatch):
    monkeypatch.delenv("CIRCUIT_FAILURE_THRESHOLD", raising=False)
    monkeypatch.delenv("CIRCUIT_MIN_AVAILABILITY", raising=False)
    registry = EndpointHealthRegistry()
    endpoint_id = "qwen3.6-35b:local-8001"

    with patch("routing.endpoint_health.alert_slack", new=AsyncMock()):
        for _ in range(5):
            registry.record_failure(endpoint_id, reason="stream_exception", exc=_StatusError(502))
        await asyncio.sleep(0)

    assert registry.snapshot()[endpoint_id]["circuit_state"] == _CircuitState.OPEN


# ---------------------------------------------------------------------------
# RouteWiseRouter must forward the caught exception to _on_failure so the
# registry's client-error guard actually sees it. This is the RouteWise sibling of
# the FixedRouter fix from #813: both chat_completion and stream_chat_completion
# used to call _on_failure without exc=, so a 4xx was misclassified as a fault.
# ---------------------------------------------------------------------------


class _FakeConfig:
    provider = "vllm"
    endpoint_id = "vllm:local:8001"
    base_url = "http://local:8001"


class _FakeAdapter:
    config = _FakeConfig()


class _CaptureRouteWise(RouteWiseRouter):
    """RouteWiseRouter stub that captures _on_failure kwargs on the except path.

    Bypasses the heavy __init__ so the test drives only the failure-recording
    code path; every attribute the two chat paths touch before re-raising is
    stubbed here.
    """

    def __init__(self) -> None:
        self.captured: list[dict] = []
        self._pending_decisions: dict = {}
        # Terminal-failure cleanup reclaims the prefix stash on the except path.
        self._route_commit_lock = threading.RLock()
        self._prefix_cache_pending: dict = {}
        # fallback_mode != "policy" so both paths re-raise right after recording.
        self.config = types.SimpleNamespace(fallback_mode="off")
        self._fake = _FakeAdapter()

    def _select_adapter(self, model_id, context=None, **kwargs):  # type: ignore[override]
        return self._fake

    async def _execute_adapter(self, adapter, model_id, messages, **params):  # type: ignore[override]
        raise _StatusError(400)

    async def _execute_stream_adapter(self, adapter, model_id, messages, **params):  # type: ignore[override]
        raise _StatusError(400)
        yield  # unreachable; marks this coroutine as an async generator

    def _on_failure(self, endpoint_id, *, reason="error", detail=None, exc=None):  # type: ignore[override]
        self.captured.append({"endpoint_id": endpoint_id, "reason": reason, "exc": exc})

    def _release_pending_primary_reservation(self, request_id):
        pass


def test_routewise_chat_forwards_exc_to_on_failure():
    router = _CaptureRouteWise()
    with pytest.raises(_StatusError):
        asyncio.run(router.chat_completion("m", []))
    assert router.captured, "chat_completion did not record a failure"
    exc = router.captured[-1]["exc"]
    # The bug: exc was omitted (None), so _is_client_error was never consulted
    # and the 400 tripped the breaker for everyone.
    assert exc is not None
    assert _is_client_error(exc) is True


def test_routewise_stream_forwards_exc_to_on_failure():
    router = _CaptureRouteWise()

    async def _drain() -> None:
        async for _ in router.stream_chat_completion("m", []):
            pass

    with pytest.raises(_StatusError):
        asyncio.run(_drain())
    assert router.captured, "stream_chat_completion did not record a failure"
    exc = router.captured[-1]["exc"]
    assert exc is not None
    assert _is_client_error(exc) is True
