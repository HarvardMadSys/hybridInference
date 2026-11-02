"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}
"""

# Expand by default: the previous release keeps serving while this runs, and
# goes on running on the result if its deploy is rolled back. A revision that
# drops, renames or narrows what earlier code uses calls
# ``migrations.compat.raise_compat_floor(<the marker revision of the release
# that stopped using it>, by=revision)`` (see the design record). Statements
# wait at most 3 s for a table lock; an index on a large table belongs to the
# background builder, not here.

from __future__ import annotations

from alembic import op
${imports if imports else ""}
revision = ${repr(up_revision)}
down_revision = ${repr(down_revision)}
branch_labels = ${repr(branch_labels)}
depends_on = ${repr(depends_on)}


def upgrade() -> None:
    """Apply this revision."""
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    """Revert this revision (development and tests only)."""
    ${downgrades if downgrades else "pass"}
