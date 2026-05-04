# Routing Config Expressiveness — Design

**Date:** 2026-05-03
**Status:** Draft → ready for plan
**Author:** Architecture review follow-up (issue #6 of 6)

## Problem

The routing layer's strategy assignment is not expressible via config alone:

- [config/routing.yaml](../../../config/routing.yaml) supports `routing_strategy: "fixed"` and one parameter (`local_fraction`); no other strategy is selectable.
- RouteWise enablement requires editing the hardcoded canary list in [apps/backend/routing/model_router_registry.py](../../../apps/backend/routing/model_router_registry.py) — a code change + redeploy.
- [config/models.yaml](../../../config/models.yaml) carries some per-adapter routing hints (`subscription_type`) but the routing strategy itself isn't expressible per-model.

Operators wanting to A/B-test routing strategies, or add a model that should use RouteWise without a code deploy, can't.

## Goals

1. Make per-model strategy choice and per-model strategy parameters expressible in `models.yaml` — no code change required for routine operator changes.
2. Replace the hardcoded canary list in `model_router_registry.py` with config-driven dispatch.
3. Introduce a small strategy registry so adding a new routing strategy is a one-file change (no edits to the registry/dispatch core).
4. Keep all existing routing decisions working — backward-compatible migration.
5. Land in **one PR**.

## Non-goals

- Build a plug-in / entry-point system for third-party strategies. The registry uses simple decorator-based registration; strategies live in this repo.
- Health monitor for remote endpoints (currently only local endpoints are pinged). Separate concern, separate brainstorm if it ever bites.
- Strategy hot-reload. Today: `models.yaml` requires app restart; that stays.
- Per-route (not per-model) strategy selection. If a single model's adapters need different strategies for different routes, that's a future need.
- Deprecate / remove old `routing_strategy` / `routing_parameter` aliases. They land here as one-release shims; removal is a follow-up.

## Architecture

### `models.yaml` schema additions

Each model entry gets two optional new fields:

```yaml
models:
  gpt-4:
    routes: [...]                   # existing — unchanged
    # No router specified → default_router from routing.yaml ("fixed" by default)

  claude-3-opus:
    router: routewise               # NEW — strategy choice
    router_params:                  # NEW — strategy params, optional
      hedge_threshold_ms: 200
      quota_tier: premium
    routes: [...]

  claude-3-haiku:
    router: routewise               # opted into routewise
    # No router_params → strategy's default Pydantic values apply
    routes: [...]
```

**Validation:** Pydantic `extra="forbid"` per-strategy params. Bad `router_params` → boot fails with a clear error naming model + field.

### Code structure

```
apps/backend/routing/
  strategies/
    __init__.py                     # NEW — registry: register_strategy(name)((Router, ParamsModel))
                                    #       and build_router(name, params_dict) -> BaseRouter
    fixed.py                        # NEW — FixedParams Pydantic + register_strategy("fixed")(...)
    routewise.py                    # NEW — RouteWiseParams Pydantic + register_strategy("routewise")(...)
                                    #       (re-exports RouteWiseRouter from apps/backend/routing/routewise/)
  model_router_registry.py          # MODIFIED — uses build_router(...) instead of canary list
```

### Strategy registry

```python
# apps/backend/routing/strategies/__init__.py
from typing import Any
from pydantic import BaseModel
from routing.routers import BaseRouter

_STRATEGIES: dict[str, tuple[type[BaseRouter], type[BaseModel]]] = {}


def register_strategy(name: str):
    """Decorator for registering a (Router class, Params model) tuple."""
    def deco(item: tuple[type[BaseRouter], type[BaseModel]]):
        cls, params_cls = item
        _STRATEGIES[name] = (cls, params_cls)
        return item
    return deco


def build_router(name: str, params: dict[str, Any] | None) -> BaseRouter:
    """Construct a router by strategy name + raw params dict from YAML."""
    if name not in _STRATEGIES:
        raise ValueError(
            f"unknown router strategy {name!r}; known: {sorted(_STRATEGIES)}"
        )
    router_cls, params_cls = _STRATEGIES[name]
    validated = params_cls.model_validate(params or {})
    return router_cls(params=validated)


# Trigger registration of built-in strategies via import side effects.
from routing.strategies import fixed, routewise  # noqa: F401, E402
```

```python
# apps/backend/routing/strategies/fixed.py
from pydantic import BaseModel, Field
from routing.routers import FixedRouter
from routing.strategies import register_strategy


class FixedParams(BaseModel, extra="forbid"):
    local_fraction: float = Field(default=0.5, ge=0.0, le=1.0)


register_strategy("fixed")((FixedRouter, FixedParams))
```

```python
# apps/backend/routing/strategies/routewise.py
from pydantic import BaseModel
from routing.routewise.router import RouteWiseRouter
from routing.strategies import register_strategy


class RouteWiseParams(BaseModel, extra="forbid"):
    # Mirror current RouteWiseConfig dataclass field-for-field.
    hedge_threshold_ms: int = 200
    quota_tier: str = "default"
    # ... full set of params copied from apps/backend/routing/routewise/config.py
    # All have sensible defaults; only fields explicitly overridden in
    # router_params: ... in models.yaml take effect.


register_strategy("routewise")((RouteWiseRouter, RouteWiseParams))
```

`FixedRouter` and `RouteWiseRouter` constructors gain a `params: PydanticModel` keyword arg (forwarded from `build_router`). They translate it into whatever internal config they need (a small adapter inside each `__init__` keeps the rest of the routing code untouched).

### `ModelRouterRegistry` rewrite

```python
class ModelRouterRegistry:
    def __init__(self, models_config, default_router_name: str = "fixed") -> None:
        self._configs = models_config              # parsed models.yaml
        self._default = default_router_name
        self._cache: dict[str, BaseRouter] = {}

    def get_router(self, model: str) -> BaseRouter:
        if model in self._cache:
            return self._cache[model]
        cfg = self._configs.get(model, {})
        name = cfg.get("router", self._default)
        params = cfg.get("router_params", {})
        log.info(
            "router_initialized",
            extra={
                "event": "router_initialized",
                "model": model,
                "strategy": name,
                "param_keys": sorted(params.keys()) if params else [],
            },
        )
        router = build_router(name, params)
        self._cache[model] = router
        return router
```

The hardcoded canary list and its dispatch logic are deleted.

### `routing.yaml` simplified

```yaml
default_router: fixed             # NEW — fallback when models.yaml doesn't specify a router
health_check:
  interval_sec: 30
  timeout_sec: 5

# DEPRECATED (Pydantic alias for one release):
# routing_strategy: fixed         # → migrated to default_router
# routing_parameter:               # → migrated into models.yaml router_params per model
#   local_fraction: 0.5
```

`apps/backend/routing/config.py` Pydantic model:

```python
class RoutingConfig(BaseModel):
    default_router: str = "fixed"
    health_check: HealthCheckConfig = Field(default_factory=HealthCheckConfig)

    # Deprecated aliases — keep for one release.
    routing_strategy: str | None = Field(default=None, deprecated=True)
    routing_parameter: dict[str, Any] | None = Field(default=None, deprecated=True)

    @model_validator(mode="after")
    def _migrate_legacy_fields(self) -> "RoutingConfig":
        if self.routing_strategy and self.default_router == "fixed":
            object.__setattr__(self, "default_router", self.routing_strategy)
        if self.routing_strategy or self.routing_parameter:
            log.warning(
                "routing.yaml uses deprecated 'routing_strategy'/'routing_parameter' fields; "
                "migrate to 'default_router' + per-model 'router'/'router_params' in models.yaml"
            )
        return self
```

### Migration of existing canary list

Identify every model currently routed through RouteWise via the hardcoded canary list. For each, add `router: routewise` to its entry in `models.yaml`. Mechanical translation; the diff makes parity obvious to the reviewer.

If any of those models also need RouteWise-specific params today (subscription_type, etc.), those move from being adapter-config implicit defaults into explicit `router_params` per model.

### What stays the same

- [apps/backend/routing/routers.py](../../../apps/backend/routing/routers.py) — `BaseRouter`, `FixedRouter`, `_CircuitBreaker`, EWMA health tracking — untouched (other than constructor accepting a `params` kwarg).
- [apps/backend/routing/routewise/](../../../apps/backend/routing/routewise/) — internal RouteWise classes — untouched.
- Adapters, request lifecycle, observability — untouched.
- `BaseRouter` interface (`select(...)`, `stream_chat_completion(...)`) — unchanged; downstream callers don't notice.

## Testing

| Layer | What | New |
|---|---|---|
| Unit — registry | `register_strategy` adds entries; `build_router("fixed", {"local_fraction": 0.5})` returns `FixedRouter`; `build_router("unknown", ...)` raises with helpful message. | `tests/unit/apps/backend/routing/test_strategies.py` |
| Unit — params validation | `FixedParams.model_validate({"local_fraction": 1.5})` rejects (range); `RouteWiseParams` rejects unknown fields; defaults apply when params omitted. | same file |
| Unit — `ModelRouterRegistry` dispatch | Stub `models.yaml` with mixed `router: fixed` / `router: routewise` / unspecified; assert `get_router(model)` returns the right type per case; assert `default_router` from `routing.yaml` is honored when a model omits the field; assert per-model cache hits. | `tests/unit/apps/backend/routing/test_model_router_registry.py` (extend) |
| Unit — `RoutingConfig` migration | Legacy `routing_strategy: fixed` migrates to `default_router`; deprecation warning logged. | `tests/unit/apps/backend/routing/test_config.py` (extend) |
| Integration | One new fixture-driven test that loads a real `models.yaml` snippet with both strategies and routes a request through each; existing routing tests continue to pass. | extend existing `tests/integration/apps/backend/routing/...` |
| Manual smoke (post-deploy staging) | Verify `router_initialized` log events appear for the expected models with the expected strategies. | manual |

## Risk + rollback

| Risk | Mitigation |
|---|---|
| Canary-list migration to YAML misses a model | PR description compares old canary list ↔ new `router: routewise` entries side-by-side; reviewer confirms parity. |
| Bad `router_params` in production crashes boot | Pydantic validation fails fast with model name + field. Better than silent fallback. |
| Adapter config mismatch (model says `routewise` but adapters lack subscription_type) | RouteWise validates at startup; failure surfaces with adapter name. Per-model YAML opt-in just exposes this earlier. |
| Existing canary observability lost (alerts referencing canary code paths) | Emit `router_initialized` log event on each cache miss; existing alert rules can read these to confirm migration. |
| Operator's local checkout has old `routing_strategy:` setting | One-release Pydantic alias migrates with deprecation warning; no immediate breakage. |
| Strategy registry import order — circular import between `apps/backend/routing/strategies/` and `apps/backend/routing/routers.py` / `apps/backend/routing/routewise/` | `strategies/__init__.py` imports `fixed` and `routewise` at module bottom; the strategy modules import from `apps/backend/routing/routers.py` and `apps/backend/routing/routewise/router.py` at top. One-way dependency: strategies → routers, never reverse. |

**Rollback:** single `git revert`. Migration is YAML schema additions + a code rewrite of `model_router_registry.py`. Reverting restores the canary list and old YAML works because the alias keeps both old and new shapes acceptable for one release.

## Performance

- `ModelRouterRegistry.get_router` already caches per-model. Lookup changes from "check canary list (O(1), tiny)" to "dict lookup in models.yaml config (O(1))". No measurable difference.
- Strategy registry: one dict lookup at cache miss; negligible.
- Routing decision hot path: unchanged — the chosen router instance handles the request the same way.

## Open questions (resolved)

| Question | Resolution |
|---|---|
| What if `models.yaml` references an unknown strategy? | Boot fails with `ValueError("unknown router strategy 'foo'; known: ['fixed', 'routewise']")`. |
| How are RouteWise's many params represented in `RouteWiseParams`? | Mirror current `RouteWiseConfig` dataclass field-for-field as Pydantic with `extra="forbid"`. |
| Can a future PR add a third strategy without modifying the registry? | Yes — new `apps/backend/routing/strategies/cost_weighted.py` calls `register_strategy(...)`; add to `__init__.py` imports. |
| Health-monitor remote-endpoint blind spot — fix here? | No, separate brainstorm. |
| Rollout sequence | Single deploy; operator reviews migration diff vs. canary list pre-merge; watch `router_initialized` events for 24h post-deploy. |

## Out-of-scope follow-ups

- Health monitor for remote endpoints.
- Strategy hot-reload (today: `models.yaml` requires restart).
- Per-route strategy selection (if needed in the future).
- Removal of legacy `routing_strategy` / `routing_parameter` aliases (one release later).
- Decompose [completions.py](../../../apps/backend/serving/servers/routers/completions.py) (issue #2 — spec exists).
- Schema migrations (issue #3 — spec exists).
- Tracked fire-and-forget tasks (issue #4 — spec exists).
- Decompose admin page (issue #5 — spec exists).
