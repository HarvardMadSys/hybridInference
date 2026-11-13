"""Notification policy: which firings reach the channel, and how often one problem may.

The ``ALERT_RULES`` document's ``notification_policy`` block lands here at
startup (see :class:`~serving.observability.alert_config.NotificationPolicy`
for what each setting means). ``alert_slack`` asks :func:`hold_reason` about
every firing, next to the per-type mute, and reports back through
:func:`note_delivered` and :func:`note_closed`. The policy never sees a
resolution: whether a close is announced is decided by whether its firing was,
as for a mute.

Every function here is synchronous and never raises into the alert path: a
policy that cannot decide lets the alert through, because the cost of that is
one message someone wanted held and the cost of the reverse is an outage
nobody hears about.

Problem state is per process and in memory, like the rest of the alert path's
dedupe state. :func:`preload_from_history` restores every currently open
incident that delivered a firing, even when it has been open longer than the
re-notify window, so a restart can suppress the duplicate firing and still
deliver the eventual recovery. Workers do not see each other's later posts;
each can post a new problem once.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

from serving.observability.alert_config import NotificationPolicy
from serving.observability.alert_history.capture import BACKEND_SOURCE
from serving.observability.alert_types import alert_type_of

log = logging.getLogger(__name__)

#: Reasons :func:`hold_reason` returns, recorded as alert-history decisions.
RECORD_ONLY = "record_only"
ALREADY_NOTIFIED = "already_notified"
BELOW_MIN_DURATION = "below_min_duration"

#: How often finished problems are dropped. Dynamic keys (per user, per day)
#: would otherwise grow the table for the life of the process.
_PRUNE_INTERVAL_SEC = 300.0

#: Upper bound on the history read at startup, so a slow database delays the
#: gateway's start by at most this much.
_PRELOAD_TIMEOUT_SEC = 3.0

#: These notifications are discrete events, not incidents with recovery edges.
#: Keep their existing alert_slack cooldown instead of suppressing every later
#: event forever because no producer can call :func:`note_closed`.
_EVENT_ALERT_TYPES = frozenset({"cost_counter_write_refused", "cost_above_request_ceiling"})


@dataclass
class _Problem:
    """One alert key's problem, from its first firing until it has stayed closed long enough."""

    #: Start of the current open stretch, or ``None`` while closed.
    open_since: float | None
    #: Open time of the stretches already closed.
    open_total: float = 0.0
    #: When the last open stretch closed.
    last_closed: float | None = None
    #: When a firing for this problem was last delivered.
    posted_at: float | None = None
    #: Last firing seen, for pruning keys that stopped reporting without a close.
    last_seen: float = 0.0


_POLICY = NotificationPolicy()
_PROBLEMS: dict[str, _Problem] = {}
_last_prune = 0.0


def apply_notification_policy(policy: NotificationPolicy) -> None:
    """Install ``policy``. Problem state is kept: it describes the channel, not the config."""
    global _POLICY
    _POLICY = policy


def notification_policy() -> NotificationPolicy:
    """Return the policy in force."""
    return _POLICY


def reset_notification_policy() -> None:
    """Restore the default (off) policy and forget every problem. For tests."""
    global _last_prune
    apply_notification_policy(NotificationPolicy())
    _PROBLEMS.clear()
    _last_prune = 0.0


def destination(key: str) -> str:
    """Where the policy sends firings; unavailable digest delivery fails open to channel."""
    configured = _POLICY.destinations.get(alert_type_of(key), "channel")
    return "channel" if configured == "digest" else configured


def _renotifies_on_recurrence(key: str) -> bool:
    for entry in _POLICY.renotify_on_recurrence:
        if entry.endswith("*"):
            if key.startswith(entry[:-1]):
                return True
        elif key == entry:
            return True
    return False


def _expired(problem: _Problem, now: float) -> bool:
    """Whether ``problem`` is over: closed, or silent, for longer than the window."""
    window = _POLICY.renotify_after_sec
    if problem.open_since is None:
        return problem.last_closed is None or now - problem.last_closed > window
    return now - problem.last_seen > window


def _prune(now: float) -> None:
    global _last_prune
    if now - _last_prune < _PRUNE_INTERVAL_SEC:
        return
    _last_prune = now
    for key in [key for key, problem in _PROBLEMS.items() if _expired(problem, now)]:
        del _PROBLEMS[key]


def _observe_firing(key: str, now: float) -> _Problem:
    problem = _PROBLEMS.get(key)
    if problem is None or _expired(problem, now):
        problem = _PROBLEMS[key] = _Problem(open_since=now)
    elif problem.open_since is None:
        if _renotifies_on_recurrence(key):
            problem = _PROBLEMS[key] = _Problem(open_since=now)
        else:
            # A recurrence inside the window: the same problem, open again.
            problem.open_since = now
    problem.last_seen = now
    return problem


def hold_reason(key: str, now: float | None = None) -> str | None:
    """Observe a firing on ``key`` and return why it must stay out of the channel, or ``None``.

    ``None`` whenever the policy is off. Never raises.
    """
    if not _POLICY.enabled:
        return None
    try:
        moment = time.time() if now is None else now
        _prune(moment)
        problem = _observe_firing(key, moment)
        where = destination(key)
        if where == "record":
            return RECORD_ONLY if _history_capture() is not None else None
        if alert_type_of(key) in _EVENT_ALERT_TYPES:
            return None
        if problem.posted_at is not None:
            return ALREADY_NOTIFIED
        need = _POLICY.min_open_sec.get(alert_type_of(key), 0.0)
        open_since = problem.open_since
        open_for = problem.open_total + (moment - open_since if open_since is not None else 0.0)
        if open_for < need:
            return BELOW_MIN_DURATION
        return None
    except Exception:
        log.debug("notification policy failed for %s; sending alert", key, exc_info=True)
        return None


