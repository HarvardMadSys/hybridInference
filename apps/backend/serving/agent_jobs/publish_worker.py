"""The publish step: patch → branch → draft PR (issue #1041, P0).

This is the last hop of the chain and the one that produces the visible
product outcome. It runs **in the gateway**, never in the sandbox, for one
reason: it is the only component that holds a GitHub credential. The agent
hands over a patch and nothing else, which is what lets the sandbox stay
credential-free.

Order of operations is load-bearing:

1. Claim one finished job that has a patch and no PR yet. The claim is atomic
   (``SKIP LOCKED`` + ``published_pr_url IS NULL``), so several gateway
   processes can run this loop without racing into duplicate PRs.
2. Validate the patch **before** any bytes touch a worktree — the gate is the
   security boundary, and a blocked ``.github/`` change must never reach a
   branch, because pushing it can execute a workflow with repository secrets
   before a human reads the draft PR.
3. Push the turn onto its conversation's stable ``agent/<thread-id>`` branch
   with a pinned refspec.
4. Open a **draft** PR, so a human reviews before anything can merge.

A rejected patch fails the job with the reason rather than retrying: gate
violations are human-review situations, and a retry loop would either spam the
repository or bury the rejection.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx

from serving.agent_jobs.entitlement import RepoNotAllowed, require_entitled_repo
from serving.agent_jobs.patch_gate import branch_name_for
from serving.agent_jobs.publisher import PublishError, publish_patch
from serving.utils.logging import get_logger

if TYPE_CHECKING:
    from serving.storage.agent_job_store import AgentJobStore

logger = get_logger(__name__)

GITHUB_API = "https://api.github.com"
POLL_INTERVAL_S = 15.0

# Exactly what publishing does: push one branch, open one draft PR. Notably not
# `workflows`, so a patch touching `.github/` cannot be pushed even if both
# gates were bypassed — the credential itself would refuse.
_PUBLISH_SCOPE = {"contents": "write", "pull_requests": "write"}


@dataclass(frozen=True)
class GitHubCredential:
    """How the publisher authenticates to GitHub for one repository.

    ``token`` is expected to be short-lived (a GitHub App installation token,
    which expires in an hour) and scoped to the repositories the user actually
    installed the app on. It stays in this process: it is used to build the
    push URL and the API call, and never reaches a sandbox.
    """

    token: str
    api_base: str = GITHUB_API

    def clone_url(self, repo: str) -> str:
        """Return an authenticated clone URL for ``owner/name``."""
        return f"https://x-access-token:{self.token}@github.com/{repo}.git"


async def create_draft_pull_request(
    *,
    repo: str,
    head_branch: str,
    base_branch: str,
    title: str,
    body: str,
    credential: GitHubCredential,
    timeout_s: float = 30.0,
) -> str:
    """Open a draft PR and return its URL.

    Draft is not a nicety: the whole safety story assumes a human reads the
    change before it can merge, so this must never open a ready-for-review PR.
    """
    async with httpx.AsyncClient(timeout=timeout_s) as client:
        response = await client.post(
            f"{credential.api_base}/repos/{repo}/pulls",
            headers={
                "Authorization": f"Bearer {credential.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            json={
                "title": title,
                "head": head_branch,
                "base": base_branch,
                "body": body,
                "draft": True,
            },
        )
    if response.status_code >= 400:
        raise PublishError(
            f"GitHub refused the draft PR ({response.status_code}): {response.text[:300]}"
        )
    return response.json().get("html_url", "")


def _pr_body(job: dict[str, Any], changed_files: list[str]) -> str:
    """Compose a PR body that tells the reviewer what they are reviewing."""
    files = "\n".join(f"- `{path}`" for path in changed_files[:50])
    if len(changed_files) > 50:
        files += f"\n- …and {len(changed_files) - 50} more"
    return (
        f"Opened by a cloud agent job (`{job['job_id']}`).\n\n"
        f"**Task**\n\n> {job['task_prompt'][:1500]}\n\n"
        f"**Changed files ({len(changed_files)})**\n\n{files}\n\n"
        "---\n"
        "This branch was produced by an agent running in a sandbox with no "
        "repository credential: it emitted a patch, which the platform "
        "validated (workflow-file block, secret scan, path-escape and size "
        "limits) before pushing. It is a draft on purpose — please review "
        "before merging."
    )


async def publish_one(
    store: AgentJobStore,
    *,
    credential: GitHubCredential | None = None,
    app_credentials: Any | None = None,
    base_branch: str = "dev",
    allow_workflow_changes: bool = False,
) -> str | None:
    """Publish at most one pending job. Returns the PR URL, or ``None``.

    Never raises for an individual job: a failure is recorded against that job
    so the owner sees why, and the loop stays alive for the next one.
    """
    job = await store.claim_for_publish()
    if job is None:
        return None

    job_id = job["job_id"]
    base_sha = job["base_sha"]

    # Re-checked here, before any credential is minted. Publishing happens well
    # after the job ran, and it is the step that asks for *write* authority —
    # so an entitlement withdrawn in between (a revoked connection, a narrowed
    # allowlist) has to be able to stop it. The read side already refuses at
    # claim; without this the same deployment would block the read-only token
    # and still hand out the push token.
    try:
        await require_entitled_repo(
            job["repo"],
            job.get("user_id", ""),
            store=store,
            app_credentials=app_credentials,
        )
    except RepoNotAllowed as exc:
        await store.fail_publish(
            job_id=job_id, detail=f"no longer entitled to publish to {job['repo']}: {exc}"
        )
        return None

    # Prefer the App: it mints a token scoped to this repository's
    # installation, valid an hour. A static token is the fallback for a
    # deployment that has not set the App up.
    if app_credentials is not None:
        try:
            # Scoped twice: to this one repository, and to the two permissions
            # publishing actually uses. An installation token inherits every
            # permission the App holds unless it asks for less, so omitting
            # `permissions` here handed the publisher whatever the App could
            # ever do — against a repository the *user* named.
            credential = GitHubCredential(
                await app_credentials.token_for(
                    job["repo"], permissions=_PUBLISH_SCOPE, repository_scoped=True
                )
            )
        except Exception as exc:
            await store.fail_publish(
                job_id=job_id, detail=f"could not obtain a GitHub credential: {exc}"
            )
            return None
    if credential is None:
        await store.fail_publish(
            job_id=job_id, detail="no GitHub credential is configured for publishing"
        )
        return None
    if not base_sha:
        await store.fail_publish(
            job_id=job_id,
            detail="cannot publish: the job has no base_sha to apply the patch onto",
        )
        return None

    try:
        # Blocking git work: keep it off the event loop so a slow clone cannot
        # stall the gateway's request handling.
        result = await asyncio.to_thread(
            publish_patch,
            job_id=job_id,
            branch_id=job.get("thread_id") or job_id,
            patch=job["patch"],
            clone_url=credential.clone_url(job["repo"]),
            base_sha=base_sha,
            commit_message=f"agent: {job['task_prompt'][:60]}".strip(),
            allow_workflow_changes=allow_workflow_changes,
        )
        pr_url = job.get("parent_pr_url")
        if not pr_url:
            pr_url = await create_draft_pull_request(
                repo=job["repo"],
                head_branch=result.branch,
                base_branch=base_branch,
                title=f"[agent] {job['task_prompt'][:70]}".strip(),
                body=_pr_body(job, result.changed_files),
                credential=credential,
            )
    except PublishError as exc:
        logger.warning(
            "agent_job_publish_rejected",
            extra={"event": "agent_job_publish_rejected", "job_id": job_id},
        )
        await store.fail_publish(job_id=job_id, detail=str(exc))
        return None
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception("agent job publish failed unexpectedly")
        await store.fail_publish(job_id=job_id, detail=f"publish failed: {exc}")
        return None

    if not await store.record_publish(job_id=job_id, pr_url=pr_url, commit_sha=result.commit_sha):
        # Someone recorded a PR while we worked. The branch we pushed is
        # harmless (it is the same one), but say so rather than pretend.
        logger.warning(
            "agent_job_publish_raced",
            extra={"event": "agent_job_publish_raced", "job_id": job_id, "pr_url": pr_url},
        )
        return None

    logger.info(
        "agent_job_pr_opened",
        extra={
            "event": "agent_job_pr_opened",
            "job_id": job_id,
            "branch": branch_name_for(job.get("thread_id") or job_id),
            "pr_url": pr_url,
        },
    )
    return pr_url


async def publish_loop(
    store: AgentJobStore,
    *,
    credential_provider: Any = None,
    app_credentials: Any | None = None,
    base_branch: str = "dev",
    interval_seconds: float = POLL_INTERVAL_S,
) -> None:
    """Drain the publish queue forever.

    ``credential_provider`` is called per job so a short-lived installation
    token is minted fresh rather than held for the lifetime of the process.
    A provider that returns ``None`` (GitHub not configured) makes this a
    no-op, which is what a deployment without the GitHub App should do.
    """
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            credential = credential_provider() if credential_provider else None
            # Either source is enough. Skipping when only the App is
            # configured is what made an App-only deployment — the one this
            # module recommends — silently never publish anything.
            if credential is None and app_credentials is None:
                continue
            while await publish_one(
                store,
                credential=credential,
                app_credentials=app_credentials,
                base_branch=base_branch,
            ):
                # Drain rather than publishing one per tick, so a burst of
                # finished jobs does not sit in the queue for minutes.
                pass
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("agent job publish loop iteration failed", exc_info=True)
