"""A comparable description of a database's schema, and the frozen baseline's.

:func:`describe` reads the catalog into plain data: every table's columns (type,
nullability, default), constraints, indexes, triggers, functions, sequences,
views and storage parameters, keyed by name so that column order (which differs
between a fresh build and an upgraded database) does not count. The migration
metadata tables are left out.

``baseline_schema.json`` next to this module is that description of the schema
the gateway built at startup, recorded when the builders were frozen into
``migrations.baseline``. Regenerate it only together with a deliberate change to
the baseline, which is to say never::

    python -m tests.fixtures.schema_snapshot write
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

SNAPSHOT_PATH = Path(__file__).with_name("baseline_schema.json")

_EXCLUDED = ("hybridinference_alembic_version", "hybridinference_schema_compat")

_COLUMNS_SQL = """
SELECT c.relname AS table_name, a.attname AS column_name,
       format_type(a.atttypid, a.atttypmod) AS data_type,
       a.attnotnull AS not_null,
       pg_get_expr(d.adbin, d.adrelid) AS default_expr,
       a.attidentity::text AS identity, a.attgenerated::text AS generated
FROM pg_attribute a
JOIN pg_class c ON c.oid = a.attrelid
LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
WHERE c.relnamespace = 'public'::regnamespace
  AND c.relkind IN ('r', 'p')
  AND a.attnum > 0 AND NOT a.attisdropped
"""

_CONSTRAINTS_SQL = """
SELECT c.relname AS table_name, k.conname, pg_get_constraintdef(k.oid) AS definition
FROM pg_constraint k JOIN pg_class c ON c.oid = k.conrelid
WHERE c.relnamespace = 'public'::regnamespace
"""

_INDEXES_SQL = "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = 'public'"

_TRIGGERS_SQL = """
SELECT c.relname AS table_name, t.tgname, pg_get_triggerdef(t.oid) AS definition
FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid
WHERE c.relnamespace = 'public'::regnamespace AND NOT t.tgisinternal
"""

_FUNCTIONS_SQL = """
SELECT p.proname || '(' || pg_get_function_identity_arguments(p.oid) || ')' AS signature,
       pg_get_functiondef(p.oid) AS definition
FROM pg_proc p
WHERE p.pronamespace = 'public'::regnamespace AND p.prokind = 'f'
"""

_RELATIONS_SQL = """
SELECT c.relname, c.relkind::text AS relkind, c.reloptions,
       CASE WHEN c.relkind IN ('v', 'm') THEN pg_get_viewdef(c.oid) END AS view_definition
FROM pg_class c
WHERE c.relnamespace = 'public'::regnamespace AND c.relkind IN ('r', 'p', 'S', 'v', 'm')
"""


async def describe(conn: Any) -> dict[str, Any]:
    """Describe the ``public`` schema on an asyncpg connection, as JSON-ready data."""
    tables: dict[str, dict[str, Any]] = {}
    for r in await conn.fetch(_COLUMNS_SQL):
        if r["table_name"] in _EXCLUDED:
            continue
        tables.setdefault(r["table_name"], {})[r["column_name"]] = {
            "type": r["data_type"],
            "not_null": r["not_null"],
            "default": r["default_expr"],
            "identity": r["identity"] or None,
            "generated": r["generated"] or None,
        }
    relations = [r for r in await conn.fetch(_RELATIONS_SQL) if r["relname"] not in _EXCLUDED]
    return {
        "tables": {name: dict(sorted(cols.items())) for name, cols in sorted(tables.items())},
        "constraints": dict(
            sorted(
                (f"{r['table_name']}.{r['conname']}", r["definition"])
                for r in await conn.fetch(_CONSTRAINTS_SQL)
                if r["table_name"] not in _EXCLUDED
            )
        ),
        "indexes": dict(
            sorted(
                (r["indexname"], r["indexdef"])
                for r in await conn.fetch(_INDEXES_SQL)
                if not any(r["indexname"].startswith(t) for t in _EXCLUDED)
            )
        ),
        "triggers": dict(
            sorted(
                (f"{r['table_name']}.{r['tgname']}", r["definition"])
                for r in await conn.fetch(_TRIGGERS_SQL)
            )
        ),
        "functions": dict(
            sorted((r["signature"], r["definition"]) for r in await conn.fetch(_FUNCTIONS_SQL))
        ),
        "sequences": sorted(r["relname"] for r in relations if r["relkind"] == "S"),
        "views": dict(
            sorted((r["relname"], r["view_definition"]) for r in relations if r["relkind"] in "vm")
        ),
        "storage_parameters": dict(
            sorted(
                (r["relname"], sorted(r["reloptions"]))
                for r in relations
                if r["relkind"] in "rp" and r["reloptions"]
            )
        ),
    }


def load() -> dict[str, Any]:
    """The recorded description of the frozen baseline's schema."""
    return json.loads(SNAPSHOT_PATH.read_text())


def differences(expected: dict[str, Any], actual: dict[str, Any]) -> list[str]:
    """Readable differences between two descriptions; empty when they match."""
    out: list[str] = []
    for section in sorted(set(expected) | set(actual)):
        want, got = expected.get(section), actual.get(section)
        if want == got:
            continue
        if isinstance(want, dict) and isinstance(got, dict):
            for key in sorted(set(want) | set(got)):
                if key not in got:
                    out.append(f"{section}: missing {key}")
                elif key not in want:
                    out.append(f"{section}: unexpected {key}")
                elif want[key] != got[key]:
                    out.append(
                        f"{section}: {key} differs: expected {want[key]!r}, got {got[key]!r}"
                    )
        else:
            out.append(f"{section}: expected {want!r}, got {got!r}")
    return out


async def _write(database: str) -> None:
    from tests.fixtures import postgres

    conn = await postgres.connect(database=database)
    try:
        SNAPSHOT_PATH.write_text(json.dumps(await describe(conn), indent=1, sort_keys=True) + "\n")
    finally:
        await conn.close()


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Record the baseline schema description.")
    parser.add_argument("command", choices=["write"])
    parser.add_argument("--database", required=True, help="a database holding exactly the baseline")
    args = parser.parse_args(argv)
    asyncio.run(_write(args.database))
    print(f"wrote {SNAPSHOT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
