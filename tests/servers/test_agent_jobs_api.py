"""API-surface tests for the agent-sandbox job router.

Runs against a minimal in-memory stand-in for ``AgentJobStore`` so the default
(no-database) suite covers the HTTP contract: owner scoping, capability-token
authentication, the fenced-write → 409 mapping, and SSE framing. The store's
own concurrency semantics are pinned separately by the ``dbtest`` suite.
"""

from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from serving.agent_jobs import terminal_coordination
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
        self.messages: list[dict[str, Any]] = []
        self.archived_threads: dict[str, datetime] = {}
        self.pinned_threads: dict[str, datetime] = {}
        self.live_fence: tuple[int, int] | None = None
        self.terminal_readiness: dict[int, bool] = {}
        self.released: list[tuple[str, int]] = []
        self.runner_hosts: dict[str, dict[str, Any]] = {}
        self.active_host: str | None = None
        self._next_event_id = 1
        self._next_job = 1

    # -- owner surface --
    async def create_job(self, **kwargs: Any) -> dict[str, Any]:
        job_id = f"ajob_{self._next_job:04d}"
        self._next_job += 1
        thread_id = f"athr_{job_id}"
        job = {
            "id": job_id,
            "thread_id": thread_id,
            "parent_job_id": None,
            "turn_no": 1,
            "state": "queued",
            "cancel_requested": False,
            "terminal_resume_pending": False,
            "current_attempt_id": None,
            "published_pr_url": None,
            "published_commit_sha": None,
            "detail": None,
            "created_at": None,
            "updated_at": None,
            **kwargs,
        }
        job.setdefault("base_sha", None)
        job.setdefault("metadata", None)
        self.jobs[job_id] = job
        self.messages.append(
            {
                "id": len(self.messages) + 1,
                "thread_id": thread_id,
                "job_id": job_id,
                "role": "user",
                "content": job["task_prompt"],
                "created_at": None,
            }
        )
        return job

    async def create_follow_up(
        self,
        *,
        parent_job_id: str,
        user_id: str,
        prompt: str,
        runtime: str | None = None,
        model: str | None = None,
        budget_usd: float | None = None,
    ) -> dict[str, Any] | None:
        requested = self.jobs.get(parent_job_id)
        if requested is None or requested["user_id"] != user_id:
            return None
        parent = max(
            (job for job in self.jobs.values() if job["thread_id"] == requested["thread_id"]),
            key=lambda job: job["turn_no"],
        )
        job_id = f"ajob_{self._next_job:04d}"
        self._next_job += 1
        turn_no = (
            max(
                job["turn_no"]
                for job in self.jobs.values()
                if job["thread_id"] == parent["thread_id"]
            )
            + 1
        )
        job = {
            **parent,
            "id": job_id,
            "parent_job_id": parent["id"],
            "turn_no": turn_no,
            "base_sha": parent.get("published_commit_sha") or parent.get("base_sha"),
            "task_prompt": prompt,
            "runtime": runtime or parent["runtime"],
            "model": model or parent["model"],
            "budget_usd": budget_usd if budget_usd is not None else parent.get("budget_usd"),
            "state": (
                "queued"
                if parent["state"] in {"failed", "cancelled"}
                or (
                    parent["state"] == "succeeded"
                    and (
                        parent.get("published_commit_sha")
                        or (parent["id"], "patch") not in self.artifacts
                    )
                )
                else "waiting"
            ),
            "cancel_requested": False,
            "terminal_resume_pending": False,
            "current_attempt_id": None,
            "published_pr_url": None,
            "published_commit_sha": None,
            "detail": None,
            "created_at": None,
            "updated_at": None,
            "pinned_at": self.pinned_threads.get(parent["thread_id"]),
        }
        self.jobs[job_id] = job
        self.messages.append(
            {
                "id": len(self.messages) + 1,
                "thread_id": job["thread_id"],
                "job_id": job_id,
                "role": "user",
                "content": prompt,
                "created_at": None,
            }
        )
        return job

    async def fork_thread(self, *, source_job_id: str, user_id: str) -> dict[str, Any] | None:
        source = self.jobs.get(source_job_id)
        if source is None or source["user_id"] != user_id:
            return None
        if source["state"] not in {"succeeded", "failed", "cancelled"}:
            return None
        new_thread_id = f"athr_fork_{self._next_job:04d}"
        turns = sorted(
            (
                job
                for job in self.jobs.values()
                if job["thread_id"] == source["thread_id"] and job["turn_no"] <= source["turn_no"]
            ),
            key=lambda job: job["turn_no"],
        )
        copied_id: dict[str, str] = {}
        previous: str | None = None
        for turn in turns:
            copy_id = f"ajob_{self._next_job:04d}"
            self._next_job += 1
            self.jobs[copy_id] = {
                **turn,
                "id": copy_id,
                "thread_id": new_thread_id,
                "parent_job_id": previous,
                "state": (
                    turn["state"]
                    if turn["state"] in {"succeeded", "failed", "cancelled"}
                    else "cancelled"
                ),
                "cancel_requested": False,
                "terminal_resume_pending": False,
                "current_attempt_id": None,
                "published_pr_url": None,
                "fork_source_job_id": turn["id"],
            }
            copied_id[turn["id"]] = copy_id
            previous = copy_id
        source_messages = sorted(
            (
                message
                for message in self.messages
                if message["thread_id"] == source["thread_id"]
                and self.jobs[message["job_id"]]["turn_no"] <= source["turn_no"]
            ),
            key=lambda message: (
                self.jobs[message["job_id"]]["turn_no"],
                0 if message["role"] == "user" else 1,
                message["id"],
            ),
        )
        for message in source_messages:
            self.messages.append(
                {
                    "id": len(self.messages) + 1,
                    "thread_id": new_thread_id,
                    "job_id": copied_id[message["job_id"]],
                    "role": message["role"],
                    "content": message["content"],
                    "created_at": message["created_at"],
                }
            )
        return self.jobs[copied_id[source["id"]]]

    async def resolve_legacy_published_commit(self, *, parent_job_id: str, commit_sha: str) -> bool:
        parent = self.jobs.get(parent_job_id)
        if parent is None or not parent.get("published_pr_url"):
            return False
        parent["published_commit_sha"] = commit_sha
        for child in self.jobs.values():
            if child.get("parent_job_id") == parent_job_id and child["state"] == "waiting":
                child["base_sha"] = commit_sha
                child["state"] = "queued"
        return True

    async def fail_waiting_follow_up(self, *, job_id: str, detail: str) -> bool:
        job = self.jobs.get(job_id)
        if job is None or job["state"] != "waiting":
            return False
        job["state"] = "failed"
        job["detail"] = detail
        return True

    async def get_thread_for_job(self, *, job_id: str, user_id: str) -> dict[str, Any] | None:
        job = self.jobs.get(job_id)
        if job is None or job["user_id"] != user_id:
            return None
        thread_id = job["thread_id"]
        jobs = sorted(
            (
                {**item, "pinned_at": self.pinned_threads.get(thread_id)}
                for item in self.jobs.values()
                if item["thread_id"] == thread_id
            ),
            key=lambda item: item["turn_no"],
        )
        return {
            "id": thread_id,
            "repo": job["repo"],
            "title": jobs[0]["task_prompt"].splitlines()[0],
            "created_at": None,
            "updated_at": None,
            "messages": sorted(
                (m for m in self.messages if m["thread_id"] == thread_id),
                key=lambda m: (
                    self.jobs[m["job_id"]]["turn_no"],
                    0 if m["role"] == "user" else 1,
                    m["id"],
                ),
            ),
            "jobs": jobs,
        }

    async def follow_up_context(self, *, job_id: str) -> dict[str, Any]:
        job = self.jobs[job_id]
        parent_id = job.get("parent_job_id")
        messages = [
            {"role": message["role"], "content": message["content"]}
            for message in self.messages
            if message["thread_id"] == job["thread_id"]
            and self.jobs[message["job_id"]]["turn_no"] < job["turn_no"]
        ]
        parent = self.jobs.get(parent_id) if parent_id else None
        artifact = self.artifacts.get((parent_id, "patch")) if parent_id else None
        patch = (
            artifact["content"]
            if artifact
            and parent
            and parent["state"] in {"succeeded", "publishing", "cancelled"}
            and not parent.get("published_commit_sha")
            else None
        )
        if patch is None and parent is not None and not parent.get("published_commit_sha"):
            source = self.jobs.get(parent.get("fork_source_job_id") or "")
            source_artifact = self.artifacts.get((parent.get("fork_source_job_id"), "patch"))
            if (
                source is not None
                and source_artifact
                and source["state"] in {"succeeded", "publishing", "cancelled"}
            ):
                patch = source_artifact["content"]
        return {"messages": messages, "patch": patch}

    async def get_job(self, job_id: str) -> dict[str, Any] | None:
        job = self.jobs.get(job_id)
        if job is None:
            return None
        return {**job, "pinned_at": self.pinned_threads.get(job["thread_id"])}

    async def list_jobs(
        self,
        *,
        user_id: str,
        limit: int = 50,
        archived: bool = False,
        repo: str | None = None,
    ) -> list[dict[str, Any]]:
        jobs = [
            {**job, "pinned_at": self.pinned_threads.get(job["thread_id"])}
            for job in self.jobs.values()
            if job["user_id"] == user_id
            and (job["thread_id"] in self.archived_threads) is archived
            and (repo is None or job["repo"] == repo)
        ]
        minimum = datetime.min.replace(tzinfo=timezone.utc)
        jobs.sort(
            key=lambda job: (
                job["pinned_at"] is not None,
                job["pinned_at"] or minimum,
                job.get("created_at") or minimum,
            ),
            reverse=True,
        )
        return jobs[:limit]

    async def list_projects(self, *, user_id: str, archived: bool = False) -> list[dict[str, Any]]:
        projects: dict[str, dict[str, Any]] = {}
        for job in self.jobs.values():
            if job["user_id"] != user_id:
                continue
            if (job["thread_id"] in self.archived_threads) is not archived:
                continue
            project = projects.setdefault(
                job["repo"],
                {
                    "repo": job["repo"],
                    "task_count": 0,
                    "active_count": 0,
                    "last_activity_at": None,
                    "pinned_count": 0,
                    "pinned_at": None,
                    "_threads": set(),
                    "_pinned_threads": set(),
                },
            )
            project["_threads"].add(job["thread_id"])
            project["task_count"] = len(project["_threads"])
            if job["state"] in {"queued", "waiting", "running", "publishing"}:
                project["active_count"] += 1
            pinned_at = self.pinned_threads.get(job["thread_id"])
            if pinned_at is not None:
                project["_pinned_threads"].add(job["thread_id"])
                project["pinned_count"] = len(project["_pinned_threads"])
                if project["pinned_at"] is None or pinned_at > project["pinned_at"]:
                    project["pinned_at"] = pinned_at
            created = job.get("created_at")
            if created is not None and (
                project["last_activity_at"] is None or created > project["last_activity_at"]
            ):
                project["last_activity_at"] = created
        for project in projects.values():
            project.pop("_threads")
            project.pop("_pinned_threads")
        minimum = datetime.min.replace(tzinfo=timezone.utc)
        return sorted(
            projects.values(),
            key=lambda project: (
                project["pinned_at"] is not None,
                project["pinned_at"] or minimum,
                project["last_activity_at"] or minimum,
            ),
            reverse=True,
        )

    async def set_thread_archived(
        self, *, job_id: str, user_id: str, archived: bool
    ) -> dict[str, Any] | None:
        job = self.jobs.get(job_id)
        if job is None or job["user_id"] != user_id:
            return None
        thread_id = job["thread_id"]
        if archived:
            archived_at = self.archived_threads.setdefault(thread_id, datetime.now(timezone.utc))
        else:
            self.archived_threads.pop(thread_id, None)
            archived_at = None
        return {"thread_id": thread_id, "archived_at": archived_at}

    async def set_thread_pinned(
        self, *, job_id: str, user_id: str, pinned: bool
    ) -> dict[str, Any] | None:
        job = self.jobs.get(job_id)
        if job is None or job["user_id"] != user_id:
            return None
        thread_id = job["thread_id"]
        if pinned:
            pinned_at = self.pinned_threads.setdefault(thread_id, datetime.now(timezone.utc))
        else:
            self.pinned_threads.pop(thread_id, None)
            pinned_at = None
        return {"thread_id": thread_id, "pinned_at": pinned_at}

    async def request_cancel(self, *, job_id: str, user_id: str | None = None) -> str | None:
        job = self.jobs.get(job_id)
        if job is None or (user_id is not None and job["user_id"] != user_id):
            return None
        job["cancel_requested"] = True
        if job["state"] in {"queued", "waiting"}:
            job["state"] = "cancelled"
            job["terminal_resume_pending"] = True
        return job["state"]

    async def list_events_after(
        self, *, job_id: str, after_id: int = 0, limit: int = 500
    ) -> list[dict[str, Any]]:
        return [
            event for event in self.events if event["job_id"] == job_id and event["id"] > after_id
        ][:limit]

    async def terminal_workspace_ready(self, *, attempt_id: int) -> bool:
        return self.terminal_readiness.get(attempt_id, False)

    async def fence_terminal_workspace(self, *, attempt_id: int, lease_generation: int) -> bool:
        if not self._fenced(attempt_id, lease_generation):
            return False
        self.terminal_readiness[attempt_id] = False
        return True

    async def list_terminal_resumes_pending(self, *, limit: int = 100) -> list[str]:
        return [
            job["id"]
            for job in self.jobs.values()
            if job["terminal_resume_pending"]
            and job["state"] in {"succeeded", "failed", "cancelled"}
        ][:limit]

    async def mark_terminal_resume_complete(self, *, job_id: str) -> bool:
        job = self.jobs[job_id]
        if not job["terminal_resume_pending"]:
            return False
        job["terminal_resume_pending"] = False
        return True

    async def get_artifact(self, *, job_id: str, kind: str) -> dict[str, Any] | None:
        return self.artifacts.get((job_id, kind))

    # -- worker surface (fenced) --
    def _fenced(self, attempt_id: int, lease_generation: int) -> bool:
        return self.live_fence == (attempt_id, lease_generation)

    async def touch_runner_host(self, *, host: str | None, worker_id: str) -> None:
        if not host:
            return
        now = datetime.now(timezone.utc)
        entry = self.runner_hosts.setdefault(
            host,
            {
                "host": host,
                "last_worker_id": worker_id,
                "first_seen_at": now,
                "last_seen_at": now,
            },
        )
        entry["last_seen_at"] = now
        entry["last_worker_id"] = worker_id

    async def active_runner_host(self) -> str | None:
        return self.active_host

    async def list_runner_hosts(self) -> list[dict[str, Any]]:
        return sorted(
            (
                {**entry, "is_active": entry["host"] == self.active_host}
                for entry in self.runner_hosts.values()
            ),
            key=lambda e: e["last_seen_at"],
            reverse=True,
        )

    async def set_active_runner_host(self, *, host: str | None) -> bool:
        if host is not None and host not in self.runner_hosts:
            return False
        self.active_host = host
        return True

    async def forget_runner_host(self, *, host: str) -> bool:
        return self.runner_hosts.pop(host, None) is not None

    async def claim_job(
        self, *, worker_id: str, lease_ttl_seconds: float, host: str | None = None
    ) -> dict[str, Any] | None:
        await self.touch_runner_host(host=host, worker_id=worker_id)
        if self.active_host is not None and host != self.active_host:
            return None
        queued = [job for job in self.jobs.values() if job["state"] == "queued"]
        if not queued:
            return None
        job = queued[0]
        job["state"] = "running"
        job["current_attempt_id"] = 100
        self.live_fence = (100, 1)
        self.terminal_readiness[100] = False
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
        phase = (payload or {}).get("phase") if event_type == "lifecycle" else None
        if phase == "workspace_ready":
            self.terminal_readiness[attempt_id] = True
        elif phase in {"workspace_preparing", "workspace_finalizing"}:
            self.terminal_readiness[attempt_id] = False
        if event_type == "message" and isinstance((payload or {}).get("text"), str):
            self.messages.append(
                {
                    "id": len(self.messages) + 1,
                    "thread_id": job["thread_id"],
                    "job_id": job["id"],
                    "role": "assistant",
                    "content": payload["text"],
                    "created_at": None,
                }
            )
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
        if to_state in {"succeeded", "failed", "cancelled"}:
            job["terminal_resume_pending"] = False
        job["detail"] = detail
        # Mirrors the store's COALESCE: a worker may fill in a base the job
        # lacked, never overwrite one the owner pinned.
        if base_sha and not job.get("base_sha"):
            job["base_sha"] = base_sha
        has_patch = (job_id, "patch") in self.artifacts
        if to_state in {"failed", "cancelled"} or (to_state == "succeeded" and not has_patch):
            for child in self.jobs.values():
                if child.get("parent_job_id") == job_id and child["state"] == "waiting":
                    child["state"] = "queued"
        return True

    async def record_publish(
        self, *, job_id: str, pr_url: str, commit_sha: str | None = None
    ) -> bool:
        job = self.jobs[job_id]
        job["published_pr_url"] = pr_url
        job["published_commit_sha"] = commit_sha
        for child in self.jobs.values():
            if child.get("parent_job_id") == job_id and child["state"] == "waiting":
                child["state"] = "queued"
                child["base_sha"] = commit_sha or child.get("base_sha")
        return True

    async def release_claim(
        self, *, job_id: str, attempt_id: int, lease_generation: int | None = None
    ) -> bool:
        job = self.jobs.get(job_id)
        if job is None or job.get("current_attempt_id") != attempt_id:
            return False
        self.released.append((job_id, attempt_id))
        job["state"] = "queued"
        job["current_attempt_id"] = None
        self.live_fence = None
        return True


