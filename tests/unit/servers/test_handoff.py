"""A release handoff: drain, resume and seal one gateway process.

What a seal waits for is the point of these tests. Two requirements are tested
on their own: work a request leaves behind it (a stream's close, its log row
and charge written in tasks of their own) keeps the process from being sealed
until it is done, and the release's own polling and the platform's probes never
do.
"""

from __future__ import annotations

import asyncio
import json
import types
from typing import Any

import anyio
import httpx
import pytest
from fastapi import FastAPI, Response
from starlette.responses import StreamingResponse

from serving.observability import detached_tasks, rejection_log
from serving.servers import handoff, idle_connection, streaming_response
from serving.servers.handoff import Handoff, HandoffMiddleware, State
from serving.servers.routers import handoff as handoff_routes
from serving.servers.streaming_response import ClosingStreamingResponse

pytestmark = pytest.mark.unit

TOKEN = "t" * 40
#: The real connection reading, before the fixture replaces it.
REAL_CONNECTION_COUNTS = idle_connection.connection_counts


class _Logger:
    def __init__(self) -> None:
        self.report = {"queued": 0, "writing": 0, "dropped": 0, "failed": 0, "finished": True}

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
    monkeypatch.setattr(streaming_response, "_ABANDONED_CLOSES", 0)
    monkeypatch.setattr(rejection_log, "_LOST_ROWS", 0)
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
        (
            lambda w: w.spend.append({"mode": "local", "finished": False, "confirmed": True}),
            "spend_unwritten",
        ),
        (
            lambda w: w.services.completions_logger.report.update(dropped=1),
            "request_log_lost",
        ),
        (
            lambda w: w.services.completions_logger.report.update(failed=1),
            "request_log_lost",
        ),
        (
            lambda w: setattr(streaming_response, "_ABANDONED_CLOSES", 1),
            "stream_close_abandoned",
        ),
        (
            lambda w: setattr(rejection_log, "_LOST_ROWS", 1),
            "request_log_lost",
        ),
        # A local cost whose write had an unknown outcome, or that the counters
        # refused; a shared reservation quarantined or unaccounted.
        (
            lambda w: w.spend.append(
                {"mode": "local", "finished": True, "confirmed": False, "unknown_requests": 1}
            ),
            "spend_unconfirmed",
        ),
        (
            lambda w: w.spend.append(
                {
                    "mode": "shared",
                    "finished": True,
                    "confirmed": False,
                    "unconfirmed_reservations": 1,
                }
            ),
            "spend_unconfirmed",
        ),
        # A ledger that does not say is read as unconfirmed.
        (lambda w: w.spend.append({"mode": "local", "finished": True}), "spend_unconfirmed"),
    ],
)
def test_anything_left_keeps_the_process_draining(world, change, reason):
    _drain(world)
    change(world)

    assert handoff.seal(world.services, by="test") == [reason]
    assert world.handoff.state is State.DRAINING


def test_a_standing_task_does_not_hold_a_drain(world):
    _drain(world)
    # The shared ledger's hourly upkeep runs for the life of the process.
    world.tracked["spend_upkeep"] = 1

    assert handoff.status(world.services)["tasks"]["tracked"] == {}
    assert handoff.seal(world.services, by="test") == []


def test_a_cost_not_confirmed_keeps_the_process_draining_however_long_it_waits(world):
    """It never resolves by waiting, and that is no reason to let the process go."""
    _drain(world)
    world.spend.append(
        {
            "mode": "local",
            "finished": True,
            "confirmed": False,
            "refused_requests": 3,
            "unconfirmed_requests": 3,
            "unconfirmed_usd": 0.75,
        }
    )

    for _ in range(3):
        assert handoff.seal(world.services, by="test") == ["spend_unconfirmed"]
    report = handoff.status(world.services)
    assert (report["state"], report["idle"], report["blocking"]) == (
        "draining",
        False,
        ["spend_unconfirmed"],
    )
    assert report["spend"][0]["unconfirmed_requests"] == 3
    assert report["spend"][0]["unconfirmed_usd"] == 0.75


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


async def test_a_close_that_is_abandoned_keeps_the_process_draining(world, monkeypatch):
    """Whatever the close still had to bill or log is not known to be done."""
    monkeypatch.setattr(streaming_response, "CLOSE_TIMEOUT_SECONDS", 0.02)

    async def body():
        try:
            yield b"one"
            yield b"two"
        finally:
            await asyncio.Event().wait()  # a close that hangs

    hang_up, receive, send, _ = _blocked_client()
    _drain(world)

    async def app(scope, receive, send):
        await ClosingStreamingResponse(body())(scope, receive, send)

    call = asyncio.create_task(HandoffMiddleware(app)({**SCOPE}, receive, send))
    await asyncio.sleep(0.01)
    hang_up.set()
    await call

    assert world.handoff.requests == 0
    assert handoff.seal(world.services, by="test") == ["stream_close_abandoned"]
    assert handoff.status(world.services)["streams"] == {"close_abandoned": 1}


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


