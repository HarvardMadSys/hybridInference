"""Tests for routing_info dataclasses."""

from __future__ import annotations

import dataclasses

import pytest

from serving.servers.routers.routing_info import (
    Pricing,
    RoutingInfo,
    _provider_for_error,
    _status_code_from_exception,
    build_initial_routing_info,
    merge_adapter_routing,
)
from serving.utils import context as req_ctx


def test_pricing_frozen():
    p = Pricing(prompt_price=0.5, completion_price=1.5)
    with pytest.raises(dataclasses.FrozenInstanceError):
        p.prompt_price = 0.0  # type: ignore[misc]


def test_strategy_metadata_defaults_to_none():
    r = RoutingInfo(request_id="abc", model="gpt-4")
    assert r.strategy_metadata is None
    assert r.routewise is None


def test_routing_info_constructor_accepts_legacy_routewise():
    r = RoutingInfo(
        request_id="rid",
        model="gpt-4",
        routewise={"selected_provider_type": "A"},
    )
    assert r.strategy_metadata == {"routewise": {"selected_provider_type": "A"}}
    assert r.routewise == {"selected_provider_type": "A"}


def test_routing_info_constructor_routewise_merges_over_strategy_metadata_routewise():
    r = RoutingInfo(
        request_id="rid",
        model="gpt-4",
        strategy_metadata={
            "routewise": {"lp_status": "optimal", "selected_provider_type": "generic"},
            "other": {"x": 1},
        },
        routewise={"selected_provider_type": "legacy"},
    )

    assert r.strategy_metadata == {
        "routewise": {"lp_status": "optimal", "selected_provider_type": "legacy"},
        "other": {"x": 1},
    }


def test_routing_info_replace_returns_new_instance():
    r = RoutingInfo(request_id="abc", model="gpt-4")
    r2 = dataclasses.replace(r, provider="openai", endpoint_id="openai-prod")
    assert r2.provider == "openai"
    assert r2.endpoint_id == "openai-prod"
    # Original unchanged
    assert r.provider is None
    assert r.endpoint_id is None


def test_routing_info_replace_routewise_none_clears_routewise():
    r = RoutingInfo(
        request_id="rid",
        model="gpt-4",
        routewise={"selected_provider_type": "A"},
    )
    r2 = dataclasses.replace(r, routewise=None)

    assert r2.routewise is None
    assert r2.strategy_metadata is None or "routewise" not in r2.strategy_metadata


def test_routing_info_replace_routewise_none_preserves_other_metadata():
    r = RoutingInfo(
        request_id="rid",
        model="gpt-4",
        strategy_metadata={"existing": {"kept": True}},
        routewise={"selected_provider_type": "A"},
    )
    r2 = dataclasses.replace(r, routewise=None)

    assert r2.routewise is None
    assert r2.strategy_metadata == {"existing": {"kept": True}}


def test_routing_info_frozen():
    r = RoutingInfo(request_id="abc", model="gpt-4")
    with pytest.raises(dataclasses.FrozenInstanceError):
        r.provider = "openai"  # type: ignore[misc]


def test_build_initial_routing_info_minimal():
    class _Req:
        model = "gpt-4"

    r = build_initial_routing_info(_Req(), request_id="rid-1", pin_provider=None)
    assert r.request_id == "rid-1"
    assert r.model == "gpt-4"
    assert r.provider is None
    assert r.pricing is None
    assert r.strategy_metadata is None
    assert r.routewise is None
    assert r.upstream_cost_usd is None


def test_build_initial_routing_info_with_pin():
    class _Req:
        model = "gpt-4"

    r = build_initial_routing_info(_Req(), request_id="rid-1", pin_provider="openai")
    assert r.provider == "openai"
    assert r.model == "gpt-4"


def test_build_initial_routing_info_missing_model_attr():
    """Defensive: handler may construct from objects without `.model`."""

    class _Req:
        pass

    r = build_initial_routing_info(_Req(), request_id="rid-x", pin_provider=None)
    assert r.model == ""


