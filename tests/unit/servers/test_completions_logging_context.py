"""Request-context attribution of the request-log worker pool.

The pool's workers are long-lived, and the first ``schedule_log`` starts them
from inside whichever request completed first. A worker that copied that
request's context stamped its request id, model and provider -- through
``JsonFormatter`` -- on the log lines of every later row it wrote. Each write
must run in the context of the request whose row it is, as the per-request
task it replaced did.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import json
import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from serving.servers.routers.completions_logging import CompletionsLogger
from serving.utils import context as req_ctx
from serving.utils.logging import JsonFormatter

_LOGGER = "serving.servers.routers.completions_logging"
_TRACKED = "serving.observability.tracked_tasks"
_REQUEST_FIELDS = {"request_id", "model", "provider"}


class _JsonCapture(logging.Handler):
    """Format each record when it is emitted, in the emitting task's context.

    That is when a real ``StreamHandler`` runs ``JsonFormatter``; ``caplog``
    formats later, in the test's own context, and would hide the bug.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.setFormatter(JsonFormatter())
        self.lines: list[dict[str, Any]] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(json.loads(self.format(record)))


@pytest.fixture
def json_lines():
    handler = _JsonCapture()
    loggers = [logging.getLogger(name) for name in (_LOGGER, _TRACKED)]
    levels = [lg.level for lg in loggers]
    for lg in loggers:
        lg.addHandler(handler)
        lg.setLevel(logging.DEBUG)
    yield handler.lines
    for lg, level in zip(loggers, levels, strict=True):
        lg.removeHandler(handler)
        lg.setLevel(level)


def _failing_store() -> MagicMock:
    store = MagicMock()
    store.log_request = AsyncMock(side_effect=RuntimeError("db down"))
    return store


def _ok_store() -> MagicMock:
    store = MagicMock()
    store.log_request = AsyncMock(return_value=None)
    return store


async def _wait_for_writes(store: MagicMock, count: int) -> None:
    for _ in range(200):
        if store.log_request.await_count >= count:
            return
        await asyncio.sleep(0)
    raise AssertionError(f"expected {count} writes, saw {store.log_request.await_count}")


def _schedule_as(cl: CompletionsLogger, request_id: str) -> None:
    """Schedule a row from inside a request scope named after it."""
    with req_ctx.push(
        request_id=request_id, model=f"model-{request_id}", provider=f"provider-{request_id}"
    ):
        cl.schedule_log(request_id, {"request_id": request_id})


def _row_line(lines: list[dict[str, Any]], request_id: str) -> dict[str, Any]:
    """The logger's own line about one row's write (completed or failed)."""
    (line,) = [
        line
        for line in lines
        if line["name"] == _LOGGER and f"for request {request_id}" in line["message"]
    ]
    return line


def _assert_attributed_to(line: dict[str, Any], request_id: str) -> None:
    assert (line.get("request_id"), line.get("model"), line.get("provider")) == (
        request_id,
        f"model-{request_id}",
        f"provider-{request_id}",
    )


def _tracked_events(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [line for line in lines if line["message"] == "tracked_task_completed"]


async def test_each_write_is_logged_under_its_own_request(json_lines):
    store = _failing_store()
    cl = CompletionsLogger(log_store=store)

    _schedule_as(cl, "req-A")  # starts the pool from inside request A
    await _wait_for_writes(store, 1)
    _schedule_as(cl, "req-B")
    await cl.stop()

    _assert_attributed_to(_row_line(json_lines, "req-A"), "req-A")
    _assert_attributed_to(_row_line(json_lines, "req-B"), "req-B")
    # The failure events the alert engine consumes are attributed per row too.
    events = _tracked_events(json_lines)
    assert sorted(event.get("request_id") for event in events) == ["req-A", "req-B"]
    for event in events:
        _assert_attributed_to(event, event["request_id"])


async def test_a_row_scheduled_outside_a_request_carries_no_request_fields(json_lines):
    store = _ok_store()
    cl = CompletionsLogger(log_store=store)

    _schedule_as(cl, "req-A")
    await _wait_for_writes(store, 1)
    # A background producer with no request scope of its own.
    contextvars.Context().run(cl.schedule_log, "bg-1", {"request_id": "bg-1"})
    await cl.stop()

    _assert_attributed_to(_row_line(json_lines, "req-A"), "req-A")
    assert not _REQUEST_FIELDS & _row_line(json_lines, "bg-1").keys()
    (bg_event,) = [e for e in _tracked_events(json_lines) if e.get("request_id") != "req-A"]
    assert not _REQUEST_FIELDS & bg_event.keys()


@pytest.mark.skipif(
    not hasattr(asyncio.Task, "get_context"), reason="Task.get_context needs Python 3.12"
)
async def test_workers_do_not_keep_the_starting_request_context():
    store = _ok_store()
    cl = CompletionsLogger(log_store=store)

    _schedule_as(cl, "req-A")
    await _wait_for_writes(store, 1)

    assert cl._workers
    for worker in cl._workers:
        assert worker.get_context().run(req_ctx.get) == {}
    await cl.stop()


async def test_replacement_workers_start_clean_and_keep_per_row_attribution(json_lines):
    store = _failing_store()
    cl = CompletionsLogger(log_store=store)

    _schedule_as(cl, "req-A")
    await _wait_for_writes(store, 1)
    assert cl._workers
    for worker in cl._workers:
        worker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await worker

    _schedule_as(cl, "req-C")  # replaces the dead pool from inside request C
    await _wait_for_writes(store, 2)
    if hasattr(asyncio.Task, "get_context"):
        for worker in cl._workers:
            assert worker.get_context().run(req_ctx.get) == {}
    _schedule_as(cl, "req-D")
    await cl.stop()

    _assert_attributed_to(_row_line(json_lines, "req-C"), "req-C")
    _assert_attributed_to(_row_line(json_lines, "req-D"), "req-D")


async def test_a_write_cannot_change_the_producer_or_the_next_row_context():
    seen: list[str | None] = []

    async def _write(**kwargs: object) -> None:
        seen.append(req_ctx.get().get("provider"))
        req_ctx.update({"provider": "changed-by-write"})

    store = MagicMock()
    store.log_request = AsyncMock(side_effect=_write)
    cl = CompletionsLogger(log_store=store)

    with req_ctx.push(request_id="req-A", model="model-A", provider="provider-A"):
        cl.schedule_log("req-A", {"request_id": "req-A"})
        await _wait_for_writes(store, 1)
        cl.schedule_log("req-A2", {"request_id": "req-A2"})
        await cl.stop()
        assert req_ctx.get()["provider"] == "provider-A"

    assert seen == ["provider-A", "provider-A"]
