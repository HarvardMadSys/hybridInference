"""Contracts for candidate-range views over the read-only route table."""

from __future__ import annotations

from typing import Any

import pytest

from routing.route_scope import (
    RouteScopeView,
    adapter_in_endpoint_scope,
    endpoint_ids_in_view,
    scope_view_for_endpoints,
)
from routing.route_table import EffectiveRoute, RouteTableView
from routing.routers import FixedRouter


class _FakeAdapter:
    """Minimal adapter stand-in exposing the config attributes routing reads."""

    def __init__(self, endpoint_id: str, provider: str | None = None) -> None:
        self.config = _FakeConfig(endpoint_id, provider or endpoint_id)
        self.calls: list[dict[str, Any]] = []


class _FakeConfig:
    """Config stand-in; routing only reads ``endpoint_id`` and ``provider``."""

    def __init__(self, endpoint_id: str, provider: str) -> None:
        self.endpoint_id = endpoint_id
        self.provider = provider


def _adapter(endpoint_id: str, provider: str | None = None) -> _FakeAdapter:
    return _FakeAdapter(endpoint_id, provider)


def _table(*routes: tuple[str, list[_FakeAdapter]]) -> FixedRouter:
    table = FixedRouter()
    for model_id, adapters in routes:
        table.register_route(model_id, [(adapter, 1.0) for adapter in adapters])
    return table


@pytest.mark.unit
def test_adapter_in_endpoint_scope_matches_endpoint_id_or_provider() -> None:
    adapter = _adapter("model:cloud-api", provider="zai")

    assert adapter_in_endpoint_scope(adapter, frozenset({"model:cloud-api"}))
    assert adapter_in_endpoint_scope(adapter, frozenset({"zai"}))
    assert not adapter_in_endpoint_scope(adapter, frozenset({"model:local-12003"}))


@pytest.mark.unit
def test_adapter_in_endpoint_scope_selects_nothing_when_scope_is_empty() -> None:
    adapter = _adapter("model:cloud-api", provider="zai")

    assert not adapter_in_endpoint_scope(adapter, frozenset())
    assert not adapter_in_endpoint_scope(adapter, set())


@pytest.mark.unit
def test_scope_view_filters_adapters_and_drops_empty_routes() -> None:
    local = _adapter("model:local-12003", provider="local")
    cloud = _adapter("model:cloud-api", provider="zai")
    other = _adapter("other:cloud-api", provider="zai")
    table = _table(("model", [local, cloud]), ("other", [other]))

    view = scope_view_for_endpoints(table, {"model:cloud-api"})
    snapshot = view.iter_effective_routes()

    assert isinstance(view, RouteTableView)
    assert len(snapshot) == 1
    assert snapshot[0].canonical_model_id == "model"
    # Captured weights are preserved, not renormalized: 0.5 is this cloud
    # adapter's share of the full local+cloud pool, which is the ratio
    # RouteWise's LP already priced.
    assert snapshot[0].adapters == ((cloud, 0.5),)
    assert view.source is table


@pytest.mark.unit
def test_scope_view_drops_a_model_whose_candidates_are_all_out_of_scope() -> None:
    table = _table(("model", [_adapter("model:local-12003", provider="local")]))

    view = scope_view_for_endpoints(table, {"model:cloud-api"})

    assert view.iter_effective_routes() == ()


@pytest.mark.unit
def test_scope_view_narrows_to_the_declared_model_scope() -> None:
    table = _table(
        ("model-a", [_adapter("model-a:cloud-api")]),
        ("model-b", [_adapter("model-b:cloud-api")]),
    )

    view = scope_view_for_endpoints(
        table,
        {"model-a:cloud-api", "model-b:cloud-api"},
        model_scope={"model-a"},
    )

    assert view.model_scope == frozenset({"model-a"})
    assert [route.canonical_model_id for route in view.iter_effective_routes()] == ["model-a"]
    assert view.includes_model("model-a")
    assert not view.includes_model("model-b")


@pytest.mark.unit
def test_scope_view_delegates_canonical_id_resolution() -> None:
    adapter = _adapter("model:cloud-api")
    table = FixedRouter()
    table.register_route("model", [(adapter, 1.0)], aliases=["model-alias"])

    view = scope_view_for_endpoints(table, {"model:cloud-api"})

    assert view.canonical_id("model-alias") == "model"
    assert view.canonical_id("unknown") == "unknown"


@pytest.mark.unit
def test_scope_view_cache_can_be_cleared_after_the_source_changes() -> None:
    first = _adapter("model:cloud-api")
    table = _table(("model", [first]))
    view = scope_view_for_endpoints(table, {"model:cloud-api", "model:cloud-api-2"})

    assert view.iter_effective_routes()[0].adapters == ((first, 1.0),)

    second = _adapter("model:cloud-api-2")
    table.register_route("model", [(first, 1.0), (second, 1.0)])
    assert view.iter_effective_routes()[0].adapters == ((first, 1.0),)

    view.clear_cache()
    assert view.iter_effective_routes()[0].adapters == ((first, 0.5), (second, 0.5))


@pytest.mark.unit
def test_scope_view_narrows_endpoint_ids_to_the_declared_range() -> None:
    local = _adapter("model:local-12003", provider="local")
    cloud = _adapter("model:cloud-api", provider="zai")
    table = _table(("model", [local, cloud]))

    view = scope_view_for_endpoints(table, {"model:cloud-api"})

    assert endpoint_ids_in_view(view) == frozenset({"model:cloud-api"})
    assert view.includes_adapter(cloud)
    assert not view.includes_adapter(local)


@pytest.mark.unit
def test_scope_view_keeps_effective_route_snapshot_immutable() -> None:
    adapter = _adapter("model:cloud-api")
    table = _table(("model", [adapter]))

    route = scope_view_for_endpoints(table, {"model:cloud-api"}).iter_effective_routes()[0]

    assert isinstance(route, EffectiveRoute)
    assert isinstance(route.adapters, tuple)
    assert isinstance(
        RouteScopeView(
            table,
            filter=lambda _adapter, _weight: True,
        ).iter_effective_routes(),
        tuple,
    )
