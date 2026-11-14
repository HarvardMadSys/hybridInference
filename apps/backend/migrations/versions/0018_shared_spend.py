"""Add the tables shared spend accounting keeps its reservations and settlements in.

Expand-only. ``spend_accounting`` holds the deployment's accounting mode, one
row, created ``local``: every gateway keeps today's per-process accounting,
and code older than this revision never reads the table. Nothing in this
release changes the row; switching to ``shared`` is a future, separately
reviewed offline transition.

In the shared mode an admitted request's estimated cost is a row of
``spend_reservations`` until the batch that settles it retires the row and adds
its actual cost to the counters in the same transaction. ``spend_settlements``
records each batch by its id and payload digest, so a batch sent again after
its COMMIT's answer was lost is recognised rather than counted twice, and
``spend_settlement_members`` records which batch retired each reservation (its
primary key allows one). Both are receipts and are never deleted by the
gateway. None of these three name an account except ``spend_reservations``,
which the hard delete purges.

``spend_unidentified_writes`` is evidence, not enforcement: while the mode is
``shared``, a statement-level trigger on the three counter tables records any
write made outside the shared settlement (which marks its own transaction), so
a gateway that does not take part -- an older build, say -- is seen. It never
refuses the write. While the mode is ``local`` the trigger reads one row and
records nothing.

Revision ID: 0018_shared_spend
Revises: 0017_revoke_scoped_keys
Create Date: 2026-11-14
"""

from __future__ import annotations

from alembic import op

revision = "0018_shared_spend"
down_revision = "0017_revoke_scoped_keys"
branch_labels = None
depends_on = None
compatibility = "expand"

#: The counter tables whose writes the trigger watches.
COUNTER_TABLES = ("user_daily_cost", "api_key_daily_cost", "spend_counters")

#: The setting the shared settlement sets, transaction-locally, on its writes.
WRITER_SETTING = "hybridinference.spend_writer"

STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS spend_accounting (
        id         BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (id),
        mode       TEXT NOT NULL DEFAULT 'local' CHECK (mode IN ('local', 'shared')),
        changed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        changed_by TEXT
    )
    """,
    "INSERT INTO spend_accounting (id, mode) VALUES (TRUE, 'local') ON CONFLICT (id) DO NOTHING",
    """
    CREATE TABLE IF NOT EXISTS spend_reservations (
        reservation_id TEXT PRIMARY KEY,
        instance_id    TEXT NOT NULL,
        user_id        TEXT NOT NULL,
        api_key_id     BIGINT,
        project_id     TEXT,
        amount_usd     DECIMAL(14, 6) NOT NULL CHECK (amount_usd >= 0),
        check_day      TEXT NOT NULL,
        operation      TEXT NOT NULL,
        digest         TEXT NOT NULL,
        state          TEXT NOT NULL DEFAULT 'active'
                         CHECK (state IN ('active', 'unresolved', 'quarantined')),
        liability_day  TEXT,
        cost_usd       DECIMAL(14, 6),
        note           TEXT,
        created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        CHECK (state = 'active' OR liability_day IS NOT NULL)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_spend_reservations_user ON spend_reservations(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_spend_reservations_key "
    "ON spend_reservations(api_key_id) WHERE api_key_id IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_spend_reservations_project "
    "ON spend_reservations(project_id) WHERE project_id IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_spend_reservations_instance ON spend_reservations(instance_id)",
    """
    CREATE TABLE IF NOT EXISTS spend_settlements (
        batch_id       TEXT PRIMARY KEY,
        instance_id    TEXT NOT NULL,
        payload_digest TEXT NOT NULL,
        result         JSONB NOT NULL DEFAULT '{}'::jsonb,
        settled_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_spend_settlements_settled_at ON spend_settlements(settled_at)",
    """
    CREATE TABLE IF NOT EXISTS spend_settlement_members (
        reservation_id TEXT PRIMARY KEY,
        batch_id       TEXT NOT NULL,
        outcome        TEXT NOT NULL
                         CHECK (outcome IN ('settled', 'released', 'unresolved')),
        settled_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_spend_settlement_members_settled_at "
    "ON spend_settlement_members(settled_at)",
    """
    CREATE TABLE IF NOT EXISTS spend_unidentified_writes (
        id               BIGSERIAL PRIMARY KEY,
        table_name       TEXT NOT NULL,
        application_name TEXT,
        backend_pid      INTEGER,
        written_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    f"""
    CREATE OR REPLACE FUNCTION spend_note_unidentified_write() RETURNS trigger
    LANGUAGE plpgsql AS $$
    BEGIN
        IF COALESCE(current_setting('{WRITER_SETTING}', true), '') <> 'shared'
           AND EXISTS (SELECT 1 FROM spend_accounting WHERE mode = 'shared') THEN
            INSERT INTO spend_unidentified_writes (table_name, application_name, backend_pid)
            VALUES (TG_TABLE_NAME, current_setting('application_name', true), pg_backend_pid());
        END IF;
        RETURN NULL;
    END
    $$
    """,
)


def _trigger_statements() -> tuple[str, ...]:
    statements: list[str] = []
    for table in COUNTER_TABLES:
        name = f"spend_unidentified_{table}"
        statements.append(f"DROP TRIGGER IF EXISTS {name} ON {table}")
        statements.append(
            f"CREATE TRIGGER {name} AFTER INSERT OR UPDATE ON {table} "
            "FOR EACH STATEMENT EXECUTE FUNCTION spend_note_unidentified_write()"
        )
    return tuple(statements)


TRIGGER_STATEMENTS = _trigger_statements()


def upgrade() -> None:
    """Create the shared-accounting tables, in the ``local`` mode, and the write trigger."""
    for statement in (*STATEMENTS, *TRIGGER_STATEMENTS):
        op.execute(statement)


def downgrade() -> None:
    """Keep everything: earlier code never reads these tables, and the trigger is inert locally."""
