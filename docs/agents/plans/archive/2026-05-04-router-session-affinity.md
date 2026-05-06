# Router Session Affinity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Per-user provider stickiness in `FixedRouter`: same `(auth_key_hash or client IP, model_id)` → same endpoint for 5 min, sliding TTL, dropped on any error or when pinned endpoint leaves the allowed pool.

**Architecture:** New `_affinity` table on `BaseRouter` keyed by `(affinity_key, model_id)`, guarded by the existing `self._lock`. `FixedRouter._select_adapter` consults the table before weighted-random selection and writes after a fresh pick. `BaseRouter.chat_completion`/`stream_chat_completion` drop the entry on primary-error before fallback. Affinity key sourced from `req_ctx`, populated by the completions handler from `auth_key_hash` (authenticated) or `f"ip:{client_ip}"` (anonymous). All state in-process, mirrors `apps/backend/serving/adapters/key_pool.py`.

**Tech Stack:** Python 3.12, FastAPI, `threading.RLock`, `time.monotonic()`, `pytest`. No new dependencies.

**Spec:** [docs/agents/specs/2026-05-04-router-session-affinity-design.md](../specs/2026-05-04-router-session-affinity-design.md)

---

## Pre-flight

- [ ] **Step 0: Confirm working tree is clean and on the feature branch**

```bash
git status
git branch --show-current
```

Expected: clean tree, branch `juncheng/claude/router-session-affinity`.

---

## Task 1: Add `ROUTING_AFFINITY` no-op metric

**Files:**
- Modify: `apps/backend/serving/observability/metrics.py`

The codebase ships Prometheus-style counters as no-ops (`_LabeledNoOp`). Adding the metric now means later tasks can `.labels(...).inc()` without conditional imports.

- [ ] **Step 1.1: Add the counter constant**

In `apps/backend/serving/observability/metrics.py`, after line 103 (`KEY_POOL_ACTIVE_AFFINITIES = _LabeledGaugeNoOp()`), insert a new section:

```python
# Routing affinity metrics
ROUTING_AFFINITY = _LabeledNoOp()
```

- [ ] **Step 1.2: Export it**

In the same file, in the `__all__` list (starts ~line 171), add `"ROUTING_AFFINITY",` in alphabetical position next to the other `ROUTING_*` entries (after `"ROUTEWISE_VALUE_ESTIMATE",`, before `"ROUTING_STRATEGY_SELECTED",`).

- [ ] **Step 1.3: Verify import**

Run:

```bash
uv run python -c "from serving.observability.metrics import ROUTING_AFFINITY; ROUTING_AFFINITY.labels(event='hit', model='m').inc()"
```

Expected: no output, no error.

- [ ] **Step 1.4: Commit**

```bash
git add apps/backend/serving/observability/metrics.py
git commit -m "feat(metrics): add ROUTING_AFFINITY no-op counter"
```

---

## Task 2: Affinity data model + helpers on `BaseRouter`

**Files:**
- Modify: `apps/backend/routing/routers.py`
- Test: `tests/unit/routing/test_session_affinity.py` (new)

Introduce `_Affinity`, the `_affinity` dict, the module-level constants, and two helpers (`_drop_affinity`, `_maybe_sweep_affinity_locked`). No selection wiring yet — that's Task 3.

- [ ] **Step 2.1: Write failing tests for the helpers**

Create `tests/unit/routing/test_session_affinity.py`:

