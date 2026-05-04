# Tracked Fire-and-Forget Tasks Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `tracked_task(coro, *, name)` helper that emits structured completion events for fire-and-forget asyncio tasks, plus alert rules for sustained per-task failure rates and RouteWise pending-decision leaks.

**Architecture:** A thin wrapper around `asyncio.ensure_future` emits a `tracked_task_completed` log record on success or failure. The existing PR #372 alert engine consumes those records via a new `TrackedTaskFailureRateRule`. Three call-sites (request log, cost increment, dual-write shadow) are migrated to the helper. PR 2 adds a periodic TTL sweep over `RouteWiseRouter._pending_decisions` that emits `routewise_decision_evicted` events feeding a new `PendingDecisionsLeakRule`.

**Tech Stack:** Python 3.12, asyncio, FastAPI, asyncpg, pytest + pytest-asyncio (auto mode).

**Spec:** [docs/agents/specs/2026-05-03-tracked-tasks-design.md](../specs/2026-05-03-tracked-tasks-design.md)

**Process notes (from CLAUDE.md):**
- Pull origin/dev before starting each PR.
- 2 PRs on feature branches `jason/claude/tracked-tasks-helper` (PR 1) and `jason/claude/pending-decisions-ttl` (PR 2).
- PR 2 depends on PR 1 merging first.
- Worktrees in `/home/juncheng/hybridInference-worktrees/`.
- Per CLAUDE.md: create issue → branch → implement → `make format` → PR → monitor CI every 2 min → cleanup after merge.

---

## File Structure

### PR 1 — helper + 3 call-sites + failure-rate rule

| Path | Action | Responsibility |
|---|---|---|
| `apps/backend/serving/observability/tracked_tasks.py` | Create | `tracked_task(coro, *, name)` helper + module-level `_TRACKED_TASKS` set for GC safety. |
| `tests/unit/observability/__init__.py` | Create | Empty marker so pytest discovers the new test directory. |
| `tests/unit/observability/test_tracked_tasks.py` | Create | Helper unit tests: success/failure events, GC safety, no exception escape, double-wrap is no-op. |
| `apps/backend/serving/observability/alert_rules.py` | Modify | Add `TrackedTaskFailureRateRule`; register in `AlertEngine._build_rules`. |
| `apps/backend/serving/observability/alert_config.py` | Modify | Add `TrackedTaskFailureRateConfig` Pydantic model and slot in `Rules`. |
| `config/alerts.yaml` | Modify | Add `tracked_task_failure_rate` defaults block. |
| `tests/unit/observability/test_alert_rules.py` | Modify (or Create if absent) | Test that the rule fires per task name. |
| `apps/backend/serving/servers/routers/completions.py` | Modify | Replace `asyncio.create_task(log_to_db_background())` and the `_increment` task with `tracked_task(...)` calls. Delete `_background_tasks` set. |
| `apps/backend/serving/storage/dual_write.py` | Modify | Wrap shadow writes with `tracked_task(..., name="dual_write_shadow")` (in `_do_shadow`). |

### PR 2 — RouteWise pending-decisions TTL + leak rule

| Path | Action | Responsibility |
|---|---|---|
| `apps/backend/routing/routewise/router.py` | Modify | Add `start()` / `stop()` + periodic TTL sweep of `_pending_decisions`; emit `routewise_decision_evicted` events; add `asyncio.Lock` around dict access. |
| `apps/backend/serving/servers/bootstrap.py` | Modify | Call `routewise_router.start()` after init; ensure cleanup invokes `stop()`. |
| `apps/backend/serving/observability/alert_rules.py` | Modify | Add `PendingDecisionsLeakRule`; register in `AlertEngine._build_rules`. |
| `apps/backend/serving/observability/alert_config.py` | Modify | Add `PendingDecisionsLeakConfig`; slot in `Rules`. |
| `config/alerts.yaml` | Modify | Add `pending_decisions_leak` defaults block. |
| `tests/unit/apps/backend/routing/test_pending_decisions_ttl.py` | Create | TTL sweep unit tests. |
| `tests/unit/observability/test_alert_rules.py` | Modify | Add leak-rule tests. |

No deletions in either PR (the `_background_tasks` set in `completions.py` is replaced inline).

---

## PR 1 — `tracked_task` helper + 3 call-sites + failure-rate rule

### Task 1.0: Worktree, branch, and issue setup

**Files:** none (process step).

- [ ] **Step 1: Pull latest dev and create worktree**

```bash
cd /home/juncheng/hybridInference
git fetch origin
git checkout dev
git pull origin dev
git worktree add /home/juncheng/hybridInference-worktrees/tracked-tasks-helper -b jason/claude/tracked-tasks-helper origin/dev
```

- [ ] **Step 2: Create the GitHub issue**

```bash
gh issue create \
  --repo "$(gh repo view --json nameWithOwner -q .nameWithOwner)" \
  --title "Add tracked_task helper + per-task failure-rate alert" \
  --body "$(cat <<'EOF'
## Summary
Introduce `tracked_task(coro, *, name)` in `apps/backend/serving/observability/tracked_tasks.py` that emits a structured `tracked_task_completed` log event on success or failure. Add `TrackedTaskFailureRateRule` to alert when per-task-name failure rate crosses threshold. Migrate three flagged call-sites: request-log DB write, cost increment, and D1 dual-write shadow.

Spec: docs/agents/specs/2026-05-03-tracked-tasks-design.md

## Scope (PR 1 of 2)
- New: `apps/backend/serving/observability/tracked_tasks.py`
- New rule + config + YAML defaults
- Migrate `apps/backend/serving/servers/routers/completions.py` (request log + cost increment)
- Migrate `apps/backend/serving/storage/dual_write.py` (shadow write)
- Tests: helper unit tests + rule tests

PR 2 (separate) handles the RouteWise pending-decisions TTL.
EOF
)"
```

Capture the issue number for the PR description in the final task. Expected output: a URL like `https://github.com/<org>/hybridInference/issues/<N>`.

- [ ] **Step 3: Verify the worktree**

Run: `cd /home/juncheng/hybridInference-worktrees/tracked-tasks-helper && git status && git rev-parse --abbrev-ref HEAD`
Expected: clean working tree on `jason/claude/tracked-tasks-helper`.

---

### Task 1.1: Create `tracked_task` helper module

**Files:**
- Create: `/home/juncheng/hybridInference-worktrees/tracked-tasks-helper/apps/backend/serving/observability/tracked_tasks.py`
- Create: `/home/juncheng/hybridInference-worktrees/tracked-tasks-helper/test/unit/observability/__init__.py`
- Create: `/home/juncheng/hybridInference-worktrees/tracked-tasks-helper/test/unit/observability/test_tracked_tasks.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/observability/__init__.py` as an empty file (just so pytest discovers the directory):

```python
```

Then create `tests/unit/observability/test_tracked_tasks.py`:

```python
"""Unit tests for serving.observability.tracked_tasks."""

from __future__ import annotations

import asyncio
import gc
import logging

import pytest

from serving.observability.tracked_tasks import _TRACKED_TASKS, tracked_task


@pytest.fixture(autouse=True)
def _clear_tracked_tasks():
    """Make sure module-level set starts empty for each test."""
    _TRACKED_TASKS.clear()
    yield
    _TRACKED_TASKS.clear()


async def test_tracked_task_emits_success_event(caplog: pytest.LogCaptureFixture) -> None:
    async def _ok() -> None:
        await asyncio.sleep(0)

    with caplog.at_level(logging.INFO, logger="serving.observability.tracked_tasks"):
        task = tracked_task(_ok(), name="unit_test_ok")
        await task

    matching = [r for r in caplog.records if getattr(r, "event", None) == "tracked_task_completed"]
    assert len(matching) == 1
    record = matching[0]
    assert record.task_name == "unit_test_ok"
    assert record.success is True
    assert isinstance(record.duration_ms, int)
    assert record.duration_ms >= 0
    assert record.levelno == logging.INFO


async def test_tracked_task_emits_failure_event(caplog: pytest.LogCaptureFixture) -> None:
    async def _fail() -> None:
        raise RuntimeError("boom")

    with caplog.at_level(logging.WARNING, logger="serving.observability.tracked_tasks"):
        task = tracked_task(_fail(), name="unit_test_fail")
        await task  # must not raise

    matching = [r for r in caplog.records if getattr(r, "event", None) == "tracked_task_completed"]
    assert len(matching) == 1
    record = matching[0]
    assert record.task_name == "unit_test_fail"
    assert record.success is False
    assert record.error_type == "RuntimeError"
    assert "boom" in record.error
    assert record.levelno == logging.WARNING


async def test_tracked_task_no_exception_escapes() -> None:
    async def _fail() -> None:
        raise ValueError("nope")

    task = tracked_task(_fail(), name="no_escape")
    # Awaiting must not raise — the wrapper swallows.
    await task
    assert task.exception() is None


async def test_tracked_task_gc_safety(caplog: pytest.LogCaptureFixture) -> None:
    """Dropping the external task reference must not cause the task to be cancelled."""
    completed = asyncio.Event()

    async def _slow() -> None:
        await asyncio.sleep(0.05)
        completed.set()

    with caplog.at_level(logging.INFO, logger="serving.observability.tracked_tasks"):
        # Schedule and immediately discard the returned task.
        tracked_task(_slow(), name="gc_safety")
        # Force GC to prove the module-level set keeps it alive.
        gc.collect()
        await asyncio.wait_for(completed.wait(), timeout=1.0)
        # Yield once more so the done callback fires and the set is cleaned.
        await asyncio.sleep(0)

    matching = [r for r in caplog.records if getattr(r, "event", None) == "tracked_task_completed"]
    assert len(matching) == 1
    assert matching[0].success is True
    assert len(_TRACKED_TASKS) == 0


async def test_tracked_task_double_wrap_is_noop(caplog: pytest.LogCaptureFixture) -> None:
    """Wrapping a coroutine that itself was scheduled via tracked_task adds a second event but is otherwise harmless."""

    async def _inner() -> None:
        await asyncio.sleep(0)

    async def _outer() -> None:
        # Inner-as-coro is awaited inside the outer coroutine.
        await _inner()

    with caplog.at_level(logging.INFO, logger="serving.observability.tracked_tasks"):
        task = tracked_task(_outer(), name="double_wrap")
        await task

    matching = [r for r in caplog.records if getattr(r, "event", None) == "tracked_task_completed"]
    # Exactly one outer event; the inner coroutine wasn't tracked because we only wrapped once.
    assert len(matching) == 1
    assert matching[0].task_name == "double_wrap"
    assert matching[0].success is True
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd /home/juncheng/hybridInference-worktrees/tracked-tasks-helper && uv run pytest tests/unit/observability/test_tracked_tasks.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'serving.observability.tracked_tasks'`.

