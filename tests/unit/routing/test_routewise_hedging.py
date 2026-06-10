"""Tests for RouteWise hedge dispatch and probability-target router wiring."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest

from routing.routewise.config import RouteWiseConfig
from routing.routewise.hedging import (
    HedgedAdapter,
    ProviderEventSink,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


class _FakeEventSink:
    """Test double for ProviderEventSink."""

    def __init__(self) -> None:
        self.successes: list[str] = []
        self.failures: list[tuple[str, str]] = []

    def on_provider_success(self, provider: str) -> None:
        self.successes.append(provider)

    def on_provider_failure(self, provider: str, reason: str) -> None:
        self.failures.append((provider, reason))


def _quota_pool(router):
    """Return the router's only quota pool (single-pool test fixtures)."""
    return next(iter(router.quota_pools.values()))


def _seed_quota_snapshots(router, *, used: float = 0.0) -> None:
    """Install a ready provider snapshot for every quota pool on the router."""
    from datetime import datetime, timezone

    from routing.routewise.quota_snapshot import ProviderQuotaSnapshot

    now = datetime.now(timezone.utc)
    for pool in router.quota_pools.values():
        router.quota_snapshots._snapshots[pool.source] = ProviderQuotaSnapshot(
            source=pool.source,
            used=used,
            limit=float(pool.policy.limit),
            reset_at=None,
            fetched_at=now,
        )
        router.quota_snapshots._local_increments[pool.source] = 0


def _conc_pool(router):
    """Return the router's only concurrency pool (single-pool test fixtures)."""
    return next(iter(router.concurrency_pools.values()))


def _make_model_config(
    model_id: str = "test-model",
    provider: str = "provider-a",
    endpoint_id: str = "test:ep-a",
) -> MagicMock:
    cfg = MagicMock()
    cfg.id = model_id
    cfg.provider = provider
    cfg.endpoint_id = endpoint_id
    cfg.base_url = f"https://{provider}.example/v1"
    cfg.pricing = {"prompt": "3.0", "completion": "15.0"}
    cfg.provider_type = "on_demand"
    return cfg


def _make_fake_adapter(
    provider: str = "provider-a",
    endpoint_id: str = "test:ep-a",
    chat_result: dict[str, Any] | None = None,
    chat_delay: float = 0.0,
    chat_error: Exception | None = None,
    stream_chunks: list[str] | None = None,
    stream_delay: float = 0.0,
    stream_error: Exception | None = None,
) -> MagicMock:
    """Create a mock adapter with async chat_completion and stream_chat_completion."""
    adapter = MagicMock()
    adapter.config = _make_model_config(provider=provider, endpoint_id=endpoint_id)

    if chat_result is None:
        chat_result = {"choices": [{"message": {"content": "hello"}}]}

    async def _chat(*args: Any, **kwargs: Any) -> dict[str, Any]:
        if chat_delay > 0:
            await asyncio.sleep(chat_delay)
        if chat_error is not None:
            raise chat_error
        return chat_result

    adapter.chat_completion = _chat

    if stream_chunks is None:
        stream_chunks = [
            'data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n',
            'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n',
            'data: {"choices":[{"delta":{"content":" world"}}]}\n\n',
            "data: [DONE]\n\n",
        ]

    async def _stream(*args: Any, **kwargs: Any) -> AsyncGenerator[str, None]:
        if stream_error is not None:
            raise stream_error
        for chunk in stream_chunks:
            if stream_delay > 0:
                await asyncio.sleep(stream_delay)
            yield chunk

    adapter.stream_chat_completion = _stream

    return adapter


def _warm_envelope(
    router: Any,
    *,
    lower: float = 0.0000001,
    upper: float = 0.001,
    model_id: str = "test-model",
) -> None:
    pool = router._routewise_pool(model_id)
    base_ts = time.time() - 1.0
    for _ in range(25):
        router.envelope.observe(pool, lower, now=base_ts)
    for _ in range(25):
        router.envelope.observe(pool, upper, now=base_ts)


# ===========================================================================
# TestHedgedAdapterNonStreaming
# ===========================================================================


