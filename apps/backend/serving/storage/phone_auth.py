"""Durable phone OTP state, serialized across gateway processes by PostgreSQL."""

from __future__ import annotations

import hmac
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from datetime import datetime

from serving.storage.log_schema import (
    apply_column_migrations,
    bounded_ddl,
    column_metadata,
    constraint_admitted_values,
    constraint_definition,
    execute_ddl,
)

#: What a code may be spent on. ``bind`` and ``delete_account`` codes belong to
#: a signed-in account, which must still be active when the code is sent and
#: when it is spent.
PHONE_CODE_PURPOSES = ("signup", "login", "bind", "delete_account")
_ACCOUNT_PURPOSES = ("bind", "delete_account")
_PURPOSE_CHECK = "CHECK (purpose IN ({}))".format(
    ", ".join(f"'{purpose}'" for purpose in PHONE_CODE_PURPOSES)
)


async def initialize_phone_auth(conn: Any) -> None:
    """Install phone identity and durable OTP storage idempotently."""
    await apply_column_migrations(
        conn,
        "users",
        [
            ("phone", "ALTER TABLE users ADD COLUMN IF NOT EXISTS phone TEXT"),
            (
                "phone_verified",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS phone_verified BOOLEAN NOT NULL DEFAULT FALSE",
            ),
        ],
    )
    for column in ("email", "password_hash"):
        meta = await column_metadata(conn, "users", column)
        if meta and meta["not_null"]:
            async with bounded_ddl(conn):
                await execute_ddl(conn, f"ALTER TABLE users ALTER COLUMN {column} DROP NOT NULL")
    await conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_phone_unique ON users(phone) WHERE phone IS NOT NULL"
    )
    await conn.execute(f"""
        CREATE TABLE IF NOT EXISTS phone_auth_codes (
            phone TEXT NOT NULL,
            purpose TEXT NOT NULL {_PURPOSE_CHECK},
            user_id TEXT NOT NULL DEFAULT '',
            challenge_id TEXT NOT NULL,
            code_hash TEXT NOT NULL,
            expires_at TIMESTAMPTZ NOT NULL,
            attempts INT NOT NULL DEFAULT 0,
            active BOOLEAN NOT NULL DEFAULT FALSE,
            consumed_at TIMESTAMPTZ,
            PRIMARY KEY (phone, purpose, user_id)
        )
    """)
    # A table created before ``delete_account`` existed keeps its old CHECK.
    # Rebuild it only when it does not admit exactly the current purposes, so
    # a settled restart takes no ACCESS EXCLUSIVE lock.
    definition = await constraint_definition(
        conn, "phone_auth_codes", "phone_auth_codes_purpose_check"
    )
    if constraint_admitted_values(definition) != set(PHONE_CODE_PURPOSES):
        async with bounded_ddl(conn), conn.transaction():
            await execute_ddl(
                conn,
                "ALTER TABLE phone_auth_codes "
                "DROP CONSTRAINT IF EXISTS phone_auth_codes_purpose_check",
            )
            await execute_ddl(
                conn,
                "ALTER TABLE phone_auth_codes "
                f"ADD CONSTRAINT phone_auth_codes_purpose_check {_PURPOSE_CHECK}",
            )
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS phone_auth_attempts (
            id BIGSERIAL PRIMARY KEY,
            phone TEXT NOT NULL,
            ip TEXT NOT NULL,
            kind TEXT NOT NULL CHECK (kind IN ('send', 'verify')),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_phone_attempts_phone ON phone_auth_attempts(phone, kind, created_at)"
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_phone_attempts_ip ON phone_auth_attempts(ip, kind, created_at)"
    )


async def _lock(conn: Any, phone: str, ip: str, kind: str) -> None:
    # Global ordering prevents deadlocks when requests share one quota key.
    for key in sorted((f"phone-auth:{kind}:phone:{phone}", f"phone-auth:{kind}:ip:{ip}")):
        await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", key)


