"""API-surface tests for the agent-sandbox job router.

Runs against a minimal in-memory stand-in for ``AgentJobStore`` so the default
(no-database) suite covers the HTTP contract: owner scoping, capability-token
authentication, the fenced-write → 409 mapping, and SSE framing. The store's
own concurrency semantics are pinned separately by the ``dbtest`` suite.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from serving.agent_jobs.tokens import mint_worker_token
from serving.servers.deps import get_agent_job_store
from serving.servers.routers import agent_jobs as agent_jobs_router

pytestmark = pytest.mark.asyncio

_OWNER = "user-owner"
_OTHER = "user-other"


class FakeAgentJobStore:
    """In-memory stand-in exposing the AgentJobStore surface the router uses."""

    def __init__(self) -> None:
        self.jobs: dict[str, dict[str, Any]] = {}
        self.events: list[dict[str, Any]] = []
        self.artifacts: dict[tuple[str, str], dict[str, Any]] = {}
        self.live_fence: tuple[int, int] | None = None
        self.released: list[tuple[str, int]] = []
        self._next_event_id = 1
        self._next_job = 1

    # -- owner surface --
    async def create_job(self, **kwargs: Any) -> dict[str, Any]:
        job_id = f"ajob_{self._next_job:04d}"
        self._next_job += 1
        job = {
            "id": job_id,
            "state": "queued",
            "cancel_requested": False,
            "current_attempt_id": None,
            "published_pr_url": None,
            "detail": None,
            "created_at": None,
            "updated_at": None,
            **kwargs,
        }
        job.setdefault("base_sha", None)
        job.setdefault("metadata", None)
        self.jobs[job_id] = job
        return job

    async def get_job(self, job_id: str) -> dict[str, Any] | None:
        return self.jobs.get(job_id)

    async def list_jobs(self, *, user_id: str, limit: int = 50) -> list[dict[str, Any]]:
        return [job for job in self.jobs.values() if job["user_id"] == user_id][:limit]

    async def request_cancel(self, *, job_id: str, user_id: str | None = None) -> str | None:
        job = self.jobs.get(job_id)
        if job is None or (user_id is not None and job["user_id"] != user_id):
            return None
        job["cancel_requested"] = True
        if job["state"] == "queued":
            job["state"] = "cancelled"
        return job["state"]

    async def list_events_after(
        self, *, job_id: str, after_id: int = 0, limit: int = 500
    ) -> list[dict[str, Any]]:
        return [
            event for event in self.events if event["job_id"] == job_id and event["id"] > after_id
        ][:limit]

    async def get_artifact(self, *, job_id: str, kind: str) -> dict[str, Any] | None:
        return self.artifacts.get((job_id, kind))

    # -- worker surface (fenced) --
    def _fenced(self, attempt_id: int, lease_generation: int) -> bool:
        return self.live_fence == (attempt_id, lease_generation)

    async def claim_job(self, *, worker_id: str, lease_ttl_seconds: float) -> dict[str, Any] | None:
        queued = [job for job in self.jobs.values() if job["state"] == "queued"]
        if not queued:
            return None
        job = queued[0]
        job["state"] = "running"
        job["current_attempt_id"] = 100
        self.live_fence = (100, 1)
        return {**job, "attempt_id": 100, "attempt_no": 1, "lease_generation": 1}

    async def heartbeat(
        self, *, attempt_id: int, lease_generation: int, lease_ttl_seconds: float
    ) -> dict[str, Any]:
        if not self._fenced(attempt_id, lease_generation):
            return {"ok": False}
        job = next(job for job in self.jobs.values() if job["current_attempt_id"] == attempt_id)
        return {
            "ok": True,
            "job_id": job["id"],
            "state": job["state"],
            "cancel_requested": job["cancel_requested"],
        }

    async def append_event(
        self,
        *,
        attempt_id: int,
        lease_generation: int,
        event_type: str,
        payload: dict[str, Any] | None = None,
    ) -> int | None:
        if not self._fenced(attempt_id, lease_generation):
            return None
        job = next(job for job in self.jobs.values() if job["current_attempt_id"] == attempt_id)
        event = {
            "id": self._next_event_id,
            "job_id": job["id"],
            "attempt_id": attempt_id,
            "seq": len(self.events) + 1,
            "event_type": event_type,
            "payload": payload,
            "created_at": None,
        }
        self._next_event_id += 1
        self.events.append(event)
        return event["id"]

    async def save_artifact(
        self, *, attempt_id: int, lease_generation: int, kind: str, content: str
    ) -> int | None:
        if not self._fenced(attempt_id, lease_generation):
            return None
        job = next(job for job in self.jobs.values() if job["current_attempt_id"] == attempt_id)
        self.artifacts[(job["id"], kind)] = {
            "job_id": job["id"],
            "attempt_id": attempt_id,
            "kind": kind,
            "content": content,
            "created_at": None,
        }
        return 1

    async def transition(
        self,
        *,
        job_id: str,
        attempt_id: int,
        lease_generation: int,
        from_states: tuple[str, ...],
        to_state: str,
        detail: str | None = None,
        base_sha: str | None = None,
    ) -> bool:
        job = self.jobs.get(job_id)
        if job is None or not self._fenced(attempt_id, lease_generation):
            return False
        if job["state"] not in from_states:
            return False
        job["state"] = to_state
        job["detail"] = detail
        # Mirrors the store's COALESCE: a worker may fill in a base the job
        # lacked, never overwrite one the owner pinned.
        if base_sha and not job.get("base_sha"):
            job["base_sha"] = base_sha
        return True

    async def release_claim(self, *, job_id: str, attempt_id: int) -> bool:
        job = self.jobs.get(job_id)
        if job is None or job.get("current_attempt_id") != attempt_id:
            return False
        self.released.append((job_id, attempt_id))
        job["state"] = "queued"
        job["current_attempt_id"] = None
        self.live_fence = None
        return True

    async def begin_publish(self, **kwargs: Any) -> bool:
        return await self.transition(from_states=("running",), to_state="publishing", **kwargs)

    async def complete_publish(self, *, pr_url: str, **kwargs: Any) -> bool:
        ok = await self.transition(from_states=("publishing",), to_state="succeeded", **kwargs)
        if ok:
            self.jobs[kwargs["job_id"]]["published_pr_url"] = pr_url
        return ok


@pytest.fixture(autouse=True)
def _api_key_secret(monkeypatch):
    """Provide the signing secret for worker tokens and an entitled repo.

    The repo allowlist is deliberately empty by default (a deployment that has
    not been configured must refuse every job), so these tests declare the one
    repository they use.
    """
    monkeypatch.setenv("API_KEY_SECRET", "api-test-secret")
    monkeypatch.setenv("AGENT_REPO_ALLOWLIST", "owner/name,o/n,private/repo")
    from serving.config.settings import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture()
def store() -> FakeAgentJobStore:
    """Provide a fresh in-memory store."""
    return FakeAgentJobStore()


def _build_app(store, *, role: str = "internal", dispatcher: bool = True):
    """Build an app with the router mounted and auth stubbed to one identity.

    ``dispatcher`` controls whether the machine-to-machine gate on
    ``/worker/claim`` is satisfied, so a test can assert that an ordinary
    caller is turned away there.
    """
    from serving.servers.auth import verify_api_key
    from serving.servers.deps import (
        get_agent_app_credentials,
        get_log_store,
        get_operational_store,
        verify_admin_access,
    )

    app = FastAPI()
    app.include_router(agent_jobs_router.router)
    app.dependency_overrides[get_agent_job_store] = lambda: store
    # verify_admin_access resolves an operational store; the bare test app has
    # no app.state.services, so supply it even when the gate is left real.
    app.dependency_overrides[get_operational_store] = lambda: None
    # No GitHub App by default — the configuration most deployments start in,
    # and the one where a public repository still clones fine.
    app.dependency_overrides[get_agent_app_credentials] = lambda: None
    # No log store: spend and usage are then absent from the response rather
    # than reported as a fabricated zero.
    app.dependency_overrides[get_log_store] = lambda: None
    identity = {"user_id": _OWNER, "role": role, "authenticated": True}
    app.dependency_overrides[verify_api_key] = lambda: identity
    if dispatcher:
        app.dependency_overrides[verify_admin_access] = lambda: "dispatcher@test"
    return app


@pytest_asyncio.fixture()
async def client(store: FakeAgentJobStore):
    """Mount only the agent-jobs router with auth stubbed to a fixed owner."""
    app = _build_app(store)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as async_client:
        yield async_client


async def _create_job(client: AsyncClient) -> str:
    """Create a job through the API and return its id."""
    response = await client.post(
        "/v1/agent/jobs",
        json={"repo": "owner/name", "task_prompt": "fix it", "model": "glm-5.1"},
    )
    assert response.status_code == 201
    return response.json()["id"]


async def test_create_get_list_round_trip(client: AsyncClient):
    """A created job is retrievable and listed for its owner."""
    job_id = await _create_job(client)

    got = await client.get(f"/v1/agent/jobs/{job_id}")
    assert got.status_code == 200
    assert got.json()["state"] == "queued"
    assert got.json()["runtime"] == "claude-code"

    listed = await client.get("/v1/agent/jobs")
    assert [job["id"] for job in listed.json()["jobs"]] == [job_id]


async def test_other_users_jobs_are_404_not_403(client: AsyncClient, store: FakeAgentJobStore):
    """Someone else's job is indistinguishable from a missing one."""
    foreign = await store.create_job(
        user_id=_OTHER,
        repo="owner/other",
        task_prompt="not yours",
        runtime="claude-code",
        model="glm-5.1",
    )
    for path in (
        f"/v1/agent/jobs/{foreign['id']}",
        f"/v1/agent/jobs/{foreign['id']}/events",
        f"/v1/agent/jobs/{foreign['id']}/artifacts/patch",
    ):
        response = await client.get(path)
        assert response.status_code == 404, path
    cancel = await client.post(f"/v1/agent/jobs/{foreign['id']}/cancel")
    assert cancel.status_code == 404


