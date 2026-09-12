"""Pydantic models for config/alerts.yaml plus a loader that handles env expansion."""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import AliasChoices, BaseModel, Field

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


class AuthFailureSpikeConfig(CountRule):
    """Config for ``AuthFailureSpikeRule``, which is off unless asked for.

    Auth failures are internet background noise — scanners and bots retrying
    bad keys — so a spike names nothing an operator can act on. The per-IP
    blocklist (``utils/auth_failure_blocklist.py``) already refuses a repeat
    offender without anyone being paged. Only the *page* is off: every
    ``auth_failure`` log record is still emitted at the auth sites in
    ``servers/auth.py``, so an investigation loses no evidence.

    The ``enabled`` default lives here rather than on the field's
    ``default_factory`` because a factory only runs when the key is absent
    altogether. A deployment tuning just the threshold —

    .. code-block:: yaml

        rules:
          auth_failure_spike:
            threshold_count: 100

    — has pydantic build this model from that mapping, and would have
    inherited ``CountRule``'s ``enabled: True`` and quietly resumed paging.
    Overriding the default on the model makes an explicit ``enabled: true``
    the only way to turn the page back on.
    """

    enabled: bool = False


class AuthIpBlockedConfig(CountRule):
    """Config for ``AuthIpBlockedRule``, which pages when the gateway blocks a source.

    The companion to ``AuthFailureSpikeConfig``, and **on** where that one is
    off, because the two describe different things. A spike of bad keys is
    internet background noise. The blocklist actually *refusing* a source is a
    discrete decision the gateway made, at a threshold high enough
    (``auth_failure_block_threshold``, 200/day by default) that scanners rarely
    reach it -- and it names an address an operator can act on.

    It matters most when the blocked source turns out to be the deployment's
    own: a monitor, a CI job, or a service account whose credential went stale
    keeps retrying, crosses the threshold, and is then refused at
    ``servers/auth.py`` *before* its key is read, so repairing the credential
    does not bring it back inside the block window. Without this rule the first
    symptom is whatever that caller was responsible for going quiet.

    ``threshold_count: 1`` -- the default -- means any single block pages, which
    is the point. Counting over ``window_sec`` rather than firing per address
    keeps a scanner wave to one incident instead of a firing and a recovery per
    bucket. Note what ``cooldown_sec`` then implies: only the first breach
    message in that hour is delivered, so it names the block that opened the
    incident and points at ``GET /admin/auth-blocks`` for the live list, rather
    than carrying a total. Raise ``threshold_count`` to page only once a window
    holds that many blocks; set ``enabled: false`` to go back to the log record
    alone.
    """

    window_sec: int = 300
    threshold_count: int = 1
    cooldown_sec: int = 3600


class PendingPrefixCacheLeakConfig(BaseModel):
    """Config for ``PendingPrefixCacheLeakRule``.

    Fires when the number of ``routewise_prefix_cache_entry_evicted`` events
    seen over ``window_sec`` exceeds ``threshold_count``. Indicates that
    pending RouteWise prefix-cache state is not being consumed before its
    lifetime or capacity bound is reached.
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


class Rules(BaseModel):
    """Container for log-stream rules."""

    failed_request_rate: RateRule = Field(default_factory=RateRule)
    fivexx_rate: RateRule = Field(default_factory=lambda: RateRule(threshold_pct=2.0))
    p95_latency_per_provider: LatencyRule = Field(default_factory=LatencyRule)
    # Off unless a deployment sets ``enabled: true`` under
    # ``rules.auth_failure_spike``; see AuthFailureSpikeConfig for why, and for
    # why the default sits on that model rather than on this factory.
    auth_failure_spike: AuthFailureSpikeConfig = Field(default_factory=AuthFailureSpikeConfig)
    # On by default, unlike auth_failure_spike above: this fires on the block
    # itself, not on the failures leading to it. See AuthIpBlockedConfig.
    auth_ip_blocked: AuthIpBlockedConfig = Field(default_factory=AuthIpBlockedConfig)
    prefix_cache_pending_leak: PendingPrefixCacheLeakConfig = Field(
        default_factory=PendingPrefixCacheLeakConfig,
        validation_alias=AliasChoices(
            "prefix_cache_pending_leak",
            "pending_decisions_leak",
        ),
    )
    tracked_task_failure_rate: TrackedTaskFailureRateConfig = Field(
        default_factory=TrackedTaskFailureRateConfig
    )


class StateChange(BaseModel):
    """State-change alert configuration."""

    enabled: bool = True
    cooldown_sec: int = 300


class CircuitOpenStateChange(StateChange):
    """Circuit-open page configuration.

    ``page_on_usage_limit`` is the only field the circuit breaker reads today.
    Turn it off where subscription plans running dry is routine: that page names
    nothing an operator can act on, and the breaker re-arms itself once the
    provider's window resets. Trips for every other reason still page.
    """

    page_on_usage_limit: bool = True


class StateChanges(BaseModel):
    """Container for state-change alert configurations."""

    circuit_open: CircuitOpenStateChange = Field(default_factory=CircuitOpenStateChange)
    db_disconnect: StateChange = Field(default_factory=StateChange)


class UserOverrun(BaseModel):
    """User cost overrun ticker config."""

    enabled: bool = True
    check_interval_sec: int = 300
    cooldown_sec: int = 86400
    #: **Parsed, never consulted.** ``UserCostOverrunJob`` now measures spend
    #: against each account's own ``api_keys.quota_daily_cost_usd``, because
    #: that is the only limit anything enforces. A per-role threshold cannot
    #: express it: caps differ between keys of the same role, and the gate
    #: stops spending at the cap, so any role threshold above it is unreachable
    #: while one below it fires on users who were never refused anything.
    #:
    #: The field stays anyway, rather than being deleted: every deployment's
    #: ``alerts.yaml`` still sets it, and the value would then be swallowed by
    #: pydantic's default ``extra="ignore"`` — an operator would go on tuning a
    #: number that no longer exists, with nothing to tell them. Keeping it
    #: parsed and documented as inert is the honest version of that. Deleting
    #: it from a deployment's YAML is safe and changes nothing.
    thresholds_per_role: dict[str, float] = Field(
        default_factory=lambda: {
            "free": 5.0,
            "pro": 50.0,
            "internal": 500.0,
        }
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
