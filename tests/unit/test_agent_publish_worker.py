"""Unit tests for the publish step (patch → branch → draft PR).

This is the hop that produces the visible product outcome, and the one where a
mistake is externally visible, so the cases below are the ones that would show
up in someone's repository: double-publishing, publishing a blocked patch, and
opening a PR that is not a draft.
"""

from __future__ import annotations

import contextlib
from typing import Any

import httpx
import pytest

from serving.agent_jobs import publish_worker
from serving.agent_jobs.publish_worker import (
    GitHubCredential,
    create_draft_pull_request,
    publish_loop,
    publish_one,
)
from serving.agent_jobs.publisher import PublishError, PublishResult

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _entitled(monkeypatch):
    """Entitle the fixture repository.

    Publishing now re-checks entitlement before minting a write credential, so
    a test that does not declare one is correctly refused.
    """
    monkeypatch.setenv("AGENT_REPO_ALLOWLIST", "o/n")


_JOB = {
    "job_id": "ajob_1",
    "user_id": "u1",
    "repo": "o/n",
    "base_sha": "abc123",
    "task_prompt": "fix the flaky test",
    "patch": "diff --git a/x b/x\n",
}


class FakeStore:
    """Records what the publisher did to the job."""

    def __init__(self, job: dict[str, Any] | None = None, *, record_ok: bool = True) -> None:
        self._job = job
        self.claims = 0
        self.recorded: tuple[str, str] | None = None
        self.failed: tuple[str, str] | None = None
        self._record_ok = record_ok

    async def claim_for_publish(self):
        self.claims += 1
        job, self._job = self._job, None
        return job

    async def record_publish(self, *, job_id: str, pr_url: str) -> bool:
        self.recorded = (job_id, pr_url)
        return self._record_ok

    async def fail_publish(self, *, job_id: str, detail: str) -> None:
        self.failed = (job_id, detail)


def _patch_publish(monkeypatch, result_or_exc):
    """Stand in for the git-side publisher."""

    def fake(**kwargs):
        if isinstance(result_or_exc, Exception):
            raise result_or_exc
        return result_or_exc

    monkeypatch.setattr(publish_worker, "publish_patch", fake)


def _patch_pr(monkeypatch, url: str = "https://github.com/o/n/pull/7"):
    """Stand in for the GitHub API call, capturing its arguments."""
    captured: dict[str, Any] = {}

    async def fake(**kwargs):
        captured.update(kwargs)
        return url

    monkeypatch.setattr(publish_worker, "create_draft_pull_request", fake)
    return captured


async def test_empty_queue_is_a_no_op():
    """Nothing pending means nothing happens."""
    store = FakeStore(None)
    assert await publish_one(store, credential=GitHubCredential("t")) is None
    assert store.recorded is None and store.failed is None


async def test_successful_publish_records_the_pr(monkeypatch):
    """A clean patch becomes a branch, a draft PR, and a recorded URL."""
    store = FakeStore(dict(_JOB))
    _patch_publish(
        monkeypatch, PublishResult(branch="agent/ajob_1", commit_sha="s", changed_files=["x"])
    )
    captured = _patch_pr(monkeypatch)

    url = await publish_one(store, credential=GitHubCredential("t"), base_branch="dev")
    assert url == "https://github.com/o/n/pull/7"
    assert store.recorded == ("ajob_1", url)
    assert captured["head_branch"] == "agent/ajob_1"
    assert captured["base_branch"] == "dev"
    assert "ajob_1" in captured["body"]


async def test_gate_rejection_fails_the_job_and_never_pushes(monkeypatch):
    """A blocked patch is reported to the owner, not retried into the repo."""
    store = FakeStore(dict(_JOB))
    _patch_publish(monkeypatch, PublishError("patch rejected: modifies .github/"))
    pr_called = _patch_pr(monkeypatch)

    assert await publish_one(store, credential=GitHubCredential("t")) is None
    assert store.recorded is None
    assert store.failed[0] == "ajob_1"
    assert ".github/" in store.failed[1]
    assert pr_called == {}, "a rejected patch must never reach PR creation"


async def test_missing_base_sha_fails_instead_of_guessing(monkeypatch):
    """Without a base commit the patch has no defined target — refuse."""
    job = dict(_JOB)
    job["base_sha"] = None
    store = FakeStore(job)
    pr_called = _patch_pr(monkeypatch)

    assert await publish_one(store, credential=GitHubCredential("t")) is None
    assert "base_sha" in store.failed[1]
    assert pr_called == {}


async def test_lost_race_does_not_claim_success(monkeypatch):
    """If another publisher recorded first, report nothing rather than a dupe."""
    store = FakeStore(dict(_JOB), record_ok=False)
    _patch_publish(
        monkeypatch, PublishResult(branch="agent/ajob_1", commit_sha="s", changed_files=["x"])
    )
    _patch_pr(monkeypatch)

    assert await publish_one(store, credential=GitHubCredential("t")) is None


