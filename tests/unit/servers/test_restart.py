"""Restarting the backend from the admin console, without ending the test process."""

from __future__ import annotations

import os
import signal
import threading
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI

from serving.servers import app as app_module, restart


@pytest.fixture(autouse=True)
def _one_worker(monkeypatch):
    for name in ("WEB_CONCURRENCY", "UVICORN_WORKERS", "GUNICORN_WORKERS"):
        monkeypatch.delenv(name, raising=False)


def test_supported_under_systemd(monkeypatch) -> None:
    monkeypatch.setattr(restart.Path, "exists", lambda self: False)
    monkeypatch.delenv("INVOCATION_ID", raising=False)
    assert restart.restart_supported() is False

    monkeypatch.setenv("INVOCATION_ID", "0123abcd")
    assert restart.restart_supported() is True


def test_supported_in_a_docker_container(monkeypatch) -> None:
    monkeypatch.delenv("INVOCATION_ID", raising=False)
    monkeypatch.setattr(restart.Path, "exists", lambda self: str(self) == "/.dockerenv")
    assert restart.restart_supported() is True


def test_request_restart_signals_this_process_once() -> None:
    kills: list[tuple[int, int]] = []
    exits: list[int] = []

    restart.request_restart(
        kill=lambda pid, sig: kills.append((pid, sig)),
        exit_process=exits.append,
        watchdog_seconds=3600,
    )
    restart.request_restart(
        kill=lambda pid, sig: kills.append((pid, sig)),
        exit_process=exits.append,
        watchdog_seconds=3600,
    )

    assert kills == [(os.getpid(), signal.SIGTERM)]
    assert restart.restart_requested()
    assert exits == []


def test_the_watchdog_forces_the_exit_when_shutdown_hangs() -> None:
    exited = threading.Event()
    statuses: list[int] = []

    def exit_process(status: int) -> None:
        statuses.append(status)
        exited.set()

    restart.request_restart(
        kill=lambda pid, sig: None, exit_process=exit_process, watchdog_seconds=0
    )

    assert exited.wait(timeout=5)
    assert statuses == [restart.RESTART_EXIT_STATUS] == [75]


def test_exit_for_restart_flushes_logging_then_exits_with_the_restart_status(
    monkeypatch,
) -> None:
    calls: list[str] = []
    # Not the real logging.shutdown: it would close this test process's handlers.
    monkeypatch.setattr(restart.logging, "shutdown", lambda: calls.append("flush"))

    restart.exit_for_restart(exit_process=lambda status: calls.append(f"exit {status}"))

    assert calls == ["flush", "exit 75"]


@pytest.mark.asyncio
@pytest.mark.parametrize("requested", [False, True])
async def test_lifespan_exits_for_a_requested_restart_after_shutdown(
    monkeypatch, requested
) -> None:
    order: list[str] = []
    services = object()

    async def shutdown(received) -> None:
        assert received is services
        order.append("shutdown")

    monkeypatch.setattr(app_module.bootstrap, "initialize", AsyncMock(return_value=services))
    monkeypatch.setattr(app_module.bootstrap, "shutdown", shutdown)
    monkeypatch.setattr(restart, "exit_for_restart", lambda: order.append("exit"))
    if requested:
        restart.request_restart(kill=lambda pid, sig: None, watchdog_seconds=3600)

    async with app_module.lifespan(FastAPI()):
        pass

    assert order == (["shutdown", "exit"] if requested else ["shutdown"])


@pytest.mark.parametrize(
    "variable,count,supported",
    [
        ("WEB_CONCURRENCY", "4", False),
        ("UVICORN_WORKERS", "2", False),
        ("GUNICORN_WORKERS", "3", False),
        ("WEB_CONCURRENCY", "1", True),
        ("WEB_CONCURRENCY", "not-a-number", True),
    ],
)
def test_several_workers_mean_a_manual_restart(monkeypatch, variable, count, supported) -> None:
    """The signal reaches only the worker that served the request."""
    monkeypatch.setenv("INVOCATION_ID", "0123abcd")
    monkeypatch.setenv(variable, count)

    assert restart.restart_supported() is supported