@pytest.mark.unit
class TestHedgedAdapterNonStreaming:
    @pytest.mark.asyncio
    async def test_primary_fast_no_hedge_triggered(self):
        """Primary returns before h* -> primary wins."""
        sink = _FakeEventSink()
        primary = _make_fake_adapter(
            provider="fast-primary",
            chat_delay=0.0,
            chat_result={"source": "primary"},
        )
        backup = _make_fake_adapter(
            provider="slow-backup",
            chat_delay=0.0,  # Backup would also be fast, but delayed by h*
            chat_result={"source": "backup"},
        )
        hedged = HedgedAdapter(
            primary=primary,
            backup=backup,
            hedge_threshold_sec=10.0,  # Very long delay -- backup won't start
            event_sink=sink,
        )
        result = await hedged.chat_completion([{"role": "user", "content": "hi"}])
        assert result["source"] == "primary"
        assert "fast-primary" in sink.successes

    @pytest.mark.asyncio
    async def test_primary_slow_backup_wins(self):
        """Primary is slow; backup wins the race."""
        sink = _FakeEventSink()
        primary = _make_fake_adapter(
            provider="slow-primary",
            chat_delay=1.0,
            chat_result={"source": "primary"},
        )
        backup = _make_fake_adapter(
            provider="fast-backup",
            chat_delay=0.0,
            chat_result={"source": "backup"},
        )
        hedged = HedgedAdapter(
            primary=primary,
            backup=backup,
            hedge_threshold_sec=0.01,  # Start backup almost immediately
            event_sink=sink,
        )
        result = await hedged.chat_completion([{"role": "user", "content": "hi"}])
        assert result["source"] == "backup"
        assert "fast-backup" in sink.successes

    @pytest.mark.asyncio
    async def test_primary_fails_backup_succeeds(self):
        """Primary raises; backup succeeds."""
        sink = _FakeEventSink()
        primary = _make_fake_adapter(
            provider="fail-primary",
            chat_error=RuntimeError("primary failed"),
        )
        backup = _make_fake_adapter(
            provider="good-backup",
            chat_delay=0.0,
            chat_result={"source": "backup"},
        )
        hedged = HedgedAdapter(
            primary=primary,
            backup=backup,
            hedge_threshold_sec=0.01,
            event_sink=sink,
        )
        result = await hedged.chat_completion([{"role": "user", "content": "hi"}])
        assert result["source"] == "backup"
        assert ("fail-primary", "RuntimeError") in sink.failures
        assert "good-backup" in sink.successes
        assert hedged.failed_attempts == [
            {
                "provider": "fail-primary",
                "endpoint_id": "test:ep-a",
                "error_type": "RuntimeError",
                "error": "primary failed",
            }
        ]

    @pytest.mark.asyncio
    async def test_both_fail_primary_error_raised(self):
        """Both fail -> primary's error is re-raised."""
        sink = _FakeEventSink()
        primary = _make_fake_adapter(
            provider="fail-primary",
            chat_error=RuntimeError("primary boom"),
        )
        backup = _make_fake_adapter(
            provider="fail-backup",
            chat_error=ValueError("backup boom"),
        )
        hedged = HedgedAdapter(
            primary=primary,
            backup=backup,
            hedge_threshold_sec=0.0,
            event_sink=sink,
        )
        with pytest.raises(RuntimeError, match="primary boom"):
            await hedged.chat_completion([{"role": "user", "content": "hi"}])
        assert ("fail-primary", "RuntimeError") in sink.failures
        assert ("fail-backup", "ValueError") in sink.failures
        assert {
            "provider": "fail-primary",
            "endpoint_id": "test:ep-a",
            "error_type": "RuntimeError",
            "error": "primary boom",
        } in hedged.failed_attempts
        assert {
            "provider": "fail-backup",
            "endpoint_id": "test:ep-a",
            "error_type": "ValueError",
            "error": "backup boom",
        } in hedged.failed_attempts
        assert len(hedged.failed_attempts) == 2

    @pytest.mark.asyncio
    async def test_event_sink_called_correctly(self):
        """Verify event sink receives exactly one success for the winner."""
        sink = _FakeEventSink()
        primary = _make_fake_adapter(
            provider="p1",
            chat_delay=0.0,
            chat_result={"source": "primary"},
        )
        backup = _make_fake_adapter(
            provider="p2",
            chat_delay=0.0,
            chat_result={"source": "backup"},
        )
        hedged = HedgedAdapter(
            primary=primary,
            backup=backup,
            hedge_threshold_sec=10.0,
            event_sink=sink,
        )
        await hedged.chat_completion([{"role": "user", "content": "hi"}])
        # Primary wins (fast); exactly one success recorded.
        assert len(sink.successes) == 1
        assert sink.successes[0] == "p1"


# ===========================================================================
# TestHedgedAdapterStreaming
# ===========================================================================


@pytest.mark.unit
class TestHedgedAdapterStreaming:
    @pytest.mark.asyncio
    async def test_primary_content_before_threshold(self):
        """Primary produces content before h* -> primary stream forwarded."""
        sink = _FakeEventSink()
        primary = _make_fake_adapter(
            provider="fast-primary",
            stream_chunks=[
                'data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n',
                'data: {"choices":[{"delta":{"content":"fast"}}]}\n\n',
                "data: [DONE]\n\n",
            ],
            stream_delay=0.0,
        )
        backup = _make_fake_adapter(
            provider="slow-backup",
            stream_chunks=[
                'data: {"choices":[{"delta":{"content":"slow"}}]}\n\n',
            ],
            stream_delay=0.0,
        )
        hedged = HedgedAdapter(
            primary=primary,
            backup=backup,
            hedge_threshold_sec=10.0,
            event_sink=sink,
        )
        chunks = []
        async for chunk in hedged.stream_chat_completion([{"role": "user", "content": "hi"}]):
            chunks.append(chunk)

        # Should contain primary's content.
        combined = "".join(chunks)
        assert "fast" in combined
        assert "fast-primary" in sink.successes

    @pytest.mark.asyncio
    async def test_backup_content_wins_after_threshold(self):
        """Backup produces content first when primary is slow."""
        sink = _FakeEventSink()
        primary = _make_fake_adapter(
            provider="slow-primary",
            stream_chunks=[
                'data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n',
                'data: {"choices":[{"delta":{"content":"slow"}}]}\n\n',
            ],
            stream_delay=0.5,  # Each chunk delayed 500ms
        )
        backup = _make_fake_adapter(
            provider="fast-backup",
            stream_chunks=[
                'data: {"choices":[{"delta":{"content":"quick"}}]}\n\n',
                "data: [DONE]\n\n",
            ],
            stream_delay=0.0,
        )
        hedged = HedgedAdapter(
            primary=primary,
            backup=backup,
            hedge_threshold_sec=0.01,  # Start backup almost immediately
            event_sink=sink,
        )
        chunks = []
        async for chunk in hedged.stream_chat_completion([{"role": "user", "content": "hi"}]):
            chunks.append(chunk)

        combined = "".join(chunks)
        assert "quick" in combined
        assert "fast-backup" in sink.successes

    @pytest.mark.asyncio
    async def test_primary_tiebreaker(self):
        """If both produce content in the same batch, primary wins."""
        sink = _FakeEventSink()
        # Both produce content immediately with no delay.
        primary = _make_fake_adapter(
            provider="primary",
            stream_chunks=[
                'data: {"choices":[{"delta":{"content":"p-content"}}]}\n\n',
            ],
            stream_delay=0.0,
        )
        backup = _make_fake_adapter(
            provider="backup",
            stream_chunks=[
                'data: {"choices":[{"delta":{"content":"b-content"}}]}\n\n',
            ],
            stream_delay=0.0,
        )
        hedged = HedgedAdapter(
            primary=primary,
            backup=backup,
            hedge_threshold_sec=0.0,  # Start both immediately
            event_sink=sink,
        )
        chunks = []
        async for chunk in hedged.stream_chat_completion([{"role": "user", "content": "hi"}]):
            chunks.append(chunk)

        combined = "".join(chunks)
        # Primary should win the tiebreak.
        assert "p-content" in combined
        assert "primary" in sink.successes

    @pytest.mark.asyncio
    async def test_pre_content_chunks_forwarded(self):
        """Role-only delta chunks (pre-content) are included in output."""
        sink = _FakeEventSink()
        primary = _make_fake_adapter(
            provider="primary",
            stream_chunks=[
                'data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n',
                'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n',
                "data: [DONE]\n\n",
            ],
            stream_delay=0.0,
        )
        backup = _make_fake_adapter(provider="backup", stream_delay=1.0)
        hedged = HedgedAdapter(
            primary=primary,
            backup=backup,
            hedge_threshold_sec=10.0,
            event_sink=sink,
        )
        chunks = []
        async for chunk in hedged.stream_chat_completion([{"role": "user", "content": "hi"}]):
            chunks.append(chunk)

        # Should see at least role chunk + content chunk + DONE.
        assert len(chunks) >= 2
        assert "role" in chunks[0]

    @pytest.mark.asyncio
    async def test_primary_error_immediate_backup(self):
        """Primary stream errors -> backup starts immediately."""
        sink = _FakeEventSink()

        async def _fail_stream(*args: Any, **kwargs: Any) -> AsyncGenerator[str, None]:
            raise ConnectionError("stream failed")
            yield  # Make it a generator

        primary = _make_fake_adapter(provider="fail-primary")
        primary.stream_chat_completion = _fail_stream

        backup = _make_fake_adapter(
            provider="good-backup",
            stream_chunks=[
                'data: {"choices":[{"delta":{"content":"backup ok"}}]}\n\n',
                "data: [DONE]\n\n",
            ],
            stream_delay=0.0,
        )
        hedged = HedgedAdapter(
            primary=primary,
            backup=backup,
            hedge_threshold_sec=10.0,
            event_sink=sink,
        )
        chunks = []
        async for chunk in hedged.stream_chat_completion([{"role": "user", "content": "hi"}]):
            chunks.append(chunk)

        combined = "".join(chunks)
        assert "backup ok" in combined
        assert ("fail-primary", "ConnectionError") in sink.failures

    @pytest.mark.asyncio
    async def test_cleanup_on_cancellation(self):
        """HedgedAdapter cleans up when the caller cancels."""
        sink = _FakeEventSink()
        primary = _make_fake_adapter(
            provider="primary",
            stream_chunks=[
                'data: {"choices":[{"delta":{"content":"slow"}}]}\n\n',
            ],
            stream_delay=5.0,
        )
        backup = _make_fake_adapter(
            provider="backup",
            stream_chunks=[
                'data: {"choices":[{"delta":{"content":"slow"}}]}\n\n',
            ],
            stream_delay=5.0,
        )
        hedged = HedgedAdapter(
            primary=primary,
            backup=backup,
            hedge_threshold_sec=0.01,
            event_sink=sink,
        )

        async def _consume() -> list[str]:
            chunks: list[str] = []
            async for chunk in hedged.stream_chat_completion([{"role": "user", "content": "hi"}]):
                chunks.append(chunk)
            return chunks

        task = asyncio.ensure_future(_consume())
        await asyncio.sleep(0.05)
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task


