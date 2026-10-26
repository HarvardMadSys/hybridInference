"""Worker identity for the batch loops that claim work from the shared store."""

from __future__ import annotations

import os
import secrets
import socket


def default_worker_id() -> str:
    """Return an id naming this process in claims; unique per worker instance."""
    return f"{socket.gethostname()}:{os.getpid()}:{secrets.token_hex(3)}"