class PostgresPhoneAuthMixin:
    """Persist phone credentials, quotas, and one-time verification challenges."""

    _pool: Any

    async def get_user_by_phone(self, phone: str) -> dict | None:
        """Fetch an account by its canonical phone number."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow("SELECT id FROM users WHERE phone = $1", phone)
        return await self.get_user_by_id(row["id"]) if row else None

    async def create_phone_user(
        self, *, user_id: str, phone: str, user_name: str, status: str, signup_reason: str | None
    ) -> bool:
        """Create a verified phone-only identity, refusing duplicate identifiers."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "INSERT INTO users (id, email, password_hash, phone, phone_verified, "
                "user_name, status, signup_reason) VALUES ($1, NULL, NULL, $2, TRUE, $3, $4, $5) "
                "ON CONFLICT DO NOTHING RETURNING id",
                user_id,
                phone,
                user_name,
                status,
                signup_reason,
            )
        return row is not None

    async def bind_user_phone(self, user_id: str, phone: str) -> bool:
        """Bind a verified phone atomically without replacing or merging identities."""
        import asyncpg

        try:
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(
                    "UPDATE users SET phone = $2, phone_verified = TRUE "
                    "WHERE id = $1 AND status = 'active' AND (phone IS NULL OR phone = $2) "
                    "RETURNING id",
                    user_id,
                    phone,
                )
            return row is not None
        except asyncpg.UniqueViolationError:
            return False

    async def bind_user_email(self, user_id: str, email: str) -> bool:
        """Bind or update a verified email atomically without merging identities."""
        import asyncpg

        normalized = email.strip().lower()
        try:
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(
                    "UPDATE users SET email = $2, email_verified = TRUE "
                    "WHERE id = $1 AND status = 'active' "
                    "RETURNING id",
                    user_id,
                    normalized,
                )
            return row is not None
        except asyncpg.UniqueViolationError:
            return False

    async def reserve_phone_code(
        self,
        *,
        phone: str,
        purpose: str,
        user_id: str | None,
        challenge_id: str,
        code_hash: str,
        ip: str,
        limits: dict[str, int],
    ) -> int:
        """Reserve a send and inactive challenge; return zero or retry delay."""
        async with self._pool.acquire() as conn, conn.transaction():
            if user_id:
                owner = await conn.fetchrow(
                    "SELECT status FROM users WHERE id = $1 FOR UPDATE", user_id
                )
                if (
                    not owner
                    or owner["status"] == "deleted"
                    or (purpose in _ACCOUNT_PURPOSES and owner["status"] != "active")
                ):
                    return 86400
            await _lock(conn, phone, ip, "send")
            stats = await conn.fetchrow(
                """
                SELECT
                    COUNT(*) FILTER (WHERE phone = $1 AND created_at > NOW() - INTERVAL '1 hour') AS phone_hour,
                    COUNT(*) FILTER (WHERE phone = $1) AS phone_day,
                    COUNT(*) FILTER (WHERE ip = $2 AND created_at > NOW() - INTERVAL '1 hour') AS ip_hour,
                    COUNT(*) FILTER (WHERE ip = $2) AS ip_day,
                    EXTRACT(EPOCH FROM NOW() - MAX(created_at) FILTER (WHERE phone = $1)) AS since_last
                FROM phone_auth_attempts
                WHERE kind = 'send' AND created_at > NOW() - INTERVAL '1 day'
                    AND (phone = $1 OR ip = $2)
                """,
                phone,
                ip,
            )
            if stats["since_last"] is not None and stats["since_last"] < limits["cooldown"]:
                return max(1, int(limits["cooldown"] - stats["since_last"]) + 1)
            for key, retry in (
                ("phone_hour", 3600),
                ("phone_day", 86400),
                ("ip_hour", 3600),
                ("ip_day", 86400),
            ):
                if stats[key] >= limits[key]:
                    return retry
            await conn.execute(
                "DELETE FROM phone_auth_attempts WHERE created_at < NOW() - INTERVAL '2 days'"
            )
            await conn.execute(
                "DELETE FROM phone_auth_codes WHERE expires_at < NOW() - INTERVAL '1 day'"
            )
            await conn.execute(
                "INSERT INTO phone_auth_attempts(phone, ip, kind) VALUES ($1, $2, 'send')",
                phone,
                ip,
            )
            await conn.execute(
                """
                INSERT INTO phone_auth_codes(phone, purpose, user_id, challenge_id, code_hash, expires_at)
                VALUES ($1, $2, $3, $4, $5, NOW() + $6 * INTERVAL '1 second')
                ON CONFLICT (phone, purpose, user_id) DO UPDATE SET
                    challenge_id = EXCLUDED.challenge_id, code_hash = EXCLUDED.code_hash,
                    expires_at = EXCLUDED.expires_at, attempts = 0, active = FALSE, consumed_at = NULL
                """,
                phone,
                purpose,
                user_id or "",
                challenge_id,
                code_hash,
                limits["expire"],
            )
        return 0

    async def activate_phone_code(self, challenge_id: str) -> bool:
        """Activate only the challenge acknowledged by the SMS provider."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "UPDATE phone_auth_codes SET active = TRUE WHERE challenge_id = $1 "
                "AND expires_at > NOW() AND consumed_at IS NULL RETURNING phone",
                challenge_id,
            )
        return row is not None

    async def invalidate_phone_code(self, challenge_id: str) -> None:
        """Remove a failed delivery without removing another concurrent challenge."""
        async with self._pool.acquire() as conn:
            await conn.execute("DELETE FROM phone_auth_codes WHERE challenge_id = $1", challenge_id)

    async def consume_phone_code(
        self,
        *,
        phone: str,
        purpose: str,
        user_id: str | None,
        code_hash: str,
        ip: str,
        max_attempts: int,
        phone_limit: int,
        ip_limit: int,
    ) -> str:
        """Commit verification attempts even on failure; consume once under a row lock."""
        async with self._pool.acquire() as conn, conn.transaction():
            if user_id:
                owner = await conn.fetchrow(
                    "SELECT status FROM users WHERE id = $1 FOR UPDATE", user_id
                )
                if (
                    not owner
                    or owner["status"] == "deleted"
                    or (purpose in _ACCOUNT_PURPOSES and owner["status"] != "active")
                ):
                    return "invalid"
            await _lock(conn, phone, ip, "verify")
            stats = await conn.fetchrow(
                """
                SELECT
                    COUNT(*) FILTER (WHERE phone = $1 AND created_at > NOW() - INTERVAL '15 minutes') AS phone_count,
                    COUNT(*) FILTER (WHERE ip = $2) AS ip_count
                FROM phone_auth_attempts WHERE kind = 'verify' AND created_at > NOW() - INTERVAL '1 hour'
                    AND (phone = $1 OR ip = $2)
                """,
                phone,
                ip,
            )
            if stats["phone_count"] >= phone_limit or stats["ip_count"] >= ip_limit:
                return "rate_limited"
            await conn.execute(
                "INSERT INTO phone_auth_attempts(phone, ip, kind) VALUES ($1, $2, 'verify')",
                phone,
                ip,
            )
            row = await conn.fetchrow(
                "SELECT *, expires_at > NOW() AS unexpired FROM phone_auth_codes "
                "WHERE phone = $1 AND purpose = $2 AND user_id = $3 FOR UPDATE",
                phone,
                purpose,
                user_id or "",
            )
            if (
                not row
                or not row["active"]
                or not row["unexpired"]
                or row["consumed_at"]
                or row["attempts"] >= max_attempts
            ):
                return "invalid"
            valid = hmac.compare_digest(row["code_hash"], code_hash)
            await conn.execute(
                "UPDATE phone_auth_codes SET attempts = attempts + 1, "
                "consumed_at = CASE WHEN $4 THEN NOW() ELSE consumed_at END "
                "WHERE phone = $1 AND purpose = $2 AND user_id = $3",
                phone,
                purpose,
                user_id or "",
                valid,
            )
            return "ok" if valid else "invalid"

    async def create_phone_session(
        self,
        *,
        session_id: str,
        user_id: str,
        phone: str,
        refresh_token_hash: str,
        jti: str,
        sid: str,
        expires_at: datetime,
        ip: str,
        user_agent: str | None,
    ) -> bool:
        """Issue a session, timestamp, and success event under an active-owner lock."""
        async with self._pool.acquire() as conn, conn.transaction():
            owner = await conn.fetchrow(
                "SELECT status, email, phone, phone_verified FROM users WHERE id = $1 FOR UPDATE",
                user_id,
            )
            if (
                not owner
                or owner["status"] != "active"
                or owner["phone"] != phone
                or not owner["phone_verified"]
            ):
                return False
            await conn.execute(
                "INSERT INTO auth_sessions "
                "(id, user_id, refresh_token_hash, jti, sid, expires_at, revoked) "
                "VALUES ($1, $2, $3, $4, $5, $6, FALSE)",
                session_id,
                user_id,
                refresh_token_hash,
                jti,
                sid,
                expires_at,
            )
            await conn.execute("UPDATE users SET last_login_at = NOW() WHERE id = $1", user_id)
            await conn.execute(
                "INSERT INTO login_events(user_id, email, phone, outcome, failure_reason, ip, user_agent) "
                "VALUES ($1, $2, $3, 'success', NULL, $4, $5)",
                user_id,
                owner["email"],
                phone,
                ip,
                user_agent,
            )
        return True

    async def record_phone_login_event(
        self,
        *,
        phone: str,
        email: str | None,
        outcome: str,
        failure_reason: str | None,
        user_id: str | None,
        ip: str,
        user_agent: str | None,
    ) -> None:
        """Record phone login outcomes without fabricating an email."""
        async with self._pool.acquire() as conn, conn.transaction():
            if user_id:
                owner = await conn.fetchrow(
                    "SELECT status FROM users WHERE id = $1 FOR UPDATE", user_id
                )
                if not owner or owner["status"] == "deleted":
                    return
            await conn.execute(
                "INSERT INTO login_events(user_id, email, phone, outcome, failure_reason, ip, user_agent) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7)",
                user_id,
                email,
                phone,
                outcome,
                failure_reason,
                ip,
                user_agent,
            )
