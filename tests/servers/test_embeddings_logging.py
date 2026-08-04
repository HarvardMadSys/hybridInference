"""Unit tests for /v1/embeddings request logging.

Verifies the embeddings endpoint logs to the shared ``api_logs`` store
(tagged ``request_type=embedding``) on both success and error paths, mirroring
the chat/completions logging contract that the dashboards read from.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Any

import aiohttp
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from serving.config.runtime_settings import get_runtime_settings
from serving.observability.alert_rules import _is_failed_request
from serving.servers.auth import verify_api_key
from serving.servers.concurrency import enforce_user_concurrency
from serving.servers.deps import (
    get_completions_logger,
    get_embedding_adapters,
    get_log_store,
    get_operational_store,
)
from serving.servers.embedding_fallback import FallbackEmbeddingAdapter
from serving.servers.middleware.request_id import RequestIdMiddleware
from serving.servers.middleware.request_log import RequestLogMiddleware
from serving.servers.routers import embeddings

# A prompt price of 1.0 USD / 1M tokens.
_PAID_PRICING = {"prompt": "1.0", "completion": "0"}
_FREE_PRICING = {"prompt": "0", "completion": "0"}
_REQUEST_LOG_LOGGER = "serving.servers.middleware.request_log"


class _FakeAdapter:
    def __init__(
        self,
        *,
        response: dict[str, Any] | None = None,
        raises: Exception | None = None,
        pricing: dict[str, str] | None = None,
    ):
        self.config = SimpleNamespace(
            provider="fake-provider",
            pricing=_PAID_PRICING if pricing is None else pricing,
        )
        self._response = response
        self._raises = raises

    async def embeddings(self, input_data, **params):
        if self._raises is not None:
            raise self._raises
        return self._response


class _CapturingLogger:
    """Stand-in for ``CompletionsLogger`` that records scheduled payloads."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def schedule_log(self, request_id: str, log_data: dict[str, Any]) -> None:
        self.calls.append((request_id, log_data))


class _CapturingOpStore:
    """Records ``increment_user_cost`` calls from the embeddings handler."""

    def __init__(self) -> None:
        self.increments: list[tuple[str, float]] = []

    async def increment_user_cost(self, user_id: str, cost_usd: float, *, day=None) -> None:
        self.increments.append((user_id, cost_usd))


class _FakeRuntimeSettings:
    """Minimal RuntimeSettings stub returning a fixed log_synthetic_probes."""

    def __init__(self, log_synthetic_probes: bool = False) -> None:
        self._log_synthetic_probes = log_synthetic_probes

    async def get_bool(self, key: str) -> bool:
        return self._log_synthetic_probes if key == "log_synthetic_probes" else False


def _build_app(
    adapter: _FakeAdapter,
    logger: _CapturingLogger,
    op_store: _CapturingOpStore | None = None,
    *,
    log_synthetic_probes: bool = False,
) -> FastAPI:
    app = FastAPI()
    app.include_router(embeddings.router)

    app.dependency_overrides[verify_api_key] = lambda: {
        "user_id": "user-emb",
        "authenticated": True,
    }
    app.dependency_overrides[enforce_user_concurrency] = lambda: None
    app.dependency_overrides[get_embedding_adapters] = lambda: {"emb-model": adapter}
    app.dependency_overrides[get_log_store] = lambda: object()  # truthy → logging enabled
    app.dependency_overrides[get_completions_logger] = lambda: logger
    app.dependency_overrides[get_operational_store] = lambda: op_store
    app.dependency_overrides[get_runtime_settings] = lambda: _FakeRuntimeSettings(
        log_synthetic_probes
    )
    return app


async def _post(app: FastAPI, payload: dict[str, Any], headers: dict[str, str] | None = None):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/v1/embeddings", json=payload, headers=headers)
    # Let the fire-and-forget cost-increment background task settle.
    await asyncio.sleep(0.05)
    return resp


@pytest.mark.asyncio
async def test_embeddings_success_is_logged():
    response = {
        "object": "list",
        "model": "emb-model",
        "data": [{"object": "embedding", "index": 0, "embedding": [0.1, 0.2, 0.3]}],
        "usage": {"prompt_tokens": 5, "total_tokens": 5},
    }
    adapter = _FakeAdapter(response=response)
    logger = _CapturingLogger()
    app = _build_app(adapter, logger)

    resp = await _post(app, {"model": "emb-model", "input": "hello"})
    assert resp.status_code == 200

    assert len(logger.calls) == 1
    request_id, log_data = logger.calls[0]
    assert request_id.startswith("emb_")
    assert log_data["model_id"] == "emb-model"
    assert log_data["provider"] == "fake-provider"
    assert log_data["status_code"] == 200
    assert log_data["usage"] == {"prompt_tokens": 5, "total_tokens": 5}
    assert log_data["metadata"]["request_type"] == "embedding"
    assert log_data["metadata"]["user_id"] == "user-emb"
    # Full vectors must not be persisted — only a compact summary.
    assert log_data["response"]["data_count"] == 1
    assert log_data["response"]["dimensions"] == 3
    assert "data" not in log_data["response"]  # raw vectors omitted


