"""An engine its proxy stopped on purpose: told apart from one that is down, from ingest to the live view."""

from __future__ import annotations

import pytest

from serving.openinfra import store
from serving.openinfra.store import SnapshotWrite
from serving.openinfra.wire import IngestError, parse_snapshot

NOW = 1_800_000_000_000


def _write(engine: dict) -> SnapshotWrite:
    body = {"node": {"id": "node-a"}, "ts": NOW, "sglang": [engine]}
    return SnapshotWrite(parse_snapshot(body), NOW)


def _row(write: SnapshotWrite) -> dict:
    (row,) = write.engines
    return dict(zip(store._ENGINE_COLUMNS, row, strict=False))


@pytest.mark.parametrize(
    "engine",
    [
        {"server": {"id": "node-a-embed", "name": "bge-m3", "state": "idle"}, "metrics": {}},
        # No metrics at all: an idle engine needs none.
        {"server": {"id": "node-a-embed", "name": "bge-m3", "state": "idle"}},
        # Figures sent anyway are not kept: an idle engine has none.
        {
            "server": {"id": "node-a-embed", "name": "bge-m3", "state": "idle"},
            "metrics": {"num_running_reqs": 3, "gen_throughput": 120},
        },
    ],
)
def test_an_idle_engine_is_stored_idle_with_no_figures_and_no_sample(engine):
    write = _write(engine)
    row = _row(write)
    assert (row["id"], row["name"], row["status"]) == ("node-a-embed", "bge-m3", "idle")
    assert all(row[name] is None for name in store.ENGINE_METRICS)
    assert write.engine_samples == []


def test_an_idle_engine_without_a_model_keeps_the_stored_one():
    """Its model name is left out, so the upsert's COALESCE keeps what the engine reported."""
    row = _row(_write({"server": {"id": "node-a-embed", "state": "idle"}}))
    assert row["model_name"] is None


def test_any_other_state_is_ignored():
    write = _write(
        {"server": {"id": "node-a-chat", "state": "sleeping"}, "metrics": {"num_running_reqs": 2}}
    )
    assert _row(write)["status"] == "online"
    assert len(write.engine_samples) == 1
    with pytest.raises(IngestError, match="requires metrics"):
        _write({"server": {"id": "node-a-chat", "state": "sleeping"}})


def test_a_silent_idle_engine_reads_as_offline():
    """A reporter that stops posting leaves its idle engine offline, as any other row."""
    rows = [
        {"id": "quiet", "status": "idle", "last_seen_at": NOW - store.STALE_MS - 1},
        {"id": "fresh", "status": "idle", "last_seen_at": NOW - 1_000},
    ]
    assert [row["status"] for row in store._live(rows, NOW)] == ["offline", "idle"]