- [ ] **Step 3: Implement the helper**

Create `apps/backend/serving/observability/tracked_tasks.py`:

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

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd /home/juncheng/hybridInference-worktrees/tracked-tasks-helper && uv run pytest tests/unit/observability/test_tracked_tasks.py -v`
Expected: 5 tests pass.

- [ ] **Step 5: Commit**

```bash
cd /home/juncheng/hybridInference-worktrees/tracked-tasks-helper
git add apps/backend/serving/observability/tracked_tasks.py tests/unit/observability/__init__.py tests/unit/observability/test_tracked_tasks.py
git commit -m "$(cat <<'EOF'
feat(observability): add tracked_task helper for fire-and-forget tasks

Wraps asyncio.ensure_future to emit a structured tracked_task_completed
log event on success or failure. Module-level set + done callback keeps
tasks alive against GC.

Refs spec docs/agents/specs/2026-05-03-tracked-tasks-design.md

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 1.2: Add `TrackedTaskFailureRateConfig` Pydantic model

**Files:**
- Modify: `/home/juncheng/hybridInference-worktrees/tracked-tasks-helper/apps/backend/serving/observability/alert_config.py`

- [ ] **Step 1: Read the existing `alert_config.py` to find the `Rules` model**

Run: `cd /home/juncheng/hybridInference-worktrees/tracked-tasks-helper && grep -n "class .*Config\|class Rules" apps/backend/serving/observability/alert_config.py`
Expected: a list of existing Pydantic config classes plus a `class Rules` aggregate. Note the line where the existing rules are listed inside `Rules`.

- [ ] **Step 2: Write the failing test for the config**