```python
from __future__ import annotations

import os
import time
from typing import TYPE_CHECKING, Any

import pytest

from routing.routers import (
    AFFINITY_SWEEP_THRESHOLD,
    AFFINITY_TTL_SECONDS,
    FixedRouter,
    _Affinity,
)
from serving.adapters.base import BaseAdapter, ModelConfig
from serving.utils import context as req_ctx

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


def _cfg(mid: str, provider: str = "p", base_url: str = "http://test") -> ModelConfig:
    return ModelConfig(
        id=mid,
        name=mid,
        provider=provider,
        base_url=base_url,
        context_length=8192,
        max_output_length=4096,
    )


class _EchoAdapter(BaseAdapter):
    async def chat_completion(self, messages: list[dict[str, Any]], **params) -> dict[str, Any]:
        return self.format_response(content="ok", model=self.config.id)

    async def stream_chat_completion(
        self, messages: list[dict[str, Any]], **params
    ) -> "AsyncGenerator[str, None]":
        yield self.format_stream_chunk(model=self.config.id, content="ok")


class _FailAdapter(BaseAdapter):
    async def chat_completion(self, messages: list[dict[str, Any]], **params) -> dict[str, Any]:
        raise RuntimeError("fail")

    async def stream_chat_completion(
        self, messages: list[dict[str, Any]], **params
    ) -> "AsyncGenerator[str, None]":
        raise RuntimeError("fail")
        yield  # pragma: no cover


@pytest.mark.unit
def test_constants_exposed():
    assert AFFINITY_TTL_SECONDS == 300.0
    assert AFFINITY_SWEEP_THRESHOLD == 1000


@pytest.mark.unit
def test_affinity_dict_initialized_empty():
    r = FixedRouter()
    assert r._affinity == {}


@pytest.mark.unit
def test_drop_affinity_no_op_when_no_key():
    r = FixedRouter()
    req_ctx.set({})
    r._drop_affinity("m")  # must not raise
    assert r._affinity == {}


@pytest.mark.unit
def test_drop_affinity_removes_entry():
    r = FixedRouter()
    r._affinity[("u1", "m")] = _Affinity(endpoint_id="p:host:1", expires_at=time.monotonic() + 60)
    req_ctx.set({"affinity_key": "u1"})
    r._drop_affinity("m")
    assert ("u1", "m") not in r._affinity


@pytest.mark.unit
def test_drop_affinity_other_models_untouched():
    r = FixedRouter()
    r._affinity[("u1", "m1")] = _Affinity(endpoint_id="p:host:1", expires_at=time.monotonic() + 60)
    r._affinity[("u1", "m2")] = _Affinity(endpoint_id="p:host:2", expires_at=time.monotonic() + 60)
    req_ctx.set({"affinity_key": "u1"})
    r._drop_affinity("m1")
    assert ("u1", "m1") not in r._affinity
    assert ("u1", "m2") in r._affinity


@pytest.mark.unit
def test_maybe_sweep_below_threshold_is_noop():
    r = FixedRouter()
    now = time.monotonic()
    r._affinity[("u1", "m")] = _Affinity(endpoint_id="p:host:1", expires_at=now - 1)
    with r._lock:
        r._maybe_sweep_affinity_locked(now)
    assert ("u1", "m") in r._affinity


@pytest.mark.unit
def test_maybe_sweep_above_threshold_drops_expired():
    r = FixedRouter()
    now = time.monotonic()
    for i in range(AFFINITY_SWEEP_THRESHOLD + 1):
        r._affinity[(f"u{i}", "m")] = _Affinity(
            endpoint_id="p:host:1",
            expires_at=now + (60 if i % 2 == 0 else -1),
        )
    with r._lock:
        r._maybe_sweep_affinity_locked(now)
    expired = [k for k, a in r._affinity.items() if a.expires_at < now]
    assert expired == []
```

- [ ] **Step 2.2: Run tests to verify they fail**

```bash
uv run pytest tests/unit/routing/test_session_affinity.py -v
```

Expected: ImportError on `AFFINITY_TTL_SECONDS`, `AFFINITY_SWEEP_THRESHOLD`, `_Affinity`, or `AttributeError` on `_affinity` / `_drop_affinity` / `_maybe_sweep_affinity_locked`.

- [ ] **Step 2.3: Add the data model and constants**

In `apps/backend/routing/routers.py`, find the `# Data Classes` section header (around line 60) and add after the existing `RoutingObservation` dataclass:

```python
@dataclass
class _Affinity:
    """Per-user provider pin for one model. TTL is monotonic time."""

    endpoint_id: str
    expires_at: float
```

Then in the `# Helpers` section (around line 96, before `_get_endpoint_id`), add module-level constants:

```python
AFFINITY_TTL_SECONDS: float = 300.0
AFFINITY_SWEEP_THRESHOLD: int = 1000
AFFINITY_ENABLED: bool = os.environ.get("ROUTING_AFFINITY_ENABLED", "1") != "0"
```

(`os` is already imported at the top of the file — verify with `grep '^import os' apps/backend/routing/routers.py`. If absent, add it.)

- [ ] **Step 2.4: Add `_affinity` dict and helpers to `BaseRouter`**

In `BaseRouter.__init__` (around line 318), add after the `self._lock` line:

```python
        self._affinity: dict[tuple[str, str], _Affinity] = {}
```

Add two methods to `BaseRouter`, anywhere after `_on_failure` and before `get_provider_status`:

