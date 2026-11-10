"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}
"""

# The previous release keeps serving while this runs, and goes on running on
# the result if its deploy is rolled back. Say which this revision is, below:
# "expand" when code of earlier revisions keeps working on its result (a new
# table, a nullable or defaulted column, a plain index); "contract" when it does
# not (a drop, rename, retype or tightening of what earlier code uses, a NOT
# NULL column without a default, a unique index or constraint, dropping a
# unique index an ON CONFLICT names). A contract calls
# ``migrations.compat.raise_compat_floor(<the marker revision of the release
# that stopped using it>, by=revision)`` (see the design record). A test fails
# until this is set. Statements wait at most 3 s for a table lock; an index on a
# large table belongs to the background builder, not here. A revision that has
# run on a shared database is never edited or reverted: fix it with a new one.

from __future__ import annotations

from alembic import op
${imports if imports else ""}
revision = ${repr(up_revision)}
down_revision = ${repr(down_revision)}
branch_labels = ${repr(branch_labels)}
depends_on = ${repr(depends_on)}
compatibility = None  # "expand" or "contract"; see above


def upgrade() -> None:
    """Apply this revision."""
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    """Revert this revision (development and tests only)."""
    ${downgrades if downgrades else "pass"}