async def test_cancel_queued_job(client: AsyncClient):
    """Cancelling a queued job reports the terminal state immediately."""
    job_id = await _create_job(client)
    response = await client.post(f"/v1/agent/jobs/{job_id}/cancel")
    assert response.status_code == 200
    assert response.json()["state"] == "cancelled"
    assert response.json()["cancel_requested"] is True


async def test_worker_flow_claim_event_artifact_finish(
    client: AsyncClient, store: FakeAgentJobStore
):
    """The worker path: claim → token → event → artifact → finish."""
    job_id = await _create_job(client)

    claim = await client.post("/v1/agent/worker/claim", json={"worker_id": "w1"})
    assert claim.status_code == 200
    body = claim.json()
    assert body["job_id"] == job_id
    token = body["worker_token"]
    auth = {"Authorization": f"Bearer {token}"}

    beat = await client.post(f"/v1/agent/worker/jobs/{job_id}/heartbeat", json={}, headers=auth)
    assert beat.status_code == 200
    assert beat.json()["cancel_requested"] is False

    event = await client.post(
        f"/v1/agent/worker/jobs/{job_id}/events",
        json={"event_type": "message", "payload": {"text": "hello"}},
        headers=auth,
    )
    assert event.status_code == 201
    assert event.json()["event_id"] == 1

    artifact = await client.post(
        f"/v1/agent/worker/jobs/{job_id}/artifacts",
        json={"kind": "patch", "content": "diff --git a b"},
        headers=auth,
    )
    assert artifact.status_code == 201

    # The owner can read the events and the artifact back.
    events = await client.get(f"/v1/agent/jobs/{job_id}/events")
    assert [event["event_type"] for event in events.json()["events"]] == ["message"]
    assert events.json()["next_cursor"] == 1
    patch = await client.get(f"/v1/agent/jobs/{job_id}/artifacts/patch")
    assert patch.json()["content"] == "diff --git a b"

    finish = await client.post(
        f"/v1/agent/worker/jobs/{job_id}/finish",
        json={"state": "succeeded"},
        headers=auth,
    )
    assert finish.status_code == 200
    assert store.jobs[job_id]["state"] == "succeeded"


