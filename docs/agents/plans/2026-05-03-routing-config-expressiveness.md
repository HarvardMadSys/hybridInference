# Routing Config Expressiveness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make per-model routing strategy and per-strategy parameters expressible in `models.yaml` via a small in-repo strategy registry, so adding a routing strategy or opting a model in is a YAML/one-file change rather than a registry/bootstrap edit.

**Architecture:** A `apps/backend/routing/strategies/` package owns a tiny registry (`register_strategy(name)((Router, ParamsModel))` + `build_router(name, params)`). Each strategy module (`fixed.py`, `routewise.py`) declares a Pydantic params model with `extra="forbid"` and registers a `(Router, Params)` pair. `ModelRouterRegistry` is rewritten to read `router` / `router_params` per model from `models.yaml` and dispatch via `build_router`. `RoutingConfig` gains `default_router: str` (with deprecated `routing_strategy` / `routing_parameter` shims for one release).

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, asyncpg, pytest + pytest-asyncio (auto mode), uv, ruff.

**Spec:** [docs/agents/specs/2026-05-03-routing-config-expressiveness-design.md](../specs/2026-05-03-routing-config-expressiveness-design.md)

**Process notes (from CLAUDE.md):**
- Pull origin/dev before starting.
- Single PR on feature branch `jason/claude/routing-config-expressiveness`.
- Worktree: `/home/juncheng/hybridInference-worktrees/routing-config-expressiveness`.
- Per CLAUDE.md: create issue → branch → implement → `make format` (ruff) → PR → monitor CI every 2 min → cleanup after merge.

---

## File Structure

**New files:**
- `apps/backend/routing/strategies/__init__.py` — registry: `_STRATEGIES`, `register_strategy(name)`, `build_router(name, params)`. Triggers `fixed`/`routewise` registration via bottom-of-file imports.
- `apps/backend/routing/strategies/fixed.py` — `FixedParams` Pydantic + `register_strategy("fixed")((FixedRouter, FixedParams))`.
- `apps/backend/routing/strategies/routewise.py` — `RouteWiseParams` Pydantic mirroring `RouteWiseConfig`, `register_strategy("routewise")((RouteWiseRouter, RouteWiseParams))`.
- `tests/unit/apps/backend/routing/test_strategies.py` — registry behavior + per-strategy params validation.
- `tests/integration/apps/backend/routing/__init__.py` — empty package marker.
- `tests/integration/apps/backend/routing/test_yaml_driven_dispatch.py` — fixture-driven integration test.

**Modified files:**
- `apps/backend/routing/routers.py` — `BaseRouter.__init__` and `FixedRouter.__init__` accept optional `params: BaseModel | None = None` kwarg (default keeps existing call sites working).
- `apps/backend/routing/routewise/router.py` — `RouteWiseRouter.__init__` accepts optional `params: BaseModel | None = None`; when supplied, translates to `RouteWiseConfig` and overrides `config`.
- `apps/backend/routing/model_router_registry.py` — full rewrite around `models_config` + `default_router_name` + `build_router` cache.
- `apps/backend/routing/config.py` — `RoutingConfig` gains `default_router: str = "fixed"`; legacy `routing_strategy` / `routing_parameter` become deprecated aliases migrated by a `model_validator`.
- `apps/backend/serving/servers/registry.py` — `ModelRegistrationInfo` gains `router` / `router_params` fields populated from `models.yaml`; mirrors what `bootstrap.py` will hand to `ModelRouterRegistry`.
- `apps/backend/serving/servers/bootstrap.py` — wires `ModelRouterRegistry(models_config, default_router_name=routing_cfg.default_router)` instead of the existing `ModelRouterRegistry(default_router=router)` + manual `register(...)` calls.
- `config/routing.yaml` — replace `routing_strategy: fixed` + `routing_parameter:` block with `default_router: fixed`.
- `config/models.yaml` — opt-in models declared via `router: routewise` (and optional `router_params: {...}`).
- `tests/unit/apps/backend/routing/test_model_router_registry.py` — extended to cover the rewritten registry.
- `tests/unit/apps/backend/routing/test_config.py` — created (or extended if it ever appears) for legacy-field migration tests.
- `tests/fixtures/test_routing.yaml` and `tests/servers/conftest.py` — updated to the new field name (`default_router`) so the deprecation warning isn't noise in unrelated tests.

---

## Pre-Investigation (Task 0)

### Task 0: Read existing code and confirm field set

**Goal:** Verify current shapes before touching code.

**Files to read (no edits):**
- `apps/backend/routing/model_router_registry.py` — confirm: today's class signature is `__init__(default_router: BaseRouter)` with `register(model_id, router)` + `configure_canary(...)`. No hardcoded canary list — per-model RouteWise opt-in is already YAML-driven via `routing_strategy:` on each model entry, but only via the bootstrap loop in `apps/backend/serving/servers/bootstrap.py:270-300`, not via the registry itself.
- `apps/backend/routing/routers.py` — confirm `BaseRouter.__init__(self, experiment_mode: bool = False)` and `FixedRouter.__init__(self)`.
- `apps/backend/routing/routewise/router.py` lines 91-100 — confirm `RouteWiseRouter.__init__(self, fixed_router, config: RouteWiseConfig, experiment_mode: bool = False)`.
- `apps/backend/routing/routewise/config.py` — enumerate **every** `RouteWiseConfig` dataclass field. As of the snapshot in this plan, the field set is:

  ```
  predictor: str = "ema"
  risk_quantile: float = 0.10
  daily_quota: int = 5000
  quota_monthly_fee: float = 20.0
  reset_timezone: str = "UTC"
  concurrency_enabled: bool = False
  concurrency_limit: int = 8
  concurrency_monthly_fee: float = 25.0
  shadow_price_L_seed: float = 0.001
  shadow_price_U_seed: float = 0.500
  shadow_price_adaptive: bool = True
  shadow_price_window_hours: int = 24
  shadow_price_min_ratio: int = 10
  latency_slo_sec: float = 3.0
  latency_target_cdf: float = 0.99
  latency_error_penalty: float = 0.0
  latency_window_sec: float = 900.0
  latency_min_samples: int = 10
  latency_lp_interval_sec: float = 60.0
  latency_swrr_alpha: float = 0.3
  latency_relaxation_factors: str = "1.2,1.5,2.0"
  latency_hedge_mode: str = "shadow"
  latency_hedge_cost_ratio: float = 0.1
  latency_hedge_dispatch_overhead_sec: float = 0.05
  canary_enabled: bool = False
  canary_enabled_models: list[str] | None = None
  canary_traffic_fraction: float = 1.0
  ```

  Before writing `RouteWiseParams` in Task 4, **re-open `apps/backend/routing/routewise/config.py` and diff** — if a field has been added/renamed since this snapshot, mirror the live set, not this snapshot.
- `apps/backend/routing/config.py` — confirm current `RoutingConfig` shape: `routing_strategy: str = "fixed"`, `routing_parameter: RoutingParameter`, plus `timeout`, `health_check`, `logging`, `local_deployment`, `remote_deployment`. **All non-routing-strategy fields stay untouched.**
- `config/models.yaml` — note: as of this snapshot **no model declares `routing_strategy:`** (RouteWise is gated only by the `ENABLE_ROUTEWISE` env var). The "canary list" referenced in the spec is therefore the empty set today; the migration in Task 14 may end up adding zero entries. Confirm by `grep -n "routing_strategy\|router:" config/models.yaml`. Record the actual list (model_ids and any non-default RouteWise params) in a note for Task 14.
- `apps/backend/serving/servers/bootstrap.py:270-300` — confirm the existing wiring loop to delete in Task 12.
- `apps/backend/serving/servers/registry.py:35-42` and `apps/backend/serving/servers/registry.py:406-413` — confirm `ModelRegistrationInfo` shape and where `routing_strategy` is harvested.

**No code change in this task.** Output is the current `RouteWiseConfig` field list and the current `routing_strategy:` model_id list (likely empty), recorded in the engineer's working notes for Tasks 4 and 14.

---

## Task 1: Worktree + issue setup

