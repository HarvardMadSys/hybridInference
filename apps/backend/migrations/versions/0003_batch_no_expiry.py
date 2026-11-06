"""Let a batch have no completion window.

``batch_jobs.expires_at`` becomes nullable. NULL means the batch never expires:
it keeps starting requests until every one is done. The gateway writes NULL
for a batch created while ``BATCH_COMPLETION_WINDOW_HOURS`` is 0; every
existing batch keeps its timestamp and behaves as before.

Dropping NOT NULL only widens what the column accepts. The code of the previous
release always writes a value, so it keeps working on this schema. It does not
claim from a batch with a NULL ``expires_at`` (its claim filter is
``expires_at > now()``), nor expire one, so such a batch waits for this release
instead of failing; none exists until an operator sets the window to 0.

``DROP NOT NULL`` is a catalog-only change in PostgreSQL: it takes the table
lock briefly and scans or rewrites nothing.

Revision ID: 0003_batch_no_expiry
Revises: 0002_api_key_limits
Create Date: 2026-11-06
"""

from __future__ import annotations

from alembic import op

revision = "0003_batch_no_expiry"
down_revision = "0002_api_key_limits"
branch_labels = None
depends_on = None
compatibility = "expand"


def upgrade() -> None:
    """Allow a NULL ``expires_at`` on ``batch_jobs``."""
    op.execute("ALTER TABLE batch_jobs ALTER COLUMN expires_at DROP NOT NULL")


def downgrade() -> None:
    """Keep the column nullable.

    Restoring NOT NULL would fail on any batch created without a window, and
    earlier code never writes NULL, so a nullable column is harmless there.
    """
