"""Revoke the API keys that carry a credential scope.

``0016_agent_provisioning`` let a key carry a scope (``agent``), and the
release that shipped it answered such a key only for catalog models naming
that scope. The model gate and the invitations that issued those keys are
withdrawn. Without the gate a scoped key would be an ordinary key, reaching
every model its account's role allows: more than it was issued for. This
revision revokes every active one, so a key issued to reach a reserved model
and nothing else never comes to reach the rest.

Nothing else of ``0016`` is undone here. The ``credential_scope`` column and
the two invitation tables stay: the previous release still reads them, and
removing them belongs to a later contract revision.

The ``UPDATE`` touches scoped keys only, which a database that never issued
one does not have. A gateway that caches authenticated keys keeps answering a
revoked one until its cache entry expires or the process restarts, as with any
revocation written outside the service.

Revision ID: 0017_revoke_scoped_keys
Revises: 0016_agent_provisioning
Create Date: 2026-11-13
"""

from __future__ import annotations

from alembic import op

revision = "0017_revoke_scoped_keys"
down_revision = "0016_agent_provisioning"
branch_labels = None
depends_on = None
compatibility = "expand"

#: The statements, in order.
STATEMENTS = (
    "UPDATE api_keys SET status = 'revoked', revoked_at = NOW() "
    "WHERE credential_scope IS NOT NULL AND status = 'active'",
)


def upgrade() -> None:
    """Revoke every active key that carries a credential scope."""
    for statement in STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    """Nothing to restore: a revoked key stays revoked in every release."""
