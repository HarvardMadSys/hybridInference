from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from routing.executor import RouteExecutor
from routing.manager import RoutingManager
from serving.servers.registry import register_from_models_yaml


@pytest.mark.unit
def test_apply_keeps_alias_and_canonical_route_in_sync(tmp_path: Path):
    """RoutingManager.apply should not split alias and canonical route configs."""
    models_yaml = (
        "models:\n"
        "  - id: canonical-model\n"
        "    aliases: [alias-model]\n"
        "    name: Canonical Model\n"
        "    provider: openai_compat\n"
        "    base_url: http://localhost:8004/v1\n"
        "    context_length: 8192\n"
        "    max_output_length: 1024\n"
        "    route:\n"
        "      - kind: openai_compat\n"
        "        weight: 0.5\n"
        "        base_url: http://localhost:8004/v1\n"
        "      - kind: openai_compat\n"
        "        weight: 0.5\n"
        "        base_url: https://remote.example/v1\n"
    )
    routing_yaml = (
        "routing_strategy: fixed\n"
        "routing_parameter:\n"
        "  local_fraction: 1.0\n"
        "local_deployment:\n"
        "  - endpoint: http://localhost:8004/v1\n"
        "    models: [canonical-model]\n"
        "remote_deployment:\n"
        "  - endpoint: https://remote.example/v1\n"
        "    models: [canonical-model]\n"
    )

    models_path = tmp_path / "models.yaml"
    routing_path = tmp_path / "routing.yaml"
    models_path.write_text(models_yaml)
    routing_path.write_text(routing_yaml)

    router = RouteExecutor()
    registered, _infos = register_from_models_yaml(router, models_path)
    assert registered == 2
    assert router.routes["canonical-model"] is router.routes["alias-model"]

    manager = RoutingManager(router, routing_path)
    manager.load()
    updated = manager.apply()
    assert updated == 1

    canonical_weights = [weight for _, weight in router.routes["canonical-model"].adapters]
    alias_weights = [weight for _, weight in router.routes["alias-model"].adapters]
    assert canonical_weights == [1.0, 0.0]
    assert alias_weights == [1.0, 0.0]
    assert router.routes["canonical-model"] is router.routes["alias-model"]


@pytest.mark.unit
def test_apply_renormalizes_when_routing_yaml_covers_subset(tmp_path: Path):
    """Regression: routing.yaml listing only some adapter endpoints must not
    inflate the covered adapter past 1.0, which would starve uncovered routes.

    Reproduces the GLM-5 bug: when only the zai endpoint was listed under
    remote_deployment, FixedRatioStrategy assigned zai weight=1.0 while
    ollama/chutes/featherless retained their pre-normalized weights ~0.33.
    Without renormalization the weight sum exceeded 1.0 and FixedRouter's
    cumulative-weight walk always returned the first adapter.
    """
    models_yaml = (
        "models:\n"
        "  - id: m\n"
        "    name: M\n"
        "    provider: openai_compat\n"
        "    base_url: https://a.example/v1\n"
        "    context_length: 8192\n"
        "    max_output_length: 1024\n"
        "    route:\n"
        "      - kind: openai_compat\n"
        "        weight: 1.0\n"
        "        base_url: https://a.example/v1\n"
        "      - kind: openai_compat\n"
        "        weight: 1.0\n"
        "        base_url: https://b.example/v1\n"
        "      - kind: openai_compat\n"
        "        weight: 1.0\n"
        "        base_url: https://c.example/v1\n"
    )
    routing_yaml = (
        "routing_strategy: fixed\n"
        "routing_parameter:\n"
        "  local_fraction: 0.0\n"
        "local_deployment: []\n"
        "remote_deployment:\n"
        "  - endpoint: https://a.example/v1\n"
        "    models: [m]\n"
    )

    models_path = tmp_path / "models.yaml"
    routing_path = tmp_path / "routing.yaml"
    models_path.write_text(models_yaml)
    routing_path.write_text(routing_yaml)

    router = RouteExecutor()
    registered, _infos = register_from_models_yaml(router, models_path)
    assert registered == 1

    manager = RoutingManager(router, routing_path)
    manager.load()
    manager.apply()

    weights = [w for _, w in router.routes["m"].adapters]
    assert abs(sum(weights) - 1.0) < 1e-9, f"weights {weights} must renormalize to 1.0"
    # Adapter A (covered by routing.yaml) gets the strategy-assigned 1.0;
    # B and C carry over their pre-normalized 1/3 each. After renormalization
    # the total of 1.0 + 1/3 + 1/3 = 5/3 yields A=0.6, B=C=0.2.
    assert abs(weights[0] - 0.6) < 1e-9
    assert abs(weights[1] - 0.2) < 1e-9
    assert abs(weights[2] - 0.2) < 1e-9
