"""A release handoff: drain, resume and seal one gateway process.

What a seal waits for is the point of these tests. Two requirements are tested
on their own: work a request leaves behind it (a stream's close, its log row
and charge written in tasks of their own) keeps the process from being sealed
until it is done, and the release's own polling and the platform's probes never
do.
"""

from __future__ import annotations

import asyncio
import types
from typing import Any

import anyio
import httpx
import pytest
from fastapi import FastAPI, Response
from starlette.responses import StreamingResponse

from serving.observability import detached_tasks
from serving.servers import handoff, idle_connection
from serving.servers.handoff import Handoff, HandoffMiddleware, State
from serving.servers.routers import handoff as handoff_routes
from serving.servers.streaming_response import ClosingStreamingResponse

pytestmark = pytest.mark.unit

TOKEN = "t" * 40
#: The real connection reading, before the fixture replaces it.
REAL_CONNECTION_COUNTS = idle_connection.connection_counts


class _Logger:
    def __init__(self) -> None:
        self.report = {"queued": 0, "writing": 0, "dropped": 0, "finished": True}

    def pending(self) -> dict[str, Any]:
        return dict(self.report)


class _Batch:
    def __init__(self) -> None:
        self.report = {"running": True, "paused": False, "items": 0, "jobs": 0, "finished": True}
        self.calls: list[str] = []

    def pause_claims(self) -> None:
        self.calls.append("pause")
        self.report["paused"] = True

    def resume_claims(self) -> None:
        self.calls.append("resume")
        self.report["paused"] = False

    def handoff_report(self) -> dict[str, Any]:
        return dict(self.report)


@pytest.fixture
def world(monkeypatch):
    """A fresh handoff, with every reading it is decided on under the test's control."""
    state = types.SimpleNamespace(
        handoff=Handoff(),
        services=types.SimpleNamespace(completions_logger=_Logger(), batch_scheduler=_Batch()),
        connections={"open": 0, "busy": 0, "control": 0, "other": 0},
        tracked={},
        spend=[],
        scheduled=0,
        paused_jobs=[],
        draining_connections=[],
    )
    monkeypatch.setattr(handoff, "HANDOFF", state.handoff)
    monkeypatch.setattr(idle_connection, "connection_counts", lambda *_: dict(state.connections))
    monkeypatch.setattr(idle_connection, "set_draining", state.draining_connections.append)
    monkeypatch.setattr(
        "serving.observability.tracked_tasks.pending_tracked_tasks", lambda: dict(state.tracked)
    )
    monkeypatch.setattr("serving.spend_ledger.handoff_report", lambda: list(state.spend))
    monkeypatch.setattr("serving.utils.email_scheduler.running_jobs", lambda: state.scheduled)
    monkeypatch.setattr(
        "serving.utils.email_scheduler.pause_jobs", lambda: state.paused_jobs.append("pause")
    )
    monkeypatch.setattr(
        "serving.utils.email_scheduler.resume_jobs", lambda: state.paused_jobs.append("resume")
    )
    monkeypatch.setattr(detached_tasks, "_PENDING", {})
    return state


def _drain(world) -> None:
    assert handoff.drain(world.services, by="test")


# -- states ---------------------------------------------------------------------


def test_drain_quiets_the_process_and_resume_restores_it(world):
    _drain(world)

    assert world.handoff.state is State.DRAINING
    assert world.services.batch_scheduler.calls == ["pause"]
    assert world.paused_jobs == ["pause"]
    assert world.draining_connections == [True]
    assert not handoff.drain(world.services, by="test")  # once only

    assert handoff.resume(world.services, by="test")
    assert world.handoff.state is State.SERVING
    assert world.services.batch_scheduler.calls == ["pause", "resume"]
    assert world.paused_jobs == ["pause", "resume"]
    assert world.draining_connections == [True, False]
    assert [step["to"] for step in world.handoff.history] == ["draining", "serving"]


