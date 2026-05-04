"""Tests for ``CompletionsLogger``."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from serving.servers.routers.completions_logging import CompletionsLogger
from serving.servers.routers.routing_info import RoutingInfo


@pytest.fixture
def log_store():
    store = MagicMock()
    store.log_request = AsyncMock()
    return store


@pytest.fixture
def registry():
    return MagicMock()


@pytest.fixture
def cl_logger(log_store, registry):
    return CompletionsLogger(log_store=log_store, model_router_registry=registry)


@pytest.fixture
def routing_info():
    return RoutingInfo(
        request_id="rid-1",
        model="gpt-4",
        provider="openai",
        endpoint_id="openai-prod",
        base_url="https://api.openai.com/v1",
        pricing={"prompt": "0.5", "completion": "1.5"},
        routewise=None,
        upstream_cost_usd=0.012,
    )


# -- schedule_log -----------------------------------------------------------


@pytest.mark.asyncio
async def test_schedule_log_forwards_log_data_to_log_store(cl_logger, log_store):
    log_data = {
        "request_id": "rid-1",
        "model_id": "gpt-4",
        "provider": "openai",
        "prompt": [{"role": "user", "content": "hi"}],
        "response": {"id": "r1"},
        "usage": {"prompt_tokens": 5, "completion_tokens": 3},
        "latency_ms": 42,
        "status_code": 200,
        "params": {"stream": False},
        "metadata": {"user_id": "u1"},
        "ttft_ms": None,
        "pricing": {"prompt": "0.5", "completion": "1.5"},
        "upstream_cost_usd": 0.001,
    }

    cl_logger.schedule_log("rid-1", log_data)
    # Allow the background task to run.
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    log_store.log_request.assert_awaited_once()
    _, kwargs = log_store.log_request.call_args
    assert kwargs == log_data


@pytest.mark.asyncio
async def test_schedule_log_ignores_log_store_exceptions(cl_logger, log_store):
    log_store.log_request.side_effect = RuntimeError("DB down")
    cl_logger.schedule_log("rid", {"request_id": "rid"})
    # Wait for the background task to finish; should not raise.
    for _ in range(5):
        await asyncio.sleep(0)
    log_store.log_request.assert_awaited_once()


def test_schedule_log_no_event_loop_drops_silently():
    """Outside a running loop the call must not raise (defensive fallback)."""
    log_store = MagicMock()
    log_store.log_request = AsyncMock()
    cl_logger = CompletionsLogger(log_store=log_store)
    # No asyncio.run wrapping; this directly invokes the sync method.
    # asyncio.create_task() raises RuntimeError without a running loop
    # — schedule_log is expected to swallow it.
    cl_logger.schedule_log("rid", {"request_id": "rid"})
    log_store.log_request.assert_not_called()


def test_schedule_log_noop_when_log_store_is_none():
    cl_logger = CompletionsLogger(log_store=None)
    cl_logger.schedule_log("rid", {"request_id": "rid"})
    # Nothing to assert beyond "did not raise".


# -- record_routing_observation --------------------------------------------


def test_record_routing_observation_with_routing_info(cl_logger, routing_info):
    active_router = MagicMock()
    cl_logger.record_routing_observation(
        active_router,
        "gpt-4",
        routing_info,
        ttft_ms=42.0,
        total_latency_ms=120.0,
        prompt_tokens=10,
        completion_tokens=5,
        success=True,
    )
    active_router.record_observation.assert_called_once()
    obs = active_router.record_observation.call_args[0][0]
    assert obs.model_id == "gpt-4"
    assert obs.endpoint_id == "openai-prod"  # from routing.endpoint_id
    assert obs.ttft_ms == 42.0
    assert obs.total_latency_ms == 120.0
    assert obs.prompt_tokens == 10
    assert obs.completion_tokens == 5
    assert obs.token_count == 15
    assert obs.success is True


def test_record_routing_observation_falls_back_to_base_url(cl_logger):
    """When endpoint_id is missing, observation key falls back to base_url."""
    routing = RoutingInfo(
        request_id="rid",
        model="gpt-4",
        provider="openai",
        endpoint_id=None,
        base_url="https://api.openai.com/v1",
    )
    active_router = MagicMock()
    cl_logger.record_routing_observation(
        active_router,
        "gpt-4",
        routing,
        ttft_ms=None,
        total_latency_ms=10.0,
        prompt_tokens=0,
        completion_tokens=0,
        success=True,
    )
    obs = active_router.record_observation.call_args[0][0]
    assert obs.endpoint_id == "https://api.openai.com/v1"


def test_record_routing_observation_falls_back_to_provider(cl_logger):
    """When endpoint_id and base_url are both missing, fall back to provider."""
    routing = RoutingInfo(
        request_id="rid",
        model="gpt-4",
        provider="openai",
    )
    active_router = MagicMock()
    cl_logger.record_routing_observation(
        active_router,
        "gpt-4",
        routing,
        ttft_ms=None,
        total_latency_ms=0.0,
        prompt_tokens=0,
        completion_tokens=0,
        success=False,
    )
    obs = active_router.record_observation.call_args[0][0]
    assert obs.endpoint_id == "openai"


def test_record_routing_observation_unknown_when_routing_none(cl_logger):
    active_router = MagicMock()
    cl_logger.record_routing_observation(
        active_router,
        "gpt-4",
        None,
        ttft_ms=None,
        total_latency_ms=0.0,
        prompt_tokens=0,
        completion_tokens=0,
        success=False,
    )
    obs = active_router.record_observation.call_args[0][0]
    assert obs.endpoint_id == "unknown"


def test_record_routing_observation_accepts_legacy_dict(cl_logger):
    """Backwards-compat: exception._routing is still a raw dict."""
    legacy = {
        "provider": "anthropic",
        "endpoint_id": "anthropic-prod",
        "base_url": "https://api.anthropic.com/v1",
        "routewise": {
            "selected_tier": "B",
            "quota_committed": 1.5,
            "sc_committed": True,
            "hedged": True,
            "backup_won": False,
            "lp_status": "ok",
        },
    }
    active_router = MagicMock()
    cl_logger.record_routing_observation(
        active_router,
        "claude-sonnet",
        legacy,
        ttft_ms=200.0,
        total_latency_ms=500.0,
        prompt_tokens=20,
        completion_tokens=15,
        success=True,
    )
    obs = active_router.record_observation.call_args[0][0]
    assert obs.endpoint_id == "anthropic-prod"
    assert obs.selected_tier == "B"
    assert obs.quota_committed == 1.5
    assert obs.sc_committed is True
    assert obs.hedged is True
    assert obs.backup_won is False
    assert obs.lp_status == "ok"


def test_record_routing_observation_no_routewise_dict(cl_logger):
    """When routing has no ``routewise`` field, defaults pass through."""
    routing = RoutingInfo(
        request_id="rid",
        model="gpt-4",
        provider="openai",
        endpoint_id="openai-prod",
        routewise=None,
    )
    active_router = MagicMock()
    cl_logger.record_routing_observation(
        active_router,
        "gpt-4",
        routing,
        ttft_ms=None,
        total_latency_ms=0.0,
        prompt_tokens=0,
        completion_tokens=0,
        success=True,
    )
    obs = active_router.record_observation.call_args[0][0]
    assert obs.quota_committed == 0.0
    assert obs.selected_tier is None
    assert obs.sc_committed is False
    assert obs.hedged is False
    assert obs.backup_won is False
    assert obs.lp_status is None


# -- build_db_params -------------------------------------------------------


def test_build_db_params_passthrough_when_max_tokens_set(cl_logger):
    params = {"max_tokens": 1234, "temperature": 0.7}
    out = cl_logger.build_db_params(
        params, provider="openai", base_url=None,
        get_adapter_config_for_provider=MagicMock(),
    )
    assert out == params
    assert out is not params  # defensive copy


def test_build_db_params_fills_default_max_tokens(cl_logger):
    params = {"temperature": 0.7}

    class _Cfg:
        max_output_length = 4096

    def _get(_provider, _base_url):
        return _Cfg()

    out = cl_logger.build_db_params(
        params, provider="openai", base_url=None, get_adapter_config_for_provider=_get,
    )
    assert out["max_tokens"] == 4096
    assert out["temperature"] == 0.7


def test_build_db_params_unknown_provider_no_max_tokens(cl_logger):
    """Unregistered provider returns None config; max_tokens stays absent."""
    params = {"temperature": 0.7}

    def _get(_provider, _base_url):
        return None

    out = cl_logger.build_db_params(
        params, provider="router", base_url=None, get_adapter_config_for_provider=_get,
    )
    assert "max_tokens" not in out or out["max_tokens"] is None
    assert out["temperature"] == 0.7
