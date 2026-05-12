from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]


def test_sglang_local_route_uses_local_deployment_url() -> None:
    """SGLang routes must not reuse LOCAL_BASE_URL for upstream deployment URL."""
    models = yaml.safe_load((ROOT / "config" / "models.yaml").read_text())

    qwen = next((model for model in models["models"] if model["id"] == "qwen3.6-35b"), None)
    assert qwen is not None, "Model 'qwen3.6-35b' not found in config/models.yaml"
    sglang_route = next((route for route in qwen["route"] if route["kind"] == "sglang"), None)
    assert sglang_route is not None, "SGLang route not found for model 'qwen3.6-35b'"

    assert sglang_route["base_url"] == "${LOCAL_DEPLOYMENT_URL}"


def test_routing_local_deployment_uses_local_deployment_url() -> None:
    """Routing local_deployment must match the SGLang deployment env var."""
    routing = yaml.safe_load((ROOT / "config" / "routing.yaml").read_text())

    endpoints = [deployment["endpoint"] for deployment in routing["local_deployment"]]

    assert "${LOCAL_DEPLOYMENT_URL}" in endpoints
    assert "${LOCAL_BASE_URL}" not in endpoints


def test_minimax_fast_uses_routewise() -> None:
    """minimax-fast should exist as a RouteWise-routed model."""
    models = yaml.safe_load((ROOT / "config" / "models.yaml").read_text())["models"]

    minimax_fast = next((model for model in models if model["id"] == "minimax-fast"), None)

    assert minimax_fast is not None
    assert minimax_fast["name"] == "MiniMax Fast"
    assert minimax_fast["router"] == "routewise"
    assert minimax_fast["router_params"]["predictor"] == "histogram"
    assert minimax_fast["router_params"]["latency_cost_budget_alpha"] == 0.5
    assert minimax_fast["router_params"]["latency_hedge_success_target"] == 0.99
    assert minimax_fast["aliases"] == ["MiniMax-Fast"]
    assert {route["subscription_type"] for route in minimax_fast["route"]} == {"api"}
