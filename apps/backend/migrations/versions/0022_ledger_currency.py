"""Native accounting amounts beside the USD columns, for a ledger kept in another currency.

Expand only. A deployment keeps its ledger in one currency
(``docs/developer/accounting.md``): USD unless the offline, audited
``ops/accounting_currency.py`` moved it to another (CNY) with every client
stopped. Every monetary column keeps its USD meaning; this revision adds the
native ``*_amount`` column and an ``accounting_currency`` tag beside each, and
one trigger function, ``accounting_sync_amounts``, that keeps each pair in
step from the persisted ``site_settings`` policy: a writer that knows only
USD still produces the matching native amount, and a native writer its USD
equivalent. Converting existing amounts is never done here.

- the native columns and their ``<table>_accounting_amounts`` (or
  ``_accounting_budget_amounts``) triggers on the request log, the key quotas
  and budgets, the per-user, per-key and per-project spend counters, the
  provider hourly rollup, RouteWise probe samples, the account quotas,
  organization ceilings and project budgets, and the shared spend
  reservations and reconciliations;
- ``user_usage_daily.cost_amount``, the dashboard rollup's native sum. A
  rollup filled from ``api_logs`` with both sums, so no trigger; defaulted
  like its neighbours, so a fill that does not name it still inserts;
- ``accounting_currency_migrations``, the offline migration's audit table, so
  that tool issues no DDL either;
- the two compatibility bridges ``0018_projects_owned_by_accounts`` installed
  between ``account_quotas`` and ``organizations``, replaced so that on a
  non-USD ledger they carry the native amounts, never a rounded USD mirror.
  On a USD ledger they do exactly what they did.

Every column is nullable or defaulted and no earlier code names it: the
previous release keeps running on the result, and its USD-only writes get
their native amounts from the triggers. On a USD database nothing is
converted; the shared spend reservations and reconciliations a USD ledger
holds take their native amounts at the identity rate, so every shared reader
can read the native columns.

**A database that already has these objects** -- one a distribution built
with its own revisions, which installed the same function body (its digest is
:data:`SYNC_FUNCTION_SHA256` here and in ``serving.storage.accounting_schema``),
the same columns and the same triggers -- runs this revision as a no-op apart
from the two bridges, which it replaces with these: every statement checks or
``IF NOT EXISTS``-es its object first, and no amount is touched. Such a
distribution joins its own head to this revision with an empty merge revision.

Frozen like every revision: the function text, the mappings and the bridges
are copied here rather than imported from ``serving``; the service checks them
read-only at startup (``serving.storage.accounting_schema``).

Revision ID: 0022_ledger_currency
Revises: 0021_sso_identities
Create Date: 2026-11-17
"""

from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op

revision = "0022_ledger_currency"
down_revision = "0021_sso_identities"
branch_labels = None
depends_on = None
compatibility = "expand"

#: The digest of the function body below (``pg_proc.prosrc``), pinned by a test.
SYNC_FUNCTION_SHA256 = "2941954a6f1c622e20eabbf773839d3b355c582aa5cfcde8338512424475df34"

