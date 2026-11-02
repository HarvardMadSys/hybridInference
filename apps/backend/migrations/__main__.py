"""``python -m migrations``: apply the migrations, or report where a database stands.

A deploy runs ``upgrade`` with the image it is about to start, before it restarts
the gateway, and stops if it fails. The database is the one the gateway's
``DB_*`` settings name.

Exit status: 0 on success; 75 when the run could not get a lock in time (another
migration run, or a session such as a backup holding a table), so the same
command can simply be run again later; 1 on any other failure. ``status`` exits 1
when the gateway would refuse to start on the database.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from typing import Any

from migrations import LOCK_NOT_AVAILABLE, MigrationInProgress, alembic_config, sqlstate

#: ``EX_TEMPFAIL``: try again later.
EXIT_RETRY = 75

# The sessions holding locks on the gateway's tables, longest-running first.
_LOCK_HOLDERS_SQL = """
SELECT a.pid, a.application_name, a.state, a.xact_start,
       string_agg(DISTINCT c.relname || ' (' || l.mode || ')', ', ') AS locks
FROM pg_locks l
JOIN pg_stat_activity a ON a.pid = l.pid
JOIN pg_class c ON c.oid = l.relation
WHERE l.granted
  AND l.locktype = 'relation'
  AND a.datname = current_database()
  AND a.pid <> pg_backend_pid()
  AND c.relnamespace = 'public'::regnamespace
GROUP BY a.pid, a.application_name, a.state, a.xact_start
ORDER BY a.xact_start NULLS LAST
LIMIT 10
"""


async def _connect() -> Any:
    import asyncpg

    from serving.config.settings import DatabaseSettings

    settings = DatabaseSettings()
    return await asyncpg.connect(
        host=settings.db_host,
        port=settings.db_port,
        user=settings.db_user,
        password=settings.db_password or None,
        database=settings.db_name,
    )


async def _lock_holders() -> list[str]:
    conn = await _connect()
    try:
        rows = await conn.fetch(_LOCK_HOLDERS_SQL)
    finally:
        await conn.close()
    return [
        f"pid {r['pid']} {r['application_name'] or '(no application_name)'}, {r['state']}, "
        f"transaction since {r['xact_start']}: {r['locks']}"
        for r in rows
    ]


def _database_enabled() -> bool:
    """``DB_ENABLED``, read as ``serving.servers.deps.database_enabled`` reads it."""
    return os.getenv("DB_ENABLED", "true").lower() not in ("false", "0", "no")


def upgrade(target: str) -> int:
    """Apply every revision up to *target*; a lock timeout exits :data:`EXIT_RETRY`."""
    from alembic import command

    if not _database_enabled():
        # A deployment without a database (DB_ENABLED=false) has nothing to
        # migrate, so a deploy script can run this step unconditionally.
        print("DB_ENABLED is off: no database to migrate.")
        return 0
    try:
        command.upgrade(alembic_config(), target)
    except MigrationInProgress as exc:
        print(f"migration not run: {exc}", file=sys.stderr)
        return EXIT_RETRY
    except Exception as exc:
        if sqlstate(exc) != LOCK_NOT_AVAILABLE:
            raise
        print(
            f"migration stopped: a table lock was not available in time ({exc}). Nothing "
            "after the last completed step changed; run the migration again once the "
            "session holding the lock has finished.",
            file=sys.stderr,
        )
        try:
            holders = asyncio.run(_lock_holders())
        except Exception as report_exc:  # the report is a courtesy; keep the real error
            print(f"(could not list the sessions holding locks: {report_exc})", file=sys.stderr)
        else:
            for line in holders or ["(no session holds a lock on the gateway's tables now)"]:
                print(f"  {line}", file=sys.stderr)
        return EXIT_RETRY
    return 0


async def _status() -> int:
    from migrations.compat import Verdict, code_chain, describe, judge, read_state

    chain = code_chain()
    conn = await _connect()
    try:
        state = await read_state(conn)
    finally:
        await conn.close()
    verdict = judge(state, chain)
    print(f"database revision: {state.revision or '(none)'}")
    print(f"compatibility floor: {state.floor or '(none)'}")
    print(f"this code's head: {chain[-1]}")
    print(f"{verdict.value}: {describe(verdict, state, chain)}")
    return 0 if verdict in (Verdict.CURRENT, Verdict.NEWER_COMPATIBLE) else 1


def main(argv: list[str] | None = None) -> int:
    """Run the command line."""
    parser = argparse.ArgumentParser(
        prog="python -m migrations", description=__doc__.split("\n")[0]
    )
    commands = parser.add_subparsers(dest="command", required=True)
    up = commands.add_parser("upgrade", help="apply the pending revisions (the deploy step)")
    up.add_argument("--to", default="head", help="the revision to stop at (default: head)")
    commands.add_parser("status", help="show the database's revision against this code's")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("alembic.runtime.plugins").setLevel(logging.WARNING)
    if args.command == "upgrade":
        return upgrade(args.to)
    return asyncio.run(_status())


if __name__ == "__main__":
    sys.exit(main())
