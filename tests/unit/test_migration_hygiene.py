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
FROZEN_DIGEST = "d684cddc5edc40f28adcc27c27757c47cb3c9ffa71e7ca101bdade279c053bb5"

# Each revision after the baseline says whether code of earlier revisions keeps
# working on its result ("expand") or not ("contract"), and a contract raises
# the compatibility floor. The author judges; no pattern can: dropping a plain
# index is harmless, dropping the unique index an ON CONFLICT names is not.
COMPATIBILITY = ("expand", "contract")

# A tripwire for a revision labelled "expand" that plainly is not: removing,
# renaming, retyping or tightening what earlier code reads or writes. Not a
# complete list, and not the rule; the label is.
_CONTRACT = re.compile(
    r"\bDROP\s+(TABLE|COLUMN|CONSTRAINT|VIEW|FUNCTION|TRIGGER)\b"
    r"|\bRENAME\b"
    r"|\bALTER\s+COLUMN\s+\S+\s+(SET\s+DATA\s+)?TYPE\b"
    r"|\bSET\s+NOT\s+NULL\b"
    r"|\bADD\s+CONSTRAINT\b"
    r"|\bCREATE\s+UNIQUE\s+INDEX\b"
    r"|\bADD\s+(COLUMN\s+)?(IF\s+NOT\s+EXISTS\s+)?\w+\s+[^,;\"]*?\bNOT\s+NULL\b(?![^,;\"]*\bDEFAULT\b)"
    r"|\bunique\s*=\s*True\b"
    r"|\bnullable\s*=\s*False\b"
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


def compatibility(source: str) -> object:
    """The module-level ``compatibility`` a revision declares, or None."""
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "compatibility" for target in node.targets
        ):
            return node.value.value if isinstance(node.value, ast.Constant) else node.value
    return None


def compatibility_problem(source: str) -> str | None:
    """Why a revision's compatibility declaration does not hold, or None when it does."""
    label = compatibility(source)
    if label not in COMPATIBILITY:
        return f'declares compatibility = {label!r}; set "expand" or "contract"'
    if label == "contract" and not declares_floor(source):
        return 'is a "contract" but does not call raise_compat_floor(<marker>, by=revision)'
    if label == "expand" and declares_floor(source):
        return 'is an "expand" but raises the compatibility floor'
    if label == "expand" and breaks_older_code(source):
        return 'is labelled "expand" but removes or narrows something earlier code may use'
    return None


def test_the_revisions_form_one_numbered_line():
    chain = code_chain()
    assert chain[0] == "0001_baseline"
    assert [int(r.split("_", 1)[0]) for r in chain] == list(range(1, len(chain) + 1))
    assert all(re.fullmatch(r"\d{4}_[a-z0-9_]+", r) for r in chain)
    assert sorted(p.stem for p in _revision_files()) == sorted(chain)


def test_every_revision_id_fits_the_version_table():
    """Alembic records the revision in a ``VARCHAR(32)``.

    A longer id fails the run when it records the revision -- after the
    revision's own statements have already run.
    """
    assert [r for r in code_chain() if len(r) > 32] == []


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


def test_every_revision_declares_whether_older_code_keeps_working():
    """The baseline is exempt: its drops and narrowings ran at every startup already."""
    problems = {
        path.name: problem
        for path in _revision_files()[1:]
        if (problem := compatibility_problem(path.read_text()))
    }
    assert not problems, problems


_EXPAND = 'compatibility = "expand"\n'
_CONTRACT_DECL = 'compatibility = "contract"\n'
_FLOOR = 'raise_compat_floor("0004_x", by=revision)\n'


@pytest.mark.parametrize(
    ("source", "accepted"),
    [
        ("", False),
        ("compatibility = None\n", False),
        ('compatibility = "maybe"\n', False),
        (_EXPAND + 'op.execute("CREATE TABLE IF NOT EXISTS t (id BIGINT PRIMARY KEY)")', True),
        (_EXPAND + 'op.execute("DROP INDEX IF EXISTS idx_unused")', True),
        (_EXPAND + 'op.execute("ALTER TABLE users DROP COLUMN tier")', False),
        (_EXPAND + _FLOOR, False),
        (_CONTRACT_DECL + 'op.execute("ALTER TABLE users DROP COLUMN tier")', False),
        (_CONTRACT_DECL + 'op.execute("ALTER TABLE users DROP COLUMN tier")\n' + _FLOOR, True),
        # The label is the rule: an index drop is a contract when its author says so.
        (_CONTRACT_DECL + 'op.execute("DROP INDEX idx_api_keys_user_unique")\n' + _FLOOR, True),
    ],
)
def test_the_declaration_rule(source, accepted):
    assert (compatibility_problem(source) is None) is accepted


@pytest.mark.parametrize(
    "source",
    [
        'op.execute("ALTER TABLE users DROP COLUMN tier")',
        'op.execute("ALTER TABLE users RENAME COLUMN a TO b")',
        'op.execute("ALTER TABLE t ALTER COLUMN c TYPE bigint")',
        'op.execute("ALTER TABLE t ALTER COLUMN c SET NOT NULL")',
        "op.drop_column('users', 'tier')",
        "op.alter_column('users', 'email', nullable=False)",
        'op.execute("ALTER TABLE users ADD COLUMN plan TEXT NOT NULL")',
        'op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS plan TEXT NOT NULL")',
        'op.execute("CREATE UNIQUE INDEX idx_one ON t(user_id)")',
        "op.create_index('idx_one', 't', ['user_id'], unique=True)",
        "op.add_column('users', sa.Column('plan', sa.Text(), nullable=False))",
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
        """op.execute("ALTER TABLE users ADD COLUMN plan TEXT NOT NULL DEFAULT 'free'")""",
        "op.add_column('users', sa.Column('plan', sa.Text(), nullable=True))",
    ],
)
def test_the_contract_check_lets_expansion_through(source):
    assert not breaks_older_code(source)


def test_the_repository_alembic_command_finds_the_same_directory():
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    config = Config(toml_file=str(REPO / "pyproject.toml"))
    assert Path(ScriptDirectory.from_config(config).dir).resolve() == MIGRATIONS_DIR
