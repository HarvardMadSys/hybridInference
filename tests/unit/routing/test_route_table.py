"""Contracts for the read-only route-table composition port."""

from __future__ import annotations

import threading
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
from typing import Any

import pytest

from routing.route_table import EffectiveRoute, RouteTableView
from routing.routers import FixedRouter, RouteConfig


def _adapter(
    model_id: str,
    endpoint_id: str,
    *,
    provider: str | None = None,
) -> Any:
    return SimpleNamespace(
        config=SimpleNamespace(
            id=model_id,
            endpoint_id=endpoint_id,
            provider=provider or endpoint_id,
        )
    )


class _SnapshotWeightResolver:
    def __init__(self, overrides: dict[str, dict[str, float]]) -> None:
        self.overrides = overrides
        self.requested_models: list[str] = []

    def get_snapshot_for_model(self, model_id: str) -> dict[str, float]:
        self.requested_models.append(model_id)
        return dict(self.overrides.get(model_id, {}))


class _DisabledProviderResolver:
    def __init__(self, *providers: str) -> None:
        self.providers = frozenset(providers)

    def is_disabled(self, provider: str) -> bool:
        return provider in self.providers


@pytest.mark.unit
def test_fixed_router_satisfies_route_table_view_with_frozen_tuple_snapshot():
    router = FixedRouter()
    adapter = _adapter("model", "model:primary")
    router.register_route("model", [(adapter, 1.0)], aliases=["model-alias"])

    snapshot = router.iter_effective_routes()

    assert isinstance(router, RouteTableView)
    assert isinstance(snapshot, tuple)
    assert len(snapshot) == 1
    assert isinstance(snapshot[0], EffectiveRoute)
    assert isinstance(snapshot[0].adapters, tuple)
    assert snapshot[0].adapters == ((adapter, 1.0),)
    assert router.canonical_id("model") == "model"
    assert router.canonical_id("model-alias") == "model"
    assert router.canonical_id("missing") == "missing"

    with pytest.raises(FrozenInstanceError):
        snapshot[0].canonical_model_id = "changed"  # type: ignore[misc]
    with pytest.raises(TypeError):
        snapshot[0].adapters[0] = (adapter, 0.0)  # type: ignore[index]


@pytest.mark.unit
def test_effective_route_snapshot_is_first_wins_and_preserves_route_and_adapter_order():
    router = FixedRouter()
    first_a = _adapter("canonical", "canonical:first-a")
    first_b = _adapter("canonical", "canonical:first-b")
    shadowed = _adapter("canonical", "canonical:shadowed")
    other = _adapter("other", "other:primary")
    first_route = RouteConfig(
        adapters=[(first_a, 0.75), (first_b, 0.25)],
        canonical_model_id="canonical",
    )
    shadowed_route = RouteConfig(
        adapters=[(shadowed, 1.0)],
        canonical_model_id="canonical",
    )
    other_route = RouteConfig(
        adapters=[(other, 1.0)],
        canonical_model_id="other",
    )

    # A route key that resolves to the canonical id may appear before the
    # canonical key itself. Match RouteWise's historical dict-order/first-wins
    # behavior instead of sorting or replacing it with the later entry.
    with router._lock:
        router.routes["canonical-alias-first"] = first_route
        router.routes["canonical"] = shadowed_route
        router.routes["other"] = other_route
        router.routes["canonical-alias-later"] = shadowed_route

    snapshot = router.iter_effective_routes()

    assert [(route.route_key, route.canonical_model_id) for route in snapshot] == [
        ("canonical-alias-first", "canonical"),
        ("other", "other"),
    ]
    assert snapshot[0].adapters == ((first_a, 0.75), (first_b, 0.25))
    assert snapshot[1].adapters == ((other, 1.0),)
    assert router.canonical_id("canonical-alias-first") == "canonical"
    assert router.canonical_id("canonical-alias-later") == "canonical"


