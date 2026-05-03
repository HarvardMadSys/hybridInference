# Prometheus Removal & Slack Alerting Framework — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the dead Prometheus stack with an in-process Slack alerting framework that delivers 9 alerts via rolling-window log rules, in-line state-change calls, and periodic SQL tickers; then delete all Prometheus and Alertmanager code/config.

**Architecture:** Three new modules under `serving/observability/` (`alerts.py`, `log_handler.py`, `alert_rules.py`). All alerts funnel through a single async `alert_slack(...)` helper with cooldown/dedupe. Rule-based alerts read structured request log records from a `logging.Handler` queue. State-change alerts call `alert_slack(...)` directly at the source. Periodic SQL alerts run as APScheduler `IntervalTrigger` jobs. The existing `FailedRequestAlerter` is refactored to use the framework. After the framework lands, dead metric callsites are removed and Prometheus/Alertmanager infrastructure is deleted.

**Tech Stack:** Python 3.12, FastAPI, asyncpg, APScheduler, pytest + pytest-asyncio (auto mode), Pydantic v2, httpx, PyYAML.

**Spec:** [docs/superpowers/specs/2026-05-03-prometheus-removal-design.md](../specs/2026-05-03-prometheus-removal-design.md)

**Process notes (from CLAUDE.md):**
- Pull `origin/dev` before starting.
- This plan executes in three sequential PRs (Phase 1, 2, 3), each on its own feature branch (`jason/claude/observability-framework`, `jason/claude/strip-metrics-code`, `jason/claude/strip-prometheus-infra`).
- Each PR: create issue → branch in worktree → implement → `make format` → PR to `dev` → monitor CI + comments every 2 min → delete branch + worktree after merge.
- Two operational config flips (PR 2 staging-enable and PR 5 prod-enable) sit between Phase 1 and Phase 2 and after Phase 3 — see "Operational Milestones" at end of plan.

---

## File Structure

### New files (Phase 1)

| File | Responsibility |
|---|---|
| `serving/observability/alerts.py` | `alert_slack(severity, title, context, dedupe_key, cooldown_sec)` — single sink. Posts to Slack webhook, enforces in-memory cooldown/dedupe, severity → emoji. |
| `serving/observability/alert_config.py` | Pydantic models for `config/alerts.yaml`. `load_alert_config(path) -> AlertConfig`. Env-var expansion. |
| `serving/observability/log_handler.py` | `AlertingLogHandler(logging.Handler)` — captures structured records (`extra` dict + `levelname`/`name`), pushes onto bounded `asyncio.Queue` (drop-oldest on overflow). |
| `serving/observability/alert_rules.py` | `AlertEngine` — async drain task over the queue, rolling-window counters per rule, periodic SQL tickers (registered with APScheduler). |
| `serving/observability/__init__.py` | Re-exports `alert_slack`, `AlertEngine`, `AlertingLogHandler`. |
| `config/alerts.yaml` | Per-rule thresholds, cooldowns, per-provider/per-role overrides. |
| `test/unit/observability/__init__.py` | Empty. |
| `test/unit/observability/test_alerts.py` | Tests for `alerts.py`. |
| `test/unit/observability/test_alert_config.py` | Tests for config loader. |
| `test/unit/observability/test_log_handler.py` | Tests for `AlertingLogHandler`. |
| `test/unit/observability/test_alert_rules.py` | Tests for `AlertEngine` rules + tickers. |
| `test/integration/observability/test_framework_e2e.py` | Drives synthetic log records through the chain, asserts Slack-helper calls. |

### Modified files (Phase 1)