# -- Responses: the events own the chat stream they re-frame ----------------------------


def _chat_frame(text: str) -> str:
    chunk = {
        "id": "chatcmpl-1",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "m",
        "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}],
    }
    return f"data: {json.dumps(chunk)}\n\n"


class _Session:
    """The chat stream's session: its close is where it bills and queues its row.

    Its close waits for *release*, as a real one waits for its upstream reader,
    so a test can look at the process while the close is under way.
    """

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.finalized = False
        self.stream = self._stream()

    async def _stream(self):
        try:
            for index in range(50):
                yield _chat_frame(f"t{index} ")
        finally:
            await self.release.wait()
            self.finalized = True


def _primed_chat_stream(session: _Session):
    """A real :class:`ChatStream` already primed, its rest being *session*'s stream."""
    from serving.servers.routers.completions import ChatStream

    class Primed(ChatStream):
        __slots__ = ()

        def __init__(self) -> None:
            self._sse = None
            self._head = None
            self._tail = session.stream
            self._frames_started = False
            self._identity_headers_for_response = lambda: {}

        async def prime(self) -> None:
            return None

    return Primed()


def _responses_request():
    from starlette.requests import Request

    body = json.dumps({"model": "m", "input": "hi", "stream": True, "store": False}).encode()

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/v1/responses",
        "headers": [(b"content-type", b"application/json")],
        "query_string": b"",
        "client": ("127.0.0.1", 1),
        "app": types.SimpleNamespace(state=types.SimpleNamespace()),
    }
    return Request(scope, receive)


async def _v1_responses(streams):
    from serving.servers.routers import responses

    return await responses.create_response(
        request=_responses_request(),
        http_response=Response(),
        authorization=None,
        user_ctx={"user_id": "u1", "authenticated": True},
        router_exec=None,
        log_store=None,
        model_router_registry=None,
        model_visibility_resolver=None,
        completions_logger=None,
        pricing_lookup=None,
        cost_tracker=None,
        response_store=None,
        _concurrency_slot=None,
        _streams=streams,
    )


async def _playground_responses(streams):
    from serving.servers.routers import playground

    return await playground.playground_responses(
        request=_responses_request(),
        caller={"user_id": "u1", "role": "user"},
        _slot=None,
        router_exec=None,
        log_store=None,
        model_router_registry=None,
        model_visibility_resolver=None,
        completions_logger=None,
        pricing_lookup=None,
        cost_tracker=None,
        response_store=None,
        _streams=streams,
    )


@pytest.mark.parametrize("route", [_v1_responses, _playground_responses])
async def test_a_responses_stream_left_by_its_client_finishes_its_chat_stream_first(
    world, monkeypatch, route
):
    """The client leaves mid-stream: no seal until the chat stream has billed and logged.

    The test holds the session and the chat stream itself, so the garbage
    collector cannot close them behind its back.
    """
    from serving.servers.routers import responses

    session = _Session()
    chat_stream = _primed_chat_stream(session)

    async def execute_chat(*_args, **_kwargs):
        return chat_stream

    monkeypatch.setattr(responses, "execute_chat", execute_chat)

    async def app(scope, receive, send):
        streams: list[Any] = []
        response = await route(streams)
        try:
            await response(scope, receive, send)
        finally:
            # What the route's release dependency does once the response is over.
            for held in streams:
                await held.close_if_unstarted()

    hang_up, receive, send, sent = _blocked_client()
    _drain(world)
    call = asyncio.create_task(
        HandoffMiddleware(app)({**SCOPE, "path": "/v1/responses"}, receive, send)
    )
    await asyncio.sleep(0.02)
    assert len(sent) > 2  # streaming, and the client has stopped reading
    hang_up.set()
    await asyncio.sleep(0.02)

    # The chat stream's close is under way: the request is not over.
    assert not session.finalized
    assert not call.done()
    assert handoff.seal(world.services, by="test") == ["requests_in_flight"]

    session.release.set()
    await call
    assert session.finalized
    assert handoff.seal(world.services, by="test") == []