# ===========================================================================
# TestRouterHedgeMode
# ===========================================================================


def _make_router_with_two_api(
    config: RouteWiseConfig | None = None,
) -> tuple[Any, MagicMock, MagicMock]:
    """Build a RouteWiseRouter with two S_A adapters (no S_Q)."""
    from routing.routewise.router import RouteWiseRouter

    if config is None:
        config = RouteWiseConfig()

    api_a = MagicMock()
    api_a.config = _make_model_config(provider="provider-a", endpoint_id="test-model:api-a")
    api_a.config.pricing = {"prompt": "3.0", "completion": "15.0"}
    api_a.config.provider_type = "on_demand"

    api_b = MagicMock()
    api_b.config = _make_model_config(provider="provider-b", endpoint_id="test-model:api-b")
    api_b.config.pricing = {"prompt": "4.0", "completion": "20.0"}
    api_b.config.provider_type = "on_demand"

    @dataclass
    class _FakeRouteConfig:
        adapters: list[tuple[Any, float]]

    class _FakeFixedRouter:
        def __init__(self) -> None:
            self.routes: dict[str, _FakeRouteConfig] = {}

        def add(self, model_id: str, adapters: list[tuple[Any, float]]) -> None:
            self.routes[model_id] = _FakeRouteConfig(adapters=adapters)

    fr = _FakeFixedRouter()
    fr.add("test-model", [(api_a, 0.5), (api_b, 0.5)])
    router = RouteWiseRouter(fixed_router=fr, config=config)
    return router, api_a, api_b


def _make_router_with_api_and_concurrency(
    config: RouteWiseConfig,
) -> tuple[Any, MagicMock, MagicMock]:
    """Build a RouteWiseRouter with one S_A and one S_C adapter."""
    from routing.routewise.router import RouteWiseRouter

    api = MagicMock()
    api.config = _make_model_config(provider="provider-a", endpoint_id="test-model:api-a")
    api.config.pricing = {"prompt": "3.0", "completion": "15.0"}
    api.config.provider_type = "on_demand"

    concurrency = MagicMock()
    concurrency.config = _make_model_config(
        provider="provider-b",
        endpoint_id="test-model:concurrency-b",
    )
    concurrency.config.pricing = {"prompt": "0.0", "completion": "0.0"}
    concurrency.config.provider_type = "concurrency"
    concurrency.config.concurrency = {"limit": 1}

    @dataclass
    class _FakeRouteConfig:
        adapters: list[tuple[Any, float]]

    class _FakeFixedRouter:
        def __init__(self) -> None:
            self.routes: dict[str, _FakeRouteConfig] = {}

        def add(self, model_id: str, adapters: list[tuple[Any, float]]) -> None:
            self.routes[model_id] = _FakeRouteConfig(adapters=adapters)

    fr = _FakeFixedRouter()
    fr.add("test-model", [(api, 0.5), (concurrency, 0.5)])
    router = RouteWiseRouter(fixed_router=fr, config=config)
    return router, api, concurrency


