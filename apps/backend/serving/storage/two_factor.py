"""PostgreSQL persistence for an account's second sign-in factor.

One row of ``user_two_factor`` per account that has, or is adding, an
authenticator; its recovery codes beside it; and on each ``auth_sessions`` row
when that session last gave a code and from what kind of client. The policy --
what a code is, when one is asked for -- is ``serving.auth.two_factor``; this
is only the rows, and the places where a comparison and a write have to be one
transaction.

Every write that decides what a session has proved takes the account's
``user_two_factor`` row lock first and does the rest inside that transaction:
accepting a code and marking the session (:meth:`verify_session_two_factor`),
replacing the authenticator and forgetting what sessions proved with the old
one (:meth:`confirm_two_factor`), removing it (:meth:`delete_two_factor`). So a
code accepted for one authenticator can never mark a session after another has
taken its place.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from contextlib import AbstractAsyncContextManager


async def _insert_recovery_codes(conn: Any, user_id: str, code_hashes: Sequence[str]) -> None:
    await conn.execute("DELETE FROM two_factor_recovery_codes WHERE user_id = $1", user_id)
    await conn.executemany(
        "INSERT INTO two_factor_recovery_codes (user_id, code_hash) VALUES ($1, $2)",
        [(user_id, code_hash) for code_hash in code_hashes],
    )


async def _forget_sessions(conn: Any, user_id: str) -> None:
    await conn.execute(
        "UPDATE auth_sessions SET two_factor_verified_at = NULL, two_factor_environment = NULL "
        "WHERE user_id = $1 AND two_factor_verified_at IS NOT NULL",
        user_id,
    )


async def _mark_session(conn: Any, user_id: str, sid: str, environment: str) -> bool:
    return bool(
        await conn.fetchval(
            "UPDATE auth_sessions SET two_factor_verified_at = NOW(), two_factor_environment = $3 "
            "WHERE user_id = $1 AND sid = $2 AND expires_at > NOW() AND revoked IS NOT TRUE "
            "RETURNING TRUE",
            user_id,
            sid,
            environment,
        )
    )


class _SessionEnded(Exception):
    """Roll back a verification whose session is gone: nothing was spent for it."""


class PostgresTwoFactorMixin:
    """Persist authenticators, recovery codes, and what each session has proved."""

    _pool: Any
    # The store's bounded-wait connection for request-path lookups.
    _request_connection: Callable[[], AbstractAsyncContextManager[Any]]

    async def get_two_factor(self, user_id: str) -> dict | None:
        """Fetch an account's second factor and how many recovery codes are unspent."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT t.secret_encrypted, t.confirmed_at, "
                "t.last_used_step, t.failed_attempts, t.locked_until, "
                # The session whose setup still waits for its first code, if any.
                "CASE WHEN t.pending_secret_encrypted IS NOT NULL "
                "  AND t.pending_expires_at > NOW() AND t.pending_attempts_left > 0 "
                "  THEN t.pending_session_id END AS pending_session_id, "
                "(SELECT COUNT(*) FROM two_factor_recovery_codes c "
                " WHERE c.user_id = t.user_id AND c.used_at IS NULL) AS recovery_codes_left "
                "FROM user_two_factor t WHERE t.user_id = $1",
                user_id,
            )
        return dict(row) if row else None

    async def set_pending_two_factor(
        self,
        user_id: str,
        pending_secret_encrypted: str,
        *,
        sid: str,
        ttl_seconds: int,
        attempts: int,
    ) -> None:
        """Record a secret session *sid* has been shown and not yet proved."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO user_two_factor (user_id, pending_secret_encrypted, "
                "pending_session_id, pending_expires_at, pending_attempts_left) "
                "VALUES ($1, $2, $3, NOW() + make_interval(secs => $4), $5) "
                "ON CONFLICT (user_id) DO UPDATE "
                "SET pending_secret_encrypted = EXCLUDED.pending_secret_encrypted, "
                "pending_session_id = EXCLUDED.pending_session_id, "
                "pending_expires_at = EXCLUDED.pending_expires_at, "
                "pending_attempts_left = EXCLUDED.pending_attempts_left",
                user_id,
                pending_secret_encrypted,
                sid,
                ttl_seconds,
                attempts,
            )

    async def begin_two_factor_setup_attempt(self, user_id: str, sid: str) -> str | None:
        """Take one of the tries session *sid*'s pending setup has left; return its secret.

        None when that session has no setup waiting: none was started, it
        ran out of time or of tries, or another session's replaced it.
        """
        async with self._pool.acquire() as conn:
            # Taken before the code is looked at, by the statement that decides
            # whether it may be: of any number of guesses sent at once, the row
            # lock lets through as many as there are tries left.
            return await conn.fetchval(
                "UPDATE user_two_factor SET pending_attempts_left = pending_attempts_left - 1 "
                "WHERE user_id = $1 AND pending_session_id = $2 "
                "AND pending_secret_encrypted IS NOT NULL "
                "AND pending_expires_at > NOW() AND pending_attempts_left > 0 "
                "RETURNING pending_secret_encrypted",
                user_id,
                sid,
            )

    async def cancel_pending_two_factor(self, user_id: str, sid: str) -> bool:
        """Drop the setup session *sid* started; False when it has none."""
        async with self._pool.acquire() as conn:
            return bool(
                await conn.fetchval(
                    "UPDATE user_two_factor SET pending_secret_encrypted = NULL, "
                    "pending_session_id = NULL, pending_expires_at = NULL, "
                    "pending_attempts_left = 0 "
                    "WHERE user_id = $1 AND pending_session_id = $2 "
                    "AND pending_secret_encrypted IS NOT NULL RETURNING TRUE",
                    user_id,
                    sid,
                )
            )

    async def confirm_two_factor(
        self,
        user_id: str,
        *,
        pending_secret_encrypted: str,
        step: int,
        recovery_code_hashes: Sequence[str],
        sid: str,
        environment: str,
    ) -> bool:
        """Make the pending secret the account's authenticator, in one transaction.

        Session *sid*, which gave the first code, is the one session left
        verified.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            # Matching on the pending value is what makes two confirmations of
            # the same secret, or one of a secret since replaced, lose cleanly.
            confirmed = await conn.fetchval(
                "UPDATE user_two_factor SET secret_encrypted = pending_secret_encrypted, "
                "pending_secret_encrypted = NULL, pending_session_id = NULL, "
                "pending_expires_at = NULL, pending_attempts_left = 0, "
                "confirmed_at = NOW(), last_used_step = $3, "
                "failed_attempts = 0, locked_until = NULL "
                "WHERE user_id = $1 AND pending_secret_encrypted = $2 RETURNING TRUE",
                user_id,
                pending_secret_encrypted,
                step,
            )
            if not confirmed:
                return False
            await _insert_recovery_codes(conn, user_id, recovery_code_hashes)
            # A new device is a new second factor: what sessions proved with
            # the old one, a holder of the old one could have proved too.
            await _forget_sessions(conn, user_id)
            await _mark_session(conn, user_id, sid, environment)
        return True

    async def begin_two_factor_attempt(
        self, user_id: str, *, lock_every: int, lock_seconds: int, max_lock_seconds: int
    ) -> datetime | None:
        """Count one attempt at the account's codes; return when it may next try, or None.

        None means the attempt may be checked. A time means the account's
        codes are held off until then and this attempt was not counted.
        """
        async with self._pool.acquire() as conn:
            # Counted before the code is looked at, in the statement that
            # decides whether it may be: of any number of guesses sent at once,
            # the row lock lets *lock_every* through and no more. The hold
            # doubles with each further run of failures, to a ceiling; the
            # exponent is capped so power() cannot overflow.
            counted = await conn.fetchval(
                "UPDATE user_two_factor SET failed_attempts = failed_attempts + 1, "
                "locked_until = CASE WHEN (failed_attempts + 1) % $2 = 0 "
                "  THEN NOW() + make_interval(secs => LEAST("
                "    $3 * power(2, LEAST((failed_attempts + 1) / $2 - 1, 20)), $4)) "
                "  ELSE NULL END "
                "WHERE user_id = $1 AND confirmed_at IS NOT NULL "
                "AND (locked_until IS NULL OR locked_until <= NOW()) RETURNING TRUE",
                user_id,
                lock_every,
                lock_seconds,
                max_lock_seconds,
            )
            if counted:
                return None
            # Its own statement, so it reads the hold a concurrent attempt has
            # just committed rather than the row as this one first saw it.
            held_until = await conn.fetchval(
                "SELECT locked_until FROM user_two_factor WHERE user_id = $1", user_id
            )
        # No row, or a hold that ran out in between: nothing to wait for but
        # nothing was counted either, so the caller is told to come back.
        return held_until or datetime.now(UTC)

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
        """Accept a code and mark session *sid* verified by it, or do neither.

        Give *secret_encrypted* and *step* for a code from the app: the
        stored secret the caller checked the code against, and the time step
        it matched. Give *recovery_code_hash* for a recovery code.

        Returns:
            ``"verified"``; ``"rejected"`` when the account has no such
            authenticator any more, the step is not newer than the last one
            accepted, or the recovery code is unknown or spent;
            ``"session_ended"`` when the code was good but the session is
            gone, in which case the code is not spent.
        """
        try:
            async with self._pool.acquire() as conn, conn.transaction():
                # The lock every change of authenticator takes. A replacement
                # that committed while this waited is what the row now shows.
                row = await conn.fetchrow(
                    "SELECT secret_encrypted, last_used_step FROM user_two_factor "
                    "WHERE user_id = $1 AND confirmed_at IS NOT NULL FOR UPDATE",
                    user_id,
                )
                if row is None:
                    return "rejected"
                if recovery_code_hash is not None:
                    spent = await conn.fetchval(
                        "UPDATE two_factor_recovery_codes SET used_at = NOW() "
                        "WHERE user_id = $1 AND code_hash = $2 AND used_at IS NULL "
                        "RETURNING TRUE",
                        user_id,
                        recovery_code_hash,
                    )
                    if not spent:
                        return "rejected"
                else:
                    # The code was computed from the secret the caller read;
                    # it says nothing about one that has replaced it since.
                    last = row["last_used_step"]
                    if (
                        step is None
                        or secret_encrypted is None
                        or row["secret_encrypted"] != secret_encrypted
                        or (last is not None and last >= step)
                    ):
                        return "rejected"
                if not await _mark_session(conn, user_id, sid, environment):
                    raise _SessionEnded
                # A right code ends the run of wrong ones, including the hold
                # the attempt that carried it may itself have just set.
                await conn.execute(
                    "UPDATE user_two_factor SET last_used_step = COALESCE($2, last_used_step), "
                    "failed_attempts = 0, locked_until = NULL WHERE user_id = $1",
                    user_id,
                    None if recovery_code_hash is not None else step,
                )
        except _SessionEnded:
            return "session_ended"
        return "verified"

    async def replace_recovery_codes(self, user_id: str, code_hashes: Sequence[str]) -> bool:
        """Replace a confirmed account's recovery codes; False when it has no authenticator."""
        async with self._pool.acquire() as conn, conn.transaction():
            # The row lock keeps a concurrent removal from leaving codes that
            # belong to no authenticator.
            confirmed = await conn.fetchval(
                "SELECT TRUE FROM user_two_factor "
                "WHERE user_id = $1 AND confirmed_at IS NOT NULL FOR UPDATE",
                user_id,
            )
            if not confirmed:
                return False
            await _insert_recovery_codes(conn, user_id, code_hashes)
        return True

    async def delete_two_factor(self, user_id: str) -> bool:
        """Remove an account's authenticator, its recovery codes and what its sessions proved."""
        async with self._pool.acquire() as conn, conn.transaction():
            # The recovery codes go with the row they reference.
            deleted = await conn.fetchval(
                "DELETE FROM user_two_factor WHERE user_id = $1 RETURNING TRUE", user_id
            )
            await _forget_sessions(conn, user_id)
        return bool(deleted)

    async def get_session_two_factor(self, user_id: str, sid: str) -> dict | None:
        """Fetch what session *sid* of *user_id* has proved, or None for no such live session."""
        # One probe of idx_auth_sessions_user_sid and one of the enrollment's
        # primary key; asked on every administrator request while the
        # deployment requires a second factor.
        async with self._request_connection() as conn:
            row = await conn.fetchrow(
                "SELECT s.two_factor_verified_at AS verified_at, "
                "s.two_factor_environment AS environment, "
                "EXISTS (SELECT 1 FROM user_two_factor t "
                "        WHERE t.user_id = s.user_id AND t.confirmed_at IS NOT NULL) AS enrolled "
                "FROM auth_sessions s "
                "WHERE s.user_id = $1 AND s.sid = $2 AND s.expires_at > NOW() "
                "AND s.revoked IS NOT TRUE",
                user_id,
                sid,
            )
        return dict(row) if row else None