def note_delivered(key: str, now: float | None = None) -> None:
    """A firing on ``key`` reached the channel: the problem has been announced."""
    if not _POLICY.enabled:
        return
    moment = time.time() if now is None else now
    problem = _PROBLEMS.get(key)
    if problem is None:
        problem = _PROBLEMS[key] = _Problem(open_since=moment, last_seen=moment)
    problem.posted_at = moment


def note_closed(key: str, now: float | None = None) -> None:
    """The incident on ``key`` closed. The problem stays, closed, for the re-notify window."""
    problem = _PROBLEMS.get(key)
    if problem is None or problem.open_since is None:
        return
    moment = time.time() if now is None else now
    problem.open_total += max(0.0, moment - problem.open_since)
    problem.open_since = None
    problem.last_closed = moment


def note_recovered(key: str) -> None:
    """Forget a delivered recovery so any later recurrence is a new problem."""
    _PROBLEMS.pop(key, None)


def seed_open_incident(
    key: str,
    *,
    opened_at: float,
    posted_at: float,
    episode_id: str,
    restored_at: float,
    capture: Any = None,
) -> None:
    """Restore an open, announced incident from durable alert history."""
    _PROBLEMS[key] = _Problem(
        open_since=opened_at,
        posted_at=posted_at,
        # An open history episode is live until a durable end event says
        # otherwise. Reset the in-memory silence clock at restoration so an
        # old but still-open incident is not immediately treated as expired.
        last_seen=max(opened_at, posted_at, restored_at),
    )
    history = capture or _history_capture()
    if history is not None:
        seed = getattr(history, "seed_open_episode", None)
        if seed is not None:
            seed(key, episode_id=episode_id, opened_at=opened_at)


def _history_capture() -> Any | None:
    """Return the enabled capture without making alert policy depend on runtime wiring."""
    try:
        from serving.observability.alert_history import hooks

        return hooks.active()
    except Exception:
        return None


_PRELOAD_SQL = """
WITH latest_condition AS (
    SELECT DISTINCT ON (alert_key)
           alert_key, episode_id, kind, occurred_at, facts
    FROM alert_history_events
    WHERE source = $1
      AND source_namespace = $2
      AND deployment_id = $3
      AND target_environment = $4
      AND origin = 'live'
      AND kind IN ('condition_triggered', 'condition_ended')
      AND alert_key IS NOT NULL
      AND episode_id IS NOT NULL
    ORDER BY alert_key, occurred_at DESC, recorded_at DESC, record_id DESC
), delivered AS (
    SELECT alert_key, episode_id, MAX(occurred_at) AS posted_at
    FROM alert_history_events
    WHERE source = $1
      AND source_namespace = $2
      AND deployment_id = $3
      AND target_environment = $4
      AND kind = 'delivery_result'
      AND outcome = 'accepted'
      AND origin = 'live'
      AND facts->>'notification' = 'firing'
      AND alert_key IS NOT NULL
      AND episode_id IS NOT NULL
    GROUP BY alert_key, episode_id
)
SELECT current.alert_key,
       current.episode_id,
       EXTRACT(EPOCH FROM current.occurred_at)::float8 AS opened_at,
       EXTRACT(EPOCH FROM delivered.posted_at)::float8 AS posted_at,
       current.facts->>'tracker_kind' AS tracker_kind,
       NULLIF(current.facts->>'stale_after_sec', '')::float8 AS stale_after_sec
FROM latest_condition AS current
JOIN delivered USING (alert_key, episode_id)
WHERE current.kind = 'condition_triggered'
  AND delivered.posted_at >= current.occurred_at
"""


async def preload_from_history(runtime: Any) -> int:
    """Seed problem state for incidents still open after this deployment restarts.

    Best effort and bounded: with the policy off, no history runtime, no
    database or a read that fails or is slow, nothing is seeded and the
    gateway starts as it would have. The read includes older open incidents;
    the re-notify window does not close an incident that has no durable end.
    Returns how many keys were seeded.
    """
    if not _POLICY.enabled or runtime is None or runtime.capture is None:
        return 0
    ident = runtime.capture.identity

    async def _read() -> list[Any]:
        async with runtime.acquire() as conn:
            return list(
                await conn.fetch(
                    _PRELOAD_SQL,
                    BACKEND_SOURCE,
                    ident.source_namespace,
                    ident.deployment_id,
                    ident.target_environment,
                )
            )

    try:
        rows = await asyncio.wait_for(_read(), _PRELOAD_TIMEOUT_SEC)
    except Exception:
        log.warning(
            "notification policy could not read recent alerts; starting empty", exc_info=True
        )
        return 0
    seeded = 0
    restored_at = time.time()
    for row in rows:
        try:
            from serving.observability.alerts import restore_announced_incident

            restore_announced_incident(
                row["alert_key"],
                tracker_kind=row["tracker_kind"],
                opened_at=float(row["opened_at"]),
                restored_at=restored_at,
                stale_after_sec=(
                    float(row.get("stale_after_sec"))
                    if row.get("stale_after_sec") is not None
                    else None
                ),
            )
        except Exception:
            log.warning(
                "notification policy could not restore alert lifecycle for %s", row["alert_key"]
            )
            continue
        seed_open_incident(
            row["alert_key"],
            opened_at=float(row["opened_at"]),
            posted_at=float(row["posted_at"]),
            episode_id=row["episode_id"],
            restored_at=restored_at,
            capture=runtime.capture,
        )
        seeded += 1
    return seeded
