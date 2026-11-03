"""Bring any database to the schema the gateway used to build at startup.

A new database gets every table; an existing one gets what its upgrade history
left out, so no deployment needs ``alembic stamp``. The work is the frozen
startup builders in :mod:`migrations.baseline`, run on an asyncpg connection of
their own: each of their steps commits on its own and releases its table locks,
instead of all of them being held in this revision's transaction until the end.
If one step cannot get its lock in time, the revision fails without recording
itself, and running it again carries on from the step that failed.

That connection waits at most 3 s for any lock, as the revision statements do
(``env.DDL_LOCK_WAIT``). The builders bound only their ALTERs themselves, but
even a settled ``CREATE INDEX IF NOT EXISTS`` takes its table's SHARE lock
before it looks for the index: without the limit it would wait for every open
write on the live database, and every later write would queue behind it.

The baseline sets no compatibility floor. The legacy columns it drops and the
constraints it narrows are the ones every startup already dropped and narrowed.

Revision ID: 0001_baseline
Revises:
Create Date: 2026-11-02
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import asyncpg
from alembic import op
from sqlalchemy.util import await_only

from migrations.baseline import run

if TYPE_CHECKING:
    from sqlalchemy.engine import URL

revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Run the frozen startup builders against the database being migrated."""
    await_only(_converge(op.get_bind().engine.url))


async def _converge(url: URL) -> None:
    conn = await asyncpg.connect(
        host=url.host,
        port=url.port,
        user=url.username,
        password=url.password,
        database=url.database,
        server_settings={"lock_timeout": "3s", "application_name": "hybridinference-migrations"},
    )
    try:
        await run(conn)
    finally:
        await conn.close()


def downgrade() -> None:
    """The baseline has no downgrade: it converges whatever schema it finds."""
    raise NotImplementedError("0001_baseline cannot be downgraded")
