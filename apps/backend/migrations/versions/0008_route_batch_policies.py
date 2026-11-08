"""Store which routes batch work may use.

A batch is work nobody waits on, so by default it runs only on routes this
gateway serves itself (``serving.config.batch_routes``). An administrator can
allow or forbid any route for batch work; ``provider_route_batch_policies``
keeps those choices, keyed like ``provider_route_states`` by the model's
canonical id and the route id. A route with no row follows its locality.

It is a table of its own, not two more flags on ``provider_route_states``: that
table's rule is "no row means enabled", and enabling a route, its refresh and a
configuration restore all delete rows on that rule, which would wipe a batch
choice with them.

A new table that the previous release does not read: an expand revision.

Revision ID: 0008_route_batch_policies
Revises: 0007_request_volume_by_source
Create Date: 2026-11-07
"""

from __future__ import annotations

from alembic import op

revision = "0008_route_batch_policies"
down_revision = "0007_request_volume_by_source"
branch_labels = None
depends_on = None
compatibility = "expand"

#: The statements, in order.
STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS provider_route_batch_policies (
        model_id      TEXT        NOT NULL,
        route_id      TEXT        NOT NULL,
        batch_allowed BOOLEAN     NOT NULL,
        updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_by    TEXT,
        PRIMARY KEY (model_id, route_id)
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
    downgrade still works and keeps the choices made before.
    """