@pytest.mark.parametrize(
    "response",
    [
        None,
        ["unexpected", "shape"],
        "not-a-dict",
        {"data": "not-a-list"},
        {"data": ["not-a-dict-item"]},
        {},
    ],
)
def test_response_summary_is_defensive(response):
    """``_response_summary`` must never raise on unexpected response shapes.

    It runs on the success path, so a crash here would turn a successful
    embedding into a 500 for the client.
    """
    summary = embeddings._response_summary(response, "emb-model")
    # The invariant is "never raises"; dimensions can't be inferred from any of
    # these malformed shapes, so it must stay None.
    assert isinstance(summary["data_count"], int)
    assert summary["dimensions"] is None


@pytest.mark.asyncio
async def test_embeddings_unknown_model_is_logged():
    adapter = _FakeAdapter(response={})
    logger = _CapturingLogger()
    app = _build_app(adapter, logger)

    resp = await _post(app, {"model": "does-not-exist", "input": "hello"})
    assert resp.status_code == 404

    assert len(logger.calls) == 1
    _, log_data = logger.calls[0]
    assert log_data["status_code"] == 404
    assert log_data["metadata"]["request_type"] == "embedding"
    assert log_data["error"]


@pytest.mark.asyncio
async def test_embeddings_adapter_error_is_logged():
    adapter = _FakeAdapter(raises=RuntimeError("boom"))
    logger = _CapturingLogger()
    app = _build_app(adapter, logger)

    resp = await _post(app, {"model": "emb-model", "input": "hello"})
    assert resp.status_code == 500

    assert len(logger.calls) == 1
    _, log_data = logger.calls[0]
    assert log_data["status_code"] == 500
    assert log_data["metadata"]["request_type"] == "embedding"
    assert "boom" in log_data["error"]


def _ok_response() -> dict[str, Any]:
    return {
        "object": "list",
        "model": "emb-model",
        "data": [{"object": "embedding", "index": 0, "embedding": [0.1, 0.2]}],
        "usage": {"prompt_tokens": 1000, "total_tokens": 1000},
    }


@pytest.mark.asyncio
async def test_paid_embedding_increments_quota_counter():
    """A paid embedding model must bump the daily quota cost counter."""
    adapter = _FakeAdapter(response=_ok_response(), pricing=_PAID_PRICING)
    op_store = _CapturingOpStore()
    app = _build_app(adapter, _CapturingLogger(), op_store)

    resp = await _post(app, {"model": "emb-model", "input": "hello"})
    assert resp.status_code == 200

    assert len(op_store.increments) == 1
    user_id, cost = op_store.increments[0]
    assert user_id == "user-emb"
    # 1000 prompt tokens * $1.0 / 1M = $0.001
    assert cost == pytest.approx(0.001)


@pytest.mark.asyncio
async def test_free_embedding_does_not_increment_quota_counter():
    """A free (zero-priced) embedding model must not touch the cost counter."""
    adapter = _FakeAdapter(response=_ok_response(), pricing=_FREE_PRICING)
    op_store = _CapturingOpStore()
    app = _build_app(adapter, _CapturingLogger(), op_store)

    resp = await _post(app, {"model": "emb-model", "input": "hello"})
    assert resp.status_code == 200
    assert op_store.increments == []


@pytest.mark.asyncio
async def test_embedding_error_does_not_increment_quota_counter():
    """Failed embeddings must not be billed against the quota."""
    adapter = _FakeAdapter(raises=RuntimeError("boom"), pricing=_PAID_PRICING)
    op_store = _CapturingOpStore()
    app = _build_app(adapter, _CapturingLogger(), op_store)

    resp = await _post(app, {"model": "emb-model", "input": "hello"})
    assert resp.status_code == 500
    assert op_store.increments == []


