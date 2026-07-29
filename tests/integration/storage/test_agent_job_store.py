"""Integration tests for AgentJobStore (Postgres): fencing, reaper, publish.

These pin the concurrency-ownership contract from issue #1041: attempts +
lease_generation fencing (zombie workers can never write after a reap),
append-only events with a global cursor, one-shot publish, and reaper
close-and-requeue semantics.
"""

from __future__ import annotations

import asyncio
import os

import pytest
import pytest_asyncio

from serving.storage.agent_job_store import AgentJobStore

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

_ALLOWED_TEST_DB_PATTERN = "_test_"
_TABLES_IN_FK_ORDER = (
    "agent_thread_messages",
    "agent_job_artifacts",
    "agent_job_events",
    "agent_attempts",
    "agent_jobs",
    "agent_threads",
)


@pytest_asyncio.fixture
async def store():
    """Provide a clean AgentJobStore over the shared test database."""
    import asyncpg

    base_db_name = os.getenv("TEST_DB_NAME", "hybridinference_test_db")
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
        "repo": "example-org/example-repo",
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


async def test_follow_up_waits_then_inherits_thread_messages_and_patch(store: AgentJobStore):
    parent = await _create_job(store)
    child = await store.create_follow_up(
        parent_job_id=parent["id"],
        user_id="user-1",
        prompt="now add a regression test",
        model="qwen-next",
    )
    assert child is not None
    assert child["state"] == "waiting"
    assert child["turn_no"] == 2

    parent_claim = await store.claim_job(worker_id="w1", lease_ttl_seconds=60)
    await store.append_event(
        attempt_id=parent_claim["attempt_id"],
        lease_generation=parent_claim["lease_generation"],
        event_type="message",
        payload={"text": "implemented the fix"},
    )
    await store.save_artifact(
        attempt_id=parent_claim["attempt_id"],
        lease_generation=parent_claim["lease_generation"],
        kind="patch",
        content="diff --git a/x b/x\n",
    )
    await store.transition(
        job_id=parent["id"],
        attempt_id=parent_claim["attempt_id"],
        lease_generation=parent_claim["lease_generation"],
        from_states=("running",),
        to_state="succeeded",
    )

    assert (await store.get_job(child["id"]))["state"] == "waiting"
    assert (await store.claim_for_publish())["job_id"] == parent["id"]
    assert await store.record_publish(
        job_id=parent["id"], pr_url="https://x/pr/1", commit_sha="a" * 40
    )
    child_after_publish = await store.get_job(child["id"])
    assert child_after_publish["state"] == "queued"
    assert child_after_publish["base_sha"] == "a" * 40
    context = await store.follow_up_context(job_id=child["id"])
    assert context == {
        "messages": [
            {"role": "user", "content": "fix the flaky test"},
            {"role": "assistant", "content": "implemented the fix"},
        ],
        "patch": None,
    }
    thread = await store.get_thread_for_job(job_id=child["id"], user_id="user-1")
    assert thread is not None
    assert [job["id"] for job in thread["jobs"]] == [parent["id"], child["id"]]


async def test_initialize_backfills_assistant_messages_from_legacy_events(store: AgentJobStore):
    """Existing job transcripts survive the conversation-table migration."""
    job = await _create_job(store)
    claim = await store.claim_job(worker_id="w1", lease_ttl_seconds=60)
    await store.append_event(
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        event_type="message",
        payload={"text": "legacy answer"},
    )
    async with store._pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM agent_thread_messages WHERE job_id = $1 AND role = 'assistant'", job["id"]
        )

    await store.initialize()

    thread = await store.get_thread_for_job(job_id=job["id"], user_id="user-1")
    assert thread is not None
    assert [(message["role"], message["content"]) for message in thread["messages"]] == [
        ("user", "fix the flaky test"),
        ("assistant", "legacy answer"),
    ]


