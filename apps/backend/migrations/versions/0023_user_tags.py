"""User tags: named groups of accounts, each with limits its members are held to.

- ``user_tags``: one row per tag. A tag may set a daily and a monthly cost
  quota (in ``accounting_currency``, the ledger currency when they were
  written), a model allowlist and a cap on concurrent requests; NULL sets
  nothing. A member is held to the strictest of its own limit and those of
  every tag it carries. A tag with ``auto_enabled`` is given automatically to
  every account that sent requests in at least ``auto_min_active_hours`` of
  the last ``auto_window_hours`` hours. ``system`` marks a tag the gateway
  ships, which cannot be deleted.
- ``user_tag_members``: which account carries which tag. ``source`` says how:
  ``manual`` (an administrator added it), ``auto`` (the detector did), or
  ``dismissed`` (an administrator took an automatic tag away; the detector
  gives it back only for requests sent after ``assigned_at``). A dismissed
  row carries no limits.

Seeds the built-in ``unattended`` tag: automatic, for an account with
requests in every one of the last 12 hours, and no limits until an
administrator sets them.

Expand only: two new tables that earlier code never reads.

Revision ID: 0023_user_tags
Revises: 0022_ledger_currency
Create Date: 2026-11-27
"""

from __future__ import annotations

from alembic import op

revision = "0023_user_tags"
down_revision = "0022_ledger_currency"
branch_labels = None
depends_on = None
compatibility = "expand"


#: The revision's statements, in order; each can run again (``IF NOT EXISTS``,
#: ``ON CONFLICT DO NOTHING``), and the seed never overwrites an edited tag.
STATEMENTS: tuple[str, ...] = (
    "CREATE TABLE IF NOT EXISTS user_tags ("
    "name TEXT PRIMARY KEY CHECK (name ~ '^[a-z0-9][a-z0-9_-]{0,31}$'), "
    "description TEXT NOT NULL DEFAULT '', "
    "quota_daily_cost NUMERIC(20, 8) CHECK (quota_daily_cost >= 0), "
    "quota_monthly_cost NUMERIC(20, 8) CHECK (quota_monthly_cost >= 0), "
    "accounting_currency TEXT, "
    "max_concurrent_requests INTEGER CHECK (max_concurrent_requests >= 1), "
    "allowed_models TEXT[], "
    "auto_enabled BOOLEAN NOT NULL DEFAULT FALSE, "
    "auto_window_hours INTEGER NOT NULL DEFAULT 12 "
    "CHECK (auto_window_hours BETWEEN 1 AND 168), "
    "auto_min_active_hours INTEGER NOT NULL DEFAULT 12 "
    "CHECK (auto_min_active_hours >= 1), "
    "system BOOLEAN NOT NULL DEFAULT FALSE, "
    "created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), "
    "updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), "
    "CHECK (auto_min_active_hours <= auto_window_hours)"
    ")",
    "CREATE TABLE IF NOT EXISTS user_tag_members ("
    "user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE, "
    "tag TEXT NOT NULL REFERENCES user_tags(name) ON DELETE CASCADE, "
    "source TEXT NOT NULL CHECK (source IN ('manual', 'auto', 'dismissed')), "
    "assigned_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), "
    "assigned_by TEXT, "
    "PRIMARY KEY (user_id, tag)"
    ")",
    "CREATE INDEX IF NOT EXISTS idx_user_tag_members_tag ON user_tag_members (tag, source)",
    "INSERT INTO user_tags "
    "(name, description, auto_enabled, auto_window_hours, auto_min_active_hours, system) "
    "VALUES ('unattended', "
    "'Sends requests around the clock: likely a script or agent running unattended.', "
    "TRUE, 12, 12, TRUE) "
    "ON CONFLICT (name) DO NOTHING",
)


def upgrade() -> None:
    """Add the two tables and the built-in ``unattended`` tag."""
    for statement in STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    """Keep both tables: unread by earlier code, they are harmless there.

    ``upgrade`` adds them with ``IF NOT EXISTS`` and seeds with
    ``ON CONFLICT DO NOTHING``, so going back up after a downgrade still works.
    """
