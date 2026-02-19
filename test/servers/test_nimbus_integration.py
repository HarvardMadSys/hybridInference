"""Integration tests for NimbusRouter in FastAPI context.

Tests the complete flow: FastAPI -> NimbusRouter -> OutsourcingRouter/FixedRouter
"""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

project_root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(project_root))

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from routing.routers import FixedRouter, NimbusRouter
from serving.adapters.base import BaseAdapter, ModelConfig
from serving.config.settings import Settings
from serving.servers.deps import AppServices
from serving.servers.middleware.error import install_error_handlers
from serving.servers.routers import completions


# ============================================================================
# Test Adapters
# ============================================================================


class DummyAdapter(BaseAdapter):
    """Adapter that returns a simple response."""

    async def chat_completion(self, messages, **kwargs):
        import time

        return {
            "id": "test-id",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": self.config.id,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": f"Response from {self.config.provider}",
                    },
                    "finish_reason": "stop",
                }
            ],
        }

    async def stream_chat_completion(self, messages, **kwargs):
        yield f"data: {{'choices': [{{'delta': {{'content': 'Stream from {self.config.provider}'}}}}]}}\n\n"
        yield "data: [DONE]\n\n"


# ============================================================================
# Helper Functions
# ============================================================================


def _make_config(model_id: str, provider: str = "test") -> ModelConfig:
    """Create a test ModelConfig."""
    return ModelConfig(
        id=model_id,
        name=f"Test {model_id}",
        provider=provider,
        base_url="http://test.local",
        context_length=8192,
        max_output_length=4096,
    )


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def mock_settings():
    """Create mock settings for testing."""
    settings = MagicMock(spec=Settings)
    settings.routing_strategy = "nimbus"
    settings.experiment_mode = False
    settings.nimbus_enabled_models = ["nimbus-model"]
    return settings


@pytest.fixture
def fixed_router():
    """Create a FixedRouter with test routes."""
    router = FixedRouter(experiment_mode=False)

    # Register routes for nimbus-model with BOTH local and remote adapters
    nimbus_local_adapter = DummyAdapter(_make_config("nimbus-model", "local"))
    nimbus_remote_adapter = DummyAdapter(_make_config("nimbus-model", "remote"))

    # Register routes for regular-model with only local adapter
    non_nimbus_adapter = DummyAdapter(_make_config("regular-model", "local"))

    # Nimbus model needs both local and remote
    router.register_route(
        "nimbus-model", [(nimbus_local_adapter, 0.5), (nimbus_remote_adapter, 0.5)]
    )

    # Regular model only has local
    router.register_route("regular-model", [(non_nimbus_adapter, 1.0)])

    return router


class OutsourcingDummyAdapter(BaseAdapter):
    """Adapter for outsourcing router integration tests.

    Returns responses clearly marked as coming from the OutsourcingRouter path.
    """

    async def chat_completion(self, messages, **kwargs):
        import time

        return {
            "id": "outsourcing-id",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": self.config.id,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "Response from OutsourcingRouter"},
                    "finish_reason": "stop",
                }
            ],
        }

    async def stream_chat_completion(self, messages, **kwargs):
        yield "data: {'choices': [{'delta': {'content': 'Stream from OutsourcingRouter'}}]}\n\n"
        yield "data: [DONE]\n\n"


@pytest.fixture
def mock_outsourcing_router():
    """Create a mock OutsourcingRouter with decide() support.

    NimbusRouter (inheriting BaseRouter) calls decide() to get the adapter,
    then BaseRouter calls adapter.chat_completion() directly. So we need
    decide() to return an adapter that produces the expected content.
    """
    from routing.outsourcing_integration import OutsourcingRouter

    mock_router = MagicMock(spec=OutsourcingRouter)

    # Create a real adapter that returns OutsourcingRouter-style responses
    outsourcing_adapter = OutsourcingDummyAdapter(
        _make_config("nimbus-model", "outsourcing_local")
    )
    remote_adapter = OutsourcingDummyAdapter(
        _make_config("nimbus-model", "outsourcing_remote")
    )

    mock_router.local_adapter = outsourcing_adapter
    mock_router.remote_adapter = remote_adapter

    # decide() returns the adapter for BaseRouter to execute
    mock_router.decide.return_value = {
        "adapter": outsourcing_adapter,
        "routing_decision": "local",
        "request_id": "req-test-1",
        "reason": "No SLO violations detected",
        "cached_tokens": 0,
        "model_id": "nimbus-model",
        "queue_length": 0,
        "decision": MagicMock(),
    }

    # Mock get_stats
    mock_router.get_stats = MagicMock(
        return_value={"total_requests": 10, "local_requests": 6, "outsourced_requests": 4}
    )

    return mock_router


@pytest.fixture
async def nimbus_app(fixed_router, mock_settings, mock_outsourcing_router):
    """Create a FastAPI app with NimbusRouter."""
    # Create NimbusRouter
    with patch("routing.routers.OutsourcingRouter", return_value=mock_outsourcing_router):
        nimbus_router = NimbusRouter(fixed_router=fixed_router, settings=mock_settings)

    # Create app services
    services = AppServices(
        router=fixed_router,
        nimbus_router=nimbus_router,
        db_logger=None,
        rate_limiter=None,
    )

    # Create FastAPI app
    app = FastAPI(title="Nimbus Test App")
    app.state.services = services
    install_error_handlers(app)
    app.include_router(completions.router)

    return app