def _make_router_with_api_and_quota(
    config: RouteWiseConfig,
) -> tuple[Any, MagicMock, MagicMock]:
    """Build a RouteWiseRouter with one S_A and one S_Q adapter."""
    from routing.routewise.router import RouteWiseRouter

    api = MagicMock()
    api.config = _make_model_config(provider="provider-a", endpoint_id="test-model:api-a")
    api.config.pricing = {"prompt": "3.0", "completion": "15.0"}
    api.config.provider_type = "on_demand"

    quota = MagicMock()
    quota.config = _make_model_config(
        provider="provider-q",
        endpoint_id="test-model:quota-q",
    )
    quota.config.pricing = {"prompt": "0.0", "completion": "0.0"}
    quota.config.provider_type = "quota"
    quota.config.quota = {"limit": 10}
    quota.config.quota_source = {
        "provider": "stub",
        "usage_label": "Daily requests",
        "unit": "requests",
    }

    @dataclass
    class _FakeRouteConfig:
        adapters: list[tuple[Any, float]]

    class _FakeFixedRouter:
        def __init__(self) -> None:
            self.routes: dict[str, _FakeRouteConfig] = {}

        def add(self, model_id: str, adapters: list[tuple[Any, float]]) -> None:
            self.routes[model_id] = _FakeRouteConfig(adapters=adapters)

    fr = _FakeFixedRouter()
    fr.add("test-model", [(api, 0.5), (quota, 0.5)])
    router = RouteWiseRouter(fixed_router=fr, config=config)
    _seed_quota_snapshots(router)
    return router, api, quota


def _make_router_with_api_quota_and_api(
    config: RouteWiseConfig,
    quota_limit: int = 10,
) -> tuple[Any, MagicMock, MagicMock, MagicMock]:
    """Build a RouteWiseRouter with API primary plus S_Q and S_A backups."""
    from routing.routewise.router import RouteWiseRouter

    api_primary = MagicMock()
    api_primary.config = _make_model_config(
        provider="provider-a",
        endpoint_id="test-model:api-a",
    )
    api_primary.config.pricing = {"prompt": "3.0", "completion": "15.0"}
    api_primary.config.provider_type = "on_demand"

    quota = MagicMock()
    quota.config = _make_model_config(
        provider="provider-q",
        endpoint_id="test-model:quota-q",
    )
    quota.config.pricing = {"prompt": "0.0", "completion": "0.0"}
    quota.config.provider_type = "quota"
    quota.config.quota = {"limit": quota_limit}
    quota.config.quota_source = {
        "provider": "stub",
        "usage_label": "Daily requests",
        "unit": "requests",
    }

    api_backup = MagicMock()
    api_backup.config = _make_model_config(
        provider="provider-c",
        endpoint_id="test-model:api-c",
    )
    api_backup.config.pricing = {"prompt": "4.0", "completion": "20.0"}
    api_backup.config.provider_type = "on_demand"

    @dataclass
    class _FakeRouteConfig:
        adapters: list[tuple[Any, float]]

    class _FakeFixedRouter:
        def __init__(self) -> None:
            self.routes: dict[str, _FakeRouteConfig] = {}

        def add(self, model_id: str, adapters: list[tuple[Any, float]]) -> None:
            self.routes[model_id] = _FakeRouteConfig(adapters=adapters)

    fr = _FakeFixedRouter()
    fr.add("test-model", [(api_primary, 0.4), (quota, 0.3), (api_backup, 0.3)])
    router = RouteWiseRouter(fixed_router=fr, config=config)
    _seed_quota_snapshots(router)
    return router, api_primary, quota, api_backup