def test_a_seal_needs_a_drain_first(world):
    assert handoff.seal(world.services, by="test") == ["not_draining"]
    assert world.handoff.state is State.SERVING


def test_an_idle_draining_process_seals_and_can_resume(world):
    _drain(world)

    assert handoff.seal(world.services, by="test") == []
    assert world.handoff.state is State.SEALED
    assert handoff.seal(world.services, by="test") == []  # sealing again succeeds

    assert handoff.resume(world.services, by="test")
    assert world.handoff.state is State.SERVING


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (lambda w: setattr(w.handoff, "requests", 1), "requests_in_flight"),
        (lambda w: w.connections.update(open=1, other=1), "connections_open"),
        (lambda w: w.tracked.update(cost_increment=1), "background_writes"),
        (
            lambda w: w.services.completions_logger.report.update(queued=2, finished=False),
            "request_log_unwritten",
        ),
        (
            lambda w: w.services.batch_scheduler.report.update(items=1, finished=False),
            "batch_work_running",
        ),
        (lambda w: setattr(w, "scheduled", 1), "scheduled_jobs_running"),
        (lambda w: w.spend.append({"mode": "local", "finished": False}), "spend_unwritten"),
    ],
)
def test_anything_left_keeps_the_process_draining(world, change, reason):
    _drain(world)
    change(world)

    assert handoff.seal(world.services, by="test") == [reason]
    assert world.handoff.state is State.DRAINING


def test_what_never_finishes_by_waiting_is_reported_but_does_not_block(world):
    _drain(world)
    # The shared ledger's hourly upkeep runs for the life of the process; a
    # refused cost is never written again. Neither is a reason to keep it.
    world.tracked["spend_upkeep"] = 1
    world.spend.append({"mode": "local", "finished": True, "refused_requests": 3})

    report = handoff.status(world.services)
    assert report["tasks"]["tracked"] == {}
    assert report["spend"][0]["refused_requests"] == 3
    assert handoff.seal(world.services, by="test") == []


async def test_a_detached_write_keeps_the_process_draining_until_it_ends(world):
    _drain(world)
    gate = asyncio.Event()
    task = detached_tasks.rejection_log(gate.wait())

    assert handoff.seal(world.services, by="test") == ["background_writes"]
    gate.set()
    await task
    assert handoff.seal(world.services, by="test") == []


# -- the middleware ---------------------------------------------------------------


async def _plain(scope, receive, send) -> None:
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


async def _call(app, path: str = "/v1/chat/completions") -> list[dict[str, Any]]:
    sent: list[dict[str, Any]] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    await app({"type": "http", "path": path, "headers": []}, receive, send)
    return sent


def _header(message: dict[str, Any], name: bytes) -> bytes | None:
    for key, value in message.get("headers", []):
        if key == name:
            return value
    return None


async def test_a_request_is_counted_while_it_runs(world):
    seen: list[int] = []

    async def app(scope, receive, send):
        seen.append(world.handoff.requests)
        await _plain(scope, receive, send)

    await _call(HandoffMiddleware(app))

    assert seen == [1]
    assert world.handoff.requests == 0
    assert world.handoff.admitted == 1
    assert world.handoff.last_admitted_at is not None


async def test_responses_close_their_connection_only_while_draining(world):
    app = HandoffMiddleware(_plain)

    serving = await _call(app)
    assert _header(serving[0], b"connection") is None

    _drain(world)
    draining = await _call(app)
    assert _header(draining[0], b"connection") == b"close"
    assert world.handoff.admitted_since_drain == 1


async def test_a_stream_is_counted_as_one(world):
    seen: list[int] = []

    async def app(scope, receive, send):
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/event-stream; charset=utf-8")],
            }
        )
        seen.append(world.handoff.streams)
        await send({"type": "http.response.body", "body": b"data: [DONE]\n\n"})

    await _call(HandoffMiddleware(app))

    assert seen == [1]
    assert world.handoff.streams == 0


