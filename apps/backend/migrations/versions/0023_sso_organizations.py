"""Organizations: the record, its SSO connections, its members, and their audit rows.

- ``sso_organizations``: one row per organization. Its monthly limit and the
  default monthly limit of a member are money, so each is a ``*_usd`` column
  with a native ``*_amount`` column beside it, kept in step by the
  ``accounting_sync_amounts`` trigger ``0022_ledger_currency`` installed. NULL
  in a limit is "none".
- ``sso_organization_connections``: which organization an ``SSO_PROVIDERS``
  entry belongs to. The provider id is the key: one organization at most.
- ``sso_organization_members``: which organization an account belongs to, and
  its role there. The account is the key: one organization at most. A removal
  by the organization marks the row (``removed_at``, ``removed_by``) and leaves
  it; the row goes only with its account.
- ``admin_audit_log.organization_id``: the organization an audit row belongs
  to, NULL for every row that belongs to none.

The names carry ``sso_`` because ``organizations`` and ``org_members`` already
exist: ``0015_projects`` made them for the hidden personal organization,
earlier code still writes them, and a contract revision is planned to drop
them. These tables are independent of that revision.

Expand only: the new tables and the new column are invisible to earlier code,
which keeps writing audit rows without the column.

Revision ID: 0023_sso_organizations
Revises: 0022_ledger_currency
Create Date: 2026-11-18
"""

from __future__ import annotations

import json

from alembic import op

revision = "0023_sso_organizations"
down_revision = "0022_ledger_currency"
branch_labels = None
depends_on = None
compatibility = "expand"

#: USD column to native column, the pairs the trigger keeps in step.
LIMIT_PAIRS = {
    "monthly_limit_usd": "monthly_limit_amount",
    "default_member_monthly_limit_usd": "default_member_monthly_limit_amount",
}


def upgrade() -> None:
    """Add the three tables, the limits' trigger and the audit column."""
    op.execute(
        "CREATE TABLE IF NOT EXISTS sso_organizations ("
        "id TEXT PRIMARY KEY, "
        "name TEXT NOT NULL, "
        "status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'suspended')), "
        "monthly_limit_usd DECIMAL(14, 4), "
        "monthly_limit_amount NUMERIC(24, 12), "
        "default_member_monthly_limit_usd DECIMAL(10, 4), "
        "default_member_monthly_limit_amount NUMERIC(24, 12), "
        "accounting_currency TEXT, "
        "created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), "
        "updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()"
        ")"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_sso_organizations_name "
        "ON sso_organizations (lower(name))"
    )
    mapping = json.dumps(LIMIT_PAIRS, separators=(",", ":"))
    op.execute(
        "CREATE OR REPLACE TRIGGER sso_organizations_accounting_amounts "
        "BEFORE INSERT OR UPDATE ON sso_organizations "
        f"FOR EACH ROW EXECUTE FUNCTION accounting_sync_amounts('{mapping}')"
    )
    op.execute(
        "CREATE TABLE IF NOT EXISTS sso_organization_connections ("
        "provider_id TEXT PRIMARY KEY, "
        "organization_id TEXT NOT NULL REFERENCES sso_organizations(id) ON DELETE CASCADE, "
        "created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()"
        ")"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_sso_organization_connections_organization "
        "ON sso_organization_connections (organization_id)"
    )
    op.execute(
        "CREATE TABLE IF NOT EXISTS sso_organization_members ("
        "user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE, "
        "organization_id TEXT NOT NULL REFERENCES sso_organizations(id) ON DELETE CASCADE, "
        "role TEXT NOT NULL DEFAULT 'member' CHECK (role IN ('member', 'admin')), "
        "joined_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), "
        "removed_at TIMESTAMPTZ, "
        "removed_by TEXT"
        ")"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_sso_organization_members_organization "
        "ON sso_organization_members (organization_id)"
    )
    op.execute("ALTER TABLE admin_audit_log ADD COLUMN IF NOT EXISTS organization_id TEXT")
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_admin_audit_log_organization "
        "ON admin_audit_log (organization_id, timestamp DESC) "
        "WHERE organization_id IS NOT NULL"
    )


def downgrade() -> None:
    """Keep everything: earlier code names none of it.

    ``upgrade`` adds each object with ``IF NOT EXISTS`` or ``OR REPLACE``, so
    going back up after a downgrade still works, and no organization is lost.
    """
