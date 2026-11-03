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

import enum
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from migrations import COMPAT_TABLE, MIGRATIONS_DIR, VERSION_TABLE

if TYPE_CHECKING:
    from collections.abc import Sequence

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


def code_chain() -> tuple[str, ...]:
    """This code's revisions, oldest first.

    Raises:
        RuntimeError: The revisions do not form one line (more than one head).
    """
    from alembic.script import ScriptDirectory

    script = ScriptDirectory(str(MIGRATIONS_DIR))
    heads = script.get_heads()
    if len(heads) != 1:
        raise RuntimeError(f"the migrations must have exactly one head, found {sorted(heads)}")
    return tuple(reversed([s.revision for s in script.walk_revisions("base", "heads")]))


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
