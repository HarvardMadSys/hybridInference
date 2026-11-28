"""Which code a database still supports, and what the gateway does about it.

A database records two things: the revision it is at (:data:`VERSION_TABLE`,
kept by Alembic) and the oldest revision whose code still runs on it (the
*compatibility floor*, one row in :data:`COMPAT_TABLE`). Revisions only expand
the schema by default, so the code of every earlier revision keeps working and
the floor stays unset. A revision that removes or narrows something older code
still uses raises the floor in its own transaction, with
:func:`raise_compat_floor`.

At startup the gateway compares the database with its own chain of revisions
(:func:`require_schema`):

- no version recorded: refuse; the migrations have not been applied;
- at this code's head: start;
- an earlier revision of this chain: refuse; the database is behind the code;
- a revision this chain does not know, so a newer one: start, with a warning,
  only when the floor is unset or is a revision of this chain. Not knowing a
  revision proves nothing about compatibility; the floor is the database's own
  statement of which code it still supports. This is the case of a deploy
  that failed and restored the previous image.
"""

from __future__ import annotations

import ast
import enum
import logging
import re
from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from migrations import COMPAT_TABLE, MIGRATIONS_DIR, VERSION_TABLE

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

logger = logging.getLogger(__name__)

#: The command a deploy runs, as the refusals name it.
UPGRADE_COMMAND = "python -m migrations upgrade"

COMPAT_TABLE_DDL = f"""
CREATE TABLE IF NOT EXISTS {COMPAT_TABLE} (
    id BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (id),
    oldest_compatible_revision TEXT NOT NULL,
    set_by_revision TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
)
"""


#: The files Alembic loads as revisions (its ``_only_source_rev_file``): every
#: ``*.py`` but ``__init__.py`` and an editor's ``.#`` lock file.
_REVISION_FILE = re.compile(r"(?!\.\#|__init__)(.*\.py)$")

#: The revision ids :func:`code_chain` reads itself. Alembic refuses some
#: characters in an id and gives others meaning; any id outside this set is
#: left to it.
_PLAIN_REVISION_ID = re.compile(r"[A-Za-z0-9_]+")

#: The module attributes Alembic reads from a revision.
_HEADER = ("revision", "down_revision", "branch_labels", "depends_on")


def code_chain() -> tuple[str, ...]:
    """This code's revisions, oldest first.

    The gateway calls this at every start. Alembic answers it by importing
    Alembic, SQLAlchemy and every revision module, about 20 MB that the
    gateway, which uses none of them, would then hold for as long as it runs.
    So the revision files are read here instead, without running them, when
    each one plainly states its ``revision`` and ``down_revision`` and together
    they form one line from a single base -- as every revision here does, and
    as a test checks against Alembic. Anything else, a merge, a branch label or
    an id computed at import, is Alembic's to read.

    Raises:
        RuntimeError: The revisions do not form one line (more than one head).
    """
    chain = _literal_chain(MIGRATIONS_DIR / "versions")
    return chain if chain is not None else _alembic_chain()


def _alembic_chain() -> tuple[str, ...]:
    """:func:`code_chain` as Alembic reads it, loading every revision module."""
    from alembic.script import ScriptDirectory

    script = ScriptDirectory(str(MIGRATIONS_DIR))
    heads = script.get_heads()
    if len(heads) != 1:
        raise RuntimeError(f"the migrations must have exactly one head, found {sorted(heads)}")
    return tuple(reversed([s.revision for s in script.walk_revisions("base", "heads")]))


def _literal_chain(versions: Path) -> tuple[str, ...] | None:
    """The revisions in *versions*, oldest first, read from their source.

    ``None`` unless every revision file there passes :func:`_literal_header`
    and they form a single line: one base, no two revisions with the same
    parent, every parent present.
    """
    if not versions.is_dir():
        return None
    parent_of: dict[str, str | None] = {}
    for path in sorted(versions.iterdir()):
        if not _REVISION_FILE.match(path.name) or not path.is_file():
            continue
        header = _literal_header(path)
        if header is None or header[0] in parent_of:
            return None
        parent_of[header[0]] = header[1]
    child_of: dict[str | None, str] = {}
    for revision, parent in parent_of.items():
        if parent in child_of or (parent is not None and parent not in parent_of):
            return None
        child_of[parent] = revision
    chain: list[str] = []
    while (child := child_of.get(chain[-1] if chain else None)) is not None:
        chain.append(child)
    # Shorter than the revisions read: some lie on a loop that the line from
    # the base never reaches. Empty: there are none. Either way, not a line.
    return tuple(chain) if chain and len(chain) == len(parent_of) else None


