"""Integration tests for AgentJobStore (Postgres): fencing, reaper, publish.

These pin the concurrency-ownership contract from issue #1041: attempts +
lease_generation fencing (zombie workers can never write after a reap),
append-only events with a global cursor, one-shot publish, and reaper
close-and-requeue semantics.
"""

from __future__ import annotations

import os

import pytest
import pytest_asyncio

from serving.storage.agent_job_store import AgentJobStore

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

_ALLOWED_TEST_DB_PATTERN = "_test_"
_TABLES_IN_FK_ORDER = ("agent_job_artifacts", "agent_job_events", "agent_attempts", "agent_jobs")


@pytest_asyncio.fixture
async def store():
    """Provide a clean AgentJobStore over the shared test database."""
    import asyncpg

    base_db_name = os.getenv("TEST_DB_NAME", "freeinference_test_db")
    worker_id = os.environ.get("PYTEST_XDIST_WORKER", "master")
    test_db_name = base_db_name if worker_id == "master" else f"{base_db_name}_{worker_id}"
    if _ALLOWED_TEST_DB_PATTERN not in (test_db_name or ""):
        pytest.fail(
            f"SAFETY: TEST_DB_NAME='{test_db_name}' does not contain "
            f"'{_ALLOWED_TEST_DB_PATTERN}'. Refusing to run."
        )

    db_config = {
        "host": os.getenv("TEST_DB_HOST", "localhost"),
        "port": int(os.getenv("TEST_DB_PORT", "5432")),
        "database": test_db_name,
        "user": os.getenv("TEST_DB_USER", "postgres"),
        "password": os.getenv("TEST_DB_PASSWORD", "postgres"),
    }

    try:
        pool = await asyncpg.create_pool(**db_config, min_size=1, max_size=5)
    except Exception as exc:
        pytest.skip(f"PostgreSQL not available: {exc}")
        return  # unreachable

    job_store = AgentJobStore(pool)
    await job_store.initialize()

    async with pool.acquire() as conn:
        for table in _TABLES_IN_FK_ORDER:
            await conn.execute(f"DELETE FROM {table}")

    try:
        yield job_store
    finally:
        async with pool.acquire() as conn:
            for table in _TABLES_IN_FK_ORDER:
                await conn.execute(f"DELETE FROM {table}")
        await pool.close()


async def _create_job(store: AgentJobStore, **overrides) -> dict:
    """Create a job with sensible defaults."""
    params = {
        "user_id": "user-1",
        "repo": "HarvardMadSys/hybridInference",
        "task_prompt": "fix the flaky test",
        "runtime": "claude-code",
        "model": "glm-5.1",
        "base_sha": "deadbeef",
    }
    params.update(overrides)
    return await store.create_job(**params)


async def _expire_attempt(store: AgentJobStore, attempt_id: int) -> None:
    """Force an attempt's lease into the past."""
    async with store._pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_attempts SET lease_expires_at = NOW() - INTERVAL '1 second' "
            "WHERE id = $1",
            attempt_id,
        )


async def test_create_get_list_round_trip(store: AgentJobStore):
    """create -> get -> list returns the queued job with its fields."""
    job = await _create_job(store, metadata={"origin": "test"})
    assert job["state"] == "queued"
    assert job["id"].startswith("ajob_")

    fetched = await store.get_job(job["id"])
    assert fetched is not None
    assert fetched["task_prompt"] == "fix the flaky test"
    assert fetched["metadata"] == {"origin": "test"}

    listed = await store.list_jobs(user_id="user-1")
    assert [item["id"] for item in listed] == [job["id"]]
    assert await store.get_job("ajob_missing") is None


async def test_claim_creates_fenced_attempt(store: AgentJobStore):
    """Claiming moves the job to running with attempt_no = generation = 1."""
    job = await _create_job(store)
    claim = await store.claim_job(worker_id="w1", lease_ttl_seconds=60)
    assert claim is not None
    assert claim["id"] == job["id"]
    assert claim["state"] == "running"
    assert claim["attempt_no"] == 1
    assert claim["lease_generation"] == 1

    # The job is no longer queued, so a second claim finds nothing.
    assert await store.claim_job(worker_id="w2", lease_ttl_seconds=60) is None


async def test_heartbeat_extends_and_fences(store: AgentJobStore):
    """Heartbeat succeeds with the right generation and fences the wrong one."""
    await _create_job(store)
    claim = await store.claim_job(worker_id="w1", lease_ttl_seconds=60)

    beat = await store.heartbeat(
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        lease_ttl_seconds=60,
    )
    assert beat["ok"] is True
    assert beat["cancel_requested"] is False

    stale = await store.heartbeat(
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"] + 1,
        lease_ttl_seconds=60,
    )
    assert stale["ok"] is False


