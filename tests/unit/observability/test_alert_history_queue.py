"""Bounded buffer, writer retry and shutdown, without a database."""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import time

import pytest

from serving.observability.alert_history import hooks, runtime as runtime_mod
from serving.observability.alert_history.capture import BackendCapture, BackendIdentity
from serving.observability.alert_history.queue import HistoryQueue, HistoryWriter
from serving.observability.alert_history.records import HistoryRecord, finalize


def _record(n: int, *, pad: int = 0) -> HistoryRecord:
    return finalize(
        HistoryRecord(
            source="backend",
            source_namespace="ns",
            origin_record_id=f"inst:{n}",
            kind="condition_triggered",
            occurred_at=dt.datetime(2026, 10, 26, tzinfo=dt.UTC),
            deployment_id="dep",
            target_environment="test",
            facts={"pad": "x" * pad} if pad else {},
        )
    )


class FakeStore:
    """Commits by identity like the real store: duplicates are not new rows."""

    def __init__(self) -> None:
        self.rows: dict[str, str] = {}
        self.fail_next = 0
        self.lose_response_next = 0
        self.batches = 0
        self.heartbeats = 0

    async def write_batch(self, records, heartbeat):
        self.batches += 1
        if self.fail_next:
            self.fail_next -= 1
            raise ConnectionError("synthetic database outage")
        dropped = 0
        for record in records:
            existing = self.rows.get(record.origin_record_id)
            if existing is not None and existing != record.payload_digest:
                raise AssertionError("conflict in a retry")
            if record.facts.get("bad"):
                dropped += 1
                continue
            self.rows[record.origin_record_id] = record.payload_digest
        if self.lose_response_next:
            self.lose_response_next -= 1
            raise TimeoutError("synthetic lost COMMIT response")
        return dropped

    async def heartbeat(self, heartbeat):
        self.heartbeats += 1


def test_bounds_count_in_flight_records():
    queue = HistoryQueue(max_records=3, max_bytes=10_000)
    for n in range(3):
        assert queue.offer(_record(n))
    batch = queue.peek_batch()
    assert len(batch) == 3 and queue.in_flight == 3
    # The batch being written still occupies the buffer.
    assert not queue.offer(_record(9))
    assert queue.refused_total == 1
    queue.settle(batch)
    assert len(queue) == 0 and queue.offer(_record(10))


def test_byte_bound_refuses_without_dropping_queued():
    small = _record(1)
    queue = HistoryQueue(max_records=100, max_bytes=small.size * 2 + 10)
    assert queue.offer(small)
    assert not queue.offer(_record(2, pad=small.size * 3))
    assert len(queue) == 1
    counts, first, last = queue.take_refusals()
    assert counts == {"buffer_full": 1} and first and last


async def test_writer_retries_with_backoff_and_does_not_duplicate():
    store = FakeStore()
    queue = HistoryQueue()
    writer = HistoryWriter(queue, store, heartbeat=dict)
    for n in range(5):
        queue.offer(_record(n))
    store.fail_next = 2
    assert await writer.flush_once() is False
    assert writer._backoff == 1.0
    assert await writer.flush_once() is False
    assert writer._backoff == 2.0
    assert len(queue) == 5  # nothing left the buffer
    assert await writer.flush_once() is True
    assert len(queue) == 0 and len(store.rows) == 5
    assert writer._backoff == 0.0


async def test_lost_commit_response_is_resent_and_deduplicated():
    store = FakeStore()
    queue = HistoryQueue()
    writer = HistoryWriter(queue, store, heartbeat=dict)
    for n in range(3):
        queue.offer(_record(n))
    store.lose_response_next = 1
    assert await writer.flush_once() is False  # committed, but we cannot know
    assert len(queue) == 3
    assert await writer.flush_once() is True
    assert len(store.rows) == 3 and store.batches == 2
    assert queue.committed_total == 3


