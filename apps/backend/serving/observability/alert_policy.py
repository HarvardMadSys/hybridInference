"""Notification policy: which firings reach the channel, and how often one problem may.

``alerts.yaml``'s ``notification_policy`` block lands here at startup (see
:class:`~serving.observability.alert_config.NotificationPolicy` for what each
setting means). ``alert_slack`` asks :func:`hold_reason` about every firing,
next to the per-type mute, and reports back through :func:`note_delivered` and
:func:`note_closed`. The policy never sees a resolution: whether a close is
announced is decided by whether its firing was, as for a mute.

Every function here is synchronous and never raises into the alert path: a
policy that cannot decide lets the alert through, because the cost of that is
one message someone wanted held and the cost of the reverse is an outage
nobody hears about.

Problem state is per process and in memory, like the rest of the alert path's
dedupe state. :func:`preload_from_history` seeds it at startup with the keys
that already posted inside the re-notify window, so a restart or a second
worker does not announce a known problem again. Workers do not see each
other's posts after that; each can post a new problem once.
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
DIGEST_ONLY = "digest_only"
ALREADY_NOTIFIED = "already_notified"
BELOW_MIN_DURATION = "below_min_duration"

#: How often finished problems are dropped. Dynamic keys (per user, per day)
#: would otherwise grow the table for the life of the process.
_PRUNE_INTERVAL_SEC = 300.0

#: Upper bound on the history read at startup, so a slow database delays the
#: gateway's start by at most this much.
_PRELOAD_TIMEOUT_SEC = 3.0


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
    """Where the policy sends firings for ``key``: ``channel``, ``digest`` or ``record``."""
    return _POLICY.destinations.get(alert_type_of(key), "channel")


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
            return RECORD_ONLY
        if where == "digest":
            return DIGEST_ONLY
        if problem.posted_at is not None:
            return ALREADY_NOTIFIED
        need = _POLICY.min_open_sec.get(alert_type_of(key), 0.0)
        open_for = problem.open_total + (moment - (problem.open_since or moment))
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


def seed_delivered(key: str, posted_at: float) -> None:
    """Record that ``key`` posted at ``posted_at`` before this process started.

    The problem is taken as closed then: its next firing inside the window
    reopens it as already announced, and one outside the window is new.
    """
    if key in _PROBLEMS:
        return
    _PROBLEMS[key] = _Problem(
        open_since=None, last_closed=posted_at, posted_at=posted_at, last_seen=posted_at
    )


_PRELOAD_SQL = """
SELECT alert_key, EXTRACT(EPOCH FROM MAX(occurred_at))::float8 AS posted_at
FROM alert_history_events
WHERE source = $1
  AND source_namespace = $2
  AND deployment_id = $3
  AND target_environment = $4
  AND occurred_at > NOW() - make_interval(secs => $5)
  AND kind = 'delivery_result'
  AND outcome = 'accepted'
  AND origin = 'live'
  AND alert_key IS NOT NULL
  AND facts->>'notification' = 'firing'
GROUP BY alert_key
"""


async def preload_from_history(runtime: Any) -> int:
    """Seed problem state with the keys this deployment posted inside the window.

    Best effort and bounded: with the policy off, no history runtime, no
    database or a read that fails or is slow, nothing is seeded and the
    gateway starts as it would have. Returns how many keys were seeded.
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
                    float(_POLICY.renotify_after_sec),
                )
            )

    try:
        rows = await asyncio.wait_for(_read(), _PRELOAD_TIMEOUT_SEC)
    except Exception:
        log.warning("notification policy could not read recent alerts; starting empty")
        return 0
    for row in rows:
        seed_delivered(row["alert_key"], float(row["posted_at"]))
    return len(rows)
