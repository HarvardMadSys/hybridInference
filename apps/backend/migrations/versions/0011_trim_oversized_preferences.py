"""Trim the prober layouts stored before an account's preferences were bounded.

An account holder's saved llm-prober layout (``users.preferences ->
'llm_prober_layout'``) had no size bound, and the lookups every request makes
with the account's keys read the account's preferences document. A layout of
tens of MiB cost the database a decompression of it on each of them. Writes are
bounded now, to 16 KiB for the whole document, and those lookups take only the
entries they act on, but a document stored before keeps its size, and reading
any entry of it still decompresses all of it.

This revision removes the layout from every document over 64 KiB of JSON text,
four times the bound, which only a write made before the bound can have left.
The account's layout then reads as the default, exactly as after the account
holder resets it (``DELETE /user/preferences/llm-prober-layout``), in this
release and in the previous one; every other entry stays as it is. A document
of at most 1 KiB as stored is not read at all: decompressing it costs nothing
worth saving.

The ``UPDATE`` takes row locks on the documents it trims only, and waits for
each at most the run's 3 s (``env.DDL_LOCK_WAIT``); behind a long write to one
of those rows the run fails, changes nothing, and can be run again. Running it
again finds nothing more to trim.

Revision ID: 0011_trim_oversized_preferences
Revises: 0010_storage_indexes
Create Date: 2026-11-08
"""

from __future__ import annotations

from alembic import op

revision = "0011_trim_oversized_preferences"
down_revision = "0010_storage_indexes"
branch_labels = None
depends_on = None
compatibility = "expand"

#: The statements, in order.
STATEMENTS = (
    "UPDATE users SET preferences = preferences - 'llm_prober_layout' "
    "WHERE pg_column_size(preferences) > 1024 "
    "AND octet_length(preferences::text) > 65536 "
    "AND preferences -> 'llm_prober_layout' IS NOT NULL",
)


def upgrade() -> None:
    """Remove the prober layout from every oversized preferences document."""
    for statement in STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    """Nothing to restore: a trimmed layout reads as the default in every release."""
