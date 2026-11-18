"""Handing this process's traffic to another one: drain, resume, seal.

A release that starts a new gateway process beside the old one has to know when
the old one may stop without cutting a request short or losing what it still
has to write. Waiting a fixed time cannot tell; this module lets the release
ask, and decide, while the old process is still alive and can go back to
serving:

``serving``
    The normal state.
``draining`` (:func:`drain`)
    The process still serves every request that reaches it, but says it should
    get no more: ``/health/ready`` answers 503, and every response carries
    ``Connection: close``, so a client or entry holding a kept-alive connection
    opens its next request elsewhere. A connection with no request in progress
    is closed after uvicorn's own keep-alive limit
    (:data:`serving.servers.idle_connection.DRAIN_IDLE_SECONDS`), one that has
    never been used included. The process claims no new batch items and runs
    no scheduled job (the jobs alert and roll up for the whole deployment, and
    the other process runs them); batch items it is running go on, their
    leases kept.
``sealed`` (:func:`seal`)
    Reached only from ``draining``, and only at a moment when nothing is left:
    no request in progress, no client connection open but the ones carrying
    the release's own calls, no work a request left behind, no batch item or
    scheduled job running, no request-log row or cost waiting to be written,
    and nothing whose recording is not confirmed: a cost the counters refused
    or whose write had an unknown outcome, a shared reservation an operator
    has to resolve, a request-log row dropped or whose write failed, a stream whose close was
    abandoned (:func:`blockers`). The last kind never resolves by waiting; a
    process holding any stays draining, and the release keeps it and is
    recorded unfinished. Checking and switching is one step with no ``await``
    in between, so no request can start in the gap. From then on a request is
    refused with 503 before anything is done for it -- no upstream call, no
    cost -- and counted (``refused_after_seal``): a release that saw any of
    them did not hand over cleanly. The process can now be stopped and loses
    nothing.

:func:`resume` returns to ``serving`` from either state, which is how a release
is abandoned.

What counts as a request: everything except the probes and the release's own
calls (:func:`is_control_path`). Those must not hold a drain open -- the release
polls, the platform probes -- so they are counted apart, never refused, and
their connections are not counted against the seal while they run.

Stopping is unchanged: a signal still gives open requests uvicorn's grace
period and cancels them. A process that was sealed first has none.

One process only. With several worker processes behind one port, each would
drain on its own and the release could not reach a chosen one, so the calls are
refused there (:func:`refusal_to_drain`).
"""

from __future__ import annotations

import enum
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from serving.utils.logging import get_logger

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = get_logger(__name__)

#: The release's own calls (:mod:`serving.servers.routers.handoff`).
HANDOFF_PATH = "/internal/handoff"

#: Tasks named here run for the life of the process and never "finish"; a seal
#: does not wait for them. The shared spend ledger's hourly upkeep is one.
STANDING_TASKS = frozenset({"spend_upkeep"})


class State(enum.Enum):
    """Where this process stands in a handoff."""

    SERVING = "serving"
    DRAINING = "draining"
    SEALED = "sealed"