def test_merge_adapter_routing_with_none_returns_base():
    base = RoutingInfo(request_id="rid", model="gpt-4")
    assert merge_adapter_routing(base, None) is base
    assert merge_adapter_routing(base, {}) is base


def test_merge_adapter_routing_populates_known_fields():
    base = RoutingInfo(request_id="rid", model="gpt-4")
    routing_dict = {
        "provider": "openai",
        "base_url": "https://api.openai.com/v1",
        "endpoint_id": "openai-prod",
        "pricing": {"prompt": "0.5", "completion": "1.5"},
        "routewise": {"selected_provider_type": "A"},
        "upstream_cost_usd": 0.012,
    }
    enriched = merge_adapter_routing(base, routing_dict)
    assert enriched is not base  # new instance
    assert enriched.provider == "openai"
    assert enriched.base_url == "https://api.openai.com/v1"
    assert enriched.endpoint_id == "openai-prod"
    # PR B: pricing is now a typed ``Pricing | None`` field; the raw adapter
    # dict flows through ``extra["pricing"]`` for the log payload.
    assert enriched.pricing is None
    assert enriched.extra["pricing"] == {"prompt": "0.5", "completion": "1.5"}
    assert enriched.strategy_metadata == {"routewise": {"selected_provider_type": "A"}}
    assert enriched.routewise == {"selected_provider_type": "A"}
    assert enriched.upstream_cost_usd == 0.012
    # Original is untouched (frozen + immutability invariant)
    assert base.provider is None


def test_merge_adapter_routing_merges_strategy_metadata():
    base = RoutingInfo(
        request_id="rid",
        model="gpt-4",
        strategy_metadata={"existing": {"kept": True}},
    )
    enriched = merge_adapter_routing(
        base,
        {
            "strategy_metadata": {"custom": {"value": 1}},
            "routewise": {"selected_provider_type": "quota"},
        },
    )

    assert enriched.strategy_metadata == {
        "existing": {"kept": True},
        "custom": {"value": 1},
        "routewise": {"selected_provider_type": "quota"},
    }
    assert enriched.routewise == {"selected_provider_type": "quota"}


def test_merge_adapter_routing_strategy_metadata_routewise_merges_existing_routewise():
    base = RoutingInfo(
        request_id="rid",
        model="gpt-4",
        strategy_metadata={
            "routewise": {"lp_status": "optimal", "selected_provider_type": "base"},
            "base": {"y": 2},
        },
    )
    enriched = merge_adapter_routing(
        base,
        {
            "strategy_metadata": {
                "routewise": {"selected_provider_type": "incoming", "hedged": True},
                "other": {"x": 1},
            },
        },
    )

    assert enriched.strategy_metadata == {
        "routewise": {
            "lp_status": "optimal",
            "selected_provider_type": "incoming",
            "hedged": True,
        },
        "base": {"y": 2},
        "other": {"x": 1},
    }


def test_merge_adapter_routing_top_level_routewise_merges_over_strategy_metadata_routewise():
    base = RoutingInfo(request_id="rid", model="gpt-4")
    enriched = merge_adapter_routing(
        base,
        {
            "routewise": {"selected_provider_type": "legacy"},
            "strategy_metadata": {
                "routewise": {"selected_provider_type": "generic", "lp_status": "optimal"},
                "other": {"x": 1},
            },
        },
    )
    assert enriched.strategy_metadata == {
        "routewise": {"selected_provider_type": "legacy", "lp_status": "optimal"},
        "other": {"x": 1},
    }


def test_routewise_metadata_shape_for_recent_requests() -> None:
    """RouteWise DB metadata carries provider and hedging details for recent requests."""
    routewise = {
        "selected_provider_type": "on_demand",
        "selected_provider": "openai",
        "selected_endpoint_id": "openai:key-1",
        "hedging_triggered": True,
        "hedge_backup_provider": "anthropic",
        "hedge_backup_endpoint_id": "anthropic:key-2",
        "backup_won": False,
    }

    routing = merge_adapter_routing(
        RoutingInfo(request_id="rid", model="gpt-4"),
        {"provider": "openai", "routewise": routewise},
    )

    assert routing.routewise == routewise


