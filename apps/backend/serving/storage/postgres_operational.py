"""PostgreSQL implementation of OperationalStore.

Consolidates all user, API-key, session, token, and audit SQL that was
previously scattered across auth.py, admin.py, auth_routes.py,
user_routes.py, internal.py, and user_stats.py.
"""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING, Any, Literal

from serving.storage.base import OperationalStore, Row
from serving.utils.logging import get_logger

if TYPE_CHECKING:
    from datetime import datetime
    from decimal import Decimal

    import asyncpg

logger = get_logger(__name__)


def _parse_command_tag_count(command_tag: str) -> int:
    """Extract the affected row count from an asyncpg command tag."""
    parts = command_tag.split()
    if not parts:
        return 0
    try:
        return int(parts[-1])
    except ValueError:
        return 0


def _parse_admin_emails(raw: str) -> list[str]:
    """Return normalized admin email addresses from a comma-separated env var."""
    return [email.strip().lower() for email in raw.split(",") if email.strip()]


class PostgresOperationalStore(OperationalStore):
    """OperationalStore backed by an asyncpg connection pool."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        """Initialize with a shared asyncpg pool."""
        self._pool = pool

    # -- lifecycle -----------------------------------------------------------

    async def initialize(self) -> None:
        """Create operational tables/indexes and run idempotent migrations."""
        async with self._pool.acquire() as conn:
            await self._create_tables(conn)

    async def _create_tables(self, conn: asyncpg.Connection) -> None:
        """DDL for the 6 operational tables."""
        import asyncpg as _asyncpg

        # --- users ---
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                email TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                user_name TEXT,
                preferences JSONB NOT NULL DEFAULT '{}'::jsonb,
                role TEXT NOT NULL DEFAULT 'free'
                    CHECK (role IN ('free', 'pro', 'internal', 'admin')),
                email_verified BOOLEAN DEFAULT FALSE,
                status TEXT DEFAULT 'active'
                    CHECK (status IN ('active', 'suspended', 'deleted',
                                      'pending_approval', 'rejected')),
                approval_note TEXT,
                reviewed_at TIMESTAMPTZ,
                reviewed_by TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                last_login_at TIMESTAMPTZ
            )
        """)
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_users_email ON users(email)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_users_status ON users(status)")
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_users_created_at ON users(created_at DESC)"
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_users_pending_approval "
            "ON users(created_at DESC) WHERE status = 'pending_approval'"
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_users_last_login_at "
            "ON users(last_login_at DESC NULLS LAST)"
        )

        # Migrations
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS approval_note TEXT")
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS reviewed_at TIMESTAMPTZ")
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS reviewed_by TEXT")
        await conn.execute(
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS "
            "preferences JSONB NOT NULL DEFAULT '{}'::jsonb"
        )

        # Status constraint rebuild
        try:
            async with conn.transaction():
                await conn.execute("ALTER TABLE users DROP CONSTRAINT IF EXISTS users_status_check")
                await conn.execute("""
                    ALTER TABLE users ADD CONSTRAINT users_status_check
                    CHECK (status IN ('active', 'suspended', 'deleted',
                                      'pending_approval', 'rejected'))
                """)
        except _asyncpg.PostgresError as exc:
            invalid_rows = await conn.fetch(
                "SELECT id, email, status FROM users "
                "WHERE status NOT IN ('active','suspended','deleted',"
                "'pending_approval','rejected') "
                "ORDER BY created_at DESC LIMIT 10"
            )
            logger.error(
                "Failed to rebuild users_status_check; transaction rolled back. "
                "Sample invalid rows=%s error=%s",
                [dict(row) for row in invalid_rows],
                exc,
            )
            raise

        # Role column & migration
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS role TEXT")
        await conn.execute("ALTER TABLE users ALTER COLUMN role SET DEFAULT 'free'")
        tag = await conn.execute("UPDATE users SET role = 'free' WHERE role IS NULL")
        backfilled = _parse_command_tag_count(tag)
        if backfilled:
            logger.info("Backfilled default user role for %d existing rows.", backfilled)

        await conn.execute("ALTER TABLE users ALTER COLUMN role SET NOT NULL")

        try:
            async with conn.transaction():
                await conn.execute("ALTER TABLE users DROP CONSTRAINT IF EXISTS users_role_check")
                tag = await conn.execute(
                    "UPDATE users SET role = 'internal' "
                    "WHERE role IN ('internal_group', 'developer')"
                )
                migrated = _parse_command_tag_count(tag)
                if migrated:
                    logger.info(
                        "Migrated %d users from internal_group/developer to internal.",
                        migrated,
                    )
                await conn.execute("""
                    ALTER TABLE users ADD CONSTRAINT users_role_check
                    CHECK (role IN ('free', 'pro', 'internal', 'admin'))
                """)
        except _asyncpg.PostgresError as exc:
            invalid_rows = await conn.fetch(
                "SELECT id, email, role FROM users "
                "WHERE role NOT IN ('free','pro','internal','admin') "
                "ORDER BY created_at DESC LIMIT 10"
            )
            logger.error(
                "Failed to rebuild users_role_check; transaction rolled back. "
                "Sample invalid rows=%s error=%s",
                [dict(row) for row in invalid_rows],
                exc,
            )
            raise

        # Seed admin roles from ADMIN_EMAILS
        admin_emails = _parse_admin_emails(os.getenv("ADMIN_EMAILS", ""))
        if admin_emails:
            tag = await conn.execute(
                "UPDATE users SET role = 'admin' "
                "WHERE lower(trim(email)) = ANY($1::text[]) AND role = 'free'",
                admin_emails,
            )
            seeded = _parse_command_tag_count(tag)
            if seeded:
                logger.info("Seeded %d admin role assignments from ADMIN_EMAILS.", seeded)

        # --- api_keys ---
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS api_keys (
                id BIGSERIAL PRIMARY KEY,
                key_hash TEXT NOT NULL UNIQUE,
                key_prefix TEXT NOT NULL,
                user_id TEXT NOT NULL,
                user_name TEXT,
                status TEXT NOT NULL DEFAULT 'active',
                quota_daily_cost_usd DECIMAL(10, 4) DEFAULT 1000.00,
                quota_monthly_cost_usd DECIMAL(10, 4),
                created_at TIMESTAMPTZ DEFAULT NOW(),
                expires_at TIMESTAMPTZ,
                last_used_at TIMESTAMPTZ,
                notes TEXT,
                metadata JSONB
            )
        """)
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_api_keys_user ON api_keys(user_id)")
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_api_keys_status ON api_keys(status, expires_at)"
        )
        await conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_api_keys_prefix_unique ON api_keys(key_prefix)"
        )
        await conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_api_keys_user_unique "
            "ON api_keys(user_id) WHERE status = 'active'"
        )
        # Migrations
        await conn.execute(
            "ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS "
            "quota_daily_cost_usd DECIMAL(10, 4) DEFAULT 1000.00"
        )
        await conn.execute(
            "ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS quota_monthly_cost_usd DECIMAL(10, 4)"
        )
        await conn.execute("ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS account_id TEXT")
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_api_keys_account ON api_keys(account_id)"
        )
        await conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_api_keys_account_active_unique "
            "ON api_keys(account_id) WHERE status = 'active' AND account_id IS NOT NULL"
        )
        # Drop legacy 'tier' column — tier/role unified on users.role
        # (spec: docs/agents/specs/2026-05-02-unify-tier-role-design.md).
        await conn.execute("""
            ALTER TABLE api_keys DROP COLUMN IF EXISTS tier
        """)

        # --- auth_sessions ---
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS auth_sessions (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                refresh_token_hash TEXT NOT NULL UNIQUE,
                jti TEXT,
                sid TEXT NOT NULL,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                last_used_at TIMESTAMPTZ,
                expires_at TIMESTAMPTZ NOT NULL,
                revoked BOOLEAN DEFAULT FALSE,
                user_agent TEXT,
                ip_address TEXT
            )
        """)
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_auth_sessions_user "
            "ON auth_sessions(user_id, expires_at)"
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_auth_sessions_token "
            "ON auth_sessions(refresh_token_hash) WHERE NOT revoked"
        )
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_auth_sessions_jti ON auth_sessions(jti)")

        # --- login_events ---
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS login_events (
                id BIGSERIAL PRIMARY KEY,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                user_id TEXT,
                email TEXT NOT NULL,
                outcome TEXT NOT NULL
                    CHECK (outcome IN ('success', 'failure')),
                failure_reason TEXT,
                ip TEXT,
                user_agent TEXT
            )
            """
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_login_events_user "
            "ON login_events (user_id, created_at DESC) WHERE user_id IS NOT NULL"
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_login_events_email "
            "ON login_events (email, created_at DESC)"
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_login_events_created "
            "ON login_events (created_at DESC)"
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_login_events_failures "
            "ON login_events (created_at DESC) WHERE outcome = 'failure'"
        )

        # --- email_verification_tokens ---
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS email_verification_tokens (
                token TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                expires_at TIMESTAMPTZ NOT NULL,
                used_at TIMESTAMPTZ
            )
        """)
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_email_verification_user "
            "ON email_verification_tokens(user_id)"
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_email_verification_expires "
            "ON email_verification_tokens(expires_at)"
        )

        # --- password_reset_tokens ---
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS password_reset_tokens (
                token TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                expires_at TIMESTAMPTZ NOT NULL,
                used_at TIMESTAMPTZ
            )
        """)
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_password_reset_user ON password_reset_tokens(user_id)"
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_password_reset_expires "
            "ON password_reset_tokens(expires_at)"
        )

        # --- admin_audit_log ---
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS admin_audit_log (
                id BIGSERIAL PRIMARY KEY,
                timestamp TIMESTAMPTZ DEFAULT NOW(),
                admin_ip TEXT NOT NULL,
                action TEXT NOT NULL,
                target_user_id TEXT,
                details JSONB,
                success BOOLEAN DEFAULT TRUE
            )
        """)
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_admin_audit_timestamp "
            "ON admin_audit_log(timestamp DESC)"
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_admin_audit_user "
            "ON admin_audit_log(target_user_id, timestamp DESC)"
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_admin_audit_action "
            "ON admin_audit_log(action, timestamp DESC)"
        )

        # --- user_daily_cost ---
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS user_daily_cost (
                user_id TEXT NOT NULL,
                day TEXT NOT NULL,
                cost_usd DECIMAL(12, 6) NOT NULL DEFAULT 0,
                requests INTEGER NOT NULL DEFAULT 0,
                last_request_at TIMESTAMPTZ,
                PRIMARY KEY (user_id, day)
            )
        """)
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_user_daily_cost_day ON user_daily_cost(day)"
        )

        # --- signup_allowed_domains ---
        # Admin-editable allowlist of email domains whose signups auto-approve.
        # Empty table = all signups auto-approve; non-empty = only listed
        # domains (exact or *.suffix) auto-approve, others go to pending_approval.
        # ``created_by`` uses ON DELETE SET NULL so hard-deleting a user who
        # once added an entry doesn't fail with a FK violation; the field is
        # informational/audit only.
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS signup_allowed_domains (
                domain TEXT NOT NULL,
                is_wildcard BOOLEAN NOT NULL DEFAULT FALSE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                created_by TEXT REFERENCES users(id) ON DELETE SET NULL,
                PRIMARY KEY (domain, is_wildcard)
            )
        """)

        # Migration: existing databases created the FK without
        # ON DELETE SET NULL, which blocks hard_delete_user. Rebuild the
        # constraint in place. Auto-generated name is
        # ``signup_allowed_domains_created_by_fkey``.
        try:
            async with conn.transaction():
                await conn.execute(
                    "ALTER TABLE signup_allowed_domains "
                    "DROP CONSTRAINT IF EXISTS signup_allowed_domains_created_by_fkey"
                )
                await conn.execute(
                    "ALTER TABLE signup_allowed_domains "
                    "ADD CONSTRAINT signup_allowed_domains_created_by_fkey "
                    "FOREIGN KEY (created_by) REFERENCES users(id) ON DELETE SET NULL"
                )
        except _asyncpg.PostgresError as exc:
            logger.error(
                "Failed to rebuild signup_allowed_domains_created_by_fkey "
                "with ON DELETE SET NULL; transaction rolled back. error=%s",
                exc,
            )
            raise

        # --- site_settings ---
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS site_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                value_type TEXT NOT NULL DEFAULT 'str',
                updated_at TIMESTAMPTZ DEFAULT NOW(),
                updated_by TEXT
            )
        """)

    async def cleanup(self) -> None:
        """No-op — pool lifecycle is managed externally."""

    async def health_check(self) -> bool:
        """Return True if the pool can execute a trivial query."""
        try:
            async with self._pool.acquire() as conn:
                await conn.fetchval("SELECT 1")
            return True
        except Exception:
            return False

    # -- users ---------------------------------------------------------------

    async def get_user_by_id(self, user_id: str) -> Row | None:
        """Fetch a single user row by primary key."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, email, user_name, role, status, email_verified, "
                "created_at, last_login_at, password_hash, preferences "
                "FROM users WHERE id = $1",
                user_id,
            )
        return dict(row) if row else None

    async def get_user_by_email(self, email: str) -> Row | None:
        """Fetch a single user row by lowercased email."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, email, user_name, role, status, email_verified, "
                "created_at, last_login_at, password_hash, preferences "
                "FROM users WHERE email = $1",
                email.lower(),
            )
        return dict(row) if row else None

    async def create_user(
        self,
        *,
        user_id: str,
        email: str,
        password_hash: str,
        user_name: str | None = None,
        email_verified: bool = False,
        status: str = "active",
    ) -> None:
        """Insert a new user row."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO users (id, email, password_hash, user_name, "
                "email_verified, status) VALUES ($1, $2, $3, $4, $5, $6)",
                user_id,
                email.lower(),
                password_hash,
                user_name,
                email_verified,
                status,
            )

    async def update_user_fields(self, user_id: str, **fields: Any) -> None:
        """Update one or more columns on the users table for *user_id*."""
        if not fields:
            return
        from .base import USERS_MUTABLE_COLUMNS

        invalid = set(fields) - USERS_MUTABLE_COLUMNS.keys()
        if invalid:
            raise ValueError(f"Invalid column(s) for users: {invalid}")
        set_parts = []
        params: list[Any] = [user_id]
        for idx, (key, val) in enumerate(fields.items(), start=2):
            col = USERS_MUTABLE_COLUMNS[key]
            set_parts.append(f"{col} = ${idx}")
            params.append(val)
        sql = f"UPDATE users SET {', '.join(set_parts)} WHERE id = $1"
        async with self._pool.acquire() as conn:
            await conn.execute(sql, *params)

    async def update_user_last_login(self, user_id: str) -> None:
        """Set ``last_login_at`` to the current timestamp."""
        async with self._pool.acquire() as conn:
            await conn.execute("UPDATE users SET last_login_at = NOW() WHERE id = $1", user_id)

    async def delete_user(
        self,
        user_id: str,
        *,
        admin_ip: str,
        admin_id: str,
        reason: str | None = None,
        email: str | None = None,
    ) -> None:
        """Soft-delete: set status='deleted', revoke keys, purge sessions/tokens.

        All mutations and the audit-log insert are atomic (single transaction).
        """
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute("UPDATE users SET status = 'deleted' WHERE id = $1", user_id)
            await conn.execute(
                "UPDATE api_keys SET status = 'revoked' "
                "WHERE (account_id = $1 OR user_id = $1) AND status = 'active'",
                user_id,
            )
            await conn.execute("DELETE FROM auth_sessions WHERE user_id = $1", user_id)
            await conn.execute("DELETE FROM email_verification_tokens WHERE user_id = $1", user_id)
            await conn.execute("DELETE FROM password_reset_tokens WHERE user_id = $1", user_id)
            await conn.execute(
                "INSERT INTO admin_audit_log "
                "(admin_ip, action, target_user_id, details, success) "
                "VALUES ($1, $2, $3, $4::jsonb, $5)",
                admin_ip,
                "delete_user",
                user_id,
                json.dumps({"email": email, "reason": reason}),
                True,
            )

    async def resume_user(
        self,
        user_id: str,
        *,
        admin_ip: str,
        admin_id: str,
        reason: str | None = None,
        email: str | None = None,
    ) -> None:
        """Resume a soft-deleted user: set status='active' and audit.

        API keys remain ``revoked`` — the user re-creates one through the
        normal flow.  All mutations and the audit-log insert are atomic.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute("UPDATE users SET status = 'active' WHERE id = $1", user_id)
            await conn.execute(
                "INSERT INTO admin_audit_log "
                "(admin_ip, action, target_user_id, details, success) "
                "VALUES ($1, $2, $3, $4::jsonb, $5)",
                admin_ip,
                "resume_user",
                user_id,
                json.dumps({"admin_id": admin_id, "email": email, "reason": reason}),
                True,
            )

    async def hard_delete_user(
        self,
        user_id: str,
        *,
        admin_ip: str,
        admin_id: str,
        reason: str | None = None,
        email: str | None = None,
    ) -> dict[str, int]:
        """Permanently delete a user row and all operationally-linked rows.

        ``api_logs`` and ``email_broadcast_recipients`` live in the LogStore —
        the caller must purge them separately via
        ``LogStore.hard_delete_user_data``.  Returns ``{table_name: row_count}``
        for audit details.
        """

        def _row_count(status: str) -> int:
            # asyncpg execute() returns "DELETE <n>" / "UPDATE <n>" — parse n
            try:
                return int(status.rsplit(" ", 1)[-1])
            except (ValueError, IndexError):
                return 0

        async with self._pool.acquire() as conn, conn.transaction():
            keys_status = await conn.execute(
                "DELETE FROM api_keys WHERE account_id = $1 OR user_id = $1",
                user_id,
            )
            sessions_status = await conn.execute(
                "DELETE FROM auth_sessions WHERE user_id = $1", user_id
            )
            verif_status = await conn.execute(
                "DELETE FROM email_verification_tokens WHERE user_id = $1", user_id
            )
            reset_status = await conn.execute(
                "DELETE FROM password_reset_tokens WHERE user_id = $1", user_id
            )
            cost_status = await conn.execute(
                "DELETE FROM user_daily_cost WHERE user_id = $1", user_id
            )
            audit_status = await conn.execute(
                "DELETE FROM admin_audit_log WHERE target_user_id = $1", user_id
            )
            user_status = await conn.execute("DELETE FROM users WHERE id = $1", user_id)

            counts = {
                "api_keys": _row_count(keys_status),
                "auth_sessions": _row_count(sessions_status),
                "email_verification_tokens": _row_count(verif_status),
                "password_reset_tokens": _row_count(reset_status),
                "user_daily_cost": _row_count(cost_status),
                "admin_audit_log": _row_count(audit_status),
                "users": _row_count(user_status),
            }

            # Insert NEW audit row for the hard-delete itself.  The prior
            # entries for this user were wiped above — the caller accepted
            # that compliance loss.  No FK on target_user_id, so an orphan
            # reference here is fine.
            await conn.execute(
                "INSERT INTO admin_audit_log "
                "(admin_ip, action, target_user_id, details, success) "
                "VALUES ($1, $2, $3, $4::jsonb, $5)",
                admin_ip,
                "hard_delete_user",
                user_id,
                json.dumps(
                    {
                        "admin_id": admin_id,
                        "email": email,
                        "reason": reason,
                        "rows_wiped": counts,
                    }
                ),
                True,
            )

        return counts

    async def list_users(
        self,
        *,
        status: str | None = None,
        search: str | None = None,
        sort_by: Literal[
            "created", "cost_today", "cost_month", "cost_alltime", "last_login"
        ] = "created",
        limit: int = 100,
        offset: int = 0,
        # NEW filters (Phase 1 admin Users redesign)
        min_cost_today: Decimal | None = None,
        min_cost_month: Decimal | None = None,
        quota_state: Literal["near", "over", "custom", "default"] | None = None,
        provider: str | None = None,
        active_within_hours: int | None = None,
        anomaly: bool | None = None,
    ) -> tuple[int, list[Row], Row]:
        """Return ``(total_count, user_rows, status_counts_row)``.

        All filters are applied in SQL so ``total``, the returned rows, and
        pagination are consistent. Cost-based filters and sorts use CTEs that
        join against ``api_logs`` (which lives in the same Postgres instance
        for this implementation).

        Filters (all keyword-only):
        - ``min_cost_today`` / ``min_cost_month``: filter to users whose
          today/month spend in api_logs meets the threshold (USD).
        - ``quota_state``: ``"default"`` / ``"custom"`` filter via EXISTS on
          api_keys; ``"near"`` (>=80% of daily quota) / ``"over"`` (>=100%)
          compare today's api_logs spend against the active key's quota.
        - ``provider``: keep only users who hit ``provider`` in api_logs in
          the last 30 days. Uses the ``provider`` column on api_logs.
        - ``active_within_hours``: ``users.last_login_at`` must be within the
          window.
        - ``anomaly``: when ``True``, keep only *active* users whose today's
          spend is anomalously high vs. their prior 7-day average (today >=
          $1, history >= 3 days, today >= 5x avg). Uses ``user_daily_cost``
          (same rule as ``get_users_summary``).

        ``total`` reflects the count after every filter is applied.
        ``status_counts`` is intentionally computed from the unfiltered users
        table (no filters applied) — it serves as a global navigation aid
        showing how many users exist per status across the whole dataset,
        independent of the table view's filters.
        """
        # ──────────────────────────────────────────────────────────────────
        # Shared SQL fragments used by both the count query and the row
        # query so total/rows are guaranteed consistent.
        # ──────────────────────────────────────────────────────────────────
        from datetime import datetime, timedelta, timezone
        from decimal import Decimal as _Decimal

        anomaly_multiplier = _Decimal("5.0")
        anomaly_min_today = _Decimal("1.00")
        anomaly_min_history_days = 3
        today_str = datetime.now(timezone.utc).date().isoformat()
        prior_7d_start = (datetime.now(timezone.utc).date() - timedelta(days=7)).isoformat()
        prior_7d_end = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()

        where_clauses: list[str] = []
        filter_params: list[Any] = []

        if status:
            where_clauses.append(f"u.status = ${len(filter_params) + 1}")
            filter_params.append(status)
        if search:
            # Search now also matches user.id prefix and active key_prefix.
            substr_idx = len(filter_params) + 1  # %search%
            id_idx = len(filter_params) + 2  # search% (id prefix)
            kp_idx = len(filter_params) + 3  # search% (key prefix)
            where_clauses.append(
                f"(u.email ILIKE ${substr_idx} "
                f"OR u.user_name ILIKE ${substr_idx} "
                f"OR u.id::text LIKE ${id_idx} "
                f"OR EXISTS (SELECT 1 FROM api_keys k2 "
                f"           WHERE k2.account_id = u.id "
                f"             AND k2.status = 'active' "
                f"             AND k2.key_prefix LIKE ${kp_idx}))"
            )
            filter_params.append(f"%{search}%")
            filter_params.append(f"{search}%")
            filter_params.append(f"{search}%")
        if active_within_hours is not None:
            where_clauses.append(
                f"u.last_login_at >= NOW() - ${len(filter_params) + 1}::int * INTERVAL '1 hour'"
            )
            filter_params.append(active_within_hours)
        if quota_state == "default":
            where_clauses.append(
                "EXISTS (SELECT 1 FROM api_keys k3 WHERE k3.account_id = u.id "
                "AND k3.status = 'active' AND k3.quota_daily_cost_usd IS NULL)"
            )
        elif quota_state == "custom":
            where_clauses.append(
                "EXISTS (SELECT 1 FROM api_keys k3 WHERE k3.account_id = u.id "
                "AND k3.status = 'active' AND k3.quota_daily_cost_usd IS NOT NULL)"
            )
        if provider:
            # Keep users who hit `provider` in the last 30 days.
            where_clauses.append(
                f"EXISTS (SELECT 1 FROM api_logs l WHERE l.user_id = u.id "
                f"  AND l.provider = ${len(filter_params) + 1} "
                f"  AND l.timestamp >= NOW() - INTERVAL '30 days')"
            )
            filter_params.append(provider)

        # Whether we need today/month aggregates for filtering or sorting.
        needs_today_filter = (
            min_cost_today is not None or quota_state in ("near", "over") or sort_by == "cost_today"
        )
        needs_month_filter = min_cost_month is not None or sort_by == "cost_month"
        needs_alltime_sort = sort_by == "cost_alltime"
        needs_today = needs_today_filter or needs_alltime_sort
        needs_month = needs_month_filter or needs_alltime_sort
        needs_alltime = needs_alltime_sort

        # Cost-based scalar correlated subqueries — placed in WHERE so
        # filters apply before LIMIT/OFFSET.
        if min_cost_today is not None:
            where_clauses.append(
                f"COALESCE((SELECT SUM(cost_usd) FROM api_logs l "
                f"  WHERE l.user_id = u.id "
                f"    AND l.timestamp >= date_trunc('day', NOW() AT TIME ZONE 'UTC')), 0) "
                f">= ${len(filter_params) + 1}"
            )
            filter_params.append(min_cost_today)
        if min_cost_month is not None:
            where_clauses.append(
                f"COALESCE((SELECT SUM(cost_usd) FROM api_logs l "
                f"  WHERE l.user_id = u.id "
                f"    AND l.timestamp >= date_trunc('month', NOW() AT TIME ZONE 'UTC')), 0) "
                f">= ${len(filter_params) + 1}"
            )
            filter_params.append(min_cost_month)
        if quota_state in ("near", "over"):
            # threshold: 0.80 for near, 1.00 for over.
            threshold = "0.80" if quota_state == "near" else "1.00"
            where_clauses.append(
                f"EXISTS ("
                f"  SELECT 1 FROM api_keys kq "
                f"  WHERE kq.account_id = u.id "
                f"    AND kq.status = 'active' "
                f"    AND kq.quota_daily_cost_usd IS NOT NULL "
                f"    AND kq.quota_daily_cost_usd > 0 "
                f"    AND COALESCE((SELECT SUM(cost_usd) FROM api_logs l "
                f"                  WHERE l.user_id = u.id "
                f"                    AND l.timestamp >= date_trunc('day', "
                f"                                                  NOW() AT TIME ZONE 'UTC')"
                f"                 ), 0) >= {threshold} * kq.quota_daily_cost_usd"
                f")"
            )
        if anomaly:
            # Anomaly applies only to active users (status='active').
            # Today's user_daily_cost row must exist with cost >= $1 AND
            # prior 7-day window must have >=3 days of history AND today
            # must be >= 5x the prior-7d average.
            today_idx = len(filter_params) + 1
            prior_start_idx = len(filter_params) + 2
            prior_end_idx = len(filter_params) + 3
            min_today_idx = len(filter_params) + 4
            min_history_idx = len(filter_params) + 5
            multiplier_idx = len(filter_params) + 6
            where_clauses.append(
                f"u.status = 'active' AND EXISTS ("
                f"  WITH t AS ("
                f"    SELECT COALESCE(SUM(cost_usd), 0) AS today_cost "
                f"    FROM user_daily_cost "
                f"    WHERE user_id = u.id AND day = ${today_idx} "
                f"  ), p AS ("
                f"    SELECT COALESCE(SUM(cost_usd), 0) AS total_prior, "
                f"           COUNT(DISTINCT day) AS days_history "
                f"    FROM user_daily_cost "
                f"    WHERE user_id = u.id "
                f"      AND day BETWEEN ${prior_start_idx} AND ${prior_end_idx}"
                f"  ) "
                f"  SELECT 1 FROM t, p "
                f"  WHERE t.today_cost >= ${min_today_idx} "
                f"    AND p.days_history >= ${min_history_idx} "
                f"    AND p.total_prior > 0 "
                f"    AND t.today_cost >= ${multiplier_idx} * (p.total_prior / p.days_history)"
                f")"
            )
            filter_params.extend(
                [
                    today_str,
                    prior_7d_start,
                    prior_7d_end,
                    anomaly_min_today,
                    anomaly_min_history_days,
                    anomaly_multiplier,
                ]
            )

        where_sql = "WHERE " + " AND ".join(where_clauses) if where_clauses else ""

        # Sort clause map (uses fu.* aliases defined in the CTE below).
        _sort_clauses = {
            "created": "fu.created_at DESC, fu.id",
            "cost_today": "COALESCE(ut.cost, 0) DESC, fu.created_at DESC, fu.id",
            "cost_month": "COALESCE(um.cost, 0) DESC, fu.created_at DESC, fu.id",
            "cost_alltime": "COALESCE(ua.cost, 0) DESC, fu.created_at DESC, fu.id",
            "last_login": "fu.last_login_at DESC NULLS LAST, fu.created_at DESC, fu.id",
        }
        order_clause = _sort_clauses[sort_by]

        limit_idx = len(filter_params) + 1
        offset_idx = len(filter_params) + 2
        query_params = [*filter_params, limit, offset]

        async with self._pool.acquire() as conn:
            # Total count — uses the SAME WHERE clause as the row query,
            # so total reflects every applied filter.
            count_row = await conn.fetchrow(
                f"SELECT COUNT(*) as total FROM users u {where_sql}",
                *filter_params,
            )
            total = count_row["total"] if count_row else 0

            # Status counts (unfiltered — global navigation aid).
            count_rows = await conn.fetch(
                "SELECT status, COUNT(*) as cnt FROM users GROUP BY status"
            )
            sc = {r["status"]: r["cnt"] for r in count_rows}
            status_counts: Row = {
                "all": sum(sc.values()),
                "pending_approval": sc.get("pending_approval", 0),
                "active": sc.get("active", 0),
                "suspended": sc.get("suspended", 0),
                "rejected": sc.get("rejected", 0),
                "deleted": sc.get("deleted", 0),
            }

            # Main query — always uses CTE form (filtered_users) so we can
            # share the WHERE clause and join optional cost CTEs uniformly.
            cte_parts = [
                f"filtered_users AS ("
                f"  SELECT u.id, u.email, u.user_name, u.role, u.status, "
                f"  u.email_verified, u.approval_note, u.reviewed_at, u.reviewed_by, "
                f"  u.created_at, u.last_login_at, "
                f"  k.key_prefix, k.status AS key_status "
                f"  FROM users u "
                f"  LEFT JOIN api_keys k ON k.account_id = u.id AND k.status = 'active' "
                f"  {where_sql}"
                f")"
            ]
            join_parts: list[str] = []
            select_extras: list[str] = []

            if needs_today:
                cte_parts.append(
                    "usage_today AS ("
                    "  SELECT user_id, COALESCE(SUM(cost_usd), 0) AS cost"
                    "  FROM api_logs"
                    "  WHERE user_id IN (SELECT id FROM filtered_users)"
                    "    AND timestamp >= date_trunc('day', NOW() AT TIME ZONE 'UTC')"
                    "  GROUP BY user_id"
                    ")"
                )
                join_parts.append("LEFT JOIN usage_today ut ON ut.user_id = fu.id")
                select_extras.append("COALESCE(ut.cost, 0) AS usage_today")

            if needs_month:
                cte_parts.append(
                    "usage_month AS ("
                    "  SELECT user_id, COALESCE(SUM(cost_usd), 0) AS cost"
                    "  FROM api_logs"
                    "  WHERE user_id IN (SELECT id FROM filtered_users)"
                    "    AND timestamp >= date_trunc('month', NOW() AT TIME ZONE 'UTC')"
                    "  GROUP BY user_id"
                    ")"
                )
                join_parts.append("LEFT JOIN usage_month um ON um.user_id = fu.id")
                select_extras.append("COALESCE(um.cost, 0) AS usage_month")

            if needs_alltime:
                cte_parts.append(
                    "usage_alltime AS ("
                    "  SELECT user_id, COALESCE(SUM(cost_usd), 0) AS cost"
                    "  FROM api_logs"
                    "  WHERE user_id IN (SELECT id FROM filtered_users)"
                    "  GROUP BY user_id"
                    ")"
                )
                join_parts.append("LEFT JOIN usage_alltime ua ON ua.user_id = fu.id")
                select_extras.append("COALESCE(ua.cost, 0) AS usage_alltime")

            extra_cols = ", " + ", ".join(select_extras) if select_extras else ""
            joins = " ".join(join_parts)
            ctes = ", ".join(cte_parts)

            rows = await conn.fetch(
                f"WITH {ctes} "
                f"SELECT fu.*{extra_cols} FROM filtered_users fu {joins} "
                f"ORDER BY {order_clause} "
                f"LIMIT ${limit_idx} OFFSET ${offset_idx}",
                *query_params,
            )

            if not rows:
                return total, [], status_counts

            # Post-fetch: batch-query usage dimensions not in CTEs so the
            # response always carries today/month costs for the page.
            user_ids = [row["id"] for row in rows if row["key_prefix"]]

            if user_ids and not needs_today:
                today_rows = await conn.fetch(
                    "SELECT user_id, COALESCE(SUM(cost_usd), 0) AS cost "
                    "FROM api_logs "
                    "WHERE user_id = ANY($1::text[]) "
                    "  AND timestamp >= date_trunc('day', NOW() AT TIME ZONE 'UTC') "
                    "GROUP BY user_id",
                    user_ids,
                )
                today_map = {r["user_id"]: r["cost"] for r in today_rows}
            else:
                today_map = {}

            if user_ids and not needs_month:
                month_rows = await conn.fetch(
                    "SELECT user_id, COALESCE(SUM(cost_usd), 0) AS cost "
                    "FROM api_logs "
                    "WHERE user_id = ANY($1::text[]) "
                    "  AND timestamp >= date_trunc('month', NOW() AT TIME ZONE 'UTC') "
                    "GROUP BY user_id",
                    user_ids,
                )
                month_map = {r["user_id"]: r["cost"] for r in month_rows}
            else:
                month_map = {}

        # Assemble result rows with usage columns.
        result_rows: list[Row] = []
        for row in rows:
            r = dict(row)
            r["usage_today"] = r.get("usage_today") or today_map.get(r["id"], 0)
            r["usage_month"] = r.get("usage_month") or month_map.get(r["id"], 0)
            r.setdefault("usage_alltime", 0)
            result_rows.append(r)

        return total, result_rows, status_counts

    async def approve_user(
        self,
        user_id: str,
        *,
        admin_id: str,
        note: str | None = None,
    ) -> None:
        """Set status='active', record reviewer and note."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE users SET status = 'active', approval_note = $1, "
                "reviewed_at = NOW(), reviewed_by = $2 WHERE id = $3",
                note,
                admin_id,
                user_id,
            )

    async def reject_user(
        self,
        user_id: str,
        *,
        admin_id: str,
        reason: str,
    ) -> None:
        """Set status='rejected', record reviewer and reason."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE users SET status = 'rejected', approval_note = $1, "
                "reviewed_at = NOW(), reviewed_by = $2 WHERE id = $3",
                reason,
                admin_id,
                user_id,
            )

    async def get_user_counts_by_status(self) -> dict[str, int]:
        """Return ``{status_value: count}`` for all statuses."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("SELECT status, COUNT(*) as cnt FROM users GROUP BY status")
        return {r["status"]: r["cnt"] for r in rows}

    async def get_active_user_counts(self) -> dict[str, int]:
        """Return ``{"total": N, "dau": N, "mau": N}``."""
        async with self._pool.acquire() as conn:
            total_row = await conn.fetchrow(
                "SELECT COUNT(*) as count FROM users WHERE status = 'active'"
            )
            dau_row = await conn.fetchrow(
                "SELECT COUNT(DISTINCT id) as count FROM users "
                "WHERE status = 'active' AND last_login_at >= NOW() - INTERVAL '24 hours'"
            )
            mau_row = await conn.fetchrow(
                "SELECT COUNT(DISTINCT id) as count FROM users "
                "WHERE status = 'active' AND last_login_at >= NOW() - INTERVAL '30 days'"
            )
        return {
            "total": total_row["count"] if total_row else 0,
            "dau": dau_row["count"] if dau_row else 0,
            "mau": mau_row["count"] if mau_row else 0,
        }

    # -- api keys ------------------------------------------------------------

    async def get_auth_context_by_key_hash(self, key_hash: str) -> Row | None:
        """Materialized auth lookup for ``verify_api_key``.

        Returns full projection in a single round-trip.
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT k.id, k.user_id, k.user_name, k.quota_daily_cost_usd, "
                "u.email, u.role, u.email_verified "
                "FROM api_keys k "
                "LEFT JOIN users u ON u.id = k.user_id "
                "WHERE k.key_hash = $1 "
                "  AND k.status = 'active' "
                "  AND (k.expires_at IS NULL OR k.expires_at > NOW()) "
                "  AND u.id IS NOT NULL AND u.status = 'active'",
                key_hash,
            )
        return dict(row) if row else None

    async def get_auth_context_lightweight(self, key_hash: str) -> Row | None:
        """Lightweight identity lookup (no quota check, no last_used write)."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT k.user_id, u.email, u.role, u.email_verified "
                "FROM api_keys k "
                "LEFT JOIN users u ON u.id = k.user_id "
                "WHERE k.key_hash = $1 "
                "  AND k.status = 'active' "
                "  AND (k.expires_at IS NULL OR k.expires_at > NOW()) "
                "  AND u.id IS NOT NULL AND u.status = 'active'",
                key_hash,
            )
        return dict(row) if row else None

    async def update_key_last_used(self, key_id: int) -> None:
        """Set ``last_used_at = NOW()`` for the given key id."""
        async with self._pool.acquire() as conn:
            await conn.execute("UPDATE api_keys SET last_used_at = NOW() WHERE id = $1", key_id)

    async def create_key(
        self,
        *,
        key_hash: str,
        key_prefix: str,
        user_id: str,
        user_name: str | None = None,
        quota_daily_cost_usd: Decimal | float = 1000.0,
        quota_monthly_cost_usd: Decimal | float | None = None,
        expires_at: datetime | None = None,
        notes: str | None = None,
        metadata: str | None = None,
        account_id: str | None = None,
    ) -> Row:
        """Insert a new API key. Returns the inserted row."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "INSERT INTO api_keys "
                "(key_hash, key_prefix, user_id, user_name, "
                "quota_daily_cost_usd, quota_monthly_cost_usd, "
                "expires_at, notes, metadata, account_id) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10) "
                "RETURNING id, created_at",
                key_hash,
                key_prefix,
                user_id,
                user_name,
                quota_daily_cost_usd,
                quota_monthly_cost_usd,
                expires_at,
                notes,
                metadata,
                account_id,
            )
        return dict(row)

    async def check_active_key_exists(self, user_id: str) -> bool:
        """Return True if *user_id* already has an active key."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id FROM api_keys WHERE user_id = $1 AND status = 'active'",
                user_id,
            )
        return row is not None

    async def list_keys(
        self,
        *,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[int, list[Row]]:
        """Return ``(total_count, key_rows)`` with optional filters."""
        where_clauses: list[str] = []
        params: list[Any] = []

        if status:
            where_clauses.append(f"status = ${len(params) + 1}")
            params.append(status)

        where_sql = "WHERE " + " AND ".join(where_clauses) if where_clauses else ""
        params.append(limit)
        params.append(offset)

        async with self._pool.acquire() as conn:
            count_row = await conn.fetchrow(
                f"SELECT COUNT(*) as total FROM api_keys {where_sql}",
                *params[: len(params) - 2],
            )
            total = count_row["total"] if count_row else 0

            rows = await conn.fetch(
                f"SELECT user_id, user_name, key_prefix, status, "
                f"quota_daily_cost_usd, quota_monthly_cost_usd, "
                f"created_at, last_used_at, expires_at, notes "
                f"FROM api_keys {where_sql} "
                f"ORDER BY created_at DESC "
                f"LIMIT ${len(params) - 1} OFFSET ${len(params)}",
                *params,
            )

        return total, [dict(r) for r in rows]

    async def get_key_detail(self, user_id: str) -> Row | None:
        """Fetch full key row for a given *user_id*."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT user_id, user_name, key_prefix, status, "
                "quota_daily_cost_usd, quota_monthly_cost_usd, "
                "created_at, last_used_at, expires_at, notes, metadata "
                "FROM api_keys WHERE user_id = $1",
                user_id,
            )
        return dict(row) if row else None

    async def update_key(self, user_id: str, **fields: Any) -> None:
        """Dynamically update key columns for *user_id*."""
        if not fields:
            return
        from .base import API_KEYS_MUTABLE_COLUMNS

        invalid = set(fields) - API_KEYS_MUTABLE_COLUMNS.keys()
        if invalid:
            raise ValueError(f"Invalid column(s) for api_keys: {invalid}")
        set_parts = []
        params: list[Any] = [user_id]
        for idx, (key, val) in enumerate(fields.items(), start=2):
            col = API_KEYS_MUTABLE_COLUMNS[key]
            set_parts.append(f"{col} = ${idx}")
            params.append(val)
        sql = f"UPDATE api_keys SET {', '.join(set_parts)} WHERE user_id = $1"
        async with self._pool.acquire() as conn:
            await conn.execute(sql, *params)

    async def revoke_key(self, user_id: str, *, hard_delete: bool = False) -> None:
        """Soft-revoke (status='revoked') or hard-delete the key."""
        async with self._pool.acquire() as conn:
            if hard_delete:
                await conn.execute("DELETE FROM api_keys WHERE user_id = $1", user_id)
            else:
                await conn.execute(
                    "UPDATE api_keys SET status = 'revoked' WHERE user_id = $1",
                    user_id,
                )

    async def regenerate_key(
        self,
        user_id: str,
        *,
        new_key_hash: str,
        new_key_prefix: str,
    ) -> str:
        """Atomically replace the key hash/prefix. Returns old key_prefix."""
        async with self._pool.acquire() as conn, conn.transaction():
            old_row = await conn.fetchrow(
                "SELECT key_prefix FROM api_keys WHERE user_id = $1", user_id
            )
            if not old_row:
                raise ValueError(f"No key found for user_id={user_id}")
            await conn.execute(
                "UPDATE api_keys SET key_hash = $1, key_prefix = $2 WHERE user_id = $3",
                new_key_hash,
                new_key_prefix,
                user_id,
            )
        return old_row["key_prefix"]

    async def get_key_by_account_or_user(self, account_id: str) -> Row | None:
        """Fetch key row by account_id or user_id."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id FROM api_keys "
                "WHERE (account_id = $1 OR user_id = $1) AND status = 'active' "
                "LIMIT 1",
                account_id,
            )
        return dict(row) if row else None

    async def get_active_key_by_account(self, account_id: str) -> Row | None:
        """Fetch the active key for self-registered user by account_id."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, key_prefix, created_at, last_used_at, status, "
                "quota_daily_cost_usd "
                "FROM api_keys WHERE account_id = $1 AND status = 'active'",
                account_id,
            )
        return dict(row) if row else None

    # -- auth sessions -------------------------------------------------------

    async def create_session(
        self,
        *,
        session_id: str,
        user_id: str,
        refresh_token_hash: str,
        jti: str,
        sid: str,
        expires_at: datetime,
    ) -> None:
        """Insert a new auth session."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO auth_sessions "
                "(id, user_id, refresh_token_hash, jti, sid, expires_at, revoked) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7)",
                session_id,
                user_id,
                refresh_token_hash,
                jti,
                sid,
                expires_at,
                False,
            )

    async def get_session_by_token_hash(self, token_hash: str) -> Row | None:
        """Fetch session row by refresh_token_hash."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, user_id, sid, expires_at, revoked "
                "FROM auth_sessions WHERE refresh_token_hash = $1",
                token_hash,
            )
        return dict(row) if row else None

    async def rotate_session(
        self,
        session_id: str,
        *,
        new_refresh_token_hash: str,
        new_jti: str,
    ) -> None:
        """Update the session with a new refresh token hash and JTI."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE auth_sessions "
                "SET last_used_at = NOW(), jti = $1, refresh_token_hash = $2 "
                "WHERE id = $3",
                new_jti,
                new_refresh_token_hash,
                session_id,
            )

    async def revoke_session(self, session_id: str) -> None:
        """Set ``revoked = TRUE`` on the session."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE auth_sessions SET revoked = TRUE WHERE id = $1",
                session_id,
            )

    async def delete_user_sessions(self, user_id: str) -> None:
        """Delete all sessions for a user."""
        async with self._pool.acquire() as conn:
            await conn.execute("DELETE FROM auth_sessions WHERE user_id = $1", user_id)

    # -- login events --------------------------------------------------------

    async def record_login_event(
        self,
        *,
        email: str,
        outcome: str,
        failure_reason: str | None,
        user_id: str | None,
        ip: str | None,
        user_agent: str | None,
    ) -> None:
        """Insert one ``login_events`` row."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO login_events
                    (user_id, email, outcome, failure_reason, ip, user_agent)
                VALUES ($1, $2, $3, $4, $5, $6)
                """,
                user_id,
                email,
                outcome,
                failure_reason,
                ip,
                user_agent,
            )

    async def purge_login_events_older_than(self, days: int) -> int:
        """Delete rows older than ``days`` days. Returns the deleted count."""
        async with self._pool.acquire() as conn:
            status = await conn.execute(
                "DELETE FROM login_events "
                "WHERE created_at < NOW() - ($1::int || ' days')::interval",
                days,
            )
        try:
            return int(status.rsplit(" ", 1)[-1])
        except (ValueError, IndexError):
            return 0

    async def purge_login_events_for_user(self, user_id: str) -> int:
        """Delete all rows for ``user_id``. Returns the deleted count."""
        async with self._pool.acquire() as conn:
            status = await conn.execute(
                "DELETE FROM login_events WHERE user_id = $1", user_id
            )
        try:
            return int(status.rsplit(" ", 1)[-1])
        except (ValueError, IndexError):
            return 0

    # -- email verification tokens -------------------------------------------

    async def create_verification_token(
        self,
        *,
        token: str,
        user_id: str,
        expires_at: datetime,
    ) -> None:
        """Insert a new email verification token."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO email_verification_tokens (token, user_id, expires_at) "
                "VALUES ($1, $2, $3)",
                token,
                user_id,
                expires_at,
            )

    async def get_verification_token(self, token: str) -> Row | None:
        """Fetch verification token row."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT token, user_id, created_at, expires_at, used_at "
                "FROM email_verification_tokens WHERE token = $1",
                token,
            )
        return dict(row) if row else None

    async def mark_verification_used(self, token: str) -> None:
        """Set ``used_at = NOW()`` on the token."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE email_verification_tokens SET used_at = NOW() WHERE token = $1",
                token,
            )

    async def mark_user_email_verified(self, user_id: str) -> None:
        """Set ``email_verified = TRUE`` on the user."""
        async with self._pool.acquire() as conn:
            await conn.execute("UPDATE users SET email_verified = TRUE WHERE id = $1", user_id)

    async def delete_user_verification_tokens(self, user_id: str) -> None:
        """Delete all verification tokens for a user."""
        async with self._pool.acquire() as conn:
            await conn.execute("DELETE FROM email_verification_tokens WHERE user_id = $1", user_id)

    # -- password reset tokens -----------------------------------------------

    async def create_reset_token(
        self,
        *,
        token: str,
        user_id: str,
        expires_at: datetime,
    ) -> None:
        """Insert a new password reset token."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO password_reset_tokens (token, user_id, expires_at) "
                "VALUES ($1, $2, $3)",
                token,
                user_id,
                expires_at,
            )

    async def get_reset_token(self, token: str) -> Row | None:
        """Fetch reset token row."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT token, user_id, created_at, expires_at, used_at "
                "FROM password_reset_tokens WHERE token = $1",
                token,
            )
        return dict(row) if row else None

    async def mark_reset_used(self, token: str) -> None:
        """Set ``used_at = NOW()`` on the token."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE password_reset_tokens SET used_at = NOW() WHERE token = $1",
                token,
            )

    async def delete_user_reset_tokens(self, user_id: str) -> None:
        """Delete all reset tokens for a user."""
        async with self._pool.acquire() as conn:
            await conn.execute("DELETE FROM password_reset_tokens WHERE user_id = $1", user_id)

    # -- admin audit log -----------------------------------------------------

    async def log_admin_action(
        self,
        *,
        admin_ip: str,
        action: str,
        target_user_id: str | None = None,
        details: dict[str, Any] | None = None,
        success: bool = True,
    ) -> None:
        """Insert a row into admin_audit_log."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO admin_audit_log "
                "(admin_ip, action, target_user_id, details, success) "
                "VALUES ($1, $2, $3, $4::jsonb, $5)",
                admin_ip,
                action,
                target_user_id,
                json.dumps(details) if details else None,
                success,
            )

    async def list_audit_log(
        self,
        *,
        action: str | None = None,
        target_user_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[int, list[Row]]:
        """Return ``(total_count, audit_rows)`` with optional filters."""
        where_clauses: list[str] = []
        params: list[Any] = []

        if action:
            where_clauses.append(f"action = ${len(params) + 1}")
            params.append(action)
        if target_user_id:
            where_clauses.append(f"target_user_id = ${len(params) + 1}")
            params.append(target_user_id)

        where_sql = "WHERE " + " AND ".join(where_clauses) if where_clauses else ""
        params.append(limit)
        params.append(offset)

        async with self._pool.acquire() as conn:
            count_row = await conn.fetchrow(
                f"SELECT COUNT(*) as total FROM admin_audit_log {where_sql}",
                *params[: len(params) - 2],
            )
            total = count_row["total"] if count_row else 0

            rows = await conn.fetch(
                f"SELECT id, timestamp, admin_ip, action, target_user_id, "
                f"details, success FROM admin_audit_log "
                f"{where_sql} ORDER BY timestamp DESC "
                f"LIMIT ${len(params) - 1} OFFSET ${len(params)}",
                *params,
            )

        return total, [dict(r) for r in rows]

    # -- user preferences ----------------------------------------------------

    async def get_user_preferences(self, user_id: str) -> dict[str, Any]:
        """Return the preferences JSONB column for *user_id*, parsed as dict."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow("SELECT preferences FROM users WHERE id = $1", user_id)
        if not row:
            return {}
        val = row["preferences"]
        if isinstance(val, dict):
            return dict(val)
        if isinstance(val, str):
            try:
                parsed = json.loads(val)
                if isinstance(parsed, dict):
                    return parsed
            except (json.JSONDecodeError, TypeError):
                logger.debug("Malformed preferences payload for user %s", user_id)
        return {}

    async def update_user_preferences(
        self,
        user_id: str,
        preferences: dict[str, Any],
    ) -> None:
        """Atomically replace the full preferences JSONB column."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE users SET preferences = $1::jsonb WHERE id = $2",
                json.dumps(preferences),
                user_id,
            )

    # -- signup domain allowlist --------------------------------------------

    async def list_signup_allowed_domains(self) -> list[Row]:
        """Return all allowlist rows joined with the creator's email."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT d.domain, d.is_wildcard, d.created_at, d.created_by, "
                "u.email AS created_by_email "
                "FROM signup_allowed_domains d "
                "LEFT JOIN users u ON u.id = d.created_by "
                "ORDER BY d.created_at DESC, d.domain ASC"
            )
        return [dict(r) for r in rows]

    async def add_signup_allowed_domain(
        self,
        *,
        domain: str,
        is_wildcard: bool,
        created_by: str | None,
    ) -> Row:
        """Insert a new allowlist entry. Raises on duplicate composite key."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "INSERT INTO signup_allowed_domains (domain, is_wildcard, created_by) "
                "VALUES ($1, $2, $3) "
                "RETURNING domain, is_wildcard, created_at, created_by",
                domain,
                is_wildcard,
                created_by,
            )
        # Backfill creator email so the response shape matches list_*.
        result = dict(row) if row else {}
        if result.get("created_by"):
            async with self._pool.acquire() as conn:
                creator = await conn.fetchrow(
                    "SELECT email FROM users WHERE id = $1", result["created_by"]
                )
            result["created_by_email"] = creator["email"] if creator else None
        else:
            result["created_by_email"] = None
        return result

    async def remove_signup_allowed_domain(
        self,
        *,
        domain: str,
        is_wildcard: bool,
    ) -> bool:
        """Delete an allowlist entry; returns True when a row was removed."""
        async with self._pool.acquire() as conn:
            tag = await conn.execute(
                "DELETE FROM signup_allowed_domains WHERE domain = $1 AND is_wildcard = $2",
                domain,
                is_wildcard,
            )
        return _parse_command_tag_count(tag) > 0

    async def signup_allowlist_is_empty(self) -> bool:
        """Return True if the allowlist table has no rows."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow("SELECT 1 FROM signup_allowed_domains LIMIT 1")
        return row is None

    async def is_signup_domain_allowed(self, email: str) -> bool:
        """Match *email*'s domain against the allowlist (exact or wildcard).

        Uses a single round-trip: builds the set of candidate strings
        (the email domain itself, plus each parent suffix that could be
        a wildcard match) and checks whether any allowlist row matches
        with the appropriate ``is_wildcard`` flag. The bare top-level
        domain is excluded from wildcard candidates so ``*.acme.com``
        does not match ``alice@acme.com``.
        """
        if "@" not in email:
            return False
        domain = email.rsplit("@", 1)[1].strip().lower()
        if not domain:
            return False

        parts = domain.split(".")
        # Wildcard candidates: parent suffixes that *.suffix would match.
        # For a.b.example.com (parts=4): b.example.com, example.com.
        # Stops at len(parts)-1 to exclude the bare TLD.
        wildcard_candidates = [".".join(parts[i:]) for i in range(1, len(parts) - 1)]
        # Single query covers both exact and wildcard checks: for each
        # returned row, decide acceptance based on its is_wildcard flag.
        candidates = list({domain, *wildcard_candidates})
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT domain, is_wildcard FROM signup_allowed_domains "
                "WHERE domain = ANY($1::text[])",
                candidates,
            )
        wildcard_set = set(wildcard_candidates)
        for row in rows:
            row_domain = row["domain"]
            row_is_wildcard = bool(row["is_wildcard"])
            if not row_is_wildcard and row_domain == domain:
                return True
            if row_is_wildcard and row_domain in wildcard_set:
                return True
        return False

    # -- site settings --------------------------------------------------------

    async def get_setting(self, key: str) -> Row | None:
        """Fetch a single site_settings row by key."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT key, value, value_type, updated_at, updated_by "
                "FROM site_settings WHERE key = $1",
                key,
            )
        return dict(row) if row else None

    async def set_setting(
        self, key: str, value: str, value_type: str, updated_by: str | None
    ) -> None:
        """Upsert a site_settings row."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO site_settings (key, value, value_type, updated_at, updated_by) "
                "VALUES ($1, $2, $3, NOW(), $4) "
                "ON CONFLICT (key) DO UPDATE SET "
                "value = $2, value_type = $3, updated_at = NOW(), updated_by = $4",
                key,
                value,
                value_type,
                updated_by,
            )

    async def list_settings(self) -> list[Row]:
        """Return all site_settings rows ordered by key."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT key, value, value_type, updated_at, updated_by "
                "FROM site_settings ORDER BY key"
            )
        return [dict(r) for r in rows]

    # -- cost counters -------------------------------------------------------

    async def increment_user_cost(
        self,
        user_id: str,
        cost_usd: float,
        *,
        day: str | None = None,
    ) -> None:
        """Atomically increment the daily cost counter via upsert."""
        from datetime import datetime, timezone

        if day is None:
            day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO user_daily_cost (user_id, day, cost_usd, requests, last_request_at) "
                "VALUES ($1, $2, $3, 1, NOW()) "
                "ON CONFLICT (user_id, day) DO UPDATE SET "
                "cost_usd = user_daily_cost.cost_usd + $3, "
                "requests = user_daily_cost.requests + 1, "
                "last_request_at = NOW()",
                user_id,
                day,
                cost_usd,
            )

    async def get_user_cost_today(self, user_id: str) -> float:
        """Return today's cost from the counter table."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT cost_usd FROM user_daily_cost "
                "WHERE user_id = $1 AND day = to_char(NOW() AT TIME ZONE 'UTC', 'YYYY-MM-DD')",
                user_id,
            )
        return float(row["cost_usd"]) if row else 0.0

    async def query_users_over_daily_threshold(
        self,
        thresholds: dict[str, float],
    ) -> list[tuple[str, str, float]]:
        """Return users whose today's cost exceeds the per-role threshold.

        Joins the daily-cost counter table to the users table for today's row,
        and filters by the per-role threshold. Used by the
        ``UserCostOverrunJob`` periodic alert.

        Roles and thresholds are passed as bound parameters via a VALUES-based
        CTE built with ``unnest``, so callers may pass arbitrary dict keys
        without risking SQL injection.
        """
        if not thresholds:
            return []
        roles = list(thresholds.keys())
        values = [float(thresholds[r]) for r in roles]
        # ``user_daily_cost.day`` is stored as TEXT (YYYY-MM-DD); compare on
        # the same representation. Roles/thresholds flow in as bound params
        # via unnest, eliminating the previous f-string interpolation.
        sql = """
            WITH thresholds(role, threshold) AS (
                SELECT * FROM unnest($1::text[], $2::numeric[])
            )
            SELECT u.id AS user_id, u.role AS role,
                   COALESCE(udc.cost_usd, 0)::float AS daily_cost
            FROM users u
            JOIN thresholds t ON t.role = u.role
            LEFT JOIN user_daily_cost udc
              ON udc.user_id = u.id
             AND udc.day = to_char(NOW() AT TIME ZONE 'UTC', 'YYYY-MM-DD')
            WHERE COALESCE(udc.cost_usd, 0) > t.threshold
            ORDER BY daily_cost DESC
            LIMIT 100
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(sql, roles, values)
        return [(r["user_id"], r["role"], float(r["daily_cost"])) for r in rows]

    async def get_user_cost_period(
        self,
        user_id: str,
        period: Literal["today", "month"],
    ) -> float:
        """Return cost for a period from the counter table."""
        if period == "today":
            return await self.get_user_cost_today(user_id)
        # month: sum all days in current UTC month
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT COALESCE(SUM(cost_usd), 0) as total FROM user_daily_cost "
                "WHERE user_id = $1 "
                "AND day >= to_char(date_trunc('month', NOW() AT TIME ZONE 'UTC'), 'YYYY-MM-DD')",
                user_id,
            )
        return float(row["total"]) if row else 0.0

    async def get_user_cost_history(
        self,
        user_id: str,
        days: int = 7,
    ) -> list[Row]:
        """Return up to ``days`` of daily cost rows for ``user_id``.

        Output: list of {"day": str (ISO date YYYY-MM-DD), "cost_usd": Decimal,
        "requests": int}, ordered by day ascending. Days with zero activity
        are NOT included — caller fills gaps if needed.
        """
        if days <= 0:
            return []
        # ``day`` is stored as TEXT (YYYY-MM-DD) — compute cutoff in Python.
        from datetime import datetime, timedelta, timezone

        cutoff = (datetime.now(timezone.utc).date() - timedelta(days=days - 1)).isoformat()
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT day, cost_usd, requests "
                "FROM user_daily_cost "
                "WHERE user_id = $1 "
                "  AND day >= $2 "
                "ORDER BY day ASC",
                user_id,
                cutoff,
            )
        return [
            {
                "day": r["day"].isoformat() if hasattr(r["day"], "isoformat") else r["day"],
                "cost_usd": r["cost_usd"],
                "requests": r["requests"],
            }
            for r in rows
        ]

    async def get_bulk_user_cost_history(
        self,
        user_ids: list[str],
        days: int = 7,
    ) -> dict[str, list[Row]]:
        """Bulk variant of get_user_cost_history — one query, grouped by user."""
        if not user_ids or days <= 0:
            return {}
        from datetime import datetime, timedelta, timezone

        cutoff = (datetime.now(timezone.utc).date() - timedelta(days=days - 1)).isoformat()
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT user_id, day, cost_usd, requests "
                "FROM user_daily_cost "
                "WHERE user_id = ANY($1::text[]) "
                "  AND day >= $2 "
                "ORDER BY user_id, day ASC",
                user_ids,
                cutoff,
            )
        out: dict[str, list[Row]] = {uid: [] for uid in user_ids}
        for r in rows:
            out[r["user_id"]].append(
                {
                    "day": r["day"].isoformat() if hasattr(r["day"], "isoformat") else r["day"],
                    "cost_usd": r["cost_usd"],
                    "requests": r["requests"],
                }
            )
        return out

    async def get_users_summary(
        self,
        *,
        top_n: int = 5,
        anomaly_multiplier: float = 5.0,
        anomaly_min_today: Decimal | None = None,
        anomaly_min_history_days: int = 3,
        near_quota_pct: float = 0.80,
    ) -> Row:
        """Aggregate stats for the 4 dashboard summary cards.

        Returns a dict with keys: pending, top_spenders_today, anomalies,
        near_quota. Each value is {"count": int, "top": [SummaryUserItem-like]}.

        Anomaly rule: status='active' AND days_with_history>=N
            AND today_cost >= floor AND today_cost >= multiplier*avg_prior_7d.

        Near quota: any active user whose today_cost >= near_quota_pct
        of their key's quota_daily_cost_usd. Users without quota set are
        excluded.
        """
        from datetime import datetime, timedelta, timezone
        from decimal import Decimal as _Decimal

        if anomaly_min_today is None:
            anomaly_min_today = _Decimal("1.00")

        today_str = datetime.now(timezone.utc).date().isoformat()
        prior_7d_start = (datetime.now(timezone.utc).date() - timedelta(days=7)).isoformat()
        prior_7d_end = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()

        async with self._pool.acquire() as conn:
            # 1. Pending count + top
            pending_rows = await conn.fetch(
                "SELECT id, email, user_name, role, created_at "
                "FROM users WHERE status = 'pending_approval' "
                "ORDER BY created_at DESC LIMIT $1",
                top_n,
            )
            pending_count_row = await conn.fetchrow(
                "SELECT COUNT(*) AS c FROM users WHERE status = 'pending_approval'"
            )

            # 2. Today / 7d-avg per user (active users only).
            #    ``user_daily_cost.day`` is TEXT (YYYY-MM-DD) in this schema.
            usage_rows = await conn.fetch(
                """
                WITH today_costs AS (
                    SELECT user_id, COALESCE(SUM(cost_usd), 0) AS today_cost
                    FROM user_daily_cost
                    WHERE day = $1
                    GROUP BY user_id
                ),
                prior_7d AS (
                    SELECT user_id,
                           COALESCE(SUM(cost_usd), 0) AS total,
                           COUNT(DISTINCT day) AS days_with_history
                    FROM user_daily_cost
                    WHERE day BETWEEN $2 AND $3
                    GROUP BY user_id
                )
                SELECT u.id, u.email, u.user_name, u.role,
                       COALESCE(t.today_cost, 0) AS today_cost,
                       COALESCE(p.total, 0) AS prior_7d_total,
                       COALESCE(p.days_with_history, 0) AS days_with_history,
                       k.quota_daily_cost_usd
                FROM users u
                LEFT JOIN today_costs t ON t.user_id = u.id
                LEFT JOIN prior_7d p ON p.user_id = u.id
                LEFT JOIN api_keys k ON k.account_id = u.id AND k.status = 'active'
                WHERE u.status = 'active'
                """,
                today_str,
                prior_7d_start,
                prior_7d_end,
            )

            top_spenders: list[Row] = []
            anomalies: list[Row] = []
            near_quota: list[Row] = []

            for r in usage_rows:
                today = _Decimal(str(r["today_cost"] or 0))
                prior = _Decimal(str(r["prior_7d_total"] or 0))
                days = int(r["days_with_history"] or 0)
                avg_7d = (prior / days) if days > 0 else _Decimal("0")
                quota = r["quota_daily_cost_usd"]

                base_item = {
                    "id": r["id"],
                    "email": r["email"],
                    "user_name": r["user_name"],
                    "role": r["role"] or "free",
                    "today_cost_usd": today,
                    "avg_prior_7d_usd": avg_7d,
                    "quota_daily_usd": float(quota) if quota else None,
                    "multiplier": None,
                }

                # Top spenders today
                if today > 0:
                    top_spenders.append(dict(base_item))

                # Anomaly
                if (
                    days >= anomaly_min_history_days
                    and today >= anomaly_min_today
                    and avg_7d > 0
                    and today >= _Decimal(str(anomaly_multiplier)) * avg_7d
                ):
                    multiplier = float(today / avg_7d) if avg_7d > 0 else None
                    anomaly_item = dict(base_item)
                    anomaly_item["multiplier"] = multiplier
                    anomalies.append(anomaly_item)

                # Near / over quota
                if quota and float(quota) > 0:
                    pct = float(today) / float(quota)
                    if pct >= near_quota_pct:
                        near_quota.append(dict(base_item))

            top_spenders.sort(key=lambda x: x["today_cost_usd"], reverse=True)
            anomalies.sort(key=lambda x: x["multiplier"] or 0, reverse=True)
            near_quota.sort(
                key=lambda x: (
                    float(x["today_cost_usd"]) / x["quota_daily_usd"] if x["quota_daily_usd"] else 0
                ),
                reverse=True,
            )

        return {
            "pending": {
                "count": pending_count_row["c"] if pending_count_row else 0,
                "top": [
                    {
                        "id": r["id"],
                        "email": r["email"],
                        "user_name": r["user_name"],
                        "role": r["role"] or "free",
                        "today_cost_usd": _Decimal("0"),
                        "avg_prior_7d_usd": _Decimal("0"),
                        "quota_daily_usd": None,
                        "multiplier": None,
                    }
                    for r in pending_rows
                ],
            },
            "top_spenders_today": {
                "count": len([s for s in top_spenders if s["today_cost_usd"] > 0]),
                "top": top_spenders[:top_n],
            },
            "anomalies": {
                "count": len(anomalies),
                "top": anomalies[:top_n],
            },
            "near_quota": {
                "count": len(near_quota),
                "top": near_quota[:top_n],
            },
        }

    async def get_batch_usage(
        self,
        user_ids: list[str],
        period: Literal["today", "month"],
    ) -> dict[str, float]:
        """Return {user_id: cost_usd} for a batch of users."""
        if not user_ids:
            return {}
        if period == "today":
            date_filter = "day = to_char(NOW() AT TIME ZONE 'UTC', 'YYYY-MM-DD')"
        else:
            date_filter = (
                "day >= to_char(date_trunc('month', NOW() AT TIME ZONE 'UTC'), 'YYYY-MM-DD')"
            )
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT user_id, COALESCE(SUM(cost_usd), 0) as cost "
                f"FROM user_daily_cost "
                f"WHERE {date_filter} AND user_id = ANY($1::text[]) "
                f"GROUP BY user_id",
                user_ids,
            )
        return {r["user_id"]: float(r["cost"]) for r in rows}