def _literal_header(path: Path) -> tuple[str, str | None] | None:
    """A revision file's ``revision`` and ``down_revision``, read without running it.

    ``None`` when what the file states is not certainly what Alembic would read
    from the module: a name of :data:`_HEADER` bound more than once, or by
    anything other than one top-level assignment of a literal; an id
    :data:`_PLAIN_REVISION_ID` does not cover; several ``down_revision`` (a
    merge); a branch label or a dependency; a file that does not parse.
    """
    try:
        tree = ast.parse(path.read_bytes(), filename=str(path))
    except (OSError, SyntaxError, ValueError):
        return None
    stated: dict[str, object] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            target, value = node.target, node.value
        else:
            continue
        if isinstance(target, ast.Name) and target.id in _HEADER:
            if target.id in stated or not isinstance(value, ast.Constant):
                return None
            stated[target.id] = value.value
    # Each statement read above binds its name once; any other binding of a
    # header name anywhere in the file (in a block, a function, an import)
    # could change what the module ends up holding.
    bound = _bindings(tree)
    if any(bound[name] != int(name in stated) for name in _HEADER):
        return None
    revision, down_revision = stated.get("revision"), stated.get("down_revision", ...)
    if not isinstance(revision, str) or not _PLAIN_REVISION_ID.fullmatch(revision):
        return None
    if down_revision is not None and not (
        isinstance(down_revision, str) and _PLAIN_REVISION_ID.fullmatch(down_revision)
    ):
        return None
    if stated.get("branch_labels") is not None or stated.get("depends_on") is not None:
        return None
    return revision, down_revision


def _bindings(tree: ast.AST) -> Counter[str]:
    """How many times each name is bound anywhere in *tree*; a star import binds the header."""
    bound: Counter[str] = Counter()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Load):
            bound[node.id] += 1
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            bound[node.name] += 1
        elif isinstance(node, ast.alias) and node.name == "*":
            bound.update(_HEADER)
        elif isinstance(node, ast.alias):
            bound[(node.asname or node.name).partition(".")[0]] += 1
        elif isinstance(node, ast.Global | ast.Nonlocal):
            bound.update(node.names)
        elif isinstance(node, ast.ExceptHandler | ast.MatchAs | ast.MatchStar) and node.name:
            bound[node.name] += 1
    return bound


def ensure_compat_table(connection: Any) -> None:
    """Create :data:`COMPAT_TABLE` on a SQLAlchemy connection; ``env.py`` runs it first."""
    import sqlalchemy as sa

    connection.execute(sa.text(COMPAT_TABLE_DDL))


def raise_compat_floor(oldest: str, *, by: str) -> None:
    """Declare, from a revision's ``upgrade()``, that code older than *oldest* stops working.

    Call it from the revision that drops, renames or narrows what earlier code
    used, naming the revision of the release that stopped using it (the
    marker that release shipped). The floor only ever rises.

    Args:
        oldest: The oldest revision whose code still runs once *by* is applied.
        by: The calling revision, ``revision`` in its module.

    Raises:
        ValueError: *oldest* is not a revision before *by*.
    """
    import sqlalchemy as sa
    from alembic import context, op

    chain = tuple(reversed([s.revision for s in context.script.walk_revisions("base", "heads")]))
    if oldest not in chain or by not in chain or chain.index(oldest) >= chain.index(by):
        raise ValueError(f"the compatibility floor {oldest!r} must be a revision before {by!r}")
    bind = op.get_bind()
    current = bind.execute(
        sa.text(f"SELECT oldest_compatible_revision FROM {COMPAT_TABLE}")
    ).scalar()
    if current in chain and chain.index(current) >= chain.index(oldest):
        return
    bind.execute(
        sa.text(
            f"INSERT INTO {COMPAT_TABLE} (id, oldest_compatible_revision, set_by_revision) "
            "VALUES (TRUE, :oldest, :by) "
            "ON CONFLICT (id) DO UPDATE SET "
            "oldest_compatible_revision = EXCLUDED.oldest_compatible_revision, "
            "set_by_revision = EXCLUDED.set_by_revision, updated_at = NOW()"
        ),
        {"oldest": oldest, "by": by},
    )


