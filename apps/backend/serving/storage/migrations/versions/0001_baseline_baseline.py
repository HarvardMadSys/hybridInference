"""baseline.

Revision ID: 0001_baseline
Revises:
Create Date: 2026-05-04

The ``upgrade()`` body is a mechanical extraction of the
``CREATE TABLE IF NOT EXISTS`` and ``CREATE INDEX IF NOT EXISTS`` statements
that previously lived in:

  * apps/backend/serving/storage/database.py
  * apps/backend/serving/storage/postgres_log.py
  * apps/backend/serving/storage/postgres_operational.py

``IF NOT EXISTS`` is preserved so that running this migration on a
production database that was previously initialized via the legacy startup
path (or stamped by the operator) is a safe no-op.

Cut-over procedure (post-merge, before the first deploy that contains the
boot guard): on each environment, run ``uv run alembic stamp 0001_baseline``
once.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "0001_baseline"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the full current schema (idempotent)."""
    # ------------------------------------------------------------------
    # api_logs — request/response telemetry
    # ------------------------------------------------------------------
    op.execute("""
        CREATE TABLE IF NOT EXISTS api_logs (
            id BIGSERIAL PRIMARY KEY,
            timestamp TIMESTAMPTZ DEFAULT NOW(),
            request_id TEXT NOT NULL UNIQUE,
            model_id TEXT NOT NULL,
            provider TEXT NOT NULL,
            temperature FLOAT,
            top_p FLOAT,
            max_tokens INTEGER,
            seed INTEGER,
            stream BOOLEAN,
            ttft_ms INTEGER,
            latency_ms INTEGER,
            prompt_tokens INTEGER,
            completion_tokens INTEGER,
            reasoning_tokens INTEGER,
            total_tokens INTEGER,
            prompt TEXT,
            response TEXT,
            prompt_hash TEXT,
            response_hash TEXT,
            status_code INTEGER,
            error TEXT,
            user_id TEXT,
            session_id TEXT,
            metadata JSONB,
            tools JSONB,
            cache_read_tokens INTEGER,
            cache_write_tokens INTEGER,
            cost_usd DECIMAL(12, 8),
            upstream_cost_usd DECIMAL(12, 8)
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_api_logs_timestamp ON api_logs(timestamp DESC)")
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_api_logs_model ON api_logs(model_id, timestamp DESC)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_api_logs_provider ON api_logs(provider, timestamp DESC)"
    )
    op.execute("CREATE INDEX IF NOT EXISTS idx_api_logs_request_id ON api_logs(request_id)")
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_api_logs_user "
        "ON api_logs(user_id, timestamp DESC) "
        "WHERE user_id IS NOT NULL"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_api_logs_session "
        "ON api_logs(session_id, timestamp DESC) "
        "WHERE session_id IS NOT NULL"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_api_logs_model_activity "
        "ON api_logs(timestamp DESC, model_id, provider) "
        "WHERE user_id IS NOT NULL"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_api_logs_error "
        "ON api_logs(timestamp DESC) "
        "WHERE error IS NOT NULL"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_api_logs_prompt_hash "
        "ON api_logs(prompt_hash) "
        "WHERE prompt_hash IS NOT NULL"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_api_logs_response_hash "
        "ON api_logs(response_hash) "
        "WHERE response_hash IS NOT NULL"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_api_logs_user_cost "
        "ON api_logs(user_id, timestamp, cost_usd)"
    )

    # ------------------------------------------------------------------
    # api_stats_hourly — pre-aggregated stats
    # ------------------------------------------------------------------
    op.execute("""
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

    # ------------------------------------------------------------------
    # users — accounts
    # ------------------------------------------------------------------
    op.execute("""
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
    op.execute("CREATE INDEX IF NOT EXISTS idx_users_email ON users(email)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_users_status ON users(status)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_users_created_at ON users(created_at DESC)")
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_users_pending_approval "
        "ON users(created_at DESC) WHERE status = 'pending_approval'"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_users_last_login_at ON users(last_login_at DESC NULLS LAST)"
    )

    # ------------------------------------------------------------------
    # api_keys
    # ------------------------------------------------------------------
    op.execute("""
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
            account_id TEXT,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            expires_at TIMESTAMPTZ,
            last_used_at TIMESTAMPTZ,
            notes TEXT,
            metadata JSONB
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_api_keys_user ON api_keys(user_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_api_keys_status ON api_keys(status, expires_at)")
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_api_keys_prefix_unique ON api_keys(key_prefix)"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_api_keys_user_unique "
        "ON api_keys(user_id) WHERE status = 'active'"
    )
    op.execute("CREATE INDEX IF NOT EXISTS idx_api_keys_account ON api_keys(account_id)")
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_api_keys_account_active_unique "
        "ON api_keys(account_id) WHERE status = 'active' AND account_id IS NOT NULL"
    )

    # ------------------------------------------------------------------
    # auth_sessions
    # ------------------------------------------------------------------
    op.execute("""
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
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_auth_sessions_user ON auth_sessions(user_id, expires_at)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_auth_sessions_token "
        "ON auth_sessions(refresh_token_hash) WHERE NOT revoked"
    )
    op.execute("CREATE INDEX IF NOT EXISTS idx_auth_sessions_jti ON auth_sessions(jti)")

    # ------------------------------------------------------------------
    # email_verification_tokens
    # ------------------------------------------------------------------
    op.execute("""
        CREATE TABLE IF NOT EXISTS email_verification_tokens (
            token TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            expires_at TIMESTAMPTZ NOT NULL,
            used_at TIMESTAMPTZ
        )
    """)
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_email_verification_user "
        "ON email_verification_tokens(user_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_email_verification_expires "
        "ON email_verification_tokens(expires_at)"
    )

    # ------------------------------------------------------------------
    # password_reset_tokens
    # ------------------------------------------------------------------
    op.execute("""
        CREATE TABLE IF NOT EXISTS password_reset_tokens (
            token TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            expires_at TIMESTAMPTZ NOT NULL,
            used_at TIMESTAMPTZ
        )
    """)
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_password_reset_user ON password_reset_tokens(user_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_password_reset_expires ON password_reset_tokens(expires_at)"
    )

    # ------------------------------------------------------------------
    # admin_audit_log
    # ------------------------------------------------------------------
    op.execute("""
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
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_admin_audit_timestamp ON admin_audit_log(timestamp DESC)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_admin_audit_user "
        "ON admin_audit_log(target_user_id, timestamp DESC)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_admin_audit_action "
        "ON admin_audit_log(action, timestamp DESC)"
    )

    # ------------------------------------------------------------------
    # user_daily_cost
    # ------------------------------------------------------------------
    op.execute("""
        CREATE TABLE IF NOT EXISTS user_daily_cost (
            user_id TEXT NOT NULL,
            day TEXT NOT NULL,
            cost_usd DECIMAL(12, 6) NOT NULL DEFAULT 0,
            requests INTEGER NOT NULL DEFAULT 0,
            last_request_at TIMESTAMPTZ,
            PRIMARY KEY (user_id, day)
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_user_daily_cost_day ON user_daily_cost(day)")

    # ------------------------------------------------------------------
    # signup_allowed_domains — admin-editable allowlist
    # ------------------------------------------------------------------
    # ``created_by`` uses ON DELETE SET NULL so hard-deleting a user who
    # once added an entry doesn't fail with a FK violation.
    op.execute("""
        CREATE TABLE IF NOT EXISTS signup_allowed_domains (
            domain TEXT NOT NULL,
            is_wildcard BOOLEAN NOT NULL DEFAULT FALSE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            created_by TEXT REFERENCES users(id) ON DELETE SET NULL,
            PRIMARY KEY (domain, is_wildcard)
        )
    """)

    # ------------------------------------------------------------------
    # site_settings — DB-backed runtime feature flags
    # ------------------------------------------------------------------
    op.execute("""
        CREATE TABLE IF NOT EXISTS site_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            value_type TEXT NOT NULL DEFAULT 'str',
            updated_at TIMESTAMPTZ DEFAULT NOW(),
            updated_by TEXT
        )
    """)

    # ------------------------------------------------------------------
    # email_broadcasts + email_broadcast_recipients
    # ------------------------------------------------------------------
    op.execute("""
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
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_email_broadcasts_status_scheduled "
        "ON email_broadcasts(status, scheduled_at) "
        "WHERE status = 'scheduled'"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_email_broadcasts_created_at "
        "ON email_broadcasts(created_at DESC)"
    )
    op.execute("""
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
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_broadcast_recipients_broadcast "
        "ON email_broadcast_recipients(broadcast_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_broadcast_recipients_status "
        "ON email_broadcast_recipients(broadcast_id, status)"
    )

    # ------------------------------------------------------------------
    # provider_hourly_stats — APScheduler rollup target
    # ------------------------------------------------------------------
    op.execute("""
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
    op.execute("CREATE INDEX IF NOT EXISTS idx_phs_hour ON provider_hourly_stats(hour_bucket DESC)")
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_phs_provider_hour "
        "ON provider_hourly_stats(provider, hour_bucket DESC)"
    )


def downgrade() -> None:
    """Baseline migrations are not reversible.

    Going below the baseline would leave the database empty. If a clean wipe
    is what's wanted, drop and recreate the database directly.
    """
    raise NotImplementedError("baseline migration is irreversible")
