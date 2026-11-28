"""API key authorization: the one-time codes a user approves for an application.

``api_key_authorization_codes`` holds one row per approval on the console's
``/authorize/key`` page. The application exchanges the code, with its PKCE
verifier, for an API key on the user's own account
(``serving.servers.routers.key_authorization``).

- ``code_hash`` is the SHA-256 of the code; the code itself is never stored.
- ``callback_url`` is the callback the code was issued for, without its query
  string (the application's own state), so an exchange can re-check it against
  the allowlist.
- ``code_challenge`` is the application's S256 challenge.
- ``consumed_at`` is set by the one statement that claims a code, so a code is
  exchanged at most once.

A row leaves with its account. Rows expire within minutes and are swept when
later codes are issued.

Expand only: a new table that earlier code never reads.

Revision ID: 0028_key_authorization_codes
Revises: 0027_openinfra_archives
Create Date: 2026-11-28
"""

from __future__ import annotations

from alembic import op

revision = "0028_key_authorization_codes"
down_revision = "0027_openinfra_archives"
branch_labels = None
depends_on = None
compatibility = "expand"


#: The revision's statements, in order; each can run again (``IF NOT EXISTS``).
STATEMENTS: tuple[str, ...] = (
    "CREATE TABLE IF NOT EXISTS api_key_authorization_codes ("
    "code_hash TEXT PRIMARY KEY, "
    "user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE, "
    "callback_url TEXT NOT NULL, "
    "code_challenge TEXT NOT NULL, "
    "created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), "
    "expires_at TIMESTAMPTZ NOT NULL, "
    "consumed_at TIMESTAMPTZ"
    ")",
    "CREATE INDEX IF NOT EXISTS idx_api_key_authorization_codes_expires_at "
    "ON api_key_authorization_codes (expires_at)",
    # The foreign key's own lookup when an account is erased.
    "CREATE INDEX IF NOT EXISTS idx_api_key_authorization_codes_user_id "
    "ON api_key_authorization_codes (user_id)",
)


def upgrade() -> None:
    """Add the code table."""
    for statement in STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    """Keep the table: unread by earlier code, it is harmless there.

    ``upgrade`` adds it with ``IF NOT EXISTS``, so going back up after a
    downgrade still works.
    """