@pytest.mark.asyncio
async def test_malformed_response_logged_as_error_not_billable_200():
    """A malformed (but non-raising) upstream response must not be logged as a
    billable 200 nor increment the quota — the client receives a 500.
    """
    # Missing the required ``usage`` field → fails EmbeddingResponse validation.
    adapter = _FakeAdapter(
        response={"object": "list", "model": "emb-model", "data": []},
        pricing=_PAID_PRICING,
    )
    logger = _CapturingLogger()
    op_store = _CapturingOpStore()
    app = _build_app(adapter, logger, op_store)

    resp = await _post(app, {"model": "emb-model", "input": "hello"})
    assert resp.status_code == 500

    # Logged exactly once, as a 500 — never a 200.
    assert len(logger.calls) == 1
    _, log_data = logger.calls[0]
    assert log_data["status_code"] == 500
    assert "invalid embedding response" in log_data["error"]
    # And the paid quota counter is untouched.
    assert op_store.increments == []


@pytest.mark.asyncio
async def test_coercible_string_usage_logged_as_int():
    """Coercible upstream usage (e.g. ``"prompt_tokens": "1000"``) must be
    logged as validated ints so the integer-column insert can't fail while the
    quota increment silently succeeds.
    """
    adapter = _FakeAdapter(
        response={
            "object": "list",
            "model": "emb-model",
            "data": [{"object": "embedding", "index": 0, "embedding": [0.1, 0.2]}],
            "usage": {"prompt_tokens": "1000", "total_tokens": "1000"},
        },
        pricing=_PAID_PRICING,
    )
    logger = _CapturingLogger()
    op_store = _CapturingOpStore()
    app = _build_app(adapter, logger, op_store)

    resp = await _post(app, {"model": "emb-model", "input": "hello"})
    assert resp.status_code == 200

    _, log_data = logger.calls[0]
    assert log_data["usage"]["prompt_tokens"] == 1000
    assert isinstance(log_data["usage"]["prompt_tokens"], int)
    # Billing still happens, on the coerced value: 1000 * $1.0 / 1M = $0.001.
    assert op_store.increments == [("user-emb", pytest.approx(0.001))]


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_count", [-5, 2147483648])
async def test_out_of_range_usage_not_billable_200(bad_count):
    """Token counts outside the api_logs INTEGER range (negative or above
    INT4_MAX) must be rejected before recording success.
    """
    adapter = _FakeAdapter(
        response={
            "object": "list",
            "model": "emb-model",
            "data": [{"object": "embedding", "index": 0, "embedding": [0.1, 0.2]}],
            "usage": {"prompt_tokens": bad_count, "total_tokens": bad_count},
        },
        pricing=_PAID_PRICING,
    )
    logger = _CapturingLogger()
    op_store = _CapturingOpStore()
    app = _build_app(adapter, logger, op_store)

    resp = await _post(app, {"model": "emb-model", "input": "hello"})
    assert resp.status_code == 500
    assert logger.calls[0][1]["status_code"] == 500
    assert op_store.increments == []


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_value", [float("nan"), float("inf"), float("-inf")])
async def test_non_finite_embedding_not_billable_200(bad_value):
    """Non-finite floats (which Starlette can't serialize) must be rejected
    before recording a billable 200 / incrementing quota, not after.
    """
    adapter = _FakeAdapter(
        response={
            "object": "list",
            "model": "emb-model",
            "data": [{"object": "embedding", "index": 0, "embedding": [bad_value, 0.2]}],
            "usage": {"prompt_tokens": 1000, "total_tokens": 1000},
        },
        pricing=_PAID_PRICING,
    )
    logger = _CapturingLogger()
    op_store = _CapturingOpStore()
    app = _build_app(adapter, logger, op_store)

    resp = await _post(app, {"model": "emb-model", "input": "hello"})
    assert resp.status_code == 500

    assert len(logger.calls) == 1
    _, log_data = logger.calls[0]
    assert log_data["status_code"] == 500
    assert op_store.increments == []


_OK_RESPONSE = {
    "object": "list",
    "model": "emb-model",
    "data": [{"object": "embedding", "index": 0, "embedding": [0.1, 0.2, 0.3]}],
    "usage": {"prompt_tokens": 5, "total_tokens": 5},
}
_PROBE_HEADERS = {"x-probe": "synthetic"}


@pytest.mark.asyncio
async def test_synthetic_probe_not_logged_but_still_billed():
    """Probe traffic is suppressed from api_logs unless opted in, but billing
    is NOT skipped: the x-probe header is caller-controlled, so exempting it
    from quota would let any client bypass enforcement.
    """
    adapter = _FakeAdapter(response=_OK_RESPONSE, pricing=_PAID_PRICING)
    logger = _CapturingLogger()
    op_store = _CapturingOpStore()
    app = _build_app(adapter, logger, op_store, log_synthetic_probes=False)

    resp = await _post(app, {"model": "emb-model", "input": "hi"}, headers=_PROBE_HEADERS)
    assert resp.status_code == 200
    # Not logged...
    assert logger.calls == []
    # ...but still billed (5 prompt tokens * $1.0 / 1M).
    assert op_store.increments == [("user-emb", pytest.approx(5 * 1.0 / 1_000_000))]


