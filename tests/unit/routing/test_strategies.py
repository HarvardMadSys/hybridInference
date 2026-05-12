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
    from routing.routers import FixedRouter
    from routing.strategies import build_router

    router = build_router("fixed", {"local_fraction": 0.7})
    assert isinstance(router, FixedRouter)


@pytest.mark.unit
def test_routewise_params_extra_forbidden():
    from pydantic import ValidationError

    from routing.strategies.routewise import RouteWiseParams

    # Defaults work
    RouteWiseParams()
    # Known field accepted
    p = RouteWiseParams.model_validate({"daily_quota": 100})
    assert p.daily_quota == 100
    # Unknown field rejected
    with pytest.raises(ValidationError):
        RouteWiseParams.model_validate({"not_a_field": 1})


@pytest.mark.unit
def test_routewise_params_canary_enabled_models_validation():
    from pydantic import ValidationError

    from routing.strategies.routewise import RouteWiseParams

    assert (
        RouteWiseParams.model_validate({"canary_enabled_models": None}).canary_enabled_models
        is None
    )
    assert RouteWiseParams.model_validate(
        {"canary_enabled_models": ["deepseek-chat", "gpt-4o-mini"]}
    ).canary_enabled_models == ["deepseek-chat", "gpt-4o-mini"]

    with pytest.raises(ValidationError):
        RouteWiseParams.model_validate({"canary_enabled_models": "deepseek-chat"})


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
    from routing.routewise.router import RouteWiseRouter
    from routing.strategies import build_router

    # Without a fixed_router, RouteWiseRouter defers post-init classification.
    router = build_router("routewise", {"daily_quota": 100})
    assert isinstance(router, RouteWiseRouter)
    assert router.config.daily_quota == 100
    # fixed_router is None until attach_fixed_router is called.
    assert router.fixed_router is None
