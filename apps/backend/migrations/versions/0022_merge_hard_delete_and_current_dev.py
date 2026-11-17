"""Join the hard-delete lease branch with the current dev migration chain.

The claim-lease revision was authored from the 0016 schema. Current dev
continued through 0021_sso_identities independently, so this no-op merge
revision records that both histories are present before later migrations run.

Revision ID: 0022_merge_hard_delete_and_current_dev
Revises: 0016_hard_delete_claim_lease, 0021_sso_identities
Create Date: 2026-11-17
"""

from __future__ import annotations

revision = "0022_merge_hard_delete_and_current_dev"
down_revision = ("0016_hard_delete_claim_lease", "0021_sso_identities")
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Record the merge of the two already-applied schema histories."""


def downgrade() -> None:
    """Remove only the merge marker; its parent migrations remain expand-only."""