@pytest.mark.asyncio
async def test_synthetic_probe_logged_when_enabled_and_billed():
    """When log_synthetic_probes is on, the probe is logged and tagged, and is
    billed like any other request.
    """
    adapter = _FakeAdapter(response=_OK_RESPONSE, pricing=_PAID_PRICING)
    logger = _CapturingLogger()
    op_store = _CapturingOpStore()
    app = _build_app(adapter, logger, op_store, log_synthetic_probes=True)

    resp = await _post(app, {"model": "emb-model", "input": "hi"}, headers=_PROBE_HEADERS)
    assert resp.status_code == 200

    assert len(logger.calls) == 1
    _, log_data = logger.calls[0]
    assert log_data["metadata"]["synthetic_probe"] is True
    assert op_store.increments == [("user-emb", pytest.approx(5 * 1.0 / 1_000_000))]


@pytest.mark.asyncio
async def test_synthetic_probe_unknown_model_not_logged_by_default():
    """A suppressed probe to an unknown model is a clean 404 with no log row."""
    adapter = _FakeAdapter(response=_OK_RESPONSE)
    logger = _CapturingLogger()
    app = _build_app(adapter, logger, log_synthetic_probes=False)

    resp = await _post(app, {"model": "nope", "input": "hi"}, headers=_PROBE_HEADERS)
    assert resp.status_code == 404
    assert logger.calls == []


@pytest.mark.asyncio
async def test_non_probe_request_still_logged():
    """A normal (non-probe) request is unaffected by the suppression logic."""
    adapter = _FakeAdapter(response=_OK_RESPONSE)
    logger = _CapturingLogger()
    app = _build_app(adapter, logger, log_synthetic_probes=False)

    resp = await _post(app, {"model": "emb-model", "input": "hi"})
    assert resp.status_code == 200
    assert len(logger.calls) == 1
    assert "synthetic_probe" not in logger.calls[0][1]["metadata"]


@pytest.mark.asyncio
async def test_embeddings_falls_back_and_logs_serving_provider():
    """When the primary embedding backend fails, the request is served by the
    fallback (e.g. the staging canary) and the log row is attributed to the
    backend that actually served — not the primary.
    """
    primary = _FakeAdapter(raises=RuntimeError("primary down"), pricing=_FREE_PRICING)
    primary.config.provider = "sglang"
    secondary = _FakeAdapter(response=_ok_response(), pricing=_FREE_PRICING)
    secondary.config.provider = "staging"
    wrapper = FallbackEmbeddingAdapter([primary, secondary])

    logger = _CapturingLogger()
    app = _build_app(wrapper, logger)

    resp = await _post(app, {"model": "emb-model", "input": "hello"})
    assert resp.status_code == 200

    assert len(logger.calls) == 1
    _, log_data = logger.calls[0]
    assert log_data["status_code"] == 200
    # Attributed to the backend that actually served (the fallback), not primary.
    assert log_data["provider"] == "staging"


@pytest.mark.asyncio
async def test_embeddings_all_backends_fail_is_500():
    """When every backend in the fallback chain fails, the endpoint returns 500
    and logs the last error.
    """
    primary = _FakeAdapter(raises=RuntimeError("primary down"))
    primary.config.provider = "sglang"
    secondary = _FakeAdapter(raises=RuntimeError("staging down"))
    secondary.config.provider = "staging"
    wrapper = FallbackEmbeddingAdapter([primary, secondary])

    logger = _CapturingLogger()
    app = _build_app(wrapper, logger)

    resp = await _post(app, {"model": "emb-model", "input": "hello"})
    assert resp.status_code == 500

    assert len(logger.calls) == 1
    _, log_data = logger.calls[0]
    assert log_data["status_code"] == 500
    # The last backend's error is what surfaces.
    assert "staging down" in log_data["error"]


# ---------------------------------------------------------------------------
# Upstream attribution on the error path
#
# The DB log row takes ``provider`` as a value, but RequestLogMiddleware and the
# in-process alert rules can only read it from req_ctx. /v1/embeddings relays the
# upstream status verbatim, so without a req_ctx publish an upstream 401 — the
# gateway's own credential refused by the embedding backend — reaches both
# consumers as a bare 401 and is filed as routine client auth churn. The local
# embedding proxy shares its API key with the chat proxies, so this is the same
# credential outage the chat path alerts on.
# ---------------------------------------------------------------------------


