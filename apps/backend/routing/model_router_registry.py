"""Per-model router registry with YAML-config-driven dispatch.

Reads each model's ``router`` and ``router_params`` from the parsed
``models.yaml`` config. Fixed routing reuses the process-scoped router passed
at construction; other strategies are constructed through
``routing.strategies.build_router``. Routers are cached per model_id, so the
first ``get_router(model_id)`` call pays the construction cost and subsequent
calls return the same instance.

When a model omits ``router:``, ``default_router_name`` (typically read
from ``routing.yaml``'s ``default_router`` field) is used.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from routing.protocols import RouteTableRefreshable
from routing.routers import ManagedRouter
from routing.strategies import build_router, validate_router_config
from serving.utils.logging import get_logger

if TYPE_CHECKING:
    from routing.dependencies import RouterBuildDependencies
    from routing.protocols import RouterProtocol
    from routing.routers import FixedRouter

logger = get_logger(__name__)


class StaleRouterStrategyChangeError(RuntimeError):
    """Raised when a prepared router change no longer matches registry state."""


@dataclass(frozen=True, slots=True)
class RouterStrategyChange:
    """Prepared router strategy transition that has not changed live state yet."""

    canonical_model_id: str
    strategy: str
    target_override: str | None
    previous_override: str | None
    previous_router: RouterProtocol
    router: RouterProtocol
    revision: int
    _registry_identity: object = field(repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class RouterRuntimeOverride:
    """Effective runtime override owned by one canonical model router."""

    canonical_model_id: str
    strategy: str
    configured_strategy: str


class ModelRouterRegistry:
    """Maps ``model_id -> RouterProtocol`` via per-model YAML configuration.

    Args:
        models_config: Mapping ``{model_id: per_model_dict}``.  Each
            ``per_model_dict`` may contain ``router`` (strategy name) and
            ``router_params`` (dict).  Models not in the mapping fall back
            to ``default_router_name`` with empty params.
        default_router_name: Strategy name used when a model omits
            ``router``.  Must be a registered strategy (e.g. ``"fixed"``).
        alias_to_model: Optional alias-to-canonical-model mapping used to
            collapse stateful routers onto one cached instance.
        dependencies: Optional process-scoped collaborators propagated to
            every router built by this registry.
        shared_fixed_router: Process-scoped FixedRouter populated with the
            live route table. Required before the first router lookup.
    """

    def __init__(
        self,
        models_config: dict[str, dict[str, Any]],
        default_router_name: str = "fixed",
        alias_to_model: dict[str, str] | None = None,
        dependencies: RouterBuildDependencies | None = None,
        *,
        shared_fixed_router: FixedRouter | None = None,
    ) -> None:
        self._configs = models_config
        self._default = default_router_name
        self._cache: dict[str, RouterProtocol] = {}
        self._alias_to_model = dict(alias_to_model or {})
        self._router_overrides: dict[str, str] = {}
        self._router_revisions: dict[str, int] = {}
        self._registry_identity = object()
        self._dependencies = dependencies
        self._shared_fixed: FixedRouter | None = None
        if shared_fixed_router is not None:
            self._register_shared_fixed_router(shared_fixed_router)

    def _register_shared_fixed_router(self, fixed_router: FixedRouter) -> None:
        """Register the process-scoped FixedRouter at the composition boundary."""
        if self._shared_fixed is fixed_router:
            return
        if self._shared_fixed is not None:
            raise ValueError("a different shared FixedRouter is already registered")
        if self._cache:
            raise RuntimeError("shared FixedRouter must be registered before router lookup")
        if (
            self._dependencies is not None
            and fixed_router.endpoint_health_registry is not self._dependencies.health_registry
        ):
            raise ValueError("shared FixedRouter must use RouterBuildDependencies.health_registry")
        self._shared_fixed = fixed_router

    def bind_fixed_router(self, fixed_router: FixedRouter) -> None:
        """Compatibility shim for registering the shared ``FixedRouter``.

        New composition roots should pass ``shared_fixed_router=`` to the
        constructor. This method remains for one release for external callers.
        """
        self._register_shared_fixed_router(fixed_router)

    def get_router(self, model_id: str) -> RouterProtocol:
        """Return (constructing on first call) the router for ``model_id``."""
        canonical_model_id = self.canonical_model_id(model_id)
        cached = self._cache.get(canonical_model_id)
        if cached is not None:
            self._cache[model_id] = cached
            return cached
        cfg = self._configs.get(canonical_model_id, self._configs.get(model_id, {}))
        name, params = self._router_spec(canonical_model_id, cfg)
        router = self._build_router_for_spec(
            canonical_model_id=canonical_model_id,
            requested_model_id=model_id,
            name=name,
            params=params,
        )
        self._cache[canonical_model_id] = router
        self._cache[model_id] = router
        return router

    def _build_router_for_spec(
        self,
        *,
        canonical_model_id: str,
        requested_model_id: str,
        name: str,
        params: dict[str, Any],
    ) -> RouterProtocol:
        """Build one validated router without mutating registry state."""
        shared_fixed = self._shared_fixed
        if shared_fixed is None:
            # Preserve config fail-fast ordering without constructing and
            # caching an empty FixedRouter or an unattached RouteWise router.
            validate_router_config(name, params, dependencies=self._dependencies)
            raise RuntimeError(
                "shared FixedRouter is not registered; pass "
                "shared_fixed_router=... when constructing ModelRouterRegistry"
            )
        # Fixed entries share the populated process router. Per-model params
        # (currently only informational local_fraction) are still validated,
        # but cannot mutate a shared instance without last-write-wins behavior.
        if name == "fixed":
            validate_router_config(
                name,
                params,
                dependencies=self._dependencies,
            )
            router: RouterProtocol = shared_fixed
        else:
            router = build_router(name, params, dependencies=self._dependencies)
            # Late-bind the read-only route-table port for RouteWise (and any
            # future late-bound strategy that exposes attach_route_table).
            attach = getattr(router, "attach_route_table", None)
            if attach is None:
                # One-release compatibility for external strategies that still
                # expose the former binding hook.
                attach = getattr(router, "attach_fixed_router", None)
            if attach is not None:
                attach(shared_fixed)
        logger.info(
            "router_initialized",
            extra={
                "event": "router_initialized",
                "model": canonical_model_id,
                "requested_model": requested_model_id,
                "strategy": name,
                "param_keys": sorted(params.keys()),
            },
        )
        return router

    def get_router_name(self, model_id: str) -> str:
        """Return the active strategy name for ``model_id``."""
        canonical_model_id = self.canonical_model_id(model_id)
        cfg = self._configs.get(canonical_model_id, self._configs.get(model_id, {}))
        name, _params = self._router_spec(canonical_model_id, cfg)
        return name

    def get_configured_router_name(self, model_id: str) -> str:
        """Return the YAML/default strategy name, ignoring runtime overrides."""
        canonical_model_id = self.canonical_model_id(model_id)
        cfg = self._configs.get(canonical_model_id, self._configs.get(model_id, {}))
        return str(cfg.get("router") or self._default)

    def _router_spec(
        self, canonical_model_id: str, cfg: dict[str, Any]
    ) -> tuple[str, dict[str, Any]]:
        configured_name = str(cfg.get("router") or self._default)
        override_name = self._router_overrides.get(canonical_model_id)
        name = override_name or configured_name
        # Router params belong to the configured router. When an admin
        # temporarily switches strategies, do not pass fixed-only params into
        # RouteWise or vice versa.
        params = (cfg.get("router_params") or {}) if name == configured_name else {}
        return str(name), dict(params)

    def validate_router_strategy(self, model_id: str, strategy: str) -> None:
        """Validate ``strategy`` for ``model_id`` without constructing it."""
        canonical_model_id = self.canonical_model_id(model_id)
        cfg = self._configs.get(canonical_model_id, self._configs.get(model_id, {}))
        configured_name = str(cfg.get("router") or self._default)
        params = (cfg.get("router_params") or {}) if strategy == configured_name else {}
        validate_router_config(strategy, params, dependencies=self._dependencies)

    def set_router_override(self, model_id: str, strategy: str) -> None:
        """Compatibility wrapper that prepares and commits one strategy change."""
        change = self.prepare_router_strategy_change(model_id, strategy)
        self.commit_router_strategy_change(change)

    def clear_router_override(self, model_id: str) -> None:
        """Remove an override and cached router without constructing a fallback.

        This compatibility operation is also used while deleting runtime models,
        where constructing and caching the configured fallback would resurrect a
        route that no longer exists. Live transitions back to configuration should
        use ``prepare_router_strategy_change(model_id, None)`` and commit it.
        """
        canonical_model_id = self.canonical_model_id(model_id)
        self._router_overrides.pop(canonical_model_id, None)
        self._invalidate_router_cache(canonical_model_id)
        self._bump_router_revision(canonical_model_id)

    def prepare_router_strategy_change(
        self,
        model_id: str,
        strategy: str | None,
    ) -> RouterStrategyChange:
        """Build a candidate router without changing the active registry state.

        Passing ``None`` or the configured strategy removes the runtime override.
        The returned frozen token can be committed only while the canonical
        model's override revision and previous override still match.
        """
        canonical_model_id = self.canonical_model_id(model_id)
        cfg = self._configs.get(canonical_model_id, self._configs.get(model_id, {}))
        configured_strategy = str(cfg.get("router") or self._default)
        target_override = None if strategy is None or strategy == configured_strategy else strategy
        effective_strategy = configured_strategy if target_override is None else target_override
        previous_override = self._router_overrides.get(canonical_model_id)
        previous_router = self.get_router(canonical_model_id)
        previous_strategy = self.get_router_name(canonical_model_id)

        if effective_strategy == previous_strategy:
            router = previous_router
        else:
            params = (
                (cfg.get("router_params") or {})
                if effective_strategy == configured_strategy
                else {}
            )
            router = self._build_router_for_spec(
                canonical_model_id=canonical_model_id,
                requested_model_id=model_id,
                name=effective_strategy,
                params=dict(params),
            )

        return RouterStrategyChange(
            canonical_model_id=canonical_model_id,
            strategy=effective_strategy,
            target_override=target_override,
            previous_override=previous_override,
            previous_router=previous_router,
            router=router,
            revision=self._router_revisions.get(canonical_model_id, 0),
            _registry_identity=self._registry_identity,
        )

    def commit_router_strategy_change(
        self,
        change: RouterStrategyChange,
    ) -> RouterProtocol:
        """Atomically publish a prepared router change or reject a stale token."""
        if change._registry_identity is not self._registry_identity:
            raise ValueError("router strategy change belongs to a different registry")

        canonical_model_id = change.canonical_model_id
        current_revision = self._router_revisions.get(canonical_model_id, 0)
        current_override = self._router_overrides.get(canonical_model_id)
        if current_revision != change.revision or current_override != change.previous_override:
            raise StaleRouterStrategyChangeError(
                f"router strategy change is stale for model {canonical_model_id!r}"
            )

        if change.target_override is None:
            self._router_overrides.pop(canonical_model_id, None)
        else:
            self._router_overrides[canonical_model_id] = change.target_override
        self._invalidate_router_cache(canonical_model_id)
        self._cache[canonical_model_id] = change.router
        self._bump_router_revision(canonical_model_id)
        return change.router

    def runtime_override_for_router(
        self,
        router: RouterProtocol,
    ) -> RouterRuntimeOverride | None:
        """Return the one effective runtime override currently owning ``router``."""
        matches: list[RouterRuntimeOverride] = []
        for canonical_model_id, strategy in self._router_overrides.items():
            cfg = self._configs.get(canonical_model_id, {})
            configured_strategy = str(cfg.get("router") or self._default)
            if strategy == configured_strategy:
                continue
            if self._cache.get(canonical_model_id) is not router:
                continue
            matches.append(
                RouterRuntimeOverride(
                    canonical_model_id=canonical_model_id,
                    strategy=strategy,
                    configured_strategy=configured_strategy,
                )
            )

        if len(matches) > 1:
            raise RuntimeError("router owns multiple effective runtime overrides")
        return matches[0] if matches else None

    def _invalidate_router_cache(self, canonical_model_id: str) -> None:
        """Evict the canonical router and every alias that resolves to it."""
        self._cache.pop(canonical_model_id, None)
        for alias, target in self._alias_to_model.items():
            if target == canonical_model_id:
                self._cache.pop(alias, None)

    def _bump_router_revision(self, canonical_model_id: str) -> None:
        """Fence any router strategy changes prepared against older state."""
        self._router_revisions[canonical_model_id] = (
            self._router_revisions.get(canonical_model_id, 0) + 1
        )

    def get_router_override(self, model_id: str) -> str | None:
        """Return the runtime router override, if present."""
        canonical_model_id = self.canonical_model_id(model_id)
        return self._router_overrides.get(canonical_model_id)

    def canonical_model_id(self, model_id: str) -> str:
        """Return the canonical id used for router and setting ownership."""
        canonical_model_id = self._alias_to_model.get(model_id, model_id)
        shared_fixed = self._shared_fixed
        if shared_fixed is not None:
            canonical_model_id = shared_fixed.canonical_id(canonical_model_id)
        return canonical_model_id

    def has_model(self, model_id: str) -> bool:
        """Return whether ``model_id`` is known without constructing a router."""
        canonical_model_id = self.canonical_model_id(model_id)
        if canonical_model_id in self._configs:
            return True
        if canonical_model_id in self._router_overrides:
            return True
        shared_fixed = self._shared_fixed
        if shared_fixed is None:
            return False
        # Include a staged-but-unpublished runtime model while its strategy
        # transition is being prepared. A cache entry is deliberately not
        # existence evidence: get_router() remains backward-compatible and can
        # construct a fallback router for an arbitrary id.
        route_snapshot = shared_fixed.snapshot_for_transition(canonical_model_id)
        return any(
            route.canonical_model_id == canonical_model_id
            for route in route_snapshot.iter_effective_routes()
        )

    def get_configured_routewise_params(self, model_id: str) -> dict[str, Any]:
        """Return canonical YAML RouteWise params without building a router."""
        canonical_model_id = self.canonical_model_id(model_id)
        if not self.has_model(canonical_model_id):
            raise KeyError(f"unknown model: {model_id}")
        cfg = self._configs.get(canonical_model_id, self._configs.get(model_id, {}))
        configured_strategy = str(cfg.get("router") or self._default)
        if configured_strategy != "routewise":
            return {}
        validated = validate_router_config(
            "routewise",
            dict(cfg.get("router_params") or {}),
            dependencies=self._dependencies,
        )
        return dict(validated.model_dump(exclude_unset=True))

    def get_cached_router(self, model_id: str) -> RouterProtocol | None:
        """Return a live cached router without constructing a missing instance."""
        canonical_model_id = self.canonical_model_id(model_id)
        return self._cache.get(canonical_model_id)

    def registered_models(self) -> dict[str, str]:
        """Return ``{model_id: router_class_name}`` for every cached entry."""
        return {mid: type(r).__name__ for mid, r in self._cache.items()}

    def cached_routers(self) -> list[RouterProtocol]:
        """Return cached router instances."""
        return list(self._cache.values())

    def refresh_route_tables(self) -> None:
        """Refresh each unique cached router that exposes the capability."""
        seen_ids: set[int] = set()
        for router in tuple(self._cache.values()):
            if id(router) in seen_ids:
                continue
            seen_ids.add(id(router))
            if isinstance(router, RouteTableRefreshable):
                router.refresh_route_table()
                continue

            # One-release compatibility for external strategies that still
            # expose the former private hook. Keep this adapter inside the
            # routing composition boundary so serving never owns router locks.
            refresh = getattr(router, "_rebuild_from_route_table", None)
            if not callable(refresh):
                refresh = getattr(router, "_rebuild_from_fixed_router", None)
            if not callable(refresh):
                continue
            commit_lock = getattr(router, "_route_commit_lock", None)
            if commit_lock is None:
                refresh()
            else:
                with commit_lock:
                    refresh()

    def configured_model_ids(self) -> list[str]:
        """Return model ids known to the registry config."""
        return list(self._configs.keys())

    def managed_routers(self) -> list[ManagedRouter]:
        """Return unique cached routers with async lifecycle hooks."""
        seen_ids: set[int] = set()
        managed: list[ManagedRouter] = []
        for router in self._cache.values():
            if id(router) in seen_ids:
                continue
            if isinstance(router, ManagedRouter):
                seen_ids.add(id(router))
                managed.append(router)
        return managed
