"""The deployment's spend accounting mode, as this process read it at startup.

``local`` (the default, and what a database the migrations just brought to
``0020_shared_spend`` holds) is the per-process accounting of
:mod:`serving.spend_ledger`, unchanged. ``shared`` is the accounting of
:mod:`serving.shared_spend.ledger`: reservations and settlements in
PostgreSQL, the same for every process.

A process reads the mode once, at startup (:func:`load`), and keeps it until it
exits, so its admissions and its settlements always follow one protocol. A
change of the stored mode takes effect as each process restarts; ``python -m
serving.shared_spend status`` shows which processes run which.
"""

from __future__ import annotations

from typing import Any, Literal

Mode = Literal["local", "shared"]

#: Version of the shared protocol, reported in ``config_instances`` capabilities.
PROTOCOL_VERSION = 1

#: Where a process reports its mode among its capabilities.
CAPABILITY = "spend_accounting"

SELECT_MODE_SQL = "SELECT mode FROM spend_accounting WHERE id"

_mode: Mode = "local"


def current() -> Mode:
    """The mode this process runs in."""
    return _mode


def is_shared() -> bool:
    """Whether this process runs the shared accounting."""
    return _mode == "shared"


def set_mode(mode: Mode) -> None:
    """Set the mode this process runs in; startup's (and tests') to call."""
    global _mode
    if mode not in ("local", "shared"):
        raise ValueError(f"unknown spend accounting mode {mode!r}")
    _mode = mode


async def load(pool: Any) -> Mode:
    """Read the stored mode and run in it from now on.

    Raises whatever the read raises: the caller (startup) retries as it
    retries the rest of its database setup. A database without the row (it
    cannot be at this code's schema) runs ``local``.
    """
    async with pool.acquire() as conn:
        stored = await conn.fetchval(SELECT_MODE_SQL)
    set_mode("shared" if stored == "shared" else "local")
    return _mode


def capability() -> dict[str, Any]:
    """What this process reports about its accounting in ``config_instances``."""
    return {"protocol": PROTOCOL_VERSION, "mode": _mode}