async def test_bad_record_does_not_block_the_batch():
    store = FakeStore()
    queue = HistoryQueue()
    writer = HistoryWriter(queue, store, heartbeat=dict)
    bad = finalize(
        HistoryRecord(
            source="backend",
            source_namespace="ns",
            origin_record_id="inst:bad",
            kind="condition_triggered",
            occurred_at=dt.datetime(2026, 10, 26, tzinfo=dt.UTC),
            deployment_id="dep",
            target_environment="test",
            facts={"bad": True},
        )
    )
    queue.offer(_record(1))
    queue.offer(bad)
    queue.offer(_record(2))
    assert await writer.flush_once() is True
    assert set(store.rows) == {"inst:1", "inst:2"}
    assert queue.dropped_total == 1 and len(queue) == 0


async def test_refusals_become_a_gap_record_when_space_returns():
    store = FakeStore()
    queue = HistoryQueue(max_records=2)
    capture = BackendCapture(
        BackendIdentity("ns", "dep", "test", "gapinst", "p:1", dt.datetime.now(dt.UTC)),
        queue.offer,
        fence_secret=None,
    )
    writer = HistoryWriter(queue, store, heartbeat=dict, gap_record=capture.gap_record)
    queue.offer(_record(1))
    queue.offer(_record(2))
    for n in range(3, 7):
        assert not queue.offer(_record(n))
    assert await writer.flush_once() is True  # frees space
    assert await writer.flush_once() is True  # writes the gap report
    gap = [r for r in store.rows if r.startswith("gapinst:")]
    assert len(gap) == 1 and queue.pending_refusals() == 0
    assert queue.refused_total == 4


async def test_gap_record_carries_counts():
    capture = BackendCapture(
        BackendIdentity("ns", "dep", "test", "inst", "p:1", dt.datetime.now(dt.UTC)),
        lambda r: True,
        fence_secret=None,
    )
    import collections

    record = capture.gap_record(collections.Counter(buffer_full=4, after_shutdown=1), 10.0, 20.0)
    assert record.kind == "history_gap" and record.count == 5
    assert record.error_code == "multiple"
    assert record.facts["lost_buffer_full"] == 4


async def test_stop_drains_within_budget_and_reports_what_is_left():
    store = FakeStore()
    queue = HistoryQueue()
    writer = HistoryWriter(queue, store, heartbeat=dict, flush_interval_sec=0.01)
    writer.start()
    for n in range(120):
        queue.offer(_record(n))
    left = await writer.stop(drain_budget_sec=2.0)
    assert left == 0 and len(store.rows) == 120

    stuck = FakeStore()
    stuck.fail_next = 10**6
    queue = HistoryQueue()
    writer = HistoryWriter(queue, stuck, heartbeat=dict)
    queue.offer(_record(1))
    started = time.monotonic()
    left = await writer.stop(drain_budget_sec=0.3)
    assert left == 1
    assert time.monotonic() - started < 0.8


async def test_offers_after_close_are_counted_not_queued():
    queue = HistoryQueue()
    queue.close()
    assert not queue.offer(_record(1))
    counts, _, _ = queue.take_refusals()
    assert counts == {"after_shutdown": 1}


# -- runtime shutdown ordering ----------------------------------------------------


async def test_shutdown_waits_for_detached_producer_tasks(monkeypatch):
    from routing import endpoint_health

    store = FakeStore()
    rt = runtime_mod.AlertHistoryRuntime(
        enabled=True,
        identity=BackendIdentity("ns", "dep", "test", "inst", "p:1", dt.datetime.now(dt.UTC)),
        db_config=None,
        fence_secret=None,
        pool=object(),
    )
    rt.writer._storage = store
    rt.start()
    assert hooks.active() is rt.capture

    async def producer():
        await asyncio.sleep(0.2)
        rt.capture.on_breach("late_rule", severity="error", title="t", kind="metric", context={})

    task = asyncio.get_running_loop().create_task(producer())
    endpoint_health._ALERT_TASKS.add(task)
    task.add_done_callback(endpoint_health._ALERT_TASKS.discard)

    async def no_close(budget):
        return None

    monkeypatch.setattr(rt, "_close_pool", no_close)

    async def heartbeat_ok(self, snapshot):
        return None

    monkeypatch.setattr(
        "serving.storage.alert_history_store.AlertHistoryStore.heartbeat", heartbeat_ok
    )
    await rt.stop(producer_budget_sec=2.0, drain_budget_sec=2.0)
    assert task.done()
    # The fact the producer recorded after shutdown began was still written.
    assert any(True for _ in store.rows)
    assert rt.queue.refused_total == 0
    assert hooks.active() is None


