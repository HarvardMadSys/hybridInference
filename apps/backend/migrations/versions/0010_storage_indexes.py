"""Index session liveness by account and session id; drop two ``api_logs`` indexes no query uses.

``session_is_live`` -- asked on every request a console token makes, on the
request pool -- looks a session up by ``(user_id, sid)``. Neither had an index
of its own: ``idx_auth_sessions_user`` is ``(user_id, expires_at)``, so the
lookup read every unexpired session of the account to find the one sid, and an
account can hold thousands. ``idx_auth_sessions_user_sid`` makes it one probe.

Every insert into ``api_logs``, the largest table, also updated two indexes that
no query needs:

- ``idx_api_logs_served_endpoint`` ``(served_endpoint_id, timestamp DESC)``: the
  admin views match the served endpoint only through
  ``COALESCE(NULLIF(served_endpoint_id, ''), provider)``, which it cannot serve.
- ``idx_api_logs_user`` ``(user_id, timestamp DESC) WHERE user_id IS NOT NULL``:
  every lookup by account (``user_id = ...``, its time ranges and its newest rows
  first) is served as well by ``idx_api_logs_user_cost``
  ``(user_id, timestamp, cost_usd)``, which leads with the same two columns and
  stays.

Nothing the previous release runs needs either one, as ``0005`` found for the
copy of the request-id index, so dropping them is an expand revision too.

Every index is built or dropped ``CONCURRENTLY``, outside the revision's
transaction. A plain ``CREATE INDEX`` holds a SHARE lock that stops every write
to its table for the whole build -- every sign-in, refresh and sign-out -- and a
plain ``DROP INDEX`` queues an ACCESS EXCLUSIVE lock that every log write and
read waits behind. The concurrent forms take only a SHARE UPDATE EXCLUSIVE lock,
which writes and reads pass, and wait for the transactions older than them. Each
wait is bounded like every lock wait of a revision (3 s, ``env.DDL_LOCK_WAIT``):
behind a long transaction -- a nightly ``pg_dump``, a long report -- the run fails
with a lock timeout and exits 75, to be run again later. An interrupted drop
leaves its index invalid, still maintained but no longer read, and the next run
finishes it; an interrupted build leaves its index invalid too, and the next
run drops it and builds it again (:data:`INVALID_SQL`).

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

#: The indexes it drops.
DROPS = (
    "DROP INDEX CONCURRENTLY IF EXISTS idx_api_logs_served_endpoint",
    "DROP INDEX CONCURRENTLY IF EXISTS idx_api_logs_user",
)

#: Its statements on the tables ``migrations.baseline.api_logs`` builds, in order,
#: each run on its own outside a transaction. Tests that build those tables from
#: the frozen baseline in a schema of their own apply them after it.
STATEMENTS = DROPS

#: Whether the index *name* is there but invalid, as an interrupted build leaves it.
INVALID_SQL = "SELECT NOT indisvalid FROM pg_index WHERE indexrelid = to_regclass(:name)"


def upgrade() -> None:
    """Drop the unused indexes, then build each new one, all concurrently.

    An invalid index left by an interrupted build is dropped first.
    """
    bind = op.get_bind()
    invalid = [
        name for name in INDEXES if bind.execute(sa.text(INVALID_SQL), {"name": name}).scalar()
    ]
    # CONCURRENTLY cannot run inside a transaction block.
    with op.get_context().autocommit_block():
        for statement in DROPS:
            op.execute(statement)
        for name in invalid:
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
        for statement in INDEXES.values():
            op.execute(statement)


def downgrade() -> None:
    """Keep the new indexes and leave the dropped ones dropped.

    Earlier code reads the same tables, only faster with the new ones, and needs
    neither of the dropped ones; rebuilding those would lock ``api_logs`` writes.
    """
