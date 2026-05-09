"""Integration: a small models.yaml fixture drives ModelRouterRegistry dispatch.

Loads a fixture with mixed `router: fixed`, `router: routewise`, and
unspecified entries; constructs ModelRouterRegistry; asserts the right
router class per model; smoke-tests the dispatch path.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.mark.integration
def test_models_yaml_drives_router_dispatch(tmp_path):
    from routing.model_router_registry import ModelRouterRegistry
    from routing.routers import FixedRouter
    from routing.routewise.router import RouteWiseRouter
    from serving.servers import registry as serving_registry

    # Each model defines `pricing:` so RouteWise's API-baseline classification
    # has the per-token rates it expects when it runs against the shared
    # FixedRouter's routes dict.
    yaml = """
models:
  - id: m-default
    name: M-default
    provider: openai_compat
    base_url: http://example.com/v1
    pricing:
      prompt: "1.0"
      completion: "2.0"
    route:
      - kind: openai_compat
        weight: 1.0
        base_url: http://example.com/v1
  - id: m-fixed
    name: M-fixed
    provider: openai_compat
    base_url: http://example.com/v1
    router: fixed
    router_params:
      local_fraction: 0.7
    pricing:
      prompt: "1.0"
      completion: "2.0"
    route:
      - kind: openai_compat
        weight: 1.0
        base_url: http://example.com/v1
  - id: m-routewise
    name: M-routewise
    provider: openai_compat
    base_url: http://example.com/v1
    router: routewise
    router_params:
      daily_quota: 100
    pricing:
      prompt: "1.0"
      completion: "2.0"
    route:
      - kind: openai_compat
        weight: 1.0
        base_url: http://example.com/v1
"""
    p = tmp_path / "models.yaml"
    p.write_text(yaml)
    fixed = FixedRouter()
    _count, infos = serving_registry.register_from_models_yaml(fixed, Path(p))

    models_config: dict[str, dict] = {}
    for info in infos:
        entry: dict = {}
        if info.router is not None:
            entry["router"] = info.router
        if info.router_params is not None:
            entry["router_params"] = info.router_params
        models_config[info.model_id] = entry

    reg = ModelRouterRegistry(
        models_config=models_config,
        default_router_name="fixed",
    )
    reg.bind_fixed_router(fixed)

    r_default = reg.get_router("m-default")
    r_fixed = reg.get_router("m-fixed")
    r_rw = reg.get_router("m-routewise")

    assert isinstance(r_default, FixedRouter)
    assert isinstance(r_fixed, FixedRouter)
    assert isinstance(r_rw, RouteWiseRouter)
    # Both fixed-strategy routers must be the SAME instance as the shared
    # FixedRouter populated by register_from_models_yaml.  Otherwise the
    # registry hands back a fresh empty FixedRouter and requests fail with
    # "No route configured for model" at dispatch time.
    assert r_default is fixed
    assert r_fixed is fixed
    # Routes for both fixed-strategy models are registered on the shared
    # instance returned by the registry.
    assert "m-default" in r_default.routes
    assert "m-fixed" in r_fixed.routes
    # RouteWise picked up the override.
    assert r_rw.config.daily_quota == 100
    # Cache identity preserved.
    assert reg.get_router("m-default") is r_default


@pytest.mark.integration
def test_models_yaml_unknown_strategy_fails_loudly():
    """A typo in `router:` raises at first get_router call, not silently."""
    from routing.model_router_registry import ModelRouterRegistry

    reg = ModelRouterRegistry(
        models_config={"m": {"router": "nonexistent_strategy"}},
        default_router_name="fixed",
    )
    with pytest.raises(ValueError) as exc:
        reg.get_router("m")
    assert "nonexistent_strategy" in str(exc.value)


@pytest.mark.integration
def test_models_yaml_bad_router_params_fails_loudly():
    """A bad value in `router_params:` raises at first get_router call."""
    from pydantic import ValidationError

    from routing.model_router_registry import ModelRouterRegistry

    reg = ModelRouterRegistry(
        models_config={"m": {"router": "fixed", "router_params": {"local_fraction": 9.9}}},
        default_router_name="fixed",
    )
    with pytest.raises(ValidationError):
        reg.get_router("m")