**Files:** none (project hygiene only).

- [ ] **Step 1: Pull origin/dev**

```bash
cd /home/juncheng/hybridInference
git fetch origin
git checkout dev
git pull --ff-only origin dev
```

Expected: `Already up to date.` or fast-forward.

- [ ] **Step 2: Create the worktree**

```bash
git worktree add -b jason/claude/routing-config-expressiveness \
  /home/juncheng/hybridInference-worktrees/routing-config-expressiveness origin/dev
```

Expected: `Preparing worktree (new branch 'jason/claude/routing-config-expressiveness')`.

- [ ] **Step 3: Create the tracking issue**

```bash
gh issue create \
  --title "Routing config expressiveness: per-model router + params in models.yaml" \
  --body "$(cat <<'EOF'
Make per-model routing strategy and params expressible in models.yaml via a small strategy registry.

Spec: docs/agents/specs/2026-05-03-routing-config-expressiveness-design.md
Plan: docs/agents/plans/2026-05-03-routing-config-expressiveness.md

Scope (single PR):
- New `apps/backend/routing/strategies/` package (registry + fixed + routewise modules).
- `ModelRouterRegistry` rewrite around `build_router(name, params)`.
- `RoutingConfig.default_router` with deprecated aliases for `routing_strategy` / `routing_parameter`.
- `models.yaml` per-model `router` + `router_params` fields wired through `apps/backend/serving/servers/registry.py` and `bootstrap.py`.
- Backward compat: existing `routing.yaml` keeps working with a deprecation warning.

EOF
)"
```

Expected: prints issue URL. Record the issue number for the PR description in Task 16.

- [ ] **Step 4: Verify worktree**

```bash
ls /home/juncheng/hybridInference-worktrees/routing-config-expressiveness
git -C /home/juncheng/hybridInference-worktrees/routing-config-expressiveness branch --show-current
```

Expected: directory listed, branch `jason/claude/routing-config-expressiveness`.

All subsequent tasks use the worktree as cwd.

- [ ] **Step 5: Commit (none — no code changes yet).**

---

## Task 2: Create the strategy registry skeleton

**Files:**
- Create: `apps/backend/routing/strategies/__init__.py`

- [ ] **Step 1: Write the failing test**

File: `tests/unit/apps/backend/routing/test_strategies.py`

```python
"""Unit tests for the routing strategy registry."""

from __future__ import annotations

import pytest


@pytest.mark.unit
def test_register_strategy_adds_entry():
    from pydantic import BaseModel

    from routing.strategies import _STRATEGIES, build_router, register_strategy

    class _Params(BaseModel, extra="forbid"):
        pass

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
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
cd /home/juncheng/hybridInference-worktrees/routing-config-expressiveness
uv run pytest tests/unit/apps/backend/routing/test_strategies.py -v
```

Expected: `ModuleNotFoundError: No module named 'routing.strategies'`.

- [ ] **Step 3: Create the registry module**

File: `apps/backend/routing/strategies/__init__.py`

```python
"""Strategy registry for per-model router dispatch.

Each routing strategy lives in a sibling module (``fixed.py``, ``routewise.py``,
...) and self-registers a ``(Router class, Params Pydantic model)`` pair via
``register_strategy(name)((Router, Params))`` at import time.

``build_router(name, params_dict)`` is the single dispatch point used by
``ModelRouterRegistry`` to translate a YAML ``router: <name>`` declaration
into a concrete ``BaseRouter`` instance.

Import-order contract:
    Strategy submodules import from ``routing.routers`` /
    ``routing.routewise.router`` at module top.  This module imports the
    submodules at the *bottom* of the file to trigger registration without
    creating a cycle.  Direction is one-way: ``strategies -> routers``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pydantic import BaseModel

    from routing.routers import BaseRouter


_STRATEGIES: dict[str, tuple[type, type]] = {}


def register_strategy(name: str):
    """Decorator-style registrar.

    Usage::

        register_strategy("fixed")((FixedRouter, FixedParams))

    The double-call shape (``register_strategy(name)(item)``) keeps the call
    site declarative and matches the spec.
    """

    def deco(item: tuple[type, type]) -> tuple[type, type]:
        cls, params_cls = item
        _STRATEGIES[name] = (cls, params_cls)
        return item

    return deco


def build_router(name: str, params: dict[str, Any] | None) -> "BaseRouter":
    """Construct a router by strategy name + raw params dict from YAML.

    Args:
        name: Strategy name (must be registered).
        params: Raw params dict from ``models.yaml`` (``None`` and ``{}``
            both mean "use strategy defaults").

    Raises:
        ValueError: If ``name`` is not registered.  Error message lists all
            known strategies to help operators spot typos.
        pydantic.ValidationError: If ``params`` fails the strategy's Pydantic
            schema (``extra="forbid"`` on every Params model).
    """
    if name not in _STRATEGIES:
        raise ValueError(
            f"unknown router strategy {name!r}; known: {sorted(_STRATEGIES)}"
        )
    router_cls, params_cls = _STRATEGIES[name]
    validated = params_cls.model_validate(params or {})
    return router_cls(params=validated)


# Trigger registration of built-in strategies via import side effects.
# Imports are at the bottom to avoid circular imports: the strategy modules
# import from routing.routers / routing.routewise at their top.
from routing.strategies import fixed, routewise  # noqa: E402, F401
```

- [ ] **Step 4: Run the test to verify it (still) fails — submodules don't exist yet**

```bash
uv run pytest tests/unit/apps/backend/routing/test_strategies.py -v
```

Expected: `ModuleNotFoundError: No module named 'routing.strategies.fixed'` (the bottom import fails).

This is expected; Task 3 fixes it.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/routing/strategies/__init__.py tests/unit/apps/backend/routing/test_strategies.py
git commit -m "feat(routing): add strategy registry skeleton (no built-ins yet)"
```

---

## Task 3: Add the `fixed` strategy

**Files:**
- Modify: `apps/backend/routing/routers.py` (constructor signature change)
- Create: `apps/backend/routing/strategies/fixed.py`
- Modify: `tests/unit/apps/backend/routing/test_strategies.py`

- [ ] **Step 1: Add the failing tests**

Append to `tests/unit/apps/backend/routing/test_strategies.py`:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
uv run pytest tests/unit/apps/backend/routing/test_strategies.py -v
```

Expected: failures around missing module / unknown strategy.

- [ ] **Step 3: Update `FixedRouter.__init__` to accept `params`**

In `apps/backend/routing/routers.py`, find:

```python
class FixedRouter(BaseRouter):
    """Weighted random routing with automatic fallback.

    Drop-in replacement for RouteExecutor. Selects adapters via weighted
    random selection and tries remaining adapters on failure.
    """

    def __init__(self) -> None:
        super().__init__()
        self.routes: dict[str, RouteConfig] = {}
```

Replace with:

```python
class FixedRouter(BaseRouter):
    """Weighted random routing with automatic fallback.

    Drop-in replacement for RouteExecutor. Selects adapters via weighted
    random selection and tries remaining adapters on failure.

    Args:
        params: Optional Pydantic ``FixedParams`` (passed by the strategy
            registry).  ``None`` keeps existing call-site behavior.
            ``params.local_fraction`` is currently informational; the existing
            weighted-random selection over ``routes`` is unchanged.
    """

    def __init__(self, params: Any = None) -> None:
        super().__init__()
        self.routes: dict[str, RouteConfig] = {}
        # Keep the validated params accessible for future use (e.g. honoring
        # local_fraction in adapter selection).  Today FixedRouter ignores it
        # because per-route weights already encode local-vs-remote balance.
        self.params = params
```

`Any` is already imported at the top of `apps/backend/routing/routers.py` from `typing`. No other call site is affected — every existing `FixedRouter()` call still works (`params` defaults to `None`).

- [ ] **Step 4: Create `apps/backend/routing/strategies/fixed.py`**

