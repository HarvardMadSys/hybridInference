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
    assert sglang_route["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}


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

    assert minimax_fast is not None, "Model 'minimax-fast' not found in config/models.yaml"
    assert minimax_fast["name"] == "MiniMax Fast"
    assert minimax_fast["supported_params"] == [
        "temperature",
        "top_p",
        "top_k",
        "max_tokens",
        "stop",
        "seed",
        "stream",
    ]
    assert minimax_fast["router"] == "routewise"
    assert minimax_fast["router_params"]["budget_alpha"] == 0.5
    assert minimax_fast["router_params"]["latency_hedge_mode"] == "probability_target"
    assert minimax_fast["aliases"] == ["MiniMax-Fast"]
    routes_by_type = {route["provider_type"]: route for route in minimax_fast["route"]}
    assert set(routes_by_type) == {"concurrency", "on_demand", "quota"}
    # Resource limits live on the route entries, not in router_params.
    assert "concurrency_enabled" not in minimax_fast["router_params"]
    assert "concurrency_limit" not in minimax_fast["router_params"]
    assert routes_by_type["quota"]["quota"]["limit"] == 5000
    assert routes_by_type["quota"]["quota_source"]["provider"] == "chutes"
    assert routes_by_type["concurrency"]["concurrency"]["limit"] == 1


def test_minimax_fast_lists_routewise_options_in_comments() -> None:
    """RouteWise example config should keep all tunable options visible.

    Derived from the ``RouteWiseConfig`` dataclass so the reference block in
    ``models.yaml`` cannot silently go stale when fields change.
    """
    from dataclasses import fields as dataclass_fields

    from routing.routewise.config import RouteWiseConfig

    text = (ROOT / "config" / "models.yaml").read_text()

    for field in dataclass_fields(RouteWiseConfig):
        assert field.name in text, (
            f"models.yaml RouteWise reference block is missing option {field.name!r}"
        )
    # Route-level resource blocks are documented alongside the algorithm knobs.
    for route_option in ("quota:", "concurrency:", "quota_pool:", "concurrency_pool:"):
        assert route_option in text