```python
    def _drop_affinity(self, model_id: str) -> None:
        """Drop affinity entry for the current request's affinity_key + model.

        No-op if affinity_key is missing from req_ctx or no entry exists.
        Emits a `dropped_error` metric event when an entry is removed.
        """
        affinity_key = req_ctx.get().get("affinity_key")
        if not affinity_key:
            return
        with self._lock:
            removed = self._affinity.pop((affinity_key, model_id), None)
        if removed is not None:
            ROUTING_AFFINITY.labels(
                event="dropped_error",
                model=normalize_model_label(model_id),
            ).inc()

    def _maybe_sweep_affinity_locked(self, now: float) -> None:
        """Drop expired affinity entries. Caller must hold self._lock."""
        if len(self._affinity) <= AFFINITY_SWEEP_THRESHOLD:
            return
        expired = [k for k, a in self._affinity.items() if a.expires_at < now]
        for k in expired:
            del self._affinity[k]
```

Update the `from serving.observability.metrics import (...)` block at the top of the file to include `ROUTING_AFFINITY`.

- [ ] **Step 2.5: Run tests**

```bash
uv run pytest tests/unit/routing/test_session_affinity.py -v
```

Expected: all 7 tests pass.

- [ ] **Step 2.6: Commit**

```bash
git add apps/backend/routing/routers.py tests/unit/routing/test_session_affinity.py
git commit -m "feat(routing): add affinity data model and helpers on BaseRouter"
```

---

## Task 3: Wire affinity into `FixedRouter._select_adapter`

**Files:**
- Modify: `apps/backend/routing/routers.py` (`FixedRouter._select_adapter`, ~line 619)
- Test: `tests/unit/routing/test_session_affinity.py`

Make selection consult and write the affinity table.

- [ ] **Step 3.1: Add failing selection tests**

Append to `tests/unit/routing/test_session_affinity.py`:

