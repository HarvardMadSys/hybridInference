"""The neutral OpenRouter reference registry must load through the real path.

config/examples/models.openrouter.yaml is the out-of-the-box catalog for a
fresh deployment (the "default route": one OPENROUTER_API_KEY and requests
flow, plus a local-first hybrid demo). This test freezes that promise: the
example registers through ``register_from_models_yaml`` exactly like the
production catalog does.
"""

from __future__ import annotations

import re
from pathlib import Path

from routing.executor import RouteExecutor
from serving.servers import registry

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_YAML = REPO_ROOT / "config" / "examples" / "models.openrouter.yaml"


def test_example_registry_registers_with_one_key(monkeypatch):
    raw = EXAMPLE_YAML.read_text()

    referenced = sorted(set(re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*)", raw)))
    # The whole point of the reference registry: exactly one credential.
    assert referenced == ["OPENROUTER_API_KEY"]
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-example-dummy")

    exe = RouteExecutor()
    count, infos = registry.register_from_models_yaml(exe, EXAMPLE_YAML)

    assert count == 3
    assert set(exe.routes) == {"llama-3.3-70b", "llama-3.1-8b", "llama-3.1-8b-hybrid"}
    assert {i.model_id for i in infos} == set(exe.routes)

    # The hybrid entry is the product claim in miniature: a local
    # OpenAI-compatible route that is preferred, plus an OpenRouter fallback
    # that only takes over when the local server is unreachable.
    hybrid = exe.routes["llama-3.1-8b-hybrid"]
    weights = [weight for _adapter, weight in hybrid.adapters]
    assert len(weights) == 2, "hybrid model must keep both the local and fallback routes"
    assert weights[0] > weights[1], "the local route must be the preferred one"

    # A key-less local route must not be dropped at load time; that would
    # silently turn the hybrid demo into a remote-only one.
    local_adapter, _weight = hybrid.adapters[0]
    assert "localhost" in local_adapter.config.base_url
