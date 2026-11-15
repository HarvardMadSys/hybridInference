"""Make projects belong to accounts, and fold each personal organization into its account.

``0015_projects`` put a hidden personal organization between an account and
its projects. Every account had exactly one, so it carried nothing the account
does not, except one thing: the organization held the account's quota in one
place, while every other copy lived on the account's keys. This revision gives
that quota a home of its own and moves ownership of a project to the account:

- ``account_quotas`` holds each account's daily and monthly quota. It is what
  a new key inherits; the keys keep their copies, which the request path reads.
  It is filled from the organization's budget where an administrator set one,
  otherwise from the quota the account's keys carry (a live key's, else the
  most recently revoked one's), so an account without keys keeps a budget set
  for it too.
- ``projects.user_id`` owns a project, filled from each organization's owner.
- A suspended organization suspends its account.
- An organization concurrency cap has no equivalent here: it limited batch
  and interactive work together, which no account setting does. The revision
  refuses to run while any organization has one, naming them, so an
  administrator decides what each becomes before the cap stops applying.

The organization's budgets and status are then cleared, so the previous
release, which reads an organization ceiling ahead of the key quota, enforces
what this one does. Nothing is dropped or constrained: the previous release
keeps working on the result. While both may run, two triggers keep its writes
meaningful to this release: a project it creates gets its owning account, and
a budget or suspension it writes to an organization goes to the account. A
later contract revision drops the triggers, ``organizations``,
``org_members``, ``projects.org_id`` and ``api_keys.org_id``.

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

#: Refuse to run while an organization concurrency cap would silently stop applying.
REFUSE_CONCURRENCY_CAPS = """
DO $$
DECLARE capped text;
BEGIN
    SELECT string_agg(id, ', ' ORDER BY id) INTO capped
    FROM organizations WHERE max_concurrent_requests IS NOT NULL;
    IF capped IS NOT NULL THEN
        RAISE EXCEPTION 'organizations with a concurrency cap: %', capped
            USING HINT = 'Clear each cap with PATCH /admin/orgs/{org_id} on the running '
                         'release, and set the account or project cap that should replace '
                         'it, then run the migration again.';
    END IF;
END
$$
"""

#: Each account's quota: the organization's budget where set, else its keys'.
_KEY_QUOTA = (
    "SELECT DISTINCT ON (k.user_id) k.user_id, k.quota_daily_cost_usd, k.quota_monthly_cost_usd "
    "FROM api_keys k "
    "ORDER BY k.user_id, k.status = 'revoked', "
    "CASE WHEN k.status = 'revoked' THEN GREATEST(k.created_at, k.last_used_at) END "
    "DESC NULLS LAST, k.created_at, k.id"
)

#: A project the previous release creates names only its organization.
ADOPT_PROJECT_FUNCTION = """
CREATE OR REPLACE FUNCTION projects_adopt_legacy_owner() RETURNS trigger AS $$
BEGIN
    IF NEW.user_id IS NULL AND NEW.org_id LIKE 'org\\_p\\_%' THEN
        NEW.user_id := substr(NEW.org_id, 7);
    END IF;
    RETURN NEW;
END
$$ LANGUAGE plpgsql
"""

#: A budget or suspension the previous release writes to an organization goes
#: to its account, and the organization keeps none of its own.
FOLD_ORGANIZATION_FUNCTION = """
CREATE OR REPLACE FUNCTION organizations_fold_into_account() RETURNS trigger AS $$
DECLARE
    owner text;
    old_daily numeric;
    old_monthly numeric;
BEGIN
    IF NEW.kind <> 'personal' OR NEW.id NOT LIKE 'org\\_p\\_%' THEN
        RETURN NEW;
    END IF;
    owner := substr(NEW.id, 7);
    IF TG_OP = 'UPDATE' THEN
        old_daily := OLD.budget_daily_usd;
        old_monthly := OLD.budget_monthly_usd;
    END IF;
    IF NEW.budget_daily_usd IS DISTINCT FROM old_daily
       OR NEW.budget_monthly_usd IS DISTINCT FROM old_monthly THEN
        INSERT INTO account_quotas (user_id, quota_daily_cost_usd, quota_monthly_cost_usd)
        SELECT owner, NEW.budget_daily_usd, NEW.budget_monthly_usd
        WHERE EXISTS (SELECT 1 FROM users WHERE id = owner)
        ON CONFLICT (user_id) DO UPDATE SET
            quota_daily_cost_usd = CASE WHEN NEW.budget_daily_usd IS DISTINCT FROM old_daily
                THEN EXCLUDED.quota_daily_cost_usd ELSE account_quotas.quota_daily_cost_usd END,
            quota_monthly_cost_usd = CASE WHEN NEW.budget_monthly_usd IS DISTINCT FROM old_monthly
                THEN EXCLUDED.quota_monthly_cost_usd ELSE account_quotas.quota_monthly_cost_usd END,
            updated_at = NOW();
        NEW.budget_daily_usd := NULL;
        NEW.budget_monthly_usd := NULL;
    END IF;
    IF NEW.status = 'suspended' THEN
        UPDATE users SET status = 'suspended' WHERE id = owner AND status = 'active';
        NEW.status := 'active';
    END IF;
    IF NEW.max_concurrent_requests IS NOT NULL THEN
        RAISE EXCEPTION 'organization concurrency caps are no longer supported'
            USING HINT = 'Set the account or project concurrency cap instead.';
    END IF;
    RETURN NEW;