@pytest.mark.unit
class TestRouterHedgeMode:
    def test_probability_target_mode_wraps_body_router_selection(self):
        """Probability-target mode returns a real HedgedAdapter."""
        config = RouteWiseConfig(
            budget_alpha=0.0,
            latency_min_samples=5,
            latency_slo_sec=3.0,
            latency_hedge_mode="probability_target",
        )
        router, api_a, _api_b = _make_router_with_two_api(config)

        for _ in range(25):
            router.predictor.update("test-model", 500)

        now = time.time()
        for _ in range(10):
            router._latency_profiles["test-model:api-a"].record(now, 500.0)
            router._latency_profiles["test-model:api-a"].record(now, 3500.0)
            router._latency_profiles["test-model:api-b"].record(now, 200.0)

        selected = router._select_adapter("test-model", {"prompt_tokens": 1000})

        assert isinstance(selected, HedgedAdapter)
        assert selected.primary is api_a
        assert selected.backup is None
        assert selected.hedge_checkpoints_sec
        assert selected.hedge_threshold_sec > 0.0

    @pytest.mark.asyncio
    async def test_probability_target_mode_dispatches_backup_and_updates_metadata(self):
        """Probability-target hedging dispatches the backup and records the winner."""
        config = RouteWiseConfig(
            budget_alpha=0.0,
            latency_min_samples=1,
            latency_slo_sec=0.04,
            latency_hedge_mode="probability_target",
        )
        router, api_a, api_b = _make_router_with_two_api(config)

        now = time.time()
        router._latency_profiles["test-model:api-a"].record(now, 100.0)
        router._latency_profiles["test-model:api-b"].record(now, 1.0)

        async def _slow_primary(messages, **params):
            await asyncio.sleep(0.2)
            return {"choices": [{"message": {"content": "primary"}}], "source": "primary"}

        async def _fast_backup(messages, **params):
            return {"choices": [{"message": {"content": "backup"}}], "source": "backup"}

        api_a.chat_completion = _slow_primary
        api_b.chat_completion = _fast_backup

        resp = await router.chat_completion(
            "test-model",
            [{"role": "user", "content": "hi"}],
        )

        assert resp["source"] == "backup"
        assert resp["_routing"]["endpoint_id"] == "test-model:api-b"
        routewise = resp["_routing"]["routewise"]
        assert routewise["hedged"] is True
        assert routewise["hedge_triggered"] is True
        assert routewise["backup_won"] is True
        assert routewise["hedge_winner"] == "backup"
        assert routewise["backup_provider"] == "test-model:api-b"
        assert routewise["hedge_algorithm"] == "probability_target"
        assert routewise["hedge_schedule"] == "slo_relative_checkpoints"
        assert routewise["primary_routing_estimated_cost_usd"] is not None
        assert routewise["backup_routing_estimated_cost_usd"] is not None
        assert routewise["routing_estimated_cost_usd"] == pytest.approx(
            routewise["primary_routing_estimated_cost_usd"]
            + routewise["backup_routing_estimated_cost_usd"]
        )

    @pytest.mark.asyncio
    async def test_probability_target_primary_failure_surfaces_failed_attempt(self):
        """A hidden hedged primary failure must feed the normal observation path."""
        config = RouteWiseConfig(
            budget_alpha=0.0,
            latency_min_samples=1,
            latency_slo_sec=0.04,
            latency_hedge_mode="probability_target",
        )
        router, api_a, api_b = _make_router_with_two_api(config)

        now = time.time()
        router._latency_profiles["test-model:api-a"].record(now, 100.0)
        router._latency_profiles["test-model:api-b"].record(now, 1.0)

        async def _failed_primary(messages, **params):
            raise RuntimeError("primary failed")

        async def _fast_backup(messages, **params):
            return {"choices": [{"message": {"content": "backup"}}], "source": "backup"}

        api_a.chat_completion = _failed_primary
        api_b.chat_completion = _fast_backup

        resp = await router.chat_completion(
            "test-model",
            [{"role": "user", "content": "hi"}],
        )

        assert resp["source"] == "backup"
        assert resp["_routing"]["failed_attempts"] == [
            {
                "provider": "provider-a",
                "endpoint_id": "test-model:api-a",
                "error_type": "RuntimeError",
                "error": "primary failed",
            }
        ]
        assert (
            resp["_routing"]["routewise"]["failed_attempts"] == resp["_routing"]["failed_attempts"]
        )

    @pytest.mark.asyncio
    async def test_probability_target_primary_wins_before_dispatch(self):
        """A planned hedge is not recorded as triggered if primary returns first."""
        config = RouteWiseConfig(
            budget_alpha=0.0,
            latency_min_samples=1,
            latency_slo_sec=0.04,
            latency_hedge_mode="probability_target",
        )
        router, api_a, api_b = _make_router_with_two_api(config)

        now = time.time()
        router._latency_profiles["test-model:api-a"].record(now, 100.0)
        router._latency_profiles["test-model:api-b"].record(now, 1.0)

        async def _fast_primary(messages, **params):
            return {"choices": [{"message": {"content": "primary"}}], "source": "primary"}

        async def _fast_backup(messages, **params):
            return {"choices": [{"message": {"content": "backup"}}], "source": "backup"}

        api_a.chat_completion = _fast_primary
        api_b.chat_completion = _fast_backup

        resp = await router.chat_completion(
            "test-model",
            [{"role": "user", "content": "hi"}],
        )

        assert resp["source"] == "primary"
        routewise = resp["_routing"]["routewise"]
        assert routewise["hedged"] is False
        assert routewise["hedge_triggered"] is False
        assert routewise["backup_won"] is False
        assert routewise["backup_provider"] is None

    @pytest.mark.asyncio
    async def test_probability_target_streaming_backup_winner_updates_routing(self):
        """Streaming hedges emit winner routing before backup content."""
        config = RouteWiseConfig(
            budget_alpha=0.0,
            latency_min_samples=1,
            latency_slo_sec=0.04,
            latency_hedge_mode="probability_target",
        )
        router, api_a, api_b = _make_router_with_two_api(config)

        now = time.time()
        router._latency_profiles["test-model:api-a"].record(now, 100.0)
        router._latency_profiles["test-model:api-b"].record(now, 1.0)

        async def _slow_primary_stream(messages, **params):
            await asyncio.sleep(0.2)
            yield 'data: {"choices":[{"delta":{"content":"primary"}}]}\n\n'
            yield "data: [DONE]\n\n"

        async def _fast_backup_stream(messages, **params):
            yield 'data: {"choices":[{"delta":{"content":"backup"}}]}\n\n'
            yield "data: [DONE]\n\n"

        api_a.stream_chat_completion = _slow_primary_stream
        api_b.stream_chat_completion = _fast_backup_stream

        chunks = []
        async for chunk in router.stream_chat_completion(
            "test-model",
            [{"role": "user", "content": "hi"}],
            request_id="req-stream-hedge",
        ):
            chunks.append(chunk)

        combined = "".join(chunks)
        assert "backup" in combined
        assert '"endpoint_id": "test-model:api-b"' in combined

        routewise_chunks = [
            json.loads(chunk[6:])
            for chunk in chunks
            if isinstance(chunk, str) and chunk.startswith("data: ") and "routewise" in chunk
        ]
        assert routewise_chunks
        routewise = routewise_chunks[-1]["_routing"]["routewise"]
        assert routewise["hedged"] is True
        assert routewise["backup_won"] is True
        assert routewise["hedge_winner"] == "backup"

    @pytest.mark.asyncio
    async def test_probability_target_can_dispatch_concurrency_backup(self):
        """Backup selection is not restricted to on-demand providers."""
        config = RouteWiseConfig(
            budget_alpha=1.0,
            latency_min_samples=1,
            latency_slo_sec=0.04,
            latency_hedge_mode="probability_target",
        )
        router, api, concurrency = _make_router_with_api_and_concurrency(config)

        def _force_api_primary(candidates, solution):
            return next(c for c in candidates if c.endpoint_id == "test-model:api-a")

        router._sample_solution = _force_api_primary

        now = time.time()
        router._latency_profiles["test-model:api-a"].record(now, 100.0)
        router._latency_profiles["test-model:concurrency-b"].record(now, 1.0)

        async def _slow_primary(messages, **params):
            await asyncio.sleep(0.2)
            return {"choices": [{"message": {"content": "primary"}}], "source": "primary"}

        async def _fast_backup(messages, **params):
            return {"choices": [{"message": {"content": "backup"}}], "source": "backup"}

        api.chat_completion = _slow_primary
        concurrency.chat_completion = _fast_backup

        resp = await router.chat_completion(
            "test-model",
            [{"role": "user", "content": "hi"}],
        )

        assert resp["source"] == "backup"
        assert len(router.concurrency_pools) == 1
        assert _conc_pool(router).active == 0
        assert _conc_pool(router).get_stats()["total_acquired"] == 1
        routewise = resp["_routing"]["routewise"]
        assert routewise["backup_provider"] == "test-model:concurrency-b"
        assert routewise["backup_provider_type"] == "concurrency"
        assert routewise["backup_won"] is True

    @pytest.mark.asyncio
    async def test_probability_target_can_dispatch_quota_backup(self):
        """Quota backups consume quota when the hedge actually dispatches."""
        config = RouteWiseConfig(
            budget_alpha=1.0,
            latency_min_samples=1,
            latency_slo_sec=0.04,
            latency_hedge_mode="probability_target",
        )
        router, api, quota = _make_router_with_api_and_quota(config)
        _warm_envelope(router)

        def _force_api_primary(candidates, solution):
            return next(c for c in candidates if c.endpoint_id == "test-model:api-a")

        router._sample_solution = _force_api_primary

        now = time.time()
        router._latency_profiles["test-model:api-a"].record(now, 100.0)
        router._latency_profiles["test-model:quota-q"].record(now, 1.0)

        async def _slow_primary(messages, **params):
            await asyncio.sleep(0.2)
            return {"choices": [{"message": {"content": "primary"}}], "source": "primary"}

        async def _fast_backup(messages, **params):
            return {"choices": [{"message": {"content": "backup"}}], "source": "backup"}

        api.chat_completion = _slow_primary
        quota.chat_completion = _fast_backup

        before = _quota_pool(router).remaining
        resp = await router.chat_completion(
            "test-model",
            [{"role": "user", "content": "hi"}],
        )

        assert resp["source"] == "backup"
        assert before - _quota_pool(router).remaining == 1
        routewise = resp["_routing"]["routewise"]
        assert routewise["backup_provider"] == "test-model:quota-q"
        assert routewise["backup_provider_type"] == "quota"
        assert routewise["backup_won"] is True

    @pytest.mark.asyncio
    async def test_probability_target_backup_tiebreak_uses_request_cost_not_shadow_price(self):
        """Checkpoint backup selection matches SIM/REAL raw marginal-cost tiebreak."""
        config = RouteWiseConfig(
            budget_alpha=1.0,
            latency_min_samples=1,
            latency_slo_sec=0.04,
            latency_hedge_mode="probability_target",
        )
        router, api_primary, quota, api_backup = _make_router_with_api_quota_and_api(config)
        _warm_envelope(router)

        def _force_api_primary(candidates, solution):
            return next(c for c in candidates if c.endpoint_id == "test-model:api-a")

        router._sample_solution = _force_api_primary
        for _ in range(9):
            _quota_pool(router).consume()

        now = time.time()
        router._latency_profiles["test-model:api-a"].record(now, 100.0)
        router._latency_profiles["test-model:quota-q"].record(now, 1.0)
        router._latency_profiles["test-model:api-c"].record(now, 1.0)

        async def _slow_primary(messages, **params):
            await asyncio.sleep(0.2)
            return {"choices": [{"message": {"content": "primary"}}], "source": "primary"}

        async def _fast_quota_backup(messages, **params):
            return {"choices": [{"message": {"content": "backup"}}], "source": "quota-q"}

        async def _api_backup_should_not_run(messages, **params):
            raise AssertionError("API backup should lose to cheaper raw quota backup")

        api_primary.chat_completion = _slow_primary
        quota.chat_completion = _fast_quota_backup
        api_backup.chat_completion = _api_backup_should_not_run

        before = _quota_pool(router).remaining
        resp = await router.chat_completion(
            "test-model",
            [{"role": "user", "content": "hi"}],
        )

        assert resp["source"] == "quota-q"
        assert before - _quota_pool(router).remaining == 1
        routewise = resp["_routing"]["routewise"]
        assert routewise["backup_provider"] == "test-model:quota-q"
        assert routewise["backup_provider_type"] == "quota"
        assert routewise["backup_won"] is True

    @pytest.mark.asyncio
    async def test_probability_target_reselects_backup_at_checkpoint(self):
        """Checkpoint hedging re-evaluates current state instead of using a stale backup."""
        config = RouteWiseConfig(
            budget_alpha=1.0,
            latency_min_samples=1,
            latency_slo_sec=0.04,
            latency_hedge_mode="probability_target",
        )
        router, api_primary, quota, api_backup = _make_router_with_api_quota_and_api(
            config, quota_limit=1
        )
        _warm_envelope(router)

        def _force_api_primary(candidates, solution):
            return next(c for c in candidates if c.endpoint_id == "test-model:api-a")

        router._sample_solution = _force_api_primary

        now = time.time()
        router._latency_profiles["test-model:api-a"].record(now, 100.0)
        router._latency_profiles["test-model:quota-q"].record(now, 1.0)
        router._latency_profiles["test-model:api-c"].record(now, 1.0)

        async def _slow_primary(messages, **params):
            _quota_pool(router).consume()
            await asyncio.sleep(0.2)
            return {"choices": [{"message": {"content": "primary"}}], "source": "primary"}

        async def _quota_should_not_run(messages, **params):
            raise AssertionError("stale quota backup should not dispatch")

        async def _fast_api_backup(messages, **params):
            return {"choices": [{"message": {"content": "backup"}}], "source": "api-c"}

        api_primary.chat_completion = _slow_primary
        quota.chat_completion = _quota_should_not_run
        api_backup.chat_completion = _fast_api_backup

        resp = await router.chat_completion(
            "test-model",
            [{"role": "user", "content": "hi"}],
        )

        assert resp["source"] == "api-c"
        routewise = resp["_routing"]["routewise"]
        assert routewise["backup_provider"] == "test-model:api-c"
        assert routewise["backup_provider_type"] == "on_demand"
        assert routewise["backup_won"] is True


