"""End-to-end agent job lifecycle over the real Postgres-backed store.

The API-surface tests use an in-memory stand-in; this one wires the router to
a real ``AgentJobStore`` so the HTTP layer and the fencing SQL are exercised
together — including the zombie-worker case, where a reaped attempt's token
still parses but every write must be rejected with 409.
"""

from __future__ import annotations

import os

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from serving.servers.deps import get_agent_job_store
from serving.servers.routers import agent_jobs as agent_jobs_router
from serving.storage.agent_job_store import AgentJobStore

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

_ALLOWED_TEST_DB_PATTERN = "_test_"
_TABLES_IN_FK_ORDER = ("agent_job_artifacts", "agent_job_events", "agent_attempts", "agent_jobs")
_OWNER = "user-owner"


@pytest_asyncio.fixture()
async def api(monkeypatch):
    """Mount the router over a real AgentJobStore with auth stubbed."""
    import asyncpg

    monkeypatch.setenv("API_KEY_SECRET", "lifecycle-test-secret")
    from serving.config.settings import get_settings

    get_settings.cache_clear()

    base_db_name = os.getenv("TEST_DB_NAME", "freeinference_test_db")
    worker_id = os.environ.get("PYTEST_XDIST_WORKER", "master")
    test_db_name = base_db_name if worker_id == "master" else f"{base_db_name}_{worker_id}"
    if _ALLOWED_TEST_DB_PATTERN not in (test_db_name or ""):
        pytest.fail(f"SAFETY: TEST_DB_NAME='{test_db_name}' is not a test database.")

    try:
        pool = await asyncpg.create_pool(
            host=os.getenv("TEST_DB_HOST", "localhost"),
            port=int(os.getenv("TEST_DB_PORT", "5432")),
            database=test_db_name,
            user=os.getenv("TEST_DB_USER", "postgres"),
            password=os.getenv("TEST_DB_PASSWORD", "postgres"),
            min_size=1,
            max_size=5,
        )
    except Exception as exc:
        pytest.skip(f"PostgreSQL not available: {exc}")
        return  # unreachable

    store = AgentJobStore(pool)
    await store.initialize()
    async with pool.acquire() as conn:
        for table in _TABLES_IN_FK_ORDER:
            await conn.execute(f"DELETE FROM {table}")

    from serving.servers.auth import verify_api_key

    app = FastAPI()
    app.include_router(agent_jobs_router.router)
    app.dependency_overrides[get_agent_job_store] = lambda: store
    app.dependency_overrides[verify_api_key] = lambda: {"user_id": _OWNER, "role": "pro"}

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, store, pool

    async with pool.acquire() as conn:
        for table in _TABLES_IN_FK_ORDER:
            await conn.execute(f"DELETE FROM {table}")
    await pool.close()
    get_settings.cache_clear()


async def test_full_lifecycle_create_to_published_pr(api):
    """create → claim → events → artifact → publish → owner sees the PR."""
    client, _store, _pool = api

    created = await client.post(
        "/v1/agent/jobs",
        json={"repo": "owner/name", "task_prompt": "fix the flaky test", "model": "glm-5.1"},
    )
    assert created.status_code == 201
    job_id = created.json()["id"]

    claim = await client.post("/v1/agent/worker/claim", json={"worker_id": "w1"})
    assert claim.status_code == 200
    auth = {"Authorization": f"Bearer {claim.json()['worker_token']}"}

    for text in ("thinking", "editing"):
        event = await client.post(
            f"/v1/agent/worker/jobs/{job_id}/events",
            json={"event_type": "message", "payload": {"text": text}},
            headers=auth,
        )
        assert event.status_code == 201

    artifact = await client.post(
        f"/v1/agent/worker/jobs/{job_id}/artifacts",
        json={"kind": "patch", "content": "diff --git a/x b/x"},
        headers=auth,
    )
    assert artifact.status_code == 201

    assert (
        await client.post(f"/v1/agent/worker/jobs/{job_id}/publish/begin", headers=auth)
    ).status_code == 200
    assert (
        await client.post(
            f"/v1/agent/worker/jobs/{job_id}/publish/complete",
            json={"pr_url": "https://github.com/owner/name/pull/7"},
            headers=auth,
        )
    ).status_code == 200

    job = (await client.get(f"/v1/agent/jobs/{job_id}")).json()
    assert job["state"] == "succeeded"
    assert job["published_pr_url"] == "https://github.com/owner/name/pull/7"

    events = (await client.get(f"/v1/agent/jobs/{job_id}/events")).json()
    assert [event["seq"] for event in events["events"]] == [1, 2]
    patch = (await client.get(f"/v1/agent/jobs/{job_id}/artifacts/patch")).json()
    assert patch["content"] == "diff --git a/x b/x"


