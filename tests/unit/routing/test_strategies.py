"""Unit tests for the routing strategy registry."""

from __future__ import annotations

import pytest


@pytest.mark.unit
def test_register_strategy_adds_entry():
    from pydantic import BaseModel

    from routing.strategies import _STRATEGIES, build_router, register_strategy

    class _Params(BaseModel):
        model_config = {"extra": "forbid"}

    class _Router:
        def __init__(self, params=None):
            self.params = params

    register_strategy("__test_register__")((_Router, _Params))
    try:
        assert "__test_register__" in _STRATEGIES
        router = build_router("__test_register__", {})
        assert isinstance(router, _Router)
        assert isinstance(router.params, _Params)
    finally:
        _STRATEGIES.pop("__test_register__", None)


@pytest.mark.unit
def test_validate_router_config_does_not_construct_router():
    from pydantic import BaseModel

    from routing.dependencies import RouterBuildDependencies
    from routing.endpoint_health import EndpointHealthRegistry
    from routing.strategies import _STRATEGIES, register_strategy, validate_router_config

    class _Params(BaseModel):
        model_config = {"extra": "forbid"}

        value: int = 1

    class _Router:
        constructor_calls = 0

        def __init__(self, params=None, *, health_registry=None):
            type(self).constructor_calls += 1
            self.params = params
            self.health_registry = health_registry

    name = "__test_pure_validation__"
    register_strategy(name)((_Router, _Params))
    dependencies = RouterBuildDependencies(
        health_registry=EndpointHealthRegistry(),
    )
    try:
        validated = validate_router_config(
            name,
            {"value": 7},
            dependencies=dependencies,
        )

        assert validated.value == 7
        assert _Router.constructor_calls == 0
    finally:
        _STRATEGIES.pop(name, None)


@pytest.mark.unit
def test_dependency_aware_factory_rejects_params_only_strategy_clearly():
    from pydantic import BaseModel

    from routing.dependencies import RouterBuildDependencies
    from routing.endpoint_health import EndpointHealthRegistry
    from routing.strategies import _STRATEGIES, build_router, register_strategy

    class _Params(BaseModel):
        model_config = {"extra": "forbid"}

    class _ParamsOnlyRouter:
        def __init__(self, params=None):
            self.params = params

    name = "__test_params_only_with_dependencies__"
    register_strategy(name)((_ParamsOnlyRouter, _Params))
    dependencies = RouterBuildDependencies(
        health_registry=EndpointHealthRegistry(),
    )
    try:
        with pytest.raises(TypeError, match=r"must accept health_registry="):
            build_router(name, {}, dependencies=dependencies)
    finally:
        _STRATEGIES.pop(name, None)


@pytest.mark.unit
def test_dependency_aware_validation_rejects_params_only_strategy_without_constructing():
    from pydantic import BaseModel

    from routing.dependencies import RouterBuildDependencies
    from routing.endpoint_health import EndpointHealthRegistry
    from routing.strategies import _STRATEGIES, register_strategy, validate_router_config

    class _Params(BaseModel):
        model_config = {"extra": "forbid"}

    class _ParamsOnlyRouter:
        constructor_calls = 0

        def __init__(self, params=None):
            type(self).constructor_calls += 1
            self.params = params

    name = "__test_validate_params_only_with_dependencies__"
    register_strategy(name)((_ParamsOnlyRouter, _Params))
    dependencies = RouterBuildDependencies(
        health_registry=EndpointHealthRegistry(),
    )
    try:
        with pytest.raises(TypeError, match=r"must accept health_registry="):
            validate_router_config(name, {}, dependencies=dependencies)
        assert _ParamsOnlyRouter.constructor_calls == 0
    finally:
        _STRATEGIES.pop(name, None)


@pytest.mark.unit
def test_build_router_unknown_raises_with_known_list():
    from routing.strategies import build_router

    with pytest.raises(ValueError) as exc:
        build_router("__unknown__", {})
    msg = str(exc.value)
    assert "__unknown__" in msg
    assert "known:" in msg


@pytest.mark.unit
def test_build_router_validates_params_strict():
    from pydantic import ValidationError

    from routing.strategies import build_router

    with pytest.raises(ValidationError):
        build_router("fixed", {"unknown_key": 1})


@pytest.mark.unit
def test_validate_router_config_validates_params_strict_without_constructing(monkeypatch):
    from unittest.mock import MagicMock

    from pydantic import ValidationError

    from routing.routers import FixedRouter
    from routing.strategies import validate_router_config

    constructor = MagicMock(side_effect=AssertionError("router must not be constructed"))
    monkeypatch.setattr(FixedRouter, "__init__", constructor)

    with pytest.raises(ValidationError):
        validate_router_config("fixed", {"unknown_key": 1})
    constructor.assert_not_called()


@pytest.mark.unit
def test_fixed_params_local_fraction_range():
    from pydantic import ValidationError

    from routing.strategies.fixed import FixedParams

    # In range
    assert FixedParams.model_validate({"local_fraction": 0.0}).local_fraction == 0.0
    assert FixedParams.model_validate({"local_fraction": 1.0}).local_fraction == 1.0
    # Defaults
    assert FixedParams().local_fraction == 0.5
    # Out of range
    with pytest.raises(ValidationError):
        FixedParams.model_validate({"local_fraction": 1.5})
    with pytest.raises(ValidationError):
        FixedParams.model_validate({"local_fraction": -0.1})