```python
@pytest.mark.unit
def test_first_pick_creates_entry():
    r = FixedRouter()
    a = _EchoAdapter(_cfg("m", provider="A", base_url="http://A"))
    b = _EchoAdapter(_cfg("m", provider="B", base_url="http://B"))
    r.register_route("m", [(a, 0.5), (b, 0.5)])

    req_ctx.set({"affinity_key": "u1"})
    chosen = r._select_adapter("m")
    assert chosen is not None
    entry = r._affinity[("u1", "m")]
    assert entry.endpoint_id in {a.config.provider, b.config.provider}
    assert entry.expires_at > time.monotonic()


@pytest.mark.unit
def test_repeat_pick_reuses_endpoint():
    r = FixedRouter()
    a = _EchoAdapter(_cfg("m", provider="A", base_url="http://A"))
    b = _EchoAdapter(_cfg("m", provider="B", base_url="http://B"))
    r.register_route("m", [(a, 0.5), (b, 0.5)])

    req_ctx.set({"affinity_key": "u1"})
    first = r._select_adapter("m")
    for _ in range(20):
        again = r._select_adapter("m")
        assert again is first


@pytest.mark.unit
def test_repeat_pick_slides_ttl():
    r = FixedRouter()
    a = _EchoAdapter(_cfg("m", provider="A"))
    r.register_route("m", [(a, 1.0)])
    req_ctx.set({"affinity_key": "u1"})

    r._select_adapter("m")
    first_expiry = r._affinity[("u1", "m")].expires_at
    time.sleep(0.01)
    r._select_adapter("m")
    second_expiry = r._affinity[("u1", "m")].expires_at
    assert second_expiry > first_expiry


@pytest.mark.unit
def test_expired_entry_repicks():
    r = FixedRouter()
    a = _EchoAdapter(_cfg("m", provider="A"))
    r.register_route("m", [(a, 1.0)])
    req_ctx.set({"affinity_key": "u1"})

    r._select_adapter("m")
    r._affinity[("u1", "m")].expires_at = time.monotonic() - 1
    chosen = r._select_adapter("m")
    assert chosen is a
    assert r._affinity[("u1", "m")].expires_at > time.monotonic()


@pytest.mark.unit
def test_pinned_endpoint_disabled_drops_entry():
    r = FixedRouter()
    a = _EchoAdapter(_cfg("m", provider="A", base_url="http://A"))
    b = _EchoAdapter(_cfg("m", provider="B", base_url="http://B"))
    r.register_route("m", [(a, 0.5), (b, 0.5)])

    req_ctx.set({"affinity_key": "u1"})
    r._affinity[("u1", "m")] = _Affinity(
        endpoint_id="GHOST",
        expires_at=time.monotonic() + 60,
    )
    chosen = r._select_adapter("m")
    assert chosen in {a, b}
    assert r._affinity[("u1", "m")].endpoint_id != "GHOST"


@pytest.mark.unit
def test_distinct_users_independent_entries():
    r = FixedRouter()
    a = _EchoAdapter(_cfg("m", provider="A"))
    b = _EchoAdapter(_cfg("m", provider="B"))
    r.register_route("m", [(a, 0.5), (b, 0.5)])

    req_ctx.set({"affinity_key": "u1"})
    r._select_adapter("m")
    req_ctx.set({"affinity_key": "u2"})
    r._select_adapter("m")
    assert ("u1", "m") in r._affinity
    assert ("u2", "m") in r._affinity


@pytest.mark.unit
def test_distinct_models_independent_entries():
    r = FixedRouter()
    a1 = _EchoAdapter(_cfg("m1", provider="A"))
    a2 = _EchoAdapter(_cfg("m2", provider="A"))
    r.register_route("m1", [(a1, 1.0)])
    r.register_route("m2", [(a2, 1.0)])

    req_ctx.set({"affinity_key": "u1"})
    r._select_adapter("m1")
    r._select_adapter("m2")
    assert ("u1", "m1") in r._affinity
    assert ("u1", "m2") in r._affinity


@pytest.mark.unit
def test_pin_provider_overrides_affinity():
    r = FixedRouter()
    a = _EchoAdapter(_cfg("m", provider="A"))
    b = _EchoAdapter(_cfg("m", provider="B"))
    r.register_route("m", [(a, 0.5), (b, 0.5)])

    req_ctx.set({"affinity_key": "u1"})
    r._affinity[("u1", "m")] = _Affinity(
        endpoint_id=a.config.provider,
        expires_at=time.monotonic() + 60,
    )
    chosen = r._select_adapter("m", pin_provider="B")
    assert chosen is b


@pytest.mark.unit
def test_no_affinity_key_no_entry():
    r = FixedRouter()
    a = _EchoAdapter(_cfg("m", provider="A"))
    r.register_route("m", [(a, 1.0)])

    req_ctx.set({})
    r._select_adapter("m")
    assert r._affinity == {}


@pytest.mark.unit
def test_disabled_via_env(monkeypatch):
    """When AFFINITY_ENABLED is False, no entries are written or read."""
    import routing.routers as rr

    monkeypatch.setattr(rr, "AFFINITY_ENABLED", False)
    r = FixedRouter()
    a = _EchoAdapter(_cfg("m", provider="A"))
    r.register_route("m", [(a, 1.0)])
    req_ctx.set({"affinity_key": "u1"})
    r._select_adapter("m")
    assert r._affinity == {}
```

- [ ] **Step 3.2: Run new tests to verify they fail**

```bash
uv run pytest tests/unit/routing/test_session_affinity.py -v
```

Expected: the 10 new tests fail (entries never created or pin endpoint logic wrong).

- [ ] **Step 3.3: Modify `FixedRouter._select_adapter`**

