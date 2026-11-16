"""Add what a second sign-in factor needs: the shared secret, recovery codes, and what a session has proved.

``user_two_factor`` holds one row per account that has an authenticator app,
or is in the middle of adding one. ``pending_secret_encrypted`` is a secret the
account has been shown but has not yet proved it can produce codes from; it
becomes ``secret_encrypted`` when the first code is given, so replacing a
device never leaves the account without a working one. A pending secret is the
business of the session that was shown it (``pending_session_id``), for a short
while (``pending_expires_at``) and a few tries (``pending_attempts_left``):
confirming it hands over the account's second factor, so it must not be
something another holder of the account's session can guess at. ``last_used_step`` is
the newest time step a code was accepted for, which is what makes a code good
once. ``failed_attempts`` and ``locked_until`` count wrong codes in a row and
hold the account's codes off for a while after too many: six digits are only
as strong as the number of guesses allowed at them, and that number has to be
the same in every process and survive a restart.

``two_factor_recovery_codes`` holds the hashes of the one-time codes an owner
keeps for the day the device is gone. They belong to the enrollment: removing
it removes them.

``auth_sessions`` gains when the session last gave a code and the kind of
client it gave it from. Both stay NULL for a session that never has.

Expand-only: two new tables and two nullable columns. The previous release
names its ``auth_sessions`` columns in every statement and reads neither table,
so it keeps working on the result; on a rollback the rows simply go unread, and
an account's enrollment is still there when the newer release returns.

Revision ID: 0019_two_factor
Revises: 0018_projects_owned_by_accounts
Create Date: 2026-11-15
"""

from __future__ import annotations

from alembic import op

revision = "0019_two_factor"
down_revision = "0018_projects_owned_by_accounts"
branch_labels = None
depends_on = None
compatibility = "expand"

#: The statements, in order.
STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS user_two_factor (
        user_id                  TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
        secret_encrypted         TEXT,
        pending_secret_encrypted TEXT,
        pending_session_id       TEXT,
        pending_expires_at       TIMESTAMPTZ,
        pending_attempts_left    INTEGER NOT NULL DEFAULT 0,
        confirmed_at             TIMESTAMPTZ,
        last_used_step           BIGINT,
        failed_attempts          INTEGER NOT NULL DEFAULT 0,
        locked_until             TIMESTAMPTZ,
        created_at               TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS two_factor_recovery_codes (
        user_id    TEXT NOT NULL REFERENCES user_two_factor(user_id) ON DELETE CASCADE,
        code_hash  TEXT NOT NULL,
        used_at    TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        PRIMARY KEY (user_id, code_hash)
    )
    """,
    "ALTER TABLE auth_sessions "
    "ADD COLUMN IF NOT EXISTS two_factor_verified_at TIMESTAMPTZ, "
    "ADD COLUMN IF NOT EXISTS two_factor_environment TEXT",
)


def upgrade() -> None:
    """Create the two tables and the two session columns."""
    for statement in STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    """Keep everything: both tables and both columns are unread by earlier code."""
