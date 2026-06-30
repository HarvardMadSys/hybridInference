"""Client (4xx) upstream errors must not trip the circuit breaker.

Regression for the cascade where one user's bad request (e.g. vLLM's 400
"max context length exceeded", surfaced by the local_deployment_proxy) opened
the circuit for every user of the model. A 4xx means the upstream is healthy and
correctly rejected the request, so availability/the breaker must be left alone.
Genuine faults (5xx, timeout/connection errors with no status, and the
overload-signalling 408/429) must still count.
"""

from routing.routers import (
    BaseRouter,
    _CircuitState,
    _http_status_of,
    _is_client_error,
)


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


def test_client_errors_are_breaker_exempt():
    # Request/config errors: healthy upstream rejecting a bad request.
    for code in (400, 401, 403, 404, 413, 422):
        assert _is_client_error(_StatusError(code)) is True, code


def test_overload_and_server_errors_still_count():
    # 408/429 signal overload; 5xx is a fault; no status is a connection/timeout fault.
    for code in (408, 429, 500, 502, 503):
        assert _is_client_error(_StatusError(code)) is False, code
    assert _is_client_error(Exception("connection refused")) is False
    assert _is_client_error(TimeoutError()) is False


def test_repeated_4xx_never_opens_circuit(monkeypatch):
    monkeypatch.delenv("CIRCUIT_FAILURE_THRESHOLD", raising=False)
    monkeypatch.delenv("CIRCUIT_MIN_AVAILABILITY", raising=False)
    router = BaseRouter()
    endpoint_id = "qwen3.6-35b:local-8001"

    # Register the endpoint with a healthy baseline so we can prove the 4xx
    # failures leave its state (and availability) untouched, rather than the
    # endpoint simply never appearing.
    router._on_success(endpoint_id)
    baseline = router.get_provider_status()[endpoint_id]["availability"]

    # Far more 400s than the failure threshold — the breaker must stay closed
    # and availability must not drop.
    for _ in range(10):
        router._on_failure(endpoint_id, reason="stream_exception", exc=_StatusError(400))

    status = router.get_provider_status()[endpoint_id]
    assert status["circuit_state"] == _CircuitState.CLOSED
    assert status["availability"] == baseline


def test_5xx_still_opens_circuit(monkeypatch):
    monkeypatch.delenv("CIRCUIT_FAILURE_THRESHOLD", raising=False)
    monkeypatch.delenv("CIRCUIT_MIN_AVAILABILITY", raising=False)
    router = BaseRouter()
    endpoint_id = "qwen3.6-35b:local-8001"

    for _ in range(5):
        router._on_failure(endpoint_id, reason="stream_exception", exc=_StatusError(502))

    assert router.get_provider_status()[endpoint_id]["circuit_state"] == _CircuitState.OPEN
