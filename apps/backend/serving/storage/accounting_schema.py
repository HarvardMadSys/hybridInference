"""Add native accounting amounts without changing legacy USD column semantics."""

from __future__ import annotations

from typing import Any

from serving.storage.log_schema import (
    SchemaLockUnavailable,
    bounded_ddl,
    execute_ddl,
    existing_columns,
)

AMOUNT_COLUMNS = {
    "api_logs": {"cost_usd": "cost_amount"},
    "api_keys": {
        "quota_daily_cost_usd": "quota_daily_cost_amount",
        "quota_monthly_cost_usd": "quota_monthly_cost_amount",
    },
    "user_daily_cost": {"cost_usd": "cost_amount"},
    "provider_hourly_stats": {"total_cost_usd": "total_cost_amount"},
    "routewise_probe_samples": {"cost_usd": "cost_amount"},
}

_SYNC_FUNCTION = """
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


class AccountingSchemaUnavailable(RuntimeError):
    """Native money columns cannot be deferred while serving monetary requests."""


async def initialize_accounting_schema(conn: Any, *, tables: tuple[str, ...] | None = None) -> None:
    """Install additive columns; a bounded lock failure must stop startup."""
    try:
        async with bounded_ddl(conn), conn.transaction():
            await execute_ddl(conn, "SELECT pg_advisory_xact_lock(731402120)")
            await _initialize_accounting_schema(conn, tables=tables)
    except SchemaLockUnavailable as exc:
        raise AccountingSchemaUnavailable(
            "Accounting columns must be installed before serving; retry after database readers finish"
        ) from exc


async def _initialize_accounting_schema(conn: Any, *, tables: tuple[str, ...] | None) -> None:
    import json

    selected = tables if tables is not None else tuple(AMOUNT_COLUMNS)
    if set(selected) - AMOUNT_COLUMNS.keys():
        raise ValueError("unknown accounting table")
    current_function = await conn.fetchval(
        "SELECT p.prosrc FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
        "WHERE n.nspname=current_schema() AND p.proname='accounting_sync_amounts' "
        "AND p.pronargs=0"
    )
    if current_function != _SYNC_FUNCTION.split("$$")[1]:
        await execute_ddl(conn, _SYNC_FUNCTION)
    for table in selected:
        if not await conn.fetchval("SELECT to_regclass($1)", table):
            continue
        pairs = AMOUNT_COLUMNS[table]
        columns = [
            (name, f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {name} NUMERIC(24, 12)")
            for name in pairs.values()
        ]
        columns.append(
            (
                "accounting_currency",
                f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS accounting_currency TEXT",
            )
        )
        present = await existing_columns(conn, table)
        for name, ddl in columns:
            if name not in present:
                await execute_ddl(conn, ddl)
        trigger_name = f"{table}_accounting_amounts"
        exists = await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM pg_trigger WHERE tgrelid = $1::regclass AND tgname = $2)",
            table,
            trigger_name,
        )
        if not exists:
            mapping = json.dumps(pairs, separators=(",", ":"))
            await execute_ddl(
                conn,
                f"CREATE TRIGGER {trigger_name} BEFORE INSERT OR UPDATE ON {table} "
                f"FOR EACH ROW EXECUTE FUNCTION accounting_sync_amounts('{mapping}')",
            )
