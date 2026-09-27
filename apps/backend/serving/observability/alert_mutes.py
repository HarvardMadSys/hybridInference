"""Per-type Slack-alert mutes: silence one kind of alert and keep the rest.

The global snooze (:mod:`serving.observability.alert_snooze`) pauses every alert
at once, which is the wrong tool for one noisy type: muting ``auth_ip_blocked``
for a week must not also hide a database outage. A mute here names one alert
*type* from :mod:`serving.observability.alert_types` and lasts until a deadline,
or until an admin lifts it.

Each mute is its own ``site_settings`` row, ``slack_alert_mute:<type>``, holding
``{"until": <epoch seconds>}`` as JSON, with ``null`` meaning "until unmuted". A
row per type rather than one shared map, so two admins muting different types at
once cannot overwrite each other. The rows survive restarts; lifting a mute
deletes its row, and a lapsed one is ignored until a later write replaces it.

``alert_slack`` consults :func:`is_alert_type_muted` for each firing it is about
to send, and a muted firing costs no cooldown, exactly like a snoozed one: when
the mute lifts, a breach that is still live pages at its next evaluation. What a
mute does to the *close* of an incident lives in ``alerts.py``, beside the rest
of the resolution bookkeeping.

A short in-process cache keeps the lookup off the database. A write invalidates
it, so the process that served a dashboard toggle honors it at once and every
other process within :data:`_CACHE_TTL`.
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from typing import Any

from serving.utils.logging import get_logger

logger = get_logger(__name__)

MUTE_SETTING_PREFIX = "slack_alert_mute:"
MUTE_VALUE_TYPE = "json"

# Same bound as the snooze: short enough that a mute set from another process,
# or lifted early, is honored quickly.
_CACHE_TTL = 5.0


@dataclass(frozen=True, slots=True)
class AlertMute:
    """One stored mute, active or lapsed."""

    alert_type: str
    #: Epoch seconds the mute lasts until; None mutes until an admin lifts it.
    until: float | None
    #: When the row was last written, in epoch seconds, if the store says.
    muted_at: float | None = None
    muted_by: str | None = None

    def is_active(self, now: float | None = None) -> bool:
        """Whether this mute still silences its type at ``now`` (default: the clock)."""
        if self.until is None:
            return True
        return self.until > (time.time() if now is None else now)


_store: Any | None = None
# (cached_at_monotonic, every stored mute keyed by alert type)
_cache: tuple[float, dict[str, AlertMute]] | None = None
# Bumped by every write, so a read that was already in flight when a mute
# changed cannot publish what it saw before the change. The next read, or the
# TTL, picks up what it missed.
_generation = 0


def init_alert_mutes(store: Any | None) -> None:
    """Register the operational store used to read and write mutes."""
    global _store, _cache
    _store = store
    _cache = None


def mute_setting_key(alert_type: str) -> str:
    """Return the ``site_settings`` key holding one alert type's mute."""
    if not alert_type:
        raise ValueError("alert_type must not be empty")
    return f"{MUTE_SETTING_PREFIX}{alert_type}"


def alert_type_from_setting_key(key: str) -> str | None:
    """Return the alert type a setting key mutes, or None for any other key."""
    if not key.startswith(MUTE_SETTING_PREFIX):
        return None
    return key[len(MUTE_SETTING_PREFIX) :] or None


def encode_mute(until: float | None) -> str:
    """Serialize a mute deadline into its stored ``site_settings`` value."""
    return json.dumps({"until": until})


def decode_mute_until(raw: Any) -> float | None:
    """Parse a stored value back into its deadline (None: until unmuted).

    Raises:
        ValueError: The value is not a JSON object whose ``until`` is null or a
            finite number.
    """
    try:
        payload = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError as exc:
        raise ValueError("alert mute setting is not valid JSON") from exc
    if not isinstance(payload, dict) or "until" not in payload:
        raise ValueError("alert mute setting must be a JSON object with an 'until' field")
    until = payload["until"]
    if until is None:
        return None
    # ``bool`` is an ``int``; a stored ``true`` is corruption, not a deadline.
    if isinstance(until, bool) or not isinstance(until, (int, float)):
        raise ValueError("alert mute 'until' must be null or a number")
    until = float(until)
    if not math.isfinite(until):
        raise ValueError("alert mute 'until' must be finite")
    return until


def _epoch(value: Any) -> float | None:
    """``updated_at`` as epoch seconds; the store returns a datetime, or nothing."""
    try:
        return float(value.timestamp())
    except (AttributeError, TypeError, ValueError, OverflowError):
        return None


def _decode_rows(rows: list[Any]) -> dict[str, AlertMute]:
    mutes: dict[str, AlertMute] = {}
    for row in rows:
        alert_type = alert_type_from_setting_key(str(row.get("key") or ""))
        if alert_type is None:
            continue
        try:
            until = decode_mute_until(row.get("value"))
        except ValueError:
            # A corrupt row mutes nothing. Failing open is the safe direction
            # here: the cost is an alert someone wanted silenced, never an
            # outage nobody hears about.
            logger.warning("Ignoring invalid alert mute setting for %s", alert_type, exc_info=True)
            continue
        mutes[alert_type] = AlertMute(
            alert_type=alert_type,
            until=until,
            muted_at=_epoch(row.get("updated_at")),
            muted_by=row.get("updated_by"),
        )
    return mutes


async def list_mutes() -> dict[str, AlertMute]:
    """Return every stored mute, lapsed ones included, keyed by alert type.

    Empty when no store is registered. On a read failure the last snapshot keeps
    being served, cached for the TTL like a success so a database outage does
    not turn every alert check into a query: a mute an admin set stays honored
    while the store is down, and with no snapshot at all nothing is muted.
    """
    global _cache
    if _store is None:
        return {}

    now = time.monotonic()
    cached = _cache
    if cached is not None and (now - cached[0]) < _CACHE_TTL:
        return dict(cached[1])

    generation = _generation
    try:
        rows = await _store.list_settings()
    except Exception:
        logger.debug("alert mute read failed", exc_info=True)
        fallback = cached[1] if cached is not None else {}
        if generation == _generation:
            _cache = (now, fallback)
        return dict(fallback)

    loaded = _decode_rows(rows)
    if generation == _generation:
        _cache = (time.monotonic(), loaded)
    return dict(loaded)


async def is_alert_type_muted(alert_type: str) -> bool:
    """Return True when an active mute silences ``alert_type``."""
    mute = (await list_mutes()).get(alert_type)
    return mute is not None and mute.is_active()


def _invalidate() -> None:
    global _cache, _generation
    _generation += 1
    _cache = None


async def mute_alert_type(
    alert_type: str, until: float | None, updated_by: str | None
) -> AlertMute:
    """Mute ``alert_type`` until ``until`` (epoch seconds), or until unmuted if None.

    Replaces any existing mute on the type, so a shorter duration shortens it.
    """
    if _store is None:
        raise RuntimeError("alert mute store not initialized")
    if until is not None and not math.isfinite(until):
        raise ValueError("until must be finite")
    await _store.set_setting(
        mute_setting_key(alert_type),
        encode_mute(until),
        MUTE_VALUE_TYPE,
        updated_by,
    )
    _invalidate()
    return AlertMute(alert_type=alert_type, until=until, muted_at=time.time(), muted_by=updated_by)


async def unmute_alert_type(alert_type: str) -> bool:
    """Lift any mute on ``alert_type``; return whether a stored row was removed."""
    if _store is None:
        raise RuntimeError("alert mute store not initialized")
    removed = await _store.delete_setting(mute_setting_key(alert_type))
    _invalidate()
    return bool(removed)
