# Tracked Fire-and-Forget Tasks — Design

**Date:** 2026-05-03
**Status:** Draft → ready for plan
**Author:** Architecture review follow-up (issue #4 of 6)

## Problem

Multiple fire-and-forget side effects across the gateway are scheduled with `asyncio.ensure_future(...)` (or equivalent) and have no completion observability:

- **Cost increment** — [apps/backend/serving/servers/routers/completions.py:75-100](../../../apps/backend/serving/servers/routers/completions.py#L75) (will live in `CompletionsCost.CostTracker._increment` after issue #2 lands).
- **Request log** — [apps/backend/serving/servers/routers/completions.py:47-73](../../../apps/backend/serving/servers/routers/completions.py#L47) (will live in `CompletionsLogger._write`).
- **D1 dual-write shadow** — [apps/backend/serving/storage/dual_write.py:61-76](../../../apps/backend/serving/storage/dual_write.py#L61).

If a task fails or never runs to completion (DB slow, user disconnects mid-stream), the failure is logged but no metric, no alert, no rate visibility. Operators learn about billing gaps and audit-log gaps from user complaints, not from monitoring.

A separate but related issue: [`RouteWiseRouter._pending_decisions`](../../../apps/backend/routing/routewise/router.py) is a dict keyed by `request_id` that's populated when a request is routed and consumed when `record_observation(...)` is called. If the call never comes (request abort, timeout, code-path bug), entries leak. No size cap, no TTL, no eviction.

## Goals

1. Introduce a `tracked_task(coro, *, name)` helper that schedules a fire-and-forget coroutine and emits a structured log event on success or failure.
2. Add a new alert rule (`TrackedTaskFailureRateRule`) that consumes those events and alerts when per-task-name failure rate exceeds threshold.
3. Migrate the three flagged call-sites (cost, request log, D1 shadow) to use the helper.
4. Add a periodic TTL-based cleanup of `_pending_decisions` plus a leak-detection alert rule.
5. Land in 2 PRs: PR 1 covers helper + 3 call-sites + failure-rate rule; PR 2 covers pending-decisions TTL + leak rule.

## Non-goals

- Auditing every `asyncio.ensure_future` / `asyncio.create_task` call in the codebase. Only the three flagged tasks land in PR 1; future fire-and-forget work should use `tracked_task(...)` by default but legacy migrations are out of scope.
- Building a full task queue / Celery / Sidekiq replacement. The helper stays a thin wrapper over `asyncio.ensure_future`.
- Latency-on-task alerts. Duration is in the event payload, but no rule reads it in v1.
- Backpressure (rejecting work when overloaded). Tasks always run to completion; only their *log records* flow through the bounded `AlertingLogHandler` queue.
- D1 dual-write shadow architecture changes. Only the observability of its async writes changes.

## Architecture

### Approach: structured-log events into the existing rule engine

The PR #372 alerting framework already has the machinery we need:

- A `logging.Handler` (`AlertingLogHandler`) that captures structured log records.
- An `AlertEngine` that drains the handler's queue and runs rolling-window rules.
- A single `alert_slack(...)` sink with cooldown/dedupe.

`tracked_task` plugs into this by emitting `tracked_task_completed` log records at completion. A new rule reads them, applies a per-task-name failure threshold, and fires `alert_slack(...)` on sustained crossings. No new alerting plumbing.

### `tracked_task` helper

New module: `apps/backend/serving/observability/tracked_tasks.py`.

```python
"""Tracked fire-and-forget task scheduler.

Wraps asyncio.ensure_future to emit a structured ``tracked_task_completed``
log event on success or failure. Consumed by TrackedTaskFailureRateRule
in serving.observability.alert_rules.

Use this for any background work where the caller doesn't await the
result (DB writes, telemetry, dual-write shadows). The naked
``asyncio.ensure_future`` / ``asyncio.create_task`` patterns are
permitted only for tasks whose completion is otherwise observable
(e.g., the AlertEngine drain task).
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable

log = logging.getLogger(__name__)

_TRACKED_TASKS: set[asyncio.Task[None]] = set()


def tracked_task(coro: Awaitable[None], *, name: str) -> asyncio.Task[None]:
    """Schedule ``coro`` and emit a tracked_task_completed log event when done.

    The ``name`` becomes the dimension key for the alert rule —
    use a short, stable identifier (e.g., "request_log", "cost_increment",
    "dual_write_shadow"). Returns the wrapping task; callers normally
    discard the return value.
    """
    async def _runner() -> None:
        start = time.monotonic()
        try:
            await coro
            try:
                log.info(
                    "tracked_task_completed",
                    extra={
                        "event": "tracked_task_completed",
                        "task_name": name,
                        "success": True,
                        "duration_ms": int((time.monotonic() - start) * 1000),
                    },
                )
            except Exception:
                pass  # never let logging itself escape
        except Exception as e:
            try:
                log.warning(
                    "tracked_task_completed",
                    extra={
                        "event": "tracked_task_completed",
                        "task_name": name,
                        "success": False,
                        "duration_ms": int((time.monotonic() - start) * 1000),
                        "error": str(e)[:200],
                        "error_type": type(e).__name__,
                    },
                    exc_info=False,
                )
            except Exception:
                pass

    task = asyncio.ensure_future(_runner())
    _TRACKED_TASKS.add(task)
    task.add_done_callback(_TRACKED_TASKS.discard)
    return task
```

The module-level `_TRACKED_TASKS` set keeps tasks alive until completion (asyncio holds only weak refs to scheduled tasks; same GC-survival pattern PR #372 uses).

### Call-site replacements

| Module (post-#2) | Before | After |
|---|---|---|
| `apps/backend/serving/servers/routers/completions_logging.py` | `task = asyncio.ensure_future(self._write(payload))` + `_LOG_TASKS.add(task)` + `task.add_done_callback(_LOG_TASKS.discard)` | `tracked_task(self._write(payload), name="request_log")` |
| `apps/backend/serving/servers/routers/completions_cost.py` | `task = asyncio.ensure_future(self._increment(...))` + `_COST_TASKS.add(task)` + `task.add_done_callback(_COST_TASKS.discard)` | `tracked_task(self._increment(...), name="cost_increment")` |
| `apps/backend/serving/storage/dual_write.py` (around lines 61-76) | the existing shadow-write scheduling | `tracked_task(self._shadow_write_op(...), name="dual_write_shadow")` |

The `_LOG_TASKS` and `_COST_TASKS` sets become redundant and are deleted.

### New alert rule: `TrackedTaskFailureRateRule`

Added to `apps/backend/serving/observability/alert_rules.py`.

```python
class TrackedTaskFailureRateRule:
    """Alert when a tracked task type fails at a sustained rate."""

    name = "tracked_task_failure_rate"

    def __init__(self, cfg: TrackedTaskFailureRateConfig) -> None:
        self._cfg = cfg
        self._windows: dict[str, _SlidingWindow] = {}

    async def on_record(self, record: logging.LogRecord) -> None:
        """Update the per-task-name window from a tracked_task_completed event."""
        if not self._cfg.enabled:
            return
        if getattr(record, "event", None) != "tracked_task_completed":
            return
        task_name = getattr(record, "task_name", None) or "unknown"
        success = bool(getattr(record, "success", True))
        win = self._windows.setdefault(task_name, _SlidingWindow(self._cfg.window_sec))
        now = time.time()
        win.add(now, {"success": success})
        items = win.items(now)
        if len(items) < self._cfg.min_samples:
            return
        failed = sum(1 for it in items if not it["success"])
        pct = (failed / len(items)) * 100.0
        if pct < self._cfg.threshold_pct:
            return
        await alert_slack(
            AlertSeverity.ERROR,
            f"Tracked-task failure rate exceeded for {task_name}",
            {
                "task_name": task_name,
                "rate": f"{pct:.1f}% ({failed} of {len(items)} tasks, last {self._cfg.window_sec}s)",
            },
            dedupe_key=f"tracked_task_failure:{task_name}",
            cooldown_sec=self._cfg.cooldown_sec,
        )
```

Registered in `AlertEngine._build_rules`. Per-task-name dimension; one alert can fire for `request_log`, another for `cost_increment`, etc.

### Config addition (`config/alerts.yaml`)

```yaml
rules:
  # ... existing rules from PR #372 ...
  tracked_task_failure_rate:
    enabled: true
    window_sec: 300       # 5 min
    threshold_pct: 5.0    # 5% failure rate
    min_samples: 50       # need at least 50 completions before alerting
    cooldown_sec: 1800    # 30 min per task_name
```

### PR 2: Pending-decisions TTL + leak rule

Different code path; same alert pattern.

**Modify** `apps/backend/routing/routewise/router.py`:

- Add a periodic cleanup task to `RouteWiseRouter`. Started in `__init__`/`start()`; cancelled in `stop()`.
- Cleanup runs every 60 s. For each `(request_id, decision)` in `_pending_decisions`, evict if `decision.timestamp` is older than `TTL_SECONDS = 300` (5 min).
- For each evicted entry, emit a `routewise_decision_evicted` log event with `extra={"event": "routewise_decision_evicted", "request_id": request_id, "age_sec": int(...)}`.
- Use an `asyncio.Lock` around dict access to avoid races with concurrent `_select_adapter` and `record_observation` calls.

**New rule** in `apps/backend/serving/observability/alert_rules.py`:

```python
class PendingDecisionsLeakRule:
    """Alert when RouteWise pending-decision evictions exceed threshold (sustained leak)."""

    name = "pending_decisions_leak"

    def __init__(self, cfg: PendingDecisionsLeakConfig) -> None:
        self._cfg = cfg
        self._window = _SlidingWindow(cfg.window_sec)

    async def on_record(self, record: logging.LogRecord) -> None:
        """Update the count window from a routewise_decision_evicted event."""
        if not self._cfg.enabled:
            return
        if getattr(record, "event", None) != "routewise_decision_evicted":
            return
        now = time.time()
        self._window.add(now, {})
        items = self._window.items(now)
        if len(items) <= self._cfg.threshold_count:
            return
        await alert_slack(
            AlertSeverity.WARN,
            "RouteWise pending-decisions leaking",
            {
                "evicted_count": len(items),
                "window_sec": self._cfg.window_sec,
            },
            dedupe_key="pending_decisions_leak",
            cooldown_sec=self._cfg.cooldown_sec,
        )
```

**Config addition (PR 2):**

```yaml
rules:
  pending_decisions_leak:
    enabled: true
    window_sec: 600       # 10 min
    threshold_count: 20   # >20 evictions in 10 min indicates a real leak
    cooldown_sec: 3600    # 1 h
```

## PR breakdown

### PR 1 — `tracked_task` helper + 3 call-sites + failure-rate rule

**Pre-conditions:** PR #372 merged. Issue #2 PRs A + B merged (call-sites live in `CompletionsLogger` and `CostTracker`). If #2 lags, swaps target the older `completions.py` helpers.

**New files:**
- `apps/backend/serving/observability/tracked_tasks.py` — the helper.
- `tests/unit/observability/test_tracked_tasks.py` — helper tests.

**Modified:**
- `apps/backend/serving/observability/alert_rules.py` — add `TrackedTaskFailureRateRule`; register in `AlertEngine._build_rules`.
- `apps/backend/serving/observability/alert_config.py` — add `TrackedTaskFailureRateConfig` Pydantic model + add it to `Rules`.
- `config/alerts.yaml` — defaults for the new rule.
- `apps/backend/serving/servers/routers/completions_logging.py` — call-site swap; drop `_LOG_TASKS`.
- `apps/backend/serving/servers/routers/completions_cost.py` — call-site swap; drop `_COST_TASKS`.
- `apps/backend/serving/storage/dual_write.py` — wrap shadow write with `tracked_task(...)`.

**New tests:**
- `test_tracked_task_emits_success_event` — task completes; assert log record with `event=tracked_task_completed`, `success=True`, `duration_ms` present.
- `test_tracked_task_emits_failure_event` — coro raises; assert `success=False`, error type captured, no exception escapes.
- `test_tracked_task_gc_safety` — drop external reference, force `gc.collect()`, assert task still completes.
- `test_failure_rate_rule_fires_per_task_name` — 100 records for `request_log` (10 fail), 100 for `cost_increment` (1 fails); assert rule fires once for `request_log`, not for `cost_increment`.
- Update existing `test_completions_logging.py` / `test_completions_cost.py` if they referenced the dropped sets.

**Approx diff:** +250 / -30 lines.
**Risk:** low. Thin-wrapper change; no behavior of the underlying coroutines changes.

### PR 2 — RouteWise pending-decisions TTL + leak rule

**Pre-conditions:** PR 1 merged.

**Modified:**
- `apps/backend/routing/routewise/router.py` — periodic cleanup task; emit `routewise_decision_evicted`.
- `apps/backend/serving/observability/alert_rules.py` — add `PendingDecisionsLeakRule`; register.
- `apps/backend/serving/observability/alert_config.py` — add config Pydantic model; add to `Rules`.
- `config/alerts.yaml` — defaults.

**New tests:**
- `test_pending_decisions_evicted_after_ttl` — insert decision; advance time by TTL+1; trigger sweep; assert evicted + log event emitted.
- `test_pending_decisions_no_eviction_within_ttl` — insert; sweep before TTL; assert still present, no event.
- `test_leak_rule_fires_at_threshold` — emit 21 eviction events in 10 min; threshold 20; assert alert.

**Approx diff:** +180 / -10 lines.
**Risk:** medium. Concurrent dict access requires a lock; existing routing tests should catch regressions.

## Testing strategy

| Layer | What | Per PR |
|---|---|---|
| Unit | `tracked_task` helper + new rules | 1, 2 |
| Integration | E2E: structured-log event flows through `AlertingLogHandler` → `AlertEngine` → `alert_slack` (extend existing framework e2e test) | 1 |
| Production smoke (post-merge) | Confirm in staging logs that `tracked_task_completed` events appear and the rule never fires (no real failures expected) | 1 |

## Risk + rollback

| Risk | Mitigation |
|---|---|
| `tracked_task` accidentally double-wraps a coro | Defensive double-wrap is a no-op (just adds two log events); helper docstring says "wraps once at the call site". |
| Helper changes timing of fire-and-forget calls | Negligible: <1 ms wrapper overhead. |
| Failure event spam if a task type is failing constantly | `cooldown_sec=1800` (30 min) bounds Slack noise; `min_samples=50` prevents alert from a single transient failure. |
| Logging itself raises inside the wrapper | Outer `try/except` catches; nothing escapes the runner. |
| Pending-decisions cleanup contends with hot routing path | `asyncio.Lock` only around dict access; held for ~µs per access. |
| TTL too short — real long requests get evicted | 5 min is generous (typical request <30 s). Configurable if a future model legitimately runs >5 min. |

**Rollback:** each PR is a single `git revert` away. Both are additive; reverting restores the prior (working but unobservable) behavior.

## Open questions (resolved)

| Question | Resolution |
|---|---|
| Bounded queue / drop-oldest semantics on `tracked_task`? | No — bounded queue exists at the log layer (`AlertingLogHandler`). Tasks themselves always run to completion. |
| Track latency too? | Not in v1. Duration is in the event payload; a `TrackedTaskLatencyRule` is a future addition if needed. |
| What if `_runner` itself raises? | Defensive `try/except` around the inner logging calls. |
| Migrate every `asyncio.ensure_future` in the codebase? | No — only the three flagged. Future code should default to `tracked_task(...)`; legacy is best-effort cleanup. |

## Out-of-scope follow-ups

- Latency-on-tracked-task alert rule.
- Auditing the codebase for naked `asyncio.ensure_future` / `asyncio.create_task` and migrating them.
- D1 dual-write architecture (separate decisions, separate brainstorm if D1 ever becomes primary).
- Decompose [apps/backend/serving/servers/routers/completions.py](../../../apps/backend/serving/servers/routers/completions.py) (issue #2 — spec exists).
- Schema migrations (issue #3 — spec exists).
- Decompose admin page (issue #5).
- Routing config expressiveness (issue #6).