END
$$ LANGUAGE plpgsql
"""

#: The statements, in order.
STATEMENTS = (
    REFUSE_CONCURRENCY_CAPS,
    """
    CREATE TABLE IF NOT EXISTS account_quotas (
        user_id                TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
        quota_daily_cost_usd   DECIMAL(10, 4),
        quota_monthly_cost_usd DECIMAL(10, 4),
        updated_at             TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    "INSERT INTO account_quotas (user_id, quota_daily_cost_usd, quota_monthly_cost_usd) "
    "SELECT u.id, "
    "CASE WHEN o.budget_daily_usd IS NOT NULL THEN o.budget_daily_usd "
    "ELSE k.quota_daily_cost_usd END, "
    "CASE WHEN o.budget_monthly_usd IS NOT NULL THEN o.budget_monthly_usd "
    "ELSE k.quota_monthly_cost_usd END "
    f"FROM users u LEFT JOIN ({_PERSONAL_OWNER}) owner ON owner.user_id = u.id "
    "LEFT JOIN organizations o ON o.id = owner.org_id "
    f"LEFT JOIN ({_KEY_QUOTA}) k ON k.user_id = u.id "
    "WHERE k.user_id IS NOT NULL "
    "OR o.budget_daily_usd IS NOT NULL OR o.budget_monthly_usd IS NOT NULL "
    "ON CONFLICT (user_id) DO NOTHING",
    # The keys carry the account quota too: give them a budget set on the organization.
    "UPDATE api_keys k SET quota_daily_cost_usd = q.quota_daily_cost_usd, "
    "quota_monthly_cost_usd = q.quota_monthly_cost_usd "
    "FROM account_quotas q WHERE q.user_id = k.user_id "
    "AND (k.quota_daily_cost_usd IS DISTINCT FROM q.quota_daily_cost_usd "
    "OR k.quota_monthly_cost_usd IS DISTINCT FROM q.quota_monthly_cost_usd)",
    "ALTER TABLE projects ADD COLUMN IF NOT EXISTS user_id TEXT "
    "REFERENCES users(id) ON DELETE CASCADE",
    f"UPDATE projects p SET user_id = owner.user_id FROM ({_PERSONAL_OWNER}) owner "
    "WHERE p.org_id = owner.org_id AND p.user_id IS NULL",
    "CREATE INDEX IF NOT EXISTS idx_projects_user ON projects(user_id)",
    "UPDATE users u SET status = 'suspended' "
    f"FROM ({_PERSONAL_OWNER}) owner JOIN organizations o ON o.id = owner.org_id "
    "WHERE u.id = owner.user_id AND o.status = 'suspended' AND u.status = 'active'",
    "UPDATE organizations SET budget_daily_usd = NULL, budget_monthly_usd = NULL, "
    "status = 'active' "
    "WHERE budget_daily_usd IS NOT NULL OR budget_monthly_usd IS NOT NULL OR status <> 'active'",
    ADOPT_PROJECT_FUNCTION,
    "CREATE OR REPLACE TRIGGER projects_adopt_legacy_owner BEFORE INSERT OR UPDATE ON projects "
    "FOR EACH ROW EXECUTE FUNCTION projects_adopt_legacy_owner()",
    FOLD_ORGANIZATION_FUNCTION,
    "CREATE OR REPLACE TRIGGER organizations_fold_into_account BEFORE INSERT OR UPDATE ON organizations "
    "FOR EACH ROW EXECUTE FUNCTION organizations_fold_into_account()",
)


def upgrade() -> None:
    """Give the account its quota and its projects; empty the organizations."""
    for statement in STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    """Keep everything: the previous release reads none of the new objects."""