# ===========================================================================
# TestProviderEventSinkProtocol
# ===========================================================================


@pytest.mark.unit
class TestProviderEventSinkProtocol:
    def test_fake_event_sink_satisfies_protocol(self):
        """_FakeEventSink satisfies ProviderEventSink protocol."""
        sink = _FakeEventSink()
        assert isinstance(sink, ProviderEventSink)

    def test_router_satisfies_protocol(self):
        """RouteWiseRouter satisfies ProviderEventSink protocol."""
        config = RouteWiseConfig()
        router, _, _ = _make_router_with_two_api(config)
        assert isinstance(router, ProviderEventSink)


# ===========================================================================
# TestWinnerAttribution
# ===========================================================================


@pytest.mark.unit
class TestWinnerAttribution:
    """Verify that HedgedAdapter swaps self.config to the real winner."""

    @pytest.mark.asyncio
    async def test_nonstreaming_backup_wins_config_swapped(self):
        """When backup wins non-streaming race, self.config points to backup."""
        sink = _FakeEventSink()
        primary = _make_fake_adapter(
            provider="slow-primary",
            endpoint_id="ep:slow",
            chat_delay=1.0,
            chat_result={"source": "primary"},
        )
        backup = _make_fake_adapter(
            provider="fast-backup",
            endpoint_id="ep:fast",
            chat_delay=0.0,
            chat_result={"source": "backup"},
        )
        hedged = HedgedAdapter(
            primary=primary,
            backup=backup,
            hedge_threshold_sec=0.01,
            event_sink=sink,
        )
        # Before race, config is primary's.
        assert hedged.config.provider == "slow-primary"

        result = await hedged.chat_completion([{"role": "user", "content": "hi"}])
        assert result["source"] == "backup"
        # After race, config must be swapped to backup's.
        assert hedged.config.provider == "fast-backup"
        assert hedged.config.endpoint_id == "ep:fast"

    @pytest.mark.asyncio
    async def test_nonstreaming_primary_wins_config_unchanged(self):
        """When primary wins, self.config stays as primary."""
        sink = _FakeEventSink()
        primary = _make_fake_adapter(
            provider="fast-primary",
            endpoint_id="ep:fast",
            chat_delay=0.0,
            chat_result={"source": "primary"},
        )
        backup = _make_fake_adapter(
            provider="slow-backup",
            endpoint_id="ep:slow",
            chat_delay=0.0,
            chat_result={"source": "backup"},
        )
        hedged = HedgedAdapter(
            primary=primary,
            backup=backup,
            hedge_threshold_sec=10.0,
            event_sink=sink,
        )
        result = await hedged.chat_completion([{"role": "user", "content": "hi"}])
        assert result["source"] == "primary"
        assert hedged.config.provider == "fast-primary"

    @pytest.mark.asyncio
    async def test_streaming_backup_wins_config_swapped(self):
        """When backup wins streaming race, self.config points to backup."""
        sink = _FakeEventSink()
        primary = _make_fake_adapter(
            provider="slow-primary",
            endpoint_id="ep:slow",
            stream_chunks=[
                'data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n',
                'data: {"choices":[{"delta":{"content":"slow"}}]}\n\n',
            ],
            stream_delay=0.5,
        )
        backup = _make_fake_adapter(
            provider="fast-backup",
            endpoint_id="ep:fast",
            stream_chunks=[
                'data: {"choices":[{"delta":{"content":"quick"}}]}\n\n',
                "data: [DONE]\n\n",
            ],
            stream_delay=0.0,
        )
        hedged = HedgedAdapter(
            primary=primary,
            backup=backup,
            hedge_threshold_sec=0.01,
            event_sink=sink,
        )
        assert hedged.config.provider == "slow-primary"

        chunks = []
        async for chunk in hedged.stream_chat_completion([{"role": "user", "content": "hi"}]):
            chunks.append(chunk)

        combined = "".join(chunks)
        assert "quick" in combined
        assert '"endpoint_id": "ep:fast"' in combined
        # Config must be swapped to backup's.
        assert hedged.config.provider == "fast-backup"
        assert hedged.config.endpoint_id == "ep:fast"