| File | Change |
|---|---|
| `serving/servers/app.py` | Install `AlertingLogHandler` on root logger after `setup_logging()` runs. |
| `serving/servers/bootstrap.py` | Start `AlertEngine` in `initialize()`; stop in `shutdown()`. Refactor `register_alerter_job` call to pass through new framework. |
| `serving/admin/failed_request_alerter.py` | Refactor `post_slack_alert(...)` body into `alerts.py`; replace inline call with `alert_slack(...)`. |
| `serving/config/settings.py` | Add `ALERTS_ENABLED: bool` (default `False`), `SLACK_ALERTS_WEBHOOK_URL: str` (alias of existing `SLACK_WEBHOOK_URL` so we don't break the existing alerter), `ALERTS_CONFIG_PATH: str` (default `config/alerts.yaml`). |
| `routing/routers.py` | In `_CircuitBreaker.on_failure` (around line 256), add `await alert_slack(...)` call on CLOSED→OPEN and HALF_OPEN→OPEN transitions. **Metric calls stay in this PR** — they're no-ops; removed in Phase 2. |
| `serving/servers/routers/health.py` | When DB store unhealthy, call `alert_slack(...)` once per cooldown period. |
| `serving/storage/postgres_log.py` | New helper `query_provider_hourly_spend(hour) -> dict[provider, decimal]` for alert #9. |
| `serving/storage/postgres_operational.py` | New helper `query_users_over_daily_threshold(thresholds) -> list[(user_id, role, daily_cost)]` for alert #8. |
| `serving/servers/concurrency.py` | When concurrency rejected, emit a structured log event (`logger.warning("concurrency_rejected", extra=...)`); rule #5 reads this. |
| `serving/servers/auth.py` | When auth fails (existing behavior), ensure structured log includes `event=auth_failure` and source IP; rule #4 reads this. |
| `.env.example` | Add `ALERTS_ENABLED=false`, `ALERTS_CONFIG_PATH=config/alerts.yaml`, `SLACK_ALERTS_WEBHOOK_URL=` (alias). |

### Modified/deleted files (Phase 2 — strip metric code)

| File | Change |
|---|---|
| `serving/observability/metrics.py` | **Delete entire file.** |
| `routing/routers.py` | Remove imports + 12 callsites; convert PROVIDER_LATENCY / API_TTFT / STREAMING_INTERRUPTION / API_FALLBACKS to structured log events. |
| `routing/routewise/router.py` | Remove imports + 9 callsites; convert ROUTEWISE_CANARY_DECISIONS to log event; delete others. |
| `routing/model_router_registry.py` | Remove imports + 1-2 callsites. |
| `serving/adapters/openai_compat.py` | Remove imports + 8 callsites; convert KEY_POOL_* to log events. |
| `serving/servers/routers/completions.py` | Remove imports + 6 callsites; pure deletes (data already in request log). |
| `serving/servers/auth.py` | Remove imports + 6 callsites; pure deletes (DATABASE_CONNECTED was no-op). |
| `serving/servers/concurrency.py` | Remove imports + 5 callsites; rule #5 already reads from log event added in Phase 1. |
| `serving/http.py` | Remove import + callsite. |
| `serving/servers/routers/anthropic_messages.py` | Remove import + callsite. |
| `serving/servers/routers/health.py` | Remove `DATABASE_CONNECTED.set(...)` callsite. |
| `serving/servers/bootstrap.py` | Remove any metrics import. |

### Deleted files (Phase 3 — strip infra)

- `infrastructure/prometheus/` (entire directory)
- `infrastructure/alertmanager/` (entire directory, includes `alert_logger.py`, `alertmanager.yml`, `alertmanager.yml.example`)
- `infrastructure/systemd/alertmanager.service`
- `infrastructure/systemd/alert-logger.service`
- `infrastructure/docker/Dockerfile.alert-logger`

### Modified files (Phase 3)

| File | Change |
|---|---|
| `infrastructure/docker/docker-compose.yml` | Remove `alertmanager` and `alert-logger` services (lines ~130-160); remove `alertmanager_data` and `alert_log_data` volumes (lines ~162-173). |
| `Makefile` | Remove any prometheus/alertmanager targets if present. |
| Docs in `docs/source/` | Remove Prometheus/Alertmanager references; add observability section pointing at the new framework. |

---

# Phase 1 — Build Framework (PR `jason/claude/observability-framework`)

### Task 1: Project setup — branch + worktree + issue

**Files:** none (process step)

- [ ] **Step 1: Pull origin/dev**

```bash
git fetch origin
git checkout dev
git pull origin dev
```

- [ ] **Step 2: Create issue on GitHub**

Title: `Replace Prometheus with in-process Slack alerting framework`
Body: link the spec at `docs/superpowers/specs/2026-05-03-prometheus-removal-design.md`. Note this is Phase 1 of 3 PRs; Phase 1 builds the framework dark.

```bash
gh issue create --title "Phase 1: in-process Slack alerting framework" \
  --body "$(cat <<'EOF'
Phase 1 of 3 — builds the framework dark (no behavior change).

Spec: docs/superpowers/specs/2026-05-03-prometheus-removal-design.md
Plan: docs/superpowers/plans/2026-05-03-prometheus-removal.md

Phase 2 (issue TBD) strips dead metric code.
Phase 3 (issue TBD) strips Prometheus/Alertmanager infra.
EOF
)"
```

- [ ] **Step 3: Create worktree on feature branch**

```bash
git worktree add ../hybridInference-obs jason/claude/observability-framework
cd ../hybridInference-obs
```

- [ ] **Step 4: Verify clean tree + tests pass before changes**

```bash
make test 2>&1 | tail -20
```

Expected: all tests pass on clean checkout.

---

### Task 2: Settings additions

**Files:**
- Modify: `serving/config/settings.py` (add `ALERTS_ENABLED`, `SLACK_ALERTS_WEBHOOK_URL`, `ALERTS_CONFIG_PATH`)
- Test: `test/unit/test_settings_alerts.py` (create)

- [ ] **Step 1: Write the failing test**

Create `test/unit/test_settings_alerts.py`:

```python
import os
from unittest.mock import patch

from serving.config.settings import get_settings


def test_alerts_enabled_default_false(monkeypatch):
    monkeypatch.delenv("ALERTS_ENABLED", raising=False)
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.alerts_enabled is False


def test_slack_alerts_webhook_url_falls_back_to_slack_webhook_url(monkeypatch):
    monkeypatch.delenv("SLACK_ALERTS_WEBHOOK_URL", raising=False)
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.com/services/XYZ")
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.slack_alerts_webhook_url == "https://hooks.slack.com/services/XYZ"


def test_alerts_config_path_default(monkeypatch):
    monkeypatch.delenv("ALERTS_CONFIG_PATH", raising=False)
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.alerts_config_path == "config/alerts.yaml"
```

- [ ] **Step 2: Run test — verify it fails**

```bash
uv run pytest test/unit/test_settings_alerts.py -v
```

Expected: FAIL with `AttributeError: 'Settings' object has no attribute 'alerts_enabled'`.

- [ ] **Step 3: Add the fields to `serving/config/settings.py`**

Locate the `Settings` class (Pydantic `BaseSettings`). Find where `slack_webhook_url` is defined (around line 135) and add adjacent:

```python
    # Alerting framework (replaces Prometheus)
    alerts_enabled: bool = Field(default=False, alias="ALERTS_ENABLED")
    slack_alerts_webhook_url: str = Field(default="", alias="SLACK_ALERTS_WEBHOOK_URL")
    alerts_config_path: str = Field(default="config/alerts.yaml", alias="ALERTS_CONFIG_PATH")

    @model_validator(mode="after")
    def _alerts_webhook_fallback(self) -> "Settings":
        # If SLACK_ALERTS_WEBHOOK_URL unset, fall back to existing SLACK_WEBHOOK_URL.
        if not self.slack_alerts_webhook_url and self.slack_webhook_url:
            object.__setattr__(self, "slack_alerts_webhook_url", self.slack_webhook_url)
        return self
```

(Import `model_validator` from `pydantic` if not already.)

- [ ] **Step 4: Run test — verify pass**

```bash
uv run pytest test/unit/test_settings_alerts.py -v
```

Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add serving/config/settings.py test/unit/test_settings_alerts.py
git commit -m "feat(observability): add ALERTS_ENABLED + SLACK_ALERTS_WEBHOOK_URL settings"
```

---

### Task 3: Alert config schema (`alert_config.py`)

**Files:**
- Create: `serving/observability/alert_config.py`
- Create: `config/alerts.yaml`
- Test: `test/unit/observability/test_alert_config.py`

- [ ] **Step 1: Create empty package init**

```bash
mkdir -p test/unit/observability
touch test/unit/observability/__init__.py
```

- [ ] **Step 2: Write the failing test**

Create `test/unit/observability/test_alert_config.py`:

```python
import textwrap
from pathlib import Path

import pytest

from serving.observability.alert_config import AlertConfig, load_alert_config


def test_load_alert_config_minimal(tmp_path: Path):
    p = tmp_path / "alerts.yaml"
    p.write_text(textwrap.dedent("""
        rules:
          failed_request_rate:
            enabled: true
            window_sec: 300
            threshold_pct: 5.0
            min_samples: 50
            cooldown_sec: 900
        state_changes:
          circuit_open:
            enabled: true
            cooldown_sec: 300
        cost:
          user_overrun:
            enabled: false
            check_interval_sec: 300
            cooldown_sec: 86400
            thresholds_per_role: {free: 5.0}
    """))
    cfg = load_alert_config(p)
    assert isinstance(cfg, AlertConfig)
    assert cfg.rules.failed_request_rate.threshold_pct == 5.0
    assert cfg.state_changes.circuit_open.enabled is True
    assert cfg.cost.user_overrun.thresholds_per_role["free"] == 5.0


def test_load_alert_config_missing_file_returns_defaults(tmp_path: Path):
    cfg = load_alert_config(tmp_path / "missing.yaml")
    assert cfg.rules.failed_request_rate.enabled is True
    # All-default config should be valid.
```

- [ ] **Step 3: Run test — verify fail**

```bash
uv run pytest test/unit/observability/test_alert_config.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'serving.observability.alert_config'`.

- [ ] **Step 4: Create `serving/observability/alert_config.py`**

```python
"""Pydantic models for config/alerts.yaml plus a loader that handles env expansion."""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

_ENV_RE = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)(?::-([^}]*))?\}")


def _expand_env(value: Any) -> Any:
    """Recursively expand ${VAR} and ${VAR:-default} in strings."""
    if isinstance(value, str):
        def repl(m: re.Match[str]) -> str:
            return os.environ.get(m.group(1), m.group(2) or "")
        return _ENV_RE.sub(repl, value)
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value


class RateRule(BaseModel):
    enabled: bool = True
    window_sec: int = 300
    threshold_pct: float = 5.0
    min_samples: int = 50
    cooldown_sec: int = 900


class CountRule(BaseModel):
    enabled: bool = True
    window_sec: int = 60
    threshold_count: int = 50
    cooldown_sec: int = 600


class LatencyRule(BaseModel):
    enabled: bool = True
    window_sec: int = 600
    threshold_ms: int = 30000
    min_samples: int = 30
    cooldown_sec: int = 1800
    overrides: dict[str, dict[str, int]] = Field(default_factory=dict)


class Rules(BaseModel):
    failed_request_rate: RateRule = Field(default_factory=RateRule)
    fivexx_rate: RateRule = Field(default_factory=lambda: RateRule(threshold_pct=2.0))
    p95_latency_per_provider: LatencyRule = Field(default_factory=LatencyRule)
    auth_failure_spike: CountRule = Field(default_factory=CountRule)
    concurrency_exhausted: CountRule = Field(
        default_factory=lambda: CountRule(window_sec=300, threshold_count=100, cooldown_sec=1800)
    )


class StateChange(BaseModel):
    enabled: bool = True
    cooldown_sec: int = 300


class StateChanges(BaseModel):
    circuit_open: StateChange = Field(default_factory=StateChange)
    db_disconnect: StateChange = Field(default_factory=StateChange)


class UserOverrun(BaseModel):
    enabled: bool = True
    check_interval_sec: int = 300
    cooldown_sec: int = 86400
    thresholds_per_role: dict[str, float] = Field(
        default_factory=lambda: {"free": 5.0, "pro": 50.0, "internal": 500.0}
    )


class ProviderHourlySpend(BaseModel):
    enabled: bool = True
    check_interval_sec: int = 300
    cooldown_sec: int = 3600
    budgets: dict[str, float] = Field(default_factory=dict)


class CostSection(BaseModel):
    user_overrun: UserOverrun = Field(default_factory=UserOverrun)
    provider_hourly_spend: ProviderHourlySpend = Field(default_factory=ProviderHourlySpend)


class AlertConfig(BaseModel):
    rules: Rules = Field(default_factory=Rules)
    state_changes: StateChanges = Field(default_factory=StateChanges)
    cost: CostSection = Field(default_factory=CostSection)


def load_alert_config(path: str | Path) -> AlertConfig:
    p = Path(path)
    if not p.exists():
        log.warning("alerts config not found at %s; using defaults", p)
        return AlertConfig()
    raw = yaml.safe_load(p.read_text()) or {}
    raw = _expand_env(raw)
    return AlertConfig.model_validate(raw)
```

- [ ] **Step 5: Create `config/alerts.yaml`** (production-ready defaults, all enabled)

```yaml
rules:
  failed_request_rate:
    enabled: true
    window_sec: 300
    threshold_pct: 5.0
    min_samples: 50
    cooldown_sec: 900
  fivexx_rate:
    enabled: true
    window_sec: 300
    threshold_pct: 2.0
    min_samples: 50
    cooldown_sec: 900
  p95_latency_per_provider:
    enabled: true
    window_sec: 600
    threshold_ms: 30000
    min_samples: 30
    cooldown_sec: 1800
    overrides: {}
  auth_failure_spike:
    enabled: true
    window_sec: 60
    threshold_count: 50
    cooldown_sec: 600
  concurrency_exhausted:
    enabled: true
    window_sec: 300
    threshold_count: 100
    cooldown_sec: 1800

