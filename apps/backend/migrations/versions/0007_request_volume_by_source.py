"""Count the batch worker's requests apart in the stored request volume hours.

The admin Request volume cards count the requests callers made directly, with
the batch worker's stacked on each bar beside them: a large batch puts
thousands of rows in an hour and would otherwise read as the gateway's traffic.
``request_volume_hourly`` stores each completed hour's counts once
(``serving.servers.routers.admin.request_volume``); these columns store the
batch worker's share of each (``api_logs.batch_job_id`` set), so the
interactive counts are the rest.

- ``request_volume_hourly``: ``batch_request_count``, ``batch_success_count``,
  ``batch_error_count``, ``batch_client_disconnect_count``,
  ``batch_latency_count`` and ``batch_latency_sum_ms``.
- ``request_volume_hourly.definition``: the fingerprint of the counts an hour
  was stored under. An hour stored under any other is not stored as far as
  this release is concerned: it is counted again.

Every column has a default, so a fill by the previous release, which names
only its own columns, still inserts; its hours carry no fingerprint, and this
release counts them again, at startup and when a card needs them. Adding a
column with a constant default is a catalog-only change in PostgreSQL: it takes
the table lock briefly and rewrites nothing.

Revision ID: 0007_request_volume_by_source
Revises: 0006_drop_batch_finished_index
Create Date: 2026-11-07
"""

from __future__ import annotations

from alembic import op

revision = "0007_request_volume_by_source"
down_revision = "0006_drop_batch_finished_index"
branch_labels = None
depends_on = None
compatibility = "expand"


#: The statements, in order. Tests that build the rollup tables from the frozen
#: baseline in a schema of their own apply them after it.
STATEMENTS = (
    "ALTER TABLE request_volume_hourly "
    "ADD COLUMN IF NOT EXISTS batch_request_count BIGINT NOT NULL DEFAULT 0, "
    "ADD COLUMN IF NOT EXISTS batch_success_count BIGINT NOT NULL DEFAULT 0, "
    "ADD COLUMN IF NOT EXISTS batch_error_count BIGINT NOT NULL DEFAULT 0, "
    "ADD COLUMN IF NOT EXISTS batch_client_disconnect_count BIGINT NOT NULL DEFAULT 0, "
    "ADD COLUMN IF NOT EXISTS batch_latency_count BIGINT NOT NULL DEFAULT 0, "
    "ADD COLUMN IF NOT EXISTS batch_latency_sum_ms BIGINT NOT NULL DEFAULT 0, "
    "ADD COLUMN IF NOT EXISTS definition TEXT NOT NULL DEFAULT ''",
)


def upgrade() -> None:
    """Add the batch worker's counts and the fingerprint they were counted under."""
    for statement in STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    """Keep the columns: defaulted and unread by earlier code, they are harmless there.

    ``upgrade`` adds them with ``IF NOT EXISTS``, so going back up after a
    downgrade still works.
    """
