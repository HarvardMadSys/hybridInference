"""The schema the gateway built at startup, frozen on 2026-11-02 (``dev`` at ``73c5675b``).

``0001_baseline`` runs this code to bring any database to that schema: a new
database gets every table, an existing one whatever its own upgrade history
lacks. These are the startup builders themselves, moved here verbatim and run in
the order startup ran them: catalog first, strong-lock DDL only for what is
missing, every such statement under a bounded lock wait. Their comments still
describe the modules they came from.

Two things the builders did are not here, because they read runtime
configuration and so still happen at startup: the erasure-fence fingerprint
check and the ``ADMIN_EMAILS`` role seeding.

Frozen: a test pins these files' digest. A schema change is a new revision in
``versions/``, never an edit here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from migrations.baseline import (
    alert_history,
    app_config,
    batch,
    logging_store,
    operational_store,
    responses,
)

if TYPE_CHECKING:
    import asyncpg


async def run(conn: asyncpg.Connection) -> None:
    """Build the baseline schema on *conn*, or bring an existing one to it.

    Each step commits on its own, so a step that cannot get its lock in time
    (:class:`~migrations.baseline.ddl.SchemaLockUnavailable`) leaves the ones
    before it in place, and running this again carries on from there.
    """
    # Startup order: the configuration loads first, then the database logger,
    # the operational store, the response and batch stores; alert history last.
    await app_config.ensure_schema(conn, create_source_table=app_config.CREATE_TABLE_SQL)
    await logging_store.create_tables(conn)
    await operational_store.create_tables(conn)
    await responses.create_tables(conn)
    await batch.create_tables(conn)
    await alert_history.ensure_schema(conn)
