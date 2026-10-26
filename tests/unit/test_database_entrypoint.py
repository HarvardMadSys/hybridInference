"""No test reaches PostgreSQL except through ``tests/fixtures/postgres.py``.

The suite used to have about thirty private ways to find the test database:
``TEST_DB_*`` read field by field, ``TEST_PG_DSN``, ``PG_TEST_DSN``, the
gateway's own ``DB_*`` with a ``localhost:5432`` fallback. Each also decided for
itself what a failed connection meant, and most chose ``pytest.skip`` -- so when
CI exported one variable and not another, hundreds of database tests skipped and
the run stayed green. CI then grepped its own logs for skip wording, which a new
phrasing could slip past.

This scans the test tree instead. Outside the entry point it fails on:

* reading ``TEST_DB_*``, ``TEST_PG_DSN`` or ``PG_TEST_DSN`` from the environment
  (``os.getenv``, ``os.environ[...]`` / ``.get``, ``in os.environ``);
* opening a connection with ``asyncpg.connect`` / ``asyncpg.create_pool``
  directly, which bypasses the target check and the skip-or-fail decision.

Setting those variables (``monkeypatch.setenv``) is fine; that is how the entry
point's own tests drive it. Code that hands connection settings to gateway code
(``DatabaseLogger(...)``) gets them from the entry point and is not affected.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCANNED = [REPO / "tests", *sorted((REPO / "distributions").glob("*/tests"))]

ENTRYPOINT = "tests/fixtures/postgres.py"
#: Files that may do either, and why. Keep this short.
ALLOWED: dict[str, str] = {
    ENTRYPOINT: "the entry point itself",
}

DATABASE_ENV = {
    "TEST_DB_HOST",
    "TEST_DB_PORT",
    "TEST_DB_USER",
    "TEST_DB_PASSWORD",
    "TEST_DB_NAME",
    "TEST_DB_REQUIRED",
    "TEST_PG_DSN",
    "PG_TEST_DSN",
}
CONNECT_FUNCTIONS = {"connect", "create_pool"}


def _is_os_environ(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "environ"
        and isinstance(node.value, ast.Name)
        and node.value.id == "os"
    )


def _is_env_getter(func: ast.AST) -> bool:
    if not isinstance(func, ast.Attribute):
        return False
    if func.attr == "getenv":
        return isinstance(func.value, ast.Name) and func.value.id == "os"
    return func.attr == "get" and _is_os_environ(func.value)


def _literal(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _env_reads(tree: ast.AST) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        name: str | None = None
        # os.getenv("X") / os.environ.get("X")
        if isinstance(node, ast.Call) and node.args and _is_env_getter(node.func):
            name = _literal(node.args[0])
        # os.environ["X"] read
        elif (
            isinstance(node, ast.Subscript)
            and isinstance(node.ctx, ast.Load)
            and _is_os_environ(node.value)
        ):
            name = _literal(node.slice)
        # "X" in os.environ
        elif (
            isinstance(node, ast.Compare)
            and len(node.ops) == 1
            and isinstance(node.ops[0], ast.In | ast.NotIn)
            and _is_os_environ(node.comparators[0])
        ):
            name = _literal(node.left)
        if name in DATABASE_ENV:
            found.append((node.lineno, f"reads {name} from the environment"))
    return found


def _direct_connects(tree: ast.AST) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in CONNECT_FUNCTIONS
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "asyncpg"
        ):
            found.append((node.lineno, f"calls asyncpg.{node.func.attr}() directly"))
        elif isinstance(node, ast.ImportFrom) and node.module == "asyncpg":
            for alias in node.names:
                if alias.name in CONNECT_FUNCTIONS:
                    found.append((node.lineno, f"imports {alias.name} from asyncpg"))
    return found


def _violations() -> list[str]:
    problems: list[str] = []
    for root in SCANNED:
        for path in sorted(root.rglob("*.py")):
            relative = path.relative_to(REPO).as_posix()
            if relative in ALLOWED:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
            for line, what in _env_reads(tree) + _direct_connects(tree):
                problems.append(f"{relative}:{line}: {what}")
    return problems


def test_database_is_reached_only_through_the_entrypoint() -> None:
    problems = _violations()
    assert not problems, (
        "Use tests/fixtures/postgres.py (target(), connect(), create_pool(), worker_dsn()) "
        "instead:\n  " + "\n  ".join(problems)
    )


def test_allowed_files_exist() -> None:
    """A stale entry would silently widen the exemption to a file added later."""
    missing = [path for path in ALLOWED if not (REPO / path).is_file()]
    assert not missing, missing


def _scan(source: str) -> list[str]:
    tree = ast.parse(source)
    return [what for _, what in _env_reads(tree) + _direct_connects(tree)]


def test_the_scan_sees_each_way_of_reading_the_database_variables() -> None:
    source = """