class FakeOwnerAuthStore:
    """Operational-store slice needed by JWT and API-key owner auth."""

    def __init__(self, *, role: str = "internal") -> None:
        self.role = role
        self.last_used_key_id: str | None = None

    def _user(self) -> dict[str, Any]:
        return {
            "id": "key-1",
            "user_id": _OWNER,
            "user_name": "owner",
            "email": "owner@example.com",
            "role": self.role,
            "status": "active",
            "email_verified": True,
            "quota_daily_cost_usd": 100.0,
            "preferences": None,
            "max_concurrent_requests": 4,
        }

    async def get_user_by_id(self, user_id: str) -> dict[str, Any] | None:
        return self._user() if user_id == _OWNER else None

    async def get_auth_context_by_key_hash(self, _key_hash: str) -> dict[str, Any]:
        return self._user()

    async def get_user_cost_today(self, _user_id: str) -> float:
        return 0.0

    async def update_key_last_used(self, key_id: str) -> None:
        self.last_used_key_id = key_id


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


def _fake_route_exec(*model_ids: str):
    """The slice of RouteExecutor the visible-models predicate reads."""
    from types import SimpleNamespace

    routes = {}
    for model_id in model_ids:
        adapter = SimpleNamespace(config=SimpleNamespace(id=model_id, model_type="chat"))
        routes[model_id] = SimpleNamespace(
            published=True,
            required_role=None,
            admin_only=False,
            adapters=[(adapter, 1.0)],
        )
    return SimpleNamespace(routes=routes)


# Every model id the tests submit must resolve, or create fails fast by design.
_TEST_MODELS = ("glm-5.1", "qwen-next", "m")


def _build_app(store, *, role: str = "internal", dispatcher: bool = True):
    """Build an app with the router mounted and auth stubbed to one identity.

    ``dispatcher`` controls whether the machine-to-machine gate on
    ``/worker/claim`` is satisfied, so a test can assert that an ordinary
    caller is turned away there.
    """
    from serving.servers.deps import (
        get_agent_app_credentials,
        get_log_store,
        get_model_visibility_resolver,
        get_operational_store,
        get_router,
    )

    app = FastAPI()
    app.include_router(agent_jobs_router.router)
    app.dependency_overrides[get_agent_job_store] = lambda: store
    # The create endpoints fail fast on models the deployment cannot serve, so
    # the test registry must contain what the tests submit.
    app.dependency_overrides[get_router] = lambda: _fake_route_exec(*_TEST_MODELS)
    app.dependency_overrides[get_model_visibility_resolver] = lambda: None
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
    # Override the credential resolver, not the role gate, so the normal test
    # client still exercises the internal-only dogfood entitlement.
    app.dependency_overrides[agent_jobs_router.authenticate_agent_owner] = lambda: identity
    if dispatcher:
        app.dependency_overrides[agent_jobs_router.verify_dispatcher_access] = lambda: (
            "dispatcher@test"
        )
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


class _WorkspaceContentsApp:
    """GitHub Contents stand-in that records the pinned reads the API makes."""

    def __init__(self, contents: dict[str, Any]) -> None:
        self.contents = contents
        self.calls: list[tuple[str, str, str]] = []

    async def repository_contents(self, repo: str, *, path: str, ref: str) -> Any:
        from serving.agent_jobs.github_app import GitHubAppError

        self.calls.append((repo, path, ref))
        if path not in self.contents:
            raise GitHubAppError("not found", status=404)
        return self.contents[path]


async def test_create_get_list_round_trip(client: AsyncClient):
    """A created job is retrievable and listed for its owner."""
    job_id = await _create_job(client)

    got = await client.get(f"/v1/agent/jobs/{job_id}")
    assert got.status_code == 200
    assert got.json()["state"] == "queued"
    assert got.json()["runtime"] == "claude-code"

    listed = await client.get("/v1/agent/jobs")
    assert [job["id"] for job in listed.json()["jobs"]] == [job_id]


async def test_jobs_page_by_repo_and_projects_summarize_the_task_tree(client: AsyncClient):
    """The sidebar groups by project, so it pages one repo and lists them all."""
    first = await _create_job(client)
    second = await client.post(
        "/v1/agent/jobs",
        json={"repo": "o/n", "task_prompt": "a second project", "model": "glm-5.1"},
    )
    assert second.status_code == 201

    one_repo = await client.get("/v1/agent/jobs?repo=owner/name")
    assert [job["id"] for job in one_repo.json()["jobs"]] == [first]

    projects = (await client.get("/v1/agent/projects")).json()["projects"]
    by_repo = {project["repo"]: project for project in projects}
    assert set(by_repo) == {"owner/name", "o/n"}
    assert by_repo["o/n"]["task_count"] == 1
    assert by_repo["o/n"]["active_count"] == 1


async def test_repo_filter_rejects_a_value_git_would_read_as_an_option(client: AsyncClient):
    """The filter is shape-checked like the create field it mirrors."""
    rejected = await client.get("/v1/agent/jobs?repo=--upload-pack=sh")
    assert rejected.status_code == 422


async def test_archived_threads_leave_the_active_project_tree(client: AsyncClient):
    """A project with nothing but archived work is not an active folder."""
    job_id = await _create_job(client)
    assert (await client.post(f"/v1/agent/jobs/{job_id}/archive")).status_code == 200

    assert (await client.get("/v1/agent/projects")).json() == {"projects": []}
    archived = (await client.get("/v1/agent/projects?archived=true")).json()["projects"]
    assert [project["repo"] for project in archived] == ["owner/name"]