async def test_once_sealed_a_request_is_refused_before_anything_runs(world):
    ran: list[str] = []

    async def app(scope, receive, send):
        ran.append(scope["path"])
        await _plain(scope, receive, send)

    _drain(world)
    assert handoff.seal(world.services, by="test") == []
    sent = await _call(HandoffMiddleware(app))

    assert ran == []
    assert sent[0]["status"] == 503
    assert _header(sent[0], b"connection") == b"close"
    assert b"instance_sealed" in sent[1]["body"]
    assert world.handoff.refused_after_seal == 1
    assert world.handoff.requests == 0


@pytest.mark.parametrize(
    "path", ["/health", "/health/ready", "/internal/handoff", "/internal/handoff/seal"]
)
async def test_probes_and_release_calls_are_served_when_sealed(world, path):
    ran: list[str] = []

    async def app(scope, receive, send):
        ran.append(scope["path"])
        await _plain(scope, receive, send)

    _drain(world)
    handoff.seal(world.services, by="test")
    sent = await _call(HandoffMiddleware(app), path)

    assert ran == [path]
    assert sent[0]["status"] == 200
    assert world.handoff.refused_after_seal == 0


async def test_probes_and_release_calls_in_progress_do_not_hold_a_drain(world):
    """The release polls and the platform probes all along; a drain still ends."""
    gate = asyncio.Event()
    verdicts: list[list[str]] = []

    async def slow_probe(scope, receive, send):
        await gate.wait()
        await _plain(scope, receive, send)

    async def sealing_call(scope, receive, send):
        verdicts.append(handoff.seal(world.services, by="test"))
        await _plain(scope, receive, send)

    _drain(world)
    probe = asyncio.create_task(_call(HandoffMiddleware(slow_probe), "/health/ready"))
    await asyncio.sleep(0)
    # Two connections are open, the probe's and the seal call's, each with a
    # control call in progress.
    world.connections.update(open=2, busy=2, control=2, other=0)
    await _call(HandoffMiddleware(sealing_call), "/internal/handoff/seal")

    assert verdicts == [[]]
    assert world.handoff.state is State.SEALED
    gate.set()
    await probe


async def test_a_connection_beyond_the_release_calls_holds_the_seal(world):
    verdicts: list[list[str]] = []

    async def sealing_call(scope, receive, send):
        verdicts.append(handoff.seal(world.services, by="test"))
        await _plain(scope, receive, send)

    _drain(world)
    # The seal call's connection, and one more that could still bring a request.
    world.connections.update(open=2, busy=1, control=1, other=1)
    await _call(HandoffMiddleware(sealing_call), "/internal/handoff/seal")

    assert verdicts == [["connections_open"]]
    assert world.handoff.state is State.DRAINING


# -- a stream's close is part of the request ----------------------------------------


def _blocked_client():
    """A client that reads one chunk, then stops reading, then hangs up."""
    hang_up = asyncio.Event()
    sent: list[dict[str, Any]] = []

    async def receive():
        await hang_up.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)
        if message["type"] == "http.response.body" and len(sent) > 2:
            await asyncio.Event().wait()  # the client stopped reading

    return hang_up, receive, send, sent


def _stream(closed: list[str]):
    async def body():
        try:
            for chunk in (b"one", b"two", b"three"):
                yield chunk
        except GeneratorExit:
            # Where a gateway stream bills what it did and queues its row.
            closed.append("finalized")
            raise

    return body()


SCOPE = {
    "type": "http",
    "path": "/v1/chat/completions",
    "headers": [],
    "asgi": {"spec_version": "2.3"},
}


