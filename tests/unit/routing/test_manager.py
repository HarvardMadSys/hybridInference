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
        "default_router: fixed\n"
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