state_changes:
  circuit_open:
    enabled: true
    cooldown_sec: 300
  db_disconnect:
    enabled: true
    cooldown_sec: 300

cost:
  user_overrun:
    enabled: true
    check_interval_sec: 300
    cooldown_sec: 86400
    thresholds_per_role:
      free: 5.00
      pro: 50.00
      internal: 500.00
  provider_hourly_spend:
    enabled: true
    check_interval_sec: 300
    cooldown_sec: 3600
    budgets: {}
```

- [ ] **Step 6: Run test — verify pass**

```bash
uv run pytest test/unit/observability/test_alert_config.py -v
```

Expected: 2 passed.

- [ ] **Step 7: Commit**

```bash
git add serving/observability/alert_config.py config/alerts.yaml \
        test/unit/observability/__init__.py test/unit/observability/test_alert_config.py
git commit -m "feat(observability): add AlertConfig schema and config/alerts.yaml"
```

---

### Task 4: `alert_slack(...)` helper

**Files:**
- Create: `serving/observability/alerts.py`
- Test: `test/unit/observability/test_alerts.py`

- [ ] **Step 1: Write the failing tests**

Create `test/unit/observability/test_alerts.py`:

```python
from unittest.mock import AsyncMock, patch

import pytest

from serving.observability.alerts import (
    AlertSeverity,
    alert_slack,
    reset_dedupe_state,
)


@pytest.fixture(autouse=True)
def reset_state():
    reset_dedupe_state()
    yield
    reset_dedupe_state()


@pytest.mark.asyncio
async def test_alert_slack_no_op_when_webhook_unset(monkeypatch):
    monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "")
    with patch("serving.observability.alerts._post_to_slack", new=AsyncMock()) as mock_post:
        await alert_slack(AlertSeverity.ERROR, "test", {"k": "v"})
        mock_post.assert_not_called()


@pytest.mark.asyncio
async def test_alert_slack_posts_when_webhook_set(monkeypatch):
    monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://hooks.slack.com/x")
    with patch("serving.observability.alerts._post_to_slack", new=AsyncMock(return_value=True)) as mock_post:
        await alert_slack(AlertSeverity.ERROR, "test title", {"foo": "bar"})
        mock_post.assert_awaited_once()
        args, _ = mock_post.call_args
        url, message = args
        assert url == "https://hooks.slack.com/x"
        assert "test title" in message
        assert "foo" in message and "bar" in message


@pytest.mark.asyncio
async def test_alert_slack_dedupes_within_cooldown(monkeypatch):
    monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://hooks.slack.com/x")
    with patch("serving.observability.alerts._post_to_slack", new=AsyncMock(return_value=True)) as mock_post:
        await alert_slack(AlertSeverity.WARN, "t", {}, dedupe_key="K", cooldown_sec=60)
        await alert_slack(AlertSeverity.WARN, "t", {}, dedupe_key="K", cooldown_sec=60)
    assert mock_post.await_count == 1


@pytest.mark.asyncio
async def test_alert_slack_fires_again_after_cooldown(monkeypatch):
    monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://hooks.slack.com/x")
    fake_now = [1000.0]

    def now():
        return fake_now[0]

    with patch("serving.observability.alerts._post_to_slack", new=AsyncMock(return_value=True)) as mock_post, \
         patch("serving.observability.alerts._monotonic", new=now):
        await alert_slack(AlertSeverity.WARN, "t", {}, dedupe_key="K", cooldown_sec=60)
        fake_now[0] += 61
        await alert_slack(AlertSeverity.WARN, "t", {}, dedupe_key="K", cooldown_sec=60)
    assert mock_post.await_count == 2


@pytest.mark.asyncio
async def test_alert_slack_swallows_post_errors(monkeypatch):
    monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://hooks.slack.com/x")
    with patch("serving.observability.alerts._post_to_slack", new=AsyncMock(side_effect=RuntimeError("boom"))):
        await alert_slack(AlertSeverity.ERROR, "t", {})  # must not raise
```

- [ ] **Step 2: Run test — verify fail**

```bash
uv run pytest test/unit/observability/test_alerts.py -v
```

Expected: ImportError.

- [ ] **Step 3: Implement `serving/observability/alerts.py`**

```python
"""Single-sink Slack alerting helper used by all alert paths.

Reuses the post-to-webhook pattern previously embedded in
serving/admin/failed_request_alerter.py.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import enum
import logging
import os
import socket
import time
from collections import defaultdict
from typing import Any

import httpx

log = logging.getLogger(__name__)

_HOST = socket.gethostname()
_DEDUPE_LOCK = asyncio.Lock()
_LAST_FIRED: dict[str, float] = defaultdict(float)


def _monotonic() -> float:
    return time.monotonic()


def reset_dedupe_state() -> None:
    """Test helper — clears in-memory dedupe table."""
    _LAST_FIRED.clear()


class AlertSeverity(str, enum.Enum):
    CRITICAL = "critical"
    ERROR = "error"
    WARN = "warn"
    INFO = "info"


_EMOJI = {
    AlertSeverity.CRITICAL: "🚨",
    AlertSeverity.ERROR: "❌",
    AlertSeverity.WARN: "⚠️",
    AlertSeverity.INFO: "ℹ️",
}


def _format_message(severity: AlertSeverity, title: str, context: dict[str, Any]) -> str:
    ts = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    lines = [
        f"{_EMOJI[severity]} [{severity.value}] {title}",
        f"At: {ts}  Host: {_HOST}",
    ]
    for k, v in context.items():
        lines.append(f"{k}: {v}")
    return "\n".join(lines)


async def _post_to_slack(webhook_url: str, message: str) -> bool:
    """Post `{"text": message}` to Slack incoming webhook. Returns True on 2xx."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(webhook_url, json={"text": message})
        return 200 <= resp.status_code < 300
    except Exception:
        log.exception("slack webhook post failed")
        return False


async def alert_slack(
    severity: AlertSeverity,
    title: str,
    context: dict[str, Any],
    *,
    dedupe_key: str | None = None,
    cooldown_sec: int = 300,
) -> bool:
    """Send a Slack alert. No-op if webhook unset or within cooldown.

    Returns True if a message was actually sent, False otherwise.
    """
    webhook_url = os.environ.get("SLACK_ALERTS_WEBHOOK_URL", "") or os.environ.get("SLACK_WEBHOOK_URL", "")
    if not webhook_url:
        return False

    key = dedupe_key or f"{severity.value}:{title}"
    now = _monotonic()
    async with _DEDUPE_LOCK:
        last = _LAST_FIRED.get(key, 0.0)
        if now - last < cooldown_sec:
            return False
        _LAST_FIRED[key] = now

    message = _format_message(severity, title, context)
    try:
        return await _post_to_slack(webhook_url, message)
    except Exception:
        log.exception("alert_slack post raised; suppressing")
        return False
```

- [ ] **Step 4: Run test — verify pass**

```bash
uv run pytest test/unit/observability/test_alerts.py -v
```

Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add serving/observability/alerts.py test/unit/observability/test_alerts.py
git commit -m "feat(observability): add alert_slack() helper with cooldown/dedupe"
```

---

### Task 5: `AlertingLogHandler`

**Files:**
- Create: `serving/observability/log_handler.py`
- Test: `test/unit/observability/test_log_handler.py`

- [ ] **Step 1: Write failing tests**

Create `test/unit/observability/test_log_handler.py`:

```python
import asyncio
import logging

import pytest

from serving.observability.log_handler import AlertingLogHandler


@pytest.mark.asyncio
async def test_handler_pushes_records_to_queue():
    handler = AlertingLogHandler(maxsize=10)
    logger = logging.getLogger("test.alerts.handler.1")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.info("hello", extra={"foo": "bar"})

    rec = await asyncio.wait_for(handler.queue.get(), timeout=1.0)
    assert rec.getMessage() == "hello"
    assert getattr(rec, "foo", None) == "bar"


@pytest.mark.asyncio
async def test_handler_drops_oldest_on_overflow():
    handler = AlertingLogHandler(maxsize=2)
    logger = logging.getLogger("test.alerts.handler.2")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    for i in range(5):
        logger.info("m%d" % i)

    # Queue holds latest 2, dropped count = 3
    msgs = []
    while not handler.queue.empty():
        rec = handler.queue.get_nowait()
        msgs.append(rec.getMessage())
    assert len(msgs) == 2
    assert handler.dropped_count == 3
```