async def test_archive_and_restore_filter_the_entire_thread_without_cancelling(
    client: AsyncClient, store: FakeAgentJobStore
):
    """Archive hides every turn while leaving a live run untouched."""
    parent_id = await _create_job(client)
    follow_up = await client.post(
        f"/v1/agent/jobs/{parent_id}/follow-ups",
        json={"prompt": "also update the docs"},
    )
    child_id = follow_up.json()["id"]
    thread_id = follow_up.json()["thread_id"]
    store.jobs[parent_id]["state"] = "running"

    archived = await client.post(f"/v1/agent/jobs/{child_id}/archive")

    assert archived.status_code == 200
    assert archived.json()["thread_id"] == thread_id
    assert archived.json()["archived"] is True
    assert archived.json()["archived_at"] is not None
    assert store.jobs[parent_id]["state"] == "running"
    assert store.jobs[parent_id]["cancel_requested"] is False
    assert (await client.get("/v1/agent/jobs")).json() == {"jobs": []}
    archived_jobs = (await client.get("/v1/agent/jobs?archived=true")).json()["jobs"]
    assert {job["id"] for job in archived_jobs} == {parent_id, child_id}

    restored = await client.delete(f"/v1/agent/jobs/{parent_id}/archive")

    assert restored.status_code == 200
    assert restored.json() == {
        "thread_id": thread_id,
        "archived": False,
        "archived_at": None,
    }
    assert (await client.get("/v1/agent/jobs?archived=true")).json() == {"jobs": []}
    active_jobs = (await client.get("/v1/agent/jobs")).json()["jobs"]
    assert {job["id"] for job in active_jobs} == {parent_id, child_id}


async def test_pin_and_unpin_are_thread_scoped_idempotent_and_reorder_lists(
    client: AsyncClient, store: FakeAgentJobStore
):
    """Any turn pins the conversation and its project without touching run state."""
    parent_id = await _create_job(client)
    follow_up = await client.post(
        f"/v1/agent/jobs/{parent_id}/follow-ups",
        json={"prompt": "also update the docs"},
    )
    child_id = follow_up.json()["id"]
    thread_id = follow_up.json()["thread_id"]
    newer = await store.create_job(
        user_id=_OWNER,
        repo="owner/newer",
        task_prompt="newer task",
        runtime="claude-code",
        model="glm-5.1",
    )
    newer_id = newer["id"]
    store.jobs[parent_id]["created_at"] = datetime(2026, 7, 1, tzinfo=timezone.utc)
    store.jobs[child_id]["created_at"] = datetime(2026, 7, 2, tzinfo=timezone.utc)
    store.jobs[newer_id]["created_at"] = datetime(2026, 7, 3, tzinfo=timezone.utc)
    store.jobs[parent_id]["state"] = "running"

    pinned = await client.post(f"/v1/agent/jobs/{child_id}/pin")

    assert pinned.status_code == 200
    assert pinned.json()["thread_id"] == thread_id
    assert pinned.json()["pinned"] is True
    assert pinned.json()["pinned_at"] is not None
    pinned_again = await client.post(f"/v1/agent/jobs/{parent_id}/pin")
    assert pinned_again.json() == pinned.json()
    assert store.jobs[parent_id]["state"] == "running"
    assert store.jobs[parent_id]["cancel_requested"] is False

    jobs = (await client.get("/v1/agent/jobs")).json()["jobs"]
    assert [job["id"] for job in jobs[:2]] == [child_id, parent_id]
    assert jobs[0]["pinned_at"] == jobs[1]["pinned_at"] == pinned.json()["pinned_at"]
    assert jobs[2]["id"] == newer_id
    projects = (await client.get("/v1/agent/projects")).json()["projects"]
    assert [project["repo"] for project in projects] == ["owner/name", "owner/newer"]
    assert projects[0]["pinned_count"] == 1
    assert projects[0]["pinned_at"] == pinned.json()["pinned_at"]

    unpinned = await client.delete(f"/v1/agent/jobs/{parent_id}/pin")

    assert unpinned.json() == {
        "thread_id": thread_id,
        "pinned": False,
        "pinned_at": None,
    }
    jobs = (await client.get("/v1/agent/jobs")).json()["jobs"]
    assert jobs[0]["id"] == newer_id
    assert all(job["pinned_at"] is None for job in jobs)
    projects = (await client.get("/v1/agent/projects")).json()["projects"]
    assert [project["repo"] for project in projects] == ["owner/newer", "owner/name"]


async def test_follow_up_response_inherits_the_thread_pin(
    client: AsyncClient,
):
    """A new turn reports the conversation state without waiting for a list refresh."""
    parent_id = await _create_job(client)
    pinned = await client.post(f"/v1/agent/jobs/{parent_id}/pin")

    follow_up = await client.post(
        f"/v1/agent/jobs/{parent_id}/follow-ups",
        json={"prompt": "also update the docs"},
    )

    assert follow_up.status_code == 201
    assert follow_up.json()["pinned_at"] == pinned.json()["pinned_at"]


async def test_follow_up_is_a_durable_waiting_turn_with_parent_context(
    client: AsyncClient, store: FakeAgentJobStore
):
    """A user can send the next turn while the current sandbox is active."""
    parent_id = await _create_job(client)
    follow_up = await client.post(
        f"/v1/agent/jobs/{parent_id}/follow-ups",
        json={"prompt": "now add a regression test", "model": "qwen-next"},
    )
    assert follow_up.status_code == 201
    child = follow_up.json()
    assert child["state"] == "waiting"
    assert child["parent_job_id"] == parent_id
    assert child["turn_no"] == 2
    assert child["model"] == "qwen-next"

    thread = await client.get(f"/v1/agent/jobs/{child['id']}/thread")
    assert thread.status_code == 200
    assert [message["content"] for message in thread.json()["messages"]] == [
        "fix it",
        "now add a regression test",
    ]

    claim = (await client.post("/v1/agent/worker/claim", json={"worker_id": "w1"})).json()
    assert claim["job_id"] == parent_id
    auth = {"Authorization": f"Bearer {claim['worker_token']}"}
    await client.post(
        f"/v1/agent/worker/jobs/{parent_id}/events",
        json={"event_type": "message", "payload": {"text": "implemented the fix"}},
        headers=auth,
    )
    await client.post(
        f"/v1/agent/worker/jobs/{parent_id}/artifacts",
        json={"kind": "patch", "content": "diff --git a/x b/x"},
        headers=auth,
    )
    finished = await client.post(
        f"/v1/agent/worker/jobs/{parent_id}/finish",
        json={"state": "succeeded"},
        headers=auth,
    )
    assert finished.status_code == 200
    assert store.jobs[child["id"]]["state"] == "waiting"
    await store.record_publish(
        job_id=parent_id, pr_url="https://github.test/pr/1", commit_sha="a" * 40
    )
    assert store.jobs[child["id"]]["state"] == "queued"

    child_claim = (await client.post("/v1/agent/worker/claim", json={"worker_id": "w2"})).json()
    assert child_claim["job_id"] == child["id"]
    assert child_claim["base_sha"] == "a" * 40
    assert child_claim["context_patch"] is None
    assert child_claim["context_messages"] == [
        {"role": "user", "content": "fix it"},
        {"role": "assistant", "content": "implemented the fix"},
    ]


async def test_create_fails_fast_on_a_model_this_deployment_cannot_serve(client: AsyncClient):
    """An unknown model is a 400 at create, not a dead job after claim+clone."""
    response = await client.post(
        "/v1/agent/jobs",
        json={"repo": "owner/name", "task_prompt": "fix it", "model": "gpt-nonexistent"},
    )
    assert response.status_code == 400
    detail = response.json()["detail"]["error"]
    assert detail["type"] == "model_not_available"
    # The rejection names what would work, so the caller can fix the request.
    assert "glm-5.1" in detail["message"]


async def test_follow_up_model_switch_gets_the_same_fail_fast(client: AsyncClient):
    """Switching models on a follow-up is validated like a fresh job."""
    parent_id = await _create_job(client)
    response = await client.post(
        f"/v1/agent/jobs/{parent_id}/follow-ups",
        json={"prompt": "try another model", "model": "gpt-nonexistent"},
    )
    assert response.status_code == 400
    assert response.json()["detail"]["error"]["type"] == "model_not_available"


async def test_config_offers_the_models_the_create_endpoint_accepts(client: AsyncClient):
    """The picker and the validator must be the same list, or one is lying."""
    config = await client.get("/v1/agent/config")
    assert config.status_code == 200
    assert config.json()["models"] == list(_TEST_MODELS)


async def test_follow_ups_append_to_latest_turn_and_reject_blank_prompts(
    client: AsyncClient,
):
    """Old links still append linearly, and whitespace is not a durable turn."""
    parent_id = await _create_job(client)
    first = await client.post(
        f"/v1/agent/jobs/{parent_id}/follow-ups",
        json={"prompt": "first follow-up"},
    )
    assert first.status_code == 201

    # Submit from the old parent URL again. It must attach to the current tip,
    # not create a sibling that could run against the same branch concurrently.
    second = await client.post(
        f"/v1/agent/jobs/{parent_id}/follow-ups",
        json={"prompt": "second follow-up"},
    )
    assert second.status_code == 201
    assert second.json()["parent_job_id"] == first.json()["id"]
    assert second.json()["turn_no"] == 3
    assert second.json()["state"] == "waiting"

    blank = await client.post(
        f"/v1/agent/jobs/{parent_id}/follow-ups",
        json={"prompt": "   \n  "},
    )
    assert blank.status_code == 422


async def test_fork_copies_settled_history_into_a_new_thread(
    client: AsyncClient, store: FakeAgentJobStore
):
    """Fork duplicates durable turns, queues nothing, and resumes on follow-up."""
    source_id = await _create_job(client)
    claim = (await client.post("/v1/agent/worker/claim", json={"worker_id": "w1"})).json()
    auth = {"Authorization": f"Bearer {claim['worker_token']}"}
    await client.post(
        f"/v1/agent/worker/jobs/{source_id}/events",
        json={"event_type": "message", "payload": {"text": "done: fixed"}},
        headers=auth,
    )
    await client.post(
        f"/v1/agent/worker/jobs/{source_id}/artifacts",
        json={"kind": "patch", "content": "diff --git a/x b/x"},
        headers=auth,
    )
    await client.post(
        f"/v1/agent/worker/jobs/{source_id}/finish",
        json={"state": "succeeded"},
        headers=auth,
    )

    forked = await client.post(f"/v1/agent/jobs/{source_id}/fork")

    assert forked.status_code == 201
    fork = forked.json()
    assert fork["id"] != source_id
    assert fork["thread_id"] != store.jobs[source_id]["thread_id"]
    assert fork["forked_from_job_id"] == source_id
    assert fork["state"] == "succeeded"
    assert fork["published_pr_url"] is None
    assert all(job["state"] != "queued" for job in store.jobs.values())

    thread = await client.get(f"/v1/agent/jobs/{fork['id']}/thread")
    assert thread.status_code == 200
    assert [(m["role"], m["content"]) for m in thread.json()["messages"]] == [
        ("user", "fix it"),
        ("assistant", "done: fixed"),
    ]

    follow_up = await client.post(
        f"/v1/agent/jobs/{fork['id']}/follow-ups",
        json={"prompt": "continue in the fork"},
    )
    assert follow_up.status_code == 201
    child = follow_up.json()
    assert child["state"] == "queued"
    assert child["turn_no"] == 2
    child_claim = (await client.post("/v1/agent/worker/claim", json={"worker_id": "w2"})).json()
    assert child_claim["job_id"] == child["id"]
    assert child_claim["context_messages"] == [
        {"role": "user", "content": "fix it"},
        {"role": "assistant", "content": "done: fixed"},
    ]
    # The source's still-unpublished patch crosses the fork boundary.
    assert child_claim["context_patch"] == "diff --git a/x b/x"


