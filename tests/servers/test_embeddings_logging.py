"""Unit tests for /v1/embeddings request logging.

Verifies the embeddings endpoint logs to the shared ``api_logs`` store
(tagged ``request_type=embedding``) on both success and error paths, mirroring
the chat/completions logging contract that the dashboards read from.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from serving.servers.auth import verify_api_key
from serving.servers.concurrency import enforce_user_concurrency
from serving.servers.deps import (
    get_completions_logger,
    get_embedding_adapters,
    get_log_store,
    get_operational_store,
)
from serving.servers.routers import embeddings

# A prompt price of 1.0 USD / 1M tokens.
_PAID_PRICING = {"prompt": "1.0", "completion": "0"}
_FREE_PRICING = {"prompt": "0", "completion": "0"}


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


def _build_app(
    adapter: _FakeAdapter,
    logger: _CapturingLogger,
    op_store: _CapturingOpStore | None = None,
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
    return app


async def _post(app: FastAPI, payload: dict[str, Any]):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/v1/embeddings", json=payload)
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
