"""The status monitor's round-3 wire output, ingested into a real PostgreSQL.

``tests/fixtures/alert_history_monitor_wire_round3.json`` was produced by the
monitor's own pipeline (runAlerts/runCycleAlert + createMonitorHistory +
historyWireV1) from synthetic inputs: one request per invocation, each with
its final source status. Timestamps are moved to now; nothing else changes.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from serving.config import app_config
from serving.observability.alert_history.capture import BackendIdentity
from serving.observability.alert_history.runtime import AlertHistoryRuntime
from serving.servers.deps import get_operational_store
from serving.servers.routers import alert_history_auth as auth, alert_history_ingest as ingest
from serving.servers.routers.admin import alert_history as reads
from serving.storage import alert_history_store as storage
from tests.fixtures import postgres

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

WRITER = "synthetic-round3-writer-" + "c" * 32
NAMESPACE = "ahtest-round3-monitor"
FIXTURE = Path(__file__).parents[1] / "fixtures" / "alert_history_monitor_wire_round3.json"
TIME_FIELDS = (
    "occurred_at",
    "bucket_start",
    "bucket_end",
    "observed_at",
    "started_at",
    "pending_reported_at",
    "stopped_at",
    "sync_watermark",
)


def _requests() -> list[dict]:
    now = dt.datetime.now(dt.UTC).isoformat()
    requests = json.loads(FIXTURE.read_text())["requests"]
    for body in requests:
        for item in [*body.get("events", []), body.get("source_status") or {}]:
            for field in TIME_FIELDS:
                if item.get(field) is not None:
                    item[field] = now
    return requests


async def test_round3_requests_commit_and_keep_their_meaning(monkeypatch):
    pool = await postgres.create_pool(min_size=1, max_size=4)
    runtime = AlertHistoryRuntime(
        enabled=True,
        identity=BackendIdentity(
            source_namespace="ahtest-round3-backend",
            deployment_id="ahtest-round3",
            target_environment="test",
            source_instance_id="be_round3",
            process_id="round3:1",
            started_at=dt.datetime.now(dt.UTC),
        ),
        db_config=None,
        fence_secret=postgres.FENCE_SECRET,
        pool=pool,
    )
    config = {
        auth.WRITE_SETTING: json.dumps(
            [
                {
                    "id": "round3-writer",
                    "sha256": hashlib.sha256(WRITER.encode()).hexdigest(),
                    "expires_at": "2099-01-01T00:00:00Z",
                    "source": "status_monitor",
                    "source_namespace": NAMESPACE,
                    "deployment_id": "ahtest-round3",
                    "target_environments": ["test"],
                    "stale_after_sec": 1800,
                }
            ]
        ),
        auth.READ_SETTING: "",
    }
    monkeypatch.setattr(app_config, "config_value", lambda key, default=None: config.get(key))
    monkeypatch.setattr(app_config, "seconds_since_read", lambda: 0.0)
    auth._CACHE.clear()
    for module in (auth, ingest, reads):
        monkeypatch.setattr(module, "get_runtime", lambda: runtime)
    app = FastAPI()
    app.include_router(ingest.router)
    app.include_router(reads.router)
    app.dependency_overrides[get_operational_store] = lambda: SimpleNamespace()
    headers = {"Authorization": "Bearer " + WRITER}

    async def cleanup():
        async with pool.acquire() as conn:
            await storage.ensure_schema(conn)
            for table in ("alert_history_events", "alert_history_sources"):
                await conn.execute(f"DELETE FROM {table} WHERE source_namespace = $1", NAMESPACE)

    await cleanup()
    requests = _requests()
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            sent = 0
            unique = {e["origin_record_id"] for b in requests for e in b["events"]}
            for body in requests:
                response = await client.post(
                    "/internal/alert-history/events", json=body, headers=headers
                )
                assert response.status_code == 200, response.text
                receipt = response.json()
                # The receipt shape the monitor requires (Q5).
                assert receipt["committed"] is True and receipt["recorded_at"]
                assert receipt["source_status"] == (
                    "recorded" if body.get("source_status") else "absent"
                )
                assert [r["index"] for r in receipt["results"]] == list(range(len(body["events"])))
                # Later invocations re-send persisted facts: duplicates, never conflicts.
                assert {r["status"] for r in receipt["results"]} <= {"inserted", "duplicate"}
                sent += len(body["events"])
            for body in requests:  # a full replay: every fact is a duplicate
                again = await client.post(
                    "/internal/alert-history/events", json=body, headers=headers
                )
                assert {r["status"] for r in again.json()["results"]} <= {"duplicate"}

        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT kind, facts, limitations, association_incomplete, source_instance_id "
                "FROM alert_history_events WHERE source_namespace = $1",
                NAMESPACE,
            )
            sources = await conn.fetch(
                "SELECT stop_reason, lost_at_stop, pending_count, stopped_at "
                "FROM alert_history_sources WHERE source_namespace = $1",
                NAMESPACE,
            )
        assert len(rows) == len(unique) and sent >= len(unique)
        sent_facts = {}
        for body in requests:
            for event in body["events"]:
                for key, value in (event.get("facts") or {}).items():
                    sent_facts.setdefault(key, set()).add(json.dumps(value))
        stored_facts = {}
        for row in rows:
            for key, value in json.loads(row["facts"]).items():
                stored_facts.setdefault(key, set()).add(json.dumps(value))
            omitted = [n for n in row["limitations"] if n.startswith("omitted:")]
            assert omitted == [], omitted  # the monitor's typed facts all survive
            assert row["association_incomplete"] is (row["source_instance_id"] is None)
        assert stored_facts == sent_facts
        assert {"handoff_outcome", "control_plane_action", "lifecycle_state"} <= set(stored_facts)
        assert len(sources) == len([b for b in requests if b.get("source_status")])
        for source in sources:
            assert source["stop_reason"] == "invocation_complete"
            assert source["pending_count"] == 0 and source["lost_at_stop"] == 0
            assert source["stopped_at"] is not None
    finally:
        await cleanup()
        await pool.close()