async def test_empty_queue_claim_returns_null(client: AsyncClient):
    """Claiming with nothing queued is a 200 with a null body, not an error."""
    response = await client.post("/v1/agent/worker/claim", json={"worker_id": "w1"})
    assert response.status_code == 200
    assert response.json() is None


async def test_lost_lease_maps_to_409_on_every_write_path(
    client: AsyncClient, store: FakeAgentJobStore
):
    """Once the fence moves on, every worker write returns 409."""
    job_id = await _create_job(client)
    claim = await client.post("/v1/agent/worker/claim", json={"worker_id": "w1"})
    auth = {"Authorization": f"Bearer {claim.json()['worker_token']}"}

    # The reaper hands ownership to a new attempt/generation.
    store.live_fence = (101, 2)

    calls = [
        ("post", f"/v1/agent/worker/jobs/{job_id}/heartbeat", {}),
        ("post", f"/v1/agent/worker/jobs/{job_id}/events", {"event_type": "message"}),
        ("post", f"/v1/agent/worker/jobs/{job_id}/artifacts", {"kind": "p", "content": "c"}),
        ("post", f"/v1/agent/worker/jobs/{job_id}/finish", {"state": "succeeded"}),
        ("post", f"/v1/agent/worker/jobs/{job_id}/publish/begin", None),
        ("post", f"/v1/agent/worker/jobs/{job_id}/publish/complete", {"pr_url": "http://x"}),
    ]
    for method, path, payload in calls:
        response = await getattr(client, method)(
            path, json=payload if payload is not None else {}, headers=auth
        )
        assert response.status_code == 409, path
        assert response.json()["detail"]["error"]["type"] == "lease_lost"


