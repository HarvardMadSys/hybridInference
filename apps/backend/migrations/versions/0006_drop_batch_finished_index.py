"""Drop ``idx_batch_jobs_finished``, which no query can use.

The index was built for the batch retention purge, on
``COALESCE(completed_at, failed_at, expired_at, cancelled_at)``. The purge
reckons a batch's end from its sealing first --
``COALESCE(sealed_at, completed_at, ...)`` -- and compares it with each batch's
own retention, which no index on the end time can bound; no other query names
the old expression. So the index was only ever written, on every change of a
batch's status or end. The purge now finds a sweep's batches in one read of the
table instead of one read per batch (``serving.storage.batch_store``).

Nothing the previous release runs uses it either, so dropping it is an expand
revision. Like ``0005_drop_request_id_index`` it is dropped concurrently, outside
the revision's transaction, so the batch worker's claims and updates are never
queued behind an ACCESS EXCLUSIVE lock; each wait is bounded by the run's lock
timeout, and an interrupted drop is finished by the next run.

Revision ID: 0006_drop_batch_finished_index
Revises: 0005_drop_request_id_index
Create Date: 2026-11-06
"""

from __future__ import annotations

from alembic import op

revision = "0006_drop_batch_finished_index"
down_revision = "0005_drop_request_id_index"
branch_labels = None
depends_on = None
compatibility = "expand"

#: The statements, in order, each run on its own outside a transaction.
STATEMENTS = ("DROP INDEX CONCURRENTLY IF EXISTS idx_batch_jobs_finished",)


def upgrade() -> None:
    """Drop the index, concurrently."""
    # CONCURRENTLY cannot run inside a transaction block.
    with op.get_context().autocommit_block():
        for statement in STATEMENTS:
            op.execute(statement)


def downgrade() -> None:
    """Leave the index dropped: no query can use it."""