async def test_responses_events_close_their_chat_stream_however_they_end():
    """Closed by their consumer mid-way, or run to the end: the chat stream ends with them."""
    from serving.servers.routers import responses

    async def events(session):
        return responses._response_events(
            session.stream,
            response_store=None,
            body={"model": "m"},
            model="m",
            response_id="resp_1",
            created_at=1,
            previous_response_id=None,
            store=False,
            convo_messages=[],
            user_id="u1",
        )

    closed = _Session()
    closed.release.set()
    stream = await events(closed)
    await stream.__anext__()
    await stream.aclose()
    assert closed.finalized

    finished = _Session()
    finished.release.set()
    stream = await events(finished)
    async for _ in stream:
        pass
    assert finished.finalized


# -- a close that hangs is bounded at every level -------------------------------------


async def _chat_route(chat_stream):
    return ClosingStreamingResponse(chat_stream.frames(), media_type="text/event-stream")


async def _responses_route(chat_stream):
    from serving.servers.routers import responses

    events = responses._response_events(
        chat_stream.frames(),
        response_store=None,
        body={"model": "m"},
        model="m",
        response_id="resp_1",
        created_at=1,
        previous_response_id=None,
        store=False,
        convo_messages=[],
        user_id="u1",
    )
    return ClosingStreamingResponse(events, media_type="text/event-stream")


@pytest.mark.parametrize("route", [_chat_route, _responses_route])
async def test_a_hung_upstream_close_inside_a_real_chat_stream_is_cut_at_the_deadline(
    world, monkeypatch, route
):
    """The chat stream's own shielded close must not keep the deadline from reaching it.

    The request ends (bounded), the abandoned close is counted, and the process
    is never sealed for it.
    """
    monkeypatch.setattr(streaming_response, "CLOSE_TIMEOUT_SECONDS", 0.02)
    session = _Session()  # its close waits for a release that does not come
    chat_stream = _primed_chat_stream(session)

    async def app(scope, receive, send):
        await (await route(chat_stream))(scope, receive, send)

    hang_up, receive, send, _ = _blocked_client()
    _drain(world)
    call = asyncio.create_task(HandoffMiddleware(app)({**SCOPE}, receive, send))
    try:
        await asyncio.sleep(0.02)
        hang_up.set()
        await asyncio.wait_for(asyncio.shield(call), 1.0)

        assert not session.finalized
        assert world.handoff.requests == 0
        assert streaming_response.abandoned_closes() >= 1
        assert handoff.seal(world.services, by="test") == ["stream_close_abandoned"]
        assert world.handoff.state is State.DRAINING
    finally:
        session.release.set()
        await asyncio.wait({call}, timeout=1)


async def test_a_close_within_the_deadline_is_not_counted(world):
    session = _Session()
    session.release.set()
    chat_stream = _primed_chat_stream(session)
    frames = chat_stream.frames()
    await frames.__anext__()

    assert await streaming_response.close_body(frames) is True
    assert session.finalized
    assert streaming_response.abandoned_closes() == 0


# -- a refused request's row, written apart from the queue -----------------------------


class _Settings:
    async def get_bool(self, name: str) -> bool:
        return True


class _Store:
    def __init__(self, fail: bool) -> None:
        self.fail = fail
        self.rows = 0

    async def log_request(self, **_row) -> None:
        if self.fail:
            raise RuntimeError("database unavailable")
        self.rows += 1


def _rejection(store):
    source = rejection_log.RejectionSource(
        route_path="/v1/chat/completions",
        client_ip=lambda: "203.0.113.7",
        probe_marker=False,
        log_store=store,
        runtime_settings=_Settings(),
    )
    return rejection_log.log_rejection_from(
        source=source,
        status_code=429,
        error_code="concurrency_limit_exceeded",
        reason="too many requests",
        user={"user_id": "u1", "role": "user", "authenticated": True},
    )


async def test_a_refused_request_s_row_that_failed_blocks_the_seal_after_its_task_ended(world):
    store = _Store(fail=True)
    _drain(world)

    await detached_tasks.rejection_log(_rejection(store))

    assert detached_tasks.pending() == {}  # the task is over; its loss is not
    assert handoff.seal(world.services, by="test") == ["request_log_lost"]
    assert handoff.status(world.services)["request_log"]["rejected_lost"] == 1


async def test_a_refused_request_s_row_that_was_written_lets_the_process_seal(world):
    store = _Store(fail=False)
    _drain(world)

    await detached_tasks.rejection_log(_rejection(store))

    assert store.rows == 1
    assert handoff.seal(world.services, by="test") == []


async def test_a_refused_request_s_row_whose_task_failed_before_writing_is_lost(world):
    async def broken() -> None:
        raise RuntimeError("before the write")

    _drain(world)
    task = detached_tasks.rejection_log(broken())
    with pytest.raises(RuntimeError):
        await task

    assert handoff.seal(world.services, by="test") == ["request_log_lost"]


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
