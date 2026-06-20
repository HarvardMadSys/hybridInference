"""Global Slack-alert snooze: suppress all outgoing alerts until a deadline.

Admins can pause noisy alerting for a window (e.g. during a known incident or
maintenance) without disabling the alert engine entirely. The deadline is a
Unix epoch-seconds float stored in the ``site_settings`` row
``slack_alerts_snooze_until`` (``0`` or absent means "not snoozed"), so the
snooze survives process restarts.

``alert_slack`` consults :func:`is_snoozed` on every send. A short in-process
cache keeps that hot path off the database; writes update the cache eagerly so
toggles from the admin API take effect immediately.
"""

from __future__ import annotations

import time
from typing import Any

from serving.utils.logging import get_logger

logger = get_logger(__name__)

SNOOZE_SETTING_KEY = "slack_alerts_snooze_until"

# Keep reads off the DB on the alert path; short enough that a snooze set from
# another process (or cleared early) is honored quickly.
_CACHE_TTL = 5.0

_store: Any | None = None
# (cached_at_monotonic, snooze_until_epoch)
_cache: tuple[float, float] | None = None


def init_alert_snooze(store: Any | None) -> None:
    """Register the operational store used to read/write the snooze deadline."""
    global _store, _cache
    _store = store
    _cache = None


def _coerce_epoch(raw: Any) -> float:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


async def get_snooze_until() -> float:
    """Return the snooze deadline as epoch seconds (``0.0`` when not snoozed)."""
    global _cache
    if _store is None:
        return 0.0

    now = time.monotonic()
    cached = _cache
    if cached is not None and (now - cached[0]) < _CACHE_TTL:
        return cached[1]

    try:
        row = await _store.get_setting(SNOOZE_SETTING_KEY)
    except Exception:
        # Cache the fallback for the TTL so a DB outage doesn't trigger a
        # query on every alert check (thundering herd).
        logger.debug("alert snooze read failed", exc_info=True)
        fallback = cached[1] if cached is not None else 0.0
        _cache = (now, fallback)
        return fallback

    value = _coerce_epoch(row.get("value")) if row is not None else 0.0
    _cache = (now, value)
    return value


async def is_snoozed() -> bool:
    """Return True when alerts are currently snoozed."""
    return await get_snooze_until() > time.time()


async def set_snooze_until(epoch: float, updated_by: str | None) -> None:
    """Persist a new snooze deadline (epoch seconds) and refresh the cache."""
    global _cache
    if _store is None:
        raise RuntimeError("alert snooze store not initialized")
    await _store.set_setting(SNOOZE_SETTING_KEY, str(float(epoch)), "float", updated_by)
    _cache = (time.monotonic(), float(epoch))


async def clear_snooze(updated_by: str | None) -> None:
    """Resume alerting immediately by clearing the snooze deadline."""
    await set_snooze_until(0.0, updated_by)
