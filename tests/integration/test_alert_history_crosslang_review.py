"""Real PostgreSQL/API acceptance of bytes produced by the TypeScript monitor.

The JSON fixture is generated from createMonitorHistory + historyWireV1 with
synthetic inputs. Request authentication, ingestion, storage and scoped reads
below are the production code; only the runtime pool/config is test-injected.
"""

import datetime as dt
import hashlib
import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

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
TOKEN = "synthetic-crosslang-writer-" + "a" * 32
READER = "synthetic-crosslang-reader-" + "b" * 32
NAMESPACE = "review-monitor-wire"


async def test_monitor_wire_commit_retry_scope_and_rollback(monkeypatch):
    fixture = Path(__file__).parents[1] / "fixtures" / "alert_history_monitor_wire.json"
    payload = json.loads(fixture.read_text())["payloads"][0]
    # Keep the fixture valid after its generation date; preserve every other
    # producer field and all stable identities across the subsequent retries.
    timestamp = dt.datetime.now(dt.UTC).isoformat()
    for event in payload["events"]:
        for field in ("occurred_at", "bucket_start", "bucket_end"):
            if field in event:
                event[field] = timestamp
    for field in ("observed_at", "started_at", "pending_reported_at", "sync_watermark"):
        if payload["source_status"].get(field) is not None:
            payload["source_status"][field] = timestamp
    pool = await postgres.create_pool(min_size=1, max_size=4)
    identity = BackendIdentity(
        source_namespace="review-backend",
        deployment_id="review-deployment",
        target_environment="test",
        source_instance_id="review-backend-instance",
        process_id="review-process",
        started_at=dt.datetime.now(dt.UTC),
    )
    runtime = AlertHistoryRuntime(
        enabled=True,
        identity=identity,
        db_config=None,
        fence_secret=postgres.FENCE_SECRET,
        pool=pool,
    )
    config = {
        auth.WRITE_SETTING: json.dumps(
            [
                {
                    "id": "review-writer",
                    "sha256": hashlib.sha256(TOKEN.encode()).hexdigest(),
                    "expires_at": "2099-01-01T00:00:00Z",
                    "source": "status_monitor",
                    "source_namespace": NAMESPACE,
                    "deployment_id": "review-deployment",
                    "target_environments": ["test"],
                }
            ]
        ),
        auth.READ_SETTING: json.dumps(
            [
                {
                    "id": "review-reader",
                    "sha256": hashlib.sha256(READER.encode()).hexdigest(),
                    "expires_at": "2099-01-01T00:00:00Z",
                    "deployment_ids": ["review-deployment"],
                    "target_environments": ["test"],
                    "sources": ["status_monitor"],
                }
            ]
        ),
    }
    monkeypatch.setattr(app_config, "config_value", lambda key: config.get(key))
    monkeypatch.setattr(app_config, "seconds_since_read", lambda: 0.0)
    for module in (auth, ingest, reads):
        monkeypatch.setattr(module, "get_runtime", lambda: runtime)
    app = FastAPI()
    app.include_router(ingest.router)
    app.include_router(reads.router)
    app.dependency_overrides[get_operational_store] = lambda: SimpleNamespace()
    writer_headers = {"Authorization": "Bearer " + TOKEN}
    reader_headers = {"Authorization": "Bearer " + READER}

    async def cleanup():
        async with pool.acquire() as conn:
            await storage.ensure_schema(conn)
            await conn.execute(
                "DELETE FROM alert_history_events WHERE source_namespace = $1", NAMESPACE
            )
            await conn.execute(
                "DELETE FROM alert_history_sources WHERE source_namespace = $1", NAMESPACE
            )

    await cleanup()
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            first = await client.post(
                "/internal/alert-history/events", json=payload, headers=writer_headers
            )
            assert first.status_code == 200, first.text
            assert first.json()["committed"] is True
            assert {r["status"] for r in first.json()["results"]} == {"inserted"}
            repeated = await client.post(
                "/internal/alert-history/events", json=payload, headers=writer_headers
            )
            assert repeated.status_code == 200, repeated.text
            assert {r["status"] for r in repeated.json()["results"]} == {"duplicate"}
            assert [r["record_id"] for r in first.json()["results"]] == [
                r["record_id"] for r in repeated.json()["results"]
            ]

            events = await client.get("/admin/alert-history/events", headers=reader_headers)
            assert events.status_code == 200, events.text
            rows = [r for r in events.json()["events"] if r["source_namespace"] == NAMESPACE]
            assert len(rows) == 6
            assert {r["kind"] for r in rows} == {
                "condition_triggered",
                "condition_ended",
                "delivery_attempt",
                "delivery_result",
                "suppression_summary",
                "notification_decision",
            }
            assert next(r for r in rows if r["kind"] == "delivery_result")["outcome"] == "accepted"
            assert next(r for r in rows if r["kind"] == "suppression_summary")["count"] == 4
            assert {r["deployment_id"] for r in rows} == {"review-deployment"}
            assert (
                await client.get("/admin/alert-history/events", headers=writer_headers)
            ).status_code == 403
            assert (
                await client.post(
                    "/internal/alert-history/events", json=payload, headers=reader_headers
                )
            ).status_code == 403

            denied = deepcopy(payload)
            denied["events"][0]["target_environment"] = "outside-reader-scope"
            assert (
                await client.post(
                    "/internal/alert-history/events", json=denied, headers=writer_headers
                )
            ).status_code == 403
            spoofed = deepcopy(payload)
            spoofed["events"][0]["source"] = "backend"
            assert (
                await client.post(
                    "/internal/alert-history/events", json=spoofed, headers=writer_headers
                )
            ).status_code == 422

            changed = deepcopy(payload)
            changed["events"][0]["facts"]["count"] = 999
            conflict = await client.post(
                "/internal/alert-history/events", json=changed, headers=writer_headers
            )
            assert conflict.status_code == 200, conflict.text
            assert conflict.json()["results"][0]["status"] == "conflict"
            async with pool.acquire() as conn:
                count = await conn.fetchval(
                    "SELECT count(*) FROM alert_history_events WHERE source_namespace = $1",
                    NAMESPACE,
                )
                assert count == 7  # six original facts + one isolated conflict

            uncommitted = deepcopy(payload)
            for event in uncommitted["events"]:
                event["origin_record_id"] += ":rollback"
            with monkeypatch.context() as scoped:
                scoped.setattr(
                    storage,
                    "upsert_source",
                    AsyncMock(side_effect=RuntimeError("synthetic rollback")),
                )
                failed = await client.post(
                    "/internal/alert-history/events", json=uncommitted, headers=writer_headers
                )
            assert failed.status_code == 503
            async with pool.acquire() as conn:
                assert (
                    await conn.fetchval(
                        "SELECT count(*) FROM alert_history_events WHERE source_namespace = $1",
                        NAMESPACE,
                    )
                    == 7
                )
    finally:
        await cleanup()
        await pool.close()
