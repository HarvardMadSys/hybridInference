"""Store the model registry and the routing document.

The files stay the seed. The first boot that reaches the database copies each
non-empty file into ``route_catalog`` when that kind has no row
(``serving.config.route_catalog``). Later boots register from the row. A file
edit does not replace a row.

A new table that the previous release does not read: an expand revision.

Revision ID: 0009_route_catalog
Revises: 0008_route_batch_policies
Create Date: 2026-11-07
"""

from __future__ import annotations

from alembic import op

revision = "0009_route_catalog"
down_revision = "0008_route_batch_policies"
branch_labels = None
depends_on = None
compatibility = "expand"

#: The statements, in order.
STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS route_catalog (
        kind        TEXT        PRIMARY KEY,
        body        TEXT        NOT NULL,
        source      TEXT        NOT NULL,
        updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_by  TEXT
    )
    """,
)


def upgrade() -> None:
    """Create the table."""
    for statement in STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    """Keep the table: unread by earlier code, it is harmless there.

    ``upgrade`` creates it with ``IF NOT EXISTS``, so going back up after a
    downgrade still works and keeps the documents imported before.
    """
