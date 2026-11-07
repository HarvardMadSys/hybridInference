"""Add organizations, projects, and the spend counters that attribute usage.

Expand-only: every new column is nullable and every new table is additive, so
the previous release keeps serving while this runs. The backfill that fills a
personal org and a Default project for existing accounts runs in the gateway
(code), not here, because it must be idempotent and resumable.

Revision ID: 0010_projects
Revises: 0009_route_catalog
Create Date: 2026-11-05
"""

from __future__ import annotations

from alembic import op

revision = "0010_projects"
down_revision = "0009_route_catalog"
branch_labels = None
depends_on = None
compatibility = "expand"

API_LOG_STATEMENTS = (
    "ALTER TABLE api_logs ADD COLUMN IF NOT EXISTS key_id BIGINT, ADD COLUMN IF NOT EXISTS project_id TEXT",
)


def upgrade() -> None:
    """Create the project model and the attribution columns."""
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS organizations (
            id                      TEXT PRIMARY KEY,
            name                    TEXT NOT NULL,
            kind                    TEXT NOT NULL CHECK (kind IN ('personal', 'team')),
            status                  TEXT NOT NULL DEFAULT 'active'
                                      CHECK (status IN ('active', 'suspended')),
            budget_daily_usd        DECIMAL(12, 4),
            budget_monthly_usd      DECIMAL(12, 4),
            max_concurrent_requests INTEGER,
            created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS org_members (
            org_id     TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
            user_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            role       TEXT NOT NULL CHECK (role IN ('owner', 'admin', 'member')),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (org_id, user_id)
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS idx_org_members_user ON org_members(user_id)")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS projects (
            id                      TEXT PRIMARY KEY,
            org_id                  TEXT NOT NULL
                                      REFERENCES organizations(id) ON DELETE CASCADE,
            name                    TEXT NOT NULL,
            description             TEXT,
            is_default              BOOLEAN NOT NULL DEFAULT FALSE,
            status                  TEXT NOT NULL DEFAULT 'active'
                                      CHECK (status IN ('active', 'archived')),
            budget_daily_usd        DECIMAL(12, 4),
            budget_monthly_usd      DECIMAL(12, 4),
            max_concurrent_requests INTEGER,
            allowed_models          TEXT[],
            settings                JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            archived_at             TIMESTAMPTZ
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_projects_org_name "
        "ON projects(org_id, lower(name)) WHERE status = 'active'"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_projects_org_default "
        "ON projects(org_id) WHERE is_default"
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS spend_counters (
            scope_id        TEXT NOT NULL,
            period          TEXT NOT NULL,
            cost_usd        DECIMAL(14, 6) NOT NULL DEFAULT 0,
            requests        INTEGER NOT NULL DEFAULT 0,
            last_request_at TIMESTAMPTZ,
            PRIMARY KEY (scope_id, period)
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS idx_spend_counters_period ON spend_counters(period)")
    op.execute(
        """
        ALTER TABLE api_keys
            ADD COLUMN IF NOT EXISTS org_id TEXT,
            ADD COLUMN IF NOT EXISTS project_id TEXT,
            ADD COLUMN IF NOT EXISTS allowed_models TEXT[],
            ADD COLUMN IF NOT EXISTS max_concurrent_requests INTEGER,
            ADD COLUMN IF NOT EXISTS revoked_at TIMESTAMPTZ,
            ADD COLUMN IF NOT EXISTS rotated_from_id BIGINT
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_api_keys_project_active "
        "ON api_keys(project_id) WHERE status = 'active'"
    )
    for statement in API_LOG_STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    """Keep everything: every addition is nullable and unread by earlier code."""
