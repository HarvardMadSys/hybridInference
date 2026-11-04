"""Let an API key carry its own budgets and model allowlist.

Three nullable columns on ``api_keys``. NULL means "no limit at this key", so
every existing key keeps behaving as it did, and the code of the previous
release, which selects named columns, never sees them.

- ``budget_daily_usd`` / ``budget_monthly_usd``: what requests made with the
  key may cost per UTC day and month, counted in ``api_key_daily_cost``.
- ``allowed_models``: the canonical model ids the key may call and list.

Adding a nullable column without a default is a catalog-only change in
PostgreSQL: it takes the table lock briefly and rewrites nothing.

Revision ID: 0002_api_key_limits
Revises: 0001_baseline
Create Date: 2026-11-03
"""

from __future__ import annotations

from alembic import op

revision = "0002_api_key_limits"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None
compatibility = "expand"


def upgrade() -> None:
    """Add the three limit columns."""
    op.execute(
        "ALTER TABLE api_keys "
        "ADD COLUMN IF NOT EXISTS budget_daily_usd DECIMAL(12, 4), "
        "ADD COLUMN IF NOT EXISTS budget_monthly_usd DECIMAL(12, 4), "
        "ADD COLUMN IF NOT EXISTS allowed_models TEXT[]"
    )


def downgrade() -> None:
    """Keep the columns: nullable and unread by earlier code, they are harmless there.

    ``upgrade`` adds them with ``IF NOT EXISTS``, so going back up after a
    downgrade still works.
    """