#: (table, trigger, {USD column: native column}), in the order they are created.
TRIGGERS: tuple[tuple[str, str, dict[str, str]], ...] = (
    ("api_logs", "api_logs_accounting_amounts", {"cost_usd": "cost_amount"}),
    (
        "api_keys",
        "api_keys_accounting_amounts",
        {
            "quota_daily_cost_usd": "quota_daily_cost_amount",
            "quota_monthly_cost_usd": "quota_monthly_cost_amount",
        },
    ),
    ("user_daily_cost", "user_daily_cost_accounting_amounts", {"cost_usd": "cost_amount"}),
    (
        "provider_hourly_stats",
        "provider_hourly_stats_accounting_amounts",
        {"total_cost_usd": "total_cost_amount"},
    ),
    (
        "routewise_probe_samples",
        "routewise_probe_samples_accounting_amounts",
        {"cost_usd": "cost_amount"},
    ),
    (
        "api_keys",
        "api_keys_accounting_budget_amounts",
        {"budget_daily_usd": "budget_daily_amount", "budget_monthly_usd": "budget_monthly_amount"},
    ),
    ("api_key_daily_cost", "api_key_daily_cost_accounting_amounts", {"cost_usd": "cost_amount"}),
    (
        "organizations",
        "organizations_accounting_budget_amounts",
        {"budget_daily_usd": "budget_daily_amount", "budget_monthly_usd": "budget_monthly_amount"},
    ),
    (
        "projects",
        "projects_accounting_budget_amounts",
        {"budget_daily_usd": "budget_daily_amount", "budget_monthly_usd": "budget_monthly_amount"},
    ),
    ("spend_counters", "spend_counters_accounting_amounts", {"cost_usd": "cost_amount"}),
    (
        "account_quotas",
        "account_quotas_accounting_amounts",
        {
            "quota_daily_cost_usd": "quota_daily_cost_amount",
            "quota_monthly_cost_usd": "quota_monthly_cost_amount",
        },
    ),
    (
        "spend_reservations",
        "spend_reservations_accounting_amounts",
        {"amount_usd": "amount", "cost_usd": "cost_amount"},
    ),
    (
        "spend_reconciliations",
        "spend_reconciliations_accounting_amounts",
        {"amount_usd": "amount", "cost_usd": "cost_amount"},
    ),
)

#: The request log's native columns, for tests that build ``api_logs`` in a
#: schema of their own from the frozen baseline (no trigger there).
API_LOG_STATEMENTS = tuple(
    f"ALTER TABLE api_logs ADD COLUMN IF NOT EXISTS {column} "
    + ("TEXT" if column == "accounting_currency" else "NUMERIC(24, 12)")
    for column in ("cost_amount", "accounting_currency")
)

#: The dashboard rollup's native sum, likewise.
USAGE_STATEMENTS = (
    "ALTER TABLE user_usage_daily ADD COLUMN IF NOT EXISTS cost_amount NUMERIC NOT NULL DEFAULT 0",
)

