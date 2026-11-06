"""Store each user's daily usage sums beside the daily request counts.

The user dashboard (``/user/usage``) shows a user's requests, cost and token
sums for today, this month and all time. It summed them straight off
``api_logs`` -- every row the user ever logged, on every call. The admin Users
table's daily rollup (``user_usage_daily``) already counts each completed day
once per user; these columns add the dashboard's sums to it, so the all-time
and month figures read the stored days plus the days not settled yet.

- ``user_usage_daily``: the day's ``cost_usd``, ``prompt_tokens``,
  ``completion_tokens``, ``cache_read_tokens``, ``cache_write_tokens`` and
  ``reasoning_tokens`` sums, and ``reasoning_tokens_known``, how many of the
  day's rows reported a reasoning count.
- ``user_usage_daily_coverage.usage_definition``: the fingerprint the sums of
  a day were counted under. A day is read from the table for these sums only
  when it matches the current one, so the admin columns' stored days stay
  valid while the days are filled again.

Every column has a default, so a fill by the previous release, which names
only its own columns, still inserts, and its days carry no usage fingerprint
and are counted live by this one. Adding a column with a constant default is a
catalog-only change in PostgreSQL: it takes the table lock briefly and
rewrites nothing.

Revision ID: 0003_user_usage_sums
Revises: 0002_api_key_limits
Create Date: 2026-11-06
"""

from __future__ import annotations

from alembic import op

revision = "0003_user_usage_sums"
down_revision = "0002_api_key_limits"
branch_labels = None
depends_on = None
compatibility = "expand"


#: The statements, in order. Tests that build the rollup tables from the frozen
#: baseline in a schema of their own apply them after it.
STATEMENTS = (
    "ALTER TABLE user_usage_daily "
    "ADD COLUMN IF NOT EXISTS cost_usd NUMERIC NOT NULL DEFAULT 0, "
    "ADD COLUMN IF NOT EXISTS prompt_tokens BIGINT NOT NULL DEFAULT 0, "
    "ADD COLUMN IF NOT EXISTS completion_tokens BIGINT NOT NULL DEFAULT 0, "
    "ADD COLUMN IF NOT EXISTS cache_read_tokens BIGINT NOT NULL DEFAULT 0, "
    "ADD COLUMN IF NOT EXISTS cache_write_tokens BIGINT NOT NULL DEFAULT 0, "
    "ADD COLUMN IF NOT EXISTS reasoning_tokens BIGINT NOT NULL DEFAULT 0, "
    "ADD COLUMN IF NOT EXISTS reasoning_tokens_known BIGINT NOT NULL DEFAULT 0",
    "ALTER TABLE user_usage_daily_coverage "
    "ADD COLUMN IF NOT EXISTS usage_definition TEXT NOT NULL DEFAULT ''",
)


def upgrade() -> None:
    """Add the sums and the fingerprint they were counted under."""
    for statement in STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    """Keep the columns: defaulted and unread by earlier code, they are harmless there.

    ``upgrade`` adds them with ``IF NOT EXISTS``, so going back up after a
    downgrade still works.
    """