async def test_worker_token_is_required_and_scoped(client: AsyncClient):
    """Missing, malformed, and cross-job tokens are all rejected."""
    job_id = await _create_job(client)
    body = {"event_type": "message"}

    missing = await client.post(f"/v1/agent/worker/jobs/{job_id}/events", json=body)
    assert missing.status_code == 401

    bad = await client.post(
        f"/v1/agent/worker/jobs/{job_id}/events",
        json=body,
        headers={"Authorization": "Bearer not-a-token"},
    )
    assert bad.status_code == 401

    # A valid token minted for another job must not work here.
    foreign_token = mint_worker_token(job_id="ajob_other", attempt_id=1, lease_generation=1)
    wrong_job = await client.post(
        f"/v1/agent/worker/jobs/{job_id}/events",
        json=body,
        headers={"Authorization": f"Bearer {foreign_token}"},
    )
    assert wrong_job.status_code == 409


async def test_publish_two_phase(client: AsyncClient, store: FakeAgentJobStore):
    """begin → complete records the PR URL and finishes the job."""
    job_id = await _create_job(client)
    claim = await client.post("/v1/agent/worker/claim", json={"worker_id": "w1"})
    auth = {"Authorization": f"Bearer {claim.json()['worker_token']}"}

    begin = await client.post(f"/v1/agent/worker/jobs/{job_id}/publish/begin", headers=auth)
    assert begin.status_code == 200
    complete = await client.post(
        f"/v1/agent/worker/jobs/{job_id}/publish/complete",
        json={"pr_url": "https://github.com/o/n/pull/1"},
        headers=auth,
    )
    assert complete.status_code == 200
    assert store.jobs[job_id]["state"] == "succeeded"
    assert store.jobs[job_id]["published_pr_url"] == "https://github.com/o/n/pull/1"


async def test_finish_rejects_non_terminal_state(client: AsyncClient):
    """A worker cannot 'finish' into a non-terminal state."""
    job_id = await _create_job(client)
    claim = await client.post("/v1/agent/worker/claim", json={"worker_id": "w1"})
    auth = {"Authorization": f"Bearer {claim.json()['worker_token']}"}
    response = await client.post(
        f"/v1/agent/worker/jobs/{job_id}/finish", json={"state": "running"}, headers=auth
    )
    assert response.status_code == 400


async def test_sse_stream_replays_and_closes_on_terminal_state(
    client: AsyncClient, store: FakeAgentJobStore
):
    """The SSE stream emits id-tagged frames and ends with job_finished."""
    job_id = await _create_job(client)
    claim = await client.post("/v1/agent/worker/claim", json={"worker_id": "w1"})
    auth = {"Authorization": f"Bearer {claim.json()['worker_token']}"}
    await client.post(
        f"/v1/agent/worker/jobs/{job_id}/events",
        json={"event_type": "message", "payload": {"text": "one"}},
        headers=auth,
    )
    await client.post(
        f"/v1/agent/worker/jobs/{job_id}/finish", json={"state": "succeeded"}, headers=auth
    )

    async with client.stream("GET", f"/v1/agent/jobs/{job_id}/stream") as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        body = "".join([chunk async for chunk in response.aiter_text()])

    assert "id: 1" in body
    assert "event: message" in body
    assert "event: job_finished" in body
    payload = json.loads(body.split("event: job_finished\ndata: ")[1].split("\n")[0])
    assert payload["state"] == "succeeded"


