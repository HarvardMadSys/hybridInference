"""Restarting the backend from the admin console, without ending the test process."""

from __future__ import annotations

import multiprocessing
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
    monkeypatch.delenv(restart.SUPERVISED_RESTART_ENV, raising=False)


@pytest.mark.parametrize(
    "declared,supported",
    [(None, False), ("", False), ("0", False), ("false", False), ("1", True), ("true", True)],
)
def test_supported_only_where_the_deployment_declares_a_supervisor(
    monkeypatch, declared, supported
) -> None:
    """Being in a container or a unit says nothing about its restart policy.

    ``docker run`` without ``--restart`` still has ``/.dockerenv``, and systemd
    sets ``INVOCATION_ID`` under ``Restart=no`` too: exiting there would leave
    the gateway down.
    """
    monkeypatch.setenv("INVOCATION_ID", "0123abcd")
    if declared is not None:
        monkeypatch.setenv(restart.SUPERVISED_RESTART_ENV, declared)

    assert restart.restart_supported() is supported


def test_the_bundled_deployments_declare_their_supervisor() -> None:
    """Each declaration sits beside the restart policy that makes it true."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[3]
    compose = (root / "deploy/docker/docker-compose.yml").read_text()
    backend = compose[compose.index("  backend:") : compose.index("\n  frontend:")]
    assert "restart: unless-stopped" in backend
    assert 'SUPERVISOR_RESTARTS_BACKEND: "1"' in backend
    for unit in ("hybrid_inference.service", "hybrid_inference.staging.service"):
        text = (root / "deploy/systemd" / unit).read_text()
        assert "Restart=on-failure" in text, unit
        assert "Environment=SUPERVISOR_RESTARTS_BACKEND=1" in text, unit


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
    monkeypatch.setenv(restart.SUPERVISED_RESTART_ENV, "1")
    monkeypatch.setenv(variable, count)

    assert restart.restart_supported() is supported


def test_a_worker_a_process_manager_started_means_a_manual_restart(monkeypatch) -> None:
    """``uvicorn --workers 2`` sets no variable; its workers are spawned children."""
    monkeypatch.setenv(restart.SUPERVISED_RESTART_ENV, "1")
    monkeypatch.setattr(restart, "spawned_by_worker_manager", lambda: True)

    assert restart.restart_supported() is False


def _report_spawned(queue) -> None:
    from serving.utils.workers import spawned_by_worker_manager

    queue.put(spawned_by_worker_manager())


def test_a_spawned_worker_knows_it() -> None:
    """The way uvicorn starts each of several workers: a ``spawn`` child process."""
    from serving.utils.workers import spawned_by_worker_manager

    context = multiprocessing.get_context("spawn")
    queue = context.Queue()
    child = context.Process(target=_report_spawned, args=(queue,))
    child.start()
    try:
        assert queue.get(timeout=60) is True
    finally:
        child.join(timeout=60)
    assert spawned_by_worker_manager() is False
