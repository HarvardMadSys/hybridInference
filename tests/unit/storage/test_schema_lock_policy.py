"""No bare strong-lock DDL in the boot-time schema builders.

``ALTER TABLE`` (and friends) acquire ``ACCESS EXCLUSIVE`` *before* Postgres
evaluates ``IF [NOT] EXISTS``, so an idempotent no-op migration still queues a
lock that parks every query behind it. ``serving.storage.log_schema`` owns the
safe machinery: catalog first, DDL only when actually missing, always under a
bounded ``lock_timeout``.

The 2026-08-21 production outage was a bare ``ALTER TABLE api_keys ADD COLUMN
IF NOT EXISTS ...`` in ``database.py`` queueing behind the nightly ``pg_dump``:
the first container restart inside the backup window waited out the pool's 60 s
command timeout three times and failed the deploy — and while it waited, every
auth read on ``api_keys`` queued behind its exclusive request. This test pins
the refactor that removed that class: the builders may only issue strong-lock
DDL through the log_schema helpers, never through a bare ``conn.execute``.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_STORAGE = Path(__file__).resolve().parents[3] / "apps" / "backend" / "serving" / "storage"

# Statement fragments that always take ACCESS EXCLUSIVE on the target table.
# (CREATE TABLE/INDEX are deliberately absent: their locks do not conflict with
# the long ACCESS SHARE readers — pg_dump — that this policy defends against.)
_STRONG_LOCK_FRAGMENTS = (
    "ADD COLUMN",
    "DROP COLUMN",
    "ADD CONSTRAINT",
    "DROP CONSTRAINT",
    "SET DEFAULT",
    "SET NOT NULL",
    "DO $$",
)


def _string_payload(node: ast.Call) -> str:
    """Every string constant anywhere inside the call, joined."""
    parts: list[str] = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            parts.append(sub.value)
    return " ".join(parts)


def _bare_execute_calls(path: Path) -> list[tuple[int, str]]:
    """(lineno, offending fragment) for each ``conn.execute`` carrying DDL."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    offenders: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "execute"):
            continue
        payload = _string_payload(node).upper()
        for fragment in _STRONG_LOCK_FRAGMENTS:
            if fragment in payload:
                offenders.append((node.lineno, fragment))
                break
    return offenders


@pytest.mark.parametrize("filename", ["database.py", "postgres_operational.py"])
def test_schema_builders_take_no_bare_strong_locks(filename: str) -> None:
    offenders = _bare_execute_calls(_STORAGE / filename)

    assert offenders == [], (
        f"{filename} issues strong-lock DDL through a bare conn.execute at "
        f"{offenders}; route it through serving.storage.log_schema "
        "(apply_column_migrations / drop_columns_if_present / bounded_ddl + "
        "execute_ddl) so the settled startup path takes no exclusive lock"
    )
