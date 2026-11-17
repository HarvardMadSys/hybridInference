"""Single sign-on: linked identities, sign-in flows and pending links.

- ``user_identities``: which account an identity at an external identity
  provider signs in to. An identity is ``(issuer, subject)``; the email is the
  one seen when it was linked, kept for display and audit only. An account
  holds at most one identity per issuer.
- ``sso_flows``: one row per redirect to an identity provider. Only hashes of
  the ``state`` value and of the browser's binding cookie are stored; what the
  protocol needs meanwhile (for OpenID Connect, the nonce hash and the PKCE
  verifier) is one JSON value, so another protocol adds no column. A row is
  claimed exactly once. A re-authentication flow also carries the account, its
  session and what is being confirmed, and is spent a second time, once, by
  the request it confirms.
- ``sso_pending_links``: an identity that matched an existing account by email
  and waits for that account's owner to sign in and accept it. Only the hash of
  the browser's cookie is stored.

All three are expand-only: the new tables are invisible to earlier code.

Sequenced after ``0020_shared_spend``, the revision this one merged beside: two
heads break ``upgrade`` before either can run.

Revision ID: 0021_sso_identities
Revises: 0020_shared_spend
Create Date: 2026-11-13
"""

from __future__ import annotations

from alembic import op

revision = "0021_sso_identities"
down_revision = "0020_shared_spend"
branch_labels = None
depends_on = None
compatibility = "expand"


def upgrade() -> None:
    """Add the identity table and the two short-lived tables."""
    op.execute(
        "CREATE TABLE IF NOT EXISTS user_identities ("
        "id TEXT PRIMARY KEY, "
        "user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE, "
        "provider_id TEXT NOT NULL, "
        "issuer TEXT NOT NULL, "
        "subject TEXT NOT NULL, "
        "email TEXT, "
        "created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), "
        "last_login_at TIMESTAMPTZ, "
        "UNIQUE (issuer, subject), "
        "UNIQUE (user_id, issuer)"
        ")"
    )
    op.execute(
        "CREATE TABLE IF NOT EXISTS sso_flows ("
        "state_hash TEXT PRIMARY KEY, "
        "binding_hash TEXT NOT NULL, "
        "provider_id TEXT NOT NULL, "
        "purpose TEXT NOT NULL CHECK (purpose IN ('login', 'reauth', 'link')), "
        "return_to TEXT NOT NULL, "
        "protocol_state JSONB NOT NULL DEFAULT '{}'::jsonb, "
        "user_id TEXT REFERENCES users(id) ON DELETE CASCADE, "
        "sid TEXT, "
        "reauth_purpose TEXT, "
        "created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), "
        "expires_at TIMESTAMPTZ NOT NULL, "
        "consumed_at TIMESTAMPTZ, "
        "reauth_verified_at TIMESTAMPTZ, "
        "reauth_used_at TIMESTAMPTZ"
        ")"
    )
    op.execute("CREATE INDEX IF NOT EXISTS idx_sso_flows_expires_at ON sso_flows (expires_at)")
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_sso_flows_user_id ON sso_flows (user_id) "
        "WHERE user_id IS NOT NULL"
    )
    op.execute(
        "CREATE TABLE IF NOT EXISTS sso_pending_links ("
        "token_hash TEXT PRIMARY KEY, "
        "provider_id TEXT NOT NULL, "
        "issuer TEXT NOT NULL, "
        "subject TEXT NOT NULL, "
        "email TEXT NOT NULL, "
        "created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), "
        "expires_at TIMESTAMPTZ NOT NULL, "
        "consumed_at TIMESTAMPTZ"
        ")"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_sso_pending_links_expires_at "
        "ON sso_pending_links (expires_at)"
    )


def downgrade() -> None:
    """Keep all three: unread by earlier code, they are harmless there.

    ``upgrade`` adds them with ``IF NOT EXISTS``, so going back up after a
    downgrade still works.
    """
