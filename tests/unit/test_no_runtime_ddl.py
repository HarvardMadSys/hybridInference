"""The service issues no DDL: the schema comes from the migrations alone.

Startup used to build and alter tables, and so did a few request paths (loading
and writing the configuration, the public stats snapshot). All of that is
``0001_baseline`` now, and a schema change is a revision. This scan keeps it so:
no string in ``serving`` may carry a DDL statement, except the performance
indexes ``perf_metrics_index`` builds in the background (``CREATE INDEX
CONCURRENTLY``), the one kind of object the design lets be missing at runtime.
``tests/integration/test_no_runtime_ddl_postgres.py`` checks the same at runtime.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

SERVING = Path(__file__).resolve().parents[2] / "apps" / "backend" / "serving"

#: Background builders allowed to issue DDL, and what they may issue.
ALLOWED = {"storage/perf_metrics_index.py": re.compile(r"\bINDEX\s+CONCURRENTLY\b", re.I)}

_DDL = re.compile(
    r"\b(CREATE\s+(UNIQUE\s+)?(TABLE|INDEX|SEQUENCE|VIEW|MATERIALIZED\s+VIEW|TYPE|EXTENSION|SCHEMA)"
    r"|CREATE\s+(OR\s+REPLACE\s+)?(FUNCTION|TRIGGER|PROCEDURE)"
    r"|ALTER\s+(TABLE|INDEX|SEQUENCE|TYPE|FUNCTION)"
    r"|DROP\s+(TABLE|INDEX|SEQUENCE|VIEW|TYPE|FUNCTION|TRIGGER|SCHEMA)"
    r"|ADD\s+COLUMN|DROP\s+COLUMN|ADD\s+CONSTRAINT|DROP\s+CONSTRAINT)\b",
    re.I,
)


def _strings(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node.lineno, node.value
        elif isinstance(node, ast.JoinedStr):
            text = "".join(v.value for v in node.values if isinstance(v, ast.Constant))
            yield node.lineno, text


def ddl_in(source: str, allowed: re.Pattern[str] | None = None) -> list[tuple[int, str]]:
    """``(line, statement)`` for each string in *source* that reads as DDL."""
    found = []
    for line, text in _strings(ast.parse(source)):
        match = _DDL.search(text)
        if match and not (allowed and allowed.search(text)):
            found.append((line, match.group(0)))
    return found


def test_the_service_issues_no_ddl():
    offenders = []
    for path in sorted(SERVING.rglob("*.py")):
        relative = path.relative_to(SERVING).as_posix()
        for line, statement in ddl_in(path.read_text(encoding="utf-8"), ALLOWED.get(relative)):
            offenders.append(f"serving/{relative}:{line}: {statement}")
    assert not offenders, (
        "Schema changes belong in apps/backend/migrations/versions/, not in the service:\n  "
        + "\n  ".join(offenders)
    )


def test_the_allowed_background_builder_still_exists():
    """A stale allowance would let the next file of that name build tables unnoticed."""
    for relative, allowed in ALLOWED.items():
        assert allowed.search((SERVING / relative).read_text(encoding="utf-8")), relative


@pytest.mark.parametrize(
    "source",
    [
        'await conn.execute("CREATE TABLE IF NOT EXISTS t (id INT)")',
        'await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS note TEXT")',
        'await conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{name} ON t (id)")',
        'SQL = """\n  DROP TABLE legacy\n"""',
    ],
)
def test_the_scan_sees_ddl(source):
    assert ddl_in(source)


@pytest.mark.parametrize(
    "source",
    [
        'await conn.execute("UPDATE users SET role = $1")',
        'await conn.fetch("SELECT * FROM api_logs WHERE created_at > NOW()")',
        '"""Created by the schema migrations."""',
    ],
)
def test_the_scan_lets_data_statements_through(source):
    assert not ddl_in(source)