def test_merge_adapter_routing_stashes_unknown_keys_in_extra():
    base = RoutingInfo(request_id="rid", model="gpt-4")
    routing_dict = {
        "provider": "openai",
        "fallback": True,  # not a known field
        "custom": {"a": 1},
    }
    enriched = merge_adapter_routing(base, routing_dict)
    assert enriched.provider == "openai"
    assert enriched.extra == {"fallback": True, "custom": {"a": 1}}


def test_merge_adapter_routing_appends_failed_attempts():
    base = RoutingInfo(
        request_id="rid",
        model="gpt-4",
        extra={"failed_attempts": [{"provider": "primary"}]},
    )
    enriched = merge_adapter_routing(
        base,
        {
            "failed_attempts": [{"provider": "backup"}],
        },
    )

    assert enriched.extra["failed_attempts"] == [
        {"provider": "primary"},
        {"provider": "backup"},
    ]


def test_status_code_from_exception_status_code_attr():
    class _E(Exception):
        status_code = 502

    assert _status_code_from_exception(_E()) == 502


def test_status_code_from_exception_response_status_code():
    class _Resp:
        status_code = 503

    class _E(Exception):
        response = _Resp()

    assert _status_code_from_exception(_E()) == 503


def test_status_code_from_exception_response_status():
    class _Resp:
        status = 504

    class _E(Exception):
        response = _Resp()

    assert _status_code_from_exception(_E()) == 504


def test_status_code_from_exception_status_attr():
    class _E(Exception):
        status = 429

    assert _status_code_from_exception(_E()) == 429


def test_status_code_from_exception_code_attr():
    class _E(Exception):
        code = 500

    assert _status_code_from_exception(_E()) == 500


def test_status_code_from_exception_default():
    assert _status_code_from_exception(RuntimeError("boom")) == 500


def test_status_code_from_exception_none_status_code_falls_through():
    """If status_code is None, fall through to other attributes."""

    class _Resp:
        status_code = 502

    class _E(Exception):
        status_code = None
        response = _Resp()

    assert _status_code_from_exception(_E()) == 502


def test_status_code_from_exception_non_int_status_code_falls_through():
    """If status_code is non-int (e.g., string), fall through."""

    class _E(Exception):
        status_code = "bad"
        status = 408

    assert _status_code_from_exception(_E()) == 408


def test_provider_for_error_prefers_exc_routing_provider():
    """The real upstream provider on ``exc._routing`` wins over the sentinel.

    Regression: real-upstream failures (e.g. a kimi 400) were logged with
    ``provider="router"`` and hidden from the provider-performance aggregations,
    which exclude ``provider IN ('', 'router')``.
    """
    assert _provider_for_error({"provider": "kimi_coding"}) == "kimi_coding"


def test_provider_for_error_defaults_to_router_when_routing_absent():
    assert _provider_for_error(None) == "router"


def test_provider_for_error_keeps_router_sentinel():
    """A pre-routing failure explicitly tagged ``router`` stays ``router``."""
    assert _provider_for_error({"provider": "router"}) == "router"


def test_provider_for_error_ignores_empty_or_missing_provider():
    assert _provider_for_error({"base_url": "https://x"}) == "router"
    assert _provider_for_error({"provider": ""}) == "router"


def test_provider_for_error_falls_back_to_live_request_context():
    """With no routing block, use a provider still live in req_ctx, else sentinel."""
    with req_ctx.push(provider="deepseek"):
        assert _provider_for_error(None) == "deepseek"
    # Outside the push scope the context is reset, so we fall back to "router".
    assert _provider_for_error(None) == "router"
