r"""Replay recorded alert history through the notification policy.

Answers "what would the channel have received?" for a ``notification_policy``
before it is switched on, using the gateway's own policy code
(``serving.observability.alert_policy``), not a copy of its rules.

Input is a CSV export of ``alert_history_events``, one row per event, made
read-only on the database, for example::

  psql "$DATABASE_URL" -c "\copy (
    SELECT EXTRACT(EPOCH FROM occurred_at) AS t,
           EXTRACT(EPOCH FROM bucket_start) AS bucket_start,
           kind, alert_key, facts->>'notification' AS notification,
           decision_reason, outcome
    FROM alert_history_events
    WHERE source = 'backend' AND origin = 'live' AND alert_key IS NOT NULL
      AND occurred_at > NOW() - INTERVAL '7 days'
    ORDER BY occurred_at) TO 'history.csv' CSV HEADER"

Then::

  uv run python ops/db/analysis/alert_policy_replay.py history.csv
  uv run python ops/db/analysis/alert_policy_replay.py history.csv --alerts-yaml alerts.yaml

Without ``--alerts-yaml`` the policy's defaults are replayed, switched on.

What it can and cannot tell:

- Every breached evaluation the history saw is offered to the policy: a
  trigger, a delivery attempt, or a decision not to send (a bucketed summary
  counts as evaluations at its start and end). So the replay checks a
  minimum open time when the producer actually reported, as the gateway does.
- Breaches a producer held back before the alert path (a circuit's
  ``policy_disabled`` or ``rate_limited``) are offered too: that is what the
  policy sees once those producer switches are turned on.
- It cannot replay a detector that did not run; history has no events for it.
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

_BACKEND = Path(__file__).resolve().parents[3] / "apps" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from serving.observability import alert_policy
from serving.observability.alert_config import (
    NotificationPolicy,
    load_alert_config,
)
from serving.observability.alert_types import alert_type_of

#: Decisions that are not an evaluation of a breach.
_NOT_EVALUATIONS = frozenset({"resolution_silenced", "superseded_by_new_episode"})


@dataclass(frozen=True)
class Event:
    """One history row, reduced to what the replay reads."""

    t: float
    kind: str
    key: str
    notification: str
    reason: str
    outcome: str


@dataclass(frozen=True)
class Message:
    """A message posted, or that would have been."""

    t: float
    key: str
    status: str


def read_events(path: Path) -> list[Event]:
    """Read the CSV export, expanding a suppression bucket into its two ends."""
    events: list[Event] = []
    with path.open(newline="") as handle:
        for row in csv.DictReader(handle):
            if not row.get("alert_key") or not row.get("t"):
                continue
            base = Event(
                t=float(row["t"]),
                kind=row["kind"],
                key=row["alert_key"],
                notification=row.get("notification") or "",
                reason=row.get("decision_reason") or "",
                outcome=row.get("outcome") or "",
            )
            events.append(base)
            if row["kind"] == "suppression_summary" and row.get("bucket_start"):
                events.append(
                    Event(
                        float(row["bucket_start"]),
                        base.kind,
                        base.key,
                        base.notification,
                        base.reason,
                        base.outcome,
                    )
                )
    events.sort(key=lambda event: event.t)
    return events


def actual_messages(events: list[Event]) -> list[Message]:
    """Messages the channel really received: every delivery attempt."""
    return [
        Message(e.t, e.key, "resolved" if e.notification == "resolution" else "firing")
        for e in events
        if e.kind == "delivery_attempt"
    ]


def _is_evaluation(event: Event) -> bool:
    if event.kind == "condition_triggered":
        return True
    if event.notification == "resolution":
        return False
    if event.kind == "delivery_attempt":
        return True
    return (
        event.kind in ("notification_decision", "suppression_summary")
        and event.reason not in _NOT_EVALUATIONS
    )


def replay(events: list[Event], policy: NotificationPolicy) -> list[Message]:
    """Messages the channel would have received under ``policy``."""
    alert_policy.reset_notification_policy()
    alert_policy.apply_notification_policy(policy.model_copy(update={"enabled": True}))
    announced: set[str] = set()
    messages: list[Message] = []
    for event in events:
        if event.kind == "condition_ended":
            alert_policy.note_closed(event.key, event.t)
            if event.key in announced:
                announced.discard(event.key)
                messages.append(Message(event.t, event.key, "resolved"))
        elif _is_evaluation(event):
            if alert_policy.hold_reason(event.key, event.t) is None:
                alert_policy.note_delivered(event.key, event.t)
                announced.add(event.key)
                messages.append(Message(event.t, event.key, "firing"))
    return messages


def _stamp(t: float) -> str:
    return datetime.fromtimestamp(t, UTC).strftime("%m-%d %H:%M")


def report(events: list[Event], policy: NotificationPolicy) -> str:
    """Render the comparison as plain text."""
    actual = actual_messages(events)
    would = replay(events, policy)
    lines = []
    if events:
        lines.append(f"History: {_stamp(events[0].t)} to {_stamp(events[-1].t)} UTC")
    lines.append(f"Messages: {len(actual)} actual, {len(would)} under the policy")
    lines.append("")
    lines.append(f"{'alert type':32} {'actual':>7} {'policy':>7}")
    by_type_actual = Counter(alert_type_of(m.key) for m in actual)
    by_type_would = Counter(alert_type_of(m.key) for m in would)
    for kind in sorted(set(by_type_actual) | set(by_type_would)):
        lines.append(f"{kind:32} {by_type_actual[kind]:>7} {by_type_would[kind]:>7}")
    lines.append("")
    lines.append("Messages under the policy:")
    for message in would:
        lines.append(f"  {_stamp(message.t)}  {message.status:8} {message.key}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("history", type=Path, help="CSV export of alert_history_events")
    parser.add_argument("--alerts-yaml", type=Path, help="replay this file's notification_policy")
    args = parser.parse_args(argv)
    policy = (
        load_alert_config(args.alerts_yaml).notification_policy
        if args.alerts_yaml
        else NotificationPolicy()
    )
    print(report(read_events(args.history), policy))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