async def test_event_seq_and_global_cursor(store: AgentJobStore):
    """Events get per-attempt seq 1..n and a strictly increasing global id."""
    job = await _create_job(store)
    claim = await store.claim_job(worker_id="w1", lease_ttl_seconds=60)

    ids = []
    for index in range(3):
        event_id = await store.append_event(
            attempt_id=claim["attempt_id"],
            lease_generation=claim["lease_generation"],
            event_type="message",
            payload={"index": index},
        )
        assert event_id is not None
        ids.append(event_id)
    assert ids == sorted(ids)

    events = await store.list_events_after(job_id=job["id"], after_id=0)
    assert [event["seq"] for event in events] == [1, 2, 3]
    assert [event["payload"]["index"] for event in events] == [0, 1, 2]

    tail = await store.list_events_after(job_id=job["id"], after_id=ids[1])
    assert [event["id"] for event in tail] == [ids[2]]


async def test_zombie_worker_is_fenced_out_after_reap(store: AgentJobStore):
    """After a reap, every write path of the old attempt is rejected."""
    job = await _create_job(store)
    old = await store.claim_job(worker_id="w1", lease_ttl_seconds=60)
    assert (
        await store.append_event(
            attempt_id=old["attempt_id"],
            lease_generation=old["lease_generation"],
            event_type="message",
            payload={"n": 1},
        )
        is not None
    )

    await _expire_attempt(store, old["attempt_id"])
    actions = await store.reap_expired(max_attempts=3)
    assert actions == [{"job_id": job["id"], "attempt_id": old["attempt_id"], "action": "queued"}]

    # The supersede is announced as an append-only control event.
    events = await store.list_events_after(job_id=job["id"], after_id=0)
    assert events[-1]["event_type"] == "attempt_superseded"
    assert events[-1]["payload"]["reason"] == "lease_expired"

    # A new worker claims attempt 2 / generation 2.
    new = await store.claim_job(worker_id="w2", lease_ttl_seconds=60)
    assert new is not None
    assert new["attempt_no"] == 2
    assert new["lease_generation"] == 2

    # The zombie's every write path is fenced out.
    zombie_kwargs = {
        "attempt_id": old["attempt_id"],
        "lease_generation": old["lease_generation"],
    }
    assert await store.append_event(**zombie_kwargs, event_type="message", payload={}) is None
    assert (await store.heartbeat(**zombie_kwargs, lease_ttl_seconds=60))["ok"] is False
    assert (
        await store.transition(
            job_id=job["id"],
            **zombie_kwargs,
            from_states=("running",),
            to_state="succeeded",
        )
        is False
    )
    assert await store.save_artifact(**zombie_kwargs, kind="patch", content="diff") is None

    # The live attempt keeps working.
    assert (
        await store.append_event(
            attempt_id=new["attempt_id"],
            lease_generation=new["lease_generation"],
            event_type="message",
            payload={"n": 2},
        )
        is not None
    )


async def test_reap_fails_job_after_max_attempts(store: AgentJobStore):
    """Exhausting the attempt budget fails the job instead of requeueing."""
    job = await _create_job(store)
    claim = await store.claim_job(worker_id="w1", lease_ttl_seconds=60)
    await _expire_attempt(store, claim["attempt_id"])

    actions = await store.reap_expired(max_attempts=1)
    assert actions[0]["action"] == "failed"
    fetched = await store.get_job(job["id"])
    assert fetched["state"] == "failed"
    assert "exhausted" in fetched["detail"]


async def test_publish_is_one_shot(store: AgentJobStore):
    """running -> publishing -> succeeded happens at most once."""
    job = await _create_job(store)
    claim = await store.claim_job(worker_id="w1", lease_ttl_seconds=60)
    fence = {
        "job_id": job["id"],
        "attempt_id": claim["attempt_id"],
        "lease_generation": claim["lease_generation"],
    }

    # complete_publish before begin_publish is rejected.
    assert await store.complete_publish(**fence, pr_url="https://x/pr/1") is False

    assert await store.begin_publish(**fence) is True
    # begin_publish is itself one-shot (state has left 'running').
    assert await store.begin_publish(**fence) is False

    assert await store.complete_publish(**fence, pr_url="https://x/pr/1") is True
    assert await store.complete_publish(**fence, pr_url="https://x/pr/2") is False

    fetched = await store.get_job(job["id"])
    assert fetched["state"] == "succeeded"
    assert fetched["published_pr_url"] == "https://x/pr/1"


