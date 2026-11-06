"""Drop ``idx_api_logs_request_id``: the unique index on the column already serves it.

``api_logs.request_id`` is declared ``UNIQUE``, so PostgreSQL keeps a unique
B-tree on it, ``api_logs_request_id_key``: the arbiter of every log write's
``ON CONFLICT (request_id)``, and the index any lookup by request id uses.
``idx_api_logs_request_id`` is a plain B-tree on the same single column, so
every insert into the gateway's largest table maintained two identical indexes
where one does. No code or query names it; the previous release, whose writes
arbitrate on the unique index, works the same without it.

The drop is ``DROP INDEX CONCURRENTLY``, outside the revision's transaction. A
plain ``DROP INDEX`` queues an ACCESS EXCLUSIVE lock on ``api_logs``, and every
log write and read waits behind it for as long as it waits. The concurrent drop
takes only a SHARE UPDATE EXCLUSIVE lock, which writes and reads pass, marks the
index invalid, and waits for the transactions that were using the table to end.
Each of those waits is bounded like every lock wait of a revision (3 s,
``env.DDL_LOCK_WAIT``): behind a long transaction -- a nightly ``pg_dump``, a long
report -- the run fails with a lock timeout and exits 75, to be run again later.
An interrupted drop leaves the index invalid, still maintained but no longer
read, and the next run finishes it.

The index is dropped only where the unique index is there and valid, so a
database whose ``request_id`` is not unique keeps the one index it has on it.

Revision ID: 0004_drop_request_id_index
Revises: 0003_user_usage_sums
Create Date: 2026-11-06
"""

from __future__ import annotations

import logging

import sqlalchemy as sa
from alembic import op

revision = "0004_drop_request_id_index"
down_revision = "0003_user_usage_sums"
branch_labels = None
depends_on = None
compatibility = "expand"

logger = logging.getLogger(__name__)

INDEX = "idx_api_logs_request_id"

#: Whether ``api_logs`` has a valid unique index on exactly ``request_id``,
#: other than the one dropped here.
UNIQUE_INDEX_PRESENT = f"""
SELECT EXISTS (
    SELECT 1
    FROM pg_index AS i
    JOIN pg_attribute AS a ON a.attrelid = i.indrelid AND a.attnum = i.indkey[0]
    WHERE i.indrelid = to_regclass('api_logs')
      AND i.indexrelid IS DISTINCT FROM to_regclass('{INDEX}')
      AND i.indisunique
      AND i.indisvalid
      AND i.indnkeyatts = 1
      AND i.indexprs IS NULL
      AND i.indpred IS NULL
      AND a.attname = 'request_id'
)
"""

#: The statements, in order, each run on its own outside a transaction. Tests
#: that build the ``api_logs`` tables from the frozen baseline in a schema of
#: their own apply them after it.
STATEMENTS = (f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX}",)


def upgrade() -> None:
    """Drop the duplicate index, concurrently, if the unique one is there."""
    if not op.get_bind().execute(sa.text(UNIQUE_INDEX_PRESENT)).scalar():
        logger.warning(
            "%s kept: api_logs has no valid unique index on request_id to take its place",
            INDEX,
        )
        return
    # CONCURRENTLY cannot run inside a transaction block.
    with op.get_context().autocommit_block():
        for statement in STATEMENTS:
            op.execute(statement)


def downgrade() -> None:
    """Leave the index dropped: nothing reads it, and rebuilding it would lock the table's writes."""