```python
"""Fixed (weighted-random) routing strategy.

Self-registers via ``register_strategy("fixed")`` at import time.  Imported
by ``apps/backend/routing/strategies/__init__.py`` for the side effect.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from routing.routers import FixedRouter
from routing.strategies import register_strategy


class FixedParams(BaseModel):
    """Parameters for the fixed (weighted-random) routing strategy.

    Attributes:
        local_fraction: Fraction of traffic biased toward local deployments
            (0.0-1.0).  Currently informational; per-route weights in
            ``models.yaml`` already encode the local/remote split, so
            ``FixedRouter`` does not consult this field today.  Kept in the
            schema for forward compatibility with hybrid weighting.
    """

    model_config = {"extra": "forbid"}

    local_fraction: float = Field(default=0.5, ge=0.0, le=1.0)


register_strategy("fixed")((FixedRouter, FixedParams))
```

- [ ] **Step 5: Run the tests to verify they pass**

```bash
uv run pytest tests/unit/apps/backend/routing/test_strategies.py -v -k "fixed or unknown or register"
```

Expected: tests written so far pass; tests that reference `routewise` will still fail (Task 4 fixes them; remove the `-k` filter then).

- [ ] **Step 6: Confirm existing FixedRouter callers still work**

```bash
uv run pytest tests/unit/apps/backend/routing/test_executor.py tests/servers/test_registry.py -v
```

Expected: pass — no behavior change for `FixedRouter()` without args.

- [ ] **Step 7: Commit**

```bash
git add apps/backend/routing/routers.py apps/backend/routing/strategies/fixed.py tests/unit/apps/backend/routing/test_strategies.py
git commit -m "feat(routing): register fixed strategy with FixedParams Pydantic schema"
```

---

## Task 4: Add the `routewise` strategy

**Files:**
- Modify: `apps/backend/routing/routewise/router.py` (constructor signature change)
- Create: `apps/backend/routing/strategies/routewise.py`
- Modify: `tests/unit/apps/backend/routing/test_strategies.py`

**Mirror requirement:** `RouteWiseParams` must mirror `RouteWiseConfig` field-for-field with identical defaults. Re-open `apps/backend/routing/routewise/config.py` first and use the live field set, not the snapshot in Task 0. Use the snapshot only as a checklist.

- [ ] **Step 1: Add the failing tests**

Append to `tests/unit/apps/backend/routing/test_strategies.py`:

```python
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
def test_build_routewise_returns_routewise_router(monkeypatch):
    """build_router('routewise', {...}) returns a RouteWiseRouter instance."""
    from routing.routers import FixedRouter
    from routing.routewise.router import RouteWiseRouter
    from routing.strategies import _STRATEGIES, build_router

    # RouteWiseRouter requires a fixed_router for classification; the registry
    # constructs it via params only, so we verify by patching the registered
    # entry to inject a FixedRouter at construction time.
    original = _STRATEGIES["routewise"]
    router_cls, params_cls = original

    class _StubRouteWise(router_cls):  # type: ignore[misc, valid-type]
        def __init__(self, params=None):
            # Bypass classification; we only check type identity here.
            self.params = params

    _STRATEGIES["routewise"] = (_StubRouteWise, params_cls)
    try:
        router = build_router("routewise", {"daily_quota": 100})
        assert isinstance(router, _StubRouteWise)
        assert isinstance(router, RouteWiseRouter)
        assert router.params.daily_quota == 100
    finally:
        _STRATEGIES["routewise"] = original
        # Sanity: FixedRouter still importable, registry unmodified for "fixed".
        assert _STRATEGIES["fixed"][0] is FixedRouter
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
uv run pytest tests/unit/apps/backend/routing/test_strategies.py -v
```

Expected: `ModuleNotFoundError: routing.strategies.routewise`.

- [ ] **Step 3: Update `RouteWiseRouter.__init__` to accept `params`**

In `apps/backend/routing/routewise/router.py`, find:

```python
    def __init__(
        self,
        fixed_router: Any,
        config: RouteWiseConfig,
        experiment_mode: bool = False,
    ) -> None:
        super().__init__(experiment_mode=experiment_mode)
        self.fixed_router = fixed_router
        self.config = config
```

Replace with:

```python
    def __init__(
        self,
        fixed_router: Any = None,
        config: RouteWiseConfig | None = None,
        experiment_mode: bool = False,
        params: Any = None,
    ) -> None:
        """Initialize RouteWiseRouter.

        Two construction shapes are supported:

        1. Direct (legacy): pass ``fixed_router`` + ``config`` (a
           ``RouteWiseConfig`` dataclass).  Used by the existing bootstrap
           path and tests.
        2. Strategy-registry: pass ``params`` (a ``RouteWiseParams`` Pydantic
           model from the strategy registry).  ``params`` is translated to
           ``RouteWiseConfig`` via ``model_dump()``.  ``fixed_router`` is
           bound later by ``ModelRouterRegistry`` (it has the only handle on
           the live ``FixedRouter``); see ``ModelRouterRegistry`` for the
           late-binding mechanism.

        Exactly one of ``config`` or ``params`` should be provided.
        """
        super().__init__(experiment_mode=experiment_mode)

        if config is None and params is not None:
            from routing.routewise.config import RouteWiseConfig as _RWC

            config = _RWC(**params.model_dump())
        if config is None:
            from routing.routewise.config import RouteWiseConfig as _RWC

            config = _RWC()
        self.fixed_router = fixed_router
        self.config = config
```

Note: every existing call to `RouteWiseRouter(fixed_router=..., config=...)` (in `apps/backend/serving/servers/bootstrap.py`) still works (positional/keyword unchanged for the first three args; `params` is a new optional kwarg).

The classification-and-init block (`self._classify_all()` and friends) must run after `fixed_router` is set; we keep that block as-is below the assignment. **However:** when constructed via the registry without a `fixed_router`, the existing `_classify_all`, `_build_adapter_sub_type_map`, `_precompute_api_prices`, `_validate_api_baseline`, `_init_latency_profiles` calls walk `self.fixed_router.routes` and would crash on `None`. Guard the post-init bootstrap so the registry path defers it:

Find the existing init body block right after `self.config = config` (lines that follow `_classify_all`, etc.) and wrap them:

```python
        # Per-model adapter classification.
        self.classified: dict[str, list[tuple[Any, float, SubscriptionType]]] = {}
        # ... existing init state (predictor, quota_mgr, conc_mgr, _pending_decisions,
        # _api_adapter_prices, _latency_profiles, _swrr_samplers, _last_lp_times,
        # _last_lp_weights, _last_lp_statuses, _shadow_hedge_log,
        # _shadow_hedge_log_maxlen, _api_endpoint_map, _pending_lp_solves) ...

        if self.fixed_router is not None:
            self._classify_all()
            self._build_adapter_sub_type_map()
            self._precompute_api_prices()
            self._validate_api_baseline()
            self._init_latency_profiles()
        # else: late-bind via attach_fixed_router() — see below.
```

Add a new method just below `__init__`:

```python
    def attach_fixed_router(self, fixed_router: Any) -> None:
        """Bind a ``FixedRouter`` after construction.

        Used by ``ModelRouterRegistry`` when a model is configured with
        ``router: routewise`` in YAML — the registry constructs the router
        via ``build_router("routewise", params)`` first, then attaches the
        shared ``FixedRouter`` so classification and latency-profile init
        can run.

        Idempotent: a second call rewrites classification.  In normal use it
        is called exactly once, immediately after ``build_router`` returns.
        """
        self.fixed_router = fixed_router
        self.classified = {}
        self._classify_all()
        self._build_adapter_sub_type_map()
        self._precompute_api_prices()
        self._validate_api_baseline()
        self._init_latency_profiles()
```

Engineer note: read the live `RouteWiseRouter.__init__` body before pasting. The two operations are (a) make `fixed_router`/`config` optional, (b) wrap the post-init helpers in `if self.fixed_router is not None:`. Preserve every other initializer line verbatim.

- [ ] **Step 4: Create `apps/backend/routing/strategies/routewise.py`**