- [ ] **Step 2: Run — verify fail**

```bash
uv run pytest test/unit/observability/test_log_handler.py -v
```

- [ ] **Step 3: Implement `serving/observability/log_handler.py`**

```python
"""logging.Handler that pushes records onto a bounded asyncio.Queue.

The AlertEngine drains the queue and applies rule-based alerts.
"""
from __future__ import annotations

import asyncio
import logging


class AlertingLogHandler(logging.Handler):
    def __init__(self, maxsize: int = 10_000) -> None:
        super().__init__(level=logging.DEBUG)
        self.queue: asyncio.Queue[logging.LogRecord] = asyncio.Queue(maxsize=maxsize)
        self.dropped_count = 0

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.queue.put_nowait(record)
        except asyncio.QueueFull:
            # Drop oldest, push newest.
            try:
                self.queue.get_nowait()
                self.dropped_count += 1
                self.queue.put_nowait(record)
            except Exception:
                self.dropped_count += 1
```

- [ ] **Step 4: Run — verify pass**

```bash
uv run pytest test/unit/observability/test_log_handler.py -v
```

- [ ] **Step 5: Commit**

```bash
git add serving/observability/log_handler.py test/unit/observability/test_log_handler.py
git commit -m "feat(observability): add AlertingLogHandler with bounded drop-oldest queue"
```

---

### Task 6: `AlertEngine` skeleton

**Files:**
- Create: `serving/observability/alert_rules.py`
- Test: `test/unit/observability/test_alert_rules.py`

- [ ] **Step 1: Write failing test for engine lifecycle**

Create `test/unit/observability/test_alert_rules.py`:

```python
import asyncio
import logging

import pytest

from serving.observability.alert_config import AlertConfig
from serving.observability.alert_rules import AlertEngine
from serving.observability.log_handler import AlertingLogHandler


@pytest.mark.asyncio
async def test_engine_starts_and_stops_cleanly():
    handler = AlertingLogHandler(maxsize=10)
    cfg = AlertConfig()
    engine = AlertEngine(handler=handler, config=cfg, scheduler=None, op_store=None, log_store=None)

    await engine.start()
    assert engine.is_running()
    await engine.stop()
    assert not engine.is_running()
```

- [ ] **Step 2: Run — verify fail**

- [ ] **Step 3: Implement skeleton in `serving/observability/alert_rules.py`**

```python
"""AlertEngine: drains AlertingLogHandler queue, runs rule-based alerts,
and schedules periodic SQL alerts via APScheduler.

Rules implemented in subsequent tasks add themselves via _RULES list.
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Protocol

from serving.observability.alert_config import AlertConfig
from serving.observability.log_handler import AlertingLogHandler

if TYPE_CHECKING:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    from serving.storage.base import LogStore, OperationalStore

log = logging.getLogger(__name__)


class _Rule(Protocol):
    name: str

    async def on_record(self, record: logging.LogRecord) -> None: ...


class AlertEngine:
    def __init__(
        self,
        *,
        handler: AlertingLogHandler,
        config: AlertConfig,
        scheduler: "AsyncIOScheduler | None",
        op_store: "OperationalStore | None",
        log_store: "LogStore | None",
    ) -> None:
        self._handler = handler
        self._config = config
        self._scheduler = scheduler
        self._op_store = op_store
        self._log_store = log_store
        self._task: asyncio.Task[None] | None = None
        self._rules: list[_Rule] = []
        self._scheduled_jobs: list[Any] = []

    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        # Subclasses register rules + scheduled jobs here in later tasks.
        self._build_rules()
        self._schedule_periodic_jobs()
        self._task = asyncio.create_task(self._drain(), name="AlertEngine.drain")
        log.info("AlertEngine started with %d rules and %d periodic jobs",
                 len(self._rules), len(self._scheduled_jobs))

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        for job in self._scheduled_jobs:
            try:
                job.remove()
            except Exception:
                log.exception("failed to remove alert job")
        self._task = None

    def _build_rules(self) -> None:
        # Rules are added in Tasks 7-11; left empty here.
        return

    def _schedule_periodic_jobs(self) -> None:
        # Scheduled jobs are added in Tasks 14-15; left empty here.
        return

    async def _drain(self) -> None:
        try:
            while True:
                record = await self._handler.queue.get()
                for rule in self._rules:
                    try:
                        await rule.on_record(record)
                    except Exception:
                        log.exception("rule %s raised", rule.name)
        except asyncio.CancelledError:
            raise
```

- [ ] **Step 4: Run — verify pass**

- [ ] **Step 5: Commit**

```bash
git add serving/observability/alert_rules.py test/unit/observability/test_alert_rules.py
git commit -m "feat(observability): AlertEngine skeleton (drain task + lifecycle)"
```

---

### Task 7: Rule 1 — Failed-request rate

Refactors the existing `FailedRequestAlerter` into the framework. Rule reads request log records (status_code) instead of querying the DB.

**Files:**
- Modify: `serving/observability/alert_rules.py` (add `FailedRequestRateRule`)
- Test: extend `test/unit/observability/test_alert_rules.py`

- [ ] **Step 1: Write failing test**

Add to `test/unit/observability/test_alert_rules.py`:

```python
from unittest.mock import AsyncMock, patch
import logging
import time


def _fake_record(status_code: int, provider: str = "openai", model: str = "gpt-4") -> logging.LogRecord:
    rec = logging.LogRecord(
        name="serving.servers.middleware.request_log",
        level=logging.INFO, pathname="", lineno=0,
        msg="http_request", args=None, exc_info=None,
    )
    rec.status_code = status_code
    rec.provider = provider
    rec.model = model
    rec.duration_ms = 100
    return rec


@pytest.mark.asyncio
async def test_failed_request_rate_fires_on_threshold(monkeypatch):
    monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://x")
    from serving.observability.alerts import reset_dedupe_state
    reset_dedupe_state()

    cfg = AlertConfig()
    cfg.rules.failed_request_rate.window_sec = 60
    cfg.rules.failed_request_rate.threshold_pct = 5.0
    cfg.rules.failed_request_rate.min_samples = 10
    cfg.rules.failed_request_rate.cooldown_sec = 1

    handler = AlertingLogHandler(maxsize=1000)
    engine = AlertEngine(handler=handler, config=cfg, scheduler=None, op_store=None, log_store=None)

    with patch("serving.observability.alert_rules.alert_slack", new=AsyncMock()) as mock_alert:
        await engine.start()
        try:
            # 10 successes
            for _ in range(10):
                handler.queue.put_nowait(_fake_record(200))
            # 2 failures => 16.7% failure
            for _ in range(2):
                handler.queue.put_nowait(_fake_record(500))
            # Allow drain
            for _ in range(50):
                if mock_alert.await_count > 0:
                    break
                await asyncio.sleep(0.01)
            assert mock_alert.await_count == 1
        finally:
            await engine.stop()
```

- [ ] **Step 2: Run — verify fail**

- [ ] **Step 3: Implement `FailedRequestRateRule`** in `serving/observability/alert_rules.py` — add to file, register in `_build_rules`:

```python
import collections
import logging
import time

from serving.observability.alerts import AlertSeverity, alert_slack
from serving.observability.alert_config import RateRule


class _SlidingWindow:
    """Simple time-bucketed sliding window holding (ts, value) tuples."""

    def __init__(self, window_sec: int) -> None:
        self.window_sec = window_sec
        self._items: collections.deque[tuple[float, dict]] = collections.deque()

    def add(self, ts: float, value: dict) -> None:
        self._items.append((ts, value))
        self._evict(ts)

    def _evict(self, now: float) -> None:
        cutoff = now - self.window_sec
        while self._items and self._items[0][0] < cutoff:
            self._items.popleft()

    def items(self, now: float) -> list[dict]:
        self._evict(now)
        return [v for _, v in self._items]


class FailedRequestRateRule:
    name = "failed_request_rate"

    def __init__(self, cfg: RateRule) -> None:
        self._cfg = cfg
        self._window = _SlidingWindow(cfg.window_sec)

    async def on_record(self, record: logging.LogRecord) -> None:
        if not self._cfg.enabled:
            return
        if record.name != "serving.servers.middleware.request_log":
            return
        status = getattr(record, "status_code", None)
        if status is None:
            return
        now = time.time()
        self._window.add(now, {"status": int(status), "provider": getattr(record, "provider", None)})
        items = self._window.items(now)
        if len(items) < self._cfg.min_samples:
            return
        failed = sum(1 for it in items if it["status"] >= 400)
        pct = (failed / len(items)) * 100.0
        if pct < self._cfg.threshold_pct:
            return
        # Top providers among failures
        prov_counts: dict[str, int] = collections.Counter(
            it["provider"] for it in items if it["status"] >= 400 and it["provider"]
        )
        top = ", ".join(f"{p} ({c})" for p, c in prov_counts.most_common(3))
        await alert_slack(
            AlertSeverity.ERROR,
            "Failed-request rate exceeded",
            {
                "rate": f"{pct:.1f}% ({failed} of {len(items)} requests, last {self._cfg.window_sec}s)",
                "top_providers": top or "n/a",
            },
            dedupe_key="failed_request_rate",
            cooldown_sec=self._cfg.cooldown_sec,
        )
```

