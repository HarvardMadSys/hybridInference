"""PostgreSQL-backed request/metrics logger using asyncpg.

This module provides a simple database logger that writes API requests,
responses, and usage metrics into PostgreSQL tables. It is intended for
production or staging environments where PostgreSQL is available.

Pure utility functions (``calculate_cost``, ``compute_prompt_hash``, etc.)
have been moved to ``serving.storage.utils`` so they can be imported without
pulling in asyncpg. They are re-exported here for backward compatibility.
"""

import json
import os
from typing import Any

import asyncpg

from serving.storage.utils import (
    calculate_cost,
    compute_prompt_hash,
    compute_prompt_hash_chunked,
)
from serving.utils.logging import get_logger

logger = get_logger(__name__)


def _parse_admin_emails(raw: str) -> list[str]:
    """Return normalized admin email addresses from a comma-separated env var."""
    return [email.strip().lower() for email in raw.split(",") if email.strip()]


def _parse_command_tag_count(command_tag: str) -> int:
    """Extract the affected row count from an asyncpg command tag."""
    parts = command_tag.split()
    if not parts:
        return 0
    try:
        return int(parts[-1])
    except ValueError:
        return 0


class DatabaseLogger:
    """Asynchronous PostgreSQL logger using a pooled connection."""

    def __init__(
        self,
        db_config: dict[str, str],
        store_full_prompts: bool = True,
        use_chunked_hash: bool = False,
    ):
        """Initialize the logger with a DSN/config mapping.

        Args:
            db_config: Mapping with asyncpg pool connection arguments.
            store_full_prompts: If False, only store prompt_hash for privacy.
                When disabled, prompt and response fields will be NULL and only
                hashes are stored for analytics.
            use_chunked_hash: If True, use 4-token chunked hashing instead of
                full prompt hashing. Both methods provide equivalent privacy
                protection but chunked hashing may enable future optimizations.
        """
        self.db_config = db_config
        self.store_full_prompts = store_full_prompts
        self.use_chunked_hash = use_chunked_hash
        # Use Any to avoid mypy issues when asyncpg types are unavailable.
        self.pool: Any | None = None

    async def initialize(self) -> None:
        """Create the connection pool and ensure tables exist."""
        self.pool = await asyncpg.create_pool(
            **self.db_config, min_size=2, max_size=10, command_timeout=60
        )
        await self._create_tables()

    async def _create_tables(self) -> None:
        """Create tables and indexes if they do not exist."""
        if self.pool is None:
            raise RuntimeError("DatabaseLogger not initialized")
        async with self.pool.acquire() as conn:
            # Main logs table
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS api_logs (
                    id BIGSERIAL PRIMARY KEY,
                    timestamp TIMESTAMPTZ DEFAULT NOW(),
                    request_id TEXT NOT NULL UNIQUE,
                    model_id TEXT NOT NULL,
                    provider TEXT NOT NULL,

                    -- Request parameters
                    temperature FLOAT,
                    top_p FLOAT,
                    max_tokens INTEGER,
                    seed INTEGER,
                    stream BOOLEAN,

                    -- Performance metrics
                    ttft_ms INTEGER,
                    latency_ms INTEGER,

                    -- Token usage (optional)
                    prompt_tokens INTEGER,
                    completion_tokens INTEGER,
                    reasoning_tokens INTEGER,
                    total_tokens INTEGER,

                    -- Request/response content
                    prompt TEXT,
                    response TEXT,
                    prompt_hash TEXT,
                    response_hash TEXT,

                    -- Additional metadata (kept for compatibility)
                    status_code INTEGER,
                    error TEXT,
                    user_id TEXT,
                    session_id TEXT,
                    metadata JSONB,
                    tools JSONB
                )
            """)

            # Indexes for performance
            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_api_logs_timestamp
                ON api_logs(timestamp DESC)
            """)

            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_api_logs_model
                ON api_logs(model_id, timestamp DESC)
            """)

            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_api_logs_provider
                ON api_logs(provider, timestamp DESC)
            """)

            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_api_logs_request_id
                ON api_logs(request_id)
            """)

            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_api_logs_user
                ON api_logs(user_id, timestamp DESC)
                WHERE user_id IS NOT NULL
            """)

            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_api_logs_session
                ON api_logs(session_id, timestamp DESC)
                WHERE session_id IS NOT NULL
            """)

            # Covers the model-activity aggregation query which filters by
            # recent timestamp window + real users, then groups by model/provider.
            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_api_logs_model_activity
                ON api_logs(timestamp DESC, model_id, provider)
                WHERE user_id IS NOT NULL
            """)

            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_api_logs_error
                ON api_logs(timestamp DESC)
                WHERE error IS NOT NULL
            """)

            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_api_logs_prompt_hash
                ON api_logs(prompt_hash)
                WHERE prompt_hash IS NOT NULL
            """)

            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_api_logs_response_hash
                ON api_logs(response_hash)
                WHERE response_hash IS NOT NULL
            """)

            # Migrations for existing databases
            await conn.execute("""
                ALTER TABLE api_logs
                ADD COLUMN IF NOT EXISTS reasoning_tokens INTEGER
            """)

            await conn.execute("""
                ALTER TABLE api_logs
                ADD COLUMN IF NOT EXISTS stream BOOLEAN
            """)

            await conn.execute("""
                ALTER TABLE api_logs
                ADD COLUMN IF NOT EXISTS ttft_ms INTEGER
            """)

            await conn.execute("""
                ALTER TABLE api_logs
                ADD COLUMN IF NOT EXISTS prompt_hash TEXT
            """)

            await conn.execute("""
                ALTER TABLE api_logs
                ADD COLUMN IF NOT EXISTS response_hash TEXT
            """)

            await conn.execute("""
                ALTER TABLE api_logs
                ADD COLUMN IF NOT EXISTS cache_read_tokens INTEGER
            """)

            await conn.execute("""
                ALTER TABLE api_logs
                ADD COLUMN IF NOT EXISTS cache_write_tokens INTEGER
            """)

            await conn.execute("""
                ALTER TABLE api_logs
                ADD COLUMN IF NOT EXISTS cost_usd DECIMAL(12, 8)
            """)

            await conn.execute("""
                ALTER TABLE api_logs
                ADD COLUMN IF NOT EXISTS upstream_cost_usd DECIMAL(12, 8)
            """)

            # Aggregated stats table
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS api_stats_hourly (
                    hour TIMESTAMPTZ NOT NULL,
                    model_id TEXT NOT NULL,
                    provider TEXT NOT NULL,

                    request_count INTEGER DEFAULT 0,
                    success_count INTEGER DEFAULT 0,
                    error_count INTEGER DEFAULT 0,

                    total_prompt_tokens BIGINT DEFAULT 0,
                    total_completion_tokens BIGINT DEFAULT 0,
                    total_tokens BIGINT DEFAULT 0,

                    avg_latency_ms FLOAT,
                    p50_latency_ms INTEGER,
                    p95_latency_ms INTEGER,
                    p99_latency_ms INTEGER,
                    PRIMARY KEY (hour, model_id, provider)
                )
            """)

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

            # Migrations for api_keys table (from token-based to cost-based quotas)
            await conn.execute("""
                ALTER TABLE api_keys
                ADD COLUMN IF NOT EXISTS quota_daily_cost_usd DECIMAL(10, 4) DEFAULT 1000.00
            """)

            await conn.execute("""
                ALTER TABLE api_keys
                ADD COLUMN IF NOT EXISTS quota_monthly_cost_usd DECIMAL(10, 4)
            """)

            await conn.execute("""
                ALTER TABLE api_keys
                ADD COLUMN IF NOT EXISTS api_key_encrypted TEXT
            """)

            # Add account_id column to link API keys to user accounts (self-registered users only)
            await conn.execute("""
                ALTER TABLE api_keys
                ADD COLUMN IF NOT EXISTS account_id TEXT
            """)

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

            await conn.execute("""
                ALTER TABLE api_keys DROP COLUMN IF EXISTS tier
            """)

            # Users table for self-service registration
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

            # Migrations: approval-based registration columns
            await conn.execute("""
                ALTER TABLE users
                ADD COLUMN IF NOT EXISTS approval_note TEXT
            """)

            await conn.execute("""
                ALTER TABLE users
                ADD COLUMN IF NOT EXISTS reviewed_at TIMESTAMPTZ
            """)

            await conn.execute("""
                ALTER TABLE users
                ADD COLUMN IF NOT EXISTS reviewed_by TEXT
            """)

            await conn.execute("""
                ALTER TABLE users
                ADD COLUMN IF NOT EXISTS preferences JSONB NOT NULL DEFAULT '{}'::jsonb
            """)

            # Expand status CHECK constraint to include pending_approval and rejected.
            # Rebuild inside a transaction so a failed ADD does not leave the table
            # without its previous integrity constraint.
            try:
                async with conn.transaction():
                    await conn.execute("""
                        ALTER TABLE users DROP CONSTRAINT IF EXISTS users_status_check
                    """)
                    await conn.execute("""
                        ALTER TABLE users
                        ADD CONSTRAINT users_status_check
                        CHECK (status IN ('active', 'suspended', 'deleted', 'pending_approval', 'rejected'))
                    """)
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

            # Add role column for permission levels (free/pro/internal/admin)
            await conn.execute("""
                ALTER TABLE users
                ADD COLUMN IF NOT EXISTS role TEXT
            """)

            await conn.execute("""
                ALTER TABLE users
                ALTER COLUMN role SET DEFAULT 'free'
            """)

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

            await conn.execute("""
                ALTER TABLE users
                ALTER COLUMN role SET NOT NULL
            """)

            # Migrate old 4-role hierarchy to 3-role: internal_group/developer → internal.
            # The constraint must be dropped BEFORE the UPDATE — on an existing DB the
            # old CHECK (role IN ('free','internal_group','developer','admin')) would
            # reject the new 'internal' value.
            try:
                async with conn.transaction():
                    await conn.execute("""
                        ALTER TABLE users DROP CONSTRAINT IF EXISTS users_role_check
                    """)
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
                    await conn.execute("""
                        ALTER TABLE users
                        ADD CONSTRAINT users_role_check
                        CHECK (role IN ('free', 'internal', 'admin'))
                    """)
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

            # Expand the role CHECK constraint to include the "pro" tier.
            # The old constraint allowed (free, internal, admin); the new set
            # adds "pro" so the admin API can assign that role. The constraint
            # must be dropped first because the new value is not in the old set.
            try:
                async with conn.transaction():
                    await conn.execute("""
                        ALTER TABLE users DROP CONSTRAINT IF EXISTS users_role_check
                    """)
                    await conn.execute("""
                        ALTER TABLE users
                        ADD CONSTRAINT users_role_check
                        CHECK (role IN ('free', 'pro', 'internal', 'admin'))
                    """)
            except asyncpg.PostgresError as exc:
                invalid_role_rows = await conn.fetch("""
                    SELECT id, email, role
                    FROM users
                    WHERE role NOT IN ('free', 'pro', 'internal', 'admin')
                    ORDER BY created_at DESC
                    LIMIT 10
                """)
                logger.error(
                    "Failed to expand users_role_check to include pro; "
                    "transaction rolled back. Sample invalid rows=%s error=%s",
                    [dict(row) for row in invalid_role_rows],
                    exc,
                )
                raise

            admin_emails = _parse_admin_emails(os.getenv("ADMIN_EMAILS", ""))
            if admin_emails:
                seeded_admins_tag = await conn.execute(
                    """
                    UPDATE users
                    SET role = 'admin'
                    WHERE lower(trim(email)) = ANY($1::text[])
                      AND role = 'free'
                    """,
                    admin_emails,
                )
                seeded_admins = _parse_command_tag_count(seeded_admins_tag)
                if seeded_admins:
                    logger.info(
                        "Seeded %d admin role assignments from ADMIN_EMAILS during DB initialization.",
                        seeded_admins,
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

            # Admin audit log table for tracking all admin operations
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

            # Indexes for admin audit log
            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_admin_audit_timestamp
                ON admin_audit_log(timestamp DESC)
            """)

            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_admin_audit_user
                ON admin_audit_log(target_user_id, timestamp DESC)
            """)

            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_admin_audit_action
                ON admin_audit_log(action, timestamp DESC)
            """)

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
            try:
                async with conn.transaction():
                    await conn.execute("""
                        ALTER TABLE signup_allowed_domains
                        DROP CONSTRAINT IF EXISTS signup_allowed_domains_created_by_fkey
                    """)
                    await conn.execute("""
                        ALTER TABLE signup_allowed_domains
                        ADD CONSTRAINT signup_allowed_domains_created_by_fkey
                        FOREIGN KEY (created_by) REFERENCES users(id) ON DELETE SET NULL
                    """)
            except asyncpg.PostgresError as exc:
                logger.error(
                    "Failed to rebuild signup_allowed_domains_created_by_fkey "
                    "with ON DELETE SET NULL; transaction rolled back. error=%s",
                    exc,
                )
                raise

            # Critical index for usage analytics (prevents full table scan on cost queries)
            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_api_logs_user_cost
                ON api_logs(user_id, timestamp, cost_usd)
            """)

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
            # already exists or if duplicates would prevent it).
            await conn.execute("""
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
            """)

            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_broadcast_recipients_broadcast
                ON email_broadcast_recipients(broadcast_id)
            """)

            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_broadcast_recipients_status
                ON email_broadcast_recipients(broadcast_id, status)
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
            # Added by per-provider Token Usage tab. Nullable so the
            # change is metadata-only on existing tables; rows pre-dating
            # this migration are filled by backfill_token_columns at
            # startup and by subsequent hourly rollups.
            await conn.execute("""
                ALTER TABLE provider_hourly_stats
                ADD COLUMN IF NOT EXISTS total_prompt_tokens BIGINT
            """)
            await conn.execute("""
                ALTER TABLE provider_hourly_stats
                ADD COLUMN IF NOT EXISTS total_cache_read_tokens BIGINT
            """)
            await conn.execute("""
                ALTER TABLE provider_hourly_stats
                ADD COLUMN IF NOT EXISTS total_reasoning_tokens BIGINT
            """)
            await conn.execute("""
                ALTER TABLE provider_hourly_stats
                ADD COLUMN IF NOT EXISTS total_cost_usd DECIMAL(14, 8)
            """)

    async def log_request(
        self,
        request_id: str,
        model_id: str,
        provider: str,
        prompt: list[dict[str, Any]] | str,
        response: dict[str, Any] | str | None,
        usage: dict[str, int] | None,
        latency_ms: int,
        status_code: int,
        error: str | None = None,
        params: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        ttft_ms: int | None = None,
        prompt_hash: str | None = None,
        response_hash: str | None = None,
        store_full_content: bool | None = None,
        pricing: dict[str, str] | None = None,
        upstream_cost_usd: float | None = None,
    ) -> None:
        """Insert a single request log row.

        Args:
            request_id: Unique request identifier.
            model_id: Logical model identifier.
            provider: Provider name for the request.
            prompt: Request messages payload (list of dicts or string).
            response: Provider response payload (dict, string, or None).
            usage: Token usage breakdown.
            latency_ms: End-to-end latency in milliseconds.
            status_code: HTTP status code returned to client.
            error: Optional error message.
            params: Request parameters (temperature, max_tokens, stream, etc.).
            metadata: Additional metadata (user_id, session_id, etc.).
            ttft_ms: Time to first token in milliseconds.
            prompt_hash: Hash of prompt for deduplication/caching (auto-computed if None).
            response_hash: Hash of response for deduplication/caching (auto-computed if None).
            store_full_content: Override instance default for storing full prompt/response.
                If None, uses self.store_full_prompts. Set False for privacy mode (hash only).
            pricing: Model pricing config for cost calculation (per 1M tokens).
            upstream_cost_usd: OpenRouter-reported per-request upstream cost (USD).
                Internal accounting only — orthogonal to user-billed `cost_usd`.
                None for non-OpenRouter routes.
        """
        if not self.pool:
            raise RuntimeError("DatabaseLogger not initialized")

        # Auto-compute prompt_hash if not provided
        if prompt_hash is None:
            if self.use_chunked_hash:
                prompt_hash = compute_prompt_hash_chunked(prompt)
            else:
                prompt_hash = compute_prompt_hash(prompt)

        # Auto-compute response_hash if not provided
        if response_hash is None and response is not None:
            # Normalize response to string for hashing
            # Use same normalization strategy as prompt for consistency
            response_for_hash = (
                json.dumps(response, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
                if isinstance(response, dict)
                else str(response)
            )
            if self.use_chunked_hash:
                response_hash = compute_prompt_hash_chunked(response_for_hash)
            else:
                response_hash = compute_prompt_hash(response_for_hash)

        # Privacy control: use per-request override or instance default
        should_store_full = (
            store_full_content if store_full_content is not None else self.store_full_prompts
        )

        if should_store_full:
            # Store full prompt and response text
            prompt_str = json.dumps(prompt) if isinstance(prompt, list) else str(prompt)
            response_str = (
                json.dumps(response)
                if isinstance(response, dict)
                else str(response)
                if response is not None
                else None
            )
        else:
            # Privacy mode: only store hash, not content
            prompt_str = None
            response_str = None

        # Calculate cost based on usage and pricing
        cost_usd = calculate_cost(usage, pricing)

        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO api_logs (
                    request_id, model_id, provider,
                    temperature, top_p, max_tokens, seed, stream,
                    ttft_ms, latency_ms,
                    prompt_tokens, completion_tokens, reasoning_tokens, total_tokens,
                    cache_read_tokens, cache_write_tokens, cost_usd,
                    prompt, response, prompt_hash, response_hash,
                    status_code, error, user_id, session_id, metadata,
                    tools, upstream_cost_usd
                )
                VALUES (
                    $1, $2, $3,
                    $4, $5, $6, $7, $8,
                    $9, $10,
                    $11, $12, $13, $14,
                    $15, $16, $17,
                    $18, $19, $20, $21,
                    $22, $23, $24, $25, $26::jsonb,
                    $27::jsonb, $28
                )
                ON CONFLICT (request_id) DO NOTHING
                """,
                request_id,
                model_id,
                provider,
                # Request parameters
                (params or {}).get("temperature"),
                (params or {}).get("top_p"),
                (params or {}).get("max_tokens"),
                (params or {}).get("seed"),
                (params or {}).get("stream"),
                # Performance metrics
                ttft_ms,
                latency_ms,
                # Token usage
                (usage or {}).get("prompt_tokens"),
                (usage or {}).get("completion_tokens"),
                (usage or {}).get("reasoning_tokens"),
                (usage or {}).get("total_tokens"),
                # Cache and cost
                (usage or {}).get("cache_read_tokens"),
                (usage or {}).get("cache_write_tokens"),
                cost_usd,
                # Content
                prompt_str,
                response_str,
                prompt_hash,
                response_hash,
                # Metadata
                status_code,
                error,
                (metadata or {}).get("user_id"),
                (metadata or {}).get("session_id"),
                json.dumps(metadata) if metadata else None,
                json.dumps((params or {}).get("tools")) if (params or {}).get("tools") else None,
                upstream_cost_usd,
            )

    async def get_model_activity(self, window_minutes: int = 10) -> dict[str, Any]:
        """Aggregate recent real-user traffic per (model_id, provider).

        Returns a dict keyed by ``"model_id::provider"`` with per-route stats.
        Synthetic probes (``user_id IS NULL``) are excluded.
        """
        if not self.pool:
            return {}
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT model_id, provider,
                       COUNT(*)                                           AS request_count,
                       COUNT(*) FILTER (WHERE status_code < 400)          AS success_count,
                       MAX(timestamp)                                     AS last_request_at,
                       AVG(latency_ms) FILTER (WHERE status_code < 400)   AS avg_latency_ms,
                       COUNT(*) FILTER (WHERE stream = TRUE)              AS stream_count,
                       COUNT(*) FILTER (WHERE stream IS NOT TRUE)         AS non_stream_count,
                       COUNT(*) FILTER (WHERE stream = TRUE AND status_code < 400)
                           AS stream_success_count,
                       COUNT(*) FILTER (WHERE stream IS NOT TRUE AND status_code < 400)
                           AS non_stream_success_count,
                       MAX(timestamp) FILTER (WHERE stream = TRUE AND status_code < 400)
                           AS stream_last_success_at,
                       MAX(timestamp) FILTER (WHERE stream IS NOT TRUE AND status_code < 400)
                           AS non_stream_last_success_at,
                       AVG(ttft_ms) FILTER (WHERE stream = TRUE AND status_code < 400 AND ttft_ms IS NOT NULL)
                           AS stream_avg_ttft_ms,
                       AVG(completion_tokens) FILTER (WHERE status_code < 400 AND completion_tokens IS NOT NULL)
                           AS avg_completion_tokens,
                       AVG(completion_tokens) FILTER (WHERE stream = TRUE AND status_code < 400 AND completion_tokens IS NOT NULL)
                           AS stream_avg_completion_tokens,
                       AVG(completion_tokens) FILTER (WHERE stream IS NOT TRUE AND status_code < 400 AND completion_tokens IS NOT NULL)
                           AS non_stream_avg_completion_tokens
                FROM api_logs
                WHERE timestamp >= NOW() - ($1 || ' minutes')::interval
                  AND user_id IS NOT NULL
                GROUP BY model_id, provider
                """,
                str(window_minutes),
            )
        result: dict[str, Any] = {}
        for row in rows:
            key = f"{row['model_id']}::{row['provider']}"
            last_req = row["last_request_at"]

            def _iso(ts: object) -> str | None:
                if ts is None:
                    return None
                return ts.isoformat().replace("+00:00", "Z")  # type: ignore[union-attr]

            def _round_or_none(val: object) -> float | None:
                if val is None:
                    return None
                return round(float(val), 1)

            result[key] = {
                "request_count": row["request_count"],
                "success_count": row["success_count"],
                "last_request_at": _iso(last_req),
                "avg_latency_ms": _round_or_none(row["avg_latency_ms"]),
                "stream_count": row["stream_count"],
                "non_stream_count": row["non_stream_count"],
                "stream_success_count": row["stream_success_count"],
                "non_stream_success_count": row["non_stream_success_count"],
                "stream_last_success_at": _iso(row["stream_last_success_at"]),
                "non_stream_last_success_at": _iso(row["non_stream_last_success_at"]),
                "stream_avg_ttft_ms": _round_or_none(row["stream_avg_ttft_ms"]),
                "avg_completion_tokens": _round_or_none(row["avg_completion_tokens"]),
                "stream_avg_completion_tokens": _round_or_none(row["stream_avg_completion_tokens"]),
                "non_stream_avg_completion_tokens": _round_or_none(
                    row["non_stream_avg_completion_tokens"]
                ),
            }
        return result

    async def get_stats(
        self, model_id: str | None = None, provider: str | None = None, hours: int = 24
    ) -> list[dict[str, Any]]:
        """Fetch aggregated hourly stats for the given time window."""
        if not self.pool:
            return []
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT * FROM api_stats_hourly
                WHERE hour >= NOW() - ($1 || ' hours')::interval
                AND ($2::text IS NULL OR model_id = $2)
                AND ($3::text IS NULL OR provider = $3)
                ORDER BY hour DESC
                LIMIT 1000
                """,
                hours,
                model_id,
                provider,
            )
        return [dict(r) for r in rows]

    async def cleanup(self) -> None:
        """Close the connection pool if initialized."""
        if self.pool:
            try:
                await self.pool.close()  # type: ignore[attr-defined]
            finally:
                self.pool = None