async def test_reaped_worker_token_is_rejected_end_to_end(api):
    """A zombie worker's token parses but every write is fenced out with 409."""
    client, store, pool = api

    created = await client.post(
        "/v1/agent/jobs",
        json={"repo": "owner/name", "task_prompt": "slow task", "model": "glm-5.1"},
    )
    job_id = created.json()["id"]
    claim = await client.post("/v1/agent/worker/claim", json={"worker_id": "w1"})
    zombie_auth = {"Authorization": f"Bearer {claim.json()['worker_token']}"}
    attempt_id = claim.json()["attempt_id"]

    # The lease expires and the reaper requeues the job as a new attempt.
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_attempts SET lease_expires_at = NOW() - INTERVAL '1 second' "
            "WHERE id = $1",
            attempt_id,
        )
    actions = await store.reap_expired(max_attempts=3)
    assert actions[0]["action"] == "queued"

    # Every write path of the zombie is now rejected.
    for path, payload in (
        (f"/v1/agent/worker/jobs/{job_id}/heartbeat", {}),
        (f"/v1/agent/worker/jobs/{job_id}/events", {"event_type": "message"}),
        (f"/v1/agent/worker/jobs/{job_id}/artifacts", {"kind": "patch", "content": "x"}),
        (f"/v1/agent/worker/jobs/{job_id}/finish", {"state": "succeeded"}),
    ):
        response = await client.post(path, json=payload, headers=zombie_auth)
        assert response.status_code == 409, path

    # A fresh worker takes over and can write.
    retry = await client.post("/v1/agent/worker/claim", json={"worker_id": "w2"})
    assert retry.json()["attempt_no"] == 2
    live_auth = {"Authorization": f"Bearer {retry.json()['worker_token']}"}
    assert (
        await client.post(
            f"/v1/agent/worker/jobs/{job_id}/events",
            json={"event_type": "message", "payload": {"text": "retry"}},
            headers=live_auth,
        )
    ).status_code == 201

    # The supersede is visible to the owner as an append-only control event.
    events = (await client.get(f"/v1/agent/jobs/{job_id}/events")).json()["events"]
    assert [event["event_type"] for event in events] == ["attempt_superseded", "message"]


async def test_cancel_running_job_is_delivered_via_heartbeat(api):
    """Owner cancel surfaces to the worker, which performs the transition."""
    client, _store, _pool = api

    created = await client.post(
        "/v1/agent/jobs",
        json={"repo": "owner/name", "task_prompt": "long task", "model": "glm-5.1"},
    )
    job_id = created.json()["id"]
    claim = await client.post("/v1/agent/worker/claim", json={"worker_id": "w1"})
    auth = {"Authorization": f"Bearer {claim.json()['worker_token']}"}

    cancel = await client.post(f"/v1/agent/jobs/{job_id}/cancel")
    assert cancel.status_code == 200
    assert cancel.json()["state"] == "running"
    assert cancel.json()["cancel_requested"] is True

    beat = await client.post(f"/v1/agent/worker/jobs/{job_id}/heartbeat", json={}, headers=auth)
    assert beat.json()["cancel_requested"] is True

    finish = await client.post(
        f"/v1/agent/worker/jobs/{job_id}/finish", json={"state": "cancelled"}, headers=auth
    )
    assert finish.status_code == 200
    assert (await client.get(f"/v1/agent/jobs/{job_id}")).json()["state"] == "cancelled"
