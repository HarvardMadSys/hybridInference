"""Credential scopes on API keys, agent invite tokens, and the provisioning link.

- ``api_keys.credential_scope``: nullable; NULL is an ordinary key. A scoped
  key (``agent``) reaches only catalog models declaring that scope, and such
  models answer only to scoped keys (``serving.model_access``).
- ``agent_invite_tokens``: one row per invitation a detached agent service
  emails to an approved user there. Only the token hash is stored; a row is
  claimed exactly once, bound to the email that was invited.
- ``agent_provisioned_accounts``: one row per account redeemed from such an
  invitation, keyed by the agent account id, so redemption is idempotent and
  its scoped key can be revoked when access ends there.

All three are expand-only: earlier code selects named columns and never
reads them, and the new tables are invisible to it.

Revision ID: 0016_agent_provisioning
Revises: 0015_projects
Create Date: 2026-11-13
"""

from __future__ import annotations

from alembic import op

revision = "0016_agent_provisioning"
down_revision = "0015_projects"
branch_labels = None
depends_on = None
compatibility = "expand"


def upgrade() -> None:
    """Add the scope column, the invite-token table, and the link table."""
    op.execute("ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS credential_scope TEXT")
    op.execute(
        "CREATE TABLE IF NOT EXISTS agent_invite_tokens ("
        "token_hash TEXT PRIMARY KEY, "
        "agent_account_id TEXT NOT NULL, "
        "email TEXT NOT NULL, "
        "created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), "
        "expires_at TIMESTAMPTZ NOT NULL, "
        "consumed_at TIMESTAMPTZ, "
        "superseded_at TIMESTAMPTZ"
        ")"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_invite_tokens_agent_account_id "
        "ON agent_invite_tokens (agent_account_id)"
    )
    op.execute(
        "CREATE TABLE IF NOT EXISTS agent_provisioned_accounts ("
        "agent_account_id TEXT PRIMARY KEY, "
        "user_id TEXT NOT NULL, "
        "api_key_id BIGINT, "
        "created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), "
        "revoked_at TIMESTAMPTZ"
        ")"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_provisioned_accounts_user_id "
        "ON agent_provisioned_accounts (user_id)"
    )


def downgrade() -> None:
    """Keep all three: unread by earlier code, they are harmless there.

    ``upgrade`` adds them with ``IF NOT EXISTS``, so going back up after a
    downgrade still works.
    """