def is_control_path(path: str) -> bool:
    """Whether *path* is a probe or a call of the release, rather than a request to serve."""
    return (
        path == "/health"
        or path.startswith("/health/")
        or path == HANDOFF_PATH
        or path.startswith(HANDOFF_PATH + "/")
    )


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class Handoff:
    """This process's handoff state and the counts it is decided on."""

    state: State = State.SERVING
    #: Requests in progress, of which streams (a response that started as
    #: ``text/event-stream``), and probes or release calls in progress.
    requests: int = 0
    streams: int = 0
    control: int = 0
    #: Requests started since the process did, and since it last began to drain.
    admitted: int = 0
    admitted_since_drain: int = 0
    #: When the last request started (wall clock, ISO 8601), whatever the state.
    last_admitted_at: str | None = None
    #: Requests refused because the process was sealed.
    refused_after_seal: int = 0
    draining_since: str | None = None
    sealed_at: str | None = None
    #: Every change of state, newest last, so a status shows what happened.
    history: list[dict[str, str]] = field(default_factory=list)

    def enter(self, control: bool) -> None:
        """Count a request (or a control call) that is starting."""
        if control:
            self.control += 1
            return
        self.requests += 1
        self.admitted += 1
        if self.state is not State.SERVING:
            self.admitted_since_drain += 1
        self.last_admitted_at = _now()

    def leave(self, control: bool, streaming: bool) -> None:
        """Count a request (or a control call) that has ended."""
        if control:
            self.control -= 1
            return
        self.requests -= 1
        if streaming:
            self.streams -= 1

    def move(self, state: State, *, by: str) -> None:
        """Change state, recording when and why."""
        now = _now()
        if state is State.DRAINING and self.state is State.SERVING:
            self.draining_since = now
            self.admitted_since_drain = 0
        if state is State.SEALED:
            self.sealed_at = now
        if state is State.SERVING:
            self.draining_since = None
            self.sealed_at = None
        self.history.append({"from": self.state.value, "to": state.value, "at": now, "by": by})
        del self.history[:-20]
        self.state = state

    def counts(self) -> dict[str, Any]:
        """The request counts, for the status."""
        return {
            "in_flight": self.requests,
            "streams": self.streams,
            "control_in_flight": self.control,
            "admitted": self.admitted,
            "admitted_since_drain": self.admitted_since_drain,
            "last_admitted_at": self.last_admitted_at,
            "refused_after_seal": self.refused_after_seal,
        }


#: This process's handoff.
HANDOFF = Handoff()


# -- what is left -------------------------------------------------------------


def leftovers(services: Any, handoff: Handoff | None = None) -> dict[str, Any]:
    """Everything this process still has under way or to write, read now, without awaiting.

    Each part says whether it is finished. Each spend ledger also says whether
    every cost it recorded is confirmed in the counters (``confirmed``), with
    what is not; the request log counts the rows it dropped or failed to write, refused requests' rows included; ``streams`` counts
    the bodies whose close was abandoned. None of those resolves by waiting,
    and each keeps the process from being sealed (:func:`blockers`).
    """
    from serving.observability.detached_tasks import pending as detached_pending
    from serving.observability.rejection_log import lost_rows as rejected_lost
    from serving.observability.tracked_tasks import pending_tracked_tasks
    from serving.servers import idle_connection, streaming_response
    from serving.spend_ledger import handoff_report as spend_report
    from serving.utils import email_scheduler

    handoff = handoff or HANDOFF
    connections = idle_connection.connection_counts(is_control_path)
    tracked = pending_tracked_tasks()
    owed_tasks = {name: n for name, n in tracked.items() if name not in STANDING_TASKS}
    # ``rejected_lost``: rows of refused requests, written apart from the queue.
    logger_report = {**_request_log(services), "rejected_lost": rejected_lost()}
    batch = _batch(services)
    spend = spend_report()
    scheduled = email_scheduler.running_jobs()
    return {
        "requests": handoff.counts(),
        # ``other``: the open connections that could still bring a request,
        # every one but those carrying a probe or a release call right now.
        "connections": connections,
        "tasks": {"tracked": owed_tasks, "detached": detached_pending()},
        "request_log": logger_report,
        "streams": {"close_abandoned": streaming_response.abandoned_closes()},
        "batch": batch,
        "scheduled_jobs": scheduled,
        "spend": spend,
    }


def blockers(services: Any, handoff: Handoff | None = None) -> list[str]:
    """Why this process cannot be sealed now; empty when it can. Never awaits."""
    report = leftovers(services, handoff)
    return _blockers(report)


def _blockers(report: dict[str, Any]) -> list[str]:
    found = []
    if report["requests"]["in_flight"]:
        found.append("requests_in_flight")
    if report["connections"]["other"]:
        found.append("connections_open")
    if any(report["tasks"]["tracked"].values()) or any(report["tasks"]["detached"].values()):
        found.append("background_writes")
    if not report["request_log"]["finished"]:
        found.append("request_log_unwritten")
    log = report["request_log"]
    if log["dropped"] or log["failed"] or log["rejected_lost"]:
        found.append("request_log_lost")
    if report["streams"]["close_abandoned"]:
        found.append("stream_close_abandoned")
    if not report["batch"]["finished"]:
        found.append("batch_work_running")
    if report["scheduled_jobs"]:
        found.append("scheduled_jobs_running")
    if not all(ledger["finished"] for ledger in report["spend"]):
        found.append("spend_unwritten")
    # Read as unconfirmed unless a ledger says otherwise.
    if not all(ledger.get("confirmed") is True for ledger in report["spend"]):
        found.append("spend_unconfirmed")
    return found