async def test_fork_from_an_earlier_turn_rewinds_the_conversation(
    client: AsyncClient, store: FakeAgentJobStore
):
    """Anchoring the fork on turn one leaves later turns behind."""
    first_id = await _create_job(client)
    claim = (await client.post("/v1/agent/worker/claim", json={"worker_id": "w1"})).json()
    auth = {"Authorization": f"Bearer {claim['worker_token']}"}
    await client.post(
        f"/v1/agent/worker/jobs/{first_id}/events",
        json={"event_type": "message", "payload": {"text": "answer one"}},
        headers=auth,
    )
    await client.post(
        f"/v1/agent/worker/jobs/{first_id}/finish",
        json={"state": "succeeded"},
        headers=auth,
    )
    second = await client.post(
        f"/v1/agent/jobs/{first_id}/follow-ups",
        json={"prompt": "second question"},
    )
    assert second.status_code == 201

    forked = await client.post(f"/v1/agent/jobs/{first_id}/fork")

    assert forked.status_code == 201
    thread = await client.get(f"/v1/agent/jobs/{forked.json()['id']}/thread")
    assert [(m["role"], m["content"]) for m in thread.json()["messages"]] == [
        ("user", "fix it"),
        ("assistant", "answer one"),
    ]


async def test_fork_of_an_active_turn_is_refused(client: AsyncClient, store: FakeAgentJobStore):
    """A running turn's output is not history yet, so it cannot anchor a fork."""
    job_id = await _create_job(client)

    queued = await client.post(f"/v1/agent/jobs/{job_id}/fork")
    assert queued.status_code == 409
    assert queued.json()["detail"]["error"]["type"] == "not_settled"

    store.jobs[job_id]["state"] = "running"
    running = await client.post(f"/v1/agent/jobs/{job_id}/fork")
    assert running.status_code == 409


async def test_fork_is_owner_scoped(client: AsyncClient, store: FakeAgentJobStore):
    """Someone else's job id forks as 404, indistinguishable from absent."""
    job_id = await _create_job(client)
    store.jobs[job_id]["state"] = "succeeded"
    store.jobs[job_id]["user_id"] = _OTHER

    response = await client.post(f"/v1/agent/jobs/{job_id}/fork")

    assert response.status_code == 404


async def test_follow_up_resolves_a_legacy_draft_pr_branch(
    store: FakeAgentJobStore,
):
    """Pre-thread jobs resume from their existing branch instead of diverging."""
    from serving.servers.deps import get_agent_app_credentials

    class LegacyBranchApp:
        async def resolve_ref(self, repo: str, ref: str) -> str:
            assert repo == "owner/name"
            assert ref.startswith("agent/")
            return "d" * 40

    app = _build_app(store)
    app.dependency_overrides[get_agent_app_credentials] = lambda: LegacyBranchApp()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as local_client:
        parent_id = await _create_job(local_client)
        parent = store.jobs[parent_id]
        parent["state"] = "succeeded"
        parent["published_pr_url"] = "https://github.test/pr/legacy"
        parent["published_commit_sha"] = None
        store.artifacts[(parent_id, "patch")] = {"content": "diff --git a/x b/x"}

        response = await local_client.post(
            f"/v1/agent/jobs/{parent_id}/follow-ups",
            json={"prompt": "continue the old PR"},
        )

    assert response.status_code == 201
    assert response.json()["state"] == "queued"
    assert response.json()["base_sha"] == "d" * 40
    assert store.jobs[parent_id]["published_commit_sha"] == "d" * 40


