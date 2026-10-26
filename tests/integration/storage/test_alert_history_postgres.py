"""Alert history against a real PostgreSQL: identity, conflicts, erasure, reads.

Every row this file writes uses the ``ahtest-`` namespace or a synthetic user
id, and only those rows are removed afterwards.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid

import pytest
import pytest_asyncio

from serving.observability.alert_history.capture import BackendIdentity
from serving.observability.alert_history.queue import HistoryQueue, HistoryWriter
from serving.observability.alert_history.records import HistoryRecord, finalize
from serving.observability.alert_history.runtime import AlertHistoryRuntime
from serving.storage import alert_history_store as store
from serving.storage.log_schema import (
    establish_erasure_fence,
    fence_account_advisory_key,
    fence_account_digest,
)
from serving.storage.postgres_log import PostgresLogStore
from tests.fixtures import postgres

pytestmark = [pytest.mark.dbtest]

SECRET = postgres.FENCE_SECRET
NS = "ahtest-ns"


def rec(origin: str, *, ns: str = NS, subjects=(), refs=(), **over) -> HistoryRecord:
    values = {
        "source": "backend",
        "source_namespace": ns,
        "origin_record_id": origin,
        "kind": "condition_triggered",
        "occurred_at": dt.datetime(2026, 10, 26, 8, 0, tzinfo=dt.UTC),
        "deployment_id": "ahtest-dep",
        "target_environment": "test",
        "facts": {"count": 3},
        "subject_digests": tuple(subjects),
        "request_refs": tuple(refs),
    }
    values.update(over)
    return finalize(HistoryRecord(**values))


@pytest_asyncio.fixture
async def pool():
    pool = await postgres.create_pool(min_size=1, max_size=8)
    async with pool.acquire() as conn:
        await store.ensure_schema(conn)

    async def wipe():
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM alert_history_events WHERE source_namespace LIKE 'ahtest-%'"
            )
            await conn.execute(
                "DELETE FROM alert_history_sources WHERE source_namespace LIKE 'ahtest-%'"
            )
            await conn.execute("DELETE FROM api_logs WHERE request_id LIKE 'ahtest-%'")
            digests = [fence_account_digest(f"ahtest-user-{n}", SECRET) for n in range(10)]
            await conn.execute(
                "DELETE FROM erasure_fence WHERE account_digest = ANY($1::text[])", digests
            )

    await wipe()
    try:
        yield pool
    finally:
        await wipe()
        await pool.close()


async def write(pool, records, **kw):
    async with pool.acquire() as conn, conn.transaction():
        return await store.write_records(conn, records, **kw)


async def count(pool, where="TRUE", *args):
    async with pool.acquire() as conn:
        return await conn.fetchval(
            f"SELECT count(*) FROM alert_history_events WHERE source_namespace LIKE 'ahtest-%' AND {where}",
            *args,
        )


async def test_schema_is_idempotent(pool):
    async with pool.acquire() as conn:
        await store.ensure_schema(conn)
        await store.ensure_schema(conn)
        assert await store.tables_present(conn)
        assert not await store.missing_schema_objects(conn)


async def test_insert_duplicate_and_conflict(pool):
    first = rec("o:1")
    (inserted,) = await write(pool, [first])
    assert inserted.status == "inserted"
    (dup,) = await write(pool, [rec("o:1")])
    assert (dup.status, dup.record_id) == ("duplicate", inserted.record_id)
    changed = rec("o:1", facts={"count": 4})
    (conflict,) = await write(pool, [changed])
    assert (conflict.status, conflict.record_id) == ("conflict", inserted.record_id)
    # The stored fact is untouched; the losing version is quarantined by digest.
    async with pool.acquire() as conn:
        facts = await conn.fetchval(
            "SELECT facts FROM alert_history_events WHERE origin_record_id = 'o:1' AND source_namespace = $1",
            NS,
        )
        quarantine = await conn.fetch(
            "SELECT facts FROM alert_history_events WHERE kind = 'history_conflict' AND source_namespace = $1",
            NS,
        )
    assert '"count": 3' in facts
    assert len(quarantine) == 1 and changed.payload_digest in quarantine[0]["facts"]
    # Re-sending the same conflicting version does not pile up quarantine rows.
    await write(pool, [changed])
    assert await count(pool, "kind = 'history_conflict'") == 1


async def test_one_bad_record_does_not_poison_the_batch(pool):
    bad = rec("o:bad\x00")  # bypasses capture's cleaning on purpose
    results = await write(pool, [rec("o:a"), bad, rec("o:b")])
    assert [r.status for r in results] == ["inserted", "invalid", "inserted"]
    assert await count(pool) == 2


async def test_all_or_nothing_rolls_back(pool):
    with pytest.raises(store.InvalidRecords) as raised:
        await write(pool, [rec("o:c"), rec("o:bad2\x00")], all_or_nothing=True)
    assert raised.value.indexes == [1]
    assert await count(pool) == 0


async def test_record_about_an_erased_subject_is_not_written(pool):
    digest = fence_account_digest("ahtest-user-1", SECRET)
    async with pool.acquire() as conn, conn.transaction():
        await establish_erasure_fence(conn, fence_key=digest)
    results = await write(pool, [rec("o:s1", subjects=[digest]), rec("o:s2")])
    assert [r.status for r in results] == ["erased", "inserted"]


async def test_delayed_write_waits_for_an_erasure_in_progress(pool):
    digest = fence_account_digest("ahtest-user-2", SECRET)
    eraser = await pool.acquire()
    tx = eraser.transaction()
    await tx.start()
    await eraser.execute("SELECT pg_advisory_xact_lock($1)", fence_account_advisory_key(digest))
    writer = asyncio.create_task(write(pool, [rec("o:late", subjects=[digest])]))
    # Wait until the write is blocked on the fence lock, not by sleeping blindly.
    async with pool.acquire() as probe:
        for _ in range(200):
            waiting = await probe.fetchval(
                "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted"
            )
            if waiting:
                break
            await asyncio.sleep(0.01)
        assert waiting
    assert not writer.done()
    await establish_erasure_fence(eraser, fence_key=digest)
    await tx.commit()
    await pool.release(eraser)
    (result,) = await asyncio.wait_for(writer, 5)
    assert result.status == "erased"
    assert await count(pool) == 0


async def test_hard_delete_purges_history_about_the_user(pool):
    user = "ahtest-user-3"
    digest = fence_account_digest(user, SECRET)
    await write(pool, [rec("o:h1", subjects=[digest]), rec("o:h2")])
    counts = await PostgresLogStore(pool, fence_secret=SECRET).hard_delete_user_data(user)
    assert counts["alert_history_events"] == 1
    assert await count(pool) == 1
    # And a buffered record about them, written afterwards, finds the tombstone.
    (late,) = await write(pool, [rec("o:h3", subjects=[digest])])
    assert late.status == "erased"


async def test_source_snapshot_never_moves_backwards(pool):
    base = {
        "source": "backend",
        "source_namespace": NS,
        "source_instance_id": "be_ahtest",
        "deployment_id": "ahtest-dep",
        "target_environment": "test",
    }
    newer = dt.datetime(2026, 10, 26, 9, tzinfo=dt.UTC)
    older = dt.datetime(2026, 10, 26, 8, tzinfo=dt.UTC)
    async with pool.acquire() as conn:
        await store.upsert_source(
            conn,
            {**base, "pending_count": 5, "pending_reported_at": newer, "observed_at": newer},
            wrote_events=True,
        )
        await store.upsert_source(
            conn,
            {**base, "pending_count": 99, "pending_reported_at": older, "observed_at": older},
            wrote_events=False,
        )
        row = await conn.fetchrow(
            "SELECT * FROM alert_history_sources WHERE source_instance_id = 'be_ahtest'"
        )
    assert row["pending_count"] == 5
    assert row["last_observed_at"] == newer
    assert row["last_write_at"] is not None


async def test_keyset_pages_follow_recording_order_for_sequential_writes(pool):
    early = dt.datetime(2026, 10, 26, 1, tzinfo=dt.UTC)
    for n in range(5):
        await write(pool, [rec(f"o:p{n}", occurred_at=early + dt.timedelta(minutes=n))])
    window = {"since": early - dt.timedelta(hours=1), "until": early + dt.timedelta(hours=2)}
    scope = store.Scope(deployment_ids=("ahtest-dep",), target_environments=("test",))
    async with pool.acquire() as conn:
        page = await store.list_events(conn, scope=scope, filters={}, after=None, limit=3, **window)
    assert [r["origin_record_id"] for r in page[:3]] == ["o:p0", "o:p1", "o:p2"]
    cursor = (page[2]["recorded_at"], page[2]["record_id"])
    # A record that *occurred* first but is inserted (and committed) after the
    # cursor was taken has a later recorded_at, so it is on a later page. This
    # holds only for writes that do not overlap; see the next test.
    await write(pool, [rec("o:late-early", occurred_at=early - dt.timedelta(minutes=30))])
    async with pool.acquire() as conn:
        rest = await store.list_events(
            conn, scope=scope, filters={}, after=cursor, limit=10, **window
        )
    assert [r["origin_record_id"] for r in rest] == ["o:p3", "o:p4", "o:late-early"]


async def test_an_overlapping_transaction_can_be_passed_by_a_cursor(pool):
    """The documented limitation: recorded_at is insert time, not commit time.

    Transaction A inserts first and commits last; B inserts later and commits
    first. A reader that pages past B never sees A on a later page of that
    scan, although A is committed. Only a fresh scan of the window finds it.
    """
    window = {
        "since": dt.datetime(2026, 10, 25, tzinfo=dt.UTC),
        "until": dt.datetime(2026, 10, 27, tzinfo=dt.UTC),
    }
    scope = store.Scope(deployment_ids=("ahtest-dep",), target_environments=("test",))
    slow = await pool.acquire()
    slow_tx = slow.transaction()
    await slow_tx.start()
    try:
        (a,) = await store.write_records(slow, [rec("o:tx-a-inserted-first")])
        assert a.status == "inserted"
        (b,) = await write(pool, [rec("o:tx-b-inserted-second")])  # commits now
        async with pool.acquire() as conn:
            first = await store.list_events(
                conn, scope=scope, filters={}, after=None, limit=50, **window
            )
        assert [r["origin_record_id"] for r in first] == ["o:tx-b-inserted-second"]
        cursor = (first[-1]["recorded_at"], first[-1]["record_id"])
        await slow_tx.commit()
    except BaseException:
        await slow_tx.rollback()
        raise
    finally:
        await pool.release(slow)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT origin_record_id, recorded_at FROM alert_history_events "
            "WHERE source_namespace = $1 ORDER BY recorded_at",
            NS,
        )
        # A's recorded_at is earlier than B's, though A committed later.
        assert [r["origin_record_id"] for r in rows] == [
            "o:tx-a-inserted-first",
            "o:tx-b-inserted-second",
        ]
        after = await store.list_events(
            conn, scope=scope, filters={}, after=cursor, limit=50, **window
        )
        assert after == []  # the continuing scan misses the committed A
        fresh = await store.list_events(
            conn, scope=scope, filters={}, after=None, limit=50, **window
        )
    seen = {r["record_id"]: r["origin_record_id"] for r in [*first, *fresh]}
    # A rescan of the window, deduplicated by record_id, has both.
    assert sorted(seen.values()) == ["o:tx-a-inserted-first", "o:tx-b-inserted-second"]
    assert b.record_id in seen and a.record_id in seen


async def test_scope_isolation(pool):
    await write(pool, [rec("o:mine"), rec("o:theirs", deployment_id="ahtest-other")])
    await write(pool, [rec("o:staging", target_environment="staging")])
    window = {
        "since": dt.datetime(2026, 10, 25, tzinfo=dt.UTC),
        "until": dt.datetime(2026, 10, 27, tzinfo=dt.UTC),
    }
    scope = store.Scope(deployment_ids=("ahtest-dep",), target_environments=("test",))
    async with pool.acquire() as conn:
        rows = await store.list_events(
            conn, scope=scope, filters={}, after=None, limit=50, **window
        )
        admin = await store.list_events(
            conn,
            scope=store.Scope(),
            filters={"source_namespace": NS},
            after=None,
            limit=50,
            **window,
        )
    assert {r["origin_record_id"] for r in rows} == {"o:mine"}
    assert {r["origin_record_id"] for r in admin} == {"o:mine", "o:theirs", "o:staging"}


async def _log_request(pool, request_id, user_id, status=503):
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO api_logs (request_id, model_id, provider, user_id, status_code, latency_ms, "
            "served_endpoint_id, prompt) VALUES ($1, 'model-x', 'prov', $2, $3, 1234, 'model-x:prov-api', "
            "'synthetic prompt text')",
            request_id,
            user_id,
            status,
        )


async def test_request_evidence_is_owner_checked(pool):
    user = "ahtest-user-4"
    digest = fence_account_digest(user, SECRET)
    await _log_request(pool, "ahtest-req-1", user)
    await _log_request(pool, "ahtest-req-2", "ahtest-user-5")
    await write(
        pool,
        [
            rec("o:r1", subjects=[digest], refs=[("ahtest-req-1", digest)]),
            rec("o:r2", subjects=[digest], refs=[("ahtest-req-2", digest)]),
        ],
    )
    scope = store.Scope()
    async with pool.acquire() as conn:
        refs = await store.events_referencing_request(conn, scope=scope, request_id="ahtest-req-1")
        assert refs and refs[0]["owner_digests"] == [digest]
        state, evidence = await store.request_evidence(
            conn, request_id="ahtest-req-1", owner_digests={digest}, fence_secret=SECRET
        )
        assert state == "available"
        assert set(evidence) == {
            "request_id",
            "timestamp",
            "model_id",
            "endpoint_id",
            "status_code",
            "latency_ms",
            "error_class",
        }
        assert evidence["error_class"] == "server_error"
        state, _ = await store.request_evidence(
            conn, request_id="ahtest-req-2", owner_digests={digest}, fence_secret=SECRET
        )
        assert state == "ownership_mismatch"
        state, _ = await store.request_evidence(
            conn, request_id="ahtest-req-missing", owner_digests={digest}, fence_secret=SECRET
        )
        assert state == "not_found"
        out_of_scope = await store.events_referencing_request(
            conn, scope=store.Scope(deployment_ids=("nobody",)), request_id="ahtest-req-1"
        )
        assert out_of_scope == []


async def test_request_evidence_during_and_after_erasure(pool):
    user = "ahtest-user-6"
    digest = fence_account_digest(user, SECRET)
    await _log_request(pool, "ahtest-req-6", user)
    await write(pool, [rec("o:r6", subjects=[digest], refs=[("ahtest-req-6", digest)])])
    eraser = await pool.acquire()
    tx = eraser.transaction()
    await tx.start()
    await establish_erasure_fence(eraser, fence_key=digest)

    async def read():
        async with pool.acquire() as conn:
            return await store.request_evidence(
                conn, request_id="ahtest-req-6", owner_digests={digest}, fence_secret=SECRET
            )

    reader = asyncio.create_task(read())
    await asyncio.sleep(0.2)
    assert not reader.done()  # waits for the erasure, does not read around it
    await eraser.execute("DELETE FROM api_logs WHERE request_id = 'ahtest-req-6'")
    await tx.commit()
    await pool.release(eraser)
    state, evidence = await asyncio.wait_for(reader, 5)
    assert (state, evidence) == ("erased", None)


# -- the writer against an unavailable, then available, database -----------------


def _identity():
    return BackendIdentity(
        source_namespace="ahtest-writer",
        deployment_id="ahtest-dep",
        target_environment="test",
        source_instance_id=f"be_ahtest_{uuid.uuid4().hex[:8]}",
        process_id="ahtest:1",
        started_at=dt.datetime.now(dt.UTC),
    )


async def test_writer_buffers_through_an_outage_and_commits_once(pool):
    db = postgres.target()
    down = {**db.connect_kwargs(), "port": 1}  # nothing listens there
    rt = AlertHistoryRuntime(
        enabled=True, identity=_identity(), db_config=down, fence_secret=SECRET
    )
    for n in range(7):
        rt.capture.on_breach(f"rule_{n}", severity="error", title="t", kind="metric", context={})
    assert await rt.writer.flush_once() is False
    assert len(rt.queue) == 7
    status = rt.local_status()
    assert status["queued_records"] == 7 and status["last_error_code"]

    # The database comes back.
    rt._db_config = db.connect_kwargs()
    rt._pool = None
    real = store.AlertHistoryStore.write_batch
    calls = []

    async def commit_then_lose_response(self, records, heartbeat):
        calls.append(len(records))
        dropped = await real(self, records, heartbeat)
        if len(calls) == 1:
            raise ConnectionResetError("synthetic: COMMIT response lost")
        return dropped

    store.AlertHistoryStore.write_batch = commit_then_lose_response
    try:
        assert await rt.writer.flush_once() is False  # committed; we were not told
        assert await rt.writer.flush_once() is True  # resent: duplicates
    finally:
        store.AlertHistoryStore.write_batch = real
    assert len(rt.queue) == 0
    async with pool.acquire() as conn:
        n = await conn.fetchval(
            "SELECT count(*) FROM alert_history_events WHERE source_namespace = 'ahtest-writer'"
        )
        src = await conn.fetchrow(
            "SELECT * FROM alert_history_sources WHERE source_instance_id = $1",
            rt.identity.source_instance_id,
        )
    assert n == 7
    assert src["last_write_at"] is not None and src["refused_count"] == 0
    await rt.stop(producer_budget_sec=0.1, drain_budget_sec=1.0)
    async with pool.acquire() as conn:
        stopped = await conn.fetchrow(
            "SELECT stopped_at, stop_reason, lost_at_stop FROM alert_history_sources "
            "WHERE source_instance_id = $1",
            rt.identity.source_instance_id,
        )
    assert stopped["stopped_at"] is not None and stopped["lost_at_stop"] == 0


async def test_writer_respects_its_queue_bound_while_down():
    db = postgres.target()
    rt = AlertHistoryRuntime(
        enabled=True,
        identity=_identity(),
        db_config={**db.connect_kwargs(), "port": 1},
        fence_secret=SECRET,
        max_records=5,
    )
    for n in range(12):
        rt.capture.on_breach(f"rule_{n}", severity="error", title="t", kind="metric", context={})
    assert len(rt.queue) == 5 and rt.queue.refused_total == 7
    await rt.writer.flush_once()
    assert len(rt.queue) == 5
    await rt.stop(producer_budget_sec=0.0, drain_budget_sec=0.3)
    assert rt.stopped


async def test_writer_uses_a_writer_queue_separately_from_the_pool(pool):
    q = HistoryQueue()
    s = store.AlertHistoryStore(lambda: pool.acquire())
    w = HistoryWriter(
        q,
        s,
        heartbeat=lambda: {
            "source": "backend",
            "source_namespace": "ahtest-writer2",
            "source_instance_id": "be_x",
            "deployment_id": "ahtest-dep",
            "target_environment": "test",
        },
    )
    q.offer(rec("o:w1", ns="ahtest-writer2"))
    assert await w.flush_once() is True
    assert await count(pool, "source_namespace = 'ahtest-writer2'") == 1