def _request_log(services: Any) -> dict[str, Any]:
    completions_logger = getattr(services, "completions_logger", None)
    if completions_logger is None:
        return {"queued": 0, "writing": 0, "dropped": 0, "failed": 0, "finished": True}
    return completions_logger.pending()


def _batch(services: Any) -> dict[str, Any]:
    scheduler = getattr(services, "batch_scheduler", None)
    if scheduler is None:
        return {"running": False, "paused": False, "items": 0, "jobs": 0, "finished": True}
    return scheduler.handoff_report()


# -- the transitions ----------------------------------------------------------


def refusal_to_drain() -> str | None:
    """Why this process cannot take part in a handoff, or None when it can."""
    from serving.utils.workers import configured_worker_count, spawned_by_worker_manager

    workers = configured_worker_count()
    # gunicorn forks its workers, so neither of the checks below sees them.
    if (workers is not None and workers > 1) or spawned_by_worker_manager() or _under_gunicorn():
        return (
            "Several worker processes serve this port. A handoff drains one process, "
            "and a call reaches whichever worker accepts it; run one worker per instance."
        )
    return None


def _under_gunicorn() -> bool:
    import sys

    return "gunicorn" in sys.modules


def drain(services: Any, *, by: str, handoff: Handoff | None = None) -> bool:
    """Begin draining; True when the state changed (False: already draining or sealed)."""
    handoff = handoff or HANDOFF
    if handoff.state is not State.SERVING:
        return False
    handoff.move(State.DRAINING, by=by)
    _quiet(services, True)
    logger.warning(
        "handoff: draining; requests in flight %d",
        handoff.requests,
        extra={"event": "handoff_draining", "handoff_in_flight": handoff.requests},
    )
    return True


def resume(services: Any, *, by: str, handoff: Handoff | None = None) -> bool:
    """Return to serving; True when the state changed (False: already serving)."""
    handoff = handoff or HANDOFF
    if handoff.state is State.SERVING:
        return False
    handoff.move(State.SERVING, by=by)
    _quiet(services, False)
    logger.warning("handoff: serving again", extra={"event": "handoff_resumed"})
    return True


def seal(services: Any, *, by: str, handoff: Handoff | None = None) -> list[str]:
    """Seal if nothing is left; return what is left otherwise (the state then stays).

    The check and the switch are one synchronous step: no request can begin
    between them, because a request is admitted by the event loop this runs on.
    Sealing an already sealed process succeeds again.
    """
    handoff = handoff or HANDOFF
    if handoff.state is State.SEALED:
        return []
    if handoff.state is not State.DRAINING:
        return ["not_draining"]
    found = blockers(services, handoff)
    if found:
        return found
    handoff.move(State.SEALED, by=by)
    _report_lifecycle("sealed")
    logger.warning("handoff: sealed", extra={"event": "handoff_sealed"})
    return []


def _quiet(services: Any, draining: bool) -> None:
    """Stop, or restart, what this process does for the whole deployment, and wind down connections."""
    from serving.servers import idle_connection
    from serving.utils import email_scheduler

    idle_connection.set_draining(draining)
    scheduler = getattr(services, "batch_scheduler", None)
    if scheduler is not None:
        if draining:
            scheduler.pause_claims()
        else:
            scheduler.resume_claims()
    if draining:
        email_scheduler.pause_jobs()
    else:
        email_scheduler.resume_jobs()
    _report_lifecycle("draining" if draining else "running")


def _report_lifecycle(lifecycle: str) -> None:
    """Show the state in this process's registration (``config_instances``)."""
    from serving.config import live as live_config

    reporter = getattr(live_config.COORDINATOR, "reporter", None)
    if reporter is not None:
        reporter.set_lifecycle(lifecycle)


