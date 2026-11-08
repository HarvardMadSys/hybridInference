"""The purge of ended sessions' rows, without a database.

``tests/integration/storage/test_session_retention_postgres.py`` runs it against
PostgreSQL.
"""

from __future__ import annotations

import inspect
from typing import Any

from serving.storage import session_retention


class _Conn:
    def __init__(self, deleted: list[int]) -> None:
        self.deleted = list(deleted)
        self.executed: list[tuple[str, tuple[Any, ...]]] = []

    async def execute(self, sql: str, *args: Any) -> str:
        self.executed.append((sql, args))
        return f"DELETE {self.deleted.pop(0)}"


class _Pool:
    def __init__(self, conn: _Conn) -> None:
        self.conn = conn
        self.acquired = 0

    def acquire(self) -> Any:
        pool = self

        class _Acquired:
            async def __aenter__(self) -> _Conn:
                pool.acquired += 1
                return pool.conn

            async def __aexit__(self, *exc: object) -> None:
                return None

        return _Acquired()


async def test_it_deletes_in_batches_until_one_comes_back_short():
    conn = _Conn([2, 2, 1])
    pool = _Pool(conn)

    assert await session_retention.purge_ended(pool, batch=2) == 5

    assert [args for _, args in conn.executed] == [(24, 2)] * 3
    # Each statement on a connection of its own, given back in between.
    assert pool.acquired == 3


async def test_a_run_stops_after_its_batches_and_leaves_the_rest_for_the_next():
    conn = _Conn([2] * 10)

    assert await session_retention.purge_ended(_Pool(conn), batch=2, max_batches=3) == 6

    assert len(conn.executed) == 3


async def test_the_defaults_keep_ended_sessions_a_day_in_bounded_statements():
    conn = _Conn([0])

    assert await session_retention.purge_ended(_Pool(conn)) == 0

    assert conn.executed == [(session_retention._PURGE_SQL, (24, session_retention.PURGE_BATCH))]
    assert session_retention.ENDED_GRACE_HOURS == 24
    assert session_retention.PURGE_BATCH * session_retention.PURGE_MAX_BATCHES <= 100_000


def test_only_ended_sessions_match():
    """Expired, or revoked and unused since: a live session never matches."""
    sql = " ".join(session_retention._PURGE_SQL.split())
    assert "expires_at < now() - make_interval(hours => $1)" in sql
    assert "revoked IS TRUE AND COALESCE(last_used_at, created_at, expires_at)" in sql
    assert "LIMIT $2" in sql


async def test_the_hourly_job_never_raises(caplog):
    class _Broken:
        def acquire(self) -> Any:
            raise RuntimeError("pool closed")

    await session_retention.retention_job(_Broken())

    assert "auth_sessions_retention failed" in caplog.text


def test_the_job_is_registered_hourly_clear_of_the_others():
    class _Scheduler:
        def __init__(self) -> None:
            self.jobs: list[dict[str, Any]] = []

        def add_job(self, func: Any, **kwargs: Any) -> None:
            self.jobs.append({"func": func, **kwargs})

    scheduler = _Scheduler()
    pool = object()
    session_retention.register_retention_job(scheduler, pool)

    (job,) = scheduler.jobs
    assert job["func"] is session_retention.retention_job
    assert job["args"] == [pool]
    assert job["id"] == "auth_sessions_retention"
    assert (job["replace_existing"], job["coalesce"], job["max_instances"]) == (True, True, 1)
    assert str(job["trigger"].fields[6]) == "45"  # minute


def test_bootstrap_registers_it_on_the_general_pool():
    from serving.servers import bootstrap

    source = inspect.getsource(bootstrap)
    assert "session_retention.register_retention_job(sched, db_logger.pool)" in source