```python
"""RouteWise (cost-aware primal-dual) routing strategy.

Self-registers via ``register_strategy("routewise")`` at import time.

``RouteWiseParams`` mirrors ``routing.routewise.config.RouteWiseConfig``
field-for-field with the same defaults.  ``extra="forbid"`` rejects unknown
keys at boot, so a typo in ``models.yaml`` (``router_params: { daily_quotas: 5000 }``)
fails fast with a clear message rather than silently using the default.
"""

from __future__ import annotations

from pydantic import BaseModel

from routing.routewise.router import RouteWiseRouter
from routing.strategies import register_strategy


class RouteWiseParams(BaseModel):
    """Parameters for the RouteWise routing strategy.

    Mirrors :class:`routing.routewise.config.RouteWiseConfig`.  When this
    model and the dataclass diverge, the test
    ``test_routewise_params_mirror_routewise_config_fields`` fails — keeping
    them in lockstep.
    """

    model_config = {"extra": "forbid"}

    # Predictor
    predictor: str = "ema"
    risk_quantile: float = 0.10

    # S_Q quota parameters
    daily_quota: int = 5000
    quota_monthly_fee: float = 20.0
    reset_timezone: str = "UTC"

    # S_C concurrency parameters
    concurrency_enabled: bool = False
    concurrency_limit: int = 8
    concurrency_monthly_fee: float = 25.0

    # Shadow price bounds
    shadow_price_L_seed: float = 0.001
    shadow_price_U_seed: float = 0.500
    shadow_price_adaptive: bool = True
    shadow_price_window_hours: int = 24
    shadow_price_min_ratio: int = 10

    # Layer 2: latency-aware provider selection
    latency_slo_sec: float = 3.0
    latency_target_cdf: float = 0.99
    latency_error_penalty: float = 0.0
    latency_window_sec: float = 900.0
    latency_min_samples: int = 10
    latency_lp_interval_sec: float = 60.0
    latency_swrr_alpha: float = 0.3
    latency_relaxation_factors: str = "1.2,1.5,2.0"
    latency_hedge_mode: str = "shadow"
    latency_hedge_cost_ratio: float = 0.1
    latency_hedge_dispatch_overhead_sec: float = 0.05

    # Canary rollout controls
    canary_enabled: bool = False
    canary_enabled_models: list[str] | None = None
    canary_traffic_fraction: float = 1.0


register_strategy("routewise")((RouteWiseRouter, RouteWiseParams))
```

**Engineer:** if `apps/backend/routing/routewise/config.py` has fields not listed above (it may have drifted), add them here so the mirror test passes.

- [ ] **Step 5: Run the strategy tests**

```bash
uv run pytest tests/unit/apps/backend/routing/test_strategies.py -v
```

Expected: all five tests in `test_strategies.py` pass.

- [ ] **Step 6: Confirm existing RouteWise tests still pass**

```bash
uv run pytest tests/unit/apps/backend/routing/test_routewise_router.py tests/unit/apps/backend/routing/test_routewise_config.py -v
```

Expected: pass — `RouteWiseRouter(fixed_router=..., config=...)` shape is unchanged.

- [ ] **Step 7: Commit**

```bash
git add apps/backend/routing/strategies/routewise.py apps/backend/routing/routewise/router.py tests/unit/apps/backend/routing/test_strategies.py
git commit -m "feat(routing): register routewise strategy mirroring RouteWiseConfig"
```

---

## Task 5: Rewrite `ModelRouterRegistry`

**Files:**
- Modify: `apps/backend/routing/model_router_registry.py`
- Modify: `tests/unit/apps/backend/routing/test_model_router_registry.py`

- [ ] **Step 1: Replace the existing test file**

The existing tests (lines 1-77) cover the `register(model_id, router)` API which is going away. Replace the file in full:

File: `tests/unit/apps/backend/routing/test_model_router_registry.py`

```python
"""Unit tests for ModelRouterRegistry (config-driven dispatch)."""

from __future__ import annotations

import logging

import pytest


@pytest.mark.unit
class TestModelRouterRegistry:
    def test_get_router_returns_fixed_for_unspecified_model(self):
        """Model without 'router:' falls back to default_router_name."""
        from routing.model_router_registry import ModelRouterRegistry
        from routing.routers import FixedRouter

        models_config = {"glm-4.7": {}}
        reg = ModelRouterRegistry(
            models_config=models_config,
            default_router_name="fixed",
        )
        router = reg.get_router("glm-4.7")
        assert isinstance(router, FixedRouter)

    def test_get_router_returns_routewise_when_specified(self, monkeypatch):
        """Model with 'router: routewise' returns a RouteWiseRouter instance."""
        from routing.model_router_registry import ModelRouterRegistry
        from routing.routewise.router import RouteWiseRouter
        from routing.strategies import _STRATEGIES

        # Stub RouteWiseRouter to skip fixed_router classification.
        original = _STRATEGIES["routewise"]
        router_cls, params_cls = original

        class _StubRouteWise(router_cls):  # type: ignore[misc, valid-type]
            def __init__(self, params=None):
                self.params = params
                self.fixed_router = None

            def attach_fixed_router(self, fixed_router):
                self.fixed_router = fixed_router

        _STRATEGIES["routewise"] = (_StubRouteWise, params_cls)
        try:
            models_config = {"glm-4.7": {"router": "routewise"}}
            reg = ModelRouterRegistry(
                models_config=models_config,
                default_router_name="fixed",
            )
            router = reg.get_router("glm-4.7")
            assert isinstance(router, RouteWiseRouter)
        finally:
            _STRATEGIES["routewise"] = original

    def test_get_router_caches_per_model(self):
        """Repeated get_router(same_model) returns the same instance."""
        from routing.model_router_registry import ModelRouterRegistry

        reg = ModelRouterRegistry(
            models_config={"glm-4.7": {}},
            default_router_name="fixed",
        )
        a = reg.get_router("glm-4.7")
        b = reg.get_router("glm-4.7")
        assert a is b

    def test_get_router_emits_router_initialized_log(self, caplog):
        """Cache miss logs a router_initialized event."""
        from routing.model_router_registry import ModelRouterRegistry

        reg = ModelRouterRegistry(
            models_config={"glm-4.7": {"router_params": {"local_fraction": 0.7}}},
            default_router_name="fixed",
        )
        with caplog.at_level(logging.INFO, logger="routing.model_router_registry"):
            reg.get_router("glm-4.7")
        events = [
            r for r in caplog.records
            if getattr(r, "event", None) == "router_initialized"
        ]
        assert len(events) == 1
        rec = events[0]
        assert rec.model == "glm-4.7"
        assert rec.strategy == "fixed"
        assert rec.param_keys == ["local_fraction"]

    def test_get_router_uses_default_router_from_config(self, monkeypatch):
        """default_router_name='routewise' applies when model omits 'router'."""
        from routing.model_router_registry import ModelRouterRegistry
        from routing.routewise.router import RouteWiseRouter
        from routing.strategies import _STRATEGIES

        original = _STRATEGIES["routewise"]
        router_cls, params_cls = original

        class _StubRouteWise(router_cls):  # type: ignore[misc, valid-type]
            def __init__(self, params=None):
                self.params = params
                self.fixed_router = None

            def attach_fixed_router(self, fixed_router):
                self.fixed_router = fixed_router

        _STRATEGIES["routewise"] = (_StubRouteWise, params_cls)
        try:
            reg = ModelRouterRegistry(
                models_config={"glm-4.7": {}},
                default_router_name="routewise",
            )
            router = reg.get_router("glm-4.7")
            assert isinstance(router, RouteWiseRouter)
        finally:
            _STRATEGIES["routewise"] = original

    def test_get_router_unknown_strategy_raises(self):
        from routing.model_router_registry import ModelRouterRegistry

        reg = ModelRouterRegistry(
            models_config={"x": {"router": "made-up-strategy"}},
            default_router_name="fixed",
        )
        with pytest.raises(ValueError) as exc:
            reg.get_router("x")
        assert "made-up-strategy" in str(exc.value)
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
uv run pytest tests/unit/apps/backend/routing/test_model_router_registry.py -v
```

Expected: failures — `ModelRouterRegistry` still has the old signature.

