"""Linux process hardening for the triage relay."""

from __future__ import annotations

import ctypes
import os
import sys

_PR_SET_DUMPABLE = 4


def protect_process_secrets() -> None:
    """Block same-UID child processes from inspecting relay memory or ``/proc`` data."""
    if sys.platform != "linux":
        return
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(_PR_SET_DUMPABLE, 0, 0, 0, 0) != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number))
