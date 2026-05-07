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
    """Sliding-window percentage rule (e.g., failed-request rate)."""

    enabled: bool = True
    window_sec: int = 300
    threshold_pct: float = 5.0
    min_samples: int = 50
    cooldown_sec: int = 900


class CountRule(BaseModel):
    """Sliding-window count rule (e.g., auth failure spike)."""

    enabled: bool = True
    window_sec: int = 60
    threshold_count: int = 50
    cooldown_sec: int = 600


class PendingDecisionsLeakConfig(BaseModel):
    """Config for ``PendingDecisionsLeakRule``.

    Fires when the number of ``routewise_decision_evicted`` events seen
    over ``window_sec`` exceeds ``threshold_count``. Indicates that
    ``RouteWiseRouter._pending_decisions`` is leaking entries (likely
    because ``chat_completion`` / ``stream_chat_completion`` is not
    consuming them on some code path).
    """

    enabled: bool = True
    window_sec: int = 600
    threshold_count: int = 20
    cooldown_sec: int = 3600


class LatencyRule(BaseModel):
    """Sliding-window latency rule with per-key overrides."""

    enabled: bool = True
    window_sec: int = 600
    threshold_ms: int = 30000
    min_samples: int = 30
    cooldown_sec: int = 1800
    overrides: dict[str, dict[str, int]] = Field(default_factory=dict)


class Rules(BaseModel):
    """Container for log-stream rules."""

    failed_request_rate: RateRule = Field(default_factory=RateRule)
    fivexx_rate: RateRule = Field(default_factory=lambda: RateRule(threshold_pct=2.0))
    p95_latency_per_provider: LatencyRule = Field(default_factory=LatencyRule)
    auth_failure_spike: CountRule = Field(default_factory=CountRule)
    concurrency_exhausted: CountRule = Field(
        default_factory=lambda: CountRule(window_sec=300, threshold_count=100, cooldown_sec=1800)
    )
    pending_decisions_leak: PendingDecisionsLeakConfig = Field(
        default_factory=PendingDecisionsLeakConfig
    )


class StateChange(BaseModel):
    """State-change alert configuration."""

    enabled: bool = True
    cooldown_sec: int = 300


class StateChanges(BaseModel):
    """Container for state-change alert configurations."""

    circuit_open: StateChange = Field(default_factory=StateChange)
    db_disconnect: StateChange = Field(default_factory=StateChange)


class UserOverrun(BaseModel):
    """User cost overrun ticker config."""

    enabled: bool = True
    check_interval_sec: int = 300
    cooldown_sec: int = 86400
    thresholds_per_role: dict[str, float] = Field(
        default_factory=lambda: {"free": 5.0, "pro": 50.0, "internal": 500.0}
    )


class ProviderHourlySpend(BaseModel):
    """Per-provider hourly spend ticker config."""

    enabled: bool = True
    check_interval_sec: int = 300
    cooldown_sec: int = 3600
    budgets: dict[str, float] = Field(default_factory=dict)


class CostSection(BaseModel):
    """Container for cost-related ticker configs."""

    user_overrun: UserOverrun = Field(default_factory=UserOverrun)
    provider_hourly_spend: ProviderHourlySpend = Field(default_factory=ProviderHourlySpend)


class AlertConfig(BaseModel):
    """Top-level alert config."""

    rules: Rules = Field(default_factory=Rules)
    state_changes: StateChanges = Field(default_factory=StateChanges)
    cost: CostSection = Field(default_factory=CostSection)


def load_alert_config(path: str | Path) -> AlertConfig:
    """Load AlertConfig from YAML at ``path``; return defaults if missing."""
    p = Path(path)
    if not p.exists():
        log.warning("alerts config not found at %s; using defaults", p)
        return AlertConfig()
    raw = yaml.safe_load(p.read_text()) or {}
    raw = _expand_env(raw)
    return AlertConfig.model_validate(raw)
