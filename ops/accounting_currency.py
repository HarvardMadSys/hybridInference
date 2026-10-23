#!/usr/bin/env python3
"""Preview or apply an offline, audited USD-to-native accounting migration.

Run with PYTHONPATH=apps/backend. The default is read-only. Stop every gateway,
rollup and maintenance client before --apply; connection checks never terminate
other clients. DB connection settings use the gateway's existing configuration.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from decimal import Decimal
from typing import Any

from serving.accounting import MIGRATION_KEY, POLICY_KEY, AccountingPolicy
from serving.storage.accounting_schema import AMOUNT_COLUMNS, initialize_accounting_schema
from serving.storage.log_schema import existing_columns


async def _read_policy(conn: Any) -> AccountingPolicy:
    raw = await conn.fetchval("SELECT value FROM site_settings WHERE key = $1", POLICY_KEY)
    return AccountingPolicy() if raw is None else AccountingPolicy.from_dict(json.loads(raw))


async def _migration_complete(conn: Any, policy: AccountingPolicy) -> bool:
    if policy.currency == "USD":
        return True
    raw = await conn.fetchval("SELECT value FROM site_settings WHERE key = $1", MIGRATION_KEY)
    return raw is not None and AccountingPolicy.from_dict(json.loads(raw)) == policy


async def _require_offline(conn: Any) -> None:
    others = await conn.fetchval(
        "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
        "AND pid <> pg_backend_pid() AND backend_type = 'client backend'"
    )
    if others:
        raise RuntimeError(
            f"Refusing currency migration: {others} other database clients remain; "
            "stop all gateway workers, rollups and database tools first"
        )


async def preview_currency_migration(conn: Any, policy: AccountingPolicy) -> dict[str, Any]:
    """Read aggregate counts/totals only; never disclose individual account data."""
    previous = await _read_policy(conn)
    complete = await _migration_complete(conn, previous)
    tables = {}
    for table, columns in AMOUNT_COLUMNS.items():
        if not await conn.fetchval("SELECT to_regclass($1)", table):
            continue
        fields = {}
        present = await existing_columns(conn, table)
        for legacy, native in columns.items():
            target = f"round({legacy}::numeric * $1::numeric, 12)"
            if previous == policy and complete and native in present:
                target = f"COALESCE({native}, {target})"
            row = await conn.fetchrow(
                f"SELECT count(*) AS rows, count({legacy}) AS priced_rows, "
                f"sum({legacy}) AS total, sum({target}) AS native_total FROM {table}",
                policy.usd_to_currency_rate,
            )
            total = row["total"]
            fields[native] = {
                "rows": row["rows"],
                "priced_rows": row["priced_rows"],
                "original_usd_total": str(total) if total is not None else None,
                "native_total": str(row["native_total"])
                if row["native_total"] is not None
                else None,
            }
        tables[table] = fields
    return {
        "previous_policy": previous.as_dict(),
        "target_policy": policy.as_dict(),
        "tables": tables,
        "applied": False,
        "already_applied": previous == policy and complete,
        "migration_complete": complete,
    }


async def migrate_currency(
    conn: Any, policy: AccountingPolicy, *, apply: bool = False
) -> dict[str, Any]:
    """Keep all USD evidence unchanged and fill native columns in one transaction."""
    if not apply:
        return await preview_currency_migration(conn, policy)
    await _require_offline(conn)
    async with conn.transaction(isolation="serializable"):
        await conn.execute("SET LOCAL lock_timeout = '5s'")
        await conn.execute("SELECT pg_advisory_xact_lock(731402119)")
        await conn.execute("LOCK TABLE site_settings IN ACCESS EXCLUSIVE MODE")
        previous = await _read_policy(conn)
        if previous == policy:
            if not await _migration_complete(conn, policy):
                raise ValueError("Persisted CNY policy has no matching completed migration")
            result = await preview_currency_migration(conn, policy)
            result["already_applied"] = True
            return result
        if previous.currency != "USD":
            raise ValueError(
                "Accounting policy is immutable after activation; refusing another conversion"
            )
        existing = [
            table
            for table in AMOUNT_COLUMNS
            if await conn.fetchval("SELECT to_regclass($1)", table)
        ]
        for table in existing:
            await conn.execute(f"LOCK TABLE {table} IN ACCESS EXCLUSIVE MODE")
        await _require_offline(conn)
        result = await preview_currency_migration(conn, policy)
        await initialize_accounting_schema(conn, tables=tuple(existing))
        await conn.execute("SET LOCAL hybridinference.accounting_migration = 'on'")
        await conn.execute(
            "INSERT INTO site_settings (key, value, value_type, updated_at, updated_by) "
            "VALUES ($1, $2, 'json', NOW(), 'offline-currency-migration') "
            "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, value_type='json', "
            "updated_at=NOW(), updated_by=EXCLUDED.updated_by",
            POLICY_KEY,
            json.dumps(policy.as_dict(), sort_keys=True),
        )
        for table in existing:
            clauses = [
                f"{native} = {legacy}::numeric * $1::numeric"
                for legacy, native in AMOUNT_COLUMNS[table].items()
            ]
            clauses.append("accounting_currency = $2")
            await conn.execute(
                f"UPDATE {table} SET {', '.join(clauses)}",
                policy.usd_to_currency_rate,
                policy.currency,
            )
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS accounting_currency_migrations ("
            "id BIGSERIAL PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), "
            "previous_policy JSONB NOT NULL, target_policy JSONB NOT NULL, preview JSONB NOT NULL)"
        )
        await conn.execute(
            "INSERT INTO accounting_currency_migrations (previous_policy, target_policy, preview) "
            "VALUES ($1::jsonb, $2::jsonb, $3::jsonb)",
            json.dumps(previous.as_dict()),
            json.dumps(policy.as_dict()),
            json.dumps(result),
        )
        await conn.execute(
            "INSERT INTO site_settings (key, value, value_type, updated_at, updated_by) "
            "VALUES ($1, $2, 'json', NOW(), 'offline-currency-migration') "
            "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, value_type='json', "
            "updated_at=NOW(), updated_by=EXCLUDED.updated_by",
            MIGRATION_KEY,
            json.dumps(policy.as_dict(), sort_keys=True),
        )
        result["migration_complete"] = True
        result["applied"] = True
        return result


async def _main(args: argparse.Namespace) -> None:
    import asyncpg

    from serving.config.settings import get_settings

    policy = AccountingPolicy(
        currency=args.currency,
        usd_to_currency_rate=Decimal(args.usd_to_currency_rate),
        rate_date=args.rate_date,
        rate_source=args.rate_source,
    )
    settings = get_settings()
    conn = await asyncpg.connect(
        host=settings.db_host,
        port=settings.db_port,
        user=settings.db_user,
        password=settings.db_password,
        database=settings.db_name,
        server_settings={"application_name": "accounting-currency-migration"},
    )
    try:
        print(json.dumps(await migrate_currency(conn, policy, apply=args.apply), indent=2))
    finally:
        await conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--currency", choices=("USD", "CNY"), required=True)
    parser.add_argument("--usd-to-currency-rate", required=True)
    parser.add_argument("--rate-date", default="")
    parser.add_argument("--rate-source", default="")
    parser.add_argument(
        "--apply", action="store_true", help="Apply after all other database clients stop"
    )
    asyncio.run(_main(parser.parse_args()))
