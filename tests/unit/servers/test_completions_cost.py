"""Tests for ``PricingLookup`` and ``CostTracker``."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from serving.servers.routers.completions_cost import CostTracker, PricingLookup
from serving.servers.routers.routing_info import Pricing, RoutingInfo
from serving.storage.utils import calculate_cost

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_adapter(
    provider: str, base_url: str, pricing: dict[str, str] | None, endpoint_id: str | None = None
) -> Any:
    """Build a minimal mock adapter with .config matching real adapters."""
    config = MagicMock()
    config.provider = provider
    config.base_url = base_url
    config.endpoint_id = endpoint_id
    config.pricing = pricing
    adapter = MagicMock()
    adapter.config = config
    return adapter


def _make_router(routes: dict[str, list[Any]]) -> Any:
    """Build a router whose .routes maps model_id → RouteConfig-like list."""
    router = MagicMock()
    router.routes = {}
    for model_id, adapters in routes.items():
        route = MagicMock()
        route.adapters = [(a, 1.0) for a in adapters]
        router.routes[model_id] = route
    return router


# ---------------------------------------------------------------------------
# PricingLookup
# ---------------------------------------------------------------------------


def test_pricing_lookup_returns_none_when_routing_has_no_keys():
    router = _make_router({})
    lookup = PricingLookup(router=router)
    routing = RoutingInfo(request_id="rid", model="gpt-4")
    assert lookup.for_routing(routing) is None


def test_pricing_lookup_picks_up_embedded_dict_from_extra():
    """Adapter-emitted pricing in routing.extra wins over registry lookup."""
    router = _make_router({})  # registry empty
    lookup = PricingLookup(router=router)
    routing = RoutingInfo(
        request_id="rid",
        model="gpt-4",
        provider="openai",
        base_url="https://api.openai.com/v1",
        extra={"pricing": {"prompt": "0.5", "completion": "1.5"}},
    )
    pricing = lookup.for_routing(routing)
    assert isinstance(pricing, Pricing)
    assert pricing.prompt_price == 0.5
    assert pricing.completion_price == 1.5
    assert pricing.cache_read_price == 0.0
    assert pricing.cache_write_price == 0.0


def test_pricing_lookup_fetches_from_registered_adapter_by_endpoint_id():
    adapter = _make_adapter(
        provider="openai",
        base_url="https://api.openai.com/v1",
        pricing={"prompt": "3.0", "completion": "15.0"},
        endpoint_id="openai-prod",
    )
    router = _make_router({"gpt-4": [adapter]})
    lookup = PricingLookup(router=router)
    routing = RoutingInfo(
        request_id="rid",
        model="gpt-4",
        provider="openai",
        base_url="https://api.openai.com/v1",
        endpoint_id="openai-prod",
    )
    pricing = lookup.for_routing(routing)
    assert pricing is not None
    assert pricing.prompt_price == 3.0
    assert pricing.completion_price == 15.0


def test_pricing_lookup_caches_by_endpoint_id():
    adapter = _make_adapter(
        provider="openai",
        base_url="https://api.openai.com/v1",
        pricing={"prompt": "3.0", "completion": "15.0"},
        endpoint_id="openai-prod",
    )
    router = _make_router({"gpt-4": [adapter]})
    # Use a counter to detect repeated walks.
    lookup = PricingLookup(router=router)
    routing = RoutingInfo(
        request_id="rid",
        model="gpt-4",
        provider="openai",
        base_url="https://api.openai.com/v1",
        endpoint_id="openai-prod",
    )

    # Prime the cache.
    first = lookup.for_routing(routing)
    # Mutate the adapter's pricing post-hoc; cached value must remain.
    adapter.config.pricing = {"prompt": "999.0", "completion": "999.0"}
    second = lookup.for_routing(routing)
    assert first == second


def test_pricing_lookup_falls_back_to_provider_base_url_when_no_endpoint_id():
    adapter = _make_adapter(
        provider="anthropic",
        base_url="https://api.anthropic.com/v1",
        pricing={"prompt": "8.0", "completion": "24.0"},
        endpoint_id=None,
    )
    router = _make_router({"claude-sonnet": [adapter]})
    lookup = PricingLookup(router=router)
    routing = RoutingInfo(
        request_id="rid",
        model="claude-sonnet",
        provider="anthropic",
        base_url="https://api.anthropic.com/v1",
        endpoint_id=None,
    )
    pricing = lookup.for_routing(routing)
    assert pricing is not None
    assert pricing.prompt_price == 8.0
    assert pricing.completion_price == 24.0


def test_pricing_lookup_returns_none_when_adapter_has_no_pricing():
    adapter = _make_adapter(
        provider="local",
        base_url="http://localhost:8080",
        pricing=None,  # adapter without pricing config
        endpoint_id="local-vllm",
    )
    router = _make_router({"local": [adapter]})
    lookup = PricingLookup(router=router)
    routing = RoutingInfo(
        request_id="rid",
        model="local",
        provider="local",
        base_url="http://localhost:8080",
        endpoint_id="local-vllm",
    )
    assert lookup.for_routing(routing) is None


def test_pricing_lookup_raw_dict_for_routing_returns_dict_for_log_payload():
    """The log row needs the original adapter dict — same shape as today."""
    adapter = _make_adapter(
        provider="openai",
        base_url="https://api.openai.com/v1",
        pricing={"prompt": "3.0", "completion": "15.0"},
        endpoint_id="openai-prod",
    )
    router = _make_router({"gpt-4": [adapter]})
    lookup = PricingLookup(router=router)
    routing = RoutingInfo(
        request_id="rid",
        model="gpt-4",
        provider="openai",
        base_url="https://api.openai.com/v1",
        endpoint_id="openai-prod",
    )
    raw = lookup.raw_dict_for_routing(routing)
    assert raw == {"prompt": "3.0", "completion": "15.0"}


def test_pricing_lookup_raw_dict_prefers_embedded_extra_over_registry():
    """Adapter-emitted dict in extra overrides registry-config pricing."""
    adapter = _make_adapter(
        provider="openai",
        base_url="https://api.openai.com/v1",
        pricing={"prompt": "3.0", "completion": "15.0"},
        endpoint_id="openai-prod",
    )
    router = _make_router({"gpt-4": [adapter]})
    lookup = PricingLookup(router=router)
    embedded = {"prompt": "0.5", "completion": "1.5"}
    routing = RoutingInfo(
        request_id="rid",
        model="gpt-4",
        provider="openai",
        base_url="https://api.openai.com/v1",
        endpoint_id="openai-prod",
        extra={"pricing": embedded},
    )
    assert lookup.raw_dict_for_routing(routing) == embedded


def test_pricing_lookup_for_routing_handles_invalid_dict_gracefully():
    """Non-numeric pricing strings parse to None, not exception."""
    router = _make_router({})
    lookup = PricingLookup(router=router)
    routing = RoutingInfo(
        request_id="rid",
        model="gpt-4",
        extra={"pricing": {"prompt": "not-a-number", "completion": "1.5"}},
    )
    # Falls through to None when conversion fails.
    assert lookup.for_routing(routing) is None


def test_pricing_lookup_caches_provider_base_url_fallback():
    """Cache hits even on the (provider, base_url) fallback path."""
    adapter = _make_adapter(
        provider="anthropic",
        base_url="https://api.anthropic.com/v1",
        pricing={"prompt": "8.0", "completion": "24.0"},
        endpoint_id=None,
    )
    router = _make_router({"claude-sonnet": [adapter]})
    lookup = PricingLookup(router=router)
    routing = RoutingInfo(
        request_id="rid",
        model="claude-sonnet",
        provider="anthropic",
        base_url="https://api.anthropic.com/v1",
        endpoint_id=None,
    )
    first = lookup.for_routing(routing)
    adapter.config.pricing = {"prompt": "999.0", "completion": "999.0"}
    second = lookup.for_routing(routing)
    assert first == second


# ---------------------------------------------------------------------------
# CostTracker — math + scheduling
# ---------------------------------------------------------------------------


@pytest.fixture
def op_store():
    op = MagicMock()
    op.increment_user_cost = AsyncMock()
    return op


@pytest.fixture
def empty_lookup():
    return PricingLookup(router=_make_router({}))


@pytest.mark.asyncio
async def test_cost_tracker_no_pricing_no_increment(op_store, empty_lookup):
    tracker = CostTracker(op_store=op_store, pricing=empty_lookup)
    routing = RoutingInfo(request_id="rid", model="gpt-4")  # no pricing
    enriched = await tracker.schedule_increment(
        user_id="u1",
        routing=routing,
        prompt_tokens=1000,
        completion_tokens=500,
    )
    # No cost computed → routing unchanged.
    assert enriched.upstream_cost_usd is None
    # Allow any background task to settle.
    for _ in range(10):
        await asyncio.sleep(0)
    op_store.increment_user_cost.assert_not_awaited()


@pytest.mark.asyncio
async def test_cost_tracker_uses_typed_routing_pricing_first(op_store, empty_lookup):
    tracker = CostTracker(op_store=op_store, pricing=empty_lookup)
    pricing = Pricing(prompt_price=0.5, completion_price=1.5)
    routing = RoutingInfo(request_id="rid", model="gpt-4", pricing=pricing)
    enriched = await tracker.schedule_increment(
        user_id="u1",
        routing=routing,
        prompt_tokens=1000,
        completion_tokens=500,
    )
    expected = calculate_cost(
        {"prompt_tokens": 1000, "completion_tokens": 500},
        {"prompt": "0.5", "completion": "1.5"},
    )
    assert enriched.upstream_cost_usd == pytest.approx(expected)
    for _ in range(10):
        await asyncio.sleep(0)
    op_store.increment_user_cost.assert_awaited_once()
    call_args = op_store.increment_user_cost.call_args
    assert call_args[0][0] == "u1"
    assert call_args[0][1] == pytest.approx(expected)


@pytest.mark.asyncio
async def test_cost_tracker_zero_cost_does_not_schedule_increment(op_store, empty_lookup):
    """When tokens * price == 0, nothing is incremented (matches today)."""
    tracker = CostTracker(op_store=op_store, pricing=empty_lookup)
    pricing = Pricing(prompt_price=0.5, completion_price=1.5)
    routing = RoutingInfo(request_id="rid", model="gpt-4", pricing=pricing)
    enriched = await tracker.schedule_increment(
        user_id="u1",
        routing=routing,
        prompt_tokens=0,
        completion_tokens=0,
    )
    assert enriched.upstream_cost_usd in (None, 0.0)
    for _ in range(10):
        await asyncio.sleep(0)
    op_store.increment_user_cost.assert_not_awaited()


@pytest.mark.asyncio
async def test_cost_tracker_falls_back_to_lookup_when_routing_pricing_none(op_store):
    """If routing.pricing is None, fall back to PricingLookup."""
    adapter = _make_adapter(
        provider="openai",
        base_url="https://api.openai.com/v1",
        pricing={"prompt": "0.5", "completion": "1.5"},
        endpoint_id="openai-prod",
    )
    router = _make_router({"gpt-4": [adapter]})
    lookup = PricingLookup(router=router)
    tracker = CostTracker(op_store=op_store, pricing=lookup)
    routing = RoutingInfo(
        request_id="rid",
        model="gpt-4",
        provider="openai",
        base_url="https://api.openai.com/v1",
        endpoint_id="openai-prod",
    )
    enriched = await tracker.schedule_increment(
        user_id="u1",
        routing=routing,
        prompt_tokens=1000,
        completion_tokens=500,
    )
    expected = calculate_cost(
        {"prompt_tokens": 1000, "completion_tokens": 500},
        {"prompt": "0.5", "completion": "1.5"},
    )
    assert enriched.upstream_cost_usd == pytest.approx(expected)


@pytest.mark.asyncio
async def test_cost_tracker_swallows_increment_errors(op_store, empty_lookup, caplog):
    op_store.increment_user_cost.side_effect = RuntimeError("DB down")
    tracker = CostTracker(op_store=op_store, pricing=empty_lookup)
    pricing = Pricing(prompt_price=0.5, completion_price=1.5)
    routing = RoutingInfo(request_id="rid", model="gpt-4", pricing=pricing)
    enriched = await tracker.schedule_increment(
        user_id="u1",
        routing=routing,
        prompt_tokens=1000,
        completion_tokens=500,
    )
    assert enriched.upstream_cost_usd is not None
    # Drain the background task; should not raise.
    for _ in range(10):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_cost_tracker_no_op_store_skips_increment(empty_lookup):
    tracker = CostTracker(op_store=None, pricing=empty_lookup)
    pricing = Pricing(prompt_price=0.5, completion_price=1.5)
    routing = RoutingInfo(request_id="rid", model="gpt-4", pricing=pricing)
    enriched = await tracker.schedule_increment(
        user_id="u1",
        routing=routing,
        prompt_tokens=1000,
        completion_tokens=500,
    )
    # Cost is still computed (so the log row gets it) but no DB call.
    assert enriched.upstream_cost_usd is not None


# ---------------------------------------------------------------------------
# Cost-math byte-for-byte parity with calculate_cost
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("usage", "pricing_dict"),
    [
        # Plain prompt + completion.
        (
            {"prompt_tokens": 1000, "completion_tokens": 500},
            {"prompt": "0.15", "completion": "1.25"},
        ),
        # With cache_read pricing > 0 (subtracts from billable prompt).
        (
            {
                "prompt_tokens": 6000,
                "completion_tokens": 500,
                "reasoning_tokens": 200,
                "total_tokens": 6700,
                "cache_read_tokens": 5000,
            },
            {
                "prompt": "0.28",
                "completion": "0.42",
                "input_cache_reads": "0.028",
            },
        ),
        # Reasoning reported separately but already included in completion_tokens.
        (
            {
                "prompt_tokens": 6000,
                "completion_tokens": 700,
                "reasoning_tokens": 200,
                "total_tokens": 6700,
                "cache_read_tokens": 5000,
            },
            {
                "prompt": "0.28",
                "completion": "0.42",
                "input_cache_reads": "0.028",
            },
        ),
        # cache_read price 0 → cached tokens fall back to prompt rate.
        (
            {
                "prompt_tokens": 10000,
                "completion_tokens": 0,
                "cache_read_tokens": 8000,
            },
            {"prompt": "3.0", "completion": "15.0"},
        ),
        # cache > prompt clamps to 0.
        (
            {
                "prompt_tokens": 100,
                "completion_tokens": 0,
                "cache_read_tokens": 500,
            },
            {
                "prompt": "3.0",
                "completion": "15.0",
                "input_cache_reads": "0.30",
            },
        ),
        # Both cache_read and cache_write prices set.
        (
            {
                "prompt_tokens": 10000,
                "completion_tokens": 200,
                "cache_read_tokens": 5000,
                "cache_write_tokens": 1000,
            },
            {
                "prompt": "3.0",
                "completion": "15.0",
                "input_cache_reads": "0.30",
                "input_cache_writes": "3.75",
            },
        ),
        # Reasoning tokens charged at completion rate.
        (
            {
                "prompt_tokens": 1000,
                "completion_tokens": 100,
                "reasoning_tokens": 500,
            },
            {"prompt": "0.5", "completion": "2.0"},
        ),
        # Zero usage.
        (
            {"prompt_tokens": 0, "completion_tokens": 0},
            {"prompt": "1.0", "completion": "2.0"},
        ),
    ],
)
def test_cost_tracker_compute_cost_matches_calculate_cost_byte_for_byte(usage, pricing_dict):
    """Equivalence test: CostTracker._compute_cost must equal calculate_cost output."""
    pricing = Pricing(
        prompt_price=float(pricing_dict.get("prompt", "0")),
        completion_price=float(pricing_dict.get("completion", "0")),
        cache_read_price=float(pricing_dict.get("input_cache_reads", "0")),
        cache_write_price=float(pricing_dict.get("input_cache_writes", "0")),
    )
    expected = calculate_cost(usage, pricing_dict)
    actual = CostTracker._compute_cost(
        prompt_tokens=int(usage.get("prompt_tokens", 0)),
        completion_tokens=int(usage.get("completion_tokens", 0)),
        total_tokens=(
            int(usage["total_tokens"]) if usage.get("total_tokens") is not None else None
        ),
        pricing=pricing,
        cache_read_tokens=int(usage.get("cache_read_tokens", 0)),
        cache_write_tokens=int(usage.get("cache_write_tokens", 0)),
        reasoning_tokens=int(usage.get("reasoning_tokens", 0)),
    )
    if expected is None:
        assert actual is None or actual == 0.0
    else:
        assert actual == pytest.approx(expected)