In `AlertEngine._build_rules`, add:

```python
        self._rules.append(FailedRequestRateRule(self._config.rules.failed_request_rate))
```

- [ ] **Step 4: Run — verify pass**

- [ ] **Step 5: Commit**

```bash
git add serving/observability/alert_rules.py test/unit/observability/test_alert_rules.py
git commit -m "feat(observability): rule 1 — failed-request rate over sliding window"
```

---

### Task 8: Rule 2 — 5xx rate spike

Same shape as Rule 1, but only counts `status >= 500`.

**Files:**
- Modify: `serving/observability/alert_rules.py` (add `FivexxRateRule`)
- Test: extend `test/unit/observability/test_alert_rules.py`

- [ ] **Step 1: Test — drive 50 records, 2 of them 500, expect alert at threshold 2.0%.**

(Mirror Rule 1 test; threshold checks failures with `status >= 500`.)

- [ ] **Step 2: Run — fail**

- [ ] **Step 3: Implement** — copy `FailedRequestRateRule`, change predicate to `status >= 500`, dedupe key `fivexx_rate`, title "5xx rate exceeded".

- [ ] **Step 4: Run — pass**

- [ ] **Step 5: Commit** with message `feat(observability): rule 2 — 5xx rate spike`

---

### Task 9: Rule 3 — p95 latency per provider

**Files:**
- Modify: `serving/observability/alert_rules.py` (add `P95LatencyRule`)
- Test: extend the rules test

- [ ] **Step 1: Test** — 30 records for provider="openai" with `duration_ms` from 1000 to 30000 (p95 ~28500); set threshold 25000 → expect alert. Re-run with different provider in `overrides` to confirm per-provider override works.

- [ ] **Step 2: Run — fail**

- [ ] **Step 3: Implement**:

```python
class P95LatencyRule:
    name = "p95_latency_per_provider"

    def __init__(self, cfg) -> None:
        self._cfg = cfg
        self._windows: dict[str, _SlidingWindow] = {}

    async def on_record(self, record: logging.LogRecord) -> None:
        if not self._cfg.enabled:
            return
        if record.name != "serving.servers.middleware.request_log":
            return
        provider = getattr(record, "provider", None) or "unknown"
        duration_ms = getattr(record, "duration_ms", None)
        if duration_ms is None:
            return
        win = self._windows.setdefault(provider, _SlidingWindow(self._cfg.window_sec))
        now = time.time()
        win.add(now, {"duration_ms": int(duration_ms)})
        items = win.items(now)
        if len(items) < self._cfg.min_samples:
            return
        sorted_durations = sorted(it["duration_ms"] for it in items)
        idx = int(0.95 * (len(sorted_durations) - 1))
        p95 = sorted_durations[idx]
        threshold = self._cfg.overrides.get(provider, {}).get("threshold_ms", self._cfg.threshold_ms)
        if p95 < threshold:
            return
        p99_idx = int(0.99 * (len(sorted_durations) - 1))
        p99 = sorted_durations[p99_idx]
        await alert_slack(
            AlertSeverity.WARN,
            f"p95 latency exceeded for provider {provider}",
            {
                "provider": provider,
                "p95_ms": p95,
                "p99_ms": p99,
                "samples": len(items),
                "window_sec": self._cfg.window_sec,
                "threshold_ms": threshold,
            },
            dedupe_key=f"p95_latency:{provider}",
            cooldown_sec=self._cfg.cooldown_sec,
        )
```

Register in `_build_rules`.

- [ ] **Step 4: Run — pass**

- [ ] **Step 5: Commit** `feat(observability): rule 3 — p95 latency per provider`

---

### Task 10: Rule 4 — Auth failure spike

Reads structured log records emitted at auth-failure sites. First, enrich the auth failure log emission.

**Files:**
- Modify: `serving/servers/auth.py` (ensure structured `auth_failure` log event with `extra={"event": "auth_failure", "remote_ip": ..., "key_prefix": ...}`)
- Modify: `serving/observability/alert_rules.py` (add `AuthFailureSpikeRule`)
- Test: extend rules test

- [ ] **Step 1: Test** — 60 records over 1s with `event=auth_failure`; threshold 50 → expect alert.

- [ ] **Step 2: Run — fail**

- [ ] **Step 3a: In `serving/servers/auth.py`**, locate the auth failure path (the function that returns 401). Replace any pure metric increment with:

```python
log.warning(
    "auth_failure",
    extra={
        "event": "auth_failure",
        "remote_ip": remote_ip,
        "key_prefix": api_key[:6] if api_key else None,
        "reason": reason,
    },
)
```

- [ ] **Step 3b: Implement `AuthFailureSpikeRule`** in `alert_rules.py`:

```python
class AuthFailureSpikeRule:
    name = "auth_failure_spike"

    def __init__(self, cfg) -> None:
        self._cfg = cfg
        self._window = _SlidingWindow(cfg.window_sec)

    async def on_record(self, record: logging.LogRecord) -> None:
        if not self._cfg.enabled:
            return
        if getattr(record, "event", None) != "auth_failure":
            return
        now = time.time()
        self._window.add(now, {"remote_ip": getattr(record, "remote_ip", None),
                               "key_prefix": getattr(record, "key_prefix", None)})
        items = self._window.items(now)
        if len(items) <= self._cfg.threshold_count:
            return
        ip_counts = collections.Counter(it["remote_ip"] for it in items if it["remote_ip"])
        key_counts = collections.Counter(it["key_prefix"] for it in items if it["key_prefix"])
        await alert_slack(
            AlertSeverity.WARN,
            "Auth failure spike",
            {
                "count": len(items),
                "window_sec": self._cfg.window_sec,
                "top_ips": ", ".join(f"{ip} ({c})" for ip, c in ip_counts.most_common(3)) or "n/a",
                "top_key_prefixes": ", ".join(f"{p} ({c})" for p, c in key_counts.most_common(3)) or "n/a",
            },
            dedupe_key="auth_failure_spike",
            cooldown_sec=self._cfg.cooldown_sec,
        )
```

Register in `_build_rules`.

- [ ] **Step 4: Run — pass**

- [ ] **Step 5: Commit** `feat(observability): rule 4 — auth failure spike (+ structured log event)`

---

### Task 11: Rule 5 — Concurrency-exhausted

Reads structured log records emitted when user concurrency limit rejects.

**Files:**
- Modify: `serving/servers/concurrency.py` (emit structured log event on rejection)
- Modify: `serving/observability/alert_rules.py` (add `ConcurrencyExhaustedRule`)
- Test: extend rules test

- [ ] **Step 1: Test** — 110 `concurrency_rejected` records over 5 min; threshold 100 → alert.

- [ ] **Step 2: Run — fail**

- [ ] **Step 3a: In `serving/servers/concurrency.py`** locate the rejection path (around lines 106-122). Replace metric increment with:

```python
log.warning(
    "concurrency_rejected",
    extra={
        "event": "concurrency_rejected",
        "user_id": user_id,
        "role": role,
    },
)
```

- [ ] **Step 3b: Implement `ConcurrencyExhaustedRule`** in `alert_rules.py` — same shape as `AuthFailureSpikeRule` but matches `event == "concurrency_rejected"` and groups by `user_id`/`role` instead of IP/key.

- [ ] **Step 4: Run — pass**

- [ ] **Step 5: Commit** `feat(observability): rule 5 — concurrency-exhausted spike`

---

### Task 12: State-change alert 6 — Provider circuit-open

**Files:**
- Modify: `routing/routers.py` (`_CircuitBreaker.on_failure`)
- Test: `test/unit/routing/test_circuit_breaker_alert.py` (create)

- [ ] **Step 1: Write failing test**

```python
import asyncio
import pytest
from unittest.mock import AsyncMock, patch

from routing.routers import _CircuitBreaker, _CircuitState


@pytest.mark.asyncio
async def test_circuit_open_fires_alert(monkeypatch):
    monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://x")
    from serving.observability.alerts import reset_dedupe_state
    reset_dedupe_state()

    cb = _CircuitBreaker(
        endpoint_id="openai-prod",
        provider="openai",
        failure_threshold=2,
        cooldown_seconds=30,
        min_availability=0.7,
    )
    with patch("routing.routers.alert_slack", new=AsyncMock()) as mock_alert:
        cb.on_failure(reason="upstream_500")
        cb.on_failure(reason="upstream_500")
        assert cb.state == _CircuitState.OPEN
        # alert_slack is fired via asyncio.ensure_future — let pending tasks run
        await asyncio.sleep(0)
        mock_alert.assert_awaited_once()
```