@pytest.mark.unit
def test_old_effective_route_snapshot_is_independent_of_later_route_mutations():
    router = FixedRouter()
    original = _adapter("model", "model:original")
    replacement = _adapter("model", "model:replacement")
    added = _adapter("added", "added:primary")
    router.register_route("model", [(original, 1.0)])

    old_snapshot = router.iter_effective_routes()
    router.register_route("model", [(replacement, 1.0)])
    router.register_route("added", [(added, 1.0)])
    new_snapshot = router.iter_effective_routes()

    assert [(route.canonical_model_id, route.adapters) for route in old_snapshot] == [
        ("model", ((original, 1.0),))
    ]
    assert [(route.canonical_model_id, route.adapters) for route in new_snapshot] == [
        ("model", ((replacement, 1.0),)),
        ("added", ((added, 1.0),)),
    ]


@pytest.mark.unit
def test_effective_route_snapshot_applies_canonical_weight_overrides_then_disables_provider():
    resolver = _SnapshotWeightResolver(
        {"canonical": {"canonical:override": 7.0, "canonical:blocked": 11.0}}
    )
    disabled = _DisabledProviderResolver("blocked-provider")
    router = FixedRouter(
        weight_override_resolver=resolver,
        disabled_provider_resolver=disabled,
    )
    overridden = _adapter("canonical", "canonical:override", provider="active-provider")
    blocked = _adapter("canonical", "canonical:blocked", provider="blocked-provider")
    untouched = _adapter("canonical", "canonical:untouched", provider="active-provider")
    router.register_route(
        "canonical",
        [(overridden, 1.0), (blocked, 3.0), (untouched, 5.0)],
        aliases=["alias"],
    )

    snapshot = router.iter_effective_routes()

    # Runtime overrides replace raw weights without renormalizing. Provider
    # disablement is applied last and therefore wins over a positive override.
    assert snapshot[0].adapters == (
        (overridden, 7.0),
        (blocked, 0.0),
        (untouched, 5.0),
    )
    # The alias entry is deduplicated before effective weights are resolved,
    # and override lookup always uses the canonical id.
    assert resolver.requested_models == ["canonical"]


class _RecordingRLock:
    """Small context-manager wrapper exposing current acquisition depth."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.depth = 0

    def __enter__(self) -> _RecordingRLock:
        self._lock.acquire()
        self.depth += 1
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.depth -= 1
        self._lock.release()


@pytest.mark.unit
def test_effective_route_snapshot_is_built_under_lock_and_releases_it_before_return():
    router = FixedRouter()
    adapter = _adapter("model", "model:primary")
    router.register_route("model", [(adapter, 1.0)])
    recording_lock = _RecordingRLock()
    router._lock = recording_lock  # type: ignore[assignment]

    class _LockCheckingResolver:
        def get_snapshot_for_model(self, _model_id: str) -> dict[str, float]:
            assert recording_lock.depth == 1
            return {}

    router.weight_override_resolver = _LockCheckingResolver()

    snapshot = router.iter_effective_routes()

    assert snapshot[0].adapters == ((adapter, 1.0),)
    assert recording_lock.depth == 0

    acquired_after_return = threading.Event()

    def _acquire_from_another_thread() -> None:
        with recording_lock:
            acquired_after_return.set()

    thread = threading.Thread(target=_acquire_from_another_thread)
    thread.start()
    thread.join(timeout=1.0)
    assert not thread.is_alive()
    assert acquired_after_return.is_set()


@pytest.mark.unit
@pytest.mark.parametrize("new_hook", [True, False], ids=["route-table-hook", "legacy-hook"])
def test_model_router_registry_prefers_route_table_hook_with_legacy_fallback(
    monkeypatch: pytest.MonkeyPatch,
    new_hook: bool,
):
    import routing.model_router_registry as registry_module
    from routing.model_router_registry import ModelRouterRegistry

    calls: list[tuple[str, object]] = []

    class _LegacyStrategy:
        def attach_fixed_router(self, route_table: object) -> None:
            calls.append(("legacy", route_table))

    class _NewStrategy(_LegacyStrategy):
        def attach_route_table(self, route_table: object) -> None:
            calls.append(("new", route_table))

    strategy = _NewStrategy() if new_hook else _LegacyStrategy()
    monkeypatch.setattr(
        registry_module,
        "build_router",
        lambda _name, _params, *, dependencies=None: strategy,
    )
    shared_fixed = FixedRouter()
    registry = ModelRouterRegistry(
        models_config={"model": {"router": "routewise"}},
        shared_fixed_router=shared_fixed,
    )

    assert registry.get_router("model") is strategy
    assert calls == [("new" if new_hook else "legacy", shared_fixed)]