def _build_logging_app(adapter: Any, logger: _CapturingLogger) -> FastAPI:
    """``_build_app`` plus the real middleware pair, in the order ``app.py`` uses.

    ``add_middleware`` prepends, so ``RequestIdMiddleware`` added last is
    outermost and seeds the context before ``RequestLogMiddleware`` observes it.
    """
    app = _build_app(adapter, logger)
    app.add_middleware(RequestLogMiddleware)
    app.add_middleware(RequestIdMiddleware)
    return app


def _window_item(record: logging.LogRecord) -> dict[str, Any]:
    """Project a request-log record exactly as ``FailedRequestRateRule`` does."""
    return {
        "status": int(record.status_code),
        "provider": getattr(record, "provider", None),
        "path": getattr(record, "path", None),
        "client_error_kind": getattr(record, "client_error_kind", None),
    }


def _upstream_error(status: int) -> aiohttp.ClientResponseError:
    """An ``aiohttp`` status error shaped like one a real embedding call raises."""
    url = "http://127.0.0.1:8001/v1/embeddings"
    request_info = SimpleNamespace(real_url=url, method="POST", url=url, headers={})
    return aiohttp.ClientResponseError(
        request_info,  # type: ignore[arg-type]
        (),
        status=status,
        message="Unauthorized",
    )


@pytest.mark.asyncio
async def test_embeddings_upstream_401_is_attributed_logged_and_counted(caplog):
    """An upstream 401 on /v1/embeddings must be loud, attributed, and counted.

    Regression for the class of outage this alerting exists for: the local
    embedding proxy rejects the gateway's configured key on every request, the
    endpoint relays the 401, and — with no upstream attribution in req_ctx — the
    request log demotes it to DEBUG and the failed-request rule skips it, so a
    total embedding outage produces no signal at all.
    """
    adapter = _FakeAdapter(raises=_upstream_error(401))
    adapter.config.provider = "local-embed"
    log_stub = _CapturingLogger()
    app = _build_logging_app(adapter, log_stub)

    with caplog.at_level(logging.DEBUG, logger=_REQUEST_LOG_LOGGER):
        resp = await _post(app, {"model": "emb-model", "input": "hello"})

    # The client still sees the upstream status, as before.
    assert resp.status_code == 401

    records = [r for r in caplog.records if r.getMessage() == "http_request"]
    assert records, "expected an http_request record for the failed embedding"
    record = records[-1]

    # Attributed...
    assert record.provider == "local-embed"
    # ...logged at INFO rather than demoted into the auth-challenge bucket...
    assert record.levelno == logging.INFO
    assert record.status_code == 401
    # ...and counted by the failed-request rule, from the same record fields it
    # reads in production.
    assert _is_failed_request(_window_item(record)) is True

    # The DB log row keeps the status and provider it always had.
    assert len(log_stub.calls) == 1
    _, log_data = log_stub.calls[0]
    assert log_data["status_code"] == 401
    assert log_data["provider"] == "local-embed"


@pytest.mark.asyncio
async def test_embeddings_gateway_404_stays_unattributed(caplog):
    """A gateway-issued rejection must not gain a provider label.

    The consumers read a present ``provider`` as "an upstream answered us", so
    attributing a request that never reached one would both misattribute the
    failure and erode that distinction.
    """
    app = _build_logging_app(_FakeAdapter(response=_OK_RESPONSE), _CapturingLogger())

    with caplog.at_level(logging.DEBUG, logger=_REQUEST_LOG_LOGGER):
        resp = await _post(app, {"model": "does-not-exist", "input": "hello"})

    assert resp.status_code == 404
    records = [r for r in caplog.records if r.getMessage() == "http_request"]
    assert records
    assert records[-1].provider is None


@pytest.mark.asyncio
async def test_embeddings_upstream_500_is_attributed(caplog):
    """The non-``ClientResponseError`` path is post-dispatch and attributable too.

    A 500 counts as a failure either way, so what the label buys here is naming
    the offending backend in the alert's provider breakdown instead of "n/a".
    """
    adapter = _FakeAdapter(raises=RuntimeError("boom"))
    adapter.config.provider = "local-embed"
    app = _build_logging_app(adapter, _CapturingLogger())

    with caplog.at_level(logging.DEBUG, logger=_REQUEST_LOG_LOGGER):
        resp = await _post(app, {"model": "emb-model", "input": "hello"})

    assert resp.status_code == 500
    records = [r for r in caplog.records if r.getMessage() == "http_request"]
    assert records
    assert records[-1].provider == "local-embed"