- [ ] **Step 2: Run — fail**

- [ ] **Step 3: Implement**

In `routing/routers.py`:

1. Add imports at top: `import asyncio` (if not already present) and `from serving.observability.alerts import AlertSeverity, alert_slack`.
2. Locate `_CircuitBreaker.on_failure` (around line 256). **Keep it synchronous** — do not change its signature. After the state transition `self.state = _CircuitState.OPEN`, add a fire-and-forget Slack alert using `asyncio.ensure_future`:

```python
            try:
                asyncio.ensure_future(alert_slack(
                    AlertSeverity.ERROR,
                    "Provider circuit opened",
                    {
                        "endpoint_id": self.endpoint_id,
                        "provider": self.provider,
                        "consecutive_failures": self.consecutive_failures,
                        "availability": f"{self.availability:.2f}",
                        "reason": reason or "unknown",
                    },
                    dedupe_key=f"circuit_open:{self.endpoint_id}",
                    cooldown_sec=300,
                ))
            except RuntimeError:
                # No running event loop (e.g., unit tests outside pytest-asyncio).
                # Alert is best-effort; skip silently.
                pass
```

(Keep existing `CIRCUIT_STATE.labels(...).set(1)` no-op call for now — Phase 2 removes it.)

- [ ] **Step 4: Run — pass**

```bash
uv run pytest test/unit/routing/test_circuit_breaker_alert.py -v
```

- [ ] **Step 5: Commit** `feat(observability): alert on circuit CLOSED→OPEN transition`

---

### Task 13: State-change alert 7 — DB disconnect

**Files:**
- Modify: `serving/servers/routers/health.py` (call `alert_slack` when store unhealthy)
- Test: `test/unit/servers/test_health_alert.py` (create)

- [ ] **Step 1: Test** — call `_test_store_health` with a mock that raises; assert `alert_slack` called once with severity CRITICAL and dedupe key `db_disconnect:postgres`.

- [ ] **Step 2: Run — fail**

- [ ] **Step 3: Implement** — at the point in `_test_store_health` where unhealthy state is detected (around line 78), call:

```python
        await alert_slack(
            AlertSeverity.CRITICAL,
            "Database disconnected",
            {"db_kind": kind, "error": str(error)[:500]},
            dedupe_key=f"db_disconnect:{kind}",
            cooldown_sec=300,
        )
```

- [ ] **Step 4: Run — pass**

- [ ] **Step 5: Commit** `feat(observability): alert on DB disconnect`

---

### Task 14: Periodic alert 8 — User cost overrun

**Files:**
- Modify: `serving/storage/postgres_operational.py` (add `query_users_over_daily_threshold`)
- Modify: `serving/observability/alert_rules.py` (add `UserCostOverrunJob`)

- [ ] **Step 1: Test** — extend `test_alert_rules.py` to mock an operational store whose `query_users_over_daily_threshold` returns one row exceeding the role threshold; manually invoke the job; assert `alert_slack` called.

- [ ] **Step 2: Run — fail**

- [ ] **Step 3a: In `serving/storage/postgres_operational.py`**, add:

```python
    async def query_users_over_daily_threshold(
        self,
        thresholds: dict[str, float],
    ) -> list[tuple[str, str, float]]:
        """Return users whose daily_cost crossed the per-role threshold today."""
        # Build CASE expression for thresholds
        cases = "\n".join(
            f"WHEN role = '{role}' THEN {threshold}"
            for role, threshold in thresholds.items()
        )
        sql = f"""
            SELECT user_id, role, daily_cost
            FROM users
            WHERE daily_cost > CASE
                {cases}
                ELSE 1e18
            END
            ORDER BY daily_cost DESC
            LIMIT 100
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(sql)
        return [(r["user_id"], r["role"], float(r["daily_cost"])) for r in rows]
```

- [ ] **Step 3b: Add `UserCostOverrunJob`** to `alert_rules.py` (registered via `_schedule_periodic_jobs` using `IntervalTrigger(seconds=cfg.check_interval_sec)`):

```python
import datetime as dt
from apscheduler.triggers.interval import IntervalTrigger


class UserCostOverrunJob:
    name = "user_cost_overrun"

    def __init__(self, cfg, op_store) -> None:
        self._cfg = cfg
        self._op_store = op_store

    async def run(self) -> None:
        if not self._cfg.enabled or self._op_store is None:
            return
        rows = await self._op_store.query_users_over_daily_threshold(self._cfg.thresholds_per_role)
        today = dt.date.today().isoformat()
        for user_id, role, daily_cost in rows:
            await alert_slack(
                AlertSeverity.WARN,
                "User cost overrun",
                {
                    "user_id": user_id,
                    "role": role,
                    "daily_cost": f"${daily_cost:.2f}",
                    "threshold": f"${self._cfg.thresholds_per_role.get(role, 0):.2f}",
                },
                dedupe_key=f"cost_overrun:{user_id}:{today}",
                cooldown_sec=self._cfg.cooldown_sec,
            )
```

- [ ] **Step 3c: Register in `AlertEngine._schedule_periodic_jobs`:**

```python
        if self._scheduler and self._op_store:
            job = UserCostOverrunJob(self._config.cost.user_overrun, self._op_store)
            scheduled = self._scheduler.add_job(
                job.run,
                trigger=IntervalTrigger(seconds=self._config.cost.user_overrun.check_interval_sec),
                id="alert_user_cost_overrun",
                replace_existing=True,
            )
            self._scheduled_jobs.append(scheduled)
```

- [ ] **Step 4: Run — pass**

- [ ] **Step 5: Commit** `feat(observability): periodic alert 8 — user cost overrun`

---

### Task 15: Periodic alert 9 — Per-provider hourly spend

**Files:**
- Modify: `serving/storage/postgres_log.py` (add `query_provider_hourly_spend`)
- Modify: `serving/observability/alert_rules.py` (add `ProviderHourlySpendJob`)

- [ ] **Step 1: Test** — mock log store returns `{"openai": 150.0}`; `budgets={"openai": 100.0}`; expect alert.

- [ ] **Step 2: Run — fail**

- [ ] **Step 3a: In `serving/storage/postgres_log.py`**, add:

```python
    async def query_provider_hourly_spend(self, hour_iso: str) -> dict[str, float]:
        """Return per-provider total cost for the given hour bucket."""
        sql = """
            SELECT provider, COALESCE(SUM(cost), 0) AS total
            FROM api_logs
            WHERE date_trunc('hour', timestamp) = $1::timestamp
            GROUP BY provider
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(sql, hour_iso)
        return {r["provider"]: float(r["total"]) for r in rows if r["provider"]}
```

- [ ] **Step 3b: Add `ProviderHourlySpendJob`**:

```python
class ProviderHourlySpendJob:
    name = "provider_hourly_spend"

    def __init__(self, cfg, log_store) -> None:
        self._cfg = cfg
        self._log_store = log_store

    async def run(self) -> None:
        if not self._cfg.enabled or self._log_store is None:
            return
        now = dt.datetime.now(dt.timezone.utc).replace(minute=0, second=0, microsecond=0)
        hour_iso = now.isoformat()
        spends = await self._log_store.query_provider_hourly_spend(hour_iso)
        for provider, spend in spends.items():
            budget = self._cfg.budgets.get(provider)
            if budget is None or spend < budget:
                continue
            await alert_slack(
                AlertSeverity.WARN,
                f"Provider hourly spend exceeded budget for {provider}",
                {
                    "provider": provider,
                    "hourly_spend": f"${spend:.2f}",
                    "budget": f"${budget:.2f}",
                    "hour": hour_iso,
                },
                dedupe_key=f"provider_spend:{provider}:{hour_iso}",
                cooldown_sec=self._cfg.cooldown_sec,
            )
```

- [ ] **Step 3c: Register** in `_schedule_periodic_jobs` mirror of Task 14.

- [ ] **Step 4: Run — pass**

- [ ] **Step 5: Commit** `feat(observability): periodic alert 9 — per-provider hourly spend`

---

### Task 16: Wire AlertEngine + AlertingLogHandler into bootstrap

**Files:**
- Modify: `serving/servers/bootstrap.py` (start AlertEngine in `initialize`, stop in `shutdown`)
- Modify: `serving/servers/app.py` (install AlertingLogHandler on root logger)
- Modify: `serving/servers/services.py` (add `alert_engine: AlertEngine | None` to `AppServices`)

