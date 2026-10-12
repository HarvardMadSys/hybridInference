"""First-run setup state.

A fresh database-backed deployment has no accounts, and its first account must
be an administrator created by whoever operates the deployment, not by whoever
reaches the signup page first. While setup is pending:

- there is one setup code, stored in ``site_settings`` (``setup_code``) by the
  first process to boot, so every worker accepts it and a restart keeps it; it
  is deleted when setup completes. Each process logs it at boot, at WARNING
  whatever ``LOG_LEVEL`` says, and keeps only its hash in memory;
- ``POST /auth/signup`` answers 503;
- ``/site-config`` reports ``setup.required`` so the console sends every route
  to ``/setup``, where ``POST /auth/setup/admin`` exchanges the code for an
  administrator account.

Setup is complete once the ``site_settings`` row ``setup_completed_at`` exists.
A database that already has users but no marker (an upgrade from a release
without first-run setup, or an account inserted by an operator script) gets the
marker on sight, so it never shows the setup page. Database-free mode has no
setup at all.

The code is stored in plaintext: anyone who can read ``site_settings`` can
already read the plaintext secrets kept beside it.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from serving.config.settings import get_settings
from serving.utils.logging import get_logger

logger = get_logger(__name__)

SETUP_MARKER_KEY = "setup_completed_at"
SETUP_CODE_KEY = "setup_code"

# Read off a log and typed by hand, so no 0/O or 1/I.
SETUP_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
SETUP_CODE_LENGTH = 12
_SETUP_CODE_GROUP = 4

# While setup is pending, re-read the database at most this often, and give up
# on a read that takes longer than the timeout: ``/site-config`` waits on it,
# and it renders every console page.
_REFRESH_INTERVAL_SECONDS = 2.0
_REFRESH_TIMEOUT_SECONDS = 1.0


@dataclass
class _SetupState:
    """What this process knows about first-run setup."""

    store: Any = None
    required: bool = False
    code_digest: bytes | None = None
    checked_at: float = 0.0


_state = _SetupState()


def generate_setup_code() -> str:
    """Return a new random setup code, without separators."""
    return "".join(secrets.choice(SETUP_CODE_ALPHABET) for _ in range(SETUP_CODE_LENGTH))


def format_setup_code(code: str) -> str:
    """Group a setup code for display: ``ABCDEFGHJKLM`` becomes ``ABCD-EFGH-JKLM``."""
    return "-".join(
        code[start : start + _SETUP_CODE_GROUP] for start in range(0, len(code), _SETUP_CODE_GROUP)
    )


def normalize_setup_code(code: str) -> str:
    """Return a typed code in canonical form: uppercase, without spaces or dashes."""
    return "".join(ch for ch in code.upper() if ch != "-" and not ch.isspace())


def _digest(code: str) -> bytes:
    """Hash a code in canonical form; only the hash is kept in memory."""
    return hashlib.sha256(normalize_setup_code(code).encode()).digest()


def _now_iso() -> str:
    """Return the current time as an ISO 8601 timestamp, the marker's value."""
    return datetime.now(timezone.utc).isoformat()


async def _load_code(store: Any) -> str | None:
    """Return the shared setup code while setup is pending, None once it is complete."""
    return await store.get_or_create_setup_code(
        marker_key=SETUP_MARKER_KEY,
        code_key=SETUP_CODE_KEY,
        completed_at=_now_iso(),
        candidate_code=generate_setup_code(),
    )


def _announce(code: str) -> None:
    """Tell the operator the setup code, whatever ``LOG_LEVEL`` is set to.

    Setup cannot be completed without this line, so the record goes straight
    to the handlers: a level above WARNING would otherwise drop it. It is
    written once per process per code, so this cannot become noise.
    """
    console = get_settings().frontend_url.rstrip("/")
    record = logger.makeRecord(
        logger.name,
        logging.WARNING,
        __file__,
        0,
        "First-run setup is pending. Open %s/setup and enter setup code %s",
        (console, format_setup_code(code)),
        None,
    )
    logger.handle(record)