@pytest.fixture
async def nimbus_client(nimbus_app):
    """Create an async test client for Nimbus app."""
    transport = ASGITransport(app=nimbus_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


# ============================================================================
# Tests: Routing Selection
# ============================================================================


class TestNimbusRoutingSelection:
    """Test that NimbusRouter correctly selects between OutsourcingRouter and FixedRouter."""

    @pytest.mark.asyncio
    async def test_nimbus_enabled_model_uses_outsourcing_router(self, nimbus_client):
        """Test that Nimbus-enabled models use OutsourcingRouter."""
        response = await nimbus_client.post(
            "/v1/chat/completions",
            json={
                "model": "nimbus-model",
                "messages": [{"role": "user", "content": "Hello"}],
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["choices"][0]["message"]["content"] == "Response from OutsourcingRouter"

    @pytest.mark.asyncio
    async def test_non_nimbus_model_uses_fixed_router(self, nimbus_client):
        """Test that non-Nimbus models use FixedRouter."""
        response = await nimbus_client.post(
            "/v1/chat/completions",
            json={
                "model": "regular-model",
                "messages": [{"role": "user", "content": "Hello"}],
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["choices"][0]["message"]["content"] == "Response from local"

    @pytest.mark.asyncio
    async def test_nimbus_streaming_uses_outsourcing_router(self, nimbus_client):
        """Test that Nimbus-enabled models use OutsourcingRouter for streaming."""
        response = await nimbus_client.post(
            "/v1/chat/completions",
            json={
                "model": "nimbus-model",
                "messages": [{"role": "user", "content": "Hello"}],
                "stream": True,
            },
        )

        assert response.status_code == 200
        content = response.text
        assert "Stream from OutsourcingRouter" in content

    @pytest.mark.asyncio
    async def test_non_nimbus_streaming_uses_fixed_router(self, nimbus_client):
        """Test that non-Nimbus models use FixedRouter for streaming."""
        response = await nimbus_client.post(
            "/v1/chat/completions",
            json={
                "model": "regular-model",
                "messages": [{"role": "user", "content": "Hello"}],
                "stream": True,
            },
        )

        assert response.status_code == 200
        content = response.text
        assert "Stream from local" in content


# ============================================================================
# Tests: Statistics and Monitoring
# ============================================================================


class TestNimbusStatistics:
    """Test NimbusRouter statistics collection."""

    @pytest.mark.asyncio
    async def test_statistics_collection(self, nimbus_app):
        """Test that NimbusRouter collects statistics correctly."""
        services = nimbus_app.state.services
        nimbus_router = services.nimbus_router

        stats = nimbus_router.get_stats()

        # Should include OutsourcingRouter stats for nimbus-model
        assert "nimbus-model" in stats
        assert stats["nimbus-model"]["total_requests"] == 10
        assert stats["nimbus-model"]["local_requests"] == 6
        assert stats["nimbus-model"]["outsourced_requests"] == 4

    @pytest.mark.asyncio
    async def test_model_registration(self, fixed_router, mock_settings):
        """Test that NimbusRouter correctly registers Nimbus-enabled models."""
        mock_outsourcing_router = MagicMock()

        with patch("routing.routers.OutsourcingRouter", return_value=mock_outsourcing_router):
            nimbus_router = NimbusRouter(fixed_router=fixed_router, settings=mock_settings)

        # Should have registered nimbus-model
        assert "nimbus-model" in nimbus_router.outsourcing_routers

        # Should not have registered regular-model
        assert "regular-model" not in nimbus_router.outsourcing_routers


# ============================================================================
# Tests: Configuration
# ============================================================================


class TestNimbusConfiguration:
    """Test NimbusRouter configuration handling."""

    @pytest.mark.asyncio
    async def test_empty_nimbus_enabled_models(self, fixed_router):
        """Test that NimbusRouter handles empty nimbus_enabled_models gracefully."""
        settings = MagicMock(spec=Settings)
        settings.routing_strategy = "nimbus"
        settings.nimbus_enabled_models = []

        nimbus_router = NimbusRouter(fixed_router=fixed_router, settings=settings)

        # Should have no outsourcing routers
        assert len(nimbus_router.outsourcing_routers) == 0

    @pytest.mark.asyncio
    async def test_model_not_in_fixed_router(self, fixed_router):
        """Test that NimbusRouter skips models not in FixedRouter."""
        settings = MagicMock(spec=Settings)
        settings.routing_strategy = "nimbus"
        settings.nimbus_enabled_models = ["nimbus-model", "non-existent-model"]

        mock_outsourcing_router = MagicMock()

        with patch("routing.routers.OutsourcingRouter", return_value=mock_outsourcing_router):
            nimbus_router = NimbusRouter(fixed_router=fixed_router, settings=settings)

        # Should only register nimbus-model
        assert "nimbus-model" in nimbus_router.outsourcing_routers
        assert "non-existent-model" not in nimbus_router.outsourcing_routers