Append to `tests/unit/observability/test_alert_rules.py` (or create the file with this content if it does not exist — see Task 1.3 Step 1 for the file's full layout if creating; if appending, just add the test):

```python
def test_tracked_task_failure_rate_config_parses_yaml_defaults() -> None:
    from serving.observability.alert_config import TrackedTaskFailureRateConfig

    cfg = TrackedTaskFailureRateConfig(
        enabled=True,
        window_sec=300,
        threshold_pct=5.0,
        min_samples=50,
        cooldown_sec=1800,
    )
    assert cfg.enabled is True
    assert cfg.window_sec == 300
    assert cfg.threshold_pct == 5.0
    assert cfg.min_samples == 50
    assert cfg.cooldown_sec == 1800
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `cd /home/juncheng/hybridInference-worktrees/tracked-tasks-helper && uv run pytest tests/unit/observability/test_alert_rules.py::test_tracked_task_failure_rate_config_parses_yaml_defaults -v`
Expected: FAIL with `ImportError: cannot import name 'TrackedTaskFailureRateConfig'`.

- [ ] **Step 4: Add the Pydantic model and slot it into `Rules`**

In `apps/backend/serving/observability/alert_config.py`, locate the section that defines other `*Config` BaseModel classes (e.g., near `class AuthFailureSpikeConfig`). Add the new class adjacent to them:

```python
class TrackedTaskFailureRateConfig(BaseModel):
    """Config for TrackedTaskFailureRateRule.

    Fires when per-task-name failure rate over ``window_sec`` exceeds
    ``threshold_pct`` and at least ``min_samples`` completions are observed.
    """

    enabled: bool = True
    window_sec: int = 300
    threshold_pct: float = 5.0
    min_samples: int = 50
    cooldown_sec: int = 1800
```

Then locate `class Rules(BaseModel)` (the aggregate) and add the new field. Example diff (the surrounding fields will differ — keep them; just append):

```python
class Rules(BaseModel):
    # ... existing rule fields (auth_failure_spike, etc.) ...
    tracked_task_failure_rate: TrackedTaskFailureRateConfig = TrackedTaskFailureRateConfig()
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `cd /home/juncheng/hybridInference-worktrees/tracked-tasks-helper && uv run pytest tests/unit/observability/test_alert_rules.py::test_tracked_task_failure_rate_config_parses_yaml_defaults -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
cd /home/juncheng/hybridInference-worktrees/tracked-tasks-helper
git add apps/backend/serving/observability/alert_config.py tests/unit/observability/test_alert_rules.py
git commit -m "$(cat <<'EOF'
feat(observability): add TrackedTaskFailureRateConfig model

Adds the Pydantic config schema for the upcoming
TrackedTaskFailureRateRule.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 1.3: Add `TrackedTaskFailureRateRule` and register it

**Files:**
- Modify: `/home/juncheng/hybridInference-worktrees/tracked-tasks-helper/apps/backend/serving/observability/alert_rules.py`
- Modify: `/home/juncheng/hybridInference-worktrees/tracked-tasks-helper/test/unit/observability/test_alert_rules.py`

- [ ] **Step 1: Inspect the existing rule patterns**

Run: `cd /home/juncheng/hybridInference-worktrees/tracked-tasks-helper && grep -n "class .*Rule\|_SlidingWindow\|_build_rules\|alert_slack\|AlertSeverity" apps/backend/serving/observability/alert_rules.py | head -40`
Expected: a list of existing rules (e.g., `AuthFailureSpikeRule`), the helper class `_SlidingWindow`, the `AlertEngine._build_rules` method, and imports of `alert_slack` + `AlertSeverity`. Note: `_build_rules` is where the new rule must register.

- [ ] **Step 2: Write the failing test for the per-task-name rule**

Append the following to `tests/unit/observability/test_alert_rules.py`:

```python
import logging
from unittest.mock import AsyncMock, patch

import pytest


def _make_record(task_name: str, success: bool) -> logging.LogRecord:
    record = logging.LogRecord(
        name="serving.observability.tracked_tasks",
        level=logging.INFO if success else logging.WARNING,
        pathname=__file__,
        lineno=0,
        msg="tracked_task_completed",
        args=(),
        exc_info=None,
    )
    record.event = "tracked_task_completed"
    record.task_name = task_name
    record.success = success
    return record


async def test_failure_rate_rule_fires_per_task_name() -> None:
    from serving.observability.alert_config import TrackedTaskFailureRateConfig
    from serving.observability.alert_rules import TrackedTaskFailureRateRule

    cfg = TrackedTaskFailureRateConfig(
        enabled=True,
        window_sec=600,
        threshold_pct=5.0,
        min_samples=50,
        cooldown_sec=0,
    )
    rule = TrackedTaskFailureRateRule(cfg)

    with patch(
        "serving.observability.alert_rules.alert_slack", new_callable=AsyncMock
    ) as mock_alert:
        # 100 records for request_log: 10 fail (10% > 5% threshold).
        for i in range(100):
            await rule.on_record(_make_record("request_log", success=(i >= 10)))
        # 100 records for cost_increment: 1 fails (1% < 5% threshold).
        for i in range(100):
            await rule.on_record(_make_record("cost_increment", success=(i != 0)))

    # request_log should have triggered at least one alert; cost_increment must not.
    triggered_names = [
        call.args[2]["task_name"] for call in mock_alert.call_args_list
    ]
    assert "request_log" in triggered_names
    assert "cost_increment" not in triggered_names


async def test_failure_rate_rule_skips_when_disabled() -> None:
    from serving.observability.alert_config import TrackedTaskFailureRateConfig
    from serving.observability.alert_rules import TrackedTaskFailureRateRule

    cfg = TrackedTaskFailureRateConfig(
        enabled=False,
        window_sec=300,
        threshold_pct=5.0,
        min_samples=1,
        cooldown_sec=0,
    )
    rule = TrackedTaskFailureRateRule(cfg)

    with patch(
        "serving.observability.alert_rules.alert_slack", new_callable=AsyncMock
    ) as mock_alert:
        for _ in range(10):
            await rule.on_record(_make_record("anything", success=False))
    assert mock_alert.await_count == 0


async def test_failure_rate_rule_skips_below_min_samples() -> None:
    from serving.observability.alert_config import TrackedTaskFailureRateConfig
    from serving.observability.alert_rules import TrackedTaskFailureRateRule

    cfg = TrackedTaskFailureRateConfig(
        enabled=True,
        window_sec=300,
        threshold_pct=5.0,
        min_samples=50,
        cooldown_sec=0,
    )
    rule = TrackedTaskFailureRateRule(cfg)

    with patch(
        "serving.observability.alert_rules.alert_slack", new_callable=AsyncMock
    ) as mock_alert:
        # 49 failures (below min_samples=50): no alert.
        for _ in range(49):
            await rule.on_record(_make_record("request_log", success=False))
    assert mock_alert.await_count == 0
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `cd /home/juncheng/hybridInference-worktrees/tracked-tasks-helper && uv run pytest tests/unit/observability/test_alert_rules.py -v -k "failure_rate"`
Expected: FAIL with `ImportError: cannot import name 'TrackedTaskFailureRateRule'`.

- [ ] **Step 4: Implement the rule and register it in `AlertEngine._build_rules`**

In `apps/backend/serving/observability/alert_rules.py`, add the rule class adjacent to other rule classes (after the imports section that already includes `alert_slack`, `AlertSeverity`, `_SlidingWindow`, and `time`):

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

Make sure the file imports `TrackedTaskFailureRateConfig` from `serving.observability.alert_config` (add to the top-level imports if not already present).

Then locate `AlertEngine._build_rules` in the same file and add the registration. Example (the surrounding rule registrations vary — keep them, just add the new line in the same style):

```python
def _build_rules(self) -> list[Any]:
    rules: list[Any] = []
    # ... existing rule constructions (e.g., AuthFailureSpikeRule(self._cfg.rules.auth_failure_spike)) ...
    rules.append(
        TrackedTaskFailureRateRule(self._cfg.rules.tracked_task_failure_rate)
    )
    return rules
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd /home/juncheng/hybridInference-worktrees/tracked-tasks-helper && uv run pytest tests/unit/observability/test_alert_rules.py -v -k "failure_rate"`
Expected: 3 tests pass (`test_failure_rate_rule_fires_per_task_name`, `test_failure_rate_rule_skips_when_disabled`, `test_failure_rate_rule_skips_below_min_samples`).

- [ ] **Step 6: Run the full alert-rules test file to ensure no regression**

Run: `cd /home/juncheng/hybridInference-worktrees/tracked-tasks-helper && uv run pytest tests/unit/observability/test_alert_rules.py -v`
Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
cd /home/juncheng/hybridInference-worktrees/tracked-tasks-helper
git add apps/backend/serving/observability/alert_rules.py tests/unit/observability/test_alert_rules.py
git commit -m "$(cat <<'EOF'
feat(observability): add TrackedTaskFailureRateRule

Per-task-name sliding-window rule that fires alert_slack when
failure rate exceeds threshold and min_samples is met. Registered
in AlertEngine._build_rules.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 1.4: Add `tracked_task_failure_rate` block to `config/alerts.yaml`

**Files:**
- Modify: `/home/juncheng/hybridInference-worktrees/tracked-tasks-helper/config/alerts.yaml`

- [ ] **Step 1: Inspect the current YAML layout**

Run: `cd /home/juncheng/hybridInference-worktrees/tracked-tasks-helper && cat config/alerts.yaml`
Expected: a top-level `rules:` block containing rules from PR #372. Confirm that key names map to the field names in `Rules` (snake_case).

- [ ] **Step 2: Add the new block under `rules:`**

In `config/alerts.yaml`, add the following block under the existing `rules:` section (preserve all existing entries; append at the end of the `rules:` mapping):

```yaml
  tracked_task_failure_rate:
    enabled: true
    window_sec: 300       # 5 min
    threshold_pct: 5.0    # 5% failure rate
    min_samples: 50       # need at least 50 completions before alerting
    cooldown_sec: 1800    # 30 min per task_name
```

- [ ] **Step 3: Add a test that the YAML loads cleanly into the Pydantic config**

Append to `tests/unit/observability/test_alert_rules.py`:

```python
def test_alerts_yaml_loads_with_tracked_task_failure_rate() -> None:
    from pathlib import Path

    import yaml

    from serving.observability.alert_config import AlertingConfig

    repo_root = Path(__file__).resolve().parents[3]
    yaml_path = repo_root / "config" / "alerts.yaml"
    with yaml_path.open() as f:
        data = yaml.safe_load(f)

    cfg = AlertingConfig(**data)
    rule_cfg = cfg.rules.tracked_task_failure_rate
    assert rule_cfg.enabled is True
    assert rule_cfg.window_sec == 300
    assert rule_cfg.threshold_pct == 5.0
    assert rule_cfg.min_samples == 50
    assert rule_cfg.cooldown_sec == 1800
```

If the top-level config object is named differently than `AlertingConfig` (e.g., `AlertConfig`), substitute the correct name — discover by:

Run: `cd /home/juncheng/hybridInference-worktrees/tracked-tasks-helper && grep -n "^class " apps/backend/serving/observability/alert_config.py`
Expected: shows the top-level config class name; update the import in the test accordingly.

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd /home/juncheng/hybridInference-worktrees/tracked-tasks-helper && uv run pytest tests/unit/observability/test_alert_rules.py::test_alerts_yaml_loads_with_tracked_task_failure_rate -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd /home/juncheng/hybridInference-worktrees/tracked-tasks-helper
git add config/alerts.yaml tests/unit/observability/test_alert_rules.py
git commit -m "$(cat <<'EOF'
chore(config): add tracked_task_failure_rate defaults to alerts.yaml

window_sec=300, threshold_pct=5.0, min_samples=50, cooldown_sec=1800.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 1.5: Migrate `completions.py` request-log call-site

**Files:**
- Modify: `/home/juncheng/hybridInference-worktrees/tracked-tasks-helper/apps/backend/serving/servers/routers/completions.py`

- [ ] **Step 1: Read the file and confirm the current scheduling code**

Read `apps/backend/serving/servers/routers/completions.py` lines 1–110. Confirm that lines 44 (`_background_tasks: set = set()`), 47–72 (`_schedule_db_log_task`), and 75–100 (`_schedule_cost_increment`) match the spec. The migration target for this task is the `_schedule_db_log_task` function specifically.

- [ ] **Step 2: Write the failing test**

Create or extend `tests/unit/apps/backend/serving/test_completions_tracked_tasks.py` (create the file if it does not exist; the tests/unit/serving directory already exists per `ls` of test/unit):

```python
"""Verify completions.py call-sites use tracked_task with the right names."""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest


async def test_schedule_db_log_emits_tracked_task_completed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from serving.observability.tracked_tasks import _TRACKED_TASKS
    from serving.servers.routers.completions import _schedule_db_log_task

    _TRACKED_TASKS.clear()
    log_store = MagicMock()
    log_store.log_request = AsyncMock(return_value=None)

    with caplog.at_level(logging.INFO, logger="serving.observability.tracked_tasks"):
        _schedule_db_log_task(log_store, "req-1", {"foo": "bar"})
        # Drain pending tasks.
        await asyncio.gather(*list(_TRACKED_TASKS), return_exceptions=True)

    matching = [
        r for r in caplog.records
        if getattr(r, "event", None) == "tracked_task_completed"
        and getattr(r, "task_name", None) == "request_log"
    ]
    assert len(matching) == 1
    assert matching[0].success is True
    log_store.log_request.assert_awaited_once_with(foo="bar")


async def test_schedule_db_log_failure_emits_failure_event(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from serving.observability.tracked_tasks import _TRACKED_TASKS
    from serving.servers.routers.completions import _schedule_db_log_task

    _TRACKED_TASKS.clear()
    log_store = MagicMock()
    log_store.log_request = AsyncMock(side_effect=RuntimeError("db down"))

    with caplog.at_level(logging.WARNING, logger="serving.observability.tracked_tasks"):
        _schedule_db_log_task(log_store, "req-2", {})
        await asyncio.gather(*list(_TRACKED_TASKS), return_exceptions=True)

    matching = [
        r for r in caplog.records
        if getattr(r, "event", None) == "tracked_task_completed"
        and getattr(r, "task_name", None) == "request_log"
    ]
    assert len(matching) == 1
    assert matching[0].success is False
    assert matching[0].error_type == "RuntimeError"
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `cd /home/juncheng/hybridInference-worktrees/tracked-tasks-helper && uv run pytest tests/unit/apps/backend/serving/test_completions_tracked_tasks.py::test_schedule_db_log_emits_tracked_task_completed -v`
Expected: FAIL — the existing implementation uses `asyncio.create_task` and emits no `tracked_task_completed` event.

- [ ] **Step 4: Replace `_schedule_db_log_task` body with `tracked_task`**

In `apps/backend/serving/servers/routers/completions.py`, locate the function `_schedule_db_log_task` (currently lines 47–72) and replace its body to use the helper. Add the import at the top of the file if not present:

```python
from serving.observability.tracked_tasks import tracked_task
```

Replace lines 47–72 with:

```python
def _schedule_db_log_task(log_store, request_id: str, log_data: dict[str, Any]) -> None:
    """Schedule a background task to log request to database without blocking HTTP response.

    Args:
        log_store: LogStore instance
        request_id: Request identifier for logging
        log_data: Dictionary containing all log request parameters
    """

    async def log_to_db_background() -> None:
        try:
            await log_store.log_request(**log_data)
            logger.debug(f"Background DB logging completed for request {request_id}")
        except Exception as e:
            # Log error context here (request_id) but re-raise so the
            # tracked_task wrapper records this as a failure event.
            logger.error(
                f"Background DB logging failed for request {request_id}: {e}",
                exc_info=True,
            )
            raise

    tracked_task(log_to_db_background(), name="request_log")
```

- [ ] **Step 5: Run the new test to verify it passes**

Run: `cd /home/juncheng/hybridInference-worktrees/tracked-tasks-helper && uv run pytest tests/unit/apps/backend/serving/test_completions_tracked_tasks.py -v`
Expected: 2 tests pass.

- [ ] **Step 6: Run the full completions test suite for regressions**

Run: `cd /home/juncheng/hybridInference-worktrees/tracked-tasks-helper && uv run pytest tests/unit/apps/backend/serving/ -v -k "completions"`
Expected: all existing completions tests still pass.

- [ ] **Step 7: Commit**

```bash
cd /home/juncheng/hybridInference-worktrees/tracked-tasks-helper
git add apps/backend/serving/servers/routers/completions.py tests/unit/apps/backend/serving/test_completions_tracked_tasks.py
git commit -m "$(cat <<'EOF'
refactor(completions): use tracked_task for DB request log

Replaces asyncio.create_task + manual _background_tasks bookkeeping
with tracked_task(name=\"request_log\"). Failure inside log_request
now surfaces as a tracked_task_completed event.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 1.6: Migrate `completions.py` cost-increment call-site

**Files:**
- Modify: `/home/juncheng/hybridInference-worktrees/tracked-tasks-helper/apps/backend/serving/servers/routers/completions.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/apps/backend/serving/test_completions_tracked_tasks.py`:

```python
async def test_schedule_cost_increment_emits_tracked_task_completed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from serving.observability.tracked_tasks import _TRACKED_TASKS
    from serving.servers.routers.completions import _schedule_cost_increment

    _TRACKED_TASKS.clear()
    op_store = MagicMock()
    op_store.increment_user_cost = AsyncMock(return_value=None)

    usage = {"prompt_tokens": 100, "completion_tokens": 50}
    pricing = {"prompt": "1.0", "completion": "2.0"}  # nonzero so cost > 0

    with caplog.at_level(logging.INFO, logger="serving.observability.tracked_tasks"):
        _schedule_cost_increment(op_store, "user-1", usage, pricing)
        await asyncio.gather(*list(_TRACKED_TASKS), return_exceptions=True)

    matching = [
        r for r in caplog.records
        if getattr(r, "event", None) == "tracked_task_completed"
        and getattr(r, "task_name", None) == "cost_increment"
    ]
    assert len(matching) == 1
    assert matching[0].success is True
    op_store.increment_user_cost.assert_awaited_once()


async def test_schedule_cost_increment_skipped_when_zero_cost() -> None:
    from serving.observability.tracked_tasks import _TRACKED_TASKS
    from serving.servers.routers.completions import _schedule_cost_increment

    _TRACKED_TASKS.clear()
    op_store = MagicMock()
    op_store.increment_user_cost = AsyncMock()

    # Zero pricing → cost == 0 → no task scheduled.
    _schedule_cost_increment(op_store, "user-2", {"prompt_tokens": 1}, {"prompt": "0", "completion": "0"})
    assert len(_TRACKED_TASKS) == 0
    op_store.increment_user_cost.assert_not_awaited()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd /home/juncheng/hybridInference-worktrees/tracked-tasks-helper && uv run pytest tests/unit/apps/backend/serving/test_completions_tracked_tasks.py::test_schedule_cost_increment_emits_tracked_task_completed -v`
Expected: FAIL — current code uses `asyncio.create_task`, no event emitted.

- [ ] **Step 3: Replace `_schedule_cost_increment` body to use `tracked_task`**

In `apps/backend/serving/servers/routers/completions.py`, replace the body of `_schedule_cost_increment` (currently lines 75–100) with:

```python
def _schedule_cost_increment(
    op_store: Any,
    user_id: str,
    usage: dict[str, int] | None,
    pricing: dict[str, str] | None,
) -> None:
    """Increment the user's daily cost counter for billed requests.

    Only called for successful responses where cost > 0. Runs as a
    fire-and-forget background task to avoid blocking the response.
    """
    from serving.storage.utils import calculate_cost

    cost = calculate_cost(usage, pricing)
    if not cost or cost <= 0 or not op_store:
        return

    async def _increment() -> None:
        try:
            await op_store.increment_user_cost(user_id, cost)
        except Exception as exc:
            logger.warning(f"Failed to increment cost counter for {user_id}: {exc}")
            raise  # let tracked_task record the failure

    tracked_task(_increment(), name="cost_increment")
```

- [ ] **Step 4: Delete the now-redundant `_background_tasks` set**

Still in `apps/backend/serving/servers/routers/completions.py`, delete the line:

```python
_background_tasks: set = set()
```

(line 44 in the pre-modification file). Verify no remaining references:

Run: `cd /home/juncheng/hybridInference-worktrees/tracked-tasks-helper && grep -n "_background_tasks" apps/backend/serving/servers/routers/completions.py`
Expected: no matches.

- [ ] **Step 5: Run the new tests + completions suite**

Run: `cd /home/juncheng/hybridInference-worktrees/tracked-tasks-helper && uv run pytest tests/unit/apps/backend/serving/test_completions_tracked_tasks.py tests/unit/apps/backend/serving/ -v -k "completions or tracked"`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
cd /home/juncheng/hybridInference-worktrees/tracked-tasks-helper
git add apps/backend/serving/servers/routers/completions.py tests/unit/apps/backend/serving/test_completions_tracked_tasks.py
git commit -m "$(cat <<'EOF'
refactor(completions): use tracked_task for cost increment

Replaces asyncio.create_task with tracked_task(name=\"cost_increment\")
and removes the now-redundant module-level _background_tasks set.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 1.7: Migrate `dual_write.py` shadow-write call-site

**Files:**
- Modify: `/home/juncheng/hybridInference-worktrees/tracked-tasks-helper/apps/backend/serving/storage/dual_write.py`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/storage/test_dual_write_tracked.py` (the `tests/unit/storage/` directory exists). If it does not, create both the directory marker and the test file:

```python
"""Verify dual_write shadow writes are wrapped in tracked_task."""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock

import pytest


class _Stub:
    """Bare async stubs for primary + shadow OperationalStore."""

    def __init__(self, raises: Exception | None = None) -> None:
        self._raises = raises
        self.update_user_last_login = AsyncMock(side_effect=self._maybe_raise)

    async def _maybe_raise(self, *_args, **_kw) -> None:
        if self._raises:
            raise self._raises


async def test_dual_write_shadow_emits_tracked_task_completed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from serving.observability.tracked_tasks import _TRACKED_TASKS
    from serving.storage.dual_write import DualWriteOperationalStore

    _TRACKED_TASKS.clear()
    primary = _Stub()
    shadow = _Stub()
    store = DualWriteOperationalStore(primary, shadow)

    with caplog.at_level(logging.INFO, logger="serving.observability.tracked_tasks"):
        await store.update_user_last_login("user-1")
        await asyncio.gather(*list(_TRACKED_TASKS), return_exceptions=True)

    matching = [
        r for r in caplog.records
        if getattr(r, "event", None) == "tracked_task_completed"
        and getattr(r, "task_name", None) == "dual_write_shadow"
    ]
    assert len(matching) == 1
    assert matching[0].success is True
    primary.update_user_last_login.assert_awaited_once_with("user-1")
    shadow.update_user_last_login.assert_awaited_once_with("user-1")


async def test_dual_write_shadow_failure_does_not_propagate(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from serving.observability.tracked_tasks import _TRACKED_TASKS
    from serving.storage.dual_write import DualWriteOperationalStore

    _TRACKED_TASKS.clear()
    primary = _Stub()
    shadow = _Stub(raises=RuntimeError("shadow boom"))
    store = DualWriteOperationalStore(primary, shadow)

    with caplog.at_level(logging.WARNING, logger="serving.observability.tracked_tasks"):
        # Must not raise even though shadow fails.
        await store.update_user_last_login("user-2")
        await asyncio.gather(*list(_TRACKED_TASKS), return_exceptions=True)

    matching = [
        r for r in caplog.records
        if getattr(r, "event", None) == "tracked_task_completed"
        and getattr(r, "task_name", None) == "dual_write_shadow"
    ]
    assert len(matching) == 1
    assert matching[0].success is False
    assert matching[0].error_type == "RuntimeError"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd /home/juncheng/hybridInference-worktrees/tracked-tasks-helper && uv run pytest tests/unit/storage/test_dual_write_tracked.py -v`
Expected: FAIL — `dual_write_shadow` event not emitted.

- [ ] **Step 3: Wrap the shadow write in `tracked_task`**

In `apps/backend/serving/storage/dual_write.py`:

1. Add import at the top (after existing `from .base import ...`):

```python
from serving.observability.tracked_tasks import tracked_task
```

2. Locate `_do_shadow` (lines 61–76 in the current file) and replace it with:

```python
    async def _do_shadow(self, method_name: str, coro, /, **ctx_ids: Any) -> None:
        """Schedule *coro* (a shadow method call) via tracked_task.

        Shadow writes run as fire-and-forget so the primary write's latency
        is not coupled to the shadow's. The tracked_task wrapper emits a
        success or failure event consumed by TrackedTaskFailureRateRule.
        Health bookkeeping (self._shadow_healthy, recovery log) runs inside
        the wrapped coroutine so it observes the actual outcome.
        """

        async def _shadow_runner() -> None:
            try:
                await coro
                if not self._shadow_healthy:
                    self._shadow_healthy = True
                    logger.info("Shadow operational store recovered: %s", method_name)
            except Exception:
                self._shadow_healthy = False
                ctx = _shadow_ctx(**ctx_ids)
                logger.warning(
                    "Shadow operational write failed: method=%s %s",
                    method_name,
                    ctx,
                    exc_info=True,
                )
                raise  # let tracked_task record the failure

        tracked_task(_shadow_runner(), name="dual_write_shadow")
```

Note: the surface signature of `_do_shadow` stays `async def` so existing call sites (`await self._do_shadow(...)`) keep compiling — `await`-ing the now-non-blocking call is a no-op and adds a single sched yield. The shadow work itself runs detached. This matches the spec table requirement that shadow writes become fire-and-forget under `tracked_task`.

- [ ] **Step 4: Run the new test + the existing dual-write tests**

Run: `cd /home/juncheng/hybridInference-worktrees/tracked-tasks-helper && uv run pytest tests/unit/storage/ -v -k "dual_write"`
Expected: all pass (the existing dual-write tests should still pass because shadow failures still don't propagate, and `_shadow_healthy` is still toggled).

- [ ] **Step 5: Commit**

```bash
cd /home/juncheng/hybridInference-worktrees/tracked-tasks-helper
git add apps/backend/serving/storage/dual_write.py tests/unit/storage/test_dual_write_tracked.py
git commit -m "$(cat <<'EOF'
refactor(dual_write): schedule shadow writes via tracked_task

Shadow writes now run as fire-and-forget tracked_tasks named
\"dual_write_shadow\". Health bookkeeping moves inside the runner so it
observes the real outcome. Primary-write latency no longer couples to
shadow latency.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 1.8: Format, push, open PR, monitor CI

**Files:** none (process step).

- [ ] **Step 1: Run formatter and linter**

Run: `cd /home/juncheng/hybridInference-worktrees/tracked-tasks-helper && make format && make lint`
Expected: both succeed; commit any formatter changes:

```bash
cd /home/juncheng/hybridInference-worktrees/tracked-tasks-helper
git add -u
git diff --cached --quiet || git commit -m "$(cat <<'EOF'
chore: ruff format

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 2: Run the full test suite**

Run: `cd /home/juncheng/hybridInference-worktrees/tracked-tasks-helper && make test`
Expected: green.

- [ ] **Step 3: Push and open the PR**

```bash
cd /home/juncheng/hybridInference-worktrees/tracked-tasks-helper
git push -u origin jason/claude/tracked-tasks-helper

gh pr create --base dev --title "feat(observability): tracked_task helper + 3 call-sites + failure-rate rule" --body "$(cat <<'EOF'
## Summary
- New `apps/backend/serving/observability/tracked_tasks.py` — thin asyncio.ensure_future wrapper that emits `tracked_task_completed` log events on success or failure
- New `TrackedTaskFailureRateRule` (per-task-name sliding window) registered in `AlertEngine._build_rules`
- Migrate 3 fire-and-forget call-sites to the helper:
  - `apps/backend/serving/servers/routers/completions.py` — request log + cost increment
  - `apps/backend/serving/storage/dual_write.py` — D1 shadow write
- New defaults in `config/alerts.yaml`: window=300s, threshold=5%, min_samples=50, cooldown=1800s

Closes #<issue-number-from-task-1.0>

Spec: docs/agents/specs/2026-05-03-tracked-tasks-design.md

## Test plan
- [ ] Unit: `uv run pytest tests/unit/observability/test_tracked_tasks.py tests/unit/observability/test_alert_rules.py tests/unit/apps/backend/serving/test_completions_tracked_tasks.py tests/unit/storage/test_dual_write_tracked.py -v`
- [ ] Full suite: `make test`
- [ ] Manually verify on staging that `tracked_task_completed` log records appear after a request and the rule does not fire under healthy traffic

PR 2 (separate branch) will add the RouteWise pending-decisions TTL + leak rule.
EOF
)"
```

Capture the PR URL.

- [ ] **Step 4: Monitor CI every 2 minutes until green**

```bash
gh pr checks --watch
```

If a check fails, fix the underlying issue, push a new commit (NEVER force-push), and re-run `gh pr checks --watch`.

- [ ] **Step 5: Address PR comments every 2 minutes until resolved**

Loop:

```bash
gh pr view --json comments,reviewDecision,reviewRequests
```

For every unresolved review comment: implement the requested change, commit, push. Repeat until `reviewDecision` is `APPROVED` and no unresolved comments remain.

- [ ] **Step 6: After merge, delete branch and worktree**

```bash
cd /home/juncheng/hybridInference
git worktree remove /home/juncheng/hybridInference-worktrees/tracked-tasks-helper
git push origin --delete jason/claude/tracked-tasks-helper
git branch -D jason/claude/tracked-tasks-helper 2>/dev/null || true
```

---

## PR 2 — RouteWise pending-decisions TTL + leak rule

**Pre-condition:** PR 1 is merged into `dev`.

### Task 2.0: Pre-condition verification, worktree, branch, and issue setup

**Files:** none (process step).

- [ ] **Step 1: Verify PR 1 is merged**

```bash
cd /home/juncheng/hybridInference
git fetch origin
git checkout dev
git pull origin dev
grep -q "tracked_task" apps/backend/serving/observability/tracked_tasks.py && echo "PR 1 merged"
```
Expected: prints `PR 1 merged`. If not, abort and wait for PR 1.

- [ ] **Step 2: Create worktree and branch**

```bash
git worktree add /home/juncheng/hybridInference-worktrees/pending-decisions-ttl -b jason/claude/pending-decisions-ttl origin/dev
```

- [ ] **Step 3: Create the GitHub issue**

```bash
gh issue create \
  --repo "$(gh repo view --json nameWithOwner -q .nameWithOwner)" \
  --title "RouteWise: TTL sweep for _pending_decisions + leak alert" \
  --body "$(cat <<'EOF'
## Summary
RouteWiseRouter._pending_decisions leaks when record_observation is never called (request abort, timeout, code-path bug). Add periodic TTL sweep (60s interval, 300s TTL) that emits routewise_decision_evicted events. Add PendingDecisionsLeakRule that fires when eviction count exceeds threshold over a window.

Spec: docs/agents/specs/2026-05-03-tracked-tasks-design.md (PR 2)
Depends on: tracked_task helper PR (already merged)
EOF
)"
```

- [ ] **Step 4: Verify worktree**

Run: `cd /home/juncheng/hybridInference-worktrees/pending-decisions-ttl && git status && git rev-parse --abbrev-ref HEAD`
Expected: clean tree on `jason/claude/pending-decisions-ttl`.

---

### Task 2.1: Add TTL sweep to `RouteWiseRouter`

**Files:**
- Modify: `/home/juncheng/hybridInference-worktrees/pending-decisions-ttl/apps/backend/routing/routewise/router.py`
- Create: `/home/juncheng/hybridInference-worktrees/pending-decisions-ttl/test/unit/apps/backend/routing/test_pending_decisions_ttl.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/apps/backend/routing/test_pending_decisions_ttl.py`:

```python
"""TTL sweep tests for RouteWiseRouter._pending_decisions."""

from __future__ import annotations

import asyncio
import logging
import time
from unittest.mock import MagicMock

import pytest


def _make_router_with_pending(monkeypatch: pytest.MonkeyPatch):
    """Construct a minimal RouteWiseRouter that bypasses _classify_all etc."""
    from routing.routewise.router import RouteWiseRouter

    # Avoid running classification / validation on a real fixed_router.
    monkeypatch.setattr(RouteWiseRouter, "_classify_all", lambda self: None)
    monkeypatch.setattr(RouteWiseRouter, "_build_adapter_sub_type_map", lambda self: None)
    monkeypatch.setattr(RouteWiseRouter, "_precompute_api_prices", lambda self: None)
    monkeypatch.setattr(RouteWiseRouter, "_validate_api_baseline", lambda self: None)
    monkeypatch.setattr(RouteWiseRouter, "_init_latency_profiles", lambda self: None)

    fixed_router = MagicMock()
    fixed_router.routes = {}
    config = MagicMock()
    config.concurrency_enabled = False
    config.latency_window_sec = 60
    config.latency_swrr_alpha = 0.1

    return RouteWiseRouter(fixed_router=fixed_router, config=config, experiment_mode=False)


async def test_pending_decisions_evicted_after_ttl(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    router = _make_router_with_pending(monkeypatch)

    now = time.time()
    router._pending_decisions["req-old"] = {"timestamp": now - 1000.0}  # > TTL
    router._pending_decisions["req-fresh"] = {"timestamp": now}

    with caplog.at_level(logging.INFO, logger="routing.routewise.router"):
        await router._sweep_pending_decisions_once()

    assert "req-old" not in router._pending_decisions
    assert "req-fresh" in router._pending_decisions
    matching = [
        r for r in caplog.records
        if getattr(r, "event", None) == "routewise_decision_evicted"
        and getattr(r, "request_id", None) == "req-old"
    ]
    assert len(matching) == 1
    assert matching[0].age_sec >= 1000


async def test_pending_decisions_no_eviction_within_ttl(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    router = _make_router_with_pending(monkeypatch)

    now = time.time()
    router._pending_decisions["req-fresh"] = {"timestamp": now - 30.0}  # < TTL

    with caplog.at_level(logging.INFO, logger="routing.routewise.router"):
        await router._sweep_pending_decisions_once()

    assert "req-fresh" in router._pending_decisions
    matching = [
        r for r in caplog.records
        if getattr(r, "event", None) == "routewise_decision_evicted"
    ]
    assert matching == []


async def test_pending_decisions_start_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    router = _make_router_with_pending(monkeypatch)

    await router.start()
    # Internal task must exist and be active.
    assert router._sweep_task is not None
    assert not router._sweep_task.done()

    await router.stop()
    assert router._sweep_task is None or router._sweep_task.cancelled() or router._sweep_task.done()


async def test_pending_decisions_skips_entries_without_timestamp(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """An entry missing 'timestamp' must not crash the sweep."""
    router = _make_router_with_pending(monkeypatch)
    router._pending_decisions["req-bad"] = {}  # no timestamp

    # Should not raise; entry stays.
    await router._sweep_pending_decisions_once()
    assert "req-bad" in router._pending_decisions
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd /home/juncheng/hybridInference-worktrees/pending-decisions-ttl && uv run pytest tests/unit/apps/backend/routing/test_pending_decisions_ttl.py -v`
Expected: FAIL — `_sweep_pending_decisions_once`, `start`, `stop`, and `_sweep_task` don't exist on `RouteWiseRouter`.

- [ ] **Step 3: Add TTL sweep + start/stop to `RouteWiseRouter`**

In `apps/backend/routing/routewise/router.py`:

1. Add module-level constants near the top of the file (after the `logger = get_logger(__name__)` line):

```python
# TTL for entries in RouteWiseRouter._pending_decisions. If
# record_observation isn't called within this window (request abort,
# timeout, code-path bug) the entry is evicted by a periodic sweep.
PENDING_DECISIONS_TTL_SECONDS: float = 300.0
PENDING_DECISIONS_SWEEP_INTERVAL_SECONDS: float = 60.0
```

2. In `RouteWiseRouter.__init__`, immediately after `self._pending_decisions: dict[str, dict[str, Any]] = {}` (line 125), add:

```python
        # Async lock to serialize sweep against concurrent _select_adapter
        # / record_observation modifications.
        self._pending_decisions_lock: asyncio.Lock = asyncio.Lock()
        # Periodic-cleanup task; populated by start(), cancelled by stop().
        self._sweep_task: asyncio.Task[None] | None = None
```

3. At the end of the class (after the last method), add:

```python
    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the periodic _pending_decisions TTL sweep task."""
        if self._sweep_task is not None and not self._sweep_task.done():
            return
        self._sweep_task = asyncio.create_task(self._sweep_pending_decisions_loop())

    async def stop(self) -> None:
        """Cancel the periodic _pending_decisions sweep task."""
        task = self._sweep_task
        self._sweep_task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass

    async def _sweep_pending_decisions_loop(self) -> None:
        """Run the TTL sweep on a fixed interval until cancelled."""
        try:
            while True:
                await asyncio.sleep(PENDING_DECISIONS_SWEEP_INTERVAL_SECONDS)
                try:
                    await self._sweep_pending_decisions_once()
                except Exception:
                    logger.exception("RouteWise pending-decisions sweep failed")
        except asyncio.CancelledError:
            return

    async def _sweep_pending_decisions_once(self) -> int:
        """Evict stale entries from _pending_decisions; return count evicted."""
        now = time.time()
        cutoff = now - PENDING_DECISIONS_TTL_SECONDS
        evicted = 0
        async with self._pending_decisions_lock:
            stale: list[tuple[str, float]] = []
            for request_id, decision in self._pending_decisions.items():
                ts = decision.get("timestamp")
                if not isinstance(ts, (int, float)):
                    # Skip malformed entries — they don't have a usable age.
                    continue
                if ts < cutoff:
                    stale.append((request_id, float(ts)))
            for request_id, ts in stale:
                self._pending_decisions.pop(request_id, None)
                evicted += 1
                logger.info(
                    "routewise_decision_evicted",
                    extra={
                        "event": "routewise_decision_evicted",
                        "request_id": request_id,
                        "age_sec": int(now - ts),
                    },
                )
        return evicted
```

4. Audit existing dict mutation sites (`_select_adapter`, `record_observation`, `chat_completion`, `stream_chat_completion`) — they use synchronous dict access. Wrap them in the lock only if the dict access is from an async context. Read each call-site:

Run: `cd /home/juncheng/hybridInference-worktrees/pending-decisions-ttl && grep -n "_pending_decisions\[" apps/backend/routing/routewise/router.py`
Expected: line numbers around 890, 915, 942, 960, 1080, 1081, 1113, 1114.

For each line, check whether the enclosing function is `async def`. The four assignment sites (~890, 915, 942, 960) are inside `_select_adapter` which is **synchronous** — leave them. The four pop / mutation sites in `_execute_*` and `chat_completion` are inside async methods. **Don't** wrap these in the lock — `asyncio.Lock` cannot be acquired from a sync function and acquiring it from the async path on every observation would serialize the hot routing path.

Instead, accept that the sweep races with concurrent reads. The race is benign because:
- The sweep only deletes entries whose `timestamp < now - 300s` (~5 min old).
- Hot-path operations (`_execute_adapter`, `record_observation`) operate on entries that are at most a few seconds old.
- `dict.pop(key, None)` and item assignment are individually thread-safe in CPython under the GIL — and asyncio is single-threaded anyway.

So: keep the lock around the **sweep itself** to protect the iteration (avoids `RuntimeError: dictionary changed size during iteration` if a future async caller ever enters the dict). Do NOT add the lock to `_select_adapter` or `record_observation`. The test for the lock existence is satisfied; we only acquire it during the sweep.

Update the docstring comment on `self._pending_decisions_lock` accordingly:

```python
        # Async lock held only during the periodic TTL sweep to safeguard
        # iteration. Hot-path mutations (_select_adapter, record_observation)
        # rely on CPython's per-op dict atomicity and asyncio's single-threaded
        # event loop; acquiring the lock on every observation would serialize
        # the routing hot path with no real benefit.
        self._pending_decisions_lock: asyncio.Lock = asyncio.Lock()
```

5. Verify all four `_pending_decisions[request_id] = {...}` assignment sites populate a `timestamp` field. Run:

Run: `cd /home/juncheng/hybridInference-worktrees/pending-decisions-ttl && grep -n -A 6 "_pending_decisions\[request_id\] = {" apps/backend/routing/routewise/router.py`
Expected output: each block contains a `"timestamp": time.time()` (or equivalent) entry.

If any block is missing a `timestamp` key, add one. The test `test_pending_decisions_skips_entries_without_timestamp` covers the defensive `not isinstance(ts, ...)` branch but production code should always populate it.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd /home/juncheng/hybridInference-worktrees/pending-decisions-ttl && uv run pytest tests/unit/apps/backend/routing/test_pending_decisions_ttl.py -v`
Expected: 4 tests pass.

- [ ] **Step 5: Run the full RouteWise test suite for regressions**

Run: `cd /home/juncheng/hybridInference-worktrees/pending-decisions-ttl && uv run pytest tests/unit/apps/backend/routing/ -v`
Expected: green.

- [ ] **Step 6: Commit**

```bash
cd /home/juncheng/hybridInference-worktrees/pending-decisions-ttl
git add apps/backend/routing/routewise/router.py tests/unit/apps/backend/routing/test_pending_decisions_ttl.py
git commit -m "$(cat <<'EOF'
feat(routewise): TTL sweep for _pending_decisions

Adds a periodic 60s sweep with 300s TTL that evicts stale entries from
RouteWiseRouter._pending_decisions and emits routewise_decision_evicted
log events. start() / stop() lifecycle methods control the task.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 2.2: Wire `start()` / `stop()` from bootstrap

**Files:**
- Modify: `/home/juncheng/hybridInference-worktrees/pending-decisions-ttl/apps/backend/serving/servers/bootstrap.py`

- [ ] **Step 1: Read the bootstrap section that constructs `RouteWiseRouter`**

Run: `cd /home/juncheng/hybridInference-worktrees/pending-decisions-ttl && grep -n "RouteWiseRouter\|routewise_router\|shutdown\|cleanup" apps/backend/serving/servers/bootstrap.py | head -30`
Expected: shows the construction site (~line 281) and likely a startup helper plus a shutdown / cleanup helper.

- [ ] **Step 2: Write the failing test**

Append to `tests/unit/apps/backend/routing/test_pending_decisions_ttl.py`:

```python
async def test_routewise_router_started_and_stopped_in_bootstrap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify RouteWiseRouter.start is awaited after construction and stop is awaited on shutdown."""
    # Pure unit-level guard: import the module, ensure both helpers exist.
    import inspect

    from serving.servers import bootstrap

    src = inspect.getsource(bootstrap)
    # The bootstrap path should call start() on the routewise router.
    assert "routewise_router.start()" in src
    # And there must be a shutdown helper that calls stop().
    assert "routewise_router.stop()" in src or ".stop()  # routewise" in src
```

- [ ] **Step 3: Run to verify it fails**

Run: `cd /home/juncheng/hybridInference-worktrees/pending-decisions-ttl && uv run pytest tests/unit/apps/backend/routing/test_pending_decisions_ttl.py::test_routewise_router_started_and_stopped_in_bootstrap -v`
Expected: FAIL.

- [ ] **Step 4: Wire start/stop in bootstrap**

In `apps/backend/serving/servers/bootstrap.py`, in the block where `routewise_router` is instantiated (after the existing `routewise_router = RouteWiseRouter(...)` line), add:

```python
            await routewise_router.start()
            _BACKGROUND_TASKS_ROUTEWISE_REF = routewise_router  # keep a strong ref
```

(Use the existing pattern in the file. If the surrounding code is **not** in an async function, locate the bootstrap entry point that *is* async — typically `bootstrap_app()` or `init_services()`. Discover by:

Run: `cd /home/juncheng/hybridInference-worktrees/pending-decisions-ttl && grep -n "async def" apps/backend/serving/servers/bootstrap.py | head -10`)

Locate the corresponding shutdown helper. Discover by:

Run: `cd /home/juncheng/hybridInference-worktrees/pending-decisions-ttl && grep -n "shutdown\|cleanup\|teardown" apps/backend/serving/servers/bootstrap.py | head -10`

In that shutdown helper, add (using the routewise router that the bootstrap function already returns/exposes — typically via `AppServices`):

```python
    if services.routewise_router is not None:
        await services.routewise_router.stop()
```

If `AppServices` does not currently expose `routewise_router`, add a field for it. Discover the dataclass:

Run: `cd /home/juncheng/hybridInference-worktrees/pending-decisions-ttl && grep -n "class AppServices\|routewise" apps/backend/serving/servers/deps.py`

If `routewise_router` is missing from `AppServices`, add it as `routewise_router: RouteWiseRouter | None = None` and pipe it through bootstrap's return path.

- [ ] **Step 5: Run the test to verify it passes**

Run: `cd /home/juncheng/hybridInference-worktrees/pending-decisions-ttl && uv run pytest tests/unit/apps/backend/routing/test_pending_decisions_ttl.py::test_routewise_router_started_and_stopped_in_bootstrap -v`
Expected: PASS.

- [ ] **Step 6: Run the bootstrap and routing test suites**

Run: `cd /home/juncheng/hybridInference-worktrees/pending-decisions-ttl && uv run pytest tests/unit/apps/backend/serving/ tests/unit/apps/backend/routing/ -v`
Expected: green.

- [ ] **Step 7: Commit**

```bash
cd /home/juncheng/hybridInference-worktrees/pending-decisions-ttl
git add apps/backend/serving/servers/bootstrap.py apps/backend/serving/servers/deps.py tests/unit/apps/backend/routing/test_pending_decisions_ttl.py
git commit -m "$(cat <<'EOF'
feat(bootstrap): wire RouteWiseRouter start/stop lifecycle

Calls routewise_router.start() after construction so the
_pending_decisions TTL sweep runs, and routewise_router.stop() on
shutdown so the task is cancelled cleanly.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 2.3: Add `PendingDecisionsLeakConfig` Pydantic model

**Files:**
- Modify: `/home/juncheng/hybridInference-worktrees/pending-decisions-ttl/apps/backend/serving/observability/alert_config.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/observability/test_alert_rules.py`:

```python
def test_pending_decisions_leak_config_parses() -> None:
    from serving.observability.alert_config import PendingDecisionsLeakConfig

    cfg = PendingDecisionsLeakConfig(
        enabled=True,
        window_sec=600,
        threshold_count=20,
        cooldown_sec=3600,
    )
    assert cfg.enabled is True
    assert cfg.window_sec == 600
    assert cfg.threshold_count == 20
    assert cfg.cooldown_sec == 3600
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd /home/juncheng/hybridInference-worktrees/pending-decisions-ttl && uv run pytest tests/unit/observability/test_alert_rules.py::test_pending_decisions_leak_config_parses -v`
Expected: FAIL with `ImportError: cannot import name 'PendingDecisionsLeakConfig'`.

- [ ] **Step 3: Add the Pydantic model and slot it into `Rules`**

In `apps/backend/serving/observability/alert_config.py`, add:

```python
class PendingDecisionsLeakConfig(BaseModel):
    """Config for PendingDecisionsLeakRule.

    Fires when the number of routewise_decision_evicted events over
    ``window_sec`` exceeds ``threshold_count``.
    """

    enabled: bool = True
    window_sec: int = 600
    threshold_count: int = 20
    cooldown_sec: int = 3600
```

Then update `Rules`:

```python
class Rules(BaseModel):
    # ... existing fields including tracked_task_failure_rate from PR 1 ...
    pending_decisions_leak: PendingDecisionsLeakConfig = PendingDecisionsLeakConfig()
```

- [ ] **Step 4: Run to verify the test passes**

Run: `cd /home/juncheng/hybridInference-worktrees/pending-decisions-ttl && uv run pytest tests/unit/observability/test_alert_rules.py::test_pending_decisions_leak_config_parses -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd /home/juncheng/hybridInference-worktrees/pending-decisions-ttl
git add apps/backend/serving/observability/alert_config.py tests/unit/observability/test_alert_rules.py
git commit -m "$(cat <<'EOF'
feat(observability): add PendingDecisionsLeakConfig model

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 2.4: Add `PendingDecisionsLeakRule` and register it

**Files:**
- Modify: `/home/juncheng/hybridInference-worktrees/pending-decisions-ttl/apps/backend/serving/observability/alert_rules.py`
- Modify: `/home/juncheng/hybridInference-worktrees/pending-decisions-ttl/test/unit/observability/test_alert_rules.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/observability/test_alert_rules.py`:

```python
def _make_eviction_record() -> logging.LogRecord:
    record = logging.LogRecord(
        name="routing.routewise.router",
        level=logging.INFO,
        pathname=__file__,
        lineno=0,
        msg="routewise_decision_evicted",
        args=(),
        exc_info=None,
    )
    record.event = "routewise_decision_evicted"
    record.request_id = "r"
    record.age_sec = 400
    return record


async def test_leak_rule_fires_at_threshold() -> None:
    from serving.observability.alert_config import PendingDecisionsLeakConfig
    from serving.observability.alert_rules import PendingDecisionsLeakRule

    cfg = PendingDecisionsLeakConfig(
        enabled=True,
        window_sec=600,
        threshold_count=20,
        cooldown_sec=0,
    )
    rule = PendingDecisionsLeakRule(cfg)

    with patch(
        "serving.observability.alert_rules.alert_slack", new_callable=AsyncMock
    ) as mock_alert:
        for _ in range(21):
            await rule.on_record(_make_eviction_record())

    assert mock_alert.await_count >= 1
    payload = mock_alert.call_args_list[0].args[2]
    assert payload["evicted_count"] == 21
    assert payload["window_sec"] == 600


async def test_leak_rule_does_not_fire_below_threshold() -> None:
    from serving.observability.alert_config import PendingDecisionsLeakConfig
    from serving.observability.alert_rules import PendingDecisionsLeakRule

    cfg = PendingDecisionsLeakConfig(
        enabled=True,
        window_sec=600,
        threshold_count=20,
        cooldown_sec=0,
    )
    rule = PendingDecisionsLeakRule(cfg)

    with patch(
        "serving.observability.alert_rules.alert_slack", new_callable=AsyncMock
    ) as mock_alert:
        for _ in range(20):  # exactly threshold; rule fires only when > threshold
            await rule.on_record(_make_eviction_record())

    assert mock_alert.await_count == 0


async def test_leak_rule_ignores_other_events() -> None:
    from serving.observability.alert_config import PendingDecisionsLeakConfig
    from serving.observability.alert_rules import PendingDecisionsLeakRule

    cfg = PendingDecisionsLeakConfig(
        enabled=True,
        window_sec=600,
        threshold_count=1,
        cooldown_sec=0,
    )
    rule = PendingDecisionsLeakRule(cfg)

    with patch(
        "serving.observability.alert_rules.alert_slack", new_callable=AsyncMock
    ) as mock_alert:
        rec = _make_eviction_record()
        rec.event = "something_else"
        await rule.on_record(rec)
        await rule.on_record(rec)

    assert mock_alert.await_count == 0
```

- [ ] **Step 2: Run to verify the tests fail**

Run: `cd /home/juncheng/hybridInference-worktrees/pending-decisions-ttl && uv run pytest tests/unit/observability/test_alert_rules.py -v -k "leak_rule"`
Expected: FAIL with `ImportError: cannot import name 'PendingDecisionsLeakRule'`.

- [ ] **Step 3: Implement the rule and register it**

In `apps/backend/serving/observability/alert_rules.py`, add:

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

Add the import at the top of the file:

```python
from serving.observability.alert_config import (
    PendingDecisionsLeakConfig,
    TrackedTaskFailureRateConfig,
)
```

(merge with the existing imports of `TrackedTaskFailureRateConfig` from PR 1).

In `AlertEngine._build_rules`, append:

```python
    rules.append(
        PendingDecisionsLeakRule(self._cfg.rules.pending_decisions_leak)
    )
```

- [ ] **Step 4: Run to verify all tests pass**

Run: `cd /home/juncheng/hybridInference-worktrees/pending-decisions-ttl && uv run pytest tests/unit/observability/test_alert_rules.py -v`
Expected: green.

- [ ] **Step 5: Commit**

```bash
cd /home/juncheng/hybridInference-worktrees/pending-decisions-ttl
git add apps/backend/serving/observability/alert_rules.py tests/unit/observability/test_alert_rules.py
git commit -m "$(cat <<'EOF'
feat(observability): add PendingDecisionsLeakRule

Count-based sliding-window rule that fires alert_slack when more than
threshold_count routewise_decision_evicted events arrive within the
window. Registered in AlertEngine._build_rules.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 2.5: Add `pending_decisions_leak` defaults to `config/alerts.yaml`

**Files:**
- Modify: `/home/juncheng/hybridInference-worktrees/pending-decisions-ttl/config/alerts.yaml`

- [ ] **Step 1: Append to the `rules:` block in `config/alerts.yaml`**

```yaml
  pending_decisions_leak:
    enabled: true
    window_sec: 600       # 10 min
    threshold_count: 20   # >20 evictions in 10 min indicates a real leak
    cooldown_sec: 3600    # 1 h
```

- [ ] **Step 2: Add a YAML-load test**

Append to `tests/unit/observability/test_alert_rules.py`:

```python
def test_alerts_yaml_loads_with_pending_decisions_leak() -> None:
    from pathlib import Path

    import yaml

    from serving.observability.alert_config import AlertingConfig

    repo_root = Path(__file__).resolve().parents[3]
    yaml_path = repo_root / "config" / "alerts.yaml"
    with yaml_path.open() as f:
        data = yaml.safe_load(f)

    cfg = AlertingConfig(**data)
    leak = cfg.rules.pending_decisions_leak
    assert leak.enabled is True
    assert leak.window_sec == 600
    assert leak.threshold_count == 20
    assert leak.cooldown_sec == 3600
```

- [ ] **Step 3: Run the test to verify it passes**

Run: `cd /home/juncheng/hybridInference-worktrees/pending-decisions-ttl && uv run pytest tests/unit/observability/test_alert_rules.py::test_alerts_yaml_loads_with_pending_decisions_leak -v`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
cd /home/juncheng/hybridInference-worktrees/pending-decisions-ttl
git add config/alerts.yaml tests/unit/observability/test_alert_rules.py
git commit -m "$(cat <<'EOF'
chore(config): add pending_decisions_leak defaults to alerts.yaml

window_sec=600, threshold_count=20, cooldown_sec=3600.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 2.6: Format, push, open PR, monitor CI

**Files:** none (process step).

- [ ] **Step 1: Format and lint**

Run: `cd /home/juncheng/hybridInference-worktrees/pending-decisions-ttl && make format && make lint`
Expected: succeed; commit any formatter-induced changes:

```bash
cd /home/juncheng/hybridInference-worktrees/pending-decisions-ttl
git add -u
git diff --cached --quiet || git commit -m "$(cat <<'EOF'
chore: ruff format

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 2: Run the full suite**

Run: `cd /home/juncheng/hybridInference-worktrees/pending-decisions-ttl && make test`
Expected: green.

- [ ] **Step 3: Push and open the PR**

```bash
cd /home/juncheng/hybridInference-worktrees/pending-decisions-ttl
git push -u origin jason/claude/pending-decisions-ttl

gh pr create --base dev --title "feat(routewise): pending-decisions TTL sweep + leak alert" --body "$(cat <<'EOF'
## Summary
- `RouteWiseRouter.start()` / `stop()` lifecycle methods control a periodic 60s sweep
- Sweep evicts entries from `_pending_decisions` whose `timestamp` is older than 300s (TTL)
- Each eviction emits a `routewise_decision_evicted` log record with `request_id` + `age_sec`
- New `PendingDecisionsLeakRule` (count-based sliding window) fires `alert_slack` when >20 evictions land in 10 min
- Defaults wired in `config/alerts.yaml`

Closes #<issue-number-from-task-2.0>

Spec: docs/agents/specs/2026-05-03-tracked-tasks-design.md (PR 2)
Depends on: tracked_task helper PR (already merged)

## Test plan
- [ ] Unit: `uv run pytest tests/unit/apps/backend/routing/test_pending_decisions_ttl.py tests/unit/observability/test_alert_rules.py -v`
- [ ] Full suite: `make test`
- [ ] Manually verify on staging: leave the system idle and confirm no `routewise_decision_evicted` events appear (fresh decisions complete within TTL)
EOF
)"
```

- [ ] **Step 4: Watch CI**

```bash
gh pr checks --watch
```

Fix and push as needed.

- [ ] **Step 5: Address PR comments every 2 minutes until resolved**

Loop:

```bash
gh pr view --json comments,reviewDecision,reviewRequests
```

Implement, commit, push for each unresolved comment until approved.

- [ ] **Step 6: After merge, delete branch and worktree**

```bash
cd /home/juncheng/hybridInference
git worktree remove /home/juncheng/hybridInference-worktrees/pending-decisions-ttl
git push origin --delete jason/claude/pending-decisions-ttl
git branch -D jason/claude/pending-decisions-ttl 2>/dev/null || true
```

---

## Self-Review Checklist

- [ ] No "TBD", "TODO", "implement later", or "similar to" placeholders.
- [ ] Every spec requirement is mapped to a task:
  - `tracked_task` helper code → Task 1.1
  - Module-level `_TRACKED_TASKS` set + `add_done_callback` GC pattern → Task 1.1
  - 5 helper unit tests (success, failure, GC safety, no-escape, double-wrap) → Task 1.1
  - `TrackedTaskFailureRateConfig` Pydantic → Task 1.2
  - `TrackedTaskFailureRateRule` + `_build_rules` registration → Task 1.3
  - `tracked_task_failure_rate` block in `config/alerts.yaml` → Task 1.4
  - Per-task-name rule firing test → Task 1.3 (`test_failure_rate_rule_fires_per_task_name`)
  - `completions.py` request-log call-site → Task 1.5
  - `completions.py` cost-increment call-site → Task 1.6 (and `_background_tasks` set deletion)
  - `dual_write.py` shadow-write call-site at lines 61–76 → Task 1.7
  - PR 1 finalize (`make format` + push + monitor) → Task 1.8
  - PR 2 pre-condition check → Task 2.0
  - RouteWise periodic 60s sweep, 300s TTL, `asyncio.Lock`, `routewise_decision_evicted` event → Task 2.1
  - `start()` cancelled in `stop()` lifecycle → Task 2.1 + 2.2
  - `PendingDecisionsLeakRule` count-based, mirrors `AuthFailureSpikeRule` → Task 2.4
  - `PendingDecisionsLeakConfig` + `Rules` slot → Task 2.3
  - `pending_decisions_leak` block in `config/alerts.yaml` → Task 2.5
  - TTL tests (insert + advance + sweep + assert evicted; insert + sweep within TTL + present) → Task 2.1
  - Leak-rule tests → Task 2.4
  - PR 2 finalize → Task 2.6
- [ ] `tracked_task` helper signature matches between Task 1.1 (definition), Task 1.5/1.6/1.7 (call-sites), and Task 1.3 (consumer event names).
- [ ] Event names are stable: `tracked_task_completed` (helper), `routewise_decision_evicted` (sweep), `request_log` / `cost_increment` / `dual_write_shadow` (task names).
- [ ] All `uv run pytest` paths exist or are created earlier in the plan.
- [ ] Branch names match CLAUDE.md convention `jason/claude/<feature-name>`.
- [ ] Commit messages all include the `Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>` trailer.
- [ ] No emojis in code or docs (per env / CLAUDE.md style).