- [ ] **Step 3: Rewrite `apps/backend/routing/model_router_registry.py`**

Full replacement:

```python
"""Per-model router registry with YAML-config-driven dispatch.

Reads each model's ``router`` and ``router_params`` from the parsed
``models.yaml`` config and constructs the corresponding strategy via
``routing.strategies.build_router``.  Routers are cached per model_id, so
the first ``get_router(model_id)`` call pays the construction cost and
subsequent calls return the same instance.

When a model omits ``router:``, ``default_router_name`` (typically read
from ``routing.yaml``'s ``default_router`` field) is used.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from routing.strategies import build_router
from serving.utils.logging import get_logger

if TYPE_CHECKING:
    from routing.routers import BaseRouter

logger = get_logger(__name__)


class ModelRouterRegistry:
    """Maps ``model_id -> BaseRouter`` via per-model YAML configuration.

    Args:
        models_config: Mapping ``{model_id: per_model_dict}``.  Each
            ``per_model_dict`` may contain ``router`` (strategy name) and
            ``router_params`` (dict).  Models not in the mapping fall back
            to ``default_router_name`` with empty params.
        default_router_name: Strategy name used when a model omits
            ``router``.  Must be a registered strategy (e.g. ``"fixed"``).
    """

    def __init__(
        self,
        models_config: dict[str, dict[str, Any]],
        default_router_name: str = "fixed",
    ) -> None:
        self._configs = models_config
        self._default = default_router_name
        self._cache: dict[str, BaseRouter] = {}
        # The shared FixedRouter is bound after construction (see
        # bind_fixed_router); RouteWise needs it for classification.
        self._shared_fixed: BaseRouter | None = None

    def bind_fixed_router(self, fixed_router: BaseRouter) -> None:
        """Provide the shared ``FixedRouter`` for late-bound strategies.

        Strategies like RouteWise need a handle on the live ``FixedRouter``
        (whose ``routes`` dict provides the per-model adapter lists).  The
        registry constructs the strategy first, then calls
        ``attach_fixed_router(self._shared_fixed)`` on it if available.

        Must be called before the first ``get_router(...)`` call for any
        model whose strategy late-binds to the FixedRouter.
        """
        self._shared_fixed = fixed_router

    def get_router(self, model_id: str) -> BaseRouter:
        """Return (constructing on first call) the router for ``model_id``."""
        cached = self._cache.get(model_id)
        if cached is not None:
            return cached
        cfg = self._configs.get(model_id, {})
        name = cfg.get("router", self._default)
        params = cfg.get("router_params") or {}
        logger.info(
            "router_initialized",
            extra={
                "event": "router_initialized",
                "model": model_id,
                "strategy": name,
                "param_keys": sorted(params.keys()),
            },
        )
        router = build_router(name, params)
        # Late-bind FixedRouter for RouteWise (and any future late-bound
        # strategy that exposes attach_fixed_router).
        attach = getattr(router, "attach_fixed_router", None)
        if attach is not None and self._shared_fixed is not None:
            attach(self._shared_fixed)
        self._cache[model_id] = router
        return router

    def registered_models(self) -> dict[str, str]:
        """Return ``{model_id: router_class_name}`` for every cached entry."""
        return {mid: type(r).__name__ for mid, r in self._cache.items()}
```

- [ ] **Step 4: Run the new tests to verify they pass**

```bash
uv run pytest tests/unit/apps/backend/routing/test_model_router_registry.py -v
```

Expected: all six tests pass.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/routing/model_router_registry.py tests/unit/apps/backend/routing/test_model_router_registry.py
git commit -m "feat(routing): rewrite ModelRouterRegistry around build_router + cache"
```

---

## Task 6: Migrate `RoutingConfig` (default_router + deprecated aliases)

**Files:**
- Modify: `apps/backend/routing/config.py`
- Create (or extend): `tests/unit/apps/backend/routing/test_config.py`
- Modify: `tests/fixtures/test_routing.yaml`
- Modify: `tests/servers/conftest.py` (single-line YAML key rename to silence the deprecation warning in unrelated tests)

- [ ] **Step 1: Write the failing tests**

File: `tests/unit/apps/backend/routing/test_config.py`

```python
"""Unit tests for RoutingConfig migration of legacy fields."""

from __future__ import annotations

import logging

import pytest

from routing.config import RoutingConfig


@pytest.mark.unit
def test_default_router_default_is_fixed():
    cfg = RoutingConfig()
    assert cfg.default_router == "fixed"


@pytest.mark.unit
def test_legacy_routing_strategy_migrates_to_default_router():
    """routing_strategy: 'routewise' migrates to default_router='routewise'."""
    cfg = RoutingConfig.model_validate({"routing_strategy": "routewise"})
    assert cfg.default_router == "routewise"


@pytest.mark.unit
def test_explicit_default_router_wins_over_legacy():
    """When both fields are set, default_router takes precedence."""
    cfg = RoutingConfig.model_validate(
        {"routing_strategy": "routewise", "default_router": "fixed"}
    )
    assert cfg.default_router == "fixed"


@pytest.mark.unit
def test_legacy_fields_emit_deprecation_warning(caplog):
    with caplog.at_level(logging.WARNING, logger="routing.config"):
        RoutingConfig.model_validate({"routing_strategy": "fixed"})
    msgs = [r.getMessage() for r in caplog.records]
    assert any("deprecated" in m.lower() for m in msgs)


@pytest.mark.unit
def test_no_warning_when_only_default_router_used(caplog):
    with caplog.at_level(logging.WARNING, logger="routing.config"):
        RoutingConfig.model_validate({"default_router": "fixed"})
    msgs = [r.getMessage() for r in caplog.records]
    assert not any("deprecated" in m.lower() for m in msgs)


@pytest.mark.unit
def test_legacy_routing_parameter_still_accepted(caplog):
    """Legacy 'routing_parameter' block round-trips with a deprecation."""
    with caplog.at_level(logging.WARNING, logger="routing.config"):
        cfg = RoutingConfig.model_validate(
            {"routing_parameter": {"local_fraction": 0.3}}
        )
    assert cfg.routing_parameter is not None
    assert cfg.routing_parameter.local_fraction == 0.3
    msgs = [r.getMessage() for r in caplog.records]
    assert any("deprecated" in m.lower() for m in msgs)
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
uv run pytest tests/unit/apps/backend/routing/test_config.py -v
```

Expected: failures — `default_router` does not exist; legacy migration logic absent.

- [ ] **Step 3: Patch `apps/backend/routing/config.py`**

Find the `RoutingConfig` class and replace it (preserving `Deployment`, `RoutingParameter`, helpers, and `load_routing_config` above and below):

```python
class RoutingConfig(BaseModel):  # type: ignore[no-any-unimported]
    """Complete routing configuration schema.

    Attributes:
        default_router: Strategy name used when a model in models.yaml
            omits its own ``router:`` field.  Defaults to ``"fixed"``.
        timeout: HTTP timeout in seconds for health checks.
        health_check: Health check interval in seconds (0 to disable).
        logging: Logging configuration dictionary.
        local_deployment: List of local deployment configurations.
        remote_deployment: List of remote deployment configurations.

        routing_strategy: DEPRECATED — use ``default_router``.  Migrated by
            ``_migrate_legacy_fields``.
        routing_parameter: DEPRECATED — move per-strategy params into
            ``router_params`` per model in ``models.yaml``.  Kept for one
            release so existing ``routing.yaml`` files keep loading.
    """

    default_router: str = Field(default="fixed")
    timeout: int = 2
    health_check: int = 0
    logging: dict[str, Any] = Field(default_factory=dict)
    local_deployment: list[Deployment] = Field(default_factory=list)
    remote_deployment: list[Deployment] = Field(default_factory=list)

    # Deprecated aliases — keep for one release.
    routing_strategy: str | None = Field(default=None)
    routing_parameter: RoutingParameter | None = Field(default=None)

    @field_validator("timeout", "health_check")
    @classmethod
    def _validate_pos(cls, v: int) -> int:
        if v < 0:
            raise ValueError("value must be non-negative")
        return v

    @model_validator(mode="after")
    def _migrate_legacy_fields(self) -> "RoutingConfig":
        # If user provided routing_strategy and didn't override default_router,
        # promote the legacy value.  We detect "default_router not overridden"
        # by checking the model_fields_set frozenset populated by Pydantic.
        if (
            self.routing_strategy
            and "default_router" not in self.model_fields_set
        ):
            object.__setattr__(self, "default_router", self.routing_strategy)
        if self.routing_strategy is not None or self.routing_parameter is not None:
            _logger.warning(
                "routing.yaml uses deprecated 'routing_strategy'/'routing_parameter' "
                "fields; migrate to 'default_router' + per-model 'router'/'router_params' "
                "in models.yaml. The legacy fields will be removed in a future release."
            )
        return self