async def test_a_plain_streaming_response_leaves_an_abandoned_body_unfinished():
    """The defect: the body's close waits for the garbage collector."""
    closed: list[str] = []
    response = StreamingResponse(_stream(closed))
    hang_up, receive, send, _ = _blocked_client()

    call = asyncio.create_task(response(SCOPE, receive, send))
    await asyncio.sleep(0.01)
    hang_up.set()
    await call

    assert closed == []
    await response.body_iterator.aclose()


async def test_the_closing_response_finishes_the_body_before_the_request_ends():
    closed: list[str] = []
    response = ClosingStreamingResponse(_stream(closed))
    hang_up, receive, send, _ = _blocked_client()

    call = asyncio.create_task(response(SCOPE, receive, send))
    await asyncio.sleep(0.01)
    hang_up.set()
    await call

    assert closed == ["finalized"]


async def test_the_body_is_finished_even_while_the_request_is_cancelled():
    """The gateway's own timeout cancels the request; the close is shielded from it.

    A gateway stream's close awaits (it closes its upstream reader), and an
    await inside a cancelled scope is cancelled again unless shielded.
    """
    closed: list[str] = []

    async def body():
        try:
            for chunk in (b"one", b"two", b"three"):
                yield chunk
        finally:
            await asyncio.sleep(0)  # closing the upstream reader
            closed.append("finalized")

    response = ClosingStreamingResponse(body())
    _, receive, send, _ = _blocked_client()

    with anyio.move_on_after(0.02):
        await response(SCOPE, receive, send)

    assert closed == ["finalized"]


async def test_work_the_close_leaves_behind_keeps_the_process_draining(world):
    """In flight drops to zero when the request ends; what it queued still counts."""
    gate = asyncio.Event()

    async def body():
        try:
            yield b"one"
            yield b"two"
        except GeneratorExit:
            detached_tasks.detach(gate.wait(), name="request_log_fallback")
            raise

    async def app(scope, receive, send):
        await ClosingStreamingResponse(body())(scope, receive, send)

    hang_up, receive, send, _ = _blocked_client()
    _drain(world)
    call = asyncio.create_task(HandoffMiddleware(app)({**SCOPE}, receive, send))
    await asyncio.sleep(0.01)
    assert handoff.seal(world.services, by="test") == ["requests_in_flight"]

    hang_up.set()
    await call
    assert world.handoff.requests == 0
    assert handoff.seal(world.services, by="test") == ["background_writes"]

    gate.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert handoff.seal(world.services, by="test") == []


# -- the routes -------------------------------------------------------------------


@pytest.fixture
def client(world, monkeypatch):
    monkeypatch.setenv("HANDOFF_TOKEN", TOKEN)
    monkeypatch.setattr(handoff, "refusal_to_drain", lambda: None)
    app = FastAPI()
    app.state.services = world.services
    app.include_router(handoff_routes.router)
    app.add_middleware(HandoffMiddleware)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gw")


AUTH = {"Authorization": f"Bearer {TOKEN}"}


async def test_the_routes_do_not_exist_without_a_token(client, monkeypatch):
    monkeypatch.delenv("HANDOFF_TOKEN")
    async with client:
        assert (await client.get("/internal/handoff", headers=AUTH)).status_code == 404
        assert (await client.post("/internal/handoff/drain", headers=AUTH)).status_code == 404


async def test_a_short_token_leaves_the_routes_disabled(client, monkeypatch):
    monkeypatch.setenv("HANDOFF_TOKEN", "short")
    async with client:
        response = await client.get("/internal/handoff", headers={"Authorization": "Bearer short"})
    assert response.status_code == 404


async def test_a_wrong_or_missing_credential_is_refused(client, world):
    async with client:
        assert (await client.get("/internal/handoff")).status_code == 401
        wrong = {"Authorization": "Bearer " + "x" * 40}
        assert (await client.post("/internal/handoff/drain", headers=wrong)).status_code == 401
    assert world.handoff.state is State.SERVING