async def raise_floor_outside_revision(
    conn: Any, oldest: str, *, by: str, chain: Sequence[str] | None = None
) -> bool:
    """Raise the floor to *oldest* on an asyncpg connection, from a maintenance operation.

    For a change of the data, not of the schema, that older code would
    misread: the offline accounting-currency migration, whose ledger code
    older than the revision that introduced it does not know. Like
    :func:`raise_compat_floor` it only ever rises; *by* names the operation in
    ``set_by_revision``. Runs in the caller's transaction.

    Returns:
        Whether the floor was raised (False: it already was at or above *oldest*).

    Raises:
        ValueError: *oldest* is not a revision of this code's chain.
    """
    chain = tuple(chain) if chain is not None else code_chain()
    if oldest not in chain:
        raise ValueError(f"the compatibility floor {oldest!r} is not a revision of this code")
    current = await conn.fetchval(f"SELECT oldest_compatible_revision FROM {COMPAT_TABLE}")
    if current in chain and chain.index(current) >= chain.index(oldest):
        return False
    await conn.execute(
        f"INSERT INTO {COMPAT_TABLE} (id, oldest_compatible_revision, set_by_revision) "
        "VALUES (TRUE, $1, $2) "
        "ON CONFLICT (id) DO UPDATE SET "
        "oldest_compatible_revision = EXCLUDED.oldest_compatible_revision, "
        "set_by_revision = EXCLUDED.set_by_revision, updated_at = NOW()",
        oldest,
        by,
    )
    return True


@dataclass(frozen=True)
class SchemaState:
    """What a database records about its schema."""

    #: The revision it is at; ``None`` when the migrations never ran.
    revision: str | None
    #: The oldest revision whose code still runs on it; ``None`` for any.
    floor: str | None


class Verdict(enum.Enum):
    """How a database's schema relates to this code."""

    CURRENT = "current"
    NEWER_COMPATIBLE = "newer, still compatible"
    NOT_MIGRATED = "not migrated"
    BEHIND = "behind"
    TOO_NEW = "newer, no longer compatible"


class SchemaNotReady(RuntimeError):
    """The database's schema is not one this code can run on."""


async def read_state(conn: Any) -> SchemaState:
    """Read the version and the compatibility floor on an asyncpg connection or pool.

    Raises:
        SchemaNotReady: The version table holds more than one revision.
    """
    if await conn.fetchval("SELECT to_regclass($1)", VERSION_TABLE) is None:
        return SchemaState(revision=None, floor=None)
    versions = [
        r["version_num"] for r in await conn.fetch(f"SELECT version_num FROM {VERSION_TABLE}")
    ]
    if len(versions) > 1:
        raise SchemaNotReady(f"the database records more than one revision: {sorted(versions)}")
    floor = None
    if await conn.fetchval("SELECT to_regclass($1)", COMPAT_TABLE) is not None:
        floor = await conn.fetchval(f"SELECT oldest_compatible_revision FROM {COMPAT_TABLE}")
    return SchemaState(revision=versions[0] if versions else None, floor=floor)


def judge(state: SchemaState, chain: Sequence[str]) -> Verdict:
    """Place *state* against this code's *chain* (oldest first)."""
    if state.revision is None:
        return Verdict.NOT_MIGRATED
    if state.revision == chain[-1]:
        return Verdict.CURRENT
    if state.revision in chain:
        return Verdict.BEHIND
    if state.floor is None or state.floor in chain:
        return Verdict.NEWER_COMPATIBLE
    return Verdict.TOO_NEW


def describe(verdict: Verdict, state: SchemaState, chain: Sequence[str]) -> str:
    """The sentence an operator reads for *verdict*."""
    head = chain[-1]
    if verdict is Verdict.NOT_MIGRATED:
        return (
            "The database records no schema version: run "
            f"`{UPGRADE_COMMAND}` with this image before starting the gateway."
        )
    if verdict is Verdict.BEHIND:
        return (
            f"The database is at revision {state.revision}, behind this code's {head}: run "
            f"`{UPGRADE_COMMAND}` with this image before starting the gateway."
        )
    if verdict is Verdict.TOO_NEW:
        return (
            f"The database is at revision {state.revision}, newer than this code's {head}, "
            f"and no longer supports code older than {state.floor}: run a newer image."
        )
    if verdict is Verdict.NEWER_COMPATIBLE:
        floor = state.floor or "any revision"
        return (
            f"The database is at revision {state.revision}, newer than this code's {head}; "
            f"it still supports this code (oldest supported: {floor}). Expected after a "
            "deploy restored the previous image."
        )
    return f"The database is at this code's revision {head}."


async def require_schema(conn: Any, *, chain: Sequence[str] | None = None) -> SchemaState:
    """Refuse a schema this code cannot run on; warn about a newer compatible one.

    Args:
        conn: An asyncpg connection or pool on the gateway's database.
        chain: This code's revisions, oldest first; read from ``versions/``
            when omitted.

    Raises:
        SchemaNotReady: The migrations have not been applied, the database is
            behind this code, or it no longer supports this code.
    """
    chain = tuple(chain) if chain is not None else code_chain()
    state = await read_state(conn)
    verdict = judge(state, chain)
    if verdict is Verdict.NEWER_COMPATIBLE:
        logger.warning(describe(verdict, state, chain))
    elif verdict is not Verdict.CURRENT:
        raise SchemaNotReady(describe(verdict, state, chain))
    return state
