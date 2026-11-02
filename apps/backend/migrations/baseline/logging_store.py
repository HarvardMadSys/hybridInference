"""The tables ``DatabaseLogger`` built at startup, frozen.

Copied from ``serving.storage.database.DatabaseLogger._create_tables``. It ran
first and so created the auth tables the operational store also defines
(:mod:`migrations.baseline.operational_store`); both copies run, in that order.
Left out: the erasure-fence fingerprint check and the ``ADMIN_EMAILS`` role
seeding, which read runtime configuration and stay at startup.
"""

from __future__ import annotations

import logging

import asyncpg

from migrations.baseline.api_logs import ensure_api_logs_schema
from migrations.baseline.audit_log import ensure_admin_audit_log_schema
from migrations.baseline.browser_sessions import ensure_browser_session_tables
from migrations.baseline.ddl import (
    apply_column_migrations,
    bounded_ddl,
    column_metadata,
    constraint_admitted_values,
    constraint_definition,
    drop_columns_if_present,
    execute_ddl,
    parse_command_tag_count as _parse_command_tag_count,
)
from migrations.baseline.geo_demand import ensure_geo_rollup_schema
from migrations.baseline.public_stats import ensure_public_stats_schema

logger = logging.getLogger(__name__)


async def create_tables(conn: asyncpg.Connection) -> None:
    """Create tables and indexes if they do not exist."""
    # api_logs + api_stats_hourly (table, migrations, indexes) live in a
    # single shared module so this boot-time builder and the runtime
    # PostgresLogStore.initialize cannot drift. Add future api_logs
    # schema changes in serving.storage.log_schema, never here.
    await ensure_api_logs_schema(conn)

    # API Keys table for user authentication
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS api_keys (
            id BIGSERIAL PRIMARY KEY,
            key_hash TEXT NOT NULL UNIQUE,
            api_key_encrypted TEXT,
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

    # Indexes for api_keys
    await conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_api_keys_user
        ON api_keys(user_id)
    """)

    await conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_api_keys_status
        ON api_keys(status, expires_at)
    """)

    await conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_api_keys_prefix_unique
        ON api_keys(key_prefix)
    """)

    # Enforce 1:1 user-to-key relationship (one active key per user)
    await conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_api_keys_user_unique
        ON api_keys(user_id) WHERE status = 'active'
    """)

    # Migrations for api_keys table (from token-based to cost-based
    # quotas). Catalog-gated (serving.storage.log_schema): ALTER TABLE
    # takes ACCESS EXCLUSIVE before evaluating IF NOT EXISTS, and this
    # exact bare cluster queued behind the nightly pg_dump and took
    # production down on 2026-08-21.
    await apply_column_migrations(
        conn,
        "api_keys",
        [
            (
                "quota_daily_cost_usd",
                "ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS "
                "quota_daily_cost_usd DECIMAL(10, 4) DEFAULT 1000.00",
            ),
            (
                "quota_monthly_cost_usd",
                "ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS "
                "quota_monthly_cost_usd DECIMAL(10, 4)",
            ),
            (
                "api_key_encrypted",
                "ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS api_key_encrypted TEXT",
            ),
            # Links API keys to user accounts (self-registered users only).
            (
                "account_id",
                "ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS account_id TEXT",
            ),
        ],
    )

    await conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_api_keys_account
        ON api_keys(account_id)
    """)

    # Prevent concurrent duplicate active keys per account (self-registered users)
    await conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_api_keys_account_active_unique
        ON api_keys(account_id)
        WHERE status = 'active' AND account_id IS NOT NULL
    """)

    await drop_columns_if_present(
        conn, "api_keys", [("tier", "ALTER TABLE api_keys DROP COLUMN IF EXISTS tier")]
    )

    # Users table for self-service registration. ``email`` admits NULL
    # for the account created without an address (the first-run setup
    # administrator), which signs in with ``login_name``; keep in step
    # with the copy in ``postgres_operational.py``.
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            email TEXT UNIQUE,
            login_name TEXT,
            password_hash TEXT,
            phone TEXT,
            phone_verified BOOLEAN NOT NULL DEFAULT FALSE,
            user_name TEXT,
            preferences JSONB NOT NULL DEFAULT '{}'::jsonb,
            role TEXT NOT NULL DEFAULT 'free'
                CHECK (role IN ('free', 'pro', 'internal', 'admin')),
            email_verified BOOLEAN DEFAULT FALSE,
            status TEXT DEFAULT 'active'
                CHECK (status IN ('active', 'suspended', 'deleted', 'pending_approval', 'rejected')),
            approval_note TEXT,
            reviewed_at TIMESTAMPTZ,
            reviewed_by TEXT,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            last_login_at TIMESTAMPTZ
        )
    """)

    await conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_users_email
        ON users(email)
    """)

    await conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_users_status
        ON users(status)
    """)

    await conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_users_created_at
        ON users(created_at DESC)
    """)

    # Migrations: approval-based registration columns (catalog-gated —
    # users is on auth's read path, same doctrine as api_keys above).
    await apply_column_migrations(
        conn,
        "users",
        [
            (
                "approval_note",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS approval_note TEXT",
            ),
            (
                "reviewed_at",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS reviewed_at TIMESTAMPTZ",
            ),
            (
                "reviewed_by",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS reviewed_by TEXT",
            ),
            (
                "preferences",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS "
                "preferences JSONB NOT NULL DEFAULT '{}'::jsonb",
            ),
            (
                "login_name",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS login_name TEXT",
            ),
        ],
    )

    # Databases created before email-less accounts have email NOT NULL;
    # DROP NOT NULL takes ACCESS EXCLUSIVE, so gate it on the catalog.
    email_meta = await column_metadata(conn, "users", "email")
    if email_meta is not None and email_meta["not_null"]:
        async with bounded_ddl(conn):
            await execute_ddl(conn, "ALTER TABLE users ALTER COLUMN email DROP NOT NULL")

    await conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_users_login_name
        ON users (lower(login_name)) WHERE login_name IS NOT NULL
    """)

    # Expand status CHECK constraint to include pending_approval and rejected.
    # Rebuild inside a transaction so a failed ADD does not leave the table
    # without its previous integrity constraint. Gated on the current
    # constraint definition: the rebuild takes ACCESS EXCLUSIVE, so the
    # settled path reads the catalog and walks away. Settled means the
    # admitted set matches exactly — see constraint_admitted_values.
    status_def = await constraint_definition(conn, "users", "users_status_check")
    status_settled = constraint_admitted_values(status_def) == {
        "active",
        "suspended",
        "deleted",
        "pending_approval",
        "rejected",
    }
    try:
        if not status_settled:
            async with bounded_ddl(conn), conn.transaction():
                await execute_ddl(
                    conn,
                    """
                ALTER TABLE users DROP CONSTRAINT IF EXISTS users_status_check
            """,
                )
                await execute_ddl(
                    conn,
                    """
                ALTER TABLE users
                ADD CONSTRAINT users_status_check
                CHECK (status IN ('active', 'suspended', 'deleted', 'pending_approval', 'rejected'))
            """,
                )
    except asyncpg.PostgresError as exc:
        invalid_status_rows = await conn.fetch("""
            SELECT id, email, status
            FROM users
            WHERE status NOT IN ('active', 'suspended', 'deleted', 'pending_approval', 'rejected')
            ORDER BY created_at DESC
            LIMIT 10
        """)
        logger.error(
            "Failed to rebuild users_status_check; transaction rolled back. "
            "Sample invalid rows=%s error=%s",
            [dict(row) for row in invalid_status_rows],
            exc,
        )
        raise

    await conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_users_pending_approval
        ON users(created_at DESC) WHERE status = 'pending_approval'
    """)

    # Add role column for permission levels (free/pro/internal/admin).
    # Each strong-lock step gated on the catalog.
    await apply_column_migrations(
        conn,
        "users",
        [("role", "ALTER TABLE users ADD COLUMN IF NOT EXISTS role TEXT")],
    )
    role_meta = await column_metadata(conn, "users", "role")
    role_default = str(role_meta["default_expr"] or "") if role_meta else ""
    if "'free'" not in role_default:
        async with bounded_ddl(conn):
            await execute_ddl(conn, "ALTER TABLE users ALTER COLUMN role SET DEFAULT 'free'")

    updated_roles_tag = await conn.execute("""
        UPDATE users
        SET role = 'free'
        WHERE role IS NULL
    """)
    updated_roles = _parse_command_tag_count(updated_roles_tag)
    if updated_roles:
        logger.info(
            "Backfilled default user role for %d existing rows.",
            updated_roles,
        )

    if role_meta is None or not role_meta["not_null"]:
        async with bounded_ddl(conn):
            await execute_ddl(conn, "ALTER TABLE users ALTER COLUMN role SET NOT NULL")

    # Migrate legacy roles and rebuild users_role_check to the current
    # allowed set (free, pro, internal, admin). The constraint
    # must be dropped BEFORE the UPDATE — older DBs may have CHECK
    # constraints that reject 'internal'. Settled requires the admitted
    # set to match exactly: the historical five-member constraint
    # ('trial', 'free', 'pro', 'internal', 'admin') contains all four
    # current roles, and a membership test would skip the trial->free
    # row migration below while trial rows still exist under it.
    role_def = await constraint_definition(conn, "users", "users_role_check")
    role_settled = constraint_admitted_values(role_def) == {
        "free",
        "pro",
        "internal",
        "admin",
    }
    try:
        if not role_settled:
            async with bounded_ddl(conn), conn.transaction():
                await execute_ddl(
                    conn,
                    """
                ALTER TABLE users DROP CONSTRAINT IF EXISTS users_role_check
            """,
                )
                migrated_roles_tag = await conn.execute("""
                UPDATE users
                SET role = 'internal'
                WHERE role IN ('internal_group', 'developer')
            """)
                migrated_roles = _parse_command_tag_count(migrated_roles_tag)
                if migrated_roles:
                    logger.info(
                        "Migrated %d users from internal_group/developer to internal.",
                        migrated_roles,
                    )
                migrated_trial_tag = await conn.execute("""
                UPDATE users
                SET role = 'free'
                WHERE role = 'trial'
            """)
                migrated_trial = _parse_command_tag_count(migrated_trial_tag)
                if migrated_trial:
                    logger.info(
                        "Migrated %d users from trial to free.",
                        migrated_trial,
                    )
                await execute_ddl(
                    conn,
                    """
                ALTER TABLE users
                ADD CONSTRAINT users_role_check
                CHECK (role IN ('free', 'pro', 'internal', 'admin'))
            """,
                )
    except asyncpg.PostgresError as exc:
        invalid_role_rows = await conn.fetch("""
            SELECT id, email, role
            FROM users
            WHERE role NOT IN ('free', 'pro', 'internal', 'admin')
            ORDER BY created_at DESC
            LIMIT 10
        """)
        logger.error(
            "Failed to rebuild users_role_check; transaction rolled back. "
            "Sample invalid rows=%s error=%s",
            [dict(row) for row in invalid_role_rows],
            exc,
        )
        raise

    # Backfill account_id = user_id for legacy self-registered keys
    # created before the account_id column existed (account_id IS NULL).
    # Makes those keys visible/manageable in the dashboard and reconciles
    # idx_api_keys_user_unique (user_id) with
    # idx_api_keys_account_active_unique (account_id). Idempotent: only
    # NULL rows whose user still exists are touched. Runs here (not next
    # to the account_id column above) because api_keys is created before
    # the users table in this method.
    backfilled_account_ids_tag = await conn.execute("""
        UPDATE api_keys k
        SET account_id = k.user_id
        WHERE k.account_id IS NULL
          AND EXISTS (SELECT 1 FROM users u WHERE u.id = k.user_id)
    """)
    backfilled_account_ids = _parse_command_tag_count(backfilled_account_ids_tag)
    if backfilled_account_ids:
        logger.info(
            "Backfilled account_id for %d legacy api_keys rows.",
            backfilled_account_ids,
        )

    # Auth sessions table for refresh token management
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

    await conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_auth_sessions_user
        ON auth_sessions(user_id, expires_at)
    """)

    await conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_auth_sessions_token
        ON auth_sessions(refresh_token_hash) WHERE NOT revoked
    """)

    await conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_auth_sessions_jti
        ON auth_sessions(jti)
    """)

    await ensure_browser_session_tables(conn)

    # Email verification tokens table
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS email_verification_tokens (
            token TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            expires_at TIMESTAMPTZ NOT NULL,
            used_at TIMESTAMPTZ
        )
    """)

    await conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_email_verification_user
        ON email_verification_tokens(user_id)
    """)

    await conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_email_verification_expires
        ON email_verification_tokens(expires_at)
    """)

    # Password reset tokens table
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS password_reset_tokens (
            token TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            expires_at TIMESTAMPTZ NOT NULL,
            used_at TIMESTAMPTZ
        )
    """)

    await conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_password_reset_user
        ON password_reset_tokens(user_id)
    """)

    await conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_password_reset_expires
        ON password_reset_tokens(expires_at)
    """)

    # Admin audit log: table, indexes and attribution columns, shared
    # with PostgresOperationalStore (serving.storage.audit_schema). A
    # lock timeout here is deferred like this builder's other
    # migrations; the operational store's pass, which runs next on the
    # same database, is the one that refuses to serve without them.
    await ensure_admin_audit_log_schema(conn)

    # Signup domain allowlist (admin-editable approval policy).
    # Empty table = all signups auto-approve; non-empty table requires
    # the signup email's domain to match (exact or wildcard suffix).
    # ``created_by`` uses ON DELETE SET NULL so hard-deleting a user
    # who once added an allowlist entry doesn't fail with a FK
    # violation; the audit value is informational only.
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
    # ON DELETE SET NULL, which blocks hard_delete_user. Rebuild
    # the constraint in place. The auto-generated constraint name
    # is ``signup_allowed_domains_created_by_fkey``.
    fk_def = await constraint_definition(
        conn, "signup_allowed_domains", "signup_allowed_domains_created_by_fkey"
    )
    try:
        if fk_def is None or "ON DELETE SET NULL" not in fk_def:
            async with bounded_ddl(conn), conn.transaction():
                await execute_ddl(
                    conn,
                    """
                ALTER TABLE signup_allowed_domains
                DROP CONSTRAINT IF EXISTS signup_allowed_domains_created_by_fkey
            """,
                )
                await execute_ddl(
                    conn,
                    """
                ALTER TABLE signup_allowed_domains
                ADD CONSTRAINT signup_allowed_domains_created_by_fkey
                FOREIGN KEY (created_by) REFERENCES users(id) ON DELETE SET NULL
            """,
                )
    except asyncpg.PostgresError as exc:
        logger.error(
            "Failed to rebuild signup_allowed_domains_created_by_fkey "
            "with ON DELETE SET NULL; transaction rolled back. error=%s",
            exc,
        )
        raise

    # Sort by last_login in admin user list (DESC NULLS LAST)
    await conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_users_last_login_at
        ON users(last_login_at DESC NULLS LAST)
    """)

    # Broadcast email tables
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS email_broadcasts (
            id TEXT PRIMARY KEY,
            subject TEXT NOT NULL,
            body_html TEXT NOT NULL,
            body_text TEXT NOT NULL,
            template_key TEXT,
            template_vars JSONB NOT NULL DEFAULT '{}',
            target_roles TEXT[] NOT NULL DEFAULT '{}',
            target_statuses TEXT[] NOT NULL DEFAULT '{}',
            recipient_count INT NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'scheduled'
                CHECK (status IN ('scheduled','sending','sent','failed','cancelled')),
            scheduled_at TIMESTAMPTZ,
            created_by TEXT NOT NULL,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            sent_at TIMESTAMPTZ
        )
    """)

    await conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_email_broadcasts_status_scheduled
        ON email_broadcasts(status, scheduled_at)
        WHERE status = 'scheduled'
    """)

    await conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_email_broadcasts_created_at
        ON email_broadcasts(created_at DESC)
    """)

    await conn.execute("""
        CREATE TABLE IF NOT EXISTS email_broadcast_recipients (
            id BIGSERIAL PRIMARY KEY,
            broadcast_id TEXT NOT NULL REFERENCES email_broadcasts(id),
            user_id TEXT NOT NULL,
            email TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending','sent','failed')),
            error TEXT,
            sent_at TIMESTAMPTZ,
            UNIQUE (broadcast_id, user_id)
        )
    """)
    # Backfill the unique constraint on existing tables (no-op if it
    # already exists or if duplicates would prevent it). Gated on the
    # catalog so the settled path takes no lock at all.
    uniq_def = await constraint_definition(
        conn,
        "email_broadcast_recipients",
        "email_broadcast_recipients_broadcast_user_uniq",
    )
    if uniq_def is None:
        async with bounded_ddl(conn):
            await execute_ddl(
                conn,
                """
        DO $$
        BEGIN
            BEGIN
                ALTER TABLE email_broadcast_recipients
                    ADD CONSTRAINT email_broadcast_recipients_broadcast_user_uniq
                    UNIQUE (broadcast_id, user_id);
            EXCEPTION
                WHEN duplicate_object THEN NULL;
                WHEN duplicate_table THEN NULL;
            END;
        END $$;
    """,
            )

    await conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_broadcast_recipients_broadcast
        ON email_broadcast_recipients(broadcast_id)
    """)

    await conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_broadcast_recipients_status
        ON email_broadcast_recipients(broadcast_id, status)
    """)

    # ====================================================
    # site_updates — admin-managed homepage announcements.
    # ``placement='feed'`` rows render in the chronological
    # Updates section; ``placement='banner'`` rows surface as
    # the single dismissible notice at the top of the homepage
    # (only the most recent published banner is shown).
    # ====================================================
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS site_updates (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            body TEXT NOT NULL DEFAULT '',
            placement TEXT NOT NULL DEFAULT 'feed'
                CHECK (placement IN ('feed','banner')),
            published BOOLEAN NOT NULL DEFAULT TRUE,
            link_url TEXT,
            link_label TEXT,
            created_by TEXT NOT NULL,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    # Public homepage read filters on (published, placement) and
    # orders by created_at DESC — index covers both query shapes.
    await conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_site_updates_published
        ON site_updates(published, placement, created_at DESC)
    """)

    # ====================================================
    # provider_hourly_stats — hourly rollup of api_logs by
    # (provider, model_id). Populated by the
    # rollup_provider_stats APScheduler job.
    # ====================================================
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS provider_hourly_stats (
            hour_bucket             TIMESTAMPTZ NOT NULL,
            provider                TEXT        NOT NULL,
            model_id                TEXT        NOT NULL,

            request_count           INTEGER     NOT NULL,
            error_count             INTEGER     NOT NULL,
            stream_count            INTEGER     NOT NULL,

            ttft_p50_ms             INTEGER,
            ttft_p95_ms             INTEGER,
            ttft_p99_ms             INTEGER,

            latency_p50_ms          INTEGER,
            latency_p95_ms          INTEGER,
            latency_p99_ms          INTEGER,

            throughput_avg_tps      FLOAT,
            throughput_p50_tps      FLOAT,
            throughput_p95_tps      FLOAT,

            prompt_tokens_avg       FLOAT,
            completion_tokens_avg   FLOAT,
            total_completion_tokens BIGINT      NOT NULL,

            total_prompt_tokens     BIGINT,
            total_cache_read_tokens BIGINT,
            total_reasoning_tokens  BIGINT,
            total_cost_usd          DECIMAL(14, 8),

            PRIMARY KEY (provider, model_id, hour_bucket)
        )
    """)

    await conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_phs_hour
        ON provider_hourly_stats(hour_bucket DESC)
    """)

    await conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_phs_provider_hour
        ON provider_hourly_stats(provider, hour_bucket DESC)
    """)

    # Migration: token totals + cost on provider_hourly_stats.
    # Added for the Analytics overview's token usage. Nullable so the
    # change is metadata-only on existing tables; rows pre-dating
    # this migration are filled by backfill_token_columns at
    # startup and by subsequent hourly rollups.
    await apply_column_migrations(
        conn,
        "provider_hourly_stats",
        [
            (
                "total_prompt_tokens",
                "ALTER TABLE provider_hourly_stats "
                "ADD COLUMN IF NOT EXISTS total_prompt_tokens BIGINT",
            ),
            (
                "total_cache_read_tokens",
                "ALTER TABLE provider_hourly_stats "
                "ADD COLUMN IF NOT EXISTS total_cache_read_tokens BIGINT",
            ),
            (
                "total_reasoning_tokens",
                "ALTER TABLE provider_hourly_stats "
                "ADD COLUMN IF NOT EXISTS total_reasoning_tokens BIGINT",
            ),
            (
                "total_cost_usd",
                "ALTER TABLE provider_hourly_stats "
                "ADD COLUMN IF NOT EXISTS total_cost_usd DECIMAL(14, 8)",
            ),
        ],
    )

    # provider_hourly_coverage — one row per hour the provider rollup
    # has aggregated, quiet hours included, written with that hour's
    # provider_hourly_stats rows. provider_hourly_stats has no row for
    # an hour without traffic, so only this tells a quiet hour from a
    # missed run; serving.admin.provider_stats_rollup rolls up the hours
    # that lack one. The first rollup marks hours that already have
    # rows, so for those completed_at is when coverage began.
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS provider_hourly_coverage (
            hour_bucket  TIMESTAMPTZ PRIMARY KEY,
            completed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)

    # Privacy-safe country/hour aggregates and their explicit
    # completed-hour coverage markers for the admin geo dashboard.
    await ensure_geo_rollup_schema(conn)

    # Daily aggregate snapshots served by GET /public-stats.
    await ensure_public_stats_schema(conn)
