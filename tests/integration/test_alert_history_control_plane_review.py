"""Independent review of real control-plane/monitor wire output through PostgreSQL.

The fixture comes from real producer, state-machine, executor, recorder and
exporter code with synthetic Slack/D1/store dependencies. Its provenance and
case descriptions are stored alongside the requests. Only timestamps move.
"""

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
FIXTURE = Path(__file__).parents[1] / "fixtures" / "alert_history_control_plane_wire.json"
TOKENS = {
    "alert_control_plane": "synthetic-cp-review-writer-" + "c" * 32,
    "status_monitor": "synthetic-monitor-review-writer-" + "d" * 32,
}
READER = "synthetic-cp-review-reader-" + "e" * 32
NAMESPACES = {source: "review-cp-wire-" + source for source in TOKENS}


async def test_control_plane_real_wire_roundtrip_preserves_each_fact(monkeypatch, tmp_path):
    fixture = json.loads(FIXTURE.read_text())
    groups = {
        "alert_control_plane": fixture["requests"],
        "status_monitor": fixture["status_monitor_counterpart_requests"],
    }
    timestamp = dt.datetime.now(dt.UTC).isoformat()
    for requests in groups.values():
        for body in requests:
            for item in [*body["events"], body.get("source_status") or {}]:
                for field in (
                    "occurred_at",
                    "bucket_start",
                    "bucket_end",
                    "observed_at",
                    "started_at",
                    "pending_reported_at",
                    "stopped_at",
                    "sync_watermark",
                ):
                    if item.get(field) is not None:
                        item[field] = timestamp
    pool = await postgres.create_pool(min_size=1, max_size=4)
    runtime = AlertHistoryRuntime(
        enabled=True,
        db_config=None,
        fence_secret=postgres.FENCE_SECRET,
        pool=pool,
        identity=BackendIdentity(
            source_namespace="review-cp-backend",
            deployment_id="review-cp-test",
            target_environment="test",
            source_instance_id="review-cp-process",
            process_id="review:1",
            started_at=dt.datetime.now(dt.UTC),
        ),
    )
    config = {
        auth.WRITE_SETTING: json.dumps(
            [
                {
                    "id": source,
                    "sha256": hashlib.sha256(token.encode()).hexdigest(),
                    "expires_at": "2099-01-01T00:00:00Z",
                    "source": source,
                    "source_namespace": NAMESPACES[source],
                    "deployment_id": "review-cp-test",
                    "target_environments": ["production"],
                    "stale_after_sec": 1800,
                }
                for source, token in TOKENS.items()
            ]
        ),
        auth.READ_SETTING: json.dumps(
            [
                {
                    "id": "cp-reader",
                    "sha256": hashlib.sha256(READER.encode()).hexdigest(),
                    "expires_at": "2099-01-01T00:00:00Z",
                    "deployment_ids": ["review-cp-test"],
                    "target_environments": ["production"],
                    "sources": list(TOKENS),
                }
            ]
        ),
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

    async def cleanup():
        async with pool.acquire() as conn:
            await storage.ensure_schema(conn)
            for table in ("alert_history_events", "alert_history_sources"):
                await conn.execute(
                    f"DELETE FROM {table} WHERE source_namespace=ANY($1::text[])",
                    list(NAMESPACES.values()),
                )

    await cleanup()
    receipts = []
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            expected = {}
            for source, requests in groups.items():
                headers = {"Authorization": "Bearer " + TOKENS[source]}
                for body in requests:
                    response = await client.post(
                        "/internal/alert-history/events", json=body, headers=headers
                    )
                    assert response.status_code == 200, response.text
                    receipt = response.json()
                    assert receipt["committed"] is True
                    assert receipt["source_status"] == "recorded"
                    assert len(receipt["results"]) == len(body["events"])
                    assert {item["status"] for item in receipt["results"]} <= {
                        "inserted",
                        "duplicate",
                    }
                    receipts.append({"request": body, "receipt": receipt})
                    for event in body["events"]:
                        expected[source, event["origin_record_id"]] = event
                for body in requests:
                    response = await client.post(
                        "/internal/alert-history/events", json=body, headers=headers
                    )
                    assert response.status_code == 200
                    assert {item["status"] for item in response.json()["results"]} <= {"duplicate"}
            response = await client.get(
                "/admin/alert-history/events",
                params={"limit": 200},
                headers={"Authorization": "Bearer " + READER},
            )
            assert response.status_code == 200, response.text
            actual = {
                (row["source"], row["origin_record_id"]): row for row in response.json()["events"]
            }
            assert actual.keys() == expected.keys()
            for key, sent in expected.items():
                saved = actual[key]
                assert saved["facts"] == sent.get("facts", {}), (key, saved["limitations"])
                assert saved["association_incomplete"] is (sent.get("source_instance_id") is None)
                for field in (
                    "kind",
                    "source_incident_id",
                    "source_event_id",
                    "generation",
                    "action_id",
                    "attempt_id",
                    "outcome",
                    "end_basis",
                    "model_id",
                ):
                    assert saved.get(field) == sent.get(field), (key, field)
            cp = [row for (source, _), row in actual.items() if source == "alert_control_plane"]
            assert any(
                row["outcome"] == "accepted" and row["facts"].get("commit") == "unknown"
                for row in cp
            )
            assert any(row["facts"].get("check") == "reconciliation" for row in cp)
            assert any(row["kind"] == "resolution_retry_terminated" for row in cp)
            assert any(row["facts"].get("model_id_omitted") == "too_long" for row in cp)
            assert any(
                "Example-Org/Synthetic-Model-7B" in row["facts"].get("monitor_incident_key", "")
                for row in actual.values()
            )
            status = await client.get(
                "/admin/alert-history/status", headers={"Authorization": "Bearer " + READER}
            )
            assert status.status_code == 200
            assert len(status.json()["sources"]) == 2
            assert all(row["connection"] == "reporting" for row in status.json()["sources"])
            for target in status.json()["sources"]:
                assert all(instance["state"] == "stopped" for instance in target["instances"])
        (tmp_path / "control-plane-real-receipts.json").write_text(json.dumps(receipts))
    finally:
        await cleanup()
        await pool.close()
