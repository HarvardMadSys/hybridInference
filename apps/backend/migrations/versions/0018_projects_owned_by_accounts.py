"""Make projects belong to accounts, and give the account quota a home of its own.

``0015_projects`` put a hidden personal organization between an account and
its projects. Every account had exactly one, so it carried nothing the account
does not, except one thing: its budgets were the one record of the account's
quota that did not live on a key. This revision moves that record and the
ownership of projects to the account:

- ``account_quotas`` holds each account's daily and monthly quota, and is what
  this release enforces. A daily quota left NULL is unset: a key's own copy
  applies. It is filled with what the previous release enforced: the
  organization's budget where set, otherwise the quota the account's keys
  carry (a live key's, else the most recently revoked one's).
- ``projects.user_id`` owns a project.

The previous release keeps working on the result, during the rollout and
after a rollback. It enforces an organization budget ahead of a key's copy, so
both stay copies of the account quota: the revision writes them once, and two
triggers keep them so. One mirrors every ``account_quotas`` write to the
account's organization; the other carries a budget the previous release writes
on an organization to ``account_quotas``. A third gives a project the previous
release creates its owning account.

Three things an organization could hold have no account equivalent. The
revision refuses to run while any organization has one, naming them, and the
triggers refuse them afterwards:

- a concurrency cap, which limited batch and interactive work together;
- a suspension, which is not an account suspension (that revokes sessions);
- a budget of 1,000,000 USD or more, which a key's quota column cannot hold.

All three could only be set through ``PATCH /admin/orgs``, which no console
used. A later contract revision drops the triggers, ``organizations``,
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

#: The lock every write of an account's keys or quota takes, shared or
#: exclusive (``serving.storage.postgres_operational._lock_all_account_keys``).
#: Held to the end of the revision, so no quota written by the running release
#: falls between the copy below and the triggers that take over from it.
LOCK_QUOTA_WRITERS = "SELECT pg_advisory_xact_lock(hashtextextended('hybridinference:api-keys', 0))"

#: Refuse to run while an organization holds what no account setting replaces.
REFUSE_UNSUPPORTED_SETTINGS = """
DO $$
DECLARE unsupported text;
BEGIN
    SELECT string_agg(id || ' (' || reason || ')', ', ' ORDER BY id) INTO unsupported
    FROM (
        SELECT id, concat_ws(' and ',
            CASE WHEN max_concurrent_requests IS NOT NULL THEN 'concurrency cap' END,
            CASE WHEN status <> 'active' THEN 'suspended' END,
            CASE WHEN budget_daily_usd >= 1000000 OR budget_monthly_usd >= 1000000
                 THEN 'budget of 1000000 or more' END) AS reason
        FROM organizations
    ) settings
    WHERE reason <> '';
    IF unsupported IS NOT NULL THEN
        RAISE EXCEPTION 'organizations with settings accounts cannot take over: %', unsupported
            USING HINT = 'On the running release, clear each with PATCH /admin/orgs/{org_id} '
                         '(a concurrency cap, status back to active, a budget under 1000000) '
                         'and set what should replace it on the account or its projects; '
                         'then run the migration again.';
    END IF;
END
$$
"""

#: The quota an account's keys carry: a live key's, else the most recently
#: revoked one's (what ``create_key`` inherited from before this revision).
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
    IF NEW.user_id IS NULL AND left(NEW.org_id, 6) = 'org_p_' THEN
        NEW.user_id := substr(NEW.org_id, 7);
    END IF;
    RETURN NEW;
END
$$ LANGUAGE plpgsql
"""

