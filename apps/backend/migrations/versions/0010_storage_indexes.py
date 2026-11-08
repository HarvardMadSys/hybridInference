"""Index session liveness by account and session id.

``session_is_live`` -- asked on every request a console token makes, on the
request pool -- looks a session up by ``(user_id, sid)``. Neither had an index
of its own: ``idx_auth_sessions_user`` is ``(user_id, expires_at)``, so the
lookup read every unexpired session of the account to find the one sid, and an
account can hold thousands. ``idx_auth_sessions_user_sid`` makes it one probe.

Each index is built with ``CREATE INDEX CONCURRENTLY``, outside the revision's
transaction: a plain ``CREATE INDEX`` holds a SHARE lock that stops every sign-in,
refresh and sign-out for the whole build. The concurrent build takes only a
SHARE UPDATE EXCLUSIVE lock, which those pass, and waits for the transactions
older than it. Each wait is bounded like every lock wait of a revision (3 s,
``env.DDL_LOCK_WAIT``): behind a long transaction -- a nightly ``pg_dump``, a long
report -- the run fails with a lock timeout and exits 75, to be run again later.
An interrupted build leaves its index invalid, maintained by writes but never
read; the next run drops it and builds it again (:data:`INVALID_SQL`).

Adding an index changes nothing the previous release reads or writes, so this
is an expand revision.

Revision ID: 0010_storage_indexes
Revises: 0009_route_catalog
Create Date: 2026-11-08
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0010_storage_indexes"
down_revision = "0009_route_catalog"
branch_labels = None
depends_on = None
compatibility = "expand"

#: The indexes this revision builds: each name, and the statement that builds it.
INDEXES = {
    "idx_auth_sessions_user_sid": (
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_auth_sessions_user_sid "
        "ON auth_sessions (user_id, sid)"
    ),
}

#: Whether the index *name* is there but invalid, as an interrupted build leaves it.
INVALID_SQL = "SELECT NOT indisvalid FROM pg_index WHERE indexrelid = to_regclass(:name)"


def upgrade() -> None:
    """Build each index concurrently, first dropping what an interrupted build left."""
    bind = op.get_bind()
    invalid = [
        name for name in INDEXES if bind.execute(sa.text(INVALID_SQL), {"name": name}).scalar()
    ]
    # CONCURRENTLY cannot run inside a transaction block.
    with op.get_context().autocommit_block():
        for name in invalid:
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
        for statement in INDEXES.values():
            op.execute(statement)


def downgrade() -> None:
    """Keep the indexes: earlier code reads the same tables, only faster with them."""