@pytest.mark.unit
def test_build_fixed_returns_fixed_router():
    from routing.protocols import RouterProtocol
    from routing.routers import FixedRouter
    from routing.strategies import build_router

    router = build_router("fixed", {"local_fraction": 0.7})
    assert isinstance(router, FixedRouter)
    assert isinstance(router, RouterProtocol)


@pytest.mark.unit
def test_build_router_injects_explicit_shared_dependencies():
    from routing.dependencies import RouterBuildDependencies
    from routing.endpoint_health import EndpointHealthRegistry
    from routing.protocols import RouterProtocol
    from routing.strategies import build_router

    health_registry = EndpointHealthRegistry()
    dependencies = RouterBuildDependencies(health_registry=health_registry)

    fixed = build_router("fixed", {}, dependencies=dependencies)
    routewise = build_router("routewise", {}, dependencies=dependencies)

    assert fixed._health_registry is health_registry
    assert routewise._health_registry is health_registry
    assert isinstance(fixed, RouterProtocol)
    assert isinstance(routewise, RouterProtocol)


@pytest.mark.unit
def test_routewise_params_extra_forbidden():
    from pydantic import ValidationError

    from routing.strategies.routewise import RouteWiseParams

    # Defaults work
    RouteWiseParams()
    # Known field accepted
    p = RouteWiseParams.model_validate({"latency_min_samples": 100})
    assert p.latency_min_samples == 100
    # Unknown field rejected
    with pytest.raises(ValidationError):
        RouteWiseParams.model_validate({"not_a_field": 1})
    # Removed legacy hedge knobs are rejected.
    with pytest.raises(ValidationError):
        RouteWiseParams.model_validate({"latency_hedge_cost_ratio": 0.1})
    # Moved resource fields are rejected with a pointer to the new location.
    with pytest.raises(ValidationError, match=r"route-level quota\.limit"):
        RouteWiseParams.model_validate({"daily_quota": 100})
    with pytest.raises(ValidationError, match=r"route-level concurrency\.limit"):
        RouteWiseParams.model_validate({"concurrency_limit": 4})


@pytest.mark.unit
def test_routewise_params_reject_legacy_hedge_modes():
    from pydantic import ValidationError

    from routing.strategies.routewise import RouteWiseParams

    with pytest.raises(ValidationError):
        RouteWiseParams.model_validate({"latency_hedge_mode": "shadow"})
    with pytest.raises(ValidationError):
        RouteWiseParams.model_validate({"latency_hedge_mode": "economic"})


@pytest.mark.unit
def test_routewise_params_mirror_routewise_config_fields():
    """Pydantic params must cover every RouteWiseConfig dataclass field."""
    from dataclasses import fields

    from routing.routewise.config import RouteWiseConfig
    from routing.strategies.routewise import RouteWiseParams

    rw_field_names = {f.name for f in fields(RouteWiseConfig)}
    pydantic_field_names = set(RouteWiseParams.model_fields.keys())

    missing = rw_field_names - pydantic_field_names
    extra = pydantic_field_names - rw_field_names
    assert not missing, f"RouteWiseParams missing fields: {sorted(missing)}"
    assert not extra, f"RouteWiseParams has extra fields: {sorted(extra)}"


@pytest.mark.unit
def test_routewise_params_defaults_match_routewise_config():
    from dataclasses import fields

    from routing.routewise.config import RouteWiseConfig
    from routing.strategies.routewise import RouteWiseParams

    cfg = RouteWiseConfig()
    params = RouteWiseParams()

    for field in fields(RouteWiseConfig):
        assert getattr(params, field.name) == getattr(cfg, field.name)


@pytest.mark.unit
def test_build_routewise_returns_routewise_router():
    """build_router('routewise', {...}) returns a RouteWiseRouter instance."""
    from routing.protocols import RouterProtocol
    from routing.routewise.router import RouteWiseRouter
    from routing.strategies import build_router

    # Without a route table, RouteWiseRouter defers post-init classification.
    router = build_router("routewise", {"latency_min_samples": 100})
    assert isinstance(router, RouteWiseRouter)
    assert isinstance(router, RouterProtocol)
    assert router.config.latency_min_samples == 100
    # route_table is None until attach_route_table is called.
    assert router.route_table is None


@pytest.mark.unit
def test_missing_strategy_fails_validation_with_actionable_message(monkeypatch):
    """A known-but-uninstalled strategy fails config validation, not import."""
    import routing.strategies as strategies_module
    from routing.strategies import validate_router_config

    monkeypatch.delitem(strategies_module._STRATEGIES, "ghost", raising=False)
    monkeypatch.setitem(
        strategies_module._MISSING_STRATEGIES,
        "ghost",
        "router strategy 'ghost' requires the optional 'ghost' extra",
    )
    with pytest.raises(ValueError, match="optional 'ghost' extra"):
        validate_router_config("ghost", {})


@pytest.mark.unit
def test_installed_strategy_wins_over_a_missing_marker(monkeypatch):
    """register_strategy after register_missing_strategy takes precedence."""
    import routing.strategies as strategies_module
    from routing.strategies import validate_router_config

    monkeypatch.setitem(strategies_module._MISSING_STRATEGIES, "routewise", "should never surface")
    # routewise is installed in this environment, so validation still works.
    params = validate_router_config("routewise", {})
    assert params is not None


@pytest.mark.unit
def test_unknown_strategy_error_is_unchanged(monkeypatch):
    """Names in neither registry keep the known-strategies error."""
    from routing.strategies import validate_router_config

    with pytest.raises(ValueError, match="unknown router strategy 'nope'"):
        validate_router_config("nope", {})
