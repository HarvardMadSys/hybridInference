"""Rules every schema migration keeps, checked without a database."""

from __future__ import annotations

import ast
import hashlib
import re
from pathlib import Path

import pytest

from migrations import MIGRATIONS_DIR
from migrations.compat import code_chain

VERSIONS = MIGRATIONS_DIR / "versions"
BASELINE = MIGRATIONS_DIR / "baseline"
REPO = MIGRATIONS_DIR.parents[2]

# Changing a frozen file changes what 0001_baseline did to every database that
# already ran it. A schema change is a new revision; this digest never moves.
FROZEN_DIGEST = "27533a3d7707e0cf43b85cb2126e82dfd3d93e19006e889b240a95d0c465afd9"

# What makes a revision break the code of earlier revisions: removing, renaming,
# retyping or tightening something that code reads or writes.
_CONTRACT = re.compile(
    r"\bDROP\s+(TABLE|COLUMN|CONSTRAINT|VIEW|FUNCTION|TRIGGER)\b"
    r"|\bRENAME\b"
    r"|\bALTER\s+COLUMN\s+\S+\s+(SET\s+DATA\s+)?TYPE\b"
    r"|\bSET\s+NOT\s+NULL\b"
    r"|\bADD\s+CONSTRAINT\b"
    r"|\bop\.(drop_table|drop_column|drop_constraint|rename_table|alter_column"
    r"|create_check_constraint|create_unique_constraint|create_foreign_key)\b",
    re.IGNORECASE,
)


def _revision_files() -> list[Path]:
    return sorted(p for p in VERSIONS.glob("*.py") if p.name != "__init__.py")


def breaks_older_code(source: str) -> bool:
    """Whether a revision's source removes or narrows something, by its SQL or ``op`` calls."""
    return bool(_CONTRACT.search(source))


def declares_floor(source: str) -> bool:
    """Whether a revision raises the compatibility floor in its own name."""
    return bool(re.search(r"raise_compat_floor\([^)]*\bby\s*=\s*revision\b", source, re.S))


def test_the_revisions_form_one_numbered_line():
    chain = code_chain()
    assert chain[0] == "0001_baseline"
    assert [int(r.split("_", 1)[0]) for r in chain] == list(range(1, len(chain) + 1))
    assert all(re.fullmatch(r"\d{4}_[a-z0-9_]+", r) for r in chain)
    assert sorted(p.stem for p in _revision_files()) == sorted(chain)


@pytest.mark.parametrize(
    "path",
    [*_revision_files(), *sorted(BASELINE.glob("*.py"))],
    ids=lambda p: p.relative_to(MIGRATIONS_DIR).as_posix(),
)
def test_history_does_not_import_the_service(path):
    """A revision runs the same way forever, however ``serving`` changes."""
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        else:
            continue
        assert not [n for n in names if n == "serving" or n.startswith("serving.")], (
            f"{path.name} imports {names}"
        )


def test_the_baseline_is_frozen():
    files = [*sorted(BASELINE.glob("*.py")), VERSIONS / "0001_baseline.py"]
    digest = hashlib.sha256()
    for path in files:
        digest.update(
            path.relative_to(MIGRATIONS_DIR).as_posix().encode() + b"\0" + path.read_bytes() + b"\0"
        )
    assert digest.hexdigest() == FROZEN_DIGEST, (
        "migrations/baseline and 0001_baseline are frozen: they record what every "
        "existing database already ran. Write the change as a new revision instead."
    )


def test_a_revision_that_breaks_older_code_raises_the_floor():
    """The baseline is exempt: its drops and narrowings ran at every startup already."""
    undeclared = [
        path.name
        for path in _revision_files()[1:]
        if breaks_older_code(path.read_text()) and not declares_floor(path.read_text())
    ]
    assert not undeclared, (
        f"{undeclared} remove or narrow something earlier code may use: call "
        "migrations.compat.raise_compat_floor(<marker revision>, by=revision)"
    )


@pytest.mark.parametrize(
    "source",
    [
        'op.execute("ALTER TABLE users DROP COLUMN tier")',
        'op.execute("ALTER TABLE users RENAME COLUMN a TO b")',
        'op.execute("ALTER TABLE t ALTER COLUMN c TYPE bigint")',
        'op.execute("ALTER TABLE t ALTER COLUMN c SET NOT NULL")',
        "op.drop_column('users', 'tier')",
        "op.alter_column('users', 'email', nullable=False)",
    ],
)
def test_the_contract_check_recognizes_narrowing(source):
    assert breaks_older_code(source)
    assert not declares_floor(source)
    assert declares_floor(source + '\nraise_compat_floor("0004_x", by=revision)')


@pytest.mark.parametrize(
    "source",
    [
        'op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS note TEXT")',
        'op.execute("CREATE TABLE IF NOT EXISTS t (id BIGINT PRIMARY KEY)")',
        'op.execute("CREATE INDEX IF NOT EXISTS idx_t ON t(id)")',
        'op.execute("DROP INDEX IF EXISTS idx_unused")',
    ],
)
def test_the_contract_check_lets_expansion_through(source):
    assert not breaks_older_code(source)


def test_the_repository_alembic_command_finds_the_same_directory():
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    config = Config(toml_file=str(REPO / "pyproject.toml"))
    assert Path(ScriptDirectory.from_config(config).dir).resolve() == MIGRATIONS_DIR