import os
import asyncpg
from asyncpg import create_pool

os.getenv("TEST_DB_PORT")
os.environ.get("TEST_PG_DSN")
os.environ["PG_TEST_DSN"]
"TEST_DB_REQUIRED" in os.environ
asyncpg.connect(dsn)
asyncpg.create_pool(dsn)
"""
    assert sorted(_scan(source)) == sorted(
        [
            "reads TEST_DB_PORT from the environment",
            "reads TEST_PG_DSN from the environment",
            "reads PG_TEST_DSN from the environment",
            "reads TEST_DB_REQUIRED from the environment",
            "calls asyncpg.connect() directly",
            "calls asyncpg.create_pool() directly",
            "imports create_pool from asyncpg",
        ]
    )


def test_the_scan_allows_setting_the_variables_and_unrelated_reads() -> None:
    source = """
import os
monkeypatch.setenv("TEST_DB_HOST", "db")
os.environ["TEST_DB_NAME"] = "x_test_db"
os.getenv("DB_HOST")
pool.acquire()
"""
    assert _scan(source) == []


# --- one erasure-fence secret ------------------------------------------------

#: Literal fence secrets other than the suite's, and why each may stay. Keep short.
FENCE_SECRET_LITERALS: dict[tuple[str, str], str] = {
    (
        "tests/integration/storage/test_erasure_fence_races.py",
        "different-secret",
    ): "asserts another secret cannot see a fence; it never pins one",
    (
        "tests/unit/storage/test_schema_lock_timeout.py",
        "test-secret",
    ): "a fake connection; no database is involved",
    (
        "tests/unit/observability/test_alert_history_review_boundaries.py",
        "synthetic-review-fence",
    ): "derives digests in memory only; no database is involved",
}


def _fence_secret_literals(tree: ast.AST) -> list[tuple[int, str]]:
    """``fence_secret="..."`` arguments and ``x.fence_secret = "..."`` assignments."""
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for keyword in node.keywords:
                value = _literal(keyword.value) if keyword.arg == "fence_secret" else None
                if value is not None:
                    found.append((node.lineno, value))
        elif isinstance(node, ast.Assign):
            value = _literal(node.value)
            if value is not None and any(
                isinstance(target, ast.Attribute) and target.attr == "fence_secret"
                for target in node.targets
            ):
                found.append((node.lineno, value))
    return found


def test_every_test_uses_the_one_fence_secret() -> None:
    """The fence pins the first secret it sees into a database, and test
    databases are shared and reused; a second literal fails whichever suite runs
    second. Use ``postgres.FENCE_SECRET``."""
    problems = []
    for root in SCANNED:
        for path in sorted(root.rglob("*.py")):
            relative = path.relative_to(REPO).as_posix()
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
            for line, value in _fence_secret_literals(tree):
                if (relative, value) not in FENCE_SECRET_LITERALS:
                    problems.append(f"{relative}:{line}: fence_secret={value!r}")
    assert not problems, "Use tests.fixtures.postgres.FENCE_SECRET:\n  " + "\n  ".join(problems)


def test_fence_secret_exemptions_are_still_used() -> None:
    stale = []
    for (relative, value), _why in FENCE_SECRET_LITERALS.items():
        tree = ast.parse((REPO / relative).read_text(encoding="utf-8"))
        if value not in {found for _line, found in _fence_secret_literals(tree)}:
            stale.append((relative, value))
    assert not stale, stale


def test_the_fence_scan_sees_arguments_and_assignments() -> None:
    tree = ast.parse(
        'PostgresLogStore(pool, fence_secret="a")\n'
        'store.fence_secret = "b"\n'
        "DatabaseLogger({}, fence_secret=FENCE_SECRET)\n"
    )
    assert sorted(_fence_secret_literals(tree)) == [(1, "a"), (2, "b")]
