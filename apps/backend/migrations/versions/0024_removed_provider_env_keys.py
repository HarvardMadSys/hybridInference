"""Mark an env-sourced provider key as removed, not merely disabled.

``disabled_provider_env_keys.removed`` distinguishes a key an administrator
deleted from one they disabled. Disabled keys stay listed so they can be
turned back on. Removed keys stay tombstoned — every worker still excludes
them from its pools — but the admin list hides them, and the saved setting
is cleared so the environment cannot put the secret back.

``NOT NULL DEFAULT FALSE`` is expand-safe: a row written by the previous
release, whose INSERT does not name the column, stays re-enableable.

Revision ID: 0024_removed_provider_env_keys
Revises: 0023_user_tags
Create Date: 2026-11-27
"""

from __future__ import annotations

from alembic import op

revision = "0024_removed_provider_env_keys"
down_revision = "0023_user_tags"
branch_labels = None
depends_on = None
compatibility = "expand"


STATEMENTS: tuple[str, ...] = (
    "ALTER TABLE disabled_provider_env_keys "
    "ADD COLUMN IF NOT EXISTS removed BOOLEAN NOT NULL DEFAULT FALSE",
)


def upgrade() -> None:
    """Add the removed flag. Existing tombstones stay disabled, not removed."""
    for statement in STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    """Keep the column: unread by earlier code, and false for every row it wrote.

    ``upgrade`` adds it with ``IF NOT EXISTS``, so going back up after a
    downgrade still works.
    """
