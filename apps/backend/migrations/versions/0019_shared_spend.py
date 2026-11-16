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
primary key allows one). Both are receipts. One is needed for as long as the
process that made its batch may send that batch again, and no longer: it is
deleted, never before it is a week old, by its own process once the batch was
answered, and by any process once its maker is known to be gone
(:data:`serving.shared_spend.store.RECEIPT_RETENTION_SECONDS`).

``spend_instance_closures`` names the processes that will send nothing more.
One that shuts down with everything sent records itself; one that was killed
is recorded by an operator (``python -m serving.shared_spend reconcile``), and
that command retires what it left, each reservation with a row of
``spend_reconciliations``: what it held, what it was charged and on which
request-log rows, who said the process was gone and why. Those rows are an
audit record and are never deleted.

None of these name an account except ``spend_reservations``, which the hard
delete purges.

``spend_unidentified_writes`` is evidence, not enforcement: while the mode is
``shared``, a statement-level trigger on the three counter tables records any
write made outside the shared settlement (which marks its own transaction), so
a gateway that does not take part -- an older build, say -- is seen. It never
refuses the write. While the mode is ``local`` the trigger reads one row and
records nothing. It runs with the privileges of whoever writes the counters, so
that role needs to read ``spend_accounting`` (and, in the shared mode, to
insert into ``spend_unidentified_writes``), as it already needs to use every
table a revision adds.

Creating a trigger takes a SHARE ROW EXCLUSIVE lock on its table, so writes to
each counter table wait a moment while its trigger is created (reads do not).
The three tables are taken in the order the cost writer itself writes them,
and each wait is bounded by the migration's lock timeout: a transaction that
holds a counter table for longer fails this revision, which rolls back whole
and is run again. An account hard delete writes the same tables in another
order, so one running at that very moment can deadlock with this revision;
PostgreSQL then fails one of the two, nothing is half done, and it is run
again.

Revision ID: 0019_shared_spend
Revises: 0018_projects_owned_by_accounts
Create Date: 2026-11-14
"""

from __future__ import annotations

from alembic import op

revision = "0019_shared_spend"
down_revision = "0018_projects_owned_by_accounts"
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
                         CHECK (state IN ('active', 'quarantined')),
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
    # By maker and age: what the retention rule deletes, and what it asks of a
    # closure (does this instance still have receipts?).
    "CREATE INDEX IF NOT EXISTS idx_spend_settlements_instance "
    "ON spend_settlements(instance_id, settled_at)",
    """
    CREATE TABLE IF NOT EXISTS spend_settlement_members (
        reservation_id TEXT PRIMARY KEY,
        batch_id       TEXT NOT NULL,
        outcome        TEXT NOT NULL CHECK (outcome IN ('settled', 'released')),
        settled_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    # By batch: a batch's member receipts are deleted with it.
    "CREATE INDEX IF NOT EXISTS idx_spend_settlement_members_batch "
    "ON spend_settlement_members(batch_id)",
    """
    CREATE TABLE IF NOT EXISTS spend_instance_closures (
        instance_id TEXT PRIMARY KEY,
        closed_by   TEXT NOT NULL CHECK (closed_by IN ('shutdown', 'operator')),
        operator    TEXT,
        reason      TEXT,
        closed_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        CHECK (closed_by = 'shutdown' OR (operator IS NOT NULL AND reason IS NOT NULL))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS spend_reconciliations (
        reservation_id TEXT PRIMARY KEY,
        instance_id    TEXT NOT NULL,
        batch_id       TEXT NOT NULL,
        amount_usd     DECIMAL(14, 6) NOT NULL,
        operation      TEXT NOT NULL,
        admitted_at    TIMESTAMPTZ NOT NULL,
        outcome        TEXT NOT NULL CHECK (outcome IN ('settled', 'released')),
        cost_usd       DECIMAL(14, 6) NOT NULL DEFAULT 0 CHECK (cost_usd >= 0),
        evidence       JSONB NOT NULL DEFAULT '[]'::jsonb,
        owner_state    TEXT NOT NULL,
        operator       TEXT NOT NULL,
        reason         TEXT NOT NULL,
        reconciled_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
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
    # Created only where missing, so the revision can run again without
    # replacing (or first removing) anything.
    statements: list[str] = []
    for table in COUNTER_TABLES:
        name = f"spend_unidentified_{table}"
        statements.append(
            f"""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_trigger
                    WHERE tgname = '{name}' AND tgrelid = '{table}'::regclass
                ) THEN
                    CREATE TRIGGER {name} AFTER INSERT OR UPDATE ON {table}
                    FOR EACH STATEMENT EXECUTE FUNCTION spend_note_unidentified_write();
                END IF;
            END
            $$
            """
        )
    return tuple(statements)


TRIGGER_STATEMENTS = _trigger_statements()


def upgrade() -> None:
    """Create the shared-accounting tables, in the ``local`` mode, and the write trigger."""
    for statement in (*STATEMENTS, *TRIGGER_STATEMENTS):
        op.execute(statement)


def downgrade() -> None:
    """Keep everything: earlier code never reads these tables, and the trigger is inert locally."""