```

Imports at the top of `apps/backend/routing/config.py` need to be augmented:

```python
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator
```

And add a module logger near the top of the file (after the imports, before `_ENV_PATTERN`):

```python
from serving.utils.logging import get_logger

_logger = get_logger(__name__)
```

- [ ] **Step 4: Update fixtures and conftest YAMLs to the new field name**

In `tests/fixtures/test_routing.yaml`, change line 1 from `routing_strategy: fixed` to `default_router: fixed`.

In `tests/servers/conftest.py` line 609 (find with `grep -n "routing_strategy: fixed" tests/servers/conftest.py`), change `routing_strategy: fixed` to `default_router: fixed` inside the embedded YAML string. **Only that one line.**

- [ ] **Step 5: Run the new tests + the manager test that loads the fixture**

```bash
uv run pytest tests/unit/apps/backend/routing/test_config.py tests/unit/apps/backend/routing/test_manager.py tests/servers/test_registry.py -v
```

Expected: all pass. `test_manager.py` may load `routing.yaml` in a way that touches `routing_strategy`; if it does, fix it in the same commit by switching its embedded YAML string from `routing_strategy: fixed` to `default_router: fixed`. Re-run.

- [ ] **Step 6: Commit**

```bash
git add apps/backend/routing/config.py tests/unit/apps/backend/routing/test_config.py \
        tests/fixtures/test_routing.yaml tests/servers/conftest.py
git commit -m "feat(routing): RoutingConfig.default_router with legacy field migration"
```

---

## Task 7: Surface `router` / `router_params` in `ModelRegistrationInfo`

**Files:**
- Modify: `apps/backend/serving/servers/registry.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/servers/test_registry.py`:

```python
def test_register_from_models_yaml_propagates_router_fields(tmp_path):
    """router and router_params from models.yaml flow into ModelRegistrationInfo."""
    from pathlib import Path

    from routing.routers import FixedRouter
    from serving.servers import registry

    yaml = """
models:
  - id: model-with-router
    name: M1
    provider: zai
    router: routewise
    router_params:
      daily_quota: 1000
    route:
      - kind: zai
        weight: 1.0
        base_url: http://example.com
        api_key: x
        provider_model_id: m1
  - id: model-without-router
    name: M2
    provider: zai
    route:
      - kind: zai
        weight: 1.0
        base_url: http://example.com
        api_key: x
        provider_model_id: m2
"""
    p = tmp_path / "models.yaml"
    p.write_text(yaml)
    exe = FixedRouter()
    _count, infos = registry.register_from_models_yaml(exe, Path(p))

    by_id = {i.model_id: i for i in infos}
    assert by_id["model-with-router"].router == "routewise"
    assert by_id["model-with-router"].router_params == {"daily_quota": 1000}
    assert by_id["model-without-router"].router is None
    assert by_id["model-without-router"].router_params is None
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
uv run pytest tests/servers/test_registry.py::test_register_from_models_yaml_propagates_router_fields -v
```

Expected: `AttributeError: 'ModelRegistrationInfo' object has no attribute 'router'`.

- [ ] **Step 3: Extend `ModelRegistrationInfo` and the harvest call**

In `apps/backend/serving/servers/registry.py`:

Find:

```python
@dataclass
class ModelRegistrationInfo:
    """Per-model metadata returned from YAML registration."""

    model_id: str
    strategy: str | None = None
    aliases: list[str] = field(default_factory=list)
```

Replace with:

```python
@dataclass
class ModelRegistrationInfo:
    """Per-model metadata returned from YAML registration.

    Attributes:
        model_id: Canonical model identifier.
        strategy: DEPRECATED — legacy ``routing_strategy:`` value.  Read by
            existing bootstrap code; new code should use ``router`` instead.
        aliases: Alternate model_ids that share this model's route.
        router: Strategy name from ``models.yaml`` ``router:`` field
            (e.g. ``"fixed"``, ``"routewise"``).  ``None`` means "use
            ``default_router`` from routing.yaml".
        router_params: Raw params dict from ``models.yaml`` ``router_params:``,
            passed to the strategy's Pydantic model by ``ModelRouterRegistry``.
            ``None`` means "use strategy defaults".
    """

    model_id: str
    strategy: str | None = None
    aliases: list[str] = field(default_factory=list)
    router: str | None = None
    router_params: dict[str, Any] | None = None
```

Add `Any` to the imports at the top if not already there: `from typing import Any` — check first; the file likely already imports it.

Find the `model_infos.append(ModelRegistrationInfo(...))` block (around line 407):

```python
        model_infos.append(
            ModelRegistrationInfo(
                model_id=model_id,
                strategy=m.get("routing_strategy"),
                aliases=aliases,
            )
        )
```

Replace with:

```python
        model_infos.append(
            ModelRegistrationInfo(
                model_id=model_id,
                strategy=m.get("routing_strategy"),
                aliases=aliases,
                router=m.get("router"),
                router_params=m.get("router_params"),
            )
        )
```

- [ ] **Step 4: Run the test**

```bash
uv run pytest tests/servers/test_registry.py -v
```

Expected: all tests pass, including the new one.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/serving/servers/registry.py tests/servers/test_registry.py
git commit -m "feat(serving): propagate router/router_params from models.yaml to ModelRegistrationInfo"
```

---

## Task 8: Wire `ModelRouterRegistry` into bootstrap

**Files:**
- Modify: `apps/backend/serving/servers/bootstrap.py`

- [ ] **Step 1: Read the existing bootstrap block**

Open `apps/backend/serving/servers/bootstrap.py` lines 264-301. The current block manually instantiates `RouteWiseRouter` and registers it per model. Replace with a config-driven build.

- [ ] **Step 2: Replace the bootstrap block**

Find:

```python
    # RouteWise router (optional, per-model opt-in via models.yaml routing_strategy)
    model_router_registry: ModelRouterRegistry | None = None
    settings = get_settings()
    needs_routewise = settings.enable_routewise or any(
        info.strategy == "routewise" for info in model_infos
    )
    if needs_routewise:
        try:
            from routing.routewise import RouteWiseRouter, load_routewise_config

            rw_config = load_routewise_config()
            routewise_router = RouteWiseRouter(
                fixed_router=router,
                config=rw_config,
                experiment_mode=settings.experiment_mode,
            )
            model_router_registry = ModelRouterRegistry(default_router=router)
            for info in model_infos:
                if info.strategy == "routewise":
                    model_router_registry.register(info.model_id, routewise_router)
                    for alias in info.aliases:
                        model_router_registry.register(alias, routewise_router)
            # TODO: Wire canary rollout from routewise.yaml canary section.
            # Currently configure_canary() is never called; canary config is dead.
            # rw_config has canary fields; call model_router_registry.configure_canary()
            # once canary rollout is ready for production.
            rw_models = [i.model_id for i in model_infos if i.strategy == "routewise"]
            logger.info(f"RouteWise initialized for {len(rw_models)} model(s): {rw_models}")
        except Exception as exc:
            logger.warning(f"RouteWise initialization failed: {exc}. Using fixed routing.")
            model_router_registry = None
```

Replace with:

```python
    # Per-model router registry — config-driven from models.yaml.
    settings = get_settings()
    models_config: dict[str, dict[str, Any]] = {}
    for info in model_infos:
        # Effective router: explicit `router:` wins; otherwise legacy
        # `routing_strategy:` (one-release shim) maps onto `router`.
        effective_router = info.router or info.strategy
        entry: dict[str, Any] = {}
        if effective_router is not None:
            entry["router"] = effective_router
        if info.router_params is not None:
            entry["router_params"] = info.router_params
        # Aliases share the canonical model's config.
        models_config[info.model_id] = entry
        for alias in info.aliases:
            models_config[alias] = entry

    # Default router name from routing.yaml; falls back to "fixed".
    default_router_name = getattr(routing_cfg, "default_router", "fixed")

    # ENABLE_ROUTEWISE legacy: opts every model into routewise as the default.
    if settings.enable_routewise and default_router_name == "fixed":
        default_router_name = "routewise"

    model_router_registry: ModelRouterRegistry | None = ModelRouterRegistry(
        models_config=models_config,
        default_router_name=default_router_name,
    )
    model_router_registry.bind_fixed_router(router)

    # Eagerly construct routers for every known model so config errors
    # (bad strategy name, bad router_params) surface at boot, not on the
    # first request.
    try:
        for info in model_infos:
            model_router_registry.get_router(info.model_id)
    except Exception as exc:
        logger.error(f"ModelRouterRegistry initialization failed: {exc}")
        model_router_registry = None
```

The `Any` import at the top of bootstrap.py is already present via `from typing import Any` — verify with `grep -n "from typing" apps/backend/serving/servers/bootstrap.py`; if missing, add it.

`routing_cfg` (the parsed `RoutingConfig`) must be in scope here. Search for it:

```bash
grep -n "routing_cfg\|RoutingConfig\|load_routing_config" apps/backend/serving/servers/bootstrap.py
```

If the variable name differs, use the actual one. If the parsed config is not in scope at this point, load it locally:

```python
from pathlib import Path
from routing.config import load_routing_config
routing_cfg = load_routing_config(Path("config/routing.yaml"))
```

Place this fallback just above the `default_router_name = ...` line if needed.

- [ ] **Step 3: Run the bootstrap-adjacent tests**

```bash
uv run pytest tests/unit/routing -v test/servers -v
```

Expected: pass.

- [ ] **Step 4: Commit**

```bash
git add apps/backend/serving/servers/bootstrap.py
git commit -m "feat(serving): wire ModelRouterRegistry from models.yaml router fields"
```

---

## Task 9: Update `config/routing.yaml` and `config/models.yaml`

**Files:**
- Modify: `config/routing.yaml`
- Modify: `config/models.yaml`

- [ ] **Step 1: Update `config/routing.yaml`**

Replace lines 1-3:

```yaml
routing_strategy: fixed
routing_parameter:
  local_fraction: 1
```

With:

```yaml
default_router: fixed
```

The remaining file (`timeout`, `health_check`, `logging`, `local_deployment`, `remote_deployment`) stays unchanged.

- [ ] **Step 2: Migrate the canary list into `models.yaml`**

Engineer pre-step (already done in Task 0): record the list of models that today have `routing_strategy: routewise` set (or are gated only by `ENABLE_ROUTEWISE=1` env var). Today (per Task 0 grep) the list is **empty in the canonical `config/models.yaml`** — RouteWise is currently driven entirely by the env var and per-model `routing_strategy:` is unset.

Therefore:

- If the Task 0 grep returned **zero** models with `routing_strategy: routewise`, no edits to `config/models.yaml` are required for the canary migration. Document this in the PR description as "canary list was empty; RouteWise opt-in remains via `ENABLE_ROUTEWISE` until operators opt models in via `router: routewise`."
- If the Task 0 grep returned **N>0** models, for each such model rename `routing_strategy: routewise` to `router: routewise`. Move any `routing_parameter:` block on the same model into `router_params:` (same indentation, same keys — RouteWise field names match).

For both cases, the diff in the PR description must show the before/after for affected model entries (or "no entries affected" if N=0).

- [ ] **Step 3: Lint the YAML files**

```bash
uv run python -c "import yaml, pathlib; \
yaml.safe_load(pathlib.Path('config/routing.yaml').read_text()); \
yaml.safe_load(pathlib.Path('config/models.yaml').read_text()); \
print('ok')"
```

Expected: prints `ok`.

- [ ] **Step 4: Confirm `RoutingConfig` loads the new file cleanly**

```bash
uv run python -c "from pathlib import Path; from routing.config import load_routing_config; \
cfg = load_routing_config(Path('config/routing.yaml')); \
print('default_router:', cfg.default_router); \
print('routing_strategy (legacy):', cfg.routing_strategy)"
```

Expected: `default_router: fixed`, `routing_strategy (legacy): None`. No deprecation warning printed.

- [ ] **Step 5: Commit**

```bash
git add config/routing.yaml config/models.yaml
git commit -m "config: switch routing.yaml to default_router; migrate per-model router declarations"
```

---

## Task 10: Integration test — YAML-driven dispatch

**Files:**
- Create: `tests/integration/apps/backend/routing/__init__.py` (empty)
- Create: `tests/integration/apps/backend/routing/test_yaml_driven_dispatch.py`

- [ ] **Step 1: Create the package marker**

File: `tests/integration/apps/backend/routing/__init__.py`

```python
```

(Empty file.)

- [ ] **Step 2: Write the integration test**

File: `tests/integration/apps/backend/routing/test_yaml_driven_dispatch.py`

```python
"""Integration: a small models.yaml fixture drives ModelRouterRegistry dispatch.

Loads a fixture with mixed `router: fixed`, `router: routewise`, and
unspecified entries; constructs ModelRouterRegistry; asserts the right
router class per model; smoke-tests that select doesn't crash.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.mark.integration
def test_models_yaml_drives_router_dispatch(tmp_path):
    from routing.model_router_registry import ModelRouterRegistry
    from routing.routers import FixedRouter
    from routing.routewise.router import RouteWiseRouter
    from routing.strategies import _STRATEGIES
    from serving.servers import registry as serving_registry

    yaml = """
models:
  - id: m-default
    name: M-default
    provider: zai
    route:
      - kind: zai
        weight: 1.0
        base_url: http://example.com
        api_key: k
        provider_model_id: m-default
  - id: m-fixed
    name: M-fixed
    provider: zai
    router: fixed
    router_params:
      local_fraction: 0.7
    route:
      - kind: zai
        weight: 1.0
        base_url: http://example.com
        api_key: k
        provider_model_id: m-fixed
  - id: m-routewise
    name: M-routewise
    provider: zai
    router: routewise
    router_params:
      daily_quota: 100
    route:
      - kind: zai
        weight: 1.0
        base_url: http://example.com
        api_key: k
        provider_model_id: m-routewise
"""
    p = tmp_path / "models.yaml"
    p.write_text(yaml)
    fixed = FixedRouter()
    _count, infos = serving_registry.register_from_models_yaml(fixed, Path(p))

    # Stub RouteWiseRouter to skip the heavy classification + LP init paths.
    original = _STRATEGIES["routewise"]
    router_cls, params_cls = original

    class _StubRouteWise(router_cls):  # type: ignore[misc, valid-type]
        def __init__(self, params=None):
            self.params = params
            self.fixed_router = None

        def attach_fixed_router(self, fixed_router):
            self.fixed_router = fixed_router

    _STRATEGIES["routewise"] = (_StubRouteWise, params_cls)
    try:
        models_config = {
            info.model_id: {
                **({"router": info.router} if info.router else {}),
                **({"router_params": info.router_params} if info.router_params else {}),
            }
            for info in infos
        }
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
        # Cache identity preserved.
        assert reg.get_router("m-default") is r_default

        # Smoke: FixedRouter._select_adapter on m-fixed (route registered above)
        # returns one of the configured adapters.
        adapter = r_fixed._select_adapter("m-fixed")
        assert adapter is not None
    finally:
        _STRATEGIES["routewise"] = original


@pytest.mark.integration
def test_models_yaml_unknown_strategy_fails_loudly(tmp_path):
    """A typo in `router:` raises at first get_router call, not silently."""
    from pathlib import Path

    from routing.model_router_registry import ModelRouterRegistry

    reg = ModelRouterRegistry(
        models_config={"m": {"router": "nonexistent_strategy"}},
        default_router_name="fixed",
    )
    with pytest.raises(ValueError) as exc:
        reg.get_router("m")
    assert "nonexistent_strategy" in str(exc.value)


@pytest.mark.integration
def test_models_yaml_bad_router_params_fails_loudly(tmp_path):
    """A bad value in `router_params:` raises at first get_router call."""
    from pydantic import ValidationError

    from routing.model_router_registry import ModelRouterRegistry

    reg = ModelRouterRegistry(
        models_config={"m": {"router": "fixed", "router_params": {"local_fraction": 9.9}}},
        default_router_name="fixed",
    )
    with pytest.raises(ValidationError):
        reg.get_router("m")
```