async def test_browser_jwt_can_list_agent_jobs(store: FakeAgentJobStore):
    """Regression: the web login JWT was interpreted as an invalid API key."""
    from serving.servers.deps import get_operational_store
    from serving.utils.jwt import create_access_token

    app = _build_app(store)
    app.dependency_overrides.pop(agent_jobs_router.authenticate_agent_owner)
    app.dependency_overrides[get_operational_store] = lambda: FakeOwnerAuthStore()
    token, _ = create_access_token(
        user_id=_OWNER,
        email="owner@example.com",
        role="internal",
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as jwt_client:
        response = await jwt_client.get(
            "/v1/agent/jobs",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    assert response.json() == {"jobs": []}


async def test_expired_browser_jwt_is_not_reinterpreted_as_api_key(store: FakeAgentJobStore):
    """Credential dispatch must preserve a JWT's own expiry failure."""
    from datetime import timedelta

    from serving.servers.deps import get_operational_store
    from serving.utils.jwt import create_access_token

    app = _build_app(store)
    app.dependency_overrides.pop(agent_jobs_router.authenticate_agent_owner)
    app.dependency_overrides[get_operational_store] = lambda: FakeOwnerAuthStore()
    token, _ = create_access_token(
        user_id=_OWNER,
        email="owner@example.com",
        role="internal",
        expires_delta=timedelta(seconds=-1),
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as jwt_client:
        response = await jwt_client.get(
            "/v1/agent/jobs",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 401
    assert response.json()["detail"].startswith("Token has expired")


async def test_normal_api_key_still_authenticates_agent_owner(store: FakeAgentJobStore):
    """Programmatic clients keep the existing API-key path and quota checks."""
    from serving.servers.deps import get_operational_store

    auth_store = FakeOwnerAuthStore()
    app = _build_app(store)
    app.dependency_overrides.pop(agent_jobs_router.authenticate_agent_owner)
    app.dependency_overrides[get_operational_store] = lambda: auth_store

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as api_client:
        response = await api_client.get(
            "/v1/agent/jobs",
            headers={"Authorization": "Bearer hyi-programmatic-client"},
        )

    assert response.status_code == 200
    assert auth_store.last_used_key_id == "key-1"


async def test_sandbox_token_stays_out_of_owner_routes(store: FakeAgentJobStore):
    """A structured Agent token must not be mistaken for a browser JWT."""
    from serving.agent_jobs.tokens import SCOPE_MODEL
    from serving.servers.deps import get_operational_store

    app = _build_app(store)
    app.dependency_overrides.pop(agent_jobs_router.authenticate_agent_owner)
    app.dependency_overrides[get_operational_store] = lambda: FakeOwnerAuthStore()
    token = mint_worker_token(
        job_id="ajob_0001",
        attempt_id=1,
        lease_generation=1,
        scope=SCOPE_MODEL,
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as sandbox_client:
        response = await sandbox_client.get(
            "/v1/agent/jobs",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 403
    assert response.json()["detail"]["error"]["type"] == "insufficient_scope"


@pytest.mark.parametrize("role", ["free", "pro"])
async def test_non_internal_owner_is_denied_server_side(store: FakeAgentJobStore, role: str):
    """The client-side dogfood gate must not be bypassable via the API."""
    app = _build_app(store, role=role)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as denied_client:
        response = await denied_client.get("/v1/agent/jobs")

    assert response.status_code == 403
    assert response.json()["detail"] == "Agent access requires role 'internal'."


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
        f"/v1/agent/jobs/{foreign['id']}/files",
        f"/v1/agent/jobs/{foreign['id']}/git",
    ):
        response = await client.get(path)
        assert response.status_code == 404, path

    write = await client.put(
        f"/v1/agent/jobs/{foreign['id']}/files",
        params={"path": "README.md"},
        json={"content": "forbidden"},
    )
    terminal = await client.post(
        f"/v1/agent/jobs/{foreign['id']}/terminal",
        json={"command": "pwd", "cwd": "/workspace"},
    )
    terminal_session_requests = [
        await client.post(
            f"/v1/agent/jobs/{foreign['id']}/terminals",
            json={"rows": 24, "cols": 80},
        ),
        await client.get(f"/v1/agent/jobs/{foreign['id']}/terminals"),
        await client.get(f"/v1/agent/jobs/{foreign['id']}/terminals/term_1/stream"),
        await client.post(
            f"/v1/agent/jobs/{foreign['id']}/terminals/term_1/input",
            json={"data": "bHMK"},
        ),
        await client.post(
            f"/v1/agent/jobs/{foreign['id']}/terminals/term_1/resize",
            json={"rows": 30, "cols": 100},
        ),
        await client.delete(f"/v1/agent/jobs/{foreign['id']}/terminals/term_1"),
    ]
    assert write.status_code == 404
    assert terminal.status_code == 404
    assert all(response.status_code == 404 for response in terminal_session_requests)
    cancel = await client.post(f"/v1/agent/jobs/{foreign['id']}/cancel")
    assert cancel.status_code == 404
    for method in (client.post, client.delete):
        archive = await method(f"/v1/agent/jobs/{foreign['id']}/archive")
        assert archive.status_code == 404
        missing = await method("/v1/agent/jobs/ajob_missing/archive")
        assert missing.status_code == 404
        pin = await method(f"/v1/agent/jobs/{foreign['id']}/pin")
        assert pin.status_code == 404
        missing_pin = await method("/v1/agent/jobs/ajob_missing/pin")
        assert missing_pin.status_code == 404


async def test_workspace_files_merge_pinned_base_and_changed_snapshot(
    store: FakeAgentJobStore,
):
    """Files are read at the job SHA and overlaid with added/modified/deleted entries."""
    import base64

    from serving.servers.deps import get_agent_app_credentials

    app_creds = _WorkspaceContentsApp(
        {
            "": [
                {"name": "README.md", "path": "README.md", "type": "file", "size": 4},
                {"name": "config", "path": "config", "type": "file", "size": 3},
                {"name": "src", "path": "src", "type": "dir", "size": 0},
            ],
            "src": [
                {"name": "base.py", "path": "src/base.py", "type": "file", "size": 5},
                {"name": "old.py", "path": "src/old.py", "type": "file", "size": 3},
            ],
            "src/base.py": {
                "path": "src/base.py",
                "type": "file",
                "size": 5,
                # GitHub's payload is normally line-wrapped.
                "content": base64.b64encode(b"base\n").decode() + "\n",
                "encoding": "base64",
            },
        }
    )
    app = _build_app(store)
    app.dependency_overrides[get_agent_app_credentials] = lambda: app_creds
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as local_client:
        job_id = await _create_job(local_client)
        store.jobs[job_id]["base_sha"] = "a" * 40
        store.artifacts[(job_id, "workspace_snapshot")] = {
            "job_id": job_id,
            "attempt_id": 100,
            "kind": "workspace_snapshot",
            "content": json.dumps(
                {
                    "version": 1,
                    "files": [
                        {
                            "path": "README.md",
                            "status": "modified",
                            "size": 8,
                            "content": "changed\n",
                        },
                        {
                            "path": "src/new.py",
                            "status": "added",
                            "size": 4,
                            "content": "new\n",
                        },
                        {"path": "src/old.py", "status": "deleted", "size": 0},
                        {"path": "config", "status": "deleted", "size": 0},
                        {
                            "path": "config/app.py",
                            "status": "added",
                            "size": 4,
                            "content": "app\n",
                        },
                    ],
                    "truncated": False,
                }
            ),
            "created_at": None,
        }

        root = await local_client.get(f"/v1/agent/jobs/{job_id}/files")
        src = await local_client.get(f"/v1/agent/jobs/{job_id}/files", params={"path": "src"})
        config = await local_client.get(f"/v1/agent/jobs/{job_id}/files", params={"path": "config"})
        changed = await local_client.get(
            f"/v1/agent/jobs/{job_id}/files", params={"path": "README.md"}
        )
        unchanged = await local_client.get(
            f"/v1/agent/jobs/{job_id}/files", params={"path": "src/base.py"}
        )

    assert root.status_code == 200
    root_entries = {entry["path"]: entry for entry in root.json()["entries"]}
    assert root_entries["README.md"]["status"] == "modified"
    assert root_entries["config"]["kind"] == "directory"
    assert root_entries["config"]["status"] == "modified"
    assert root_entries["src"]["status"] == "modified"
    src_entries = {entry["path"]: entry for entry in src.json()["entries"]}
    assert src_entries["src/base.py"]["status"] is None
    assert src_entries["src/new.py"]["status"] == "added"
    assert src_entries["src/old.py"]["status"] == "deleted"
    assert config.status_code == 200
    config_entries = {entry["path"]: entry for entry in config.json()["entries"]}
    assert config_entries["config/app.py"]["status"] == "added"
    assert changed.json()["content"] == "changed\n"
    assert unchanged.json()["content"] == "base\n"
    assert app_creds.calls == [
        ("owner/name", "", "a" * 40),
        ("owner/name", "src", "a" * 40),
        ("owner/name", "src/base.py", "a" * 40),
    ]


async def test_live_workspace_routes_use_the_job_broker(store: FakeAgentJobStore, monkeypatch):
    """Files, Terminal and Git all address the same durable job worktree."""

    class Broker:
        def __init__(self) -> None:
            self.calls: list[tuple] = []

        async def files(self, workspace_id: str, path: str):
            self.calls.append(("files", workspace_id, path))
            return {
                "path": path,
                "kind": "directory",
                "entries": [],
                "writable": True,
                "source": "workspace",
            }

        async def write_file(self, workspace_id: str, path: str, content: str):
            self.calls.append(("write", workspace_id, path, content))
            return {
                "path": path,
                "kind": "file",
                "content": content,
                "size": len(content),
                "writable": True,
                "source": "workspace",
            }

        async def terminal(self, workspace_id: str, **kwargs):
            self.calls.append(("terminal", workspace_id, kwargs["command"], kwargs["cwd"]))
            return {"output": "ok\n", "stderr": "", "exit_code": 0, "cwd": "/workspace"}

        async def git(self, workspace_id: str, base_sha: str | None = None):
            self.calls.append(("git", workspace_id, base_sha))
            return {
                "available": True,
                "branch": "agent/thread",
                "changes": [{"code": " M", "path": "README.md"}],
                "patch": "diff --git a/README.md b/README.md\n",
                "commits": [],
            }

    broker = Broker()
    monkeypatch.setattr(agent_jobs_router, "workspace_broker_from_env", lambda: broker)
    app = _build_app(store)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as local_client:
        job_id = await _create_job(local_client)
        listing = await local_client.get(f"/v1/agent/jobs/{job_id}/files")
        saved = await local_client.put(
            f"/v1/agent/jobs/{job_id}/files",
            params={"path": "README.md"},
            json={"content": "updated"},
        )
        terminal = await local_client.post(
            f"/v1/agent/jobs/{job_id}/terminal",
            json={"command": "pwd", "cwd": "/workspace"},
        )
        git = await local_client.get(f"/v1/agent/jobs/{job_id}/git")

    workspace_id = job_id
    assert listing.status_code == 200
    assert listing.json()["writable"] is True
    assert listing.json()["source"] == "workspace"
    assert saved.status_code == 200
    assert terminal.json()["output"] == "ok\n"
    assert git.json()["branch"] == "agent/thread"
    assert broker.calls == [
        ("files", workspace_id, ""),
        ("write", workspace_id, "README.md", "updated"),
        ("terminal", workspace_id, "pwd", "/workspace"),
        ("git", workspace_id, store.jobs[job_id].get("base_sha")),
    ]


def _terminal_session_descriptor(*, state: str = "running") -> dict[str, Any]:
    return {
        "id": "term_1",
        "shell": "/bin/bash",
        "state": state,
        "cwd": "/workspace",
        "rows": 24,
        "cols": 80,
        "last_seq": 2,
    }


class _FiniteTerminalStream:
    def __init__(self, *chunks: bytes) -> None:
        self.chunks = chunks
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


class _TerminalSessionBroker:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.stream = _FiniteTerminalStream(
            b'event: output\ndata: {"seq":1,"data":"aGk="}\n\n',
            b'event: exit\ndata: {"seq":2,"exit_code":0}\n\n',
        )

    async def create_terminal(self, workspace_id: str, *, rows: int, cols: int):
        self.calls.append(("create", workspace_id, rows, cols))
        return _terminal_session_descriptor()

    async def list_terminals(self, workspace_id: str):
        self.calls.append(("list", workspace_id))
        return {"terminals": [_terminal_session_descriptor()]}

    async def suspend_terminals(self, workspace_id: str, *, lease_generation: int):
        self.calls.append(("suspend_all", workspace_id, lease_generation))
        return {"ok": True}

    async def resume_terminals(self, workspace_id: str, *, lease_generation: int):
        self.calls.append(("resume_all", workspace_id, lease_generation))
        return {"ok": True}

    async def resume_settled_terminals(self, workspace_id: str):
        self.calls.append(("resume_settled", workspace_id))
        return {"ok": True}

    async def stream_terminal(self, workspace_id: str, terminal_id: str, *, after: int):
        self.calls.append(("stream", workspace_id, terminal_id, after))
        return self.stream

    async def terminal_input(self, workspace_id: str, terminal_id: str, *, data: str):
        self.calls.append(("input", workspace_id, terminal_id, data))
        return _terminal_session_descriptor()

    async def resize_terminal(self, workspace_id: str, terminal_id: str, *, rows: int, cols: int):
        self.calls.append(("resize", workspace_id, terminal_id, rows, cols))
        return {**_terminal_session_descriptor(), "rows": rows, "cols": cols}

    async def delete_terminal(self, workspace_id: str, terminal_id: str):
        self.calls.append(("delete", workspace_id, terminal_id))
        return _terminal_session_descriptor(state="closed")


async def test_interactive_terminal_routes_proxy_owner_session_lifecycle(
    store: FakeAgentJobStore, monkeypatch
):
    """The gateway keeps broker credentials private while proxying PTY controls."""
    broker = _TerminalSessionBroker()
    monkeypatch.setattr(agent_jobs_router, "workspace_broker_from_env", lambda: broker)
    app = _build_app(store)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as local_client:
        job_id = await _create_job(local_client)
        store.jobs[job_id]["state"] = "succeeded"

        created = await local_client.post(
            f"/v1/agent/jobs/{job_id}/terminals",
            json={"rows": 24, "cols": 80},
        )
        listed = await local_client.get(f"/v1/agent/jobs/{job_id}/terminals")
        wrote = await local_client.post(
            f"/v1/agent/jobs/{job_id}/terminals/term_1/input",
            json={"data": base64.b64encode(b"ls\n").decode()},
        )
        resized = await local_client.post(
            f"/v1/agent/jobs/{job_id}/terminals/term_1/resize",
            json={"rows": 40, "cols": 120},
        )
        killed = await local_client.delete(f"/v1/agent/jobs/{job_id}/terminals/term_1")

    assert created.status_code == 200
    assert created.json()["id"] == "term_1"
    assert listed.json() == {"terminals": [_terminal_session_descriptor()]}
    assert wrote.status_code == 200
    assert resized.json()["rows"] == 40
    assert resized.json()["cols"] == 120
    assert killed.status_code == 200
    assert killed.json()["state"] == "closed"
    assert broker.calls == [
        ("create", job_id, 24, 80),
        ("list", job_id),
        ("input", job_id, "term_1", "bHMK"),
        ("resize", job_id, "term_1", 40, 120),
        ("delete", job_id, "term_1"),
    ]


async def test_terminal_controls_use_the_attached_session_capability(
    store: FakeAgentJobStore, monkeypatch
):
    """Attached PTY controls skip entitlement and never replay the event log."""
    broker = _TerminalSessionBroker()
    monkeypatch.setattr(agent_jobs_router, "workspace_broker_from_env", lambda: broker)

    async def unexpected_entitlement_recheck(**_kwargs):
        raise AssertionError("attached terminal controls must not call external entitlement")

    monkeypatch.setattr(
        agent_jobs_router,
        "_require_workspace_entitlement",
        unexpected_entitlement_recheck,
    )

    async def unexpected_event_query(**_kwargs):
        raise AssertionError("terminal input must use the attempt readiness field")

    monkeypatch.setattr(store, "list_events_after", unexpected_event_query)
    app = _build_app(store)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as local_client:
        job_id = await _create_job(local_client)
        claimed = await store.claim_job(worker_id="worker-1", lease_ttl_seconds=60)
        assert claimed is not None
        await store.append_event(
            attempt_id=claimed["attempt_id"],
            lease_generation=claimed["lease_generation"],
            event_type="lifecycle",
            payload={"phase": "workspace_ready"},
        )
        wrote = await local_client.post(
            f"/v1/agent/jobs/{job_id}/terminals/term_1/input",
            json={"data": "eA=="},
        )
        resized = await local_client.post(
            f"/v1/agent/jobs/{job_id}/terminals/term_1/resize",
            json={"rows": 30, "cols": 100},
        )
        killed = await local_client.delete(f"/v1/agent/jobs/{job_id}/terminals/term_1")

    assert wrote.status_code == 200
    assert resized.status_code == 200
    assert killed.status_code == 200
    assert broker.calls == [
        ("input", job_id, "term_1", "eA=="),
        ("resize", job_id, "term_1", 30, 100),
        ("delete", job_id, "term_1"),
    ]


async def test_interactive_terminal_stream_is_relayed_byte_for_byte(
    store: FakeAgentJobStore, monkeypatch
):
    """Output and exit SSE frames cross the gateway without re-encoding."""
    broker = _TerminalSessionBroker()
    monkeypatch.setattr(agent_jobs_router, "workspace_broker_from_env", lambda: broker)

    async def unexpected_event_query(**_kwargs):
        raise AssertionError("attached terminal streams must not query lifecycle events")

    monkeypatch.setattr(store, "list_events_after", unexpected_event_query)
    app = _build_app(store)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as local_client:
        job_id = await _create_job(local_client)
        response = await local_client.get(
            f"/v1/agent/jobs/{job_id}/terminals/term_1/stream",
            params={"after": 7},
        )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.content == (
        b'event: output\ndata: {"seq":1,"data":"aGk="}\n\n'
        b'event: exit\ndata: {"seq":2,"exit_code":0}\n\n'
    )
    assert broker.calls == [("stream", job_id, "term_1", 7)]
    assert broker.stream.closed is True


async def test_full_terminal_lifecycle_is_available_while_agent_runs(
    store: FakeAgentJobStore, monkeypatch
):
    """PTY creation waits for checkout, then stays available during the run."""
    broker = _TerminalSessionBroker()
    monkeypatch.setattr(agent_jobs_router, "workspace_broker_from_env", lambda: broker)
    app = _build_app(store)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as local_client:
        job_id = await _create_job(local_client)
        queued_create = await local_client.post(
            f"/v1/agent/jobs/{job_id}/terminals",
            json={"rows": 24, "cols": 80},
        )
        queued_list = await local_client.get(f"/v1/agent/jobs/{job_id}/terminals")

        claimed = await store.claim_job(worker_id="worker-1", lease_ttl_seconds=60)
        assert claimed is not None
        early_create = await local_client.post(
            f"/v1/agent/jobs/{job_id}/terminals",
            json={"rows": 24, "cols": 80},
        )
        early_list = await local_client.get(f"/v1/agent/jobs/{job_id}/terminals")

        event_id = await store.append_event(
            attempt_id=claimed["attempt_id"],
            lease_generation=claimed["lease_generation"],
            event_type="lifecycle",
            payload={"phase": "workspace_ready"},
        )
        assert event_id is not None
        create = await local_client.post(
            f"/v1/agent/jobs/{job_id}/terminals",
            json={"rows": 24, "cols": 80},
        )
        write_before_retry = await local_client.post(
            f"/v1/agent/jobs/{job_id}/terminals/term_1/input",
            json={"data": "bHMK"},
        )
        store.jobs[job_id]["state"] = "queued"
        write_while_requeued = await local_client.post(
            f"/v1/agent/jobs/{job_id}/terminals/term_1/input",
            json={"data": "cHdkCg=="},
        )
        store.jobs[job_id]["state"] = "running"
        store.jobs[job_id]["current_attempt_id"] = 101
        store.live_fence = (101, 2)
        write_while_preparing = await local_client.post(
            f"/v1/agent/jobs/{job_id}/terminals/term_1/input",
            json={"data": "cHdkCg=="},
        )
        retry_event_id = await store.append_event(
            attempt_id=101,
            lease_generation=2,
            event_type="lifecycle",
            payload={"phase": "workspace_ready"},
        )
        assert retry_event_id is not None
        write_after_retry = await local_client.post(
            f"/v1/agent/jobs/{job_id}/terminals/term_1/input",
            json={"data": "cHdkCg=="},
        )
        resize = await local_client.post(
            f"/v1/agent/jobs/{job_id}/terminals/term_1/resize",
            json={"rows": 30, "cols": 100},
        )
        listed = await local_client.get(f"/v1/agent/jobs/{job_id}/terminals")
        killed_once = await local_client.delete(f"/v1/agent/jobs/{job_id}/terminals/term_1")
        killed_twice = await local_client.delete(f"/v1/agent/jobs/{job_id}/terminals/term_1")

    for response in (queued_create, queued_list, early_create, early_list):
        assert response.status_code == 409
        assert response.json()["detail"]["error"]["type"] == "workspace_not_ready"
    for response in (write_while_requeued, write_while_preparing):
        assert response.status_code == 409
        assert response.json()["detail"]["error"]["type"] == "workspace_not_ready"
    for response in (
        create,
        write_before_retry,
        write_after_retry,
        resize,
        listed,
        killed_once,
        killed_twice,
    ):
        assert response.status_code == 200
    assert create.json()["state"] == "running"
    assert write_before_retry.json()["state"] == "running"
    assert write_after_retry.json()["state"] == "running"
    assert resize.json()["rows"] == 30
    assert resize.json()["cols"] == 100
    assert listed.status_code == 200
    assert listed.json() == {"terminals": [_terminal_session_descriptor()]}
    assert killed_once.json()["state"] == "closed"
    assert killed_twice.json()["state"] == "closed"
    assert broker.calls == [
        ("create", job_id, 24, 80),
        ("input", job_id, "term_1", "bHMK"),
        ("input", job_id, "term_1", "cHdkCg=="),
        ("resize", job_id, "term_1", 30, 100),
        ("list", job_id),
        ("delete", job_id, "term_1"),
        ("delete", job_id, "term_1"),
    ]


async def test_worker_suspends_terminals_around_protected_workspace_phases(
    store: FakeAgentJobStore, monkeypatch
):
    """Preparation and capture pause PTYs; execution and settled jobs resume them."""
    broker = _TerminalSessionBroker()
    monkeypatch.setattr(agent_jobs_router, "workspace_broker_from_env", lambda: broker)
    app = _build_app(store)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as local_client:
        job_id = await _create_job(local_client)
        claim = await local_client.post(
            "/v1/agent/worker/claim",
            json={"worker_id": "worker-1"},
        )
        auth = {"Authorization": f"Bearer {claim.json()['worker_token']}"}

        preparing = await local_client.post(
            f"/v1/agent/worker/jobs/{job_id}/terminals/suspend",
            json={"phase": "workspace_preparing"},
            headers=auth,
        )
        resumed = await local_client.post(
            f"/v1/agent/worker/jobs/{job_id}/terminals/resume",
            headers=auth,
        )
        input_during_run = await local_client.post(
            f"/v1/agent/jobs/{job_id}/terminals/term_1/input",
            json={"data": "eA=="},
        )
        finalizing = await local_client.post(
            f"/v1/agent/worker/jobs/{job_id}/terminals/suspend",
            json={"phase": "workspace_finalizing"},
            headers=auth,
        )
        input_during_capture = await local_client.post(
            f"/v1/agent/jobs/{job_id}/terminals/term_1/input",
            json={"data": "eA=="},
        )
        finished = await local_client.post(
            f"/v1/agent/worker/jobs/{job_id}/finish",
            json={"state": "succeeded"},
            headers=auth,
        )
        input_after_finish = await local_client.post(
            f"/v1/agent/jobs/{job_id}/terminals/term_1/input",
            json={"data": "eA=="},
        )

    assert preparing.status_code == 200
    assert resumed.status_code == 200
    assert input_during_run.status_code == 200
    assert finalizing.status_code == 200
    assert input_during_capture.status_code == 409
    assert finished.status_code == 200
    assert input_after_finish.status_code == 200
    assert broker.calls == [
        ("suspend_all", job_id, 1),
        ("resume_all", job_id, 1),
        ("input", job_id, "term_1", "eA=="),
        ("suspend_all", job_id, 1),
        ("resume_all", job_id, 1),
        ("input", job_id, "term_1", "eA=="),
    ]


async def test_settled_terminal_access_replays_durable_resume_after_restart(
    store: FakeAgentJobStore,
    monkeypatch,
):
    """The first owner access repairs a persisted resume missed before restart."""
    broker = _TerminalSessionBroker()
    monkeypatch.setattr(agent_jobs_router, "workspace_broker_from_env", lambda: broker)
    monkeypatch.setattr(terminal_coordination, "workspace_broker_from_env", lambda: broker)
    app = _build_app(store)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as local_client:
        job_id = await _create_job(local_client)
        store.jobs[job_id]["state"] = "cancelled"
        store.jobs[job_id]["terminal_resume_pending"] = True
        response = await local_client.get(f"/v1/agent/jobs/{job_id}/terminals")

    assert response.status_code == 200
    assert broker.calls == [
        ("resume_settled", job_id),
        ("list", job_id),
    ]
    assert store.jobs[job_id]["terminal_resume_pending"] is False


async def test_worker_re_suspends_terminals_if_ready_event_loses_lease(
    store: FakeAgentJobStore,
    monkeypatch,
):
    """A lease expiring during broker resume cannot expose background processes."""
    broker = _TerminalSessionBroker()
    monkeypatch.setattr(agent_jobs_router, "workspace_broker_from_env", lambda: broker)
    app = _build_app(store)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as local_client:
        job_id = await _create_job(local_client)
        claim = await local_client.post(
            "/v1/agent/worker/claim",
            json={"worker_id": "worker-1"},
        )
        auth = {"Authorization": f"Bearer {claim.json()['worker_token']}"}
        await local_client.post(
            f"/v1/agent/worker/jobs/{job_id}/terminals/suspend",
            json={"phase": "workspace_preparing"},
            headers=auth,
        )

        async def lose_lease_before_ready(**_kwargs):
            store.live_fence = None
            return None

        monkeypatch.setattr(store, "append_event", lose_lease_before_ready)
        response = await local_client.post(
            f"/v1/agent/worker/jobs/{job_id}/terminals/resume",
            headers=auth,
        )

    assert response.status_code == 409
    assert response.json()["detail"]["error"]["type"] == "lease_lost"
    assert broker.calls == [
        ("suspend_all", job_id, 1),
        ("resume_all", job_id, 1),
        ("suspend_all", job_id, 1),
    ]


async def test_worker_re_suspends_terminals_if_finish_loses_lease(
    store: FakeAgentJobStore,
    monkeypatch,
):
    """A failed terminal transition compensates after broker resume."""
    broker = _TerminalSessionBroker()
    monkeypatch.setattr(agent_jobs_router, "workspace_broker_from_env", lambda: broker)
    app = _build_app(store)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as local_client:
        job_id = await _create_job(local_client)
        claim = await local_client.post(
            "/v1/agent/worker/claim",
            json={"worker_id": "worker-1"},
        )
        auth = {"Authorization": f"Bearer {claim.json()['worker_token']}"}
        await local_client.post(
            f"/v1/agent/worker/jobs/{job_id}/terminals/suspend",
            json={"phase": "workspace_finalizing"},
            headers=auth,
        )

        async def lose_lease_before_finish(**_kwargs):
            store.live_fence = None
            return False

        monkeypatch.setattr(store, "transition", lose_lease_before_finish)
        response = await local_client.post(
            f"/v1/agent/worker/jobs/{job_id}/finish",
            json={"state": "succeeded"},
            headers=auth,
        )

    assert response.status_code == 409
    assert response.json()["detail"]["error"]["type"] == "lease_lost"
    assert broker.calls == [
        ("suspend_all", job_id, 1),
        ("resume_all", job_id, 1),
        ("suspend_all", job_id, 1),
    ]


async def test_terminal_readiness_ignores_ready_event_from_superseded_attempt(
    store: FakeAgentJobStore, monkeypatch
):
    """A retry must finish its own workspace preparation before terminals open."""
    broker = _TerminalSessionBroker()
    monkeypatch.setattr(agent_jobs_router, "workspace_broker_from_env", lambda: broker)
    app = _build_app(store)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as local_client:
        job_id = await _create_job(local_client)
        claimed = await store.claim_job(worker_id="worker-1", lease_ttl_seconds=60)
        assert claimed is not None
        event_id = await store.append_event(
            attempt_id=claimed["attempt_id"],
            lease_generation=claimed["lease_generation"],
            event_type="lifecycle",
            payload={"phase": "workspace_ready"},
        )
        assert event_id is not None
        store.events.extend(
            {
                "id": event_id + offset,
                "job_id": job_id,
                "attempt_id": claimed["attempt_id"],
                "seq": event_id + offset,
                "event_type": "message",
                "payload": {"text": "old attempt noise"},
                "created_at": None,
            }
            for offset in range(1, agent_jobs_router._EVENT_PAGE_SIZE)
        )
        store._next_event_id = agent_jobs_router._EVENT_PAGE_SIZE + 1

        store.jobs[job_id]["state"] = "queued"
        while_requeued = await local_client.post(
            f"/v1/agent/jobs/{job_id}/terminals",
            json={"rows": 24, "cols": 80},
        )

        store.jobs[job_id]["state"] = "running"
        store.jobs[job_id]["current_attempt_id"] = 101
        store.live_fence = (101, 2)
        before_retry_checkout = await local_client.post(
            f"/v1/agent/jobs/{job_id}/terminals",
            json={"rows": 24, "cols": 80},
        )

        retry_event_id = await store.append_event(
            attempt_id=101,
            lease_generation=2,
            event_type="lifecycle",
            payload={"phase": "workspace_ready"},
        )
        assert retry_event_id is not None
        after_retry_checkout = await local_client.post(
            f"/v1/agent/jobs/{job_id}/terminals",
            json={"rows": 24, "cols": 80},
        )

    for response in (while_requeued, before_retry_checkout):
        assert response.status_code == 409
        assert response.json()["detail"]["error"]["type"] == "workspace_not_ready"
    assert after_retry_checkout.status_code == 200
    assert broker.calls == [("create", job_id, 24, 80)]


async def test_terminal_routes_recheck_entitlement_before_contacting_broker(
    store: FakeAgentJobStore, monkeypatch
):
    """An owned legacy row is not enough to access a now-unentitled worktree."""
    job = await store.create_job(
        user_id=_OWNER,
        repo="someone/private",
        task_prompt="old task",
        runtime="claude-code",
        model="glm-5.1",
    )
    job["state"] = "succeeded"
    broker = _TerminalSessionBroker()
    monkeypatch.setattr(agent_jobs_router, "workspace_broker_from_env", lambda: broker)
    app = _build_app(store)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as local_client:
        response = await local_client.post(
            f"/v1/agent/jobs/{job['id']}/terminals",
            json={"rows": 24, "cols": 80},
        )

    assert response.status_code == 403
    assert response.json()["detail"]["error"]["type"] == "repo_not_allowed"
    assert broker.calls == []


async def test_terminal_routes_report_missing_broker_and_workspace_as_conflicts(
    store: FakeAgentJobStore, monkeypatch
):
    """Legacy deployments and pre-workspace jobs get actionable 409 responses."""
    from serving.agent_jobs.workspace_broker_client import WorkspaceBrokerError

    app = _build_app(store)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as local_client:
        job_id = await _create_job(local_client)
        store.jobs[job_id]["state"] = "succeeded"
        monkeypatch.setattr(agent_jobs_router, "workspace_broker_from_env", lambda: None)
        missing_broker = await local_client.post(
            f"/v1/agent/jobs/{job_id}/terminals",
            json={"rows": 24, "cols": 80},
        )

        class MissingWorkspaceBroker(_TerminalSessionBroker):
            async def create_terminal(self, workspace_id: str, *, rows: int, cols: int):
                raise WorkspaceBrokerError(404, "workspace is not materialized")

        monkeypatch.setattr(
            agent_jobs_router,
            "workspace_broker_from_env",
            lambda: MissingWorkspaceBroker(),
        )
        missing_workspace = await local_client.post(
            f"/v1/agent/jobs/{job_id}/terminals",
            json={"rows": 24, "cols": 80},
        )

    assert missing_broker.status_code == 409
    assert missing_broker.json()["detail"]["error"]["type"] == "workspace_unavailable"
    assert missing_workspace.status_code == 409
    assert missing_workspace.json()["detail"]["error"]["type"] == "workspace_unavailable"


@pytest.mark.parametrize(
    ("path", "method", "body"),
    [
        ("terminals", "post", {"rows": 1, "cols": 80}),
        ("terminals", "post", {"rows": 24, "cols": 501}),
        ("terminals/term_1/input", "post", {"data": "not base64"}),
        (
            "terminals/term_1/input",
            "post",
            {"data": base64.b64encode(b"x" * (64 * 1024 + 1)).decode()},
        ),
        ("terminals/term_1/resize", "post", {"rows": 201, "cols": 80}),
        ("terminals/term_1/resize", "post", {"rows": 24, "cols": 19}),
        ("terminals/term_1/stream?after=-1", "get", None),
        (f"terminals/term_1/stream?after={2**63}", "get", None),
    ],
)
async def test_interactive_terminal_request_bounds(
    client: AsyncClient, path: str, method: str, body: dict[str, Any] | None
):
    """PTY dimensions, input bytes, and replay cursors are bounded at the gateway."""
    job_id = await _create_job(client)
    url = f"/v1/agent/jobs/{job_id}/{path}"
    if body is None:
        response = await getattr(client, method)(url)
    else:
        response = await getattr(client, method)(url, json=body)

    assert response.status_code == 422


@pytest.mark.parametrize(
    "unsafe_path",
    [
        "../secret",
        "/etc/passwd",
        "C:/Windows",
        r"dir\file",
        ".GIT/config",
        "a//b",
        "nul\x00path",
    ],
)
async def test_workspace_files_reject_path_traversal(store: FakeAgentJobStore, unsafe_path: str):
    """Decoded traversal and platform-specific aliases fail before GitHub is called."""
    from serving.servers.deps import get_agent_app_credentials

    app_creds = _WorkspaceContentsApp({"": []})
    app = _build_app(store)
    app.dependency_overrides[get_agent_app_credentials] = lambda: app_creds
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as local_client:
        job_id = await _create_job(local_client)
        store.jobs[job_id]["base_sha"] = "b" * 40
        response = await local_client.get(
            f"/v1/agent/jobs/{job_id}/files", params={"path": unsafe_path}
        )

    assert response.status_code == 400
    assert response.json()["detail"]["error"]["type"] == "invalid_path"
    assert app_creds.calls == []


async def test_workspace_files_recheck_repository_entitlement(store: FakeAgentJobStore):
    """Owning a legacy row does not entitle its repository after access is revoked."""
    from serving.servers.deps import get_agent_app_credentials

    foreign_repo_job = await store.create_job(
        user_id=_OWNER,
        repo="someone/private",
        task_prompt="old task",
        runtime="claude-code",
        model="glm-5.1",
        base_sha="c" * 40,
    )
    app_creds = _WorkspaceContentsApp({"": []})
    app = _build_app(store)
    app.dependency_overrides[get_agent_app_credentials] = lambda: app_creds
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as local_client:
        response = await local_client.get(f"/v1/agent/jobs/{foreign_repo_job['id']}/files")

    assert response.status_code == 403
    assert app_creds.calls == []


async def test_cancel_queued_job(client: AsyncClient):
    """Cancelling a queued job reports the terminal state immediately."""
    job_id = await _create_job(client)
    response = await client.post(f"/v1/agent/jobs/{job_id}/cancel")
    assert response.status_code == 200
    assert response.json()["state"] == "cancelled"
    assert response.json()["cancel_requested"] is True


async def test_cancel_requeued_job_authoritatively_resumes_terminals(
    store: FakeAgentJobStore,
    monkeypatch,
):
    """Cancellation must not strand sessions paused by an expired attempt."""
    broker = _TerminalSessionBroker()
    monkeypatch.setattr(terminal_coordination, "workspace_broker_from_env", lambda: broker)
    app = _build_app(store)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as local_client:
        job_id = await _create_job(local_client)
        store.jobs[job_id]["current_attempt_id"] = 100
        response = await local_client.post(f"/v1/agent/jobs/{job_id}/cancel")

    assert response.status_code == 200
    assert response.json()["state"] == "cancelled"
    assert broker.calls == [("resume_settled", job_id)]
    assert store.jobs[job_id]["terminal_resume_pending"] is False


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


async def test_dedicated_dispatcher_token_opens_exactly_one_door(
    store: FakeAgentJobStore, monkeypatch
):
    """AGENT_DISPATCHER_TOKEN claims work and does nothing else.

    The runner host executes untrusted repository code next door; the
    credential it holds should open the claim endpoint, not the admin surface
    — which is what handing it ADMIN_TOKEN did. The dedicated token must be
    accepted at claim, rejected when wrong, and be an ordinary invalid
    credential everywhere else.
    """
    monkeypatch.setenv("AGENT_DISPATCHER_TOKEN", "dispatch-secret-1")
    await store.create_job(
        user_id=_OWNER,
        repo="owner/name",
        task_prompt="fix it",
        runtime="claude-code",
        model="glm-5.1",
    )

    # dispatcher=False leaves the real gate in place, so the env token is what
    # is being exercised — not a test override.
    app = _build_app(store, dispatcher=False)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        wrong = await client.post(
            "/v1/agent/worker/claim",
            json={"worker_id": "w1"},
            headers={"Authorization": "Bearer not-the-token"},
        )
        assert wrong.status_code in (401, 403)

        claimed = await client.post(
            "/v1/agent/worker/claim",
            json={"worker_id": "w1"},
            headers={"Authorization": "Bearer dispatch-secret-1"},
        )
        assert claimed.status_code == 200
        assert claimed.json()["repo"] == "owner/name"

    # On the owner surface the same value is just an unknown API key: the
    # only authenticator that recognizes it is the claim gate. (The base fake
    # store validates any key, which would mask exactly this property.)
    from serving.servers.deps import get_operational_store

    class _NoSuchKey(FakeOwnerAuthStore):
        async def get_auth_context_by_key_hash(self, _key_hash: str) -> None:
            return None

    owner_app = _build_app(store, dispatcher=False)
    owner_app.dependency_overrides.pop(agent_jobs_router.authenticate_agent_owner)
    owner_app.dependency_overrides[get_operational_store] = lambda: _NoSuchKey()
    transport = ASGITransport(app=owner_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/v1/agent/jobs",
            headers={"Authorization": "Bearer dispatch-secret-1"},
        )
    assert response.status_code in (401, 403)


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

    Agent owner auth delegates sandbox-token-shaped credentials to
    verify_api_key, whose inference-path allowlist must keep them away from the
    control plane. Resolving one as its owner would let the sandbox enumerate,
    cancel, or create that owner's other jobs — the authority the model scope
    exists to withhold.
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


async def test_a_worker_cannot_write_the_publish_record(client: AsyncClient):
    """Publishing is the platform's, not the sandbox runner's.

    These endpoints had no production caller — the runner leaves publishing to
    the platform and the publisher uses its own claim path — while letting a
    worker token write `published_pr_url` from an arbitrary string. That marked
    the job succeeded, showed the owner that URL, and made the real publisher
    skip the job forever (it only claims rows where the URL is null), so the
    patch was never gated or pushed.
    """
    job_id = await _create_job(client)
    claim = await client.post("/v1/agent/worker/claim", json={"worker_id": "w1"})
    auth = {"Authorization": f"Bearer {claim.json()['worker_token']}"}

    for path, body in (
        (f"/v1/agent/worker/jobs/{job_id}/publish/begin", None),
        (f"/v1/agent/worker/jobs/{job_id}/publish/complete", {"pr_url": "https://evil/pr/1"}),
    ):
        response = await client.post(path, json=body, headers=auth)
        assert response.status_code == 404, f"{path} must not exist"


class _FakeGitHubApp:
    """Stands in for the App: a user token maps to installations, which cover repos."""

    def __init__(self, installations, repos_by_installation, *, exchange_fails=False):
        self.installations = installations
        self.repos = repos_by_installation
        self.exchange_fails = exchange_fails
        self.exchanged: list[str] = []

    async def exchange_user_code(self, code: str) -> str:
        if self.exchange_fails:
            raise RuntimeError("GitHub refused the code exchange")
        self.exchanged.append(code)
        return f"user-token-for-{code}"

    async def installations_for_user(self, user_token: str) -> list[dict[str, Any]]:
        return self.installations

    async def repositories_for_installation(self, installation_id: int) -> list[str]:
        return self.repos.get(installation_id, [])

    async def token_for(self, repo: str, **kwargs: Any) -> str:
        return "ghs_x"


def _store_with_grants(store: FakeAgentJobStore) -> FakeAgentJobStore:
    """Give the fake store the grant surface the router uses."""
    store.grants = {}
    store.consumed_oauth_states = set()

    async def record_repo_grant(*, user_id, installation_id, account_login=None):
        store.grants.setdefault(user_id, {})[installation_id] = account_login

    async def list_repo_grants(*, user_id):
        return [
            {"installation_id": iid, "account_login": login}
            for iid, login in store.grants.get(user_id, {}).items()
        ]

    async def revoke_repo_grant(*, user_id, installation_id):
        return store.grants.get(user_id, {}).pop(installation_id, "missing") != "missing"

    async def consume_oauth_state(*, state_hash, user_id, provider):
        assert user_id == "user-owner"
        assert provider == "github"
        if state_hash in store.consumed_oauth_states:
            return None
        store.consumed_oauth_states.add(state_hash)
        return {"code_verifier_ciphertext": None}

    store.record_repo_grant = record_repo_grant
    store.list_repo_grants = list_repo_grants
    store.revoke_repo_grant = revoke_repo_grant
    store.consume_oauth_state = consume_oauth_state
    return store


async def test_connecting_records_only_what_github_attests(store: FakeAgentJobStore, monkeypatch):
    """The entitlement must come from GitHub's answer, not the browser's claim.

    The browser sends a code; the platform exchanges it for a token that speaks
    as that user and asks GitHub which installations they can reach. An
    installation id posted directly would be exactly the confused deputy this
    path exists to prevent.
    """
    monkeypatch.delenv("AGENT_REPO_ALLOWLIST", raising=False)
    from serving.servers.deps import get_agent_app_credentials

    app_creds = _FakeGitHubApp(
        installations=[{"installation_id": 77, "account_login": "acme"}],
        repos_by_installation={77: ["acme/service", "acme/web"]},
    )
    app = _build_app(_store_with_grants(store))
    app.dependency_overrides[get_agent_app_credentials] = lambda: app_creds
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        connected = await client.post(
            "/v1/agent/github/connect",
            json={"code": "abc123", "state": "test-state-value-123456"},
        )
        config = await client.get("/v1/agent/config")

    assert connected.status_code == 200
    assert app_creds.exchanged == ["abc123"], "the code must be exchanged, not trusted"
    assert connected.json()["connections"][0]["installation_id"] == 77
    # The picker now offers what this user actually connected.
    assert config.json()["repos"] == ["acme/service", "acme/web"]
    assert config.json()["github_connected"] is True


async def test_a_connected_user_may_run_only_their_own_repos(store: FakeAgentJobStore, monkeypatch):
    """Connecting one owner must not entitle someone else's repository."""
    monkeypatch.delenv("AGENT_REPO_ALLOWLIST", raising=False)
    from serving.servers.deps import get_agent_app_credentials

    app_creds = _FakeGitHubApp(
        installations=[{"installation_id": 77, "account_login": "acme"}],
        repos_by_installation={77: ["acme/service"]},
    )
    app = _build_app(_store_with_grants(store))
    app.dependency_overrides[get_agent_app_credentials] = lambda: app_creds
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post(
            "/v1/agent/github/connect",
            json={"code": "abc123", "state": "test-state-value-123456"},
        )
        mine = await client.post(
            "/v1/agent/jobs",
            json={"repo": "acme/service", "task_prompt": "fix it", "model": "m"},
        )
        theirs = await client.post(
            "/v1/agent/jobs",
            json={"repo": "someone-else/private", "task_prompt": "exfiltrate", "model": "m"},
        )

    assert mine.status_code == 201
    assert theirs.status_code == 403
    assert "connected" in theirs.text


async def test_a_failed_exchange_grants_nothing(store: FakeAgentJobStore, monkeypatch):
    """A code that does not verify must leave the user with no entitlement."""
    monkeypatch.delenv("AGENT_REPO_ALLOWLIST", raising=False)
    from serving.servers.deps import get_agent_app_credentials

    app_creds = _FakeGitHubApp(
        installations=[{"installation_id": 77, "account_login": "acme"}],
        repos_by_installation={77: ["acme/service"]},
        exchange_fails=True,
    )
    app = _build_app(_store_with_grants(store))
    app.dependency_overrides[get_agent_app_credentials] = lambda: app_creds
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        connected = await client.post(
            "/v1/agent/github/connect",
            json={"code": "stolen", "state": "test-state-value-123456"},
        )
        config = await client.get("/v1/agent/config")

    assert connected.status_code == 400
    assert config.json()["repos"] == []


async def test_disconnecting_removes_the_entitlement(store: FakeAgentJobStore, monkeypatch):
    """Revoking here is half of it; uninstalling on GitHub is the other half."""
    monkeypatch.delenv("AGENT_REPO_ALLOWLIST", raising=False)
    from serving.servers.deps import get_agent_app_credentials

    app_creds = _FakeGitHubApp(
        installations=[{"installation_id": 77, "account_login": "acme"}],
        repos_by_installation={77: ["acme/service"]},
    )
    app = _build_app(_store_with_grants(store))
    app.dependency_overrides[get_agent_app_credentials] = lambda: app_creds
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post(
            "/v1/agent/github/connect",
            json={"code": "abc123", "state": "test-state-value-123456"},
        )
        await client.delete("/v1/agent/github/connect/77")
        after = await client.post(
            "/v1/agent/jobs",
            json={"repo": "acme/service", "task_prompt": "fix it", "model": "m"},
        )

    assert after.status_code == 403


async def test_a_branch_is_pinned_to_a_commit_at_creation(store: FakeAgentJobStore, monkeypatch):
    """A job records the sha, never the branch name.

    A branch moves. A job that stored "dev" would silently mean a different
    tree by the time it ran, and the publisher applies its patch onto a pinned
    commit — so resolving late would mean generating a diff against one tree
    and pushing it onto another.
    """
    monkeypatch.setenv("AGENT_REPO_ALLOWLIST", "owner/name")
    from serving.servers.deps import get_agent_app_credentials

    class BranchApp(_FakeGitHubApp):
        async def resolve_ref(self, repo: str, ref: str) -> str:
            assert ref == "dev"
            return "f" * 40

    app_creds = BranchApp(installations=[], repos_by_installation={})
    app = _build_app(_store_with_grants(store))
    app.dependency_overrides[get_agent_app_credentials] = lambda: app_creds
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post(
            "/v1/agent/jobs",
            json={
                "repo": "owner/name",
                "task_prompt": "fix it",
                "model": "m",
                "base_ref": "dev",
                "metadata": {"_agent_base_ref": "spoofed", "ticket": "ABC-1"},
            },
        )

    assert created.status_code == 201
    assert created.json()["base_sha"] == "f" * 40
    assert created.json()["base_ref"] == "dev"
    assert created.json()["output_branch"].startswith("agent/")
    assert created.json()["metadata"] == {"ticket": "ABC-1"}


async def test_reserved_base_ref_metadata_cannot_invent_a_branch(client: AsyncClient):
    """Without base_ref, caller metadata must not forge the Git panel's base branch."""
    created = await client.post(
        "/v1/agent/jobs",
        json={
            "repo": "owner/name",
            "task_prompt": "fix it",
            "model": "m",
            "metadata": {"_agent_base_ref": "forged"},
        },
    )

    assert created.status_code == 201
    assert created.json()["base_ref"] is None
    assert created.json()["metadata"] is None


async def test_an_unresolvable_branch_is_refused_at_creation(store: FakeAgentJobStore, monkeypatch):
    """Better a clear 400 than a job queued against a ref that does not exist."""
    monkeypatch.setenv("AGENT_REPO_ALLOWLIST", "owner/name")
    from serving.servers.deps import get_agent_app_credentials

    class BrokenBranchApp(_FakeGitHubApp):
        async def resolve_ref(self, repo: str, ref: str) -> str:
            raise RuntimeError("no such branch")

    app = _build_app(_store_with_grants(store))
    app.dependency_overrides[get_agent_app_credentials] = lambda: BrokenBranchApp(
        installations=[], repos_by_installation={}
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post(
            "/v1/agent/jobs",
            json={
                "repo": "owner/name",
                "task_prompt": "fix it",
                "model": "m",
                "base_ref": "nope",
            },
        )

    assert created.status_code == 400
    assert "nope" in created.text


async def test_branches_are_only_listed_for_an_entitled_repo(store: FakeAgentJobStore, monkeypatch):
    """This reads through the platform's installation, so it needs the same gate.

    Without it, the endpoint enumerates branches of any repository the App
    happens to cover — which is the confused deputy again, one level down.
    """
    monkeypatch.setenv("AGENT_REPO_ALLOWLIST", "owner/name")
    from serving.servers.deps import get_agent_app_credentials

    class BranchApp(_FakeGitHubApp):
        async def branches_for_repo(self, repo: str) -> dict[str, Any]:
            return {"default": "main", "branches": ["main", "dev"]}

    app = _build_app(_store_with_grants(store))
    app.dependency_overrides[get_agent_app_credentials] = lambda: BranchApp(
        installations=[], repos_by_installation={}
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        mine = await client.get("/v1/agent/branches", params={"repo": "owner/name"})
        theirs = await client.get("/v1/agent/branches", params={"repo": "someone/private"})

    assert mine.status_code == 200
    assert mine.json()["default"] == "main"
    assert theirs.status_code == 403


# ── Runner host pool ───────────────────────────────────────────────────
#
# Runners pull, so the claim is the only place the platform can say "not this
# machine". These cover that gate from the wire in, because everything the
# admin switch promises reduces to what /worker/claim answers.


async def _queue_one(store: FakeAgentJobStore) -> str:
    job = await store.create_job(
        user_id="u1",
        repo="owner/name",
        task_prompt="fix it",
        runtime="claude-code",
        model="glm-5.1",
    )
    return job["id"]


async def test_claim_is_unrestricted_until_a_host_is_pinned(client, store):
    """The compatible default: an unpinned pool behaves as it always has."""
    job_id = await _queue_one(store)

    claim = await client.post(
        "/v1/agent/worker/claim", json={"worker_id": "w1", "host": "runner-a"}
    )

    assert claim.status_code == 200
    assert claim.json()["job_id"] == job_id


async def test_a_host_joins_the_pool_even_while_it_is_being_turned_away(client, store):
    """The chicken-and-egg case: you cannot switch to a host you cannot see.

    A new machine polls, is refused because another host is pinned, and must
    still appear in the pool — otherwise it could only become visible once it
    was already active, and no operator could ever pick it.
    """
    await store.touch_runner_host(host="runner-a", worker_id="w0")
    await store.set_active_runner_host(host="runner-a")
    await _queue_one(store)

    refused = await client.post(
        "/v1/agent/worker/claim", json={"worker_id": "w2", "host": "runner-b"}
    )

    assert refused.status_code == 200
    assert refused.json() is None
    assert "runner-b" in store.runner_hosts


async def test_pinning_moves_new_work_to_the_named_host(client, store):
    # Both machines join the pool by polling an empty queue, which is how a
    # newly added host becomes selectable in the first place.
    await client.post("/v1/agent/worker/claim", json={"worker_id": "w1", "host": "runner-a"})
    await client.post("/v1/agent/worker/claim", json={"worker_id": "w2", "host": "runner-b"})
    await store.set_active_runner_host(host="runner-b")
    job_id = await _queue_one(store)

    wrong_host = await client.post(
        "/v1/agent/worker/claim", json={"worker_id": "w1", "host": "runner-a"}
    )
    right_host = await client.post(
        "/v1/agent/worker/claim", json={"worker_id": "w2", "host": "runner-b"}
    )

    assert wrong_host.json() is None
    assert right_host.json()["job_id"] == job_id


async def test_a_runner_reporting_no_host_is_refused_while_one_is_pinned(client, store):
    """Fail closed. A runner left on the old machine must not keep claiming.

    Runners that predate host reporting send no host at all; treating that as
    "allowed" would leave the machine an operator just switched away from still
    taking jobs, which makes the switch a lie.
    """
    await client.post("/v1/agent/worker/claim", json={"worker_id": "w2", "host": "runner-b"})
    await store.set_active_runner_host(host="runner-b")
    await _queue_one(store)

    legacy = await client.post("/v1/agent/worker/claim", json={"worker_id": "w-old"})

    assert legacy.status_code == 200
    assert legacy.json() is None


async def test_claim_rejects_a_host_that_is_not_a_hostname(client, store):
    """The field is a primary key an admin reads off a page and clicks."""
    await _queue_one(store)

    response = await client.post(
        "/v1/agent/worker/claim",
        json={"worker_id": "w1", "host": "runner-a; DROP TABLE agent_jobs"},
    )

    assert response.status_code == 422
    assert store.runner_hosts == {}