async def test_shutdown_producer_wait_is_bounded():
    from routing import endpoint_health

    async def forever():
        await asyncio.Event().wait()

    task = asyncio.get_running_loop().create_task(forever())
    endpoint_health._ALERT_TASKS.add(task)
    try:
        started = time.monotonic()
        settled = await runtime_mod.wait_for_producers(0.2)
        assert settled is False
        assert time.monotonic() - started < 0.6
    finally:
        task.cancel()
        endpoint_health._ALERT_TASKS.discard(task)


async def test_shutdown_waits_for_a_send_in_the_alert_path(monkeypatch):
    from serving.observability import alerts

    gate = asyncio.Event()

    @alerts._tracked
    async def sending():
        await gate.wait()

    task = asyncio.get_running_loop().create_task(sending())
    await asyncio.sleep(0)
    assert task in alerts.active_alert_tasks()
    waiter = asyncio.get_running_loop().create_task(runtime_mod.wait_for_producers(2.0))
    await asyncio.sleep(0.05)
    assert not waiter.done()
    gate.set()
    assert await waiter is True


def test_rules_hash_is_stable_and_stamped():
    rt = runtime_mod.AlertHistoryRuntime(
        enabled=True,
        identity=BackendIdentity("ns", "dep", "test", "inst", "p:1", dt.datetime.now(dt.UTC)),
        db_config=None,
        fence_secret=None,
        pool=object(),
    )
    rt.set_rules({"rules": {"fivexx_rate": {"threshold_pct": 2.0}}})
    expected = hashlib.sha256(b'{"rules":{"fivexx_rate":{"threshold_pct":2.0}}}').hexdigest()
    assert rt.rules_hash == f"sha256:{expected}"
    assert rt.capture.rule_version == expected[:16]
    assert rt.heartbeat()["rules_snapshot_hash"] == rt.rules_hash


@pytest.fixture(autouse=True)
def _no_capture_leak():
    yield
    hooks.install(None)


def test_heartbeat_pending_fields_describe_the_same_records():
    rt = runtime_mod.AlertHistoryRuntime(
        enabled=True,
        identity=BackendIdentity("ns", "dep", "test", "inst", "p:1", dt.datetime.now(dt.UTC)),
        db_config=None,
        fence_secret=None,
        pool=object(),
    )
    records = [
        finalize(
            HistoryRecord(
                source="backend",
                source_namespace="ns",
                origin_record_id=f"inst:{n}",
                kind="condition_triggered",
                occurred_at=dt.datetime(2026, 10, 26, 0, n, tzinfo=dt.UTC),
                deployment_id="dep",
                target_environment="test",
            )
        )
        for n in range(5)
    ]
    for record in records:
        rt.queue.offer(record)
    rt.queue.peek_batch(max_records=3)  # three in flight with this heartbeat
    beat = rt.heartbeat()
    assert beat["pending_count"] == 2
    assert beat["pending_bytes"] == records[3].size + records[4].size
    assert beat["oldest_pending_at"] == records[3].occurred_at
    rt.queue.release_in_flight()
    beat = rt.heartbeat()
    assert beat["pending_count"] == 5
    assert beat["pending_bytes"] == sum(r.size for r in records)
    assert beat["oldest_pending_at"] == records[0].occurred_at
    rt.queue.settle(rt.queue.peek_batch())
    beat = rt.heartbeat()
    assert (beat["pending_count"], beat["pending_bytes"], beat["oldest_pending_at"]) == (0, 0, None)
