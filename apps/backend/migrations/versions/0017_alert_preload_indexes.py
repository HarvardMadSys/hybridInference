"""Index live alert history used to restore open incidents at startup.

The notification-policy preload finds the newest condition event per alert key
and the accepted firing for its episode. These partial indexes cover the two
all-history reads without indexing test events, unrelated event kinds, or
undelivered history.

Revision ID: 0017_alert_preload_indexes
Revises: 0016_hard_delete_claim_lease
Create Date: 2026-11-13
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0017_alert_preload_indexes"
down_revision = "0016_hard_delete_claim_lease"
branch_labels = None
depends_on = None
compatibility = "expand"

INDEXES = {
    "idx_alert_history_events_preload_condition": (
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_alert_history_events_preload_condition "
        "ON alert_history_events (source, source_namespace, deployment_id, target_environment, "
        "alert_key, occurred_at DESC, recorded_at DESC, record_id DESC) "
        "WHERE origin = 'live' AND kind IN ('condition_triggered', 'condition_ended') "
        "AND alert_key IS NOT NULL AND episode_id IS NOT NULL"
    ),
    "idx_alert_history_events_preload_delivery": (
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_alert_history_events_preload_delivery "
        "ON alert_history_events (source, source_namespace, deployment_id, target_environment, "
        "alert_key, episode_id, occurred_at DESC) "
        "WHERE origin = 'live' AND kind = 'delivery_result' AND outcome = 'accepted' "
        "AND facts->>'notification' = 'firing' AND alert_key IS NOT NULL AND episode_id IS NOT NULL"
    ),
}

STATEMENTS = tuple(INDEXES.values())
INVALID_SQL = "SELECT NOT indisvalid FROM pg_index WHERE indexrelid = to_regclass(:name)"


def upgrade() -> None:
    """Build both targeted history indexes without blocking writers."""
    bind = op.get_bind()
    invalid = [
        name for name in INDEXES if bind.execute(sa.text(INVALID_SQL), {"name": name}).scalar()
    ]
    with op.get_context().autocommit_block():
        for name in invalid:
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
        for statement in STATEMENTS:
            op.execute(statement)


def downgrade() -> None:
    """Keep these additive indexes; the prior release does not depend on them."""