async def test_sse_resumes_from_last_event_id(client: AsyncClient, store: FakeAgentJobStore):
    """Last-Event-ID skips already-delivered events."""
    job_id = await _create_job(client)
    claim = await client.post("/v1/agent/worker/claim", json={"worker_id": "w1"})
    auth = {"Authorization": f"Bearer {claim.json()['worker_token']}"}
    for text in ("one", "two"):
        await client.post(
            f"/v1/agent/worker/jobs/{job_id}/events",
            json={"event_type": "message", "payload": {"text": text}},
            headers=auth,
        )
    await client.post(
        f"/v1/agent/worker/jobs/{job_id}/finish", json={"state": "succeeded"}, headers=auth
    )

    async with client.stream(
        "GET", f"/v1/agent/jobs/{job_id}/stream", headers={"Last-Event-ID": "1"}
    ) as response:
        body = "".join([chunk async for chunk in response.aiter_text()])

    assert '"text":"one"' not in body
    assert '"text":"two"' in body


async def test_missing_store_returns_503(store: FakeAgentJobStore):
    """Without a database the agent API reports 503 rather than crashing."""
    app = _build_app(None)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/v1/agent/jobs")
    assert response.status_code == 503


async def test_claim_requires_the_dispatcher_credential(store: FakeAgentJobStore):
    """An ordinary customer cannot dequeue and read another tenant's job.

    Regression: claim_job takes the oldest queued job across all tenants and
    the response carries that job's repo, prompt, metadata, and a working
    capability token — so plain API-key auth here was a cross-tenant leak.
    """
    await store.create_job(
        user_id="somebody-else",
        repo="private/repo",  # entitled below; the point here is the auth gate
        task_prompt="confidential task",
        runtime="claude-code",
        model="glm-5.1",
    )

    app = _build_app(store, role="pro", dispatcher=False)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        denied = await client.post("/v1/agent/worker/claim", json={"worker_id": "w1"})
    assert denied.status_code in (401, 403)
    assert store.jobs[next(iter(store.jobs))]["state"] == "queued"

    app = _build_app(store, dispatcher=True)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        allowed = await client.post("/v1/agent/worker/claim", json={"worker_id": "w1"})
    assert allowed.status_code == 200
    assert allowed.json()["repo"] == "private/repo"


async def test_worker_cannot_inject_sse_frames_via_event_type(
    client: AsyncClient, store: FakeAgentJobStore
):
    """A newline in event_type must not break out of the SSE event field.

    Regression: event_type was interpolated raw into "event: {type}", so a
    worker could append a crafted type and inject arbitrary frames — including
    a fake job_finished — into the owner's live stream.
    """
    job_id = await _create_job(client)
    claim = await client.post("/v1/agent/worker/claim", json={"worker_id": "w1"})
    auth = {"Authorization": f"Bearer {claim.json()['worker_token']}"}

    rejected = await client.post(
        f"/v1/agent/worker/jobs/{job_id}/events",
        json={"event_type": "message\nevent: job_finished\ndata: {}\n\nx", "payload": {}},
        headers=auth,
    )
    assert rejected.status_code == 422

    # Belt and braces: a row that somehow carries a bad type still renders safely.
    store.events.append(
        {
            "id": 999,
            "job_id": job_id,
            "attempt_id": 100,
            "seq": 1,
            "event_type": "evil\nevent: job_finished\ndata: {}\n",
            "payload": {},
            "created_at": None,
        }
    )
    await client.post(
        f"/v1/agent/worker/jobs/{job_id}/finish", json={"state": "succeeded"}, headers=auth
    )
    async with client.stream("GET", f"/v1/agent/jobs/{job_id}/stream") as response:
        body = "".join([chunk async for chunk in response.aiter_text()])
    assert "event: evil" not in body
    assert "event: malformed" in body
    # The payload still *contains* the crafted text, but only JSON-escaped
    # inside a data field — it never starts a frame. Count frame boundaries,
    # not substrings: exactly one real job_finished frame was emitted.
    assert body.count("\n\nevent: job_finished") == 1
    assert "\nevent: job_finished" not in body.split("data: ", 1)[1].split("\n\n", 1)[0]


async def test_lease_ttl_is_capped_server_side(client: AsyncClient):
    """A worker cannot pick a lease long enough to outlive the reaper.

    Regression: lease_ttl_seconds had no upper bound, so a worker could claim
    with a decade-long lease. The reaper would never reclaim the job, making
    the attempt's capability token neither self-revoking nor cancellable.
    """
    await _create_job(client)
    response = await client.post(
        "/v1/agent/worker/claim",
        json={"worker_id": "w1", "lease_ttl_seconds": 99_999_999},
    )
    assert response.status_code == 422


