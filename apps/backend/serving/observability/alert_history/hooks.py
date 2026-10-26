"""The calls the alert path makes into history. Each is a no-op when disabled.

Every function here returns quickly, never awaits and never raises: history is
not allowed to change what an alert does, how long it takes beyond a few dict
operations, or what its caller is told. A failure is logged at DEBUG on the
history diagnostics logger and otherwise ignored.
"""

from __future__ import annotations

import contextvars
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from serving.observability.alert_history.capture import DeliveryOutcome
from serving.observability.alert_history.queue import diag

if TYPE_CHECKING:
    from collections.abc import Mapping

    from serving.observability.alert_history.capture import AttemptHandle, BackendCapture

_CAPTURE: BackendCapture | None = None


def install(capture: BackendCapture | None) -> None:
    """Make ``capture`` the process's recorder (``None`` turns capture off)."""
    global _CAPTURE
    _CAPTURE = capture


def active() -> BackendCapture | None:
    """The installed recorder, or ``None``."""
    return _CAPTURE


def _safe(name: str, fn: Any, *args: Any, **kwargs: Any) -> Any:
    try:
        return fn(*args, **kwargs)
    except Exception:
        diag.debug("alert history hook %s failed", name, exc_info=True)
        return None


def breach(
    key: str,
    *,
    severity: Any,
    title: str,
    kind: str,
    context: Mapping[str, Any] | None,
    now: float | None = None,
) -> str | None:
    """A breached evaluation; returns the episode it belongs to."""
    capture = _CAPTURE
    if capture is None:
        return None
    return _safe(
        "breach",
        capture.on_breach,
        key,
        severity=getattr(severity, "value", severity),
        title=title,
        kind=kind,
        context=context,
        now=now,
    )


def condition_ended(key: str, *, basis: str, now: float | None = None) -> str | None:
    """The condition on ``key`` ended; returns the episode its recovery belongs to."""
    capture = _CAPTURE
    if capture is None:
        return None
    return _safe("condition_ended", capture.on_end, key, basis=basis, now=now)


def open_episode(key: str) -> str | None:
    """The episode open on ``key``, if any."""
    capture = _CAPTURE
    if capture is None:
        return None
    return _safe("open_episode", capture.open_episode, key)


def closed_episode(key: str) -> str | None:
    """The last ended episode on ``key``, if any."""
    capture = _CAPTURE
    if capture is None:
        return None
    return _safe("closed_episode", capture.closed_episode, key)


def decision(key: str, *, episode_id: str | None, reason: str, resolution: bool = False) -> None:
    """A notification was not attempted, for ``reason``."""
    capture = _CAPTURE
    if capture is None:
        return
    _safe(
        "decision",
        capture.decision,
        key,
        episode_id=episode_id,
        reason=reason,
        resolution=resolution,
    )


def external_decision(
    key: str,
    *,
    reason: str,
    severity: Any,
    title: str,
    kind: str,
    context: Mapping[str, Any] | None = None,
) -> None:
    """A producer held a breach back before it reached the alert path.

    The breach is still a fact: it opens (or joins) the episode, and the
    decision is recorded against it.
    """
    capture = _CAPTURE
    if capture is None:
        return
    episode_id = breach(key, severity=severity, title=title, kind=kind, context=context)
    decision(key, episode_id=episode_id, reason=reason)


def observed_healthy(key: str) -> None:
    """A producer that skips the alert path saw ``key`` healthy: end its episode."""
    capture = _CAPTURE
    if capture is None or capture.open_episode(key) is None:
        return
    condition_ended(key, basis="observed_healthy")


def retry_terminated(
    key: str, *, episode_id: str | None, reason: str, attempts: int | None = None
) -> None:
    """A recovery card stopped being retried."""
    capture = _CAPTURE
    if capture is None:
        return
    _safe(
        "retry_terminated",
        capture.retry_terminated,
        key,
        episode_id=episode_id,
        reason=reason,
        attempts=attempts,
    )


def forget(key: str, *, episode_id: str | None) -> None:
    """The notification side is done with ``episode_id`` on ``key``."""
    capture = _CAPTURE
    if capture is None:
        return
    _safe("forget", capture.forget, key, episode_id=episode_id)


def attempt_begin(key: str, **kwargs: Any) -> AttemptHandle | None:
    """A real send is starting."""
    capture = _CAPTURE
    if capture is None:
        return None
    return _safe("attempt_begin", capture.attempt_begin, key, **kwargs)


def attempt_end(handle: AttemptHandle | None, outcome: DeliveryOutcome) -> None:
    """The send started by :func:`attempt_begin` finished."""
    capture = _CAPTURE
    if capture is None or handle is None:
        return
    _safe("attempt_end", capture.attempt_end, handle, outcome)


def render_failed(key: str, *, episode_id: str | None, resolution: bool, sink: Any) -> None:
    """The card could not be rendered; nothing was sent."""
    capture = _CAPTURE
    if capture is None:
        return
    _safe(
        "render_failed",
        capture.render_failed,
        key,
        episode_id=episode_id,
        resolution=resolution,
        sink=sink,
    )


# -- detailed delivery results ----------------------------------------------


@dataclass
class DeliveryNote:
    """Filled in by the senders while a post runs, read back by ``post_detailed``."""

    outcome: str | None = None
    error_code: str | None = None
    http_status: int | None = None


_NOTE: contextvars.ContextVar[DeliveryNote | None] = contextvars.ContextVar(
    "alert_delivery_note", default=None
)


def note_delivery(
    outcome: str, *, error_code: str | None = None, status: int | None = None
) -> None:
    """Called by a sender to say what happened to the post it is making."""
    note = _NOTE.get()
    if note is None:
        return
    note.outcome = outcome
    note.error_code = error_code
    note.http_status = status


async def post_detailed(sink: Any, message: str, *, dedupe_key: str) -> DeliveryOutcome:
    """Run ``sink.post`` and report what happened, keeping its bool as ``accepted``.

    The bool the sink returns is the answer; the note only explains it. A sink
    that does not fill the note in (a test double, a third-party sink) gets
    ``accepted`` for ``True`` and ``unknown`` for ``False`` — a bare ``False``
    cannot say whether the message left. Exceptions propagate unchanged.
    """
    note = DeliveryNote()
    token = _NOTE.set(note)
    try:
        accepted = bool(await sink.post(message, dedupe_key=dedupe_key))
    finally:
        _NOTE.reset(token)
    if accepted:
        return DeliveryOutcome(True, "accepted", None, note.http_status)
    outcome = note.outcome if note.outcome in ("rejected", "not_sent", "unknown") else "unknown"
    return DeliveryOutcome(
        False, outcome, note.error_code or "sink_reported_failure", note.http_status
    )
