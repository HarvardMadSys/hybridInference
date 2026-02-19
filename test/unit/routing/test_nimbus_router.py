"""Unit tests for NimbusRouter.

Tests the NimbusRouter class which manages hybrid SLO-aware routing
by inheriting BaseRouter infrastructure (circuit breaker, health, fallback)
and delegating outsourcing decisions to OutsourcingRouter.decide() for
Nimbus-enabled models, while falling back to FixedRouter for other models.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# Add project root to Python path
project_root = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(project_root))

import pytest

from routing.routers import FixedRouter, NimbusRouter, RouteConfig
from serving.adapters.base import BaseAdapter, ModelConfig


# Test Fixtures


def _make_adapter(adapter_id: str, provider: str = "test", base_url: str | None = None) -> BaseAdapter:
    """Create a mock adapter for testing."""
    if base_url is None:
        base_url = f"http://{provider}.test"
    config = ModelConfig(
        id=adapter_id,
        name=adapter_id,
        provider=provider,
        base_url=base_url,
        context_length=8192,
        max_output_length=4096,
    )
    adapter = MagicMock(spec=BaseAdapter)
    adapter.config = config
    adapter.chat_completion = AsyncMock(
        return_value={"choices": [{"message": {"content": "test"}}]}
    )
    adapter.stream_chat_completion = AsyncMock()
    return adapter


@pytest.fixture
def mock_settings():
    """Create mock settings."""
    settings = MagicMock()
    settings.nimbus_enabled_models = []
    settings.routing_strategy = "nimbus"
    settings.experiment_mode = False
    return settings


@pytest.fixture
def local_adapter():
    """Create a local adapter."""
    return _make_adapter("glm-4.6-local", provider="sglang", base_url="http://localhost:8001")


@pytest.fixture
def remote_adapter():
    """Create a remote adapter."""
    return _make_adapter("glm-4.6-remote", provider="zhipu", base_url="https://api.zhipu.ai")


def _make_mock_outsourcing_router(local_adapter, remote_adapter):
    """Create a mock OutsourcingRouter with decide() support."""
    from routing.outsourcing_integration import OutsourcingRouter

    router = MagicMock(spec=OutsourcingRouter)
    router.local_adapter = local_adapter
    router.remote_adapter = remote_adapter

    # decide() returns routing decision pointing to local adapter by default
    router.decide.return_value = {
        "adapter": local_adapter,
        "routing_decision": "local",
        "request_id": "req-test-1",
        "reason": "No SLO violations detected",
        "cached_tokens": 0,
        "model_id": "glm-4.6",
        "queue_length": 0,
        "decision": MagicMock(),
    }

    router.chat_completion = AsyncMock(
        return_value={"choices": [{"message": {"content": "test"}}]}
    )
    router.stream_chat_completion = AsyncMock()
    return router


@pytest.fixture
def mock_outsourcing_router(local_adapter, remote_adapter):
    """Create a mock OutsourcingRouter with decide() support."""
    return _make_mock_outsourcing_router(local_adapter, remote_adapter)


@pytest.fixture
def fixed_router():
    """Create a FixedRouter for testing."""
    router = FixedRouter(experiment_mode=False)
    adapter = _make_adapter("other-model", provider="test")
    router.register_route("other-model", [(adapter, 1.0)])
    return router


# Test Classes


class TestNimbusRouterInitialization:
    """Test NimbusRouter initialization."""

    def test_init_creates_empty_routers(self, fixed_router, mock_settings):
        """Test NimbusRouter initializes with empty outsourcing_routers."""
        router = NimbusRouter(fixed_router=fixed_router, settings=mock_settings)

        assert isinstance(router.outsourcing_routers, dict)
        assert len(router.outsourcing_routers) == 0
        assert router.fixed_router is fixed_router

    def test_init_stores_settings(self, fixed_router, mock_settings):
        """Test NimbusRouter stores settings."""
        router = NimbusRouter(fixed_router=fixed_router, settings=mock_settings)

        assert router.settings is mock_settings

    def test_init_with_fixed_router(self, fixed_router, mock_settings):
        """Test NimbusRouter uses provided FixedRouter."""
        router = NimbusRouter(fixed_router=fixed_router, settings=mock_settings)

        assert router.fixed_router is fixed_router
        assert "other-model" in router.fixed_router.routes

    def test_inherits_base_router(self, fixed_router, mock_settings):
        """Test NimbusRouter inherits from BaseRouter."""
        from routing.routers import BaseRouter

        router = NimbusRouter(fixed_router=fixed_router, settings=mock_settings)

        assert isinstance(router, BaseRouter)
        assert hasattr(router, "_health")
        assert hasattr(router, "_circuits")
        assert hasattr(router, "experiment_mode")


class TestNimbusRouterRegistration:
    """Test model registration in NimbusRouter."""

    def test_register_nimbus_model(self, fixed_router, mock_settings, mock_outsourcing_router):
        """Test registering a Nimbus-enabled model."""
        router = NimbusRouter(fixed_router=fixed_router, settings=mock_settings)

        router.outsourcing_routers["glm-4.6"] = mock_outsourcing_router

        assert "glm-4.6" in router.outsourcing_routers
        assert router.outsourcing_routers["glm-4.6"] is mock_outsourcing_router

    def test_register_fixed_model(self, fixed_router, mock_settings):
        """Test registering a non-Nimbus model."""
        router = NimbusRouter(fixed_router=fixed_router, settings=mock_settings)
        adapter = _make_adapter("another-model", provider="test")

        router.fixed_router.register_route("another-model", [(adapter, 1.0)])

        assert "another-model" in router.fixed_router.routes
        assert len(router.fixed_router.routes["another-model"].adapters) == 1


class TestNimbusRouterChatCompletion:
    """Test chat_completion method via BaseRouter infrastructure."""

    @pytest.mark.asyncio
    async def test_chat_completion_nimbus_model(
        self, fixed_router, mock_settings, local_adapter, remote_adapter
    ):
        """Test chat completion for Nimbus-enabled model uses decide()."""
        router = NimbusRouter(fixed_router=fixed_router, settings=mock_settings)
        mock_or = _make_mock_outsourcing_router(local_adapter, remote_adapter)
        router.outsourcing_routers["glm-4.6"] = mock_or

        messages = [{"role": "user", "content": "Hello"}]
        result = await router.chat_completion("glm-4.6", messages)

        # Should have called decide() on OutsourcingRouter
        mock_or.decide.assert_called_once()
        # Should have called the adapter returned by decide()
        local_adapter.chat_completion.assert_awaited_once()
        assert result is not None
        # Should include _routing metadata from BaseRouter
        assert "_routing" in result
        assert result["_routing"]["provider"] == "sglang"
        # Should include outsourcing metadata
        assert "outsourcing" in result["_routing"]
        assert result["_routing"]["outsourcing"]["decision"] == "local"

    @pytest.mark.asyncio
    async def test_chat_completion_fixed_model(self, fixed_router, mock_settings):
        """Test chat completion for non-Nimbus model."""
        router = NimbusRouter(fixed_router=fixed_router, settings=mock_settings)

        messages = [{"role": "user", "content": "Hello"}]
        result = await router.chat_completion("other-model", messages)

        # Should delegate to FixedRouter
        assert result is not None

    @pytest.mark.asyncio
    async def test_chat_completion_unknown_model(self, fixed_router, mock_settings):
        """Test chat completion for unknown model raises error."""
        router = NimbusRouter(fixed_router=fixed_router, settings=mock_settings)

        messages = [{"role": "user", "content": "Hello"}]

        with pytest.raises(ValueError, match="No route configured"):
            await router.chat_completion("unknown-model", messages)

    @pytest.mark.asyncio
    async def test_chat_completion_outsourced_decision(
        self, fixed_router, mock_settings, local_adapter, remote_adapter
    ):
        """Test chat completion when outsourcing decision routes to remote."""
        router = NimbusRouter(fixed_router=fixed_router, settings=mock_settings)
        mock_or = _make_mock_outsourcing_router(local_adapter, remote_adapter)
        # Configure decide() to return remote adapter
        mock_or.decide.return_value = {
            "adapter": remote_adapter,
            "routing_decision": "outsourced",
            "request_id": "req-test-2",
            "reason": "SLO violations detected",
            "cached_tokens": 0,
            "model_id": "glm-4.6",
            "queue_length": 3,
            "decision": MagicMock(),
        }
        router.outsourcing_routers["glm-4.6"] = mock_or

        messages = [{"role": "user", "content": "Hello"}]
        result = await router.chat_completion("glm-4.6", messages)

        # Should have called remote adapter
        remote_adapter.chat_completion.assert_awaited_once()
        assert result["_routing"]["provider"] == "zhipu"
        assert result["_routing"]["outsourcing"]["decision"] == "outsourced"


class TestNimbusRouterStreamCompletion:
    """Test stream_chat_completion method."""

    @pytest.mark.asyncio
    async def test_stream_completion_nimbus_model(
        self, fixed_router, mock_settings, local_adapter, remote_adapter
    ):
        """Test streaming for Nimbus-enabled model with routing metadata chunk."""
        router = NimbusRouter(fixed_router=fixed_router, settings=mock_settings)
        mock_or = _make_mock_outsourcing_router(local_adapter, remote_adapter)
        router.outsourcing_routers["glm-4.6"] = mock_or

        # Mock stream generator on the adapter
        async def mock_stream(messages, **params):
            yield "data: chunk1\n\n"
            yield "data: chunk2\n\n"

        local_adapter.stream_chat_completion = mock_stream

        messages = [{"role": "user", "content": "Hello"}]
        chunks = []
        async for chunk in router.stream_chat_completion("glm-4.6", messages):
            chunks.append(chunk)

        # Should get 2 content chunks + 1 routing metadata chunk
        assert len(chunks) == 3
        # Last chunk should be routing metadata
        last_chunk = chunks[-1]
        assert last_chunk.startswith("data: ")
        routing_data = json.loads(last_chunk[6:].strip())
        assert "_routing" in routing_data
        assert routing_data["_routing"]["outsourcing"]["decision"] == "local"

    @pytest.mark.asyncio
    async def test_stream_content_containing_done_not_swallowed(
        self, fixed_router, mock_settings, local_adapter, remote_adapter
    ):
        """Test that content chunks containing literal '[DONE]' are NOT swallowed."""
        router = NimbusRouter(fixed_router=fixed_router, settings=mock_settings)
        mock_or = _make_mock_outsourcing_router(local_adapter, remote_adapter)
        router.outsourcing_routers["glm-4.6"] = mock_or

        async def mock_stream(messages, **params):
            # Content chunk whose text happens to contain [DONE]
            yield 'data: {"choices":[{"delta":{"content":"print [DONE] marker"}}]}\n\n'
            yield "data: chunk2\n\n"
            yield "data: [DONE]\n\n"

        local_adapter.stream_chat_completion = mock_stream

        messages = [{"role": "user", "content": "Hello"}]
        chunks = []
        async for chunk in router.stream_chat_completion("glm-4.6", messages):
            chunks.append(chunk)

        # Should get: content_with_DONE + chunk2 + routing_metadata + real_DONE = 4 chunks
        assert len(chunks) == 4
        # First chunk (with [DONE] in content) must NOT be swallowed
        assert "[DONE]" in chunks[0]
        assert "print" in chunks[0]
        # Last chunk should be the real [DONE] sentinel
        assert chunks[-1].strip() == "data: [DONE]"

    @pytest.mark.asyncio
    async def test_stream_exception_cleans_pending_decisions(
        self, fixed_router, mock_settings, local_adapter, remote_adapter
    ):
        """Test that _pending_decisions is cleaned up on stream exception."""
        settings = MagicMock()
        settings.nimbus_enabled_models = []
        settings.experiment_mode = True  # Disable fallback so exception propagates

        router = NimbusRouter(fixed_router=fixed_router, settings=settings)
        mock_or = _make_mock_outsourcing_router(local_adapter, remote_adapter)
        router.outsourcing_routers["glm-4.6"] = mock_or

        async def failing_stream(messages, **params):
            yield "data: chunk1\n\n"
            raise RuntimeError("Stream interrupted")

        local_adapter.stream_chat_completion = failing_stream

        messages = [{"role": "user", "content": "Hello"}]
        with pytest.raises(RuntimeError, match="Stream interrupted"):
            async for _ in router.stream_chat_completion("glm-4.6", messages):
                pass

        # _pending_decisions should be empty (cleaned up despite exception)
        assert len(router._pending_decisions) == 0

    @pytest.mark.asyncio
    async def test_stream_completion_fixed_model(self, fixed_router, mock_settings):
        """Test streaming for non-Nimbus model."""
        router = NimbusRouter(fixed_router=fixed_router, settings=mock_settings)

        # Get the adapter from fixed_router and set up its stream method
        adapter = fixed_router.routes["other-model"].adapters[0][0]

        # Create async generator for streaming
        async def mock_stream(messages, **params):
            yield "data: chunk1\n\n"
            yield "data: chunk2\n\n"

        adapter.stream_chat_completion = mock_stream

        messages = [{"role": "user", "content": "Hello"}]
        chunks = []
        async for chunk in router.stream_chat_completion("other-model", messages):
            chunks.append(chunk)

        # Should delegate to FixedRouter and stream from adapter (no extra routing chunk)
        assert len(chunks) == 2


class TestNimbusRouterFallback:
    """Test circuit breaker and fallback behavior inherited from BaseRouter."""

    @pytest.mark.asyncio
    async def test_fallback_local_to_remote(
        self, fixed_router, mock_settings, local_adapter, remote_adapter
    ):
        """Test that when local adapter fails, falls back to remote."""
        router = NimbusRouter(fixed_router=fixed_router, settings=mock_settings)
        mock_or = _make_mock_outsourcing_router(local_adapter, remote_adapter)
        router.outsourcing_routers["glm-4.6"] = mock_or

        # Make local adapter raise an exception
        local_adapter.chat_completion = AsyncMock(side_effect=RuntimeError("SGLang down"))

        messages = [{"role": "user", "content": "Hello"}]
        result = await router.chat_completion("glm-4.6", messages)

        # Should have fallen back to remote adapter
        remote_adapter.chat_completion.assert_awaited_once()
        assert result is not None
        assert result["_routing"]["fallback"] is True

    @pytest.mark.asyncio
    async def test_no_fallback_in_experiment_mode(
        self, fixed_router, local_adapter, remote_adapter
    ):
        """Test that experiment_mode disables fallback."""
        settings = MagicMock()
        settings.nimbus_enabled_models = []
        settings.experiment_mode = True

        router = NimbusRouter(fixed_router=fixed_router, settings=settings)
        mock_or = _make_mock_outsourcing_router(local_adapter, remote_adapter)
        router.outsourcing_routers["glm-4.6"] = mock_or

        # Make local adapter raise an exception
        local_adapter.chat_completion = AsyncMock(side_effect=RuntimeError("SGLang down"))

        messages = [{"role": "user", "content": "Hello"}]
        with pytest.raises(RuntimeError, match="SGLang down"):
            await router.chat_completion("glm-4.6", messages)

    @pytest.mark.asyncio
    async def test_get_fallback_adapters_nimbus(
        self, fixed_router, mock_settings, local_adapter, remote_adapter
    ):
        """Test _get_fallback_adapters returns correct fallback for Nimbus models."""
        router = NimbusRouter(fixed_router=fixed_router, settings=mock_settings)
        mock_or = _make_mock_outsourcing_router(local_adapter, remote_adapter)
        router.outsourcing_routers["glm-4.6"] = mock_or

        # When local fails, should return remote as fallback
        fallbacks = router._get_fallback_adapters("glm-4.6", local_adapter)
        assert len(fallbacks) == 1
        assert fallbacks[0] is remote_adapter

        # When remote fails, should return local as fallback
        fallbacks = router._get_fallback_adapters("glm-4.6", remote_adapter)
        assert len(fallbacks) == 1
        assert fallbacks[0] is local_adapter


class TestNimbusRouterStats:
    """Test statistics collection."""

    def test_get_stats_empty(self, fixed_router, mock_settings):
        """Test get_stats with no Nimbus models."""
        router = NimbusRouter(fixed_router=fixed_router, settings=mock_settings)

        stats = router.get_stats()

        assert isinstance(stats, dict)
        assert len(stats) == 0

    def test_get_stats_with_models(self, fixed_router, mock_settings, mock_outsourcing_router):
        """Test get_stats with Nimbus models."""
        router = NimbusRouter(fixed_router=fixed_router, settings=mock_settings)
        router.outsourcing_routers["glm-4.6"] = mock_outsourcing_router

        # Mock stats
        mock_outsourcing_router.get_stats.return_value = {
            "total_requests": 100,
            "local_requests": 60,
            "remote_requests": 40,
        }

        stats = router.get_stats()

        assert "glm-4.6" in stats
        assert stats["glm-4.6"]["total_requests"] == 100
        assert stats["glm-4.6"]["local_requests"] == 60


class TestNimbusRouterEdgeCases:
    """Test edge cases and error handling."""

    @pytest.mark.asyncio
    async def test_empty_messages(
        self, fixed_router, mock_settings, local_adapter, remote_adapter
    ):
        """Test handling of empty messages."""
        router = NimbusRouter(fixed_router=fixed_router, settings=mock_settings)
        mock_or = _make_mock_outsourcing_router(local_adapter, remote_adapter)
        router.outsourcing_routers["glm-4.6"] = mock_or

        # Should still call decide()
        await router.chat_completion("glm-4.6", [])

        mock_or.decide.assert_called_once()

    @pytest.mark.asyncio
    async def test_params_passed_through(
        self, fixed_router, mock_settings, local_adapter, remote_adapter
    ):
        """Test parameters are passed through to decide() and adapter."""
        router = NimbusRouter(fixed_router=fixed_router, settings=mock_settings)
        mock_or = _make_mock_outsourcing_router(local_adapter, remote_adapter)
        router.outsourcing_routers["glm-4.6"] = mock_or

        messages = [{"role": "user", "content": "Hello"}]
        params = {
            "temperature": 0.7,
            "max_tokens": 100,
            "prefill_slo_seconds": 2.0,
        }

        await router.chat_completion("glm-4.6", messages, **params)

        # Verify decide() was called (params are passed via context)
        mock_or.decide.assert_called_once()
        call_args = mock_or.decide.call_args
        # The params dict is passed as the 4th positional arg
        decide_params = call_args[0][3] if len(call_args[0]) > 3 else call_args[1].get("params", {})
        assert decide_params.get("temperature") == 0.7
        assert decide_params.get("max_tokens") == 100

        # Verify adapter received the params
        call_kwargs = local_adapter.chat_completion.call_args.kwargs
        assert call_kwargs.get("temperature") == 0.7
        assert call_kwargs.get("max_tokens") == 100

    @pytest.mark.asyncio
    async def test_chat_exception_cleans_pending_decisions(
        self, fixed_router, local_adapter, remote_adapter
    ):
        """Test that _pending_decisions is cleaned up on chat_completion exception."""
        settings = MagicMock()
        settings.nimbus_enabled_models = []
        settings.experiment_mode = True  # Disable fallback so exception propagates

        router = NimbusRouter(fixed_router=fixed_router, settings=settings)
        mock_or = _make_mock_outsourcing_router(local_adapter, remote_adapter)
        router.outsourcing_routers["glm-4.6"] = mock_or

        local_adapter.chat_completion = AsyncMock(side_effect=RuntimeError("SGLang crash"))

        messages = [{"role": "user", "content": "Hello"}]
        with pytest.raises(RuntimeError, match="SGLang crash"):
            await router.chat_completion("glm-4.6", messages)

        # _pending_decisions should be empty (cleaned up despite exception)
        assert len(router._pending_decisions) == 0

    @pytest.mark.asyncio
    async def test_explicit_request_id_none_still_works(
        self, fixed_router, mock_settings, local_adapter, remote_adapter
    ):
        """Test that passing request_id=None explicitly still generates a valid ID."""
        router = NimbusRouter(fixed_router=fixed_router, settings=mock_settings)
        mock_or = _make_mock_outsourcing_router(local_adapter, remote_adapter)
        router.outsourcing_routers["glm-4.6"] = mock_or

        messages = [{"role": "user", "content": "Hello"}]
        result = await router.chat_completion("glm-4.6", messages, request_id=None)

        # Should still get outsourcing metadata despite explicit None
        assert "_routing" in result
        assert "outsourcing" in result["_routing"]
        assert result["_routing"]["outsourcing"]["decision"] == "local"

    def test_multiple_nimbus_models(self, fixed_router, mock_settings):
        """Test managing multiple Nimbus-enabled models."""
        router = NimbusRouter(fixed_router=fixed_router, settings=mock_settings)

        router1 = MagicMock()
        router2 = MagicMock()

        router.outsourcing_routers["glm-4.6"] = router1
        router.outsourcing_routers["qwen3"] = router2

        assert len(router.outsourcing_routers) == 2
        assert router.outsourcing_routers["glm-4.6"] is router1
        assert router.outsourcing_routers["qwen3"] is router2


class TestNimbusRouterSelectAdapter:
    """Test _select_adapter method."""

    def test_select_adapter_nimbus_model(
        self, fixed_router, mock_settings, local_adapter, remote_adapter
    ):
        """Test _select_adapter calls decide() for Nimbus models."""
        router = NimbusRouter(fixed_router=fixed_router, settings=mock_settings)
        mock_or = _make_mock_outsourcing_router(local_adapter, remote_adapter)
        router.outsourcing_routers["glm-4.6"] = mock_or

        context = {"messages": [{"role": "user", "content": "test"}], "params": {}}
        adapter = router._select_adapter("glm-4.6", context)

        assert adapter is local_adapter
        mock_or.decide.assert_called_once()

    def test_select_adapter_fixed_model(self, fixed_router, mock_settings):
        """Test _select_adapter delegates to FixedRouter for non-Nimbus models."""
        router = NimbusRouter(fixed_router=fixed_router, settings=mock_settings)

        context = {"messages": [{"role": "user", "content": "test"}], "params": {}}
        adapter = router._select_adapter("other-model", context)

        # Should return an adapter from FixedRouter
        assert adapter is not None

    def test_select_adapter_unknown_model(self, fixed_router, mock_settings):
        """Test _select_adapter returns None for unknown models."""
        router = NimbusRouter(fixed_router=fixed_router, settings=mock_settings)

        context = {"messages": [], "params": {}}
        adapter = router._select_adapter("unknown-model", context)

        assert adapter is None


# ============================================================================
# Tests: Helper Methods (Internal Logic)
# ============================================================================


class TestNimbusRouterHelpers:
    """Test NimbusRouter internal helper methods."""

    def test_extract_adapters_requires_both_local_and_remote(self, fixed_router, mock_settings):
        """Test that _extract_adapters raises ValueError when missing local or remote adapter."""
        # Register a model with only local adapter
        local_adapter = MagicMock()
        local_adapter.config.provider = "sglang"
        local_adapter.config.base_url = "http://localhost:8001"

        fixed_router.register_route("local-only-model", [(local_adapter, 1.0)])

        nimbus_router = NimbusRouter(fixed_router=fixed_router, settings=mock_settings)

        # Should raise ValueError
        with pytest.raises(ValueError, match="requires both local and remote adapters"):
            nimbus_router._extract_adapters("local-only-model")

    def test_extract_adapters_detects_local_by_openai_compat_localhost(
        self, fixed_router, mock_settings
    ):
        """Test that openai_compat with localhost is detected as local adapter."""
        # Create adapters
        local_adapter = MagicMock()
        local_adapter.config.provider = "openai_compat"
        local_adapter.config.base_url = "http://127.0.0.1:8000/v1"

        remote_adapter = MagicMock()
        remote_adapter.config.provider = "zhipu"
        remote_adapter.config.base_url = "https://open.bigmodel.cn"

        fixed_router.register_route("test-model", [(local_adapter, 0.5), (remote_adapter, 0.5)])

        nimbus_router = NimbusRouter(fixed_router=fixed_router, settings=mock_settings)

        # Extract adapters
        extracted_local, extracted_remote = nimbus_router._extract_adapters("test-model")

        # Verify correct classification
        assert extracted_local.config.provider == "openai_compat"
        assert "127.0.0.1" in extracted_local.config.base_url
        assert extracted_remote.config.provider == "zhipu"

    def test_extract_adapters_ignores_extra_adapters(self, fixed_router, mock_settings):
        """Test that _extract_adapters only takes first local and first remote adapter."""
        # Create multiple adapters
        local1 = MagicMock()
        local1.config.provider = "sglang"
        local1.config.base_url = "http://localhost:8001"

        local2 = MagicMock()
        local2.config.provider = "sglang"
        local2.config.base_url = "http://localhost:8002"

        remote = MagicMock()
        remote.config.provider = "zhipu"
        remote.config.base_url = "https://api.zhipu.ai"

        fixed_router.register_route(
            "multi-adapter-model", [(local1, 0.4), (local2, 0.3), (remote, 0.3)]
        )

        nimbus_router = NimbusRouter(fixed_router=fixed_router, settings=mock_settings)

        # Extract adapters
        extracted_local, extracted_remote = nimbus_router._extract_adapters("multi-adapter-model")

        # Should only get first local and first remote
        assert extracted_local.config.base_url == "http://localhost:8001"
        assert extracted_remote.config.provider == "zhipu"

    def test_get_metrics_url_strips_v1_suffix(self, fixed_router, mock_settings):
        """Test that _get_metrics_url strips /v1 suffix from base_url."""
        adapter = MagicMock()
        adapter.config.base_url = "http://localhost:8001/v1"

        nimbus_router = NimbusRouter(fixed_router=fixed_router, settings=mock_settings)

        metrics_url = nimbus_router._get_metrics_url(adapter)

        assert metrics_url == "http://localhost:8001/metrics"

    def test_get_metrics_url_basic_append(self, fixed_router, mock_settings):
        """Test that _get_metrics_url appends /metrics to base_url."""
        adapter = MagicMock()
        adapter.config.base_url = "http://localhost:8001"

        nimbus_router = NimbusRouter(fixed_router=fixed_router, settings=mock_settings)

        metrics_url = nimbus_router._get_metrics_url(adapter)

        assert metrics_url == "http://localhost:8001/metrics"

    def test_get_model_slo_glm(self, fixed_router):
        """Test that _get_model_slo returns correct SLO for GLM models."""
        settings = MagicMock()
        settings.nimbus_enabled_models = []
        settings.glm46_slo_seconds = 1.5
        settings.experiment_mode = False

        nimbus_router = NimbusRouter(fixed_router=fixed_router, settings=settings)

        slo = nimbus_router._get_model_slo("glm-4.6")

        assert slo == 1.5

    def test_get_model_slo_qwen(self, fixed_router):
        """Test that _get_model_slo returns correct SLO for Qwen models."""
        settings = MagicMock()
        settings.nimbus_enabled_models = []
        settings.qwen3_slo_seconds = 2.5
        settings.experiment_mode = False

        nimbus_router = NimbusRouter(fixed_router=fixed_router, settings=settings)

        slo = nimbus_router._get_model_slo("qwen3-coder-30b")

        assert slo == 2.5

    def test_get_model_slo_minimax(self, fixed_router):
        """Test that _get_model_slo returns correct SLO for MiniMax models."""
        settings = MagicMock()
        settings.nimbus_enabled_models = []
        settings.minimax_slo_seconds = 3.0
        settings.experiment_mode = False

        nimbus_router = NimbusRouter(fixed_router=fixed_router, settings=settings)

        slo = nimbus_router._get_model_slo("minimax-m2")

        assert slo == 3.0

    def test_get_model_slo_default_for_unknown(self, fixed_router, mock_settings):
        """Test that _get_model_slo returns default 2.0 for unknown models."""
        nimbus_router = NimbusRouter(fixed_router=fixed_router, settings=mock_settings)

        slo = nimbus_router._get_model_slo("some-unknown-model")

        assert slo == 2.0


# ============================================================================
# Tests: Initialization with Settings
# ============================================================================


class TestNimbusRouterInitializationWithSettings:
    """Test NimbusRouter initialization with settings-driven model registration."""

    @patch("routing.routers.OutsourcingRouter")
    @patch("routing.routers.OutsourcingEngine")
    @patch("routing.routers.SGLangWaitingQueueAdapter")
    @patch("routing.routers.SimpleFLOPCalculator")
    def test_init_routers_creates_outsourcing_router_for_enabled_models(
        self, mock_flop_calc, mock_queue_adapter, mock_engine, mock_outsourcing_router, fixed_router
    ):
        """Test that _init_routers creates OutsourcingRouter for enabled models."""
        # Setup settings
        settings = MagicMock()
        settings.nimbus_enabled_models = ["glm-4.6"]
        settings.glm46_slo_seconds = 1.5
        settings.experiment_mode = False

        # Register model with both local and remote adapters
        local_adapter = MagicMock()
        local_adapter.config.provider = "sglang"
        local_adapter.config.base_url = "http://localhost:8001"

        remote_adapter = MagicMock()
        remote_adapter.config.provider = "zhipu"
        remote_adapter.config.base_url = "https://open.bigmodel.cn"

        fixed_router.register_route("glm-4.6", [(local_adapter, 0.5), (remote_adapter, 0.5)])

        # Create NimbusRouter (will call _init_routers)
        nimbus_router = NimbusRouter(fixed_router=fixed_router, settings=settings)

        # Verify OutsourcingRouter was created
        assert mock_outsourcing_router.called
        call_kwargs = mock_outsourcing_router.call_args.kwargs
        assert call_kwargs["model_id"] == "glm-4.6"
        assert call_kwargs["local_adapter"] == local_adapter
        assert call_kwargs["remote_adapter"] == remote_adapter

        # Verify it's registered
        assert "glm-4.6" in nimbus_router.outsourcing_routers

    @patch("routing.routers.OutsourcingRouter")
    def test_init_routers_skips_model_with_invalid_route(
        self, mock_outsourcing_router, fixed_router
    ):
        """Test that _init_routers skips models with invalid routes (missing adapters)."""
        # Setup settings with a model that has invalid route
        settings = MagicMock()
        settings.nimbus_enabled_models = ["bad-model"]
        settings.experiment_mode = False

        # Register model with only local adapter (missing remote)
        local_adapter = MagicMock()
        local_adapter.config.provider = "sglang"
        local_adapter.config.base_url = "http://localhost:8001"

        fixed_router.register_route("bad-model", [(local_adapter, 1.0)])

        # Create NimbusRouter
        nimbus_router = NimbusRouter(fixed_router=fixed_router, settings=settings)

        # Verify OutsourcingRouter was NOT created
        assert not mock_outsourcing_router.called

        # Verify model is NOT registered
        assert "bad-model" not in nimbus_router.outsourcing_routers

    @patch("routing.routers.OutsourcingRouter")
    def test_init_routers_skips_model_not_in_fixed_router(
        self, mock_outsourcing_router, fixed_router
    ):
        """Test that _init_routers skips models not found in FixedRouter."""
        # Setup settings with a model that doesn't exist in FixedRouter
        settings = MagicMock()
        settings.nimbus_enabled_models = ["non-existent-model"]
        settings.experiment_mode = False

        # Create NimbusRouter
        nimbus_router = NimbusRouter(fixed_router=fixed_router, settings=settings)

        # Verify OutsourcingRouter was NOT created
        assert not mock_outsourcing_router.called

        # Verify model is NOT registered
        assert "non-existent-model" not in nimbus_router.outsourcing_routers

    @patch("routing.routers.OutsourcingRouter")
    @patch("routing.routers.OutsourcingEngine")
    @patch("routing.routers.SGLangWaitingQueueAdapter")
    @patch("routing.routers.SimpleFLOPCalculator")
    def test_init_routers_handles_multiple_models(
        self, mock_flop_calc, mock_queue_adapter, mock_engine, mock_outsourcing_router, fixed_router
    ):
        """Test that _init_routers correctly initializes multiple Nimbus models."""
        # Setup settings with multiple models
        settings = MagicMock()
        settings.nimbus_enabled_models = ["glm-4.6", "qwen3-coder-30b"]
        settings.glm46_slo_seconds = 1.5
        settings.qwen3_slo_seconds = 2.5
        settings.experiment_mode = False

        # Register both models
        for model_id in ["glm-4.6", "qwen3-coder-30b"]:
            local_adapter = MagicMock()
            local_adapter.config.provider = "sglang"
            local_adapter.config.base_url = f"http://localhost:8001/{model_id}"

            remote_adapter = MagicMock()
            remote_adapter.config.provider = "zhipu"
            remote_adapter.config.base_url = "https://api.zhipu.ai"

            fixed_router.register_route(model_id, [(local_adapter, 0.5), (remote_adapter, 0.5)])

        # Create NimbusRouter
        nimbus_router = NimbusRouter(fixed_router=fixed_router, settings=settings)

        # Verify both models are registered
        assert "glm-4.6" in nimbus_router.outsourcing_routers
        assert "qwen3-coder-30b" in nimbus_router.outsourcing_routers

        # Verify OutsourcingRouter was called twice
        assert mock_outsourcing_router.call_count == 2


# ============================================================================
# Tests: Metadata Handling
# ============================================================================


class TestNimbusRouterMetadata:
    """Test _routing metadata handling (Fix 3 verification)."""

    @pytest.mark.asyncio
    async def test_no_outsourcing_key_in_response(
        self, fixed_router, mock_settings, local_adapter, remote_adapter
    ):
        """Test that response uses _routing, not _outsourcing."""
        router = NimbusRouter(fixed_router=fixed_router, settings=mock_settings)
        mock_or = _make_mock_outsourcing_router(local_adapter, remote_adapter)
        router.outsourcing_routers["glm-4.6"] = mock_or

        messages = [{"role": "user", "content": "Hello"}]
        result = await router.chat_completion("glm-4.6", messages)

        # Should have _routing but NOT _outsourcing
        assert "_routing" in result
        assert "_outsourcing" not in result

    @pytest.mark.asyncio
    async def test_routing_metadata_has_provider_and_base_url(
        self, fixed_router, mock_settings, local_adapter, remote_adapter
    ):
        """Test that _routing includes provider and base_url from BaseRouter."""
        router = NimbusRouter(fixed_router=fixed_router, settings=mock_settings)
        mock_or = _make_mock_outsourcing_router(local_adapter, remote_adapter)
        router.outsourcing_routers["glm-4.6"] = mock_or

        messages = [{"role": "user", "content": "Hello"}]
        result = await router.chat_completion("glm-4.6", messages)

        routing = result["_routing"]
        assert "provider" in routing
        assert "base_url" in routing
        assert routing["provider"] == "sglang"
        assert routing["base_url"] == "http://localhost:8001"