def status(services: Any, handoff: Handoff | None = None) -> dict[str, Any]:
    """The state, what is left and whether a seal would succeed now."""
    from serving.config.instances import build_id
    from serving.servers import restart
    from serving.shared_spend import mode as spend_mode

    handoff = handoff or HANDOFF
    report = leftovers(services, handoff)
    found = _blockers(report)
    return {
        "instance": restart.INSTANCE_ID,
        "build": build_id(),
        # The accounting this process reserves and settles in: a release
        # compares the process it starts with this one before handing over.
        "capabilities": {"spend_accounting": spend_mode.capability()},
        "state": handoff.state.value,
        "draining_since": handoff.draining_since,
        "sealed_at": handoff.sealed_at,
        "idle": not found,
        "blocking": found,
        **report,
        "history": list(handoff.history),
    }


# -- the middleware -----------------------------------------------------------

_SEALED_BODY = json.dumps(
    {
        "error": {
            "message": (
                "This gateway instance has handed its traffic to another one. "
                "Nothing was done for this request; send it again."
            ),
            "type": "service_unavailable",
            "code": "instance_sealed",
        }
    }
).encode()


class HandoffMiddleware:
    """Counts every request, marks responses while draining, refuses requests once sealed.

    Pure ASGI: it wraps ``send`` only and never touches a body, so streams pass
    as they are. A request counts until the application has returned, which
    is after its response, its dependencies' teardown and its body's close
    (:class:`~serving.servers.streaming_response.ClosingStreamingResponse`).
    """

    def __init__(self, app: ASGIApp, handoff: Handoff | None = None) -> None:
        self.app = app
        self._handoff = handoff

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Count the request while it runs, or refuse it when the process is sealed."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        handoff = self._handoff or HANDOFF
        control = is_control_path(scope.get("path", ""))
        if not control and handoff.state is State.SEALED:
            handoff.refused_after_seal += 1
            logger.warning(
                "handoff: refused a request that arrived after the seal",
                extra={
                    "event": "handoff_refused_after_seal",
                    "handoff_refused_after_seal": handoff.refused_after_seal,
                },
            )
            await _refuse(send)
            return
        handoff.enter(control)
        streaming = False

        async def marked_send(message: Message) -> None:
            nonlocal streaming
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                if not control and _is_event_stream(headers):
                    streaming = True
                    handoff.streams += 1
                if handoff.state is not State.SERVING and not _has_connection_close(headers):
                    # Replaces a ``Connection: keep-alive`` the response set.
                    headers = [(k, v) for k, v in headers if k.lower() != b"connection"]
                    headers.append((b"connection", b"close"))
                    message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, marked_send)
        finally:
            handoff.leave(control, streaming)


def _is_event_stream(headers: list[tuple[bytes, bytes]]) -> bool:
    return any(
        name.lower() == b"content-type" and value.lower().startswith(b"text/event-stream")
        for name, value in headers
    )


def _has_connection_close(headers: list[tuple[bytes, bytes]]) -> bool:
    return any(
        name.lower() == b"connection" and value.lower() == b"close" for name, value in headers
    )


async def _refuse(send: Send) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": 503,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(_SEALED_BODY)).encode()),
                (b"retry-after", b"0"),
                (b"connection", b"close"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": _SEALED_BODY})


def log_at_shutdown(handoff: Handoff | None = None) -> None:
    """Write where the handoff stood as the process began to stop, for the release record."""
    handoff = handoff or HANDOFF
    counts = handoff.counts()
    level = (
        logger.info if handoff.state is State.SEALED and not handoff.requests else logger.warning
    )
    level(
        "handoff: stopping in state %s with %d request(s) in flight",
        handoff.state.value,
        handoff.requests,
        extra={
            "event": "handoff_state_at_shutdown",
            "handoff_state": handoff.state.value,
            "handoff_in_flight": counts["in_flight"],
            "handoff_refused_after_seal": counts["refused_after_seal"],
        },
    )


def reset(handoff: Handoff | None = None) -> None:
    """Back to a fresh process's state (tests)."""
    target = handoff or HANDOFF
    fresh = Handoff()
    for name in fresh.__dataclass_fields__:
        setattr(target, name, getattr(fresh, name))


__all__ = [
    "HANDOFF",
    "HANDOFF_PATH",
    "Handoff",
    "HandoffMiddleware",
    "State",
    "blockers",
    "drain",
    "is_control_path",
    "leftovers",
    "log_at_shutdown",
    "refusal_to_drain",
    "reset",
    "resume",
    "seal",
    "status",
]