def _adopt_code(code: str) -> None:
    """Accept *code* from now on, announcing it when this process did not know it."""
    digest = _digest(code)
    if digest == _state.code_digest:
        return
    _state.code_digest = digest
    _announce(code)


async def init_setup_state(store: Any) -> None:
    """Load the setup marker, or the shared setup code while setup is pending.

    Called by bootstrap once the operational store exists, and with None in
    database-free mode, where there is nothing to set up. State that cannot be
    read counts as pending, without a code: opening signup to whoever arrives
    first is the failure this module exists to prevent, and
    :func:`refresh_setup_state` reads it again on the next status check.

    Args:
        store: The operational store, or None without a database.
    """
    _state.store = store
    _state.code_digest = None
    _state.checked_at = time.monotonic()
    if store is None:
        _state.required = False
        return

    try:
        code = await _load_code(store)
    except Exception:
        logger.exception(
            "Could not read the first-run setup state; treating setup as pending "
            "until the database answers"
        )
        _state.required = True
        return
    _state.required = code is not None
    if code is not None:
        _adopt_code(code)


def is_setup_required() -> bool:
    """Return whether first-run setup is still pending, from memory.

    False in database-free mode and before :func:`init_setup_state` has run.
    """
    return _state.required


async def refresh_setup_state() -> bool:
    """Re-check a pending setup against the database, then return whether it is pending.

    Costs nothing once setup is complete. While pending, the database is read
    again at most every couple of seconds, so a process notices a setup
    completed by another process, or an account an operator script inserted,
    without a restart, and picks up the stored code if it has none. A failed
    or slow read leaves setup pending.

    Returns:
        The same answer :func:`is_setup_required` gives afterwards.
    """
    if not _state.required or _state.store is None:
        return _state.required
    now = time.monotonic()
    if now - _state.checked_at < _REFRESH_INTERVAL_SECONDS:
        return True
    _state.checked_at = now
    try:
        code = await asyncio.wait_for(_load_code(_state.store), timeout=_REFRESH_TIMEOUT_SECONDS)
    except Exception as exc:
        logger.warning("Could not re-check the first-run setup state: %r", exc)
        return True
    if code is None:
        mark_setup_completed()
    else:
        _adopt_code(code)
    return _state.required


async def complete_setup(
    store: Any,
    *,
    user_id: str,
    login_name: str,
    password_hash: str,
    user_name: str,
    admin_ip: str,
) -> bool:
    """Create the first administrator and record that setup is complete.

    The store re-checks, under a database-wide lock, that setup is still
    pending, so this is safe against a concurrent request in any process, and
    deletes the stored setup code in the same transaction. Unless it raises,
    setup is no longer pending afterwards: this call completed it, or the
    store found it already completed.

    Returns:
        True when the administrator was created; False when setup had
        already been completed and nothing was written.
    """
    created = await store.create_first_admin(
        user_id=user_id,
        login_name=login_name,
        password_hash=password_hash,
        user_name=user_name,
        marker_key=SETUP_MARKER_KEY,
        marker_value=_now_iso(),
        code_key=SETUP_CODE_KEY,
        admin_ip=admin_ip,
    )
    mark_setup_completed()
    return created


def verify_setup_code(code: str) -> bool:
    """Return whether *code* is the deployment's setup code.

    The code is normalized (case, spaces and dashes do not matter) and its
    hash compared in constant time. Always False once setup is complete, and
    while this process has not yet read the code from the database.
    """
    expected = _state.code_digest
    if expected is None:
        return False
    return hmac.compare_digest(_digest(code), expected)


def mark_setup_completed() -> None:
    """Record in memory that setup is complete and forget the setup code."""
    if _state.required:
        logger.info("First-run setup is complete.")
    _state.required = False
    _state.code_digest = None


def reset_setup_state() -> None:
    """Forget everything this module knows (test helper)."""
    global _state
    _state = _SetupState()
