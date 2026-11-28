"""OpenInfra: per-second samples, and a record of the weeks archived.

From this release a reporter posts a snapshot every second and the gateway
keeps a sample per host, GPU and engine per second: a sample's ``bucket`` is
``ts / 1000``, where ``0026_openinfra`` made it ``ts / 6000``. The samples
stored before are given their second's bucket. That keeps every one of them:
a 6-second bucket held one sample, and no second straddles two 6-second
buckets. A sample whose second is already held by one with a per-second bucket
gives way to it.

``openinfra_archives`` records each week (from Monday 00:00 UTC,
``week_start``) whose rows the scheduler has written to the archive
directory: where, and how many rows of each table
(``serving.openinfra.archive``). Samples and request events past the
retention are deleted, and while there is an archive directory only those of
archived weeks.

Expand: a new table, and sample rows the previous release still reads. It
selects samples by ``bucket`` and ``ts`` together, and a per-second bucket is
always larger than any 6-second one, so ``ts`` decides; only its rollup
refresh selects by ``bucket`` alone, and leaves converted samples out of the
two buckets it recomputes. The samples it writes after this revision keep
6-second buckets, and this release's scheduler converts them on its next run
(``serving.openinfra.store.convert_six_second_buckets``).

Revision ID: 0027_openinfra_archives
Revises: 0026_openinfra
Create Date: 2026-11-28
"""

from __future__ import annotations

from alembic import op

revision = "0027_openinfra_archives"
down_revision = "0026_openinfra"
branch_labels = None
depends_on = None
compatibility = "expand"


#: Each sample table and the column that leads its primary key.
_SAMPLES = (
    ("openinfra_cpu_samples", "node_id"),
    ("openinfra_gpu_samples", "gpu_id"),
    ("openinfra_engine_samples", "engine_id"),
)


def _to_seconds(table: str, key: str) -> tuple[str, str]:
    """A sample table's 6-second buckets made per-second ones.

    First the 6-second samples whose second already has a sample of its own
    go, then the rest move to their second.
    """
    return (
        f"DELETE FROM {table} o "
        "WHERE o.bucket = o.ts / 6000 AND o.bucket <> o.ts / 1000 "
        f"AND EXISTS (SELECT 1 FROM {table} n WHERE n.{key} = o.{key} AND n.bucket = o.ts / 1000)",
        f"UPDATE {table} SET bucket = ts / 1000 WHERE bucket = ts / 6000 AND bucket <> ts / 1000",
    )


#: The revision's statements, in order; each can run again.
STATEMENTS: tuple[str, ...] = (
    "CREATE TABLE IF NOT EXISTS openinfra_archives ("
    "week_start BIGINT PRIMARY KEY, "
    "location TEXT NOT NULL, "
    "tables JSONB NOT NULL, "
    "archived_at BIGINT NOT NULL"
    ")",
    *(statement for table, key in _SAMPLES for statement in _to_seconds(table, key)),
)


def upgrade() -> None:
    """Add the archive record and give the stored samples per-second buckets."""
    for statement in STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    """Keep the table and the buckets: the previous release ignores one and reads the other.

    ``upgrade`` creates the table with ``IF NOT EXISTS`` and converts only
    6-second buckets, so going back up after a downgrade still works.
    """