async def test_concurrent_follow_ups_form_one_linear_thread(store: AgentJobStore):
    """The thread lock serializes simultaneous sends instead of forking."""
    parent = await _create_job(store)
    children = await asyncio.gather(
        store.create_follow_up(
            parent_job_id=parent["id"], user_id="user-1", prompt="first concurrent turn"
        ),
        store.create_follow_up(
            parent_job_id=parent["id"], user_id="user-1", prompt="second concurrent turn"
        ),
    )
    ordered = sorted(
        (child for child in children if child is not None), key=lambda job: job["turn_no"]
    )
    assert [job["turn_no"] for job in ordered] == [2, 3]
    assert ordered[0]["parent_job_id"] == parent["id"]
    assert ordered[1]["parent_job_id"] == ordered[0]["id"]
    assert [job["state"] for job in ordered] == ["waiting", "waiting"]


async def test_follow_up_waits_for_successful_patch_to_publish(store: AgentJobStore):
    """Runner success alone is not a usable base for the next turn."""
    parent = await _create_job(store)
    claim = await store.claim_job(worker_id="w1", lease_ttl_seconds=60)
    await store.save_artifact(
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        kind="patch",
        content="diff --git a/x b/x\n",
    )
    await store.transition(
        job_id=parent["id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        from_states=("running",),
        to_state="succeeded",
    )

    child = await store.create_follow_up(
        parent_job_id=parent["id"], user_id="user-1", prompt="continue"
    )
    assert child is not None
    assert child["state"] == "waiting"


async def test_follow_up_created_after_publish_uses_published_commit(store: AgentJobStore):
    """A later send starts from the branch tip even when no child was waiting."""
    parent = await _create_job(store)
    claim = await store.claim_job(worker_id="w1", lease_ttl_seconds=60)
    await store.save_artifact(
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        kind="patch",
        content="diff --git a/x b/x\n",
    )
    await store.transition(
        job_id=parent["id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        from_states=("running",),
        to_state="succeeded",
    )
    assert (await store.claim_for_publish())["job_id"] == parent["id"]
    published_sha = "b" * 40
    assert await store.record_publish(
        job_id=parent["id"], pr_url="https://x/pr/2", commit_sha=published_sha
    )

    child = await store.create_follow_up(
        parent_job_id=parent["id"], user_id="user-1", prompt="continue from the PR"
    )
    assert child is not None
    assert child["state"] == "queued"
    assert child["base_sha"] == published_sha


async def test_legacy_publish_branch_tip_releases_follow_up(store: AgentJobStore):
    """A pre-thread PR can be resumed from its existing branch head."""
    parent = await _create_job(store)
    claim = await store.claim_job(worker_id="w1", lease_ttl_seconds=60)
    await store.save_artifact(
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        kind="patch",
        content="diff --git a/x b/x\n",
    )
    await store.transition(
        job_id=parent["id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        from_states=("running",),
        to_state="succeeded",
    )
    assert (await store.claim_for_publish())["job_id"] == parent["id"]
    assert await store.record_publish(
        job_id=parent["id"], pr_url="https://x/pr/legacy", commit_sha=None
    )
    child = await store.create_follow_up(
        parent_job_id=parent["id"], user_id="user-1", prompt="resume the old PR"
    )
    assert child is not None
    assert child["state"] == "waiting"

    branch_tip = "c" * 40
    assert await store.resolve_legacy_published_commit(
        parent_job_id=parent["id"], commit_sha=branch_tip
    )
    assert (await store.get_job(parent["id"]))["published_commit_sha"] == branch_tip
    resumed = await store.get_job(child["id"])
    assert resumed["state"] == "queued"
    assert resumed["base_sha"] == branch_tip
    assert (await store.follow_up_context(job_id=child["id"]))["patch"] is None


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
    """A finished job is published at most once, by the platform publisher.

    Driven through the path that actually runs. The worker-side transitions
    this used to exercise were removed: nothing called them, and they let a
    sandbox runner's token write the terminal publish record from an arbitrary
    string.
    """
    job = await _create_job(store)
    claim = await store.claim_job(worker_id="w1", lease_ttl_seconds=60)
    await store.save_artifact(
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        kind="patch",
        content="diff --git a/x b/x\n",
    )
    await store.transition(
        job_id=job["id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        from_states=("running",),
        to_state="succeeded",
    )

    taken = await store.claim_for_publish()
    assert taken is not None and taken["job_id"] == job["id"]
    # A second publisher finds nothing: the claim moved it out of reach.
    assert await store.claim_for_publish() is None

    assert await store.record_publish(job_id=job["id"], pr_url="https://x/pr/1") is True
    # And the URL is written exactly once, so a racing publisher cannot
    # overwrite it with a second PR.
    assert await store.record_publish(job_id=job["id"], pr_url="https://x/pr/2") is False

    fetched = await store.get_job(job["id"])
    assert fetched["state"] == "succeeded"
    assert fetched["published_pr_url"] == "https://x/pr/1"


async def test_a_job_left_publishing_is_failed_not_republished(store: AgentJobStore):
    """A publish that never completes ends as failed, never as a second push.

    The branch or PR may already exist by the time anyone notices, so the rule
    is the same wherever this is detected: fail it for a human, do not retry.
    """
    job = await _create_job(store)
    claim = await store.claim_job(worker_id="w1", lease_ttl_seconds=60)
    await store.save_artifact(
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        kind="patch",
        content="diff --git a/x b/x\n",
    )
    await store.transition(
        job_id=job["id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        from_states=("running",),
        to_state="succeeded",
    )
    await store.claim_for_publish()

    assert await store.reap_stalled_publishes(stall_seconds=0) == [job["id"]]

    fetched = await store.get_job(job["id"])
    assert fetched["state"] == "failed"
    assert fetched["published_pr_url"] is None


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


async def test_model_credential_carries_the_owner_role_and_dies_with_the_account(
    store: AgentJobStore,
):
    """Model calls run at the owner's role, and stop when the account does.

    The role decides which models the sandbox can call — it must be the
    owner's, or the composer offers models whose first call 404s. The status
    check is the other half: a suspended owner's running job must stop buying
    inference without waiting for the reaper.
    """
    async with store._pool.acquire() as conn:
        # Mirror the production schema's constrained columns; everything else
        # is nullable or defaulted. IF NOT EXISTS keeps this compatible with a
        # test database where the full auth schema already exists.
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                email TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'free',
                status TEXT DEFAULT 'active'
            )
            """
        )
        await conn.execute(
            """
            INSERT INTO users (id, email, password_hash, role, status)
            VALUES ('user-1', 'agent-owner-role@test.invalid', 'x', 'internal', 'active')
            ON CONFLICT (id) DO UPDATE SET role = 'internal', status = 'active'
            """
        )
    try:
        job = await _create_job(store)
        claim = await store.claim_job(worker_id="w1", lease_ttl_seconds=60)
        fence = {
            "job_id": job["id"],
            "attempt_id": claim["attempt_id"],
            "lease_generation": claim["lease_generation"],
        }

        identity = await store.resolve_model_credential(**fence)
        assert identity is not None
        assert identity["role"] == "internal"

        async with store._pool.acquire() as conn:
            await conn.execute("UPDATE users SET status = 'suspended' WHERE id = 'user-1'")
        assert await store.resolve_model_credential(**fence) is None
    finally:
        async with store._pool.acquire() as conn:
            await conn.execute("DELETE FROM users WHERE id = 'user-1'")


async def test_publish_claim_is_exactly_once(store: AgentJobStore):
    """Two publishers cannot both take the same finished job.

    The publish step has an externally visible side effect, so 'at most once'
    has to hold at the database level rather than by convention.
    """
    job = await _create_job(store)
    claim = await store.claim_job(worker_id="w1", lease_ttl_seconds=60)
    fence = {
        "attempt_id": claim["attempt_id"],
        "lease_generation": claim["lease_generation"],
    }
    await store.save_artifact(**fence, kind="patch", content="diff --git a/x b/x\n")
    await store.transition(
        job_id=job["id"], **fence, from_states=("running",), to_state="succeeded"
    )

    first = await store.claim_for_publish()
    assert first is not None
    assert first["job_id"] == job["id"]
    assert first["patch"].startswith("diff --git")

    # Already claimed (now `publishing`), so a second publisher finds nothing.
    assert await store.claim_for_publish() is None

    assert await store.record_publish(job_id=job["id"], pr_url="https://x/pr/1") is True
    # Recording twice is refused, so a retry cannot open a second PR.
    assert await store.record_publish(job_id=job["id"], pr_url="https://x/pr/2") is False

    fetched = await store.get_job(job["id"])
    assert fetched["state"] == "succeeded"
    assert fetched["published_pr_url"] == "https://x/pr/1"


async def test_jobs_without_a_patch_are_not_published(store: AgentJobStore):
    """A job that changed nothing must never produce an empty PR."""
    job = await _create_job(store)
    claim = await store.claim_job(worker_id="w1", lease_ttl_seconds=60)
    await store.transition(
        job_id=job["id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        from_states=("running",),
        to_state="succeeded",
    )
    assert await store.claim_for_publish() is None


async def test_failed_publish_surfaces_the_reason(store: AgentJobStore):
    """A rejected patch fails the job with a reason the owner can read."""
    job = await _create_job(store)
    claim = await store.claim_job(worker_id="w1", lease_ttl_seconds=60)
    fence = {
        "attempt_id": claim["attempt_id"],
        "lease_generation": claim["lease_generation"],
    }
    await store.save_artifact(**fence, kind="patch", content="diff --git a/x b/x\n")
    await store.transition(
        job_id=job["id"], **fence, from_states=("running",), to_state="succeeded"
    )
    await store.claim_for_publish()

    await store.fail_publish(job_id=job["id"], detail="patch rejected: modifies .github/")
    fetched = await store.get_job(job["id"])
    assert fetched["state"] == "failed"
    assert ".github/" in fetched["detail"]

    events = await store.list_events_after(job_id=job["id"], after_id=0)
    assert events[-1]["event_type"] == "error"
    assert events[-1]["payload"]["phase"] == "publish_rejected"


async def test_an_expired_lease_is_dead_before_the_reaper_runs(store: AgentJobStore):
    """Expiry must take effect immediately, not when the reaper next passes.

    Previously every fenced write checked only generation and status, so a
    stalled worker kept full authority for up to a reaper interval after its
    lease ran out — it could renew, write events, store artifacts and finish
    the job. The reaper is a cleanup mechanism, not the thing that revokes.
    """
    job = await _create_job(store)
    claim = await store.claim_job(worker_id="w1", lease_ttl_seconds=60)
    fence = {
        "attempt_id": claim["attempt_id"],
        "lease_generation": claim["lease_generation"],
    }
    # Everything works while the lease is live.
    assert await store.append_event(**fence, event_type="message", payload={}) is not None

    await _expire_attempt(store, claim["attempt_id"])
    # Deliberately do NOT run the reaper.

    assert (await store.heartbeat(**fence, lease_ttl_seconds=60))["ok"] is False
    assert await store.append_event(**fence, event_type="message", payload={}) is None
    assert await store.save_artifact(**fence, kind="patch", content="x") is None
    assert (
        await store.transition(
            job_id=job["id"], **fence, from_states=("running",), to_state="succeeded"
        )
        is False
    )
    # And the job cannot be moved into publishing either.
    assert (
        await store.transition(
            job_id=job["id"], **fence, from_states=("running",), to_state="publishing"
        )
        is False
    )

    # And the job is untouched: still running, no artifact, no terminal state.
    fetched = await store.get_job(job["id"])
    assert fetched["state"] == "running"
    assert await store.get_artifact(job_id=job["id"], kind="patch") is None


async def test_a_released_claim_costs_no_retry(store: AgentJobStore):
    """A claim the platform gave up on must not spend the job's retry budget.

    The regression this pins: the budget was read off ``attempt_no``, which
    numbers every claim. A GitHub outage lasting across `max_attempts` claim
    cycles would therefore fail every queued private-repo job outright, without
    an agent ever having started — while the whole point of releasing the claim
    is that the job never got its turn.
    """
    job = await _create_job(store)

    # Three claims the platform abandons before the agent starts.
    for _ in range(3):
        claim = await store.claim_job(worker_id="w1", lease_ttl_seconds=60)
        assert claim is not None, "a released job must be claimable again"
        assert await store.release_claim(job_id=job["id"], attempt_id=claim["attempt_id"])
        assert (await store.get_job(job["id"]))["state"] == "queued"

    # The budget is untouched: a real attempt still gets to run and be reaped.
    claim = await store.claim_job(worker_id="w1", lease_ttl_seconds=60)
    await _expire_attempt(store, claim["attempt_id"])
    actions = await store.reap_expired(max_attempts=3)

    assert actions[0]["action"] == "queued", "aborted claims must not count as attempts"
    assert (await store.get_job(job["id"]))["state"] == "queued"


async def test_releasing_records_why_rather_than_erasing_the_attempt(store: AgentJobStore):
    """History stays append-only: the abandoned attempt is marked, not deleted."""
    job = await _create_job(store)
    claim = await store.claim_job(worker_id="w1", lease_ttl_seconds=60)

    await store.release_claim(job_id=job["id"], attempt_id=claim["attempt_id"])

    events = await store.list_events_after(job_id=job["id"])
    aborted = [e for e in events if e["event_type"] == "attempt_aborted"]
    assert aborted, "an operator must be able to see the platform dropped this one"
    assert aborted[0]["attempt_id"] == claim["attempt_id"]


async def test_releasing_a_job_that_moved_on_is_a_no_op(store: AgentJobStore):
    """A late release must never drag a running job back to the queue."""
    job = await _create_job(store)
    first = await store.claim_job(worker_id="w1", lease_ttl_seconds=60)
    await store.release_claim(job_id=job["id"], attempt_id=first["attempt_id"])
    second = await store.claim_job(worker_id="w2", lease_ttl_seconds=60)

    # The first attempt is already finished, so releasing it again changes nothing.
    assert await store.release_claim(job_id=job["id"], attempt_id=first["attempt_id"]) is False

    fetched = await store.get_job(job["id"])
    assert fetched["state"] == "running"
    assert fetched["current_attempt_id"] == second["attempt_id"]


async def test_a_publish_the_publisher_abandoned_is_swept(store: AgentJobStore):
    """A job stuck in `publishing` must not stay there forever.

    `claim_for_publish` moves the job in its own transaction; if the publisher
    then dies, nothing else touches that row. The attempt reaper cannot help —
    it scans *running attempts*, and this job's attempt finished before
    publishing began — so without this sweep the job is invisible to its owner
    and to the publish queue indefinitely.
    """
    job = await _create_job(store)
    claim = await store.claim_job(worker_id="w1", lease_ttl_seconds=60)
    fence = {
        "job_id": job["id"],
        "attempt_id": claim["attempt_id"],
        "lease_generation": claim["lease_generation"],
    }
    await store.save_artifact(
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        kind="patch",
        content="diff --git a/x b/x\n",
    )
    await store.transition(from_states=("running",), to_state="succeeded", **fence)

    taken = await store.claim_for_publish()
    assert taken is not None
    assert (await store.get_job(job["id"]))["state"] == "publishing"

    # Nothing has stalled yet, so a sweep must leave it alone.
    assert await store.reap_stalled_publishes(stall_seconds=3600) == []
    assert (await store.get_job(job["id"]))["state"] == "publishing"

    # Past the deadline it is failed, not retried: the branch or PR may already
    # exist and a second automatic publish must never happen.
    assert await store.reap_stalled_publishes(stall_seconds=0) == [job["id"]]
    fetched = await store.get_job(job["id"])
    assert fetched["state"] == "failed"
    assert "manual review" in fetched["detail"]


async def test_the_sweep_leaves_a_published_job_alone(store: AgentJobStore):
    """Only jobs with no PR recorded are swept."""
    job = await _create_job(store)
    claim = await store.claim_job(worker_id="w1", lease_ttl_seconds=60)
    await store.save_artifact(
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        kind="patch",
        content="diff --git a/x b/x\n",
    )
    await store.transition(
        job_id=job["id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        from_states=("running",),
        to_state="succeeded",
    )
    await store.claim_for_publish()
    await store.record_publish(job_id=job["id"], pr_url="https://github.com/o/n/pull/1")

    assert await store.reap_stalled_publishes(stall_seconds=0) == []
    assert (await store.get_job(job["id"]))["state"] == "succeeded"


async def test_releasing_with_the_wrong_generation_is_refused(store: AgentJobStore):
    """The weakest fence in the store was this one; it now matches the others.

    Releasing a claim returns the job to the queue, so an unfenced release is a
    way to yank a job out from under the attempt that legitimately holds it.
    """
    job = await _create_job(store)
    claim = await store.claim_job(worker_id="w1", lease_ttl_seconds=60)

    assert (
        await store.release_claim(
            job_id=job["id"], attempt_id=claim["attempt_id"], lease_generation=999
        )
        is False
    )
    assert (await store.get_job(job["id"]))["state"] == "running"

    assert (
        await store.release_claim(
            job_id=job["id"],
            attempt_id=claim["attempt_id"],
            lease_generation=claim["lease_generation"],
        )
        is True
    )


async def test_a_release_writes_its_event_into_the_right_job(store: AgentJobStore):
    """agent_job_events.job_id has no foreign key, so a mismatched pair would land
    a control event in another job's stream — possibly another tenant's."""
    victim = await _create_job(store)
    target = await _create_job(store)
    claim = await store.claim_job(worker_id="w1", lease_ttl_seconds=60)

    # Deliberately name the wrong job alongside the real attempt id.
    wrong = victim["id"] if claim["id"] != victim["id"] else target["id"]
    await store.release_claim(job_id=wrong, attempt_id=claim["attempt_id"])

    stray = await store.list_events_after(job_id=wrong)
    assert not [e for e in stray if e["event_type"] == "attempt_aborted"], (
        "the event must follow the attempt's real job, not the caller's claim"
    )
    owned = await store.list_events_after(job_id=claim["id"])
    assert [e for e in owned if e["event_type"] == "attempt_aborted"]


async def test_a_cancelled_job_released_from_a_claim_ends_cancelled(store: AgentJobStore):
    """Requeueing a cancelled job put it somewhere nothing could ever reach.

    `claim_job` skips queued rows with `cancel_requested`, and the reaper only
    reaches jobs that still have a *running* attempt — which a released one
    does not. So the job sat in `queued` permanently, invisible to its owner's
    cancellation and to every worker.
    """
    job = await _create_job(store)
    claim = await store.claim_job(worker_id="w1", lease_ttl_seconds=60)
    assert await store.request_cancel(job_id=job["id"]) == "running"

    await store.release_claim(
        job_id=job["id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
    )

    fetched = await store.get_job(job["id"])
    assert fetched["state"] == "cancelled", "a cancelled job must not be requeued"


async def test_an_uncancelled_job_still_returns_to_the_queue(store: AgentJobStore):
    """The ordinary release path is unchanged."""
    job = await _create_job(store)
    claim = await store.claim_job(worker_id="w1", lease_ttl_seconds=60)

    await store.release_claim(
        job_id=job["id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
    )

    assert (await store.get_job(job["id"]))["state"] == "queued"
    assert await store.claim_job(worker_id="w2", lease_ttl_seconds=60) is not None
