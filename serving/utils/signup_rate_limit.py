"""IP-based sliding-window rate limiter for the signup endpoint.

Backed by a small SQLite database so limits survive process restarts. Keeps a
per-IP log of attempt timestamps and counts attempts inside the 1h and 24h
windows on each call.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import time
from pathlib import Path

from serving.config.settings import settings

_DEFAULT_DB_PATH = "data/db/signup_rate_limits.db"
_HOUR_SECONDS = 3600
_DAY_SECONDS = 86400


def _now() -> float:
    return time.time()


def _db_path() -> str:
    return os.getenv("SIGNUP_RATE_LIMIT_DB", _DEFAULT_DB_PATH)


def _ensure_schema(path: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS signup_attempts (
                ip TEXT NOT NULL,
                ts REAL NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_signup_attempts_ip ON signup_attempts(ip)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_signup_attempts_ts ON signup_attempts(ts)")


def _check_and_record_sync(ip: str, now: float) -> tuple[bool, str | None]:
    path = _db_path()
    _ensure_schema(path)
    cutoff = now - _DAY_SECONDS
    per_hour = settings.signup_rate_limit_per_hour
    per_day = settings.signup_rate_limit_per_day

    with sqlite3.connect(path) as conn:
        conn.execute("DELETE FROM signup_attempts WHERE ts < ?", (cutoff,))
        cur = conn.execute(
            "SELECT ts FROM signup_attempts WHERE ip = ? AND ts >= ?",
            (ip, cutoff),
        )
        timestamps = [row[0] for row in cur.fetchall()]

        hour_cutoff = now - _HOUR_SECONDS
        hour_count = sum(1 for t in timestamps if t >= hour_cutoff)
        day_count = len(timestamps)

        conn.execute(
            "INSERT INTO signup_attempts (ip, ts) VALUES (?, ?)",
            (ip, now),
        )
        conn.commit()

    if hour_count >= per_hour:
        return False, "hour"
    if day_count >= per_day:
        return False, "day"
    return True, None


async def check_and_record_signup(ip: str) -> tuple[bool, str | None]:
    return await asyncio.to_thread(_check_and_record_sync, ip, _now())


def reset_rate_limit_db() -> None:
    """Wipe all recorded attempts. Test-only helper."""
    path = _db_path()
    _ensure_schema(path)
    with sqlite3.connect(path) as conn:
        conn.execute("DELETE FROM signup_attempts")
        conn.commit()
