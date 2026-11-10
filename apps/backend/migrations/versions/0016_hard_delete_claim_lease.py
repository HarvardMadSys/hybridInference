"""Add hard-delete claim lease metadata.

Two columns on ``users``:

- ``hard_delete_claimed_at`` (TIMESTAMPTZ, NULL): when the current
  ``hard_delete_pending`` claim was taken or last renewed. NULL on a pending
  claim means a claim written before this revision existed; code stamps it
  under row lock and refuses that request, so the next attempt sees a real
  timestamp and the recovery grace period runs from it.
- ``hard_delete_claim_recovered`` (BOOLEAN NOT NULL DEFAULT FALSE): whether
  the current claim was taken over from a stale one. A recovered claim is
  sticky: its owner may not release it after a pre-fence failure, because the
  worker it was recovered from may still be unwinding.

Adding a nullable column and a NOT NULL column with a constant default is a
catalog-only change in PostgreSQL: it takes the table lock briefly and
rewrites no user rows. ``compatibility = "expand"`` so earlier code, which
selects named columns and never sees the new ones, keeps running.

Backfill is bounded to one batch per upgrade over pending claims only
(typically zero or one row); a fresh database has no pending claims, so the
``UPDATE`` touches nothing.

Revision ID: 0016_hard_delete_claim_lease
Revises: 0015_projects
Create Date: 2026-11-05
"""

from __future__ import annotations

from alembic import op

revision = "0016_hard_delete_claim_lease"
down_revision = "0015_projects"
branch_labels = None
depends_on = None
compatibility = "expand"


def upgrade() -> None:
    """Add the claim-lease columns and backfill pending claims."""
    op.execute(
        "ALTER TABLE users "
        "ADD COLUMN IF NOT EXISTS hard_delete_claimed_at TIMESTAMPTZ, "
        "ADD COLUMN IF NOT EXISTS hard_delete_claim_recovered BOOLEAN NOT NULL DEFAULT FALSE"
    )
    op.execute(
        "UPDATE users SET hard_delete_claimed_at = NOW() "
        "WHERE hard_delete_pending = TRUE AND hard_delete_claimed_at IS NULL "
        "AND id IN (SELECT id FROM users WHERE hard_delete_pending = TRUE "
        "AND hard_delete_claimed_at IS NULL LIMIT 1000)"
    )


def downgrade() -> None:
    """Keep the columns: nullable / constant-default, unread by earlier code.

    Dropping them would be a contract change for a feature that is safe to
    leave in place, so this revision stays expand-only. ``upgrade`` adds them
    with ``IF NOT EXISTS``, so going back up after a downgrade still works.
    """