- [ ] **Step 1: Test** — integration test under `test/integration/observability/test_framework_e2e.py`:

```python
import asyncio
import logging

import pytest
from unittest.mock import AsyncMock, patch

from serving.observability.alert_config import AlertConfig
from serving.observability.alert_rules import AlertEngine
from serving.observability.log_handler import AlertingLogHandler


@pytest.mark.asyncio
async def test_full_chain_logs_to_slack(monkeypatch):
    monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://x")
    from serving.observability.alerts import reset_dedupe_state
    reset_dedupe_state()

    handler = AlertingLogHandler(maxsize=1000)
    logging.getLogger("serving.servers.middleware.request_log").addHandler(handler)

    cfg = AlertConfig()
    cfg.rules.failed_request_rate.window_sec = 60
    cfg.rules.failed_request_rate.threshold_pct = 5.0
    cfg.rules.failed_request_rate.min_samples = 10
    cfg.rules.failed_request_rate.cooldown_sec = 1

    engine = AlertEngine(handler=handler, config=cfg, scheduler=None, op_store=None, log_store=None)
    log = logging.getLogger("serving.servers.middleware.request_log")
    log.setLevel(logging.INFO)

    with patch("serving.observability.alert_rules.alert_slack", new=AsyncMock()) as mock_alert:
        await engine.start()
        try:
            for _ in range(10):
                log.info("http_request", extra={"status_code": 200, "provider": "openai", "duration_ms": 100})
            for _ in range(2):
                log.info("http_request", extra={"status_code": 500, "provider": "openai", "duration_ms": 100})
            for _ in range(50):
                if mock_alert.await_count > 0:
                    break
                await asyncio.sleep(0.01)
            assert mock_alert.await_count >= 1
        finally:
            await engine.stop()
            log.removeHandler(handler)
```

- [ ] **Step 2: Run — fail**

- [ ] **Step 3a: In `serving/servers/bootstrap.py`**, inside `initialize()` after the existing `register_alerter_job(...)` call (around line 213), add:

```python
    # Alerting framework (replaces Prometheus scaffolding)
    alert_engine = None
    if settings.alerts_enabled:
        from serving.observability.alert_config import load_alert_config
        from serving.observability.alert_rules import AlertEngine
        from serving.observability.log_handler import AlertingLogHandler

        alert_handler = AlertingLogHandler(maxsize=10_000)
        # Attach to the request-log logger so rules see request records
        logging.getLogger("serving.servers.middleware.request_log").addHandler(alert_handler)
        # Also attach to root for state-change events (auth, concurrency, circuit, etc.)
        logging.getLogger().addHandler(alert_handler)

        alert_cfg = load_alert_config(settings.alerts_config_path)
        alert_engine = AlertEngine(
            handler=alert_handler,
            config=alert_cfg,
            scheduler=scheduler,  # the existing AsyncIOScheduler
            op_store=op_store,
            log_store=log_store,
        )
        await alert_engine.start()
        log.info("alert engine started")
    else:
        log.info("alerts disabled (ALERTS_ENABLED=false)")
```

In `shutdown(services)`:

```python
    if services.alert_engine is not None:
        await services.alert_engine.stop()
```

- [ ] **Step 3b: In `serving/servers/services.py`** (the `AppServices` dataclass — discover its location with `grep -n 'class AppServices' serving/servers/`), add field:

```python
    alert_engine: "AlertEngine | None" = None
```

Update `bootstrap.initialize()` to populate `alert_engine=alert_engine` when constructing `AppServices(...)`.

- [ ] **Step 4: Run — pass**

```bash
uv run pytest test/integration/observability/test_framework_e2e.py -v
```

- [ ] **Step 5: Commit** `feat(observability): wire AlertEngine + handler into app lifecycle`

---

### Task 17: Refactor existing FailedRequestAlerter to use new helper

**Files:**
- Modify: `serving/admin/failed_request_alerter.py` (replace inline `post_slack_alert` with `alert_slack`; keep DB-query-based detection because it's a different shape than rule 1's log-stream detection — they coexist for now, with the new framework taking over after Phase 2)

- [ ] **Step 1: Test** — extend `test/unit/test_failed_request_alerter.py` to assert `alert_slack` is called instead of `post_slack_alert`.

- [ ] **Step 2: Run — fail**

- [ ] **Step 3: Implement** — replace `await post_slack_alert(...)` with `await alert_slack(AlertSeverity.ERROR, "Failed-request rate exceeded (DB-query detector)", context, dedupe_key="failed_request_rate_db", cooldown_sec=cooldown * 60)`. Delete the local `post_slack_alert` function.

- [ ] **Step 4: Run — pass**

- [ ] **Step 5: Commit** `refactor(observability): existing FailedRequestAlerter uses new alert_slack helper`

---

### Task 18: `.env.example` update + format + final test pass

**Files:**
- Modify: `.env.example`

- [ ] **Step 1: Add to `.env.example`** (near existing `SLACK_WEBHOOK_URL`):

```env
# Alerting framework (replaces Prometheus stack)
ALERTS_ENABLED=false
SLACK_ALERTS_WEBHOOK_URL=
ALERTS_CONFIG_PATH=config/alerts.yaml
```

- [ ] **Step 2: Run formatter**

```bash
make format
```

- [ ] **Step 3: Run full test suite**

```bash
make test
```

Expected: all pre-existing tests pass + new tests pass.

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "chore(observability): add ALERTS_* env vars to .env.example"
```

- [ ] **Step 5: Open PR**

```bash
git push -u origin jason/claude/observability-framework
gh pr create --base dev --title "Phase 1: in-process Slack alerting framework (dark)" \
  --body "$(cat <<'EOF'
Phase 1 of 3 — Prometheus replacement plan.

Spec: docs/superpowers/specs/2026-05-03-prometheus-removal-design.md
Plan: docs/superpowers/plans/2026-05-03-prometheus-removal.md

This PR adds the alerting framework dark (`ALERTS_ENABLED=false`).
- New modules: serving/observability/{alerts,log_handler,alert_rules,alert_config}.py
- 9 alerts wired up (5 rule-based, 2 state-change, 2 periodic SQL)
- Existing FailedRequestAlerter refactored onto new helper
- No Prometheus/metrics callsites changed in this PR