async def test_terminal_stream_drains_beyond_one_page(
    client: AsyncClient, store: FakeAgentJobStore
):
    """A finished job with multiple pages of backlog delivers every event.

    Regression: the terminal path drained exactly one extra page before
    emitting job_finished, so a client attaching to a finished job with a
    large backlog silently lost everything past the second page.
    """
    job_id = await _create_job(client)
    claim = await client.post("/v1/agent/worker/claim", json={"worker_id": "w1"})
    auth = {"Authorization": f"Bearer {claim.json()['worker_token']}"}

    # More than two pages (page size is 500).
    total = 1100
    for index in range(total):
        await store.append_event(
            attempt_id=100,
            lease_generation=1,
            event_type="message",
            payload={"index": index},
        )
    await client.post(
        f"/v1/agent/worker/jobs/{job_id}/finish", json={"state": "succeeded"}, headers=auth
    )

    async with client.stream("GET", f"/v1/agent/jobs/{job_id}/stream") as response:
        body = "".join([chunk async for chunk in response.aiter_text()])

    assert body.count("event: message\n") == total
    assert f'"index":{total - 1}' in body
    assert "event: job_finished" in body
    tail = json.loads(body.split("event: job_finished\ndata: ")[1].split("\n")[0])
    assert tail["last_event_id"] == total


async def test_claim_returns_a_separate_model_scoped_token(client: AsyncClient):
    """The sandbox credential is distinct from the runner's."""
    await _create_job(client)
    body = (await client.post("/v1/agent/worker/claim", json={"worker_id": "w1"})).json()
    assert body["sandbox_token"]
    assert body["sandbox_token"] != body["worker_token"]

    from serving.agent_jobs.tokens import SCOPE_FULL, SCOPE_MODEL, parse_worker_token

    assert parse_worker_token(body["worker_token"])["scope"] == SCOPE_FULL
    assert parse_worker_token(body["sandbox_token"])["scope"] == SCOPE_MODEL


async def test_sandbox_token_cannot_write_job_state(client: AsyncClient):
    """A credential leaked from inside the sandbox cannot poison the job.

    This is the payoff of running the runner outside the sandbox: the only
    credential the agent can reach buys model calls, not event-log writes,
    artifact overwrites, or terminal transitions.
    """
    job_id = await _create_job(client)
    body = (await client.post("/v1/agent/worker/claim", json={"worker_id": "w1"})).json()
    sandbox_auth = {"Authorization": f"Bearer {body['sandbox_token']}"}

    for path, payload in (
        (f"/v1/agent/worker/jobs/{job_id}/events", {"event_type": "message"}),
        (f"/v1/agent/worker/jobs/{job_id}/artifacts", {"kind": "patch", "content": "evil"}),
        (f"/v1/agent/worker/jobs/{job_id}/finish", {"state": "succeeded"}),
        (f"/v1/agent/worker/jobs/{job_id}/heartbeat", {}),
    ):
        response = await client.post(path, json=payload, headers=sandbox_auth)
        assert response.status_code == 403, path
        assert response.json()["detail"]["error"]["type"] == "insufficient_scope"

    # The runner's own token still works.
    runner_auth = {"Authorization": f"Bearer {body['worker_token']}"}
    ok = await client.post(
        f"/v1/agent/worker/jobs/{job_id}/events",
        json={"event_type": "message", "payload": {}},
        headers=runner_auth,
    )
    assert ok.status_code == 201


async def test_sandbox_token_is_refused_on_owner_routes(client: AsyncClient):
    """A model-scoped credential must not reach the control plane.

    verify_api_key is shared with /v1/agent/jobs, so resolving a sandbox token
    there as its owner would let the sandbox enumerate, cancel, or create that
    owner's other jobs — the authority the model scope exists to withhold.
    """
    from serving.servers.auth import _is_inference_path

    class _Req:
        def __init__(self, path: str) -> None:
            from urllib.parse import urlparse

            self.url = urlparse(f"http://x{path}")

    # Inference surfaces the sandbox legitimately needs.
    for path in ("/v1/chat/completions", "/v1/messages", "/v1/embeddings"):
        assert _is_inference_path(_Req(path)) is True

    # Control-plane routes it must not reach.
    for path in ("/v1/agent/jobs", "/v1/agent/jobs/ajob_1", "/v1/agent/worker/claim", "/v1/models"):
        assert _is_inference_path(_Req(path)) is False


async def test_event_type_guard_rejects_a_trailing_newline(client: AsyncClient):
    """`match()` with `$` accepted "message\\n"; the guard must use fullmatch."""
    from serving.servers.routers.agent_jobs import _SAFE_EVENT_TYPE

    assert _SAFE_EVENT_TYPE.fullmatch("message") is not None
    assert _SAFE_EVENT_TYPE.fullmatch("message\n") is None


