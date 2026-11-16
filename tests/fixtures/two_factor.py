"""An in-memory stand-in for the second-factor rows of the operational store.

Keeps what ``serving.storage.two_factor`` keeps -- an account's pending and
confirmed secrets, the last time step it accepted, its recovery codes, and what
each session has proved -- with the same answers, so a route test exercises the
real policy against rows it can read back. The SQL itself is covered by the
``dbtest`` tier (``tests/integration/test_two_factor_postgres.py``).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

STORE_METHODS = (
    "get_two_factor",
    "set_pending_two_factor",
    "begin_two_factor_setup_attempt",
    "cancel_pending_two_factor",
    "confirm_two_factor",
    "begin_two_factor_attempt",
    "verify_session_two_factor",
    "replace_recovery_codes",
    "delete_two_factor",
    "get_session_two_factor",
)


class TwoFactorFake:
    """Second-factor rows held in dictionaries."""

    def __init__(self) -> None:
        # user_id -> the user_two_factor row.
        self.factors: dict[str, dict[str, Any]] = {}
        # user_id -> {code_hash: used}
        self.recovery: dict[str, dict[str, bool]] = {}
        # (user_id, sid) -> {verified_at, environment}; a key here is a live session.
        self.sessions: dict[tuple[str, str], dict[str, Any]] = {}

    def open_session(self, user_id: str, sid: str) -> None:
        """Make *sid* a live session of *user_id* that has proved nothing."""
        self.sessions[(user_id, sid)] = {"verified_at": None, "environment": None}

    def _confirmed(self, user_id: str) -> bool:
        row = self.factors.get(user_id)
        return bool(row and row["confirmed_at"] is not None)

    def _pending_live(self, row: dict[str, Any]) -> bool:
        return (
            row["pending_secret_encrypted"] is not None
            and row["pending_expires_at"] > datetime.now(UTC)
            and row["pending_attempts_left"] > 0
        )

    async def get_two_factor(self, user_id: str) -> dict[str, Any] | None:
        row = self.factors.get(user_id)
        if row is None:
            return None
        left = sum(1 for used in self.recovery.get(user_id, {}).values() if not used)
        public = {key: value for key, value in row.items() if not key.startswith("pending_")}
        return {
            **public,
            "pending_session_id": row["pending_session_id"] if self._pending_live(row) else None,
            "recovery_codes_left": left,
        }

    async def set_pending_two_factor(
        self,
        user_id: str,
        pending_secret_encrypted: str,
        *,
        sid: str,
        ttl_seconds: int,
        attempts: int,
    ) -> None:
        row = self.factors.setdefault(
            user_id,
            {
                "secret_encrypted": None,
                "confirmed_at": None,
                "last_used_step": None,
                "failed_attempts": 0,
                "locked_until": None,
            },
        )
        row.update(
            pending_secret_encrypted=pending_secret_encrypted,
            pending_session_id=sid,
            pending_expires_at=datetime.now(UTC) + timedelta(seconds=ttl_seconds),
            pending_attempts_left=attempts,
        )

    async def begin_two_factor_setup_attempt(self, user_id: str, sid: str) -> str | None:
        row = self.factors.get(user_id)
        if row is None or not self._pending_live(row) or row["pending_session_id"] != sid:
            return None
        row["pending_attempts_left"] -= 1
        return row["pending_secret_encrypted"]

    async def cancel_pending_two_factor(self, user_id: str, sid: str) -> bool:
        row = self.factors.get(user_id)
        if (
            row is None
            or row["pending_secret_encrypted"] is None
            or row["pending_session_id"] != sid
        ):
            return False
        self._clear_pending(row)
        return True

    @staticmethod
    def _clear_pending(row: dict[str, Any]) -> None:
        row.update(
            pending_secret_encrypted=None,
            pending_session_id=None,
            pending_expires_at=None,
            pending_attempts_left=0,
        )

    async def confirm_two_factor(
        self,
        user_id: str,
        *,
        pending_secret_encrypted: str,
        step: int,
        recovery_code_hashes: Any,
        sid: str,
        environment: str,
    ) -> bool:
        row = self.factors.get(user_id)
        if row is None or row["pending_secret_encrypted"] != pending_secret_encrypted:
            return False
        row.update(
            secret_encrypted=pending_secret_encrypted,
            confirmed_at=datetime.now(UTC),
            last_used_step=step,
            failed_attempts=0,
            locked_until=None,
        )
        self._clear_pending(row)
        self.recovery[user_id] = dict.fromkeys(recovery_code_hashes, False)
        self._forget_sessions(user_id)
        self._mark(user_id, sid, environment)
        return True

    async def begin_two_factor_attempt(
        self, user_id: str, *, lock_every: int, lock_seconds: int, max_lock_seconds: int
    ) -> datetime | None:
        now = datetime.now(UTC)
        if not self._confirmed(user_id):
            return now
        row = self.factors[user_id]
        if row["locked_until"] is not None and row["locked_until"] > now:
            return row["locked_until"]
        row["failed_attempts"] += 1
        row["locked_until"] = None
        if row["failed_attempts"] % lock_every == 0:
            runs = min(row["failed_attempts"] // lock_every - 1, 20)
            row["locked_until"] = now + timedelta(
                seconds=min(lock_seconds * 2**runs, max_lock_seconds)
            )
        return None

    async def verify_session_two_factor(
        self,
        user_id: str,
        sid: str,
        environment: str,
        *,
        secret_encrypted: str | None = None,
        step: int | None = None,
        recovery_code_hash: str | None = None,
    ) -> str:
        if not self._confirmed(user_id):
            return "rejected"
        row = self.factors[user_id]
        codes = self.recovery.get(user_id, {})
        if recovery_code_hash is not None:
            if codes.get(recovery_code_hash) is not False:
                return "rejected"
        elif (
            step is None
            or row["secret_encrypted"] != secret_encrypted
            or (row["last_used_step"] is not None and row["last_used_step"] >= step)
        ):
            return "rejected"
        if not self._mark(user_id, sid, environment):
            return "session_ended"
        if recovery_code_hash is not None:
            codes[recovery_code_hash] = True
        else:
            row["last_used_step"] = step
        row.update(failed_attempts=0, locked_until=None)
        return "verified"

    async def replace_recovery_codes(self, user_id: str, code_hashes: Any) -> bool:
        if not self._confirmed(user_id):
            return False
        self.recovery[user_id] = dict.fromkeys(code_hashes, False)
        return True

    async def delete_two_factor(self, user_id: str) -> bool:
        self.recovery.pop(user_id, None)
        self._forget_sessions(user_id)
        return self.factors.pop(user_id, None) is not None

    async def get_session_two_factor(self, user_id: str, sid: str) -> dict[str, Any] | None:
        session = self.sessions.get((user_id, sid))
        if session is None:
            return None
        return {**session, "enrolled": self._confirmed(user_id)}

    def _mark(self, user_id: str, sid: str, environment: str) -> bool:
        session = self.sessions.get((user_id, sid))
        if session is None:
            return False
        session.update(verified_at=datetime.now(UTC), environment=environment)
        return True

    def _forget_sessions(self, user_id: str) -> None:
        for (owner, _sid), session in self.sessions.items():
            if owner == user_id:
                session.update(verified_at=None, environment=None)


def install(store: Any) -> TwoFactorFake:
    """Give a mock store working second-factor methods; return the fake behind them."""
    fake = TwoFactorFake()
    for name in STORE_METHODS:
        setattr(store, name, AsyncMock(side_effect=getattr(fake, name)))
    return fake