Test plan:
- [ ] Unit tests: serving/observability/* (`make test`)
- [ ] Integration test: framework e2e
- [ ] Manual: set ALERTS_ENABLED=true + SLACK_ALERTS_WEBHOOK_URL in dev → trigger circuit open → verify Slack message
EOF
)"
```

- [ ] **Step 6: Monitor CI + comments every 2 min until merged.** Per CLAUDE.md, fix CI failures and resolve review comments.

- [ ] **Step 7: After merge — clean up worktree**

```bash
cd /home/juncheng/hybridInference
git worktree remove ../hybridInference-obs
git branch -D jason/claude/observability-framework
```

---

# Operational Milestone — PR 2 (Staging Enable)

**No code changes.** Update staging `.env` on the staging host:

```bash
ALERTS_ENABLED=true
SLACK_ALERTS_WEBHOOK_URL=https://hooks.slack.com/services/...staging-channel...
```

Restart staging service. Watch Slack channel for 24-48h. Tune `config/alerts.yaml` thresholds via a follow-up commit if alerts are too noisy.

---

# Phase 2 — Strip Dead Metric Code (PR `jason/claude/strip-metrics-code`)

Pre-conditions: Phase 1 PR merged + staging soak complete.

### Task 19: Branch + issue setup

(Mirror Task 1; new branch `jason/claude/strip-metrics-code`, new GitHub issue.)

---

### Task 20: Strip metrics imports from `routing/routers.py`

**Files:**
- Modify: `routing/routers.py`

- [ ] **Step 1: Run grep to enumerate exact lines**

```bash
grep -n "serving.observability.metrics\|PROVIDER_AVAILABILITY\|PROVIDER_LATENCY\|API_FALLBACKS\|API_TTFT\|CIRCUIT_STATE\|STREAMING_INTERRUPTION\|CIRCUIT_OPEN_TOTAL" routing/routers.py
```

Save output for reference.

- [ ] **Step 2: Test (regression guard)** — run `make test` first, capture baseline pass count.

- [ ] **Step 3: Edits**

For each metric symbol, apply the spec's three-category rule:

- **Pure no-op (PROVIDER_AVAILABILITY, CIRCUIT_OPEN_TOTAL)** — delete the call entirely.
- **Carries unique signal (API_FALLBACKS at lines 442, 500, 729, 826)** — replace with structured log event:

  ```python
  log.info(
      "fallback_used",
      extra={"event": "fallback_used", "from_provider": from_p, "to_provider": to_p, "reason": reason},
  )
  ```

- **PROVIDER_LATENCY (lines 349, 683, 716)** — duration is already in request log; delete.
- **API_TTFT (lines 377, 780, 820)** — emit log event:

  ```python
  log.info("ttft", extra={"event": "ttft", "provider": provider, "ttft_ms": int(ttft * 1000)})
  ```

- **STREAMING_INTERRUPTION (lines 791, 833)** — emit log event with reason.
- **CIRCUIT_STATE (lines 222, 234, 245, 258)** — pure delete (the `alert_slack` call from Task 12 is the replacement).

After edits, remove the imports at the top of the file.

- [ ] **Step 4: Run tests + the circuit breaker alert test**

```bash
uv run pytest routing/ test/unit/routing/ -v
```

Expected: all pass.

- [ ] **Step 5: Commit** `refactor(observability): strip metric callsites from routing/routers.py`

---

### Task 21: Strip from `routing/routewise/router.py`

Same procedure as Task 20:

- [ ] **Step 1: Grep for ROUTEWISE_*, ROUTING_STRATEGY_SELECTED**
- [ ] **Step 2: Convert ROUTEWISE_CANARY_DECISIONS to log event** with `extra={"event": "canary_decision", "model": model, "router": router_kind}`. Delete the rest.
- [ ] **Step 3: Remove imports.**
- [ ] **Step 4: Test + commit** `refactor(observability): strip metric callsites from routewise router`

---

### Task 22: Strip from `routing/model_router_registry.py`

(Same shape; smaller. 1-2 callsites; pure delete.)

- [ ] Commit message: `refactor(observability): strip metric callsites from model_router_registry`

---

### Task 23: Strip from `serving/adapters/openai_compat.py`

KEY_POOL_REQUESTS / EXHAUSTED / COOLDOWNS / ACTIVE_AFFINITIES — convert to log events with `extra={"event": "key_pool_*", ...}` for each pool transition. ~8 sites.

- [ ] Test + commit: `refactor(observability): strip key-pool metric callsites in openai_compat`

---

### Task 24: Strip from `serving/servers/routers/completions.py`

API_MODEL_REQUESTS, API_TOKENS, API_TOKEN_ANOMALIES — all pure no-ops; data already in request logs. Delete callsites + import.

- [ ] Test + commit: `refactor(observability): strip metric callsites from completions router`

---

### Task 25: Strip from `serving/servers/auth.py`

DATABASE_CONNECTED.set(...) — no-op; delete.
API_MODEL_REQUESTS in auth — pure delete.

- [ ] Test + commit: `refactor(observability): strip metric callsites from auth`

---

### Task 26: Strip from `serving/servers/concurrency.py`

USER_CONCURRENCY_ACQUIRES_TOTAL / IN_FLIGHT / REJECTED_TOTAL — Task 11 already added the rejection log event. Delete remaining metric calls + import.

- [ ] Test + commit: `refactor(observability): strip user concurrency metric callsites`

---

### Task 27: Strip remaining importers

Files: `serving/http.py`, `serving/servers/routers/anthropic_messages.py`, `serving/servers/routers/health.py` (DATABASE_CONNECTED.set), `serving/servers/bootstrap.py`.

- [ ] Test + commit: `refactor(observability): strip remaining metric imports`

---

### Task 28: Delete `serving/observability/metrics.py`

- [ ] **Step 1: Verify no remaining importers**

```bash
grep -rn "from serving.observability.metrics\|from serving.observability import metrics\|import serving.observability.metrics" --include='*.py'
```

Expected: no results.

- [ ] **Step 2: Delete file**

```bash
rm serving/observability/metrics.py
```

- [ ] **Step 3: Run full test suite**

```bash
make format && make test
```

- [ ] **Step 4: Commit** `chore(observability): delete metrics.py shim — no remaining importers`

---

### Task 29: Open Phase 2 PR

```bash
git push -u origin jason/claude/strip-metrics-code
gh pr create --base dev --title "Phase 2: strip dead metric code" --body "..."
```

(Body links plan + spec; lists files touched.)

Monitor CI + comments. Merge. Clean up worktree.

---

# Phase 3 — Strip Prometheus & Alertmanager Infrastructure (PR `jason/claude/strip-prometheus-infra`)

Pre-conditions: Phase 2 merged + still no Prometheus consumers anywhere.

### Task 30: Branch + issue setup

(Mirror Task 1; branch `jason/claude/strip-prometheus-infra`.)

---

### Task 31: Delete `infrastructure/prometheus/`

- [ ] **Step 1: Confirm no references**

```bash
grep -rn "infrastructure/prometheus" --include='*.{yml,yaml,sh,service,md,py,conf,Dockerfile}' .
```

Note any references — they'll be cleaned up in subsequent tasks.

- [ ] **Step 2: Delete**

```bash
rm -rf infrastructure/prometheus
```

- [ ] **Step 3: Commit** `chore(infra): delete infrastructure/prometheus/`

---

### Task 32: Delete `infrastructure/alertmanager/`

- [ ] Confirm no references via grep, then `rm -rf infrastructure/alertmanager`
- [ ] Commit: `chore(infra): delete infrastructure/alertmanager/`

---

### Task 33: Delete systemd units

```bash
rm infrastructure/systemd/alertmanager.service
rm infrastructure/systemd/alert-logger.service
```

- [ ] Commit: `chore(infra): delete alertmanager + alert-logger systemd units`

---

### Task 34: Update `infrastructure/docker/docker-compose.yml`

- [ ] **Step 1: Read current file**

- [ ] **Step 2: Remove the `alertmanager` and `alert-logger` services** (around lines 130-160) and the `alertmanager_data` and `alert_log_data` volumes (around lines 162-173).

- [ ] **Step 3: Verify compose still parses**

```bash
docker compose -f infrastructure/docker/docker-compose.yml config > /dev/null
```

Expected: no errors.

- [ ] **Step 4: Commit** `chore(infra): drop alertmanager + alert-logger from docker-compose`

---

### Task 35: Delete `infrastructure/docker/Dockerfile.alert-logger`

```bash
rm infrastructure/docker/Dockerfile.alert-logger
```

- [ ] Commit: `chore(infra): delete Dockerfile.alert-logger`

---

### Task 36: Update Makefile and docs

- [ ] **Step 1: Grep for prometheus/alertmanager in Makefile and docs**

```bash
grep -rn -E "prometheus|alertmanager|alert-logger|alert_logger" Makefile docs/
```

- [ ] **Step 2: Remove or rewrite each match.** In `docs/source/developer/`, add a short observability section pointing at `serving/observability/` and `config/alerts.yaml`.

- [ ] **Step 3: Commit** `docs(observability): replace Prometheus references with new framework`

---

### Task 37: Open Phase 3 PR

```bash
git push -u origin jason/claude/strip-prometheus-infra
gh pr create --base dev --title "Phase 3: strip Prometheus + Alertmanager infrastructure" --body "..."
```

Monitor CI. After merge → on the production host, after deploy, manually:

```bash
docker compose down alertmanager alert-logger
docker volume rm hybridinference_alertmanager_data hybridinference_alert_log_data
sudo systemctl disable --now alertmanager.service alert-logger.service
sudo rm /etc/systemd/system/alertmanager.service /etc/systemd/system/alert-logger.service
sudo systemctl daemon-reload
```

Document this in the PR description so the operator running the deploy knows.

Clean up worktree.

---

# Operational Milestone — PR 5 (Production Enable)

**No code changes.** On the production host, edit `.env`:

```bash
ALERTS_ENABLED=true
SLACK_ALERTS_WEBHOOK_URL=https://hooks.slack.com/services/...prod-channel...
```

Restart service. Watch Slack channel for 24h. If thresholds need tuning, modify `config/alerts.yaml` via a small follow-up PR.

---

## Self-Review Checklist (post-implementation)

Before declaring complete:

- [ ] All 9 alerts have a unit test that exercises the threshold-crossing path.
- [ ] `serving/observability/metrics.py` no longer exists.
- [ ] `infrastructure/prometheus/` and `infrastructure/alertmanager/` no longer exist.
- [ ] `grep -rn 'serving.observability.metrics' --include='*.py'` returns nothing.
- [ ] `grep -rn 'PROVIDER_AVAILABILITY\|API_FALLBACKS\|CIRCUIT_STATE\|KEY_POOL_' --include='*.py'` returns nothing in production code.
- [ ] `docker compose -f infrastructure/docker/docker-compose.yml config` parses cleanly with no `alertmanager` / `alert-logger` references.
- [ ] `make format && make test` passes.
- [ ] CHANGELOG (if present) updated.
- [ ] `config/alerts.yaml` and the `ALERTS_*` env vars documented in `.env.example` and developer docs.
- [ ] Slack channel received a real test alert from staging before prod-enable.
