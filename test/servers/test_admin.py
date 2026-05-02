from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

import pytest
from fastapi import FastAPI, status
from httpx import ASGITransport, AsyncClient

from routing.executor import RouteExecutor
from serving.adapters.base import BaseAdapter, ModelConfig
from serving.servers.deps import AppServices
from serving.servers.routers import admin, models

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


class _Adapter(BaseAdapter):
    async def chat_completion(self, messages: list[dict[str, Any]], **params) -> dict[str, Any]:
        return self.format_response(content="ok", model=self.config.id)

    async def stream_chat_completion(
        self, messages: list[dict[str, Any]], **params
    ):  # pragma: no cover
        yield self.format_stream_chunk(model=self.config.id, content="ok")


def _cfg(model_id: str) -> ModelConfig:
    return ModelConfig(id=model_id, name=model_id, provider="test", base_url="http://test")


@pytest.fixture
async def admin_app(mock_db_logger) -> FastAPI:
    router = RouteExecutor()
    router.register_route("canonical-model", [(_Adapter(_cfg("canonical-model")), 1.0)])
    app = FastAPI(title="Admin App")
    app.state.services = AppServices(router=router, db_logger=mock_db_logger)  # type: ignore[attr-defined]
    app.include_router(models.router)
    app.include_router(admin.router)
    return app