#: Every write of the account quota reaches the organization the previous
#: release enforces it from.
MIRROR_QUOTA_FUNCTION = """
CREATE OR REPLACE FUNCTION account_quotas_mirror_to_organization() RETURNS trigger AS $$
BEGIN
    -- Written by organizations_carry_legacy_writes, which sets the row itself.
    IF pg_trigger_depth() > 1 THEN
        RETURN NULL;
    END IF;
    UPDATE organizations
    SET budget_daily_usd = NEW.quota_daily_cost_usd,
        budget_monthly_usd = NEW.quota_monthly_cost_usd
    WHERE id = 'org_p_' || NEW.user_id
      AND kind = 'personal'
      AND (budget_daily_usd IS DISTINCT FROM NEW.quota_daily_cost_usd
           OR budget_monthly_usd IS DISTINCT FROM NEW.quota_monthly_cost_usd);
    RETURN NULL;
END
$$ LANGUAGE plpgsql
"""

#: A budget the previous release writes on an organization is the account's
#: quota, and the organization row always shows the account's.
CARRY_LEGACY_FUNCTION = """
CREATE OR REPLACE FUNCTION organizations_carry_legacy_writes() RETURNS trigger AS $$
DECLARE
    owner text;
    daily numeric;
    monthly numeric;
    recorded boolean;
    changed boolean := FALSE;
BEGIN
    -- The mirror write of account_quotas_mirror_to_organization.
    IF pg_trigger_depth() > 1 THEN
        RETURN NEW;
    END IF;
    IF NEW.kind <> 'personal' OR left(NEW.id, 6) <> 'org_p_' THEN
        RETURN NEW;
    END IF;
    owner := substr(NEW.id, 7);
    IF NEW.max_concurrent_requests IS NOT NULL THEN
        RAISE EXCEPTION 'organization concurrency caps are no longer supported'
            USING HINT = 'Set the account or project concurrency cap instead.';
    END IF;
    IF NEW.status <> 'active' THEN
        RAISE EXCEPTION 'organizations can no longer be suspended'
            USING HINT = 'Suspend the account instead.';
    END IF;

    -- The account's quota as recorded, else as its keys carry it.
    SELECT q.quota_daily_cost_usd, q.quota_monthly_cost_usd INTO daily, monthly
    FROM account_quotas q WHERE q.user_id = owner;
    recorded := FOUND;
    IF NOT recorded THEN
        SELECT k.quota_daily_cost_usd, k.quota_monthly_cost_usd INTO daily, monthly
        FROM api_keys k WHERE k.user_id = owner
        ORDER BY k.status = 'revoked',
                 CASE WHEN k.status = 'revoked' THEN GREATEST(k.created_at, k.last_used_at) END
                 DESC NULLS LAST, k.created_at, k.id
        LIMIT 1;
    END IF;

    IF TG_OP = 'INSERT' THEN
        IF NEW.budget_daily_usd IS NOT NULL THEN
            daily := NEW.budget_daily_usd;
            changed := TRUE;
        END IF;
        IF NEW.budget_monthly_usd IS NOT NULL THEN
            monthly := NEW.budget_monthly_usd;
            changed := TRUE;
        END IF;
    ELSE
        IF NEW.budget_daily_usd IS DISTINCT FROM OLD.budget_daily_usd THEN
            daily := NEW.budget_daily_usd;
            changed := TRUE;
        END IF;
        IF NEW.budget_monthly_usd IS DISTINCT FROM OLD.budget_monthly_usd THEN
            monthly := NEW.budget_monthly_usd;
            changed := TRUE;
        END IF;
    END IF;

    IF changed THEN
        IF daily >= 1000000 OR monthly >= 1000000 THEN
            RAISE EXCEPTION 'an account quota must be under 1000000 USD';
        END IF;
        INSERT INTO account_quotas (user_id, quota_daily_cost_usd, quota_monthly_cost_usd)
        SELECT owner, daily, monthly
        WHERE EXISTS (SELECT 1 FROM users WHERE id = owner)
        ON CONFLICT (user_id) DO UPDATE SET
            quota_daily_cost_usd = EXCLUDED.quota_daily_cost_usd,
            quota_monthly_cost_usd = EXCLUDED.quota_monthly_cost_usd,
            updated_at = NOW();
        recorded := FOUND;
    END IF;
    IF recorded THEN
        NEW.budget_daily_usd := daily;
        NEW.budget_monthly_usd := monthly;
    END IF;
    RETURN NEW;
END
$$ LANGUAGE plpgsql
"""

