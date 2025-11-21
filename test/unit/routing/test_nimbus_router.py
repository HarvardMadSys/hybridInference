"""Unit tests for NimbusRouter.

Tests the NimbusRouter class which manages hybrid SLO-aware routing
by delegating to OutsourcingRouter for Nimbus-enabled models and
FixedRouter for other models.
"""

from __future__ import annotations

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


def _make_adapter(adapter_id: str, provider: str = "test") -> BaseAdapter:
    """Create a mock adapter for testing."""
    config = ModelConfig(
        id=adapter_id,
        name=adapter_id,
        provider=provider,
        base_url=f"http://{provider}.test",
        context_length=8192,
        max_output_length=4096,
    )
    adapter = MagicMock(spec=BaseAdapter)
    adapter.config = config
    adapter.chat_completion = AsyncMock(return_value={"choices": [{"message": {"content": "test"}}]})
    adapter.stream_chat_completion = AsyncMock()
    return adapter


@pytest.fixture
def mock_settings():
    """Create mock settings."""
    settings = MagicMock()
    settings.nimbus_enabled_models = []
    settings.routing_strategy = "nimbus"
    return settings


@pytest.fixture
def local_adapter():
    """Create a local adapter."""
    return _make_adapter("glm-4.6-local", provider="sglang")


@pytest.fixture
def remote_adapter():
    """Create a remote adapter."""
    return _make_adapter("glm-4.6-remote", provider="zhipu")


@pytest.fixture
def mock_outsourcing_router(local_adapter, remote_adapter):
    """Create a mock OutsourcingRouter."""
    from routing.outsourcing_integration import OutsourcingRouter

    router = MagicMock(spec=OutsourcingRouter)
    router.local_adapter = local_adapter
    router.remote_adapter = remote_adapter
    router.chat_completion = AsyncMock(return_value={"choices": [{"message": {"content": "test"}}]})
    router.stream_chat_completion = AsyncMock()
    return router


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
    """Test chat_completion method."""

    @pytest.mark.asyncio
    async def test_chat_completion_nimbus_model(self, fixed_router, mock_settings, mock_outsourcing_router):
        """Test chat completion for Nimbus-enabled model."""
        router = NimbusRouter(fixed_router=fixed_router, settings=mock_settings)
        router.outsourcing_routers["glm-4.6"] = mock_outsourcing_router

        messages = [{"role": "user", "content": "Hello"}]
        result = await router.chat_completion("glm-4.6", messages)

        # Should delegate to OutsourcingRouter
        mock_outsourcing_router.chat_completion.assert_awaited_once()
        assert result is not None

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