async def test_the_claim_carries_a_read_only_clone_credential(store: FakeAgentJobStore):
    """The runner needs to check the repository out; it must not get push rights.

    An installation token inherits *every* permission the App holds unless it
    asks for less — and this App holds `contents: write` so the publisher can
    push. Handing that to the runner would put a write credential on the host
    that executes untrusted repository code, which is exactly the thing the
    patch-out design exists to avoid.
    """
    from serving.servers.deps import get_agent_app_credentials

    asked: dict[str, Any] = {}

    class FakeApp:
        async def token_for(self, repo: str, **kwargs: Any) -> str:
            asked.update({"repo": repo, **kwargs})
            return "ghs_clone_only"

    app = _build_app(store)
    app.dependency_overrides[get_agent_app_credentials] = lambda: FakeApp()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await _create_job(client)
        claim = await client.post("/v1/agent/worker/claim", json={"worker_id": "w1"})

    assert claim.json()["clone_token"] == "ghs_clone_only"
    assert asked["permissions"] == {"contents": "read"}
    assert asked["repository_scoped"] is True


async def test_a_deployment_without_a_github_app_still_claims_jobs(client: AsyncClient):
    """No App configured is a working setup: a public repository needs no token."""
    await _create_job(client)
    claim = await client.post("/v1/agent/worker/claim", json={"worker_id": "w1"})

    assert claim.status_code == 200
    assert claim.json()["clone_token"] is None


async def test_an_uninstalled_app_hands_the_job_over_without_a_credential(
    store: FakeAgentJobStore,
):
    """A settled "not installed here" must not block the job.

    A public repository clones anonymously, and a private one fails with a
    message the owner can act on — which retrying would not improve.
    """
    from serving.agent_jobs.github_app import AppNotInstalled
    from serving.servers.deps import get_agent_app_credentials

    class UninstalledApp:
        async def token_for(self, repo: str, **kwargs: Any) -> str:
            raise AppNotInstalled(f"no App installation covers {repo}", status=404)

    app = _build_app(store)
    app.dependency_overrides[get_agent_app_credentials] = lambda: UninstalledApp()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        job_id = await _create_job(client)
        claim = await client.post("/v1/agent/worker/claim", json={"worker_id": "w1"})

    assert claim.status_code == 200
    assert claim.json()["job_id"] == job_id
    assert claim.json()["clone_token"] is None


async def test_a_transient_credential_error_is_retryable_not_terminal(
    store: FakeAgentJobStore,
):
    """GitHub having a bad minute must not permanently fail someone's job.

    Handing the runner a null token here sends it off to clone anonymously,
    fail on a private repository, and mark the job *terminally* failed.
    Abandoning the attempt instead is the retryable path: the lease expires
    unheartbeated and the reaper requeues it.
    """
    from serving.servers.deps import get_agent_app_credentials

    class FlakyApp:
        async def token_for(self, repo: str, **kwargs: Any) -> str:
            raise TimeoutError("api.github.com timed out")

    app = _build_app(store)
    app.dependency_overrides[get_agent_app_credentials] = lambda: FlakyApp()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await _create_job(client)
        claim = await client.post("/v1/agent/worker/claim", json={"worker_id": "w1"})

    assert claim.status_code == 503, "a null token here would terminally fail the job"
    # And the claim is handed back, not merely left to lapse. A lapsed lease
    # still spends a retry, so an outage lasting three claim cycles would fail
    # the job outright — the outcome this path exists to avoid, only slower.
    assert store.released, "the attempt must be returned to the queue"
    assert store.jobs[store.released[0][0]]["state"] == "queued"


async def test_the_worker_reports_the_commit_it_actually_worked_from(
    client: AsyncClient, store: FakeAgentJobStore
):
    """A job submitted without a base_sha is unpublishable until one is recorded.

    The runner resolves the default branch when it checks the repository out,
    so it is the only component that knows — and the publisher refuses to apply
    a patch without a base.
    """
    job_id = await _create_job(client)
    assert store.jobs[job_id].get("base_sha") in (None, "")

    claim = await client.post("/v1/agent/worker/claim", json={"worker_id": "w1"})
    auth = {"Authorization": f"Bearer {claim.json()['worker_token']}"}
    resolved = "a" * 40

    finish = await client.post(
        f"/v1/agent/worker/jobs/{job_id}/finish",
        json={"state": "succeeded", "base_sha": resolved},
        headers=auth,
    )

    assert finish.status_code == 200
    assert store.jobs[job_id]["base_sha"] == resolved


