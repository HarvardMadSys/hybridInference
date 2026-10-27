"""The alert-history HTTP API end to end on a real PostgreSQL.

Synthetic tokens and identities only; the webhook sender is replaced, so no
notification leaves the process.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import os
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from serving.config import app_config
from serving.config.app_config_registry import static_entries
from serving.config.settings import get_settings
from serving.observability import alerts
from serving.observability.alert_history import hooks, runtime as runtime_mod
from serving.observability.alert_history.capture import BackendIdentity
from serving.servers.deps import AppServices
from serving.servers.middleware.error import install_error_handlers
from serving.servers.routers import (
    admin as admin_router,
    alert_history_auth as auth,
    alert_history_ingest,
)
from serving.servers.routers.admin import alerts as admin_alerts
from serving.storage import alert_history_store as store
from serving.storage.log_schema import fence_account_digest
from tests.fixtures import postgres

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

ADMIN = {"Authorization": "Bearer synthetic-admin-token-for-history-tests"}
READER = "ahr_synthetic_history_reader_0123456789abcdef"
OTHER_READER = "ahr_synthetic_history_other_0123456789abcdef"
WRITER = "ahw_synthetic_history_writer_0123456789abcdef"
EXPIRED_WRITER = "ahw_synthetic_history_expired_0123456789abcd"
DEP = "ahtest-api-dep"
NS = "ahtest-api-monitor"
LATER = "2099-01-01T00:00:00Z"
SLACK = "https://hooks.slack.com/services/TSYN/BSYN/synthetic-webhook-secret"


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


def sha(token):
    return hashlib.sha256(token.encode()).hexdigest()


def credentials(monkeypatch):
    monkeypatch.setenv(
        auth.READ_SETTING,
        json.dumps(
            [
                {
                    "id": "reader",
                    "sha256": sha(READER),
                    "deployment_ids": [DEP],
                    "target_environments": ["production"],
                    "expires_at": LATER,
                },
                {
                    "id": "other",
                    "sha256": sha(OTHER_READER),
                    "deployment_ids": ["ahtest-elsewhere"],
                    "target_environments": ["production"],
                    "expires_at": LATER,
                },
            ]
        ),
    )
    monkeypatch.setenv(
        auth.WRITE_SETTING,
        json.dumps(
            [
                {
                    "id": "monitor",
                    "sha256": sha(WRITER),
                    "source": "status_monitor",
                    "source_namespace": NS,
                    "deployment_id": DEP,
                    "target_environments": ["production", "staging"],
                    "expires_at": LATER,
                },
                {
                    "id": "expired",
                    "sha256": sha(EXPIRED_WRITER),
                    "source": "status_monitor",
                    "source_namespace": NS,
                    "deployment_id": DEP,
                    "target_environments": ["production"],
                    "expires_at": "2001-01-01T00:00:00Z",
                },
            ]
        ),
    )


@pytest_asyncio.fixture
async def api(monkeypatch, tmp_path):
    for entry in static_entries():
        monkeypatch.delenv(entry.key, raising=False)
    monkeypatch.delenv("DISTRIBUTION_CONFIG_PATH", raising=False)
    (tmp_path / "models.yaml").write_text("models: []\n")
    monkeypatch.setenv("MODELS_CONFIG_PATH", str(tmp_path / "models.yaml"))
    monkeypatch.setenv("ROUTING_CONFIG_PATH", str(tmp_path / "routing.yaml"))
    monkeypatch.setenv("ALERTS_CONFIG_PATH", str(tmp_path / "alerts.yaml"))
    monkeypatch.setenv("ADMIN_TOKEN", ADMIN["Authorization"][7:])
    monkeypatch.setenv("API_KEY_SECRET", "unit-test-secret")
    monkeypatch.setenv("JWT_SECRET_KEY", "unit-test-jwt-secret-at-least-32-characters")
    monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", SLACK)
    get_settings.cache_clear()
    app_config.reset_state()
    auth._CACHE.clear()
    credentials(monkeypatch)
    pool = await postgres.create_pool(min_size=1, max_size=6)
    async with pool.acquire() as conn:
        await store.ensure_schema(conn)

    async def wipe():
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM alert_history_events WHERE deployment_id LIKE 'ahtest-%'"
            )
            await conn.execute(
                "DELETE FROM alert_history_sources WHERE deployment_id LIKE 'ahtest-%'"
            )
            await conn.execute("DELETE FROM api_logs WHERE request_id LIKE 'ahtest-%'")

    await wipe()
    identity = BackendIdentity(
        source_namespace=DEP,
        deployment_id=DEP,
        target_environment="production",
        source_instance_id=f"be_ahtest_{uuid.uuid4().hex[:8]}",
        process_id="ahtest:1",
        started_at=dt.datetime.now(dt.UTC),
    )
    rt = runtime_mod.AlertHistoryRuntime(
        enabled=True,
        identity=identity,
        db_config=None,
        pool=pool,
        fence_secret=postgres.FENCE_SECRET,
    )
    runtime_mod.set_runtime(rt)
    hooks.install(rt.capture)  # capture on, writer driven by the tests
    monkeypatch.setattr(admin_alerts, "_test_send_not_before", {})
    monkeypatch.setattr(admin_alerts, "log_admin_action", AsyncMock())
    monkeypatch.setattr(alerts, "_post_to_slack", AsyncMock(return_value=True))
    alerts.reset_dedupe_state()

    app = FastAPI()
    app.state.services = AppServices(
        router=MagicMock(), operational_store=None, db_logger=None, log_store=None
    )
    install_error_handlers(app)
    app.include_router(admin_router.router)
    app.include_router(alert_history_ingest.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, rt, pool
    hooks.install(None)
    runtime_mod.set_runtime(None)
    alerts.reset_dedupe_state()
    await wipe()
    await pool.close()
    auth._CACHE.clear()
    get_settings.cache_clear()


def now_iso(minutes=0):
    return (
        (dt.datetime.now(dt.UTC) + dt.timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")
    )


def trigger(origin="inc:i1:g1:triggered", **over):
    return {
        "origin_record_id": origin,
        "kind": "condition_triggered",
        "occurred_at": now_iso(-5),
        "target_environment": "production",
        "source_incident_id": "i1",
        "generation": 1,
        "alert_type": "model_unavailable",
        "severity": "error",
        "facts": {"failed_probes": 3},
        **over,
    }


def result(origin="act:a1:a1:result", **over):
    return {
        "origin_record_id": origin,
        "kind": "delivery_result",
        "occurred_at": now_iso(-4),
        "target_environment": "production",
        "source_incident_id": "i1",
        "generation": 1,
        "action_id": "a1",
        "attempt_id": "a1:1",
        "channel": "wecom",
        "outcome": "rejected",
        "error_code": "wecom_errcode_45009",
        "duration_ms": 412,
        **over,
    }


async def ingest(client, events, token=WRITER, **extra):
    return await client.post(
        "/internal/alert-history/events",
        headers=bearer(token),
        json={"schema_version": 1, "events": events, **extra},
    )


async def stored(pool, where="TRUE"):
    async with pool.acquire() as conn:
        return await conn.fetchval(
            f"SELECT count(*) FROM alert_history_events WHERE deployment_id LIKE 'ahtest-%' AND {where}"
        )


# -- ingestion ---------------------------------------------------------------------


async def test_commit_then_duplicate_then_conflict(api):
    client, _rt, pool = api
    events = [trigger(), result()]
    first = await ingest(client, events)
    assert first.status_code == 200
    body = first.json()
    assert body["committed"] is True and body["source"] == "status_monitor"
    assert [r["status"] for r in body["results"]] == ["inserted", "inserted"]
    again = await ingest(client, events)  # the same facts, resent
    assert [r["status"] for r in again.json()["results"]] == ["duplicate", "duplicate"]
    assert [r["record_id"] for r in again.json()["results"]] == [
        r["record_id"] for r in body["results"]
    ]
    changed = await ingest(client, [{**events[1], "outcome": "accepted", "error_code": None}])
    assert changed.json()["results"][0]["status"] == "conflict"
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT source, source_namespace, deployment_id, outcome, credential_id "
            "FROM alert_history_events WHERE origin_record_id = 'act:a1:a1:result'"
        )
    # Identity came from the credential; the stored fact was not overwritten.
    assert dict(row) == {
        "source": "status_monitor",
        "source_namespace": NS,
        "deployment_id": DEP,
        "outcome": "rejected",
        "credential_id": "monitor",
    }


@pytest.mark.parametrize("field", ["source", "source_namespace", "deployment_id", "credential_id"])
async def test_body_cannot_name_its_own_identity(api, field):
    client, _rt, pool = api
    response = await ingest(client, [trigger(**{field: "backend"})])
    assert response.status_code == 422
    assert "backend" not in response.text  # values are never echoed
    assert await stored(pool) == 0


async def test_environment_outside_the_grant_writes_nothing(api):
    client, _rt, pool = api
    response = await ingest(client, [trigger(), trigger("x:2", target_environment="development")])
    assert response.status_code == 403
    assert response.json()["error"]["type"] == "scope_denied"
    assert await stored(pool) == 0


async def test_credential_kinds_are_separate(api):
    client, _rt, pool = api
    assert (await ingest(client, [trigger()], token=READER)).status_code == 403
    assert (await ingest(client, [trigger()], token=EXPIRED_WRITER)).status_code == 401
    admin = await client.post(
        "/internal/alert-history/events",
        headers=ADMIN,
        json={"schema_version": 1, "events": [trigger()]},
    )
    assert admin.status_code == 401
    assert (await client.post("/internal/alert-history/events", json={})).status_code == 401
    # A writer cannot read history, nor use any other admin route.
    assert (
        await client.get("/admin/alert-history/events", headers=bearer(WRITER))
    ).status_code == 403
    assert (await client.get("/admin/alerts/mutes", headers=bearer(WRITER))).status_code == 401
    # A reader cannot use other admin routes.
    assert (await client.get("/admin/alerts/mutes", headers=bearer(READER))).status_code == 401
    assert (await client.delete("/admin/alerts/snooze", headers=bearer(READER))).status_code == 401
    assert await stored(pool) == 0


async def test_unparseable_credentials_fail_closed(api, monkeypatch):
    client, _rt, _pool = api
    monkeypatch.setenv(auth.WRITE_SETTING, "[{not json")
    auth._CACHE.clear()
    response = await ingest(client, [trigger()])
    assert response.status_code == 503
    assert response.json()["error"]["type"] == "history_auth_unavailable"
    # Readers fail closed too; an admin still gets in on its own credential.
    assert (
        await client.get("/admin/alert-history/status", headers=bearer(READER))
    ).status_code == 503
    assert (await client.get("/admin/alert-history/status", headers=ADMIN)).status_code == 200


async def test_limits(api):
    client, _rt, pool = api
    big = await client.post(
        "/internal/alert-history/events",
        headers={**bearer(WRITER), "content-type": "application/json"},
        content=b'{"schema_version": 1, "events": []' + b" " * (300 * 1024) + b"}",
    )
    assert big.status_code == 413
    many = await ingest(client, [trigger(f"o:{n}") for n in range(51)])
    assert many.status_code == 422
    wrong_type = await client.post(
        "/internal/alert-history/events",
        headers={**bearer(WRITER), "content-type": "text/plain"},
        content=b"x",
    )
    assert wrong_type.status_code == 415
    future = await ingest(client, [trigger(occurred_at=now_iso(60))])
    assert future.status_code == 422
    dup_in_batch = await ingest(client, [trigger("o:same"), trigger("o:same")])
    assert dup_in_batch.status_code == 422
    assert await stored(pool) == 0


async def test_database_unavailable_is_retryable_never_empty(api):
    client, rt, pool = api
    db = postgres.target()
    down = runtime_mod.AlertHistoryRuntime(
        enabled=True,
        identity=rt.identity,
        db_config={**db.connect_kwargs(), "port": 1},
        fence_secret=postgres.FENCE_SECRET,
    )
    runtime_mod.set_runtime(down)
    try:
        response = await ingest(client, [trigger()])
        assert response.status_code == 503 and response.headers["Retry-After"]
        events = await client.get("/admin/alert-history/events", headers=ADMIN)
        assert events.status_code == 503
        status = (await client.get("/admin/alert-history/status", headers=ADMIN)).json()
        assert status["database"]["status"] == "unavailable"
        assert status["known_gaps"] is None
        assert {s["connection"] for s in status["sources"]} == {"unknown"}
        assert status["unknown_gaps_possible"] is True
    finally:
        await down.stop(producer_budget_sec=0, drain_budget_sec=0.2)
        runtime_mod.set_runtime(rt)
    assert await stored(pool) == 0


# -- reading -----------------------------------------------------------------------


async def test_status_lists_configured_sources_before_they_report(api):
    client, _rt, pool = api
    status = (await client.get("/admin/alert-history/status", headers=ADMIN)).json()
    monitor = next(s for s in status["sources"] if s["source"] == "status_monitor")
    assert monitor["connection"] == "never_reported"
    assert monitor["first_recorded_at"] is None  # no claim of "zero alerts"
    assert status["completeness"] == "best_effort" and status["unknown_gaps_possible"] is True
    assert status["capture"]["this_process"]["scope"] == "answering_process_only"
    backend = next(s for s in status["sources"] if s["source"] == "backend")
    assert backend["capture_enabled"] is True

    heartbeat = {
        "source_instance_id": "monitor-ahtest-1",
        "target_environment": "production",
        "observed_at": now_iso(),
        "pending_count": 2,
        "pending_reported_at": now_iso(),
        "rules_snapshot": {"monitor_interval_sec": 60, "webhook": "https://x/y"},
    }
    beat = await ingest(client, [], source_status=heartbeat)
    assert beat.status_code == 200 and beat.json()["source_status"] == "recorded"
    status = (await client.get("/admin/alert-history/status", headers=bearer(READER))).json()
    monitor = next(s for s in status["sources"] if s["source"] == "status_monitor")
    assert monitor["connection"] == "reporting"
    (instance,) = monitor["instances"]
    assert (
        instance["pending_snapshot"]["count"] == 2 and instance["pending_snapshot"]["reported_at"]
    )
    assert status["capture"]["this_process"] is None  # local buffers are for admins
    async with pool.acquire() as conn:
        rules = await conn.fetchval(
            "SELECT rules_snapshot FROM alert_history_sources WHERE source_instance_id = 'monitor-ahtest-1'"
        )
    assert "webhook" not in rules  # secret-named source metadata is dropped


async def test_reader_scope_isolation(api):
    client, _rt, _pool = api
    await ingest(client, [trigger(), trigger("o:staging", target_environment="staging")])
    mine = (await client.get("/admin/alert-history/events", headers=bearer(READER))).json()
    assert [e["origin_record_id"] for e in mine["events"]] == ["inc:i1:g1:triggered"]
    other = (await client.get("/admin/alert-history/events", headers=bearer(OTHER_READER))).json()
    assert other["events"] == []
    sources = (await client.get("/admin/alert-history/status", headers=bearer(OTHER_READER))).json()
    assert sources["sources"] == []
    admin = (await client.get("/admin/alert-history/events", headers=ADMIN)).json()
    assert len(admin["events"]) == 2


async def test_pagination_cursor_and_bounds(api):
    client, _rt, _pool = api
    await ingest(client, [trigger(f"o:{n}", occurred_at=now_iso(-30 + n)) for n in range(7)])
    seen = []
    cursor = None
    pages = 0
    while True:
        params = {"limit": 3, **({"cursor": cursor} if cursor else {})}
        page = (
            await client.get("/admin/alert-history/events", headers=ADMIN, params=params)
        ).json()
        seen += [e["origin_record_id"] for e in page["events"]]
        pages += 1
        cursor = page["next_cursor"]
        assert page["truncated"] is (cursor is not None)
        if cursor is None:
            break
    assert seen == [f"o:{n}" for n in range(7)] and pages == 3
    foreign = await client.get(
        "/admin/alert-history/events",
        headers=ADMIN,
        params={"cursor": "not-a-cursor-at-all", "kind": "x"},
    )
    assert foreign.status_code == 422
    wide = await client.get(
        "/admin/alert-history/events",
        headers=ADMIN,
        params={"since": "2026-01-01T00:00:00Z", "until": "2026-02-01T00:00:00Z"},
    )
    assert wide.status_code == 422
    naive = await client.get(
        "/admin/alert-history/events", headers=ADMIN, params={"since": "2026-01-01T00:00:00"}
    )
    assert naive.status_code == 422
    assert (
        await client.get("/admin/alert-history/events", headers=ADMIN, params={"limit": 201})
    ).status_code == 422


async def test_a_cursor_cannot_be_reused_for_another_query(api):
    client, _rt, _pool = api
    await ingest(client, [trigger(f"o:{n}") for n in range(3)])
    page = (
        await client.get("/admin/alert-history/events", headers=ADMIN, params={"limit": 1})
    ).json()
    reuse = await client.get(
        "/admin/alert-history/events",
        headers=ADMIN,
        params={"limit": 1, "cursor": page["next_cursor"], "kind": "delivery_result"},
    )
    assert reuse.status_code == 422


async def test_source_incident_timeline_keeps_generations(api):
    client, _rt, _pool = api
    await ingest(
        client,
        [
            trigger(),
            {**trigger("inc:i1:g1:ended"), "kind": "condition_ended", "end_basis": "source_closed"},
            trigger("inc:i1:g2:triggered", generation=2),
        ],
    )
    missing_ns = await client.get(
        "/admin/alert-history/source-incidents/status_monitor/i1", headers=ADMIN
    )
    assert missing_ns.status_code == 422
    response = await client.get(
        "/admin/alert-history/source-incidents/status_monitor/i1",
        headers=bearer(READER),
        params={"source_namespace": NS},
    )
    assert response.status_code == 200, response.text
    timeline = response.json()
    assert [(e["kind"], e["generation"]) for e in timeline["events"]] == [
        ("condition_triggered", 1),
        ("condition_ended", 1),
        ("condition_triggered", 2),
    ]
    assert timeline["order"] == "recorded_at, record_id"
    out = await client.get(
        "/admin/alert-history/source-incidents/status_monitor/i1",
        headers=bearer(OTHER_READER),
        params={"source_namespace": NS},
    )
    assert out.status_code == 404


async def test_backend_episode_and_test_send_reach_postgres(api):
    client, rt, _pool = api
    await alerts.alert_on_transition(
        key="fivexx_rate",
        breached=True,
        severity=alerts.AlertSeverity.ERROR,
        title="5xx rate exceeded",
        context=lambda: {"rate": "9.0%"},
        cooldown_sec=300,
    )
    episode = rt.capture.open_episode("fivexx_rate")
    sent = await client.post("/admin/alerts/test", headers=ADMIN, json={"channel": "slack"})
    assert sent.status_code == 200
    test_id = sent.json()["test_id"]
    while rt.queue:
        assert await rt.writer.flush_once()
    timeline = (await client.get(f"/admin/alert-history/episodes/{episode}", headers=ADMIN)).json()
    assert [e["kind"] for e in timeline["events"]] == [
        "condition_triggered",
        "delivery_attempt",
        "delivery_result",
    ]
    assert timeline["end_recorded"] is False
    tests = (
        await client.get("/admin/alert-history/events", headers=ADMIN, params={"origin": "test"})
    ).json()
    assert {e["kind"] for e in tests["events"]} == {"delivery_attempt", "delivery_result"}
    assert {e["audit_ref"] for e in tests["events"]} == {test_id}
    assert all(e["episode_id"] is None for e in tests["events"])
    assert SLACK not in json.dumps(tests)


async def test_request_evidence_endpoint(api):
    client, rt, pool = api
    user = "ahtest-api-user"
    digest = fence_account_digest(user, postgres.FENCE_SECRET)
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO api_logs (request_id, model_id, provider, user_id, status_code, latency_ms) "
            "VALUES ('ahtest-api-req', 'model-x', 'prov', $1, 200, 50), "
            "('ahtest-api-unref', 'model-x', 'prov', $1, 200, 50)",
            user,
        )
    from serving.utils import context as req_ctx

    req_ctx.set({"request_id": "ahtest-api-req", "user_id": user})
    try:
        await alerts.alert_on_transition(
            key="circuit_open:prov-ev",
            breached=True,
            severity=alerts.AlertSeverity.ERROR,
            title="Provider circuit opened",
            context=lambda: {"provider": "prov-ev"},
            cooldown_sec=300,
            kind="state",
        )
    finally:
        req_ctx.set({})
    while rt.queue:
        assert await rt.writer.flush_once()
    ok = (
        await client.get("/admin/alert-history/request-evidence/ahtest-api-req", headers=ADMIN)
    ).json()
    assert ok["available"] is True
    assert ok["evidence"]["status_code"] == 200 and "user_id" not in ok["evidence"]
    assert user not in json.dumps(ok) and digest not in json.dumps(ok)
    # Not an api_logs browser: an unreferenced request is 404 even though it exists.
    unref = await client.get(
        "/admin/alert-history/request-evidence/ahtest-api-unref", headers=ADMIN
    )
    assert unref.status_code == 404
    other = await client.get(
        "/admin/alert-history/request-evidence/ahtest-api-req", headers=bearer(OTHER_READER)
    )
    assert other.status_code == 404


# -- round 2: per-target status, final snapshots, whole-timeline summaries ------


async def _seed_instances(pool, source, ns, env, count, *, age_sec, prefix):
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO alert_history_sources (source, source_namespace, source_instance_id, "
            "deployment_id, target_environment, last_report_at, last_write_at) "
            "SELECT $1, $2, $3 || n, $4, $5, now() - make_interval(secs => $6) - n * interval '1 ms', "
            "now() - make_interval(secs => $6) FROM generate_series(1, $7) AS n",
            source,
            ns,
            prefix,
            DEP,
            env,
            float(age_sec),
            count,
        )


def _target(status, source, env):
    return next(
        s for s in status["sources"] if s["source"] == source and s["target_environment"] == env
    )


async def test_status_is_exact_per_target_beyond_the_listing_caps(api):
    client, rt, pool = api
    # A busy production monitor: 250 fresh instances. The backend reported
    # once, an hour ago. Staging never reported at all.
    await _seed_instances(pool, "status_monitor", NS, "production", 250, age_sec=5, prefix="busy-")
    await _seed_instances(
        pool, "backend", DEP, "production", 1, age_sec=3600, prefix=rt.identity.source_instance_id
    )
    status = (await client.get("/admin/alert-history/status", headers=ADMIN)).json()
    production = _target(status, "status_monitor", "production")
    assert production["connection"] == "reporting"
    assert production["instance_count"] == 250
    assert len(production["instances"]) == 20 and production["instances_truncated"] is True
    staging = _target(status, "status_monitor", "staging")
    assert staging["connection"] == "never_reported"  # not hidden by the healthy target
    backend = _target(status, "backend", "production")
    # Pushed out of no shared list: it reported, so it is stale, not "never".
    assert backend["connection"] == "stale"
    assert backend["instance_count"] == 1


async def test_a_healthy_target_does_not_hide_a_silent_one(api):
    client, _rt, pool = api
    await _seed_instances(pool, "status_monitor", NS, "production", 1, age_sec=10, prefix="p-")
    await _seed_instances(pool, "status_monitor", NS, "staging", 1, age_sec=900, prefix="s-")
    status = (await client.get("/admin/alert-history/status", headers=ADMIN)).json()
    assert _target(status, "status_monitor", "production")["connection"] == "reporting"
    silent = _target(status, "status_monitor", "staging")
    assert silent["connection"] == "stale"
    assert silent["instances"][0]["state"] == "unknown"  # no stop record: maybe crashed


async def test_a_slow_cadence_source_uses_its_own_stale_bound(api, monkeypatch):
    client, _rt, pool = api
    writers = json.loads(os.environ[auth.WRITE_SETTING])
    writers[0]["stale_after_sec"] = 1800  # a 20-minute cron, with margin
    monkeypatch.setenv(auth.WRITE_SETTING, json.dumps(writers))
    auth._CACHE.clear()
    await _seed_instances(pool, "status_monitor", NS, "production", 1, age_sec=1200, prefix="cron-")
    status = (await client.get("/admin/alert-history/status", headers=ADMIN)).json()
    production = _target(status, "status_monitor", "production")
    assert production["stale_after_sec"] == 1800
    assert production["connection"] == "reporting"
    # The backend keeps the process default.
    assert _target(status, "backend", "production")["stale_after_sec"] == 180


async def test_a_completed_invocation_reads_as_stopped(api):
    client, _rt, _pool = api
    final = {
        "source_instance_id": "status-monitor:inv-final",
        "target_environment": "production",
        "observed_at": now_iso(),
        "started_at": now_iso(-1),
        "pending_count": 0,
        "pending_reported_at": now_iso(),
        "stopped_at": now_iso(),
        "stop_reason": "invocation_complete",
        "lost_at_stop": 1,
    }
    response = await ingest(
        client, [trigger(source_instance_id="status-monitor:inv-final")], source_status=final
    )
    assert response.status_code == 200 and response.json()["source_status"] == "recorded"
    status = (await client.get("/admin/alert-history/status", headers=ADMIN)).json()
    production = _target(status, "status_monitor", "production")
    (instance,) = production["instances"]
    assert (instance["state"], instance["stop_reason"], instance["lost_at_stop"]) == (
        "stopped",
        "invocation_complete",
        1,
    )
    assert production["connection"] == "reporting"
    incoherent = await ingest(client, [], source_status={**final, "pending_count": 2})
    assert incoherent.status_code == 422
    future = await ingest(client, [], source_status={**final, "stopped_at": now_iso(30)})
    assert future.status_code == 422


async def test_events_without_an_instance_read_as_incomplete(api):
    client, _rt, _pool = api
    await ingest(
        client, [trigger("inc:noinst"), trigger("inc:inst", source_instance_id="sm:inv-1")]
    )
    events = (await client.get("/admin/alert-history/events", headers=ADMIN)).json()["events"]
    flags = {e["origin_record_id"]: e["association_incomplete"] for e in events}
    assert flags == {"inc:noinst": True, "inc:inst": False}


async def test_end_recorded_covers_the_whole_timeline_not_one_page(api):
    client, _rt, _pool = api
    await ingest(
        client,
        [
            trigger(),
            {**trigger("inc:i1:g1:ended"), "kind": "condition_ended", "end_basis": "source_closed"},
        ],
    )
    await ingest(client, [trigger("inc:i1:g2:triggered", generation=2)])
    url = "/admin/alert-history/source-incidents/status_monitor/i1"
    params = {"source_namespace": NS, "limit": 1}
    pages = []
    cursor = None
    while True:
        page = (
            await client.get(
                url, headers=ADMIN, params={**params, **({"cursor": cursor} if cursor else {})}
            )
        ).json()
        pages.append(page)
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert len(pages) == 3
    # The end record was on page 2; the last page still knows it exists.
    assert all(p["end_recorded"] is True for p in pages)
    generations = {g["generation"]: g for g in pages[-1]["summary"]["generations"]}
    assert generations[1]["end_recorded"] is True and generations[2]["end_recorded"] is False
    assert pages[-1]["summary"]["records"] == 3


async def test_known_gaps_are_bounded_and_say_so(api):
    client, _rt, _pool = api
    gaps = [
        {
            **trigger(f"gap:{n}"),
            "kind": "history_gap",
            "count": 1,
            "error_code": "buffer_full",
            "facts": {},
        }
        for n in range(50)
    ]
    await ingest(client, gaps)
    await ingest(client, [{**gaps[0], "origin_record_id": "gap:extra"}])
    status = (await client.get("/admin/alert-history/status", headers=ADMIN)).json()
    assert len(status["known_gaps"]) == 50
    assert status["known_gaps_truncated"] is True
    assert "kind=history_gap" in status["known_gaps_continue"]


async def test_deep_or_malformed_bodies_are_a_neutral_422(api, caplog):
    client, _rt, pool = api
    secret_marker = "SYNTHETIC-BODY-MARKER"
    deep = b'{"schema_version": 1, "events": [' + b"[" * 100_000 + b"]" * 100_000 + b"]}"
    for body in (
        deep,
        b'{"schema_version": 1, "events": [}' + secret_marker.encode(),
        b'{"schema_version": NaN}',
        b"\xff\xfe" + secret_marker.encode(),
    ):
        response = await client.post(
            "/internal/alert-history/events",
            headers={**bearer(WRITER), "content-type": "application/json"},
            content=body[: 250 * 1024],
        )
        assert response.status_code == 422, response.text
        assert secret_marker not in response.text
    assert secret_marker not in caplog.text
    assert await stored(pool) == 0


async def test_a_slow_body_gets_its_own_bounded_budget(api, monkeypatch):
    client, rt, _pool = api

    async def never_arrives(request):
        await asyncio.Event().wait()

    monkeypatch.setattr(alert_history_ingest, "_bounded_body", never_arrives)
    monkeypatch.setattr(alert_history_ingest, "BODY_READ_TIMEOUT_SEC", 0.05)
    response = await ingest(client, [trigger()])
    assert response.status_code == 408
    assert not rt.ingest_slots.locked()  # no commit slot was taken for it


async def test_an_end_in_a_generation_past_the_group_list_is_still_reported(api):
    """101 generations; the only end record is in generation 101.

    The per-generation list holds 100 groups, so generation 101 is left out of
    it. The totals come from the exact aggregate, so the answer must still say
    an end was recorded, and say that the list is truncated.
    """
    client, _rt, _pool = api
    events = [
        trigger(f"inc:i9:g{g}:triggered", source_incident_id="i9", generation=g)
        for g in range(1, 102)
    ]
    events.append(
        {
            **trigger("inc:i9:g101:ended", source_incident_id="i9", generation=101),
            "kind": "condition_ended",
            "end_basis": "source_closed",
        }
    )
    for start in range(0, len(events), 50):
        response = await ingest(client, events[start : start + 50])
        assert response.status_code == 200, response.text
    timeline = (
        await client.get(
            "/admin/alert-history/source-incidents/status_monitor/i9",
            headers=ADMIN,
            params={"source_namespace": NS, "limit": 10},
        )
    ).json()
    summary = timeline["summary"]
    assert summary["generations_truncated"] is True
    assert len(summary["generations"]) == 100
    assert 101 not in {g["generation"] for g in summary["generations"]}
    assert not any(g["end_recorded"] for g in summary["generations"])
    # Exact, not derived from the truncated list:
    assert timeline["end_recorded"] is True and summary["end_recorded"] is True
    assert summary["records"] == 102 and summary["generation_count"] == 101
    assert summary["trigger_recorded"] is True
    assert "not commit order" in timeline["pagination"]


# -- on by default, off on request -------------------------------------------------


async def test_startup_with_nothing_configured_records_to_postgres(api, monkeypatch):
    """The real start path: no setting, no reader or writer, a non-default deployment."""
    client, rt, pool = api
    monkeypatch.delenv(auth.READ_SETTING)
    monkeypatch.delenv(auth.WRITE_SETTING)
    auth._CACHE.clear()
    monkeypatch.setenv(runtime_mod.DEPLOYMENT_SETTING, "ahtest-default-on")
    started = runtime_mod.start_runtime(
        db_config=postgres.target().connect_kwargs(), fence_secret=postgres.FENCE_SECRET
    )
    try:
        assert started is not None and started.enabled
        assert hooks.active() is started.capture
        await alerts.alert_on_transition(
            key="fivexx_rate",
            breached=True,
            severity=alerts.AlertSeverity.ERROR,
            title="5xx rate exceeded",
            context=lambda: {"rate": "9.0%"},
            cooldown_sec=300,
        )
        # Ingestion still needs a writer credential.
        assert (await ingest(client, [trigger()])).status_code == 404
    finally:
        await runtime_mod.stop_runtime()
        runtime_mod.set_runtime(rt)
        hooks.install(rt.capture)
    async with pool.acquire() as conn:
        kinds = {
            row["kind"]
            for row in await conn.fetch(
                "SELECT kind FROM alert_history_events WHERE deployment_id = 'ahtest-default-on'"
            )
        }
        source = await conn.fetchrow(
            "SELECT stop_reason, lost_at_stop FROM alert_history_sources "
            "WHERE source_instance_id = $1",
            started.identity.source_instance_id,
        )
    assert {"condition_triggered", "delivery_attempt", "delivery_result"} <= kinds
    assert dict(source) == {"stop_reason": "shutdown", "lost_at_stop": 0}


async def test_capture_turned_off_keeps_stored_history_readable(api, monkeypatch):
    client, rt, pool = api
    assert (await ingest(client, [trigger()])).status_code == 200
    monkeypatch.setenv(runtime_mod.ENABLED_SETTING, "false")
    hooks.install(None)  # a fresh process: the fixture's recorder is not there
    off = runtime_mod.start_runtime(
        db_config=postgres.target().connect_kwargs(), fence_secret=postgres.FENCE_SECRET
    )
    try:
        assert off is not None and not off.enabled and hooks.active() is None
        for headers in (ADMIN, bearer(READER)):
            events = await client.get(
                "/admin/alert-history/events", headers=headers, params={"deployment_id": DEP}
            )
            assert events.status_code == 200
            assert [e["origin_record_id"] for e in events.json()["events"]] == [
                "inc:i1:g1:triggered"
            ]
        status = (await client.get("/admin/alert-history/status", headers=ADMIN)).json()
        assert status["capture"]["enabled"] is False
        backend = next(s for s in status["sources"] if s["source"] == "backend")
        assert backend["connection"] == "disabled"
        # A valid writer is refused while capture is off.
        refused = await ingest(client, [trigger("inc:i2:g1:triggered")])
        assert refused.status_code == 404
    finally:
        await runtime_mod.stop_runtime()
        runtime_mod.set_runtime(rt)
        hooks.install(rt.capture)
    assert await stored(pool) == 1