class TestNimbusRouterStreamCompletion:
    """Test stream_chat_completion method."""

    @pytest.mark.asyncio
    async def test_stream_completion_nimbus_model(self, fixed_router, mock_settings, mock_outsourcing_router):
        """Test streaming for Nimbus-enabled model."""
        router = NimbusRouter(fixed_router=fixed_router, settings=mock_settings)
        router.outsourcing_routers["glm-4.6"] = mock_outsourcing_router

        # Mock stream generator - must be async generator
        async def mock_stream(messages, **params):
            yield "data: chunk1\n\n"
            yield "data: chunk2\n\n"

        # Set the mock to call our async generator
        mock_outsourcing_router.stream_chat_completion = mock_stream

        messages = [{"role": "user", "content": "Hello"}]
        chunks = []
        async for chunk in router.stream_chat_completion("glm-4.6", messages):
            chunks.append(chunk)

        # Should delegate to OutsourcingRouter
        assert len(chunks) == 2

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

        # Should delegate to FixedRouter and stream from adapter
        assert len(chunks) == 2


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
    async def test_empty_messages(self, fixed_router, mock_settings, mock_outsourcing_router):
        """Test handling of empty messages."""
        router = NimbusRouter(fixed_router=fixed_router, settings=mock_settings)
        router.outsourcing_routers["glm-4.6"] = mock_outsourcing_router

        # Should still call OutsourcingRouter
        await router.chat_completion("glm-4.6", [])

        mock_outsourcing_router.chat_completion.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_params_passed_through(self, fixed_router, mock_settings, mock_outsourcing_router):
        """Test parameters are passed through correctly."""
        router = NimbusRouter(fixed_router=fixed_router, settings=mock_settings)
        router.outsourcing_routers["glm-4.6"] = mock_outsourcing_router

        messages = [{"role": "user", "content": "Hello"}]
        params = {
            "temperature": 0.7,
            "max_tokens": 100,
            "prefill_slo_seconds": 2.0,
        }

        await router.chat_completion("glm-4.6", messages, **params)

        # Verify params were passed
        call_kwargs = mock_outsourcing_router.chat_completion.call_args.kwargs
        assert call_kwargs.get("temperature") == 0.7
        assert call_kwargs.get("max_tokens") == 100
        assert call_kwargs.get("prefill_slo_seconds") == 2.0

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

    def test_extract_adapters_detects_local_by_openai_compat_localhost(self, fixed_router, mock_settings):
        """Test that openai_compat with localhost is detected as local adapter."""
        # Create adapters
        local_adapter = MagicMock()
        local_adapter.config.provider = "openai_compat"
        local_adapter.config.base_url = "http://127.0.0.1:8000/v1"

        remote_adapter = MagicMock()
        remote_adapter.config.provider = "zhipu"
        remote_adapter.config.base_url = "https://open.bigmodel.cn"

        fixed_router.register_route("test-model", [
            (local_adapter, 0.5),
            (remote_adapter, 0.5)
        ])

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

        fixed_router.register_route("multi-adapter-model", [
            (local1, 0.4),
            (local2, 0.3),
            (remote, 0.3)
        ])

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

        nimbus_router = NimbusRouter(fixed_router=fixed_router, settings=settings)

        slo = nimbus_router._get_model_slo("glm-4.6")

        assert slo == 1.5

    def test_get_model_slo_qwen(self, fixed_router):
        """Test that _get_model_slo returns correct SLO for Qwen models."""
        settings = MagicMock()
        settings.nimbus_enabled_models = []
        settings.qwen3_slo_seconds = 2.5

        nimbus_router = NimbusRouter(fixed_router=fixed_router, settings=settings)

        slo = nimbus_router._get_model_slo("qwen3-coder-30b")

        assert slo == 2.5

    def test_get_model_slo_minimax(self, fixed_router):
        """Test that _get_model_slo returns correct SLO for MiniMax models."""
        settings = MagicMock()
        settings.nimbus_enabled_models = []
        settings.minimax_slo_seconds = 3.0

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

        # Register model with both local and remote adapters
        local_adapter = MagicMock()
        local_adapter.config.provider = "sglang"
        local_adapter.config.base_url = "http://localhost:8001"

        remote_adapter = MagicMock()
        remote_adapter.config.provider = "zhipu"
        remote_adapter.config.base_url = "https://open.bigmodel.cn"

        fixed_router.register_route("glm-4.6", [
            (local_adapter, 0.5),
            (remote_adapter, 0.5)
        ])

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
    def test_init_routers_skips_model_with_invalid_route(self, mock_outsourcing_router, fixed_router):
        """Test that _init_routers skips models with invalid routes (missing adapters)."""
        # Setup settings with a model that has invalid route
        settings = MagicMock()
        settings.nimbus_enabled_models = ["bad-model"]

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
    def test_init_routers_skips_model_not_in_fixed_router(self, mock_outsourcing_router, fixed_router):
        """Test that _init_routers skips models not found in FixedRouter."""
        # Setup settings with a model that doesn't exist in FixedRouter
        settings = MagicMock()
        settings.nimbus_enabled_models = ["non-existent-model"]

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

        # Register both models
        for model_id in ["glm-4.6", "qwen3-coder-30b"]:
            local_adapter = MagicMock()
            local_adapter.config.provider = "sglang"
            local_adapter.config.base_url = f"http://localhost:8001/{model_id}"

            remote_adapter = MagicMock()
            remote_adapter.config.provider = "zhipu"
            remote_adapter.config.base_url = "https://api.zhipu.ai"

            fixed_router.register_route(model_id, [
                (local_adapter, 0.5),
                (remote_adapter, 0.5)
            ])

        # Create NimbusRouter
        nimbus_router = NimbusRouter(fixed_router=fixed_router, settings=settings)

        # Verify both models are registered
        assert "glm-4.6" in nimbus_router.outsourcing_routers
        assert "qwen3-coder-30b" in nimbus_router.outsourcing_routers

        # Verify OutsourcingRouter was called twice
        assert mock_outsourcing_router.call_count == 2
