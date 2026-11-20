"""Native accounting amounts beside the legacy USD columns: what the ledger needs.

The columns, the ``accounting_sync_amounts`` trigger function and its triggers
come from migration ``0022_ledger_currency``; the service issues no DDL. A
later revision that adds a table with money adds it here too
(``0023_sso_organizations``: the limits of an organization, and the limit it
sets for a member). This
module names them for the readers, the writers and the offline currency
migration, and checks them read-only at startup, so a database whose schema
lacks them -- one only an older release migrated, say -- stops
the gateway instead of serving with a ledger that keeps only USD.
"""

from __future__ import annotations

import hashlib
from typing import Any

#: Native column per USD column, by table. The offline currency migration
#: converts exactly these; the trigger keeps each pair in step afterwards.
AMOUNT_COLUMNS: dict[str, dict[str, str]] = {
    "api_logs": {"cost_usd": "cost_amount"},
    "api_keys": {
        "quota_daily_cost_usd": "quota_daily_cost_amount",
        "quota_monthly_cost_usd": "quota_monthly_cost_amount",
        "budget_daily_usd": "budget_daily_amount",
        "budget_monthly_usd": "budget_monthly_amount",
    },
    "user_daily_cost": {"cost_usd": "cost_amount"},
    "provider_hourly_stats": {"total_cost_usd": "total_cost_amount"},
    "routewise_probe_samples": {"cost_usd": "cost_amount"},
    "api_key_daily_cost": {"cost_usd": "cost_amount"},
    "organizations": {
        "budget_daily_usd": "budget_daily_amount",
        "budget_monthly_usd": "budget_monthly_amount",
    },
    "projects": {
        "budget_daily_usd": "budget_daily_amount",
        "budget_monthly_usd": "budget_monthly_amount",
    },
    "spend_counters": {"cost_usd": "cost_amount"},
    "account_quotas": {
        "quota_daily_cost_usd": "quota_daily_cost_amount",
        "quota_monthly_cost_usd": "quota_monthly_cost_amount",
    },
    "spend_reservations": {"amount_usd": "amount", "cost_usd": "cost_amount"},
    "spend_reconciliations": {"amount_usd": "amount", "cost_usd": "cost_amount"},
    "sso_organizations": {
        "monthly_limit_usd": "monthly_limit_amount",
        "default_member_monthly_limit_usd": "default_member_monthly_limit_amount",
    },
    "sso_organization_members": {"monthly_limit_usd": "monthly_limit_amount"},
}

#: Rollups that keep a native sum beside their USD one with no trigger and no
#: currency tag: filled from ``api_logs`` with both sums (see
#: ``0022_ledger_currency``). The offline currency migration converts
#: them too, so a stored day reads as its converted rows would.
ROLLUP_AMOUNT_COLUMNS: dict[str, dict[str, str]] = {
    "user_usage_daily": {"cost_usd": "cost_amount"},
}

#: The triggers that keep each table's pairs in step, by table.
ACCOUNTING_TRIGGERS: dict[str, tuple[str, ...]] = {
    "api_logs": ("api_logs_accounting_amounts",),
    "api_keys": ("api_keys_accounting_amounts", "api_keys_accounting_budget_amounts"),
    "user_daily_cost": ("user_daily_cost_accounting_amounts",),
    "provider_hourly_stats": ("provider_hourly_stats_accounting_amounts",),
    "routewise_probe_samples": ("routewise_probe_samples_accounting_amounts",),
    "api_key_daily_cost": ("api_key_daily_cost_accounting_amounts",),
    "organizations": ("organizations_accounting_budget_amounts",),
    "projects": ("projects_accounting_budget_amounts",),
    "spend_counters": ("spend_counters_accounting_amounts",),
    "account_quotas": ("account_quotas_accounting_amounts",),
    "spend_reservations": ("spend_reservations_accounting_amounts",),
    "spend_reconciliations": ("spend_reconciliations_accounting_amounts",),
    "sso_organizations": ("sso_organizations_accounting_amounts",),
    "sso_organization_members": ("sso_organization_members_accounting_amounts",),
}

#: sha256 of the reviewed ``accounting_sync_amounts`` body (``pg_proc.prosrc``),
#: the one ``0022_ledger_currency`` installs. A database a distribution built
#: with the same function passes as it is. A test pins it to the revision's text.
SYNC_FUNCTION_SHA256 = "2941954a6f1c622e20eabbf773839d3b355c582aa5cfcde8338512424475df34"

#: The offline migration's audit table, also from ``0022_ledger_currency``.
MIGRATIONS_TABLE = "accounting_currency_migrations"


class AccountingSchemaUnavailable(RuntimeError):
    """The native accounting schema is missing; monetary requests cannot be served."""


async def existing_columns(conn: Any, table: str) -> set[str]:
    """Return the column names *table* has in the current schema."""
    rows = await conn.fetch(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = $1",
        table,
    )
    return {row["column_name"] for row in rows}


async def accounting_schema_problems(conn: Any) -> list[str]:
    """Describe what the native accounting schema lacks; empty when complete.

    Read-only: catalog queries on an asyncpg connection or pool.
    """
    problems: list[str] = []
    body = await conn.fetchval(
        "SELECT p.prosrc FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
        "WHERE n.nspname = current_schema() AND p.proname = 'accounting_sync_amounts' "
        "AND p.pronargs = 0"
    )
    if body is None:
        problems.append("function accounting_sync_amounts is missing")
    elif hashlib.sha256(body.encode()).hexdigest() != SYNC_FUNCTION_SHA256:
        problems.append("function accounting_sync_amounts is not the reviewed one")
    for table, pairs in AMOUNT_COLUMNS.items():
        if not await conn.fetchval("SELECT to_regclass($1)", table):
            problems.append(f"table {table} is missing")
            continue
        missing = {*pairs.values(), "accounting_currency"} - await existing_columns(conn, table)
        problems.extend(f"column {table}.{name} is missing" for name in sorted(missing))
        for trigger in ACCOUNTING_TRIGGERS[table]:
            enabled = await conn.fetchval(
                "SELECT tgenabled <> 'D' FROM pg_trigger "
                "WHERE tgrelid = $1::regclass AND tgname = $2",
                table,
                trigger,
            )
            if enabled is None:
                problems.append(f"trigger {trigger} is missing")
            elif not enabled:
                problems.append(f"trigger {trigger} is disabled")
    for table, pairs in ROLLUP_AMOUNT_COLUMNS.items():
        if not await conn.fetchval("SELECT to_regclass($1)", table):
            problems.append(f"table {table} is missing")
            continue
        missing = set(pairs.values()) - await existing_columns(conn, table)
        problems.extend(f"column {table}.{name} is missing" for name in sorted(missing))
    if not await conn.fetchval("SELECT to_regclass($1)", MIGRATIONS_TABLE):
        problems.append(f"table {MIGRATIONS_TABLE} is missing")
    return problems


async def require_accounting_schema(conn: Any) -> None:
    """Refuse a database without the native accounting schema.

    Raises:
        AccountingSchemaUnavailable: A column, trigger, the trigger function or
            the migration audit table is missing (or the function differs from
            the reviewed one). Apply the migrations with this release's image.
    """
    problems = await accounting_schema_problems(conn)
    if problems:
        raise AccountingSchemaUnavailable(
            "The native accounting schema is incomplete ("
            + "; ".join(problems)
            + "): run `python -m migrations upgrade` with this image before starting."
        )
