"""Tests for ``PricingLookup``."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from serving.servers.routers.completions_cost import PricingLookup
from serving.servers.routers.routing_info import Pricing, RoutingInfo


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_adapter(provider: str, base_url: str, pricing: dict[str, str] | None,
                  endpoint_id: str | None = None) -> Any:
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