async def test_publishing_lease_expiry_fails_job(store: AgentJobStore):
    """A lease that expires mid-publish fails the job (never auto-republish)."""
    job = await _create_job(store)
    claim = await store.claim_job(worker_id="w1", lease_ttl_seconds=60)
    assert (
        await store.begin_publish(
            job_id=job["id"],
            attempt_id=claim["attempt_id"],
            lease_generation=claim["lease_generation"],
        )
        is True
    )
    await _expire_attempt(store, claim["attempt_id"])

    actions = await store.reap_expired(max_attempts=3)
    assert actions[0]["action"] == "failed"
    fetched = await store.get_job(job["id"])
    assert fetched["state"] == "failed"
    assert "publish" in fetched["detail"]


async def test_cancel_queued_and_running(store: AgentJobStore):
    """Queued jobs cancel immediately; running jobs cancel via the worker."""
    queued = await _create_job(store)
    assert await store.request_cancel(job_id=queued["id"]) == "cancelled"
    assert (await store.get_job(queued["id"]))["state"] == "cancelled"

    # Owner scoping: the wrong user cannot cancel.
    running = await _create_job(store)
    claim = await store.claim_job(worker_id="w1", lease_ttl_seconds=60)
    assert await store.request_cancel(job_id=running["id"], user_id="someone-else") is None

    assert await store.request_cancel(job_id=running["id"], user_id="user-1") == "running"
    beat = await store.heartbeat(
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        lease_ttl_seconds=60,
    )
    assert beat["ok"] is True
    assert beat["cancel_requested"] is True

    # The worker performs the fenced terminal transition.
    assert (
        await store.transition(
            job_id=running["id"],
            attempt_id=claim["attempt_id"],
            lease_generation=claim["lease_generation"],
            from_states=("running",),
            to_state="cancelled",
        )
        is True
    )
    assert (await store.get_job(running["id"]))["state"] == "cancelled"

    # Cancelling a terminal job is a no-op that reports the state.
    assert await store.request_cancel(job_id=running["id"]) == "cancelled"


async def test_artifact_upsert_and_get(store: AgentJobStore):
    """Artifacts upsert per (job, attempt, kind) and read back the latest."""
    job = await _create_job(store)
    claim = await store.claim_job(worker_id="w1", lease_ttl_seconds=60)
    fence = {
        "attempt_id": claim["attempt_id"],
        "lease_generation": claim["lease_generation"],
    }

    first = await store.save_artifact(**fence, kind="patch", content="diff-v1")
    assert first is not None
    second = await store.save_artifact(**fence, kind="patch", content="diff-v2")
    assert second == first  # replaced in place, not duplicated

    artifact = await store.get_artifact(job_id=job["id"], kind="patch")
    assert artifact is not None
    assert artifact["content"] == "diff-v2"
    assert await store.get_artifact(job_id=job["id"], kind="missing") is None


async def test_model_credential_follows_the_fence(store: AgentJobStore):
    """A worker token buys inference only while its attempt owns the job.

    This is the revocation mechanism for the sandbox's model access: there is
    no key to revoke, so the checks that matter are that a live fence resolves
    and that every way of losing the fence stops resolving.
    """
    job = await _create_job(store, budget_usd=2.5)
    claim = await store.claim_job(worker_id="w1", lease_ttl_seconds=60)
    fence = {
        "job_id": job["id"],
        "attempt_id": claim["attempt_id"],
        "lease_generation": claim["lease_generation"],
    }

    identity = await store.resolve_model_credential(**fence)
    assert identity is not None
    assert identity["user_id"] == "user-1"
    assert identity["budget_usd"] == 2.5

    # A different generation never resolves.
    assert (
        await store.resolve_model_credential(
            job_id=job["id"], attempt_id=claim["attempt_id"], lease_generation=99
        )
        is None
    )

    # Lease expiry alone stops it, even before the reaper runs.
    await _expire_attempt(store, claim["attempt_id"])
    assert await store.resolve_model_credential(**fence) is None

    # After the reap the new attempt resolves and the old one stays dead.
    await store.reap_expired(max_attempts=3)
    retry = await store.claim_job(worker_id="w2", lease_ttl_seconds=60)
    assert await store.resolve_model_credential(**fence) is None
    assert (
        await store.resolve_model_credential(
            job_id=job["id"],
            attempt_id=retry["attempt_id"],
            lease_generation=retry["lease_generation"],
        )
        is not None
    )


async def test_model_credential_dies_with_a_terminal_job(store: AgentJobStore):
    """Finishing the job stops its token from buying more inference."""
    job = await _create_job(store)
    claim = await store.claim_job(worker_id="w1", lease_ttl_seconds=60)
    fence = {
        "job_id": job["id"],
        "attempt_id": claim["attempt_id"],
        "lease_generation": claim["lease_generation"],
    }
    assert await store.resolve_model_credential(**fence) is not None

    await store.transition(**fence, from_states=("running",), to_state="succeeded")
    assert await store.resolve_model_credential(**fence) is None