@pytest.fixture
async def admin_client(admin_app: FastAPI) -> AsyncGenerator[AsyncClient, None]:
    transport = ASGITransport(app=admin_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.mark.asyncio
async def test_admin_stats_without_db_logger():
    router = RouteExecutor()
    services = AppServices(router=router, db_logger=None)
    app = FastAPI()
    app.state.services = services  # type: ignore[attr-defined]
    app.include_router(admin.router)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/admin/stats")
        assert resp.status_code == status.HTTP_200_OK
        assert resp.json().get("error") == "Database logging not configured"


# ---------------------------------------------------------------------------
# /admin/performance-metrics — DB-backed
# ---------------------------------------------------------------------------


async def _insert_api_log(
    pool: Any,
    *,
    request_id: str,
    timestamp: datetime,
    model_id: str = "perf-test-model",
    provider: str = "perf-test",
    status_code: int = 200,
    stream: bool = True,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    ttft_ms: int | None = None,
    latency_ms: int | None = None,
) -> None:
    """Insert a single api_logs row (test helper)."""
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO api_logs (
                request_id, timestamp, model_id, provider, status_code,
                stream, prompt_tokens, completion_tokens, ttft_ms, latency_ms
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
            """,
            request_id,
            timestamp,
            model_id,
            provider,
            status_code,
            stream,
            prompt_tokens,
            completion_tokens,
            ttft_ms,
            latency_ms,
        )


@pytest.mark.asyncio
async def test_admin_performance_metrics_distributions(auth_client, require_db):
    """Insert varied api_logs rows and verify the distribution endpoint output.

    Asserts:
    - 200 OK with admin token
    - response shape: generated_at + non-empty windows
    - each window has 4 metric distributions
    - percentile ordering: p50 <= p95 <= p99 (when count > 0)
    - histogram bucket counts sum to the metric's count
    """
    pool = require_db.pool
    now = datetime.now(timezone.utc)

    # Tag every test row with a unique model_id so concurrent tests don't
    # pollute each other.  Assertions rely on per-window invariants
    # (histogram-sums-to-count, percentile ordering) plus `>=` lower bounds
    # on the 5m window, so they tolerate other rows already in the DB.
    tag = f"perf-test-{uuid.uuid4().hex[:10]}"

    # Clean any prior leftovers (paranoia: should be unique already)
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM api_logs WHERE model_id = $1", tag)

    # 10 streaming successful rows with varied numbers, 1 errored row
    # (should be excluded by status_code filter), 1 non-stream row
    # (excluded from ttft/tbt), and 1 buggy row with latency < ttft
    # (tbt should clamp to NULL).
    rows: list[dict[str, Any]] = []
    for i in range(10):
        rows.append(
            {
                "request_id": f"{tag}-ok-{i}",
                "timestamp": now - timedelta(seconds=30 + i),
                "status_code": 200,
                "stream": True,
                "prompt_tokens": 10 + i * 10,  # 10..100
                "completion_tokens": 5 + i * 5,  # 5..50
                "ttft_ms": 100 + i * 50,  # 100..550
                "latency_ms": 1000 + i * 100,  # 1000..1900
            }
        )
    # error row — must be excluded
    rows.append(
        {
            "request_id": f"{tag}-err",
            "timestamp": now - timedelta(seconds=15),
            "status_code": 500,
            "stream": True,
            "prompt_tokens": 99999,
            "completion_tokens": 99999,
            "ttft_ms": 99999,
            "latency_ms": 99999,
        }
    )
    # non-stream row — excluded from ttft/tbt
    rows.append(
        {
            "request_id": f"{tag}-nostream",
            "timestamp": now - timedelta(seconds=20),
            "status_code": 200,
            "stream": False,
            "prompt_tokens": 7,
            "completion_tokens": 3,
            "ttft_ms": None,
            "latency_ms": 200,
        }
    )
    # buggy provider with latency < ttft — tbt should clamp to NULL
    rows.append(
        {
            "request_id": f"{tag}-buggy",
            "timestamp": now - timedelta(seconds=25),
            "status_code": 200,
            "stream": True,
            "prompt_tokens": 12,
            "completion_tokens": 5,
            "ttft_ms": 800,
            "latency_ms": 500,
        }
    )

    for r in rows:
        await _insert_api_log(pool, model_id=tag, **r)

    try:
        resp = await auth_client.get(
            "/admin/performance-metrics",
            headers={"Authorization": "Bearer test-admin-token"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "generated_at" in body
        assert isinstance(body["windows"], list)
        assert len(body["windows"]) > 0

        # The 5m window will see only our recent rows + whatever else is in
        # the test DB.  Don't depend on absolute counts; rely on per-window
        # invariants instead.
        for window in body["windows"]:
            assert {"key", "label", "window_minutes"}.issubset(window.keys())
            for metric_key in ("prompt_tokens", "completion_tokens", "ttft_ms", "throughput_tps"):
                assert metric_key in window, f"missing {metric_key} in {window['key']}"
                dist = window[metric_key]
                # Required keys present
                assert {"count", "histogram"}.issubset(dist.keys())
                count = int(dist["count"])
                # Histogram buckets sum to count.
                hist_total = sum(int(b["count"]) for b in dist["histogram"])
                assert hist_total == count, (
                    f"{window['key']}/{metric_key}: histogram sums to {hist_total} "
                    f"but count is {count}"
                )
                # Every bucket has well-formed bounds.
                prev_upper: float | None = None
                for b in dist["histogram"]:
                    assert "lower_bound" in b
                    assert "upper_bound" in b
                    assert "count" in b
                    if prev_upper is not None:
                        # consecutive buckets line up
                        assert b["lower_bound"] == prev_upper
                    prev_upper = b["upper_bound"]
                # Final bucket is open-ended.
                assert dist["histogram"][-1]["upper_bound"] is None
                # Percentile ordering when we have data.
                if count > 0:
                    p50 = dist["p50"]
                    p95 = dist["p95"]
                    p99 = dist["p99"]
                    if p50 is not None and p95 is not None:
                        assert p50 <= p95
                    if p95 is not None and p99 is not None:
                        assert p95 <= p99
                    if dist["min"] is not None and dist["max"] is not None:
                        assert dist["min"] <= dist["max"]

        # Also sanity-check the 5-minute window for our specific data:
        # we inserted 10 streaming-OK rows with prompt_tokens 10..100 and the
        # 1 non-stream row with prompt_tokens=7.  So count >= 11 for prompt_tokens
        # and >= 10 for ttft_ms (only the streaming rows count there).
        five_min = next(w for w in body["windows"] if w["key"] == "5m")
        assert five_min["prompt_tokens"]["count"] >= 11
        assert five_min["ttft_ms"]["count"] >= 10
        # throughput: 10 valid streaming rows; the buggy row's throughput is clamped to NULL
        assert five_min["throughput_tps"]["count"] >= 10
    finally:
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM api_logs WHERE model_id = $1", tag)


@pytest.mark.asyncio
async def test_admin_performance_metrics_requires_auth(auth_client):
    resp = await auth_client.get("/admin/performance-metrics")
    assert resp.status_code == 401


class TestDecodeThroughputHelper:
    """Unit tests for `_decode_throughput_tps` used by /admin/recent-requests."""

    @staticmethod
    def _call(**kwargs):
        from serving.servers.routers.admin import _decode_throughput_tps

        return _decode_throughput_tps(**kwargs)

    def test_streaming_happy_path(self):
        # 100 completion tokens, ttft=200ms, latency=1200ms -> decode 1000ms
        # throughput = (100 - 1) / 1.0 = 99.0
        result = self._call(stream=True, latency_ms=1200, ttft_ms=200, completion_tokens=100)
        assert result == pytest.approx(99.0)

    def test_non_streaming_returns_none(self):
        assert self._call(stream=False, latency_ms=1200, ttft_ms=200, completion_tokens=100) is None

    def test_stream_none_returns_none(self):
        assert self._call(stream=None, latency_ms=1200, ttft_ms=200, completion_tokens=100) is None

    def test_missing_ttft_returns_none(self):
        assert self._call(stream=True, latency_ms=1200, ttft_ms=None, completion_tokens=100) is None

    def test_zero_ttft_returns_none(self):
        assert self._call(stream=True, latency_ms=1200, ttft_ms=0, completion_tokens=100) is None

    def test_latency_le_ttft_returns_none(self):
        assert self._call(stream=True, latency_ms=200, ttft_ms=200, completion_tokens=100) is None

    def test_missing_latency_returns_none(self):
        assert self._call(stream=True, latency_ms=None, ttft_ms=200, completion_tokens=100) is None

    def test_single_token_returns_none(self):
        assert self._call(stream=True, latency_ms=1200, ttft_ms=200, completion_tokens=1) is None

    def test_zero_tokens_returns_none(self):
        assert self._call(stream=True, latency_ms=1200, ttft_ms=200, completion_tokens=0) is None

    def test_missing_tokens_returns_none(self):
        assert self._call(stream=True, latency_ms=1200, ttft_ms=200, completion_tokens=None) is None
