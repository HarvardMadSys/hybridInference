"""Make projects belong to accounts, and fold each personal organization into its account.

``0015_projects`` put a hidden personal organization between an account and
its projects. Every account had exactly one, so it carried nothing the account
does not: its budgets duplicated the account quota that every key of the
account already holds, its concurrency cap duplicated
``users.max_concurrent_requests`` and its suspension duplicated the account's
own status. This revision moves ownership of a project to the account and
moves whatever an administrator set on an organization onto the account:

- A daily or monthly organization budget becomes the quota on every key of
  the account, revoked ones included, since a new key inherits the quota from
  the newest revoked key when no live key remains.
- An organization concurrency cap becomes the account's cap, never raising a
  cap the account already had.
- A suspended organization suspends its account.

The organization's own values are then cleared, so the previous release, which
reads an organization ceiling ahead of the key quota, enforces what this one
does. Nothing is dropped or constrained: ``organizations``, ``org_members``,
``projects.org_id`` and ``api_keys.org_id`` stay for the previous release, and
the per-account unique indexes on ``projects.org_id`` keep holding because this
release still fills it with the account's own organization id. A later
contract revision drops them and indexes ``projects.user_id`` instead.

Revision ID: 0018_projects_owned_by_accounts
Revises: 0017_revoke_scoped_keys
Create Date: 2026-11-15
"""

from __future__ import annotations

from alembic import op

revision = "0018_projects_owned_by_accounts"
down_revision = "0017_revoke_scoped_keys"
branch_labels = None
depends_on = None
compatibility = "expand"

#: The account that owns each personal organization, as ``owner(org_id, user_id)``.
_PERSONAL_OWNER = (
    "SELECT m.org_id, m.user_id FROM org_members m "
    "JOIN organizations o ON o.id = m.org_id "
    "WHERE o.kind = 'personal' AND m.role = 'owner'"
)

#: The statements, in order.
STATEMENTS = (
    "ALTER TABLE projects ADD COLUMN IF NOT EXISTS user_id TEXT "
    "REFERENCES users(id) ON DELETE CASCADE",
    f"UPDATE projects p SET user_id = owner.user_id FROM ({_PERSONAL_OWNER}) owner "
    "WHERE p.org_id = owner.org_id AND p.user_id IS NULL",
    "CREATE INDEX IF NOT EXISTS idx_projects_user ON projects(user_id)",
    # An organization budget is the account's quota: put it on every key.
    "UPDATE api_keys k SET quota_daily_cost_usd = o.budget_daily_usd "
    f"FROM ({_PERSONAL_OWNER}) owner JOIN organizations o ON o.id = owner.org_id "
    "WHERE k.user_id = owner.user_id AND o.budget_daily_usd IS NOT NULL "
    "AND k.quota_daily_cost_usd IS DISTINCT FROM o.budget_daily_usd",
    "UPDATE api_keys k SET quota_monthly_cost_usd = o.budget_monthly_usd "
    f"FROM ({_PERSONAL_OWNER}) owner JOIN organizations o ON o.id = owner.org_id "
    "WHERE k.user_id = owner.user_id AND o.budget_monthly_usd IS NOT NULL "
    "AND k.quota_monthly_cost_usd IS DISTINCT FROM o.budget_monthly_usd",
    "UPDATE users u SET max_concurrent_requests = "
    "LEAST(COALESCE(u.max_concurrent_requests, o.max_concurrent_requests), "
    "o.max_concurrent_requests) "
    f"FROM ({_PERSONAL_OWNER}) owner JOIN organizations o ON o.id = owner.org_id "
    "WHERE u.id = owner.user_id AND o.max_concurrent_requests IS NOT NULL",
    "UPDATE users u SET status = 'suspended' "
    f"FROM ({_PERSONAL_OWNER}) owner JOIN organizations o ON o.id = owner.org_id "
    "WHERE u.id = owner.user_id AND o.status = 'suspended' AND u.status = 'active'",
    "UPDATE organizations SET budget_daily_usd = NULL, budget_monthly_usd = NULL, "
    "max_concurrent_requests = NULL, status = 'active' "
    "WHERE budget_daily_usd IS NOT NULL OR budget_monthly_usd IS NOT NULL "
    "OR max_concurrent_requests IS NOT NULL OR status <> 'active'",
)


def upgrade() -> None:
    """Move project ownership and organization settings onto the account."""
    for statement in STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    """Keep everything: the previous release reads none of the new columns."""
