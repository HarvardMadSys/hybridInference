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


def test_empty_catalog_names_the_variable_that_would_fix_it(monkeypatch, caplog):
    """Silence here is what makes the quickstart look broken rather than unset.

    Every model is skipped when its credential is missing, one warning apiece.
    What the person following the quickstart actually sees is an empty
    ``/v1/models`` and then "Model not found" — which reads as a wrong model
    id, so they go looking for a typo instead of an export they skipped.
    """
    import logging

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    exe = RouteExecutor()
    with caplog.at_level(logging.ERROR, logger="serving.servers.registry"):
        count, _infos = registry.register_from_models_yaml(
            exe, EXAMPLE_YAML, continue_on_missing_env=True
        )

    assert count == 0
    errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert errors, "an empty catalog must say so at error level, not only per-model"
    assert "OPENROUTER_API_KEY" in errors[0], (
        "the message has to name the variable to set; without it the reader "
        "still has to guess which credential was missing"
    )


def test_quickstart_routing_example_declares_no_endpoint():
    """The quickstart's routing file must not send a clone at anyone's machines.

    Pointing only MODELS_CONFIG_PATH at the example leaves config/routing.yaml
    — the operator's own deployment map — in play, which greets a newcomer with
    warnings about hosts they do not have, and prints those hosts. The example
    pairs with a routing file that names none.
    """
    import yaml

    example = REPO_ROOT / "config" / "examples" / "routing.minimal.yaml"
    assert example.exists(), "README.md#quickstart points ROUTING_CONFIG_PATH here"

    config = yaml.safe_load(example.read_text())
    assert config["local_deployment"] == []
    assert config["remote_deployment"] == []
    # Deprecated spellings would make the example emit the migration warning it
    # is meant to keep a newcomer from seeing.
    assert "routing_strategy" not in config
    assert "routing_parameter" not in config

    readme = (REPO_ROOT / "README.md").read_text()
    assert "ROUTING_CONFIG_PATH=config/examples/routing.minimal.yaml" in readme