- [ ] **Step 3: Run the integration tests**

```bash
uv run pytest tests/integration/routing -v
```

Expected: three tests pass.

- [ ] **Step 4: Commit**

```bash
git add tests/integration/apps/backend/routing/__init__.py tests/integration/apps/backend/routing/test_yaml_driven_dispatch.py
git commit -m "test(routing): integration test for YAML-driven router dispatch"
```

---

## Task 11: Full test sweep + format

**Files:** none (validation step).

- [ ] **Step 1: Run the apps/backend/routing/serving test suites end-to-end**

```bash
uv run pytest tests/unit/routing test/servers tests/integration/routing -v
```

Expected: all green. Investigate and fix any red — common causes are stray `routing_strategy:` references in test YAMLs or callers passing positional args to `RouteWiseRouter` that no longer line up.

- [ ] **Step 2: Run `ruff format` per CLAUDE.md**

```bash
make format
```

Expected: clean reformat or no changes.

- [ ] **Step 3: Run `ruff check`**

```bash
uv run ruff check routing serving test
```

Expected: no errors.

- [ ] **Step 4: Run the wider unit suite for confidence**

```bash
uv run pytest test/unit -q
```

Expected: all green.

- [ ] **Step 5: Commit any formatting deltas**

```bash
git add -A
git diff --cached --quiet || git commit -m "style: ruff format"
```

---

## Task 12: Open the PR

**Files:** none.

- [ ] **Step 1: Confirm git status is clean**

```bash
git status
```

Expected: `nothing to commit, working tree clean`.

- [ ] **Step 2: Push the branch**

```bash
git push -u origin jason/claude/routing-config-expressiveness
```

- [ ] **Step 3: Open the PR**

The PR body must include the side-by-side migration diff requested by the spec. If the canary list was empty (Task 9 Step 2), say so explicitly.

```bash
gh pr create --base dev \
  --title "feat(routing): per-model router + params via models.yaml" \
  --body "$(cat <<'EOF'
## Summary
- Introduces `apps/backend/routing/strategies/` with a small registry: each strategy self-registers a `(Router, ParamsModel)` pair via `register_strategy(name)((Router, Params))`. Adding a strategy is a one-file change.
- Rewrites `ModelRouterRegistry` to read each model's `router` and `router_params` from `models.yaml` and dispatch through `routing.strategies.build_router`.
- Adds `RoutingConfig.default_router` (replacing `routing_strategy` + `routing_parameter`); the legacy fields are kept as one-release deprecated aliases that emit a warning and migrate transparently.
- Surfaces `router` / `router_params` through `ModelRegistrationInfo` and wires the registry from `apps/backend/serving/servers/bootstrap.py`.

## Spec / Issue
- Spec: `docs/agents/specs/2026-05-03-routing-config-expressiveness-design.md`
- Plan: `docs/agents/plans/2026-05-03-routing-config-expressiveness.md`
- Issue: <fill from Task 1 Step 3 output>

## Canary-list migration (parity check)

| Model (old: `routing_strategy: routewise`) | New (`router: routewise`) | `router_params:` |
|---|---|---|
| <fill from Task 0 grep — list every model that had `routing_strategy: routewise` in `config/models.yaml` before this PR. If the grep returned zero entries, write "none — RouteWise was previously enabled only via `ENABLE_ROUTEWISE=1`. This PR keeps the env-var path working (`enable_routewise=True` promotes `default_router` to `routewise`) and adds the per-model opt-in." > |

## Test plan
- [ ] `uv run pytest tests/unit/routing -v` — green.
- [ ] `uv run pytest test/servers -v` — green.
- [ ] `uv run pytest tests/integration/routing -v` — green.
- [ ] `uv run pytest test/unit -q` — green.
- [ ] `make format` clean.
- [ ] Manual smoke against staging after deploy: `journalctl -u staging-fi | grep router_initialized` shows one event per model with the expected strategy.

## Risk + rollback
- Rollback: single `git revert`. Legacy `routing_strategy` / `routing_parameter` shim keeps the previous `routing.yaml` loading without changes.
- Boot fails fast on bad `router_params` (Pydantic `extra="forbid"`); preferred over silent fallback.

EOF
)"
```

Record the PR URL.

- [ ] **Step 4: Watch CI**

Per CLAUDE.md, poll every 2 min until green and all comments are resolved:

```bash
gh pr checks --watch
gh pr view --comments
```

Fix any failures by committing to the same branch; do not amend.

- [ ] **Step 5: Cleanup after merge**

```bash
git -C /home/juncheng/hybridInference checkout dev
git -C /home/juncheng/hybridInference pull --ff-only origin dev
git worktree remove /home/juncheng/hybridInference-worktrees/routing-config-expressiveness
git branch -d jason/claude/routing-config-expressiveness
git push origin --delete jason/claude/routing-config-expressiveness
```

---

## Self-Review Checklist

Before marking the plan done, the engineer must verify:

1. **Spec coverage** — every section of `2026-05-03-routing-config-expressiveness-design.md` has a corresponding task:
   - "models.yaml schema additions" → Tasks 7 + 9 + 10.
   - "Code structure" (`apps/backend/routing/strategies/`) → Tasks 2 + 3 + 4.
   - "Strategy registry" (`register_strategy`, `build_router`, registration via bottom imports) → Task 2.
   - "`ModelRouterRegistry` rewrite" → Task 5; eager-init at boot → Task 8.
   - "`routing.yaml` simplified" + Pydantic migration → Tasks 6 + 9.
   - "Migration of existing canary list" → Task 9 Step 2 + PR body in Task 12.
   - "Testing" (registry / params validation / dispatch / migration / integration) → Tasks 2-6 + 10.
   - "Risk + rollback" — Pydantic fail-fast (Task 4 / 5 / 10) and revert mention (Task 12 PR body).
2. **Backward-compat alias is tested.** `test_legacy_routing_strategy_migrates_to_default_router` and `test_legacy_routing_parameter_still_accepted` cover this in Task 6.
3. **Type consistency check:**
   - `register_strategy(name)((Router, Params))` shape used in Tasks 3 & 4 matches the registry implementation in Task 2.
   - `build_router(name, params)` signature: `(name: str, params: dict | None) -> BaseRouter`, used identically in Tasks 5, 10.
   - `params` kwarg added to `FixedRouter.__init__` (Task 3) and `RouteWiseRouter.__init__` (Task 4); both default to `None` so legacy callers unchanged.
   - `ModelRouterRegistry.__init__(models_config, default_router_name)` matches the call in Task 8.
   - `attach_fixed_router` defined in Task 4 and called in Task 5.
4. **Placeholder scan.** No "TBD", "TODO", "fill in details", or "similar to Task N" remain. The Task 9 Step 2 placeholder for the canary migration table in the PR body is bounded ("if the grep returned zero, write …; else, fill the table") and points back to a Task 0 grep — not a free-form gap.
5. **Field mirror invariant.** `test_routewise_params_mirror_routewise_config_fields` (Task 4) makes the `RouteWiseConfig` → `RouteWiseParams` mirror a CI-enforced invariant.
6. **No file lacks a path.** Every "Files:" block is absolute or rooted at the repo root with a clear create/modify verb.

If any of these check fails, fix it inline in the affected task before handoff.