async def test_a_release_drains_polls_and_seals(client, world):
    async with client:
        drained = (await client.post("/internal/handoff/drain", headers=AUTH)).json()
        assert drained["changed"] is True
        assert drained["state"] == "draining"

        world.handoff.requests = 1  # a stream still running
        blocked = await client.post("/internal/handoff/seal", headers=AUTH)
        assert blocked.status_code == 409
        assert blocked.json()["blocking"] == ["requests_in_flight"]

        world.handoff.requests = 0
        status = (await client.get("/internal/handoff", headers=AUTH)).json()
        assert status["idle"] is True
        assert status["requests"]["control_in_flight"] == 1  # this call

        sealed = await client.post("/internal/handoff/seal", headers=AUTH)
        assert sealed.status_code == 200
        assert sealed.json()["state"] == "sealed"

        refused = await client.post("/v1/chat/completions", json={})
        assert refused.status_code == 503
        assert (await client.get("/internal/handoff", headers=AUTH)).json()["requests"][
            "refused_after_seal"
        ] == 1

        resumed = (await client.post("/internal/handoff/resume", headers=AUTH)).json()
        assert resumed["state"] == "serving"


async def test_several_workers_cannot_be_drained(client, world, monkeypatch):
    monkeypatch.setattr(handoff, "refusal_to_drain", lambda: "several workers")
    async with client:
        response = await client.post("/internal/handoff/drain", headers=AUTH)
    assert response.status_code == 409
    assert world.handoff.state is State.SERVING


async def test_readiness_fails_while_draining_and_sealed(world):
    from serving.servers.routers import health

    _drain(world)
    response = Response()
    body = await health.health_ready(request=None, response=response, op_store=None, log_store=None)
    assert (response.status_code, body["reason"]) == (503, "draining")

    handoff.seal(world.services, by="test")
    response = Response()
    body = await health.health_ready(request=None, response=response, op_store=None, log_store=None)
    assert (response.status_code, body["reason"]) == (503, "sealed")


async def test_the_playground_badge_wrapper_closes_the_stream_it_wraps():
    """The playground wraps the chat stream; closing the wrapper must close the stream."""
    from serving.servers.routers import playground

    closed: list[str] = []
    response = ClosingStreamingResponse(_stream(closed))
    stream = types.SimpleNamespace(session=types.SimpleNamespace(served_route=None))
    wrapped = playground._with_route_frame(response, stream, show_host=False)

    first = await wrapped.body_iterator.__anext__()
    assert first == b"one"
    await wrapped.body_iterator.aclose()

    assert closed == ["finalized"]


async def test_a_draining_response_replaces_a_keep_alive_header(world):
    async def app(scope, receive, send):
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"connection", b"keep-alive")],
            }
        )
        await send({"type": "http.response.body", "body": b""})

    _drain(world)
    sent = await _call(HandoffMiddleware(app))

    assert [v for k, v in sent[0]["headers"] if k == b"connection"] == [b"close"]


async def test_the_seal_reads_each_connection_s_own_request(world, monkeypatch):
    """Through the real reading: the release's own connection does not hold the seal."""

    class _Open:
        def is_closing(self) -> bool:
            return False

    def watch(path):
        return idle_connection.IdleWatch(
            _Open(), lambda: 600.0, lambda: "peer", lambda: path is not None, lambda: path
        )

    monkeypatch.setattr(idle_connection, "connection_counts", REAL_CONNECTION_COUNTS)
    monkeypatch.setattr(idle_connection, "_BY_TRANSPORT", {1: watch("/internal/handoff/seal")})
    _drain(world)
    assert handoff.seal(world.services, by="test") == []

    handoff.resume(world.services, by="test")
    _drain(world)
    monkeypatch.setattr(
        idle_connection,
        "_BY_TRANSPORT",
        {1: watch("/internal/handoff/seal"), 2: watch(None)},
    )
    assert handoff.seal(world.services, by="test") == ["connections_open"]
