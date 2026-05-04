"""Tests for routing_info dataclasses."""

from __future__ import annotations

import dataclasses

import pytest

from serving.servers.routers.routing_info import (
    Pricing,
    RouteWiseDecision,
    RoutingInfo,
    _status_code_from_exception,
    build_initial_routing_info,
    merge_adapter_routing,
)


def test_pricing_frozen():
    p = Pricing(input_per_1k=0.5, output_per_1k=1.5)
    with pytest.raises(dataclasses.FrozenInstanceError):
        p.input_per_1k = 0.0  # type: ignore[misc]


def test_routewise_decision_defaults():
    rw = RouteWiseDecision()
    assert rw.selected_tier is None
    assert rw.quota_committed == 0.0
    assert rw.sc_committed is False
    assert rw.hedged is False
    assert rw.backup_won is False
    assert rw.extra == {}


def test_routing_info_replace_returns_new_instance():
    r = RoutingInfo(request_id="abc", model="gpt-4")
    r2 = dataclasses.replace(r, provider="openai", endpoint_id="openai-prod")
    assert r2.provider == "openai"
    assert r2.endpoint_id == "openai-prod"
    # Original unchanged
    assert r.provider is None
    assert r.endpoint_id is None


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
        "routewise": {"selected_tier": "A"},
        "upstream_cost_usd": 0.012,
    }
    enriched = merge_adapter_routing(base, routing_dict)
    assert enriched is not base  # new instance
    assert enriched.provider == "openai"
    assert enriched.base_url == "https://api.openai.com/v1"
    assert enriched.endpoint_id == "openai-prod"
    assert enriched.pricing == {"prompt": "0.5", "completion": "1.5"}
    assert enriched.routewise == {"selected_tier": "A"}
    assert enriched.upstream_cost_usd == 0.012
    # Original is untouched (frozen + immutability invariant)
    assert base.provider is None


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