Replace the body of `FixedRouter._select_adapter` (currently at [routers.py:619-691](../../apps/backend/routing/routers.py#L619)) with:

```python
    def _select_adapter(  # type: ignore[override]
        self, model_id: str, *, pin_provider: str | None = None
    ) -> BaseAdapter | None:
        """Select an adapter using weighted random selection with optional affinity.

        Args:
            model_id: Model identifier.
            pin_provider: Optional provider/endpoint_id to pin to. Overrides affinity.

        Returns:
            Selected adapter or None if no route configured / no match.
        """
        route = self.routes.get(model_id)
        if not route or not route.adapters:
            return None

        if pin_provider:
            for adapter, weight in route.adapters:
                if weight <= 0:
                    continue
                eid = _get_endpoint_id(adapter)
                if adapter.config.provider == pin_provider or eid == pin_provider:
                    return adapter
            return None

        with self._lock:
            snapshot: list[tuple[BaseAdapter, float, _CircuitBreaker]] = []
            for adapter, weight in route.adapters:
                endpoint_id = _get_endpoint_id(adapter)
                cb = self._circuits.get(endpoint_id)
                if not cb:
                    cb = self._circuits[endpoint_id] = _CircuitBreaker(endpoint_id)
                snapshot.append((adapter, weight, cb))

        allowed: list[tuple[BaseAdapter, float]] = [
            (adapter, weight)
            for (adapter, weight, cb) in snapshot
            if weight > 0 and cb.allow_request()
        ]

        if not allowed:
            provider_names = [_get_endpoint_id(a) for a, _w, _cb in snapshot]
            raise AllCircuitsOpenError(
                f"All provider circuits are open for model {model_id}: {provider_names}"
            )

        affinity_key: str | None = None
        model_label = normalize_model_label(model_id)
        if AFFINITY_ENABLED:
            affinity_key = req_ctx.get().get("affinity_key") or None

        if affinity_key:
            now = time.monotonic()
            with self._lock:
                entry = self._affinity.get((affinity_key, model_id))
                if entry is not None and entry.expires_at > now:
                    for adapter, _w in allowed:
                        if _get_endpoint_id(adapter) == entry.endpoint_id:
                            entry.expires_at = now + AFFINITY_TTL_SECONDS
                            ROUTING_AFFINITY.labels(event="hit", model=model_label).inc()
                            return adapter
                    del self._affinity[(affinity_key, model_id)]
                    ROUTING_AFFINITY.labels(
                        event="dropped_unavailable", model=model_label
                    ).inc()
                elif entry is not None:
                    del self._affinity[(affinity_key, model_id)]
                    ROUTING_AFFINITY.labels(event="expired", model=model_label).inc()
                else:
                    ROUTING_AFFINITY.labels(event="miss", model=model_label).inc()

        total_allowed = sum(w for _, w in allowed)
        pool = (
            [(a, w / total_allowed) for a, w in allowed]
            if abs(total_allowed - 1.0) > 1e-9
            else allowed
        )

        rand = random.random()
        cumulative = 0.0
        chosen: BaseAdapter | None = None
        for adapter, weight in pool:
            cumulative += weight
            if rand <= cumulative:
                chosen = adapter
                break
        if chosen is None:
            chosen = pool[-1][0]

        if affinity_key:
            now = time.monotonic()
            with self._lock:
                self._affinity[(affinity_key, model_id)] = _Affinity(
                    endpoint_id=_get_endpoint_id(chosen),
                    expires_at=now + AFFINITY_TTL_SECONDS,
                )
                self._maybe_sweep_affinity_locked(now)
            ROUTING_AFFINITY.labels(event="created", model=model_label).inc()

        return chosen
```

- [ ] **Step 3.4: Run tests**

```bash
uv run pytest tests/unit/routing/test_session_affinity.py tests/unit/routing/test_executor.py -v
```

Expected: all session affinity tests pass; existing executor tests still pass.

- [ ] **Step 3.5: Commit**

```bash
git add apps/backend/routing/routers.py tests/unit/routing/test_session_affinity.py
git commit -m "feat(routing): consult and write affinity table in FixedRouter._select_adapter"
```

---

## Task 4: Drop affinity on primary error (chat + stream)

**Files:**
- Modify: `apps/backend/routing/routers.py` (`FixedRouter.chat_completion` ~line 693, `FixedRouter.stream_chat_completion` further down)
- Test: `tests/unit/routing/test_session_affinity.py`

- [ ] **Step 4.1: Add failing tests**

Append to `tests/unit/routing/test_session_affinity.py`:

```python
@pytest.mark.unit
def test_chat_completion_drops_affinity_on_primary_error():
    """Affinity entry is gone before fallback runs (regardless of fallback success)."""
    import asyncio

    r = FixedRouter()
    bad = _FailAdapter(_cfg("m", provider="BAD", base_url="http://BAD"))
    good = _EchoAdapter(_cfg("m", provider="GOOD", base_url="http://GOOD"))
    r.register_route("m", [(bad, 0.99), (good, 0.01)])

    req_ctx.set({"affinity_key": "u1"})
    # Pin to BAD so _select_adapter returns it deterministically via affinity.
    r._affinity[("u1", "m")] = _Affinity(
        endpoint_id=_get_endpoint_id_for(bad),
        expires_at=time.monotonic() + 60,
    )

    # Fallback (good) succeeds; entry must have been dropped before fallback ran.
    # If it had not been dropped, the post-success path would never write a new
    # entry (writes happen in _select_adapter, not in the fallback branch),
    # so we'd see the stale BAD-pinned entry survive.
    resp = asyncio.run(r.chat_completion("m", []))
    assert resp is not None
    assert ("u1", "m") not in r._affinity


@pytest.mark.unit
def test_chat_completion_drops_affinity_when_all_fail():
    """Affinity dropped even when no fallback is available."""
    import asyncio

    r = FixedRouter()
    bad = _FailAdapter(_cfg("m", provider="BAD"))
    r.register_route("m", [(bad, 1.0)])

    req_ctx.set({"affinity_key": "u1"})
    r._affinity[("u1", "m")] = _Affinity(
        endpoint_id=_get_endpoint_id_for(bad),
        expires_at=time.monotonic() + 60,
    )

    with pytest.raises(RuntimeError):
        asyncio.run(r.chat_completion("m", []))
    assert ("u1", "m") not in r._affinity


@pytest.mark.unit
def test_stream_chat_completion_drops_affinity_on_primary_error():
    import asyncio

    r = FixedRouter()
    bad = _FailAdapter(_cfg("m", provider="BAD"))
    r.register_route("m", [(bad, 1.0)])

    req_ctx.set({"affinity_key": "u1"})
    r._affinity[("u1", "m")] = _Affinity(
        endpoint_id=_get_endpoint_id_for(bad),
        expires_at=time.monotonic() + 60,
    )

    async def _consume():
        async for _ in r.stream_chat_completion("m", []):
            pass

    with pytest.raises(RuntimeError):
        asyncio.run(_consume())
    assert ("u1", "m") not in r._affinity


def _get_endpoint_id_for(adapter):
    """Mirror routers._get_endpoint_id without exposing the helper as public."""
    return getattr(adapter.config, "endpoint_id", None) or adapter.config.provider
```

Note on fallback semantics: `FixedRouter.chat_completion` iterates `route.adapters` directly and skips `weight <= 0` entries ([routers.py:751-755](../../apps/backend/routing/routers.py#L751)), so a weight-0 backup is never tried. Tests above use a small non-zero weight on the good adapter, or omit the backup entirely.

- [ ] **Step 4.2: Run tests to verify they fail**

```bash
uv run pytest tests/unit/routing/test_session_affinity.py::test_chat_completion_drops_affinity_on_primary_error tests/unit/routing/test_session_affinity.py::test_stream_chat_completion_drops_affinity_on_primary_error -v
```

Expected: assertion failure — entry still present after error.

- [ ] **Step 4.3: Add `_drop_affinity` calls to FixedRouter.chat_completion**

In `apps/backend/routing/routers.py`, find `FixedRouter.chat_completion` (around line 693). It contains a `try: ... except Exception as primary_error: self._on_failure(...)` block before the fallback loop. Add `self._drop_affinity(model_id)` immediately after the `_on_failure` call:

```python
            except Exception as primary_error:
                self._on_failure(_get_endpoint_id(primary), reason=primary_error.__class__.__name__)
                self._drop_affinity(model_id)
                fallback_adapters = self._get_fallback_adapters(model_id, primary)
                ...
```

Do the same in `FixedRouter.stream_chat_completion` (next method, around line 760-820). Find the matching `except Exception as primary_error:` block and add `self._drop_affinity(model_id)` immediately after `self._on_failure(...)`.

Note: the spec mentions `BaseRouter.chat_completion` as the place to patch. Verify which class actually has the live `chat_completion` (`BaseRouter` ~line 424 vs `FixedRouter` ~line 693). At the time of writing, `FixedRouter` overrides both, so the patch belongs in the override. If during implementation you find the BaseRouter version is actually invoked for chat (e.g. by `RouteWiseRouter`), patch both.

- [ ] **Step 4.4: Run tests**

```bash
uv run pytest tests/unit/routing/test_session_affinity.py -v
```

Expected: all session affinity tests pass.

- [ ] **Step 4.5: Verify wider routing test suite still passes**

```bash
uv run pytest tests/unit/routing/ -v
```

Expected: all green.

- [ ] **Step 4.6: Commit**

```bash
git add apps/backend/routing/routers.py tests/unit/routing/test_session_affinity.py
git commit -m "feat(routing): drop affinity on primary-call error in FixedRouter"
```

---

## Task 5: Push `affinity_key` into `req_ctx` from completions handler

**Files:**
- Modify: `apps/backend/serving/servers/routers/completions.py` (around line 338)
- Test: `tests/unit/serving/test_completions_affinity_key.py` (new)

- [ ] **Step 5.1: Inspect existing context-write block**

Read `apps/backend/serving/servers/routers/completions.py:330-345` to confirm the current `req_ctx.update({"auth_key_hash": ...})` line, the location of `get_client_ip` import, and the variable holding the FastAPI `Request` (likely `request`).

- [ ] **Step 5.2: Add failing test**

Create `tests/unit/serving/test_completions_affinity_key.py`:

```python
from __future__ import annotations

import pytest

from serving.utils import context as req_ctx


def _derive_affinity_key(auth_key_hash: str | None, client_ip: str) -> str:
    """Mirror of the production helper. If completions.py exports one, import it instead."""
    from serving.servers.routers.completions import derive_affinity_key

    return derive_affinity_key(auth_key_hash, client_ip)


@pytest.mark.unit
def test_authenticated_uses_auth_key_hash():
    assert _derive_affinity_key("abc123", "1.2.3.4") == "abc123"


@pytest.mark.unit
def test_anonymous_uses_ip_prefix():
    assert _derive_affinity_key(None, "1.2.3.4") == "ip:1.2.3.4"


@pytest.mark.unit
def test_anonymous_unknown_ip_falls_back():
    # When IP is "unknown" (the get_client_ip sentinel), still produce a stable key.
    assert _derive_affinity_key(None, "unknown") == "ip:unknown"
```

- [ ] **Step 5.3: Run tests to verify they fail**

```bash
uv run pytest tests/unit/serving/test_completions_affinity_key.py -v
```

Expected: ImportError for `derive_affinity_key`.

- [ ] **Step 5.4: Add the helper to completions.py**

In `apps/backend/serving/servers/routers/completions.py`, near the top of the file (after the imports), add:

```python
def derive_affinity_key(auth_key_hash: str | None, client_ip: str) -> str:
    """Compute the per-request affinity key used by FixedRouter.

    Authenticated users are keyed by their auth_key_hash; anonymous traffic
    by their client IP. The "ip:" prefix prevents collisions with hash values.
    """
    if auth_key_hash:
        return auth_key_hash
    return f"ip:{client_ip}"
```

- [ ] **Step 5.5: Use it in the request handler**

Find the existing block around line 338:

```python
req_ctx.update({"auth_key_hash": user_ctx.get("auth_key_hash") or "_anon"})
```

Change to:

```python
auth_key_hash = user_ctx.get("auth_key_hash")
affinity_key = derive_affinity_key(auth_key_hash, get_client_ip(request))
req_ctx.update(
    {
        "auth_key_hash": auth_key_hash or "_anon",
        "affinity_key": affinity_key,
    }
)
```

`get_client_ip` is already imported at [completions.py:40](../../apps/backend/serving/servers/routers/completions.py#L40).

- [ ] **Step 5.6: Run tests**

```bash
uv run pytest tests/unit/serving/test_completions_affinity_key.py -v
```

Expected: all 3 tests pass.

- [ ] **Step 5.7: Commit**

```bash
git add apps/backend/serving/servers/routers/completions.py tests/unit/serving/test_completions_affinity_key.py
git commit -m "feat(serving): populate req_ctx affinity_key for router stickiness"
```

---

## Task 6: Concurrency test

**Files:**
- Test: `tests/unit/routing/test_session_affinity.py`

The race window between the lock-released weighted-pick and the lock-acquired affinity write is benign (entries are functionally equivalent). Pin this with a test so future refactors don't break it.

- [ ] **Step 6.1: Add concurrency test**

Append to `tests/unit/routing/test_session_affinity.py`:

```python
@pytest.mark.unit
def test_concurrent_acquires_yield_one_entry():
    import threading

    r = FixedRouter()
    a = _EchoAdapter(_cfg("m", provider="A"))
    b = _EchoAdapter(_cfg("m", provider="B"))
    r.register_route("m", [(a, 0.5), (b, 0.5)])

    chosen: list[BaseAdapter] = []
    barrier = threading.Barrier(20)

    def _worker() -> None:
        req_ctx.set({"affinity_key": "u_race"})
        barrier.wait()
        sel = r._select_adapter("m")
        if sel is not None:
            chosen.append(sel)

    threads = [threading.Thread(target=_worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Exactly one entry survives; all picks are valid adapters.
    assert ("u_race", "m") in r._affinity
    assert all(c in {a, b} for c in chosen)
```

Note: `BaseAdapter` import — add to the existing imports at top of file: `from serving.adapters.base import BaseAdapter, ModelConfig` (already present).

- [ ] **Step 6.2: Run test**

```bash
uv run pytest tests/unit/routing/test_session_affinity.py::test_concurrent_acquires_yield_one_entry -v
```

Expected: pass. If the test flakes, the race is real and harmful — investigate before continuing.

- [ ] **Step 6.3: Commit**

```bash
git add tests/unit/routing/test_session_affinity.py
git commit -m "test(routing): cover concurrent acquires on session affinity"
```

---

## Task 7: Documentation

**Files:**
- Modify: `docs/developer/routing.md`

- [ ] **Step 7.1: Confirm the file exists and find the natural section**

```bash
test -f docs/developer/routing.md && head -40 docs/developer/routing.md
```

- [ ] **Step 7.2: Add session-affinity section**

Append a new top-level section (or insert near the existing routing-strategy discussion) titled `## Session affinity` with this content:

```markdown
## Session affinity

`FixedRouter` keeps a per-(user, model) pin to the last-selected provider for
five minutes (sliding TTL). Goals:

- Keep one conversation on one backend so prompt caches stay warm and latency
  stays consistent.
- Drop the pin the moment that backend errors, so users don't get stuck on a
  failing provider.

**Affinity key:**
- Authenticated requests: the user's `auth_key_hash`.
- Anonymous requests: `f"ip:{client_ip}"`.

**Pin lifecycle:**
1. First request from `(key, model)` → weighted random pick → entry stored.
2. Subsequent requests within 300 s on the same `(key, model)` reuse the same
   endpoint and refresh the TTL.
3. Any exception from the pinned provider drops the entry; fallback runs;
   the next request creates a fresh pin.
4. If the pinned endpoint is no longer in the allowed pool (weight-0 in
   `routing.yaml` or its circuit is open), the entry is dropped and a fresh
   weighted-random pick runs.
5. After 300 s of inactivity the entry expires.

**Scope and limits:**
- State is in-process. Each Uvicorn worker tracks its own table. Same as
  `key_pool.py`.
- Affinity does not survive a restart.
- `pin_provider` (admin override via `X-Route-Pin`) bypasses affinity.

**Kill switch:** set `ROUTING_AFFINITY_ENABLED=0` to disable.

**Metrics:** `routing_affinity_events_total{event,model}` with events
`hit | miss | created | expired | dropped_error | dropped_unavailable`.
```

- [ ] **Step 7.3: Commit**

```bash
git add docs/developer/routing.md
git commit -m "docs(routing): document session affinity behavior"
```

---

## Task 8: Final verification

- [ ] **Step 8.1: Format**

```bash
make format
```

- [ ] **Step 8.2: Lint**

```bash
make lint
```

Expected: clean.

- [ ] **Step 8.3: Default test suite**

```bash
make test
```

Expected: all green. Particular attention to `tests/unit/routing/`.

- [ ] **Step 8.4: Manual smoke against local dev server (optional)**

Start the gateway, send three requests with the same API key against a multi-provider model, confirm logs show the same `provider` for all three. Then either disable that provider in `routing.yaml` or restart with `ROUTING_AFFINITY_ENABLED=0` to verify the fallback path.

- [ ] **Step 8.5: Commit any format/lint touch-ups**

```bash
git status
# If anything changed:
git add -A
git commit -m "style: format/lint after session-affinity changes"
```

- [ ] **Step 8.6: Push and open PR**

```bash
git push -u origin juncheng/claude/router-session-affinity
gh pr create --base dev --title "feat(routing): per-user provider session affinity" --body "$(cat <<'EOF'
## Summary
- Adds 5-minute sliding session affinity in `FixedRouter`: same `(auth_key_hash or client IP, model_id)` → same provider.
- Affinity drops on any primary error (fallback then runs as today) or when the pinned endpoint leaves the allowed pool.
- New env kill switch `ROUTING_AFFINITY_ENABLED=0`. New metric `routing_affinity_events_total`.

Spec: `docs/agents/specs/2026-05-04-router-session-affinity-design.md`
Plan: `docs/agents/plans/2026-05-04-router-session-affinity.md`

## Test plan
- [ ] `make test` green
- [ ] Manual: send 3 requests with same API key to a multi-provider model on staging; confirm same provider in logs
- [ ] Manual: open the pinned provider's circuit (or weight-0 it), verify next request re-picks
- [ ] Manual: `ROUTING_AFFINITY_ENABLED=0` reverts to weighted-random behavior

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```