async def test_unexpected_failure_is_recorded_not_swallowed(monkeypatch):
    """An unexpected error still surfaces on the job instead of vanishing."""
    store = FakeStore(dict(_JOB))
    _patch_publish(monkeypatch, RuntimeError("disk full"))
    assert await publish_one(store, credential=GitHubCredential("t")) is None
    assert "disk full" in store.failed[1]


async def test_pull_request_is_always_a_draft():
    """The PR must be a draft — human review is the safety story."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(dict(request.headers))
        import json as _json

        captured["body"] = _json.loads(request.content)
        return httpx.Response(201, json={"html_url": "https://github.com/o/n/pull/9"})

    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient

    class PatchedClient(original):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = transport
            super().__init__(*args, **kwargs)

    httpx.AsyncClient = PatchedClient
    try:
        url = await create_draft_pull_request(
            repo="o/n",
            head_branch="agent/ajob_1",
            base_branch="dev",
            title="t",
            body="b",
            credential=GitHubCredential("secret-token"),
        )
    finally:
        httpx.AsyncClient = original

    assert url == "https://github.com/o/n/pull/9"
    assert captured["body"]["draft"] is True
    assert captured["body"]["head"] == "agent/ajob_1"


async def test_github_error_becomes_a_publish_error():
    """A refused PR is an explicit failure, not a silent success."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, text="head branch does not exist")

    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient

    class PatchedClient(original):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = transport
            super().__init__(*args, **kwargs)

    httpx.AsyncClient = PatchedClient
    try:
        with pytest.raises(PublishError):
            await create_draft_pull_request(
                repo="o/n",
                head_branch="agent/x",
                base_branch="dev",
                title="t",
                body="b",
                credential=GitHubCredential("t"),
            )
    finally:
        httpx.AsyncClient = original


async def test_credential_never_appears_in_the_pr_body(monkeypatch):
    """The GitHub token must not leak into anything the PR renders."""
    store = FakeStore(dict(_JOB))
    _patch_publish(
        monkeypatch, PublishResult(branch="agent/ajob_1", commit_sha="s", changed_files=["x"])
    )
    captured = _patch_pr(monkeypatch)
    await publish_one(store, credential=GitHubCredential("super-secret-token"))
    assert "super-secret-token" not in captured["body"]
    assert "super-secret-token" not in captured["title"]


async def test_app_only_deployment_actually_publishes(monkeypatch):
    """An App-configured deployment with no static token must still publish.

    Exercised through publish_loop, not publish_one: the bug lived in the loop,
    which skipped the entire tick when the static-token provider returned None
    and never passed app_credentials down. The recommended configuration —
    GitHub App, no static token — therefore produced patches forever and never
    a single PR, silently. A test that called publish_one directly would have
    passed against the broken loop.
    """
    import asyncio

    store = FakeStore(dict(_JOB))
    _patch_publish(
        monkeypatch, PublishResult(branch="agent/ajob_1", commit_sha="s", changed_files=["x"])
    )
    _patch_pr(monkeypatch)

    class FakeApp:
        def __init__(self) -> None:
            self.scoped: bool | None = None

        async def token_for(self, repo: str, **kwargs) -> str:
            self.scoped = kwargs.get("repository_scoped")
            return "ghs_from_app"

    app = FakeApp()

    task = asyncio.create_task(
        publish_loop(
            store,
            credential_provider=lambda: None,  # no static token configured
            app_credentials=app,
            interval_seconds=0.01,
        )
    )
    for _ in range(200):
        await asyncio.sleep(0.01)
        if store.recorded:
            break
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert store.recorded == ("ajob_1", "https://github.com/o/n/pull/7")
    # And the token it published with is scoped to this repository, not to
    # everything the App happens to be installed on.
    assert app.scoped is True


async def test_no_credential_at_all_fails_the_job_rather_than_hanging(monkeypatch):
    """With neither source configured the job says so instead of stalling."""
    store = FakeStore(dict(_JOB))
    assert await publish_one(store, credential=None, app_credentials=None) is None
    assert store.failed is not None
    assert "credential" in store.failed[1]


async def test_publishing_stops_when_the_entitlement_was_withdrawn(monkeypatch):
    """The write credential must not outlive the permission to use it.

    Publishing happens well after the job ran, and it is the step that asks for
    push authority. Without this the read side would refuse the clone token at
    claim while the same deployment still handed out the push token.
    """
    monkeypatch.setenv("AGENT_REPO_ALLOWLIST", "someone/else")
    store = FakeStore(dict(_JOB))
    minted: list[str] = []

    class App:
        async def token_for(self, repo: str, **kwargs: Any) -> str:
            minted.append(repo)
            return "ghs_should_not_happen"

    assert await publish_one(store, app_credentials=App()) is None
    assert minted == [], "no credential may be minted for a repo no longer entitled"
    assert store.failed is not None
    assert "entitled" in store.failed[1]