_SYNC_FUNCTION = r"""
CREATE OR REPLACE FUNCTION accounting_sync_amounts() RETURNS trigger AS $$
DECLARE
    configured jsonb;
    rate numeric := 1;
    currency text := 'USD';
    current_row jsonb := to_jsonb(NEW);
    previous_row jsonb;
    field_pair record;
    dollars numeric;
    amount numeric;
    native_changed boolean;
    tolerance numeric := 0.000000005;
BEGIN
    IF TG_TABLE_NAME = 'api_keys' THEN tolerance := 0.00005; END IF;
    IF to_regclass('site_settings') IS NOT NULL THEN
        SELECT value::jsonb INTO configured FROM site_settings WHERE key = 'accounting:policy';
    END IF;
    IF configured IS NOT NULL THEN
        currency := configured->>'currency';
        rate := (configured->>'usd_to_currency_rate')::numeric;
        IF currency NOT IN ('USD', 'CNY') OR currency IS NULL
           OR rate IS NULL OR rate <= 0 OR rate::text IN ('NaN', 'Infinity', '-Infinity')
           OR (currency = 'USD' AND rate <> 1) THEN
            RAISE EXCEPTION 'Invalid persisted accounting policy';
        END IF;
    END IF;
    IF current_row->>'accounting_currency' IS NOT NULL
       AND current_row->>'accounting_currency' <> currency
       AND current_setting('hybridinference.accounting_migration', true) IS DISTINCT FROM 'on' THEN
        RAISE EXCEPTION 'Accounting currency does not match persisted policy';
    END IF;
    IF TG_OP = 'UPDATE' THEN previous_row := to_jsonb(OLD); END IF;
    FOR field_pair IN SELECT key, value FROM jsonb_each_text(TG_ARGV[0]::jsonb) LOOP
        dollars := (current_row->>field_pair.key)::numeric;
        amount := (current_row->>field_pair.value)::numeric;
        native_changed := TG_OP = 'UPDATE'
            AND current_row->field_pair.value IS DISTINCT FROM previous_row->field_pair.value;
        IF native_changed OR (TG_OP = 'INSERT' AND amount IS NOT NULL) THEN
            IF current_setting('hybridinference.accounting_migration', true) = 'on' THEN
                NULL;
            ELSIF dollars IS NULL OR (TG_OP = 'UPDATE' AND
                current_row->field_pair.key IS NOT DISTINCT FROM previous_row->field_pair.key) THEN
                dollars := amount / rate;
            ELSIF TG_TABLE_NAME NOT IN ('provider_hourly_stats', 'user_daily_cost')
                  AND abs(dollars - amount / rate) > tolerance THEN
                RAISE EXCEPTION 'Native accounting amount and USD equivalent disagree';
            END IF;
        ELSIF TG_OP = 'INSERT'
              OR current_row->field_pair.key IS DISTINCT FROM previous_row->field_pair.key
              OR amount IS NULL THEN
            amount := dollars * rate;
        END IF;
        IF amount < 0 OR dollars < 0
           OR amount::text IN ('NaN', 'Infinity', '-Infinity')
           OR dollars::text IN ('NaN', 'Infinity', '-Infinity') THEN
            RAISE EXCEPTION 'Accounting amounts must be finite and nonnegative';
        END IF;
        current_row := current_row || jsonb_build_object(
            field_pair.key, dollars, field_pair.value, amount);
    END LOOP;
    current_row := current_row || jsonb_build_object('accounting_currency', currency);
    NEW := jsonb_populate_record(NEW, current_row);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""

_MIGRATIONS_TABLE = (
    "CREATE TABLE IF NOT EXISTS accounting_currency_migrations ("
    "id BIGSERIAL PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), "
    "previous_policy JSONB NOT NULL, target_policy JSONB NOT NULL, preview JSONB NOT NULL)"
)

#: ``0018_projects_owned_by_accounts``' bridges, with a branch for a native ledger
#: in front of their unchanged USD bodies.
MIRROR_QUOTA_FUNCTION = """
CREATE OR REPLACE FUNCTION account_quotas_mirror_to_organization() RETURNS trigger AS $$
BEGIN
    -- Written by organizations_carry_legacy_writes, which sets the row itself.
    IF pg_trigger_depth() > 1 THEN
        RETURN NULL;
    END IF;
    IF NEW.accounting_currency <> 'USD' THEN
        UPDATE organizations
        SET budget_daily_amount = NEW.quota_daily_cost_amount,
            budget_monthly_amount = NEW.quota_monthly_cost_amount
        WHERE id = 'org_p_' || NEW.user_id AND kind = 'personal'
          AND (budget_daily_amount IS DISTINCT FROM NEW.quota_daily_cost_amount
               OR budget_monthly_amount IS DISTINCT FROM NEW.quota_monthly_cost_amount);
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

    -- Native amounts are authoritative on a non-USD ledger; a rounded USD mirror must not
    -- become the next native value. The preceding accounting trigger has
    -- already validated NEW against the immutable policy.
    IF NEW.accounting_currency <> 'USD' THEN
        IF TG_OP = 'INSERT' THEN
            changed := NEW.budget_daily_amount IS NOT NULL OR NEW.budget_monthly_amount IS NOT NULL;
        ELSE
            changed := NEW.budget_daily_amount IS DISTINCT FROM OLD.budget_daily_amount
                OR NEW.budget_monthly_amount IS DISTINCT FROM OLD.budget_monthly_amount;
        END IF;
        IF changed THEN
            INSERT INTO account_quotas (user_id, quota_daily_cost_amount, quota_monthly_cost_amount)
            SELECT owner, NEW.budget_daily_amount, NEW.budget_monthly_amount
            WHERE EXISTS (SELECT 1 FROM users WHERE id = owner)
            ON CONFLICT (user_id) DO UPDATE SET
                quota_daily_cost_amount = EXCLUDED.quota_daily_cost_amount,
                quota_monthly_cost_amount = EXCLUDED.quota_monthly_cost_amount,
                updated_at = NOW();
        END IF;
        SELECT q.quota_daily_cost_amount, q.quota_monthly_cost_amount,
               q.quota_daily_cost_usd, q.quota_monthly_cost_usd
          INTO NEW.budget_daily_amount, NEW.budget_monthly_amount,
               NEW.budget_daily_usd, NEW.budget_monthly_usd
          FROM account_quotas q WHERE q.user_id = owner;
        RETURN NEW;
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