# ===========================================================================
# TestToolCallsWinnerDetection
# ===========================================================================


@pytest.mark.unit
class TestToolCallsWinnerDetection:
    """Verify content detection handles tool_calls and reasoning_content."""

    @pytest.mark.asyncio
    async def test_tool_calls_detected_as_content(self):
        """A stream chunk with tool_calls in delta counts as first content."""
        sink = _FakeEventSink()
        tool_chunk = (
            'data: {"choices":[{"delta":{"tool_calls":[{"function":{"name":"get_weather"}}]}}]}\n\n'
        )
        primary = _make_fake_adapter(
            provider="tool-primary",
            stream_chunks=[tool_chunk, "data: [DONE]\n\n"],
            stream_delay=0.0,
        )
        backup = _make_fake_adapter(
            provider="backup",
            stream_chunks=[
                'data: {"choices":[{"delta":{"content":"fallback"}}]}\n\n',
            ],
            stream_delay=0.0,
        )
        hedged = HedgedAdapter(
            primary=primary,
            backup=backup,
            hedge_threshold_sec=10.0,
            event_sink=sink,
        )
        chunks = []
        async for chunk in hedged.stream_chat_completion([{"role": "user", "content": "weather?"}]):
            chunks.append(chunk)

        combined = "".join(chunks)
        assert "get_weather" in combined
        assert "tool-primary" in sink.successes

    @pytest.mark.asyncio
    async def test_reasoning_content_detected(self):
        """A stream chunk with reasoning_content counts as first content."""
        sink = _FakeEventSink()
        reasoning_chunk = 'data: {"choices":[{"delta":{"reasoning_content":"thinking..."}}]}\n\n'
        primary = _make_fake_adapter(
            provider="reasoning-primary",
            stream_chunks=[reasoning_chunk, "data: [DONE]\n\n"],
            stream_delay=0.0,
        )
        backup = _make_fake_adapter(
            provider="backup",
            stream_chunks=[
                'data: {"choices":[{"delta":{"content":"fallback"}}]}\n\n',
            ],
            stream_delay=0.0,
        )
        hedged = HedgedAdapter(
            primary=primary,
            backup=backup,
            hedge_threshold_sec=10.0,
            event_sink=sink,
        )
        chunks = []
        async for chunk in hedged.stream_chat_completion([{"role": "user", "content": "think"}]):
            chunks.append(chunk)

        combined = "".join(chunks)
        assert "thinking" in combined
        assert "reasoning-primary" in sink.successes


# ===========================================================================
# TestNonStreamingFailFastBackup
# ===========================================================================


@pytest.mark.unit
class TestNonStreamingFailFastBackup:
    """Verify primary failure triggers immediate backup (no h* delay)."""

    @pytest.mark.asyncio
    async def test_primary_fail_does_not_wait_h_star(self):
        """When primary fails, backup runs immediately, not after h* seconds."""
        sink = _FakeEventSink()
        primary = _make_fake_adapter(
            provider="fail-primary",
            chat_error=RuntimeError("primary failed"),
        )
        backup = _make_fake_adapter(
            provider="good-backup",
            chat_delay=0.0,
            chat_result={"source": "backup"},
        )
        hedged = HedgedAdapter(
            primary=primary,
            backup=backup,
            hedge_threshold_sec=60.0,  # Very long delay
            event_sink=sink,
        )
        import time as _time

        start = _time.monotonic()
        result = await hedged.chat_completion([{"role": "user", "content": "hi"}])
        elapsed = _time.monotonic() - start

        assert result["source"] == "backup"
        # Must not wait 60s for backup; should complete in well under 1s.
        assert elapsed < 2.0, f"Expected fast failover, took {elapsed:.2f}s"
        assert ("fail-primary", "RuntimeError") in sink.failures
        assert "good-backup" in sink.successes
        # Config should be swapped to backup (winner attribution).
        assert hedged.config.provider == "good-backup"


