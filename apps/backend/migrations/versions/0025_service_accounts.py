"""Flag admin-created service accounts on users.

A service account is a user row with no email and an unknowable password,
created by an administrator through ``POST /admin/service-accounts`` and
used only through its API keys. ``is_service_account`` marks the row so
the admin console can tell it from a person and so browser sign-in can
refuse it outright.

``NOT NULL DEFAULT FALSE`` is expand-safe: a row written by the previous
release, whose INSERT does not name the column, stays a regular account.

Revision ID: 0025_service_accounts
Revises: 0024_removed_provider_env_keys
Create Date: 2026-11-27
"""

from __future__ import annotations

from alembic import op

revision = "0025_service_accounts"
down_revision = "0024_removed_provider_env_keys"
branch_labels = None
depends_on = None
compatibility = "expand"


STATEMENTS: tuple[str, ...] = (
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_service_account BOOLEAN NOT NULL DEFAULT FALSE",
)


def upgrade() -> None:
    """Add the service-account flag. Existing accounts stay regular users."""
    for statement in STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    """Keep the column: unread by earlier code, and false for every row it wrote.

    ``upgrade`` adds it with ``IF NOT EXISTS``, so going back up after a
    downgrade still works.
    """