#: Refuse to give a non-USD ledger's shared rows native amounts it never had:
#: those would be USD amounts read as native ones. A non-USD ledger reaches
#: this revision only through the distribution that built it, which already
#: gave them theirs.
_SHARED_ROWS_GUARD = """
DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM site_settings WHERE key = 'accounting:policy'
             AND value::jsonb->>'currency' <> 'USD')
     AND (EXISTS (SELECT 1 FROM spend_reservations WHERE amount IS NULL)
          OR EXISTS (SELECT 1 FROM spend_reconciliations WHERE amount IS NULL)) THEN
    RAISE EXCEPTION 'shared spend rows without native amounts on a non-USD ledger'
      USING HINT = 'They were written in USD; reconcile them before this upgrade.';
  END IF;
END $$
"""


def _scalar(sql: str, **params: object) -> object:
    return op.get_bind().execute(sa.text(sql), params).scalar()


def upgrade() -> None:
    """Install what is missing: the function, the columns, the triggers, the audit table."""
    current = _scalar(
        "SELECT p.prosrc FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
        "WHERE n.nspname = current_schema() AND p.proname = 'accounting_sync_amounts' "
        "AND p.pronargs = 0"
    )
    if current != _SYNC_FUNCTION.split("$$")[1]:
        op.execute(_SYNC_FUNCTION)
    for table, trigger, pairs in TRIGGERS:
        if _scalar("SELECT to_regclass(:t)", t=table) is None:
            # A table a later revision dropped; the startup check names what a
            # ledger needs and refuses one without it.
            continue
        for column in (*pairs.values(), "accounting_currency"):
            kind = "TEXT" if column == "accounting_currency" else "NUMERIC(24, 12)"
            op.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {kind}")
        exists = _scalar(
            "SELECT EXISTS(SELECT 1 FROM pg_trigger "
            "WHERE tgrelid = CAST(:t AS regclass) AND tgname = :n)",
            t=table,
            n=trigger,
        )
        if not exists:
            mapping = json.dumps(pairs, separators=(",", ":"))
            op.execute(
                f"CREATE TRIGGER {trigger} BEFORE INSERT OR UPDATE ON {table} "
                f"FOR EACH ROW EXECUTE FUNCTION accounting_sync_amounts('{mapping}')"
            )
    if _scalar("SELECT to_regclass('user_usage_daily')") is not None:
        for statement in USAGE_STATEMENTS:
            op.execute(statement)
    op.execute(_MIGRATIONS_TABLE)
    if _scalar("SELECT to_regclass('organizations')") is not None:
        op.execute(MIRROR_QUOTA_FUNCTION)
        op.execute(CARRY_LEGACY_FUNCTION)
    op.execute(_SHARED_ROWS_GUARD)
    # A USD ledger's shared rows (a few, if any: the shared mode was never
    # active) take their native amounts at the identity rate.
    for table in ("spend_reservations", "spend_reconciliations"):
        op.execute(
            f"UPDATE {table} SET amount = amount_usd, cost_amount = cost_usd WHERE amount IS NULL"
        )


def downgrade() -> None:
    """Keep everything: nullable or defaulted columns and triggers earlier code never names.

    ``upgrade`` checks each object before creating it, so going back up after a
    downgrade still works, and no native amount is discarded.
    """