# ===========================================================================
# TestStreamingReqCtxUpdate
# ===========================================================================


@pytest.mark.unit
class TestStreamingReqCtxUpdate:
    """Verify that streaming HedgedAdapter updates req_ctx after winner is determined."""

    @pytest.mark.asyncio
    async def test_backup_wins_req_ctx_updated(self):
        """When backup wins streaming race, req_ctx reflects backup's provider/endpoint_id."""
        from serving.utils import context as req_ctx

        sink = _FakeEventSink()
        primary = _make_fake_adapter(
            provider="slow-primary",
            endpoint_id="ep:slow",
            stream_chunks=[
                'data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n',
                'data: {"choices":[{"delta":{"content":"slow"}}]}\n\n',
            ],
            stream_delay=0.5,
        )
        backup = _make_fake_adapter(
            provider="fast-backup",
            endpoint_id="ep:fast",
            stream_chunks=[
                'data: {"choices":[{"delta":{"content":"quick"}}]}\n\n',
                "data: [DONE]\n\n",
            ],
            stream_delay=0.0,
        )
        hedged = HedgedAdapter(
            primary=primary,
            backup=backup,
            hedge_threshold_sec=0.01,
            event_sink=sink,
        )

        # Simulate what _execute_stream_adapter does: push primary's info.
        with req_ctx.push(
            model="test-model",
            provider="slow-primary",
            endpoint_id="ep:slow",
        ):
            ctx_snapshots: list[dict[str, Any]] = []
            async for _chunk in hedged.stream_chat_completion([{"role": "user", "content": "hi"}]):
                # Capture ctx on first real chunk.
                if not ctx_snapshots:
                    ctx_snapshots.append(dict(req_ctx.get()))

            # req_ctx should reflect the backup (winner), not the primary.
            assert len(ctx_snapshots) == 1
            assert ctx_snapshots[0]["provider"] == "fast-backup"
            assert ctx_snapshots[0]["endpoint_id"] == "ep:fast"

    @pytest.mark.asyncio
    async def test_primary_wins_req_ctx_unchanged(self):
        """When primary wins streaming race, req_ctx still has primary's info."""
        from serving.utils import context as req_ctx

        sink = _FakeEventSink()
        primary = _make_fake_adapter(
            provider="fast-primary",
            endpoint_id="ep:fast",
            stream_chunks=[
                'data: {"choices":[{"delta":{"content":"quick"}}]}\n\n',
                "data: [DONE]\n\n",
            ],
            stream_delay=0.0,
        )
        backup = _make_fake_adapter(
            provider="slow-backup",
            endpoint_id="ep:slow",
            stream_chunks=[
                'data: {"choices":[{"delta":{"content":"slow"}}]}\n\n',
            ],
            stream_delay=0.5,
        )
        hedged = HedgedAdapter(
            primary=primary,
            backup=backup,
            hedge_threshold_sec=10.0,
            event_sink=sink,
        )

        with req_ctx.push(
            model="test-model",
            provider="fast-primary",
            endpoint_id="ep:fast",
        ):
            ctx_snapshots: list[dict[str, Any]] = []
            async for _chunk in hedged.stream_chat_completion([{"role": "user", "content": "hi"}]):
                if not ctx_snapshots:
                    ctx_snapshots.append(dict(req_ctx.get()))

            assert ctx_snapshots[0]["provider"] == "fast-primary"
            assert ctx_snapshots[0]["endpoint_id"] == "ep:fast"


# ===========================================================================
# TestNonStreamingBackupPhaseTracking
# ===========================================================================


@pytest.mark.unit
class TestNonStreamingBackupPhaseTracking:
    """Verify that cancel+relaunch only happens during backup's sleep phase."""

    @pytest.mark.asyncio
    async def test_backup_in_sleep_phase_gets_relaunched(self):
        """When primary fails while backup is still sleeping, backup is relaunched immediately."""
        sink = _FakeEventSink()
        primary = _make_fake_adapter(
            provider="fail-primary",
            chat_error=RuntimeError("primary failed"),
        )
        backup = _make_fake_adapter(
            provider="good-backup",
            chat_delay=0.0,
            chat_result={"source": "backup"},
        )
        hedged = HedgedAdapter(
            primary=primary,
            backup=backup,
            hedge_threshold_sec=60.0,  # Very long delay -> backup still sleeping
            event_sink=sink,
        )
        import time as _time

        start = _time.monotonic()
        result = await hedged.chat_completion([{"role": "user", "content": "hi"}])
        elapsed = _time.monotonic() - start

        assert result["source"] == "backup"
        # Must complete fast -- backup was relaunched without 60s delay.
        assert elapsed < 2.0, f"Expected fast failover, took {elapsed:.2f}s"

    @pytest.mark.asyncio
    async def test_backup_past_sleep_not_cancelled(self):
        """When primary fails after backup has passed its sleep phase, backup continues."""
        sink = _FakeEventSink()
        # Primary sleeps 0.15s then fails.
        primary = _make_fake_adapter(
            provider="fail-primary",
            chat_delay=0.15,
            chat_error=RuntimeError("primary failed"),
        )
        # Backup: h*=0.01s (passes sleep almost immediately), then takes 0.3s.
        backup = _make_fake_adapter(
            provider="good-backup",
            chat_delay=0.3,
            chat_result={"source": "backup"},
        )
        hedged = HedgedAdapter(
            primary=primary,
            backup=backup,
            hedge_threshold_sec=0.01,  # Very short -> backup passes sleep fast
            event_sink=sink,
        )
        import time as _time

        start = _time.monotonic()
        result = await hedged.chat_completion([{"role": "user", "content": "hi"}])
        elapsed = _time.monotonic() - start

        assert result["source"] == "backup"
        # Backup was NOT cancelled and relaunched -- it continued its in-flight request.
        # Total time ~ 0.3s (backup's delay), not 0.15 + 0.3 = 0.45s (relaunch).
        assert elapsed < 0.6, f"Expected backup to continue, took {elapsed:.2f}s"
        assert ("fail-primary", "RuntimeError") in sink.failures
        assert "good-backup" in sink.successes
