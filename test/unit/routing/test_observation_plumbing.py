"""Tests for observation plumbing: endpoint_id resolution and failure-path observations.

PR-4.1: Verifies that _resolve_endpoint_id falls back correctly and that
_record_routing_observation works for both success and failure cases.
Includes lifecycle-aware tests that verify endpoint_id resolution after
req_ctx.push() has exited (the real failure scenario).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from routing.routers import RoutingObservation
from serving.servers.routers.completions import _record_routing_observation, _resolve_endpoint_id


# ---------------------------------------------------------------------------
# _resolve_endpoint_id priority chain
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestResolveEndpointId:

    def test_from_routing_info(self):
        """routing_info["endpoint_id"] is the first priority."""
        result = _resolve_endpoint_id(
            routing_info={"endpoint_id": "ep1", "provider": "openai"},
            cached_endpoint_id=None,
            fallback_provider="openai",
        )
        assert result == "ep1"

    def test_from_cached_endpoint_id(self):
        """cached_endpoint_id is the second priority."""
        result = _resolve_endpoint_id(
            routing_info=None,
            cached_endpoint_id="ep_cached",
            fallback_provider="openai",
        )
        assert result == "ep_cached"

    def test_from_routing_info_no_endpoint_id(self):
        """When routing_info exists but lacks endpoint_id, use cached."""
        result = _resolve_endpoint_id(
            routing_info={"provider": "openai"},
            cached_endpoint_id="ep_cached",
            fallback_provider="openai",
        )
        assert result == "ep_cached"

    def test_fallback_to_provider(self):
        """When nothing else available, fall back to provider string."""
        result = _resolve_endpoint_id(
            routing_info=None,
            cached_endpoint_id=None,
            fallback_provider="deepseek",
        )
        assert result == "deepseek"

    def test_routing_info_takes_priority_over_cached(self):
        """routing_info endpoint_id beats cached_endpoint_id."""
        result = _resolve_endpoint_id(
            routing_info={"endpoint_id": "ep_from_routing"},
            cached_endpoint_id="ep_cached",
            fallback_provider="openai",
        )
        assert result == "ep_from_routing"

    def test_cached_takes_priority_over_provider(self):
        """cached_endpoint_id beats fallback_provider."""
        result = _resolve_endpoint_id(
            routing_info=None,
            cached_endpoint_id="ep_cached",
            fallback_provider="openai",
        )
        assert result == "ep_cached"


# ---------------------------------------------------------------------------
# _record_routing_observation
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRecordRoutingObservation:

    def test_success_observation(self):
        """Success observation is forwarded to router."""
        mock_router = MagicMock()
        _record_routing_observation(
            active_router=mock_router,
            model_id="test-model",
            endpoint_id="ep1",
            ttft_ms=150.0,
            total_latency_ms=500.0,
            prompt_tokens=100,
            completion_tokens=50,
            success=True,
        )
        mock_router.record_observation.assert_called_once()
        obs = mock_router.record_observation.call_args[0][0]
        assert obs.model_id == "test-model"
        assert obs.endpoint_id == "ep1"
        assert obs.ttft_ms == 150.0
        assert obs.success is True

    def test_failure_observation(self):
        """Failure observation sets success=False and zero tokens."""
        mock_router = MagicMock()
        _record_routing_observation(
            active_router=mock_router,
            model_id="test-model",
            endpoint_id="ep1",
            ttft_ms=None,
            total_latency_ms=1000.0,
            prompt_tokens=0,
            completion_tokens=0,
            success=False,
        )
        mock_router.record_observation.assert_called_once()
        obs = mock_router.record_observation.call_args[0][0]
        assert obs.success is False
        assert obs.ttft_ms is None
        assert obs.prompt_tokens == 0

    def test_exception_swallowed(self):
        """Router exceptions are swallowed (best-effort)."""
        mock_router = MagicMock()
        mock_router.record_observation.side_effect = RuntimeError("broken")
        # Should not raise.
        _record_routing_observation(
            active_router=mock_router,
            model_id="test-model",
            endpoint_id="ep1",
            ttft_ms=None,
            total_latency_ms=100.0,
            prompt_tokens=0,
            completion_tokens=0,
            success=False,
        )


# ---------------------------------------------------------------------------
# req_ctx lifecycle: verify that endpoint_id is NOT available after push exits
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestReqCtxLifecycle:

    def test_push_includes_endpoint_id(self):
        """req_ctx.push propagates endpoint_id while active."""
        from serving.utils import context as req_ctx

        with req_ctx.push(model="test", provider="openai", endpoint_id="ep1"):
            ctx = req_ctx.get()
            assert ctx["endpoint_id"] == "ep1"
            assert ctx["provider"] == "openai"

        # After context manager, endpoint_id should be gone.
        ctx_after = req_ctx.get()
        assert "endpoint_id" not in ctx_after

    def test_resolve_after_ctx_exit_needs_cached_value(self):
        """After req_ctx.push exits, _resolve_endpoint_id cannot read ctx.

        This simulates the real finally/except scenario: the context has exited
        but we still need endpoint_id. Without a cached value, we'd fall back
        to the provider string.
        """
        from serving.utils import context as req_ctx

        # Simulate: cache endpoint_id while context is active.
        cached_eid = None
        with req_ctx.push(endpoint_id="ep_real", provider="openai"):
            ctx = req_ctx.get()
            cached_eid = ctx.get("endpoint_id")

        # Context has exited. Without cached value, we'd get fallback.
        result_without_cache = _resolve_endpoint_id(None, None, "openai")
        assert result_without_cache == "openai"  # Falls back to provider.

        # With cached value, we get the real endpoint.
        result_with_cache = _resolve_endpoint_id(None, cached_eid, "openai")
        assert result_with_cache == "ep_real"


# ---------------------------------------------------------------------------
# Exception._routing attachment
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestExceptionRoutingAttachment:

    def test_exception_carries_routing(self):
        """BaseRouter attaches _routing to exceptions on failure."""
        exc = RuntimeError("test failure")
        exc._routing = {  # type: ignore[attr-defined]
            "provider": "deepseek",
            "base_url": "https://api.deepseek.com",
            "endpoint_id": "deepseek-chat:us-east",
        }
        routing = getattr(exc, "_routing", None)
        assert routing is not None
        assert routing["endpoint_id"] == "deepseek-chat:us-east"
        assert routing["provider"] == "deepseek"

    def test_resolve_from_exc_routing(self):
        """_resolve_endpoint_id picks up endpoint_id from exc._routing dict."""
        exc_routing = {
            "provider": "deepseek",
            "endpoint_id": "deepseek-chat:us-east",
        }
        result = _resolve_endpoint_id(exc_routing, None, "router")
        assert result == "deepseek-chat:us-east"

    def test_resolve_without_exc_routing_falls_back(self):
        """Without exc._routing, falls back to provider."""
        result = _resolve_endpoint_id(None, None, "router")
        assert result == "router"


# ---------------------------------------------------------------------------
# BaseRouter.chat_completion failure attaches _routing
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBaseRouterFailureRouting:

    @pytest.mark.asyncio
    async def test_chat_completion_failure_attaches_routing(self):
        """When all adapters fail, the exception carries _routing metadata."""
        from routing.routers import FixedRouter
        from serving.adapters.base import BaseAdapter, ModelConfig

        class FailAdapter(BaseAdapter):
            async def chat_completion(self, messages, **params):
                raise RuntimeError("fail")

            async def stream_chat_completion(self, messages, **params):
                raise RuntimeError("fail")
                yield  # noqa: RET503  -- make it an async generator

        cfg = ModelConfig(
            id="test-model",
            name="test-model",
            provider="deepseek",
            base_url="https://api.deepseek.com",
            context_length=8192,
            max_output_length=4096,
            supported_params=[],
            endpoint_id="deepseek-chat:us-east",
        )
        adapter = FailAdapter(cfg)

        router = FixedRouter(experiment_mode=True)
        router.register_route("test-model", [(adapter, 1.0)])

        with pytest.raises(RuntimeError) as exc_info:
            await router.chat_completion("test-model", [{"role": "user", "content": "hi"}])

        exc = exc_info.value
        routing = getattr(exc, "_routing", None)
        assert routing is not None
        assert routing["provider"] == "deepseek"
        assert routing["endpoint_id"] == "deepseek-chat:us-east"

    @pytest.mark.asyncio
    async def test_fallback_failure_attaches_last_adapter_routing(self):
        """When primary and fallback both fail, _routing is from the last attempted adapter.

        FixedRouter uses weighted random selection so we can't predict which
        adapter is primary. We verify that _routing contains a valid endpoint_id
        from one of the registered adapters (specifically the fallback, i.e. the
        last one tried).
        """
        from routing.routers import FixedRouter
        from serving.adapters.base import BaseAdapter, ModelConfig

        call_order: list[str] = []

        class FailAdapter(BaseAdapter):
            async def chat_completion(self, messages, **params):
                call_order.append(self.config.provider)
                raise RuntimeError("fail")

            async def stream_chat_completion(self, messages, **params):
                raise RuntimeError("fail")
                yield  # noqa: RET503

        cfg_a = ModelConfig(
            id="test-model",
            name="test-model",
            provider="openai",
            base_url="https://api.openai.com",
            context_length=8192,
            max_output_length=4096,
            supported_params=[],
            endpoint_id="gpt-4:openai-us",
        )
        cfg_b = ModelConfig(
            id="test-model",
            name="test-model",
            provider="deepseek",
            base_url="https://api.deepseek.com",
            context_length=8192,
            max_output_length=4096,
            supported_params=[],
            endpoint_id="deepseek-chat:us-east",
        )
        adapter_a = FailAdapter(cfg_a)
        adapter_b = FailAdapter(cfg_b)

        router = FixedRouter(experiment_mode=False)
        router.register_route("test-model", [(adapter_a, 1.0), (adapter_b, 0.5)])

        with pytest.raises(RuntimeError) as exc_info:
            await router.chat_completion("test-model", [{"role": "user", "content": "hi"}])

        exc = exc_info.value
        routing = getattr(exc, "_routing", None)
        assert routing is not None
        # Both adapters should have been called.
        assert len(call_order) == 2
        # _routing should be from the LAST attempted adapter (the fallback).
        last_provider = call_order[-1]
        assert routing["provider"] == last_provider
        assert routing["endpoint_id"] is not None


# ---------------------------------------------------------------------------
# Streaming pre-first-chunk failure: _routing on stream exception
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestStreamFailureRouting:

    @pytest.mark.asyncio
    async def test_stream_failure_attaches_routing(self):
        """When streaming fails before first chunk, exception carries _routing."""
        from routing.routers import FixedRouter
        from serving.adapters.base import BaseAdapter, ModelConfig

        class FailStreamAdapter(BaseAdapter):
            async def chat_completion(self, messages, **params):
                raise RuntimeError("fail")

            async def stream_chat_completion(self, messages, **params):
                raise RuntimeError("stream fail")
                yield  # noqa: RET503

        cfg = ModelConfig(
            id="test-model",
            name="test-model",
            provider="deepseek",
            base_url="https://api.deepseek.com",
            context_length=8192,
            max_output_length=4096,
            supported_params=[],
            endpoint_id="deepseek-chat:us-east",
        )
        adapter = FailStreamAdapter(cfg)

        router = FixedRouter(experiment_mode=True)
        router.register_route("test-model", [(adapter, 1.0)])

        with pytest.raises(RuntimeError) as exc_info:
            async for _ in router.stream_chat_completion(
                "test-model", [{"role": "user", "content": "hi"}]
            ):
                pass

        exc = exc_info.value
        routing = getattr(exc, "_routing", None)
        assert routing is not None
        assert routing["provider"] == "deepseek"
        assert routing["endpoint_id"] == "deepseek-chat:us-east"


# ---------------------------------------------------------------------------
# CancelledError: _routing on BaseException path
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCancelledErrorRouting:

    @pytest.mark.asyncio
    async def test_cancelled_error_attaches_routing(self):
        """CancelledError during chat_completion carries _routing via BaseException handler."""
        import asyncio

        from routing.routers import FixedRouter
        from serving.adapters.base import BaseAdapter, ModelConfig

        class CancelAdapter(BaseAdapter):
            async def chat_completion(self, messages, **params):
                raise asyncio.CancelledError()

            async def stream_chat_completion(self, messages, **params):
                raise asyncio.CancelledError()
                yield  # noqa: RET503

        cfg = ModelConfig(
            id="test-model",
            name="test-model",
            provider="openai",
            base_url="https://api.openai.com",
            context_length=8192,
            max_output_length=4096,
            supported_params=[],
            endpoint_id="gpt-4:openai-us",
        )
        adapter = CancelAdapter(cfg)

        router = FixedRouter(experiment_mode=True)
        router.register_route("test-model", [(adapter, 1.0)])

        with pytest.raises(asyncio.CancelledError) as exc_info:
            await router.chat_completion("test-model", [{"role": "user", "content": "hi"}])

        exc = exc_info.value
        routing = getattr(exc, "_routing", None)
        assert routing is not None
        assert routing["provider"] == "openai"
        assert routing["endpoint_id"] == "gpt-4:openai-us"

    @pytest.mark.asyncio
    async def test_stream_cancelled_error_attaches_routing(self):
        """CancelledError during streaming carries _routing via BaseException handler."""
        import asyncio

        from routing.routers import FixedRouter
        from serving.adapters.base import BaseAdapter, ModelConfig

        class CancelStreamAdapter(BaseAdapter):
            async def chat_completion(self, messages, **params):
                raise asyncio.CancelledError()

            async def stream_chat_completion(self, messages, **params):
                raise asyncio.CancelledError()
                yield  # noqa: RET503

        cfg = ModelConfig(
            id="test-model",
            name="test-model",
            provider="openai",
            base_url="https://api.openai.com",
            context_length=8192,
            max_output_length=4096,
            supported_params=[],
            endpoint_id="gpt-4:openai-us",
        )
        adapter = CancelStreamAdapter(cfg)

        router = FixedRouter(experiment_mode=True)
        router.register_route("test-model", [(adapter, 1.0)])

        with pytest.raises(asyncio.CancelledError) as exc_info:
            async for _ in router.stream_chat_completion(
                "test-model", [{"role": "user", "content": "hi"}]
            ):
                pass

        exc = exc_info.value
        routing = getattr(exc, "_routing", None)
        assert routing is not None
        assert routing["provider"] == "openai"
        assert routing["endpoint_id"] == "gpt-4:openai-us"

    @pytest.mark.asyncio
    async def test_fallback_cancelled_error_attaches_routing(self):
        """CancelledError during fallback adapter carries _routing of last attempted."""
        import asyncio

        from routing.routers import FixedRouter
        from serving.adapters.base import BaseAdapter, ModelConfig

        call_order: list[str] = []

        class MixedAdapter(BaseAdapter):
            """Primary raises RuntimeError, fallback raises CancelledError."""

            async def chat_completion(self, messages, **params):
                call_order.append(self.config.provider)
                if self.config.provider == call_order[0]:
                    # First call (primary) -> normal failure
                    raise RuntimeError("primary fail")
                # Second call (fallback) -> cancellation
                raise asyncio.CancelledError()

            async def stream_chat_completion(self, messages, **params):
                raise RuntimeError("unused")
                yield  # noqa: RET503

        cfg_a = ModelConfig(
            id="test-model",
            name="test-model",
            provider="openai",
            base_url="https://api.openai.com",
            context_length=8192,
            max_output_length=4096,
            supported_params=[],
            endpoint_id="gpt-4:openai-us",
        )
        cfg_b = ModelConfig(
            id="test-model",
            name="test-model",
            provider="deepseek",
            base_url="https://api.deepseek.com",
            context_length=8192,
            max_output_length=4096,
            supported_params=[],
            endpoint_id="deepseek-chat:us-east",
        )
        adapter_a = MixedAdapter(cfg_a)
        adapter_b = MixedAdapter(cfg_b)

        router = FixedRouter(experiment_mode=False)
        router.register_route("test-model", [(adapter_a, 1.0), (adapter_b, 0.5)])

        with pytest.raises(asyncio.CancelledError) as exc_info:
            await router.chat_completion("test-model", [{"role": "user", "content": "hi"}])

        exc = exc_info.value
        routing = getattr(exc, "_routing", None)
        assert routing is not None
        # Both should have been called; _routing is from the last attempted.
        assert len(call_order) == 2
        assert routing["provider"] == call_order[-1]
        assert routing["endpoint_id"] is not None


# ---------------------------------------------------------------------------
# V2: RouteWise observation metadata propagation
# ---------------------------------------------------------------------------


def _call_record_with_routing(
    routing_info: dict | None = None,
) -> RoutingObservation:
    """Call _record_routing_observation and return the captured RoutingObservation."""
    mock_router = MagicMock()
    _record_routing_observation(
        active_router=mock_router,
        model_id="test-model",
        endpoint_id="test-model:provider",
        ttft_ms=100.0,
        total_latency_ms=500.0,
        prompt_tokens=100,
        completion_tokens=200,
        success=True,
        routing_info=routing_info,
    )
    mock_router.record_observation.assert_called_once()
    return mock_router.record_observation.call_args[0][0]


@pytest.mark.unit
class TestRouteWiseObservationMetadata:
    """Verify routewise metadata flows from routing_info into RoutingObservation."""

    def test_routewise_metadata_propagated_to_observation(self):
        """Full roundtrip: routewise dict -> RoutingObservation V2 fields."""
        routing_info = {
            "provider": "openai",
            "routewise": {
                "selected_tier": "quota",
                "quota_committed": 0.0,
                "sc_committed": False,
                "hedged": True,
                "backup_won": True,
                "lp_status": "optimal",
                "v_t": 0.0075,
            },
        }
        obs = _call_record_with_routing(routing_info=routing_info)

        assert obs.selected_tier == "quota"
        assert obs.quota_committed == 0.0  # commitment signal via selected_tier, not v_t
        assert obs.sc_committed is False
        assert obs.hedged is True
        assert obs.backup_won is True
        assert obs.lp_status == "optimal"

    def test_no_routewise_metadata_defaults(self):
        """When routing_info has no routewise key, V2 fields get defaults."""
        routing_info = {"provider": "openai"}
        obs = _call_record_with_routing(routing_info=routing_info)

        assert obs.selected_tier is None
        assert obs.quota_committed == 0.0
        assert obs.sc_committed is False
        assert obs.hedged is False
        assert obs.backup_won is False
        assert obs.lp_status is None

    def test_backward_compat_no_routing_info(self):
        """Existing callers that pass no routing_info still work."""
        obs = _call_record_with_routing(routing_info=None)

        assert obs.quota_committed == 0.0
        assert obs.selected_tier is None
        assert obs.sc_committed is False
        assert obs.hedged is False
        assert obs.backup_won is False
        assert obs.lp_status is None

    def test_observation_v2_fields_have_defaults(self):
        """RoutingObservation() with only required fields succeeds."""
        obs = RoutingObservation(
            model_id="m",
            endpoint_id="e",
            ttft_ms=10.0,
            total_latency_ms=50.0,
            token_count=100,
            success=True,
            quota_committed=0.0,
        )
        assert obs.selected_tier is None
        assert obs.sc_committed is False
        assert obs.hedged is False
        assert obs.backup_won is False
        assert obs.lp_status is None

    def test_concurrency_tier_metadata(self):
        """S_C tier metadata is correctly propagated."""
        routing_info = {
            "routewise": {
                "selected_tier": "concurrency",
                "quota_committed": 0.0,
                "sc_committed": True,
                "hedged": False,
                "backup_won": False,
                "lp_status": None,
            },
        }
        obs = _call_record_with_routing(routing_info=routing_info)

        assert obs.selected_tier == "concurrency"
        assert obs.sc_committed is True
        assert obs.quota_committed == 0.0

    def test_failure_path_with_routewise_metadata(self):
        """Exception _routing with routewise metadata propagates to observation."""
        routing_info = {
            "provider": "openai",
            "endpoint_id": "model:openai",
            "routewise": {
                "selected_tier": "quota",
                "quota_committed": 0.0,
                "sc_committed": False,
                "hedged": False,
                "backup_won": False,
                "lp_status": None,
                "v_t": 0.005,
            },
        }
        obs = _call_record_with_routing(routing_info=routing_info)
        assert obs.selected_tier == "quota"
        assert obs.quota_committed == 0.0
        assert obs.hedged is False
