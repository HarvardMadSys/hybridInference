"""Restarting the backend from the admin console.

Settings captured at startup apply only after a restart, so the console can ask
the process to exit and let its supervisor start it again: Docker with
``restart: unless-stopped``, or systemd with ``Restart=on-failure``. The
deployment declares that supervisor with :data:`SUPERVISED_RESTART_ENV`. The
process signals itself ``SIGTERM`` so uvicorn shuts down gracefully, and the
lifespan then exits with :data:`RESTART_EXIT_STATUS` — non-zero, because
``Restart=on-failure`` ignores a clean exit. A watchdog forces that exit if the
graceful shutdown hangs, say on a stream that never ends.

The restart request answers with :data:`INSTANCE_ID`, and ``/health`` reports
it too, so the console knows the restart is over once a different process
answers. Waiting to see the backend down first misses a restart quicker than
the console's polling interval.
"""

from __future__ import annotations

import logging
import os
import secrets
import signal
import threading
import time
from typing import TYPE_CHECKING

from serving.utils.logging import get_logger
from serving.utils.workers import configured_worker_count, spawned_by_worker_manager

if TYPE_CHECKING:
    from collections.abc import Callable

logger = get_logger(__name__)

#: ``EX_TEMPFAIL``: the process stopped on purpose and should be started again.
RESTART_EXIT_STATUS = 75

#: Longest the graceful shutdown may take before the process is ended anyway.
WATCHDOG_SECONDS = 30.0

#: Set to ``1`` by a deployment whose supervisor starts the backend again when
#: it exits. The bundled Compose file and systemd units set it beside their
#: restart policy; it stays in the environment, since it describes the process.
SUPERVISED_RESTART_ENV = "SUPERVISOR_RESTARTS_BACKEND"

_TRUE_WORDS = frozenset({"1", "true", "yes", "on"})

#: Names this process, and changes with every start. ``/health`` reports it, so
#: the console can tell the process that came back from the one it asked to
#: exit however short the outage between them, which polling cannot see.
INSTANCE_ID = secrets.token_hex(8)

_requested = threading.Event()


def restart_supported() -> bool:
    """Return whether a supervisor will start this process again after it exits.

    Only the deployment knows that, so it has to say so with
    :data:`SUPERVISED_RESTART_ENV`: from inside, a container started without a
    restart policy, or a systemd unit left at ``Restart=no``, looks exactly like
    one that restarts, and exiting under either leaves the gateway down. False
    as well when several worker processes serve the deployment, whether the
    count comes from the environment or from ``uvicorn --workers``: the signal
    reaches only the worker that took the request, its manager starts that one
    again, and the others keep the old settings, so the console offers the
    manual restart instead.
    """
    if os.environ.get(SUPERVISED_RESTART_ENV, "").strip().lower() not in _TRUE_WORDS:
        return False
    workers = configured_worker_count()
    if workers is not None and workers > 1:
        return False
    return not spawned_by_worker_manager()


def restart_requested() -> bool:
    """Return whether :func:`request_restart` has run in this process."""
    return _requested.is_set()


def _watchdog(seconds: float, exit_process: Callable[[int], object]) -> None:
    time.sleep(seconds)
    logger.error("Graceful shutdown took over %.0fs; exiting for the restart now", seconds)
    exit_process(RESTART_EXIT_STATUS)


def request_restart(
    *,
    kill: Callable[[int, int], object] = os.kill,
    exit_process: Callable[[int], object] = os._exit,
    watchdog_seconds: float = WATCHDOG_SECONDS,
) -> None:
    """Begin a graceful shutdown that ends in a restart.

    Args:
        kill: Sends the signal; replaceable so tests need not stop themselves.
        exit_process: Ends the process from the watchdog.
        watchdog_seconds: How long the graceful shutdown may take.
    """
    if _requested.is_set():
        return
    _requested.set()
    logger.warning("Restart requested by an administrator; shutting down")
    threading.Thread(
        target=_watchdog,
        args=(watchdog_seconds, exit_process),
        name="restart-watchdog",
        daemon=True,
    ).start()
    kill(os.getpid(), signal.SIGTERM)


def exit_for_restart(*, exit_process: Callable[[int], object] = os._exit) -> None:
    """End the process with :data:`RESTART_EXIT_STATUS` once shutdown has finished.

    Called at the very end of the application lifespan. Flushes logging first,
    because ``os._exit`` skips the interpreter's own cleanup.
    """
    logging.shutdown()
    exit_process(RESTART_EXIT_STATUS)


def reset() -> None:
    """Forget a requested restart (tests)."""
    _requested.clear()