#: The statements, in order.
STATEMENTS = (
    LOCK_QUOTA_WRITERS,
    REFUSE_UNSUPPORTED_SETTINGS,
    """
    CREATE TABLE IF NOT EXISTS account_quotas (
        user_id                TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
        quota_daily_cost_usd   DECIMAL(10, 4),
        quota_monthly_cost_usd DECIMAL(10, 4),
        updated_at             TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    # What the previous release enforced: the organization's budget ahead of a key's copy.
    "INSERT INTO account_quotas (user_id, quota_daily_cost_usd, quota_monthly_cost_usd) "
    "SELECT u.id, COALESCE(o.budget_daily_usd, k.quota_daily_cost_usd), "
    "COALESCE(o.budget_monthly_usd, k.quota_monthly_cost_usd) "
    "FROM users u "
    "LEFT JOIN organizations o ON o.id = 'org_p_' || u.id AND o.kind = 'personal' "
    f"LEFT JOIN ({_KEY_QUOTA}) k ON k.user_id = u.id "
    "WHERE k.user_id IS NOT NULL "
    "OR o.budget_daily_usd IS NOT NULL OR o.budget_monthly_usd IS NOT NULL "
    "ON CONFLICT (user_id) DO NOTHING",
    # Both copies the previous release reads say the same: the account's live keys ...
    "UPDATE api_keys k SET "
    "quota_daily_cost_usd = COALESCE(q.quota_daily_cost_usd, k.quota_daily_cost_usd), "
    "quota_monthly_cost_usd = q.quota_monthly_cost_usd "
    "FROM account_quotas q WHERE q.user_id = k.user_id AND k.status <> 'revoked' "
    "AND (k.quota_daily_cost_usd IS DISTINCT FROM "
    "COALESCE(q.quota_daily_cost_usd, k.quota_daily_cost_usd) "
    "OR k.quota_monthly_cost_usd IS DISTINCT FROM q.quota_monthly_cost_usd)",
    # ... and its organization.
    "UPDATE organizations o SET budget_daily_usd = q.quota_daily_cost_usd, "
    "budget_monthly_usd = q.quota_monthly_cost_usd "
    "FROM account_quotas q WHERE o.id = 'org_p_' || q.user_id AND o.kind = 'personal' "
    "AND (o.budget_daily_usd IS DISTINCT FROM q.quota_daily_cost_usd "
    "OR o.budget_monthly_usd IS DISTINCT FROM q.quota_monthly_cost_usd)",
    "ALTER TABLE projects ADD COLUMN IF NOT EXISTS user_id TEXT "
    "REFERENCES users(id) ON DELETE CASCADE",
    "UPDATE projects p SET user_id = u.id FROM users u "
    "WHERE p.user_id IS NULL AND p.org_id = 'org_p_' || u.id",
    "CREATE INDEX IF NOT EXISTS idx_projects_user ON projects(user_id)",
    ADOPT_PROJECT_FUNCTION,
    "CREATE OR REPLACE TRIGGER projects_adopt_legacy_owner BEFORE INSERT OR UPDATE ON projects "
    "FOR EACH ROW EXECUTE FUNCTION projects_adopt_legacy_owner()",
    MIRROR_QUOTA_FUNCTION,
    "CREATE OR REPLACE TRIGGER account_quotas_mirror_to_organization "
    "AFTER INSERT OR UPDATE ON account_quotas "
    "FOR EACH ROW EXECUTE FUNCTION account_quotas_mirror_to_organization()",
    CARRY_LEGACY_FUNCTION,
    "CREATE OR REPLACE TRIGGER organizations_carry_legacy_writes "
    "BEFORE INSERT OR UPDATE ON organizations "
    "FOR EACH ROW EXECUTE FUNCTION organizations_carry_legacy_writes()",
)


def upgrade() -> None:
    """Give the account its quota and its projects, and keep the old copies in step."""
    for statement in STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    """Keep everything: the previous release reads none of the new objects."""