async def test_a_worker_cannot_overwrite_a_base_the_owner_pinned(
    client: AsyncClient, store: FakeAgentJobStore
):
    """An owner who names a commit must get a patch against that commit."""
    pinned = "b" * 40
    created = await client.post(
        "/v1/agent/jobs",
        json={
            "repo": "o/n",
            "task_prompt": "do the thing",
            "model": "m",
            "base_sha": pinned,
        },
    )
    job_id = created.json()["id"]

    claim = await client.post("/v1/agent/worker/claim", json={"worker_id": "w1"})
    auth = {"Authorization": f"Bearer {claim.json()['worker_token']}"}
    await client.post(
        f"/v1/agent/worker/jobs/{job_id}/finish",
        json={"state": "succeeded", "base_sha": "c" * 40},
        headers=auth,
    )

    assert store.jobs[job_id]["base_sha"] == pinned


async def test_an_unentitled_repo_is_refused_at_creation(client: AsyncClient):
    """The requester picks the repo; the platform supplies the GitHub authority.

    Without this check the two combine into a confused deputy: name any
    repository the App can reach, and read it back through your own job's
    events and patch artifact.
    """
    response = await client.post(
        "/v1/agent/jobs",
        json={"repo": "someone-else/private", "task_prompt": "exfiltrate", "model": "m"},
    )

    assert response.status_code == 403
    assert "repo_not_allowed" in response.text


async def test_a_legacy_row_gets_no_credential_at_claim(store: FakeAgentJobStore):
    """A row written before the check existed must not yield a token now."""
    from serving.servers.deps import get_agent_app_credentials

    minted: list[str] = []

    class FakeApp:
        async def token_for(self, repo: str, **kwargs: Any) -> str:
            minted.append(repo)
            return "ghs_should_not_happen"

    # Straight into the store, bypassing the API — the shape a pre-existing row has.
    await store.create_job(
        user_id=_OWNER,
        repo="someone-else/private",
        task_prompt="legacy",
        runtime="claude-code",
        model="m",
    )

    app = _build_app(store)
    app.dependency_overrides[get_agent_app_credentials] = lambda: FakeApp()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        claim = await client.post("/v1/agent/worker/claim", json={"worker_id": "w1"})

    assert claim.status_code == 403
    assert minted == [], "no credential may be minted for an unentitled repo"
    # And the job went back to the queue rather than burning an attempt.
    assert store.released


async def test_spend_and_usage_come_from_the_ledger_not_the_agent(store: FakeAgentJobStore):
    """A job's numbers must not depend on what the agent says about itself.

    Models do misreport their own usage — this session watched one claim it had
    edited a file it never touched — so the owner-facing totals read the same
    billing ledger the budget check already trusts.
    """
    from serving.servers.deps import get_log_store

    class Ledger:
        async def get_agent_job_cost(self, job_id: str) -> float:
            return 0.0075

        async def get_agent_job_usage(self, job_id: str) -> dict[str, float]:
            return {"tokens_in": 74000, "tokens_out": 523, "calls": 3}

    app = _build_app(store)
    app.dependency_overrides[get_log_store] = lambda: Ledger()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        job_id = await _create_job(client)
        got = await client.get(f"/v1/agent/jobs/{job_id}")

    body = got.json()
    assert body["spent_usd"] == 0.0075
    assert body["tokens_in"] == 74000
    assert body["model_calls"] == 3


async def test_a_deployment_without_a_ledger_reports_absent_not_zero(client: AsyncClient):
    """A missing source must not look like a job that spent nothing."""
    job_id = await _create_job(client)

    body = (await client.get(f"/v1/agent/jobs/{job_id}")).json()

    assert body["spent_usd"] is None
    assert body["tokens_in"] is None


async def test_the_owner_can_see_what_the_sandbox_could_reach(client: AsyncClient, monkeypatch):
    """The egress posture is reported, not left to trust."""
    monkeypatch.setenv("AGENT_SANDBOX_NETWORK", "agent-egress")
    monkeypatch.setenv("AGENT_EGRESS_AGENT_TIER", "platform_only")
    job_id = await _create_job(client)

    body = (await client.get(f"/v1/agent/jobs/{job_id}")).json()

    assert body["agent_egress_tier"] == "platform_only"
