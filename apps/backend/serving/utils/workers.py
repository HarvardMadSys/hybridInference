"""How many worker processes serve this deployment.

State kept in process memory — RouteWise's quota accounting, the target of a
self-restart — is only ever one worker's. Whatever must know whether that is the
whole deployment asks here.
"""

from __future__ import annotations

import multiprocessing
import os

#: The variables ASGI servers and their launchers read the worker count from.
WORKER_COUNT_ENV_KEYS = (
    "WEB_CONCURRENCY",
    "UVICORN_WORKERS",
    "GUNICORN_WORKERS",
)


def configured_worker_count() -> int | None:
    """Best-effort detection for common ASGI worker-count environment vars.

    Returns:
        The first positive count found, or ``None`` when none is configured.
    """
    for key in WORKER_COUNT_ENV_KEYS:
        raw = os.getenv(key)
        if raw is None:
            continue
        try:
            count = int(raw)
        except ValueError:
            continue
        if count > 0:
            return count
    return None


def spawned_by_worker_manager() -> bool:
    """Return whether a process manager started this process as its worker.

    ``uvicorn --workers N`` (N above 1) and ``uvicorn --reload`` run the
    application in child processes they start with :mod:`multiprocessing`,
    and a count given on the command line never reaches
    :func:`configured_worker_count`. Such a process is one of several workers,
    or one its reloader starts again only when a file changes.
    """
    return multiprocessing.parent_process() is not None
