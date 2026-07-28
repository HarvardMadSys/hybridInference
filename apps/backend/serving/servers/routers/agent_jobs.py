"""Agent-sandbox job API (``/v1/agent/jobs``) — issue #1041, P0.

Two audiences, two authentication schemes:

- **Owners** (users, via the normal API key): create, inspect, list, cancel
  jobs, read artifacts, and subscribe to the event stream. Every read and
  mutation is scoped to ``user_id`` so one user can never touch another's job.
- **Workers** (the sandbox runner, via a per-attempt capability token minted at
  claim time): report events, store artifacts, renew the lease, and drive the
  fenced terminal/publish transitions. The token carries the
  ``(job_id, attempt_id, lease_generation)`` fence, so a worker whose lease was
  reaped is rejected by the store even though its token still parses. Those
  rejections surface as **409**, the agreed signal for "you lost the lease,
  stop now".

The SSE endpoint streams the append-only event log with ``Last-Event-ID``
resume. It deliberately sets ``text/event-stream``, which the timeout
middleware recognizes to apply the long stream cap instead of the 120s request
timeout (the #865/#889 lesson: long-lived SSE must not inherit the short cap).
Because that cap is finite by design, the stream is resumable: clients
reconnect with ``Last-Event-ID`` and continue from the global cursor.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from serving.agent_jobs.egress import (
    EgressPolicyError,
    build_policy_from_env as build_egress_policy,
)
from serving.agent_jobs.entitlement import (
    RepoNotAllowed,
    repos_for_user,
    require_allowed_repo,
    require_entitled_repo,
)
from serving.agent_jobs.github_app import AppNotInstalled
from serving.agent_jobs.runtimes import registered_runtimes
from serving.agent_jobs.tokens import (
    SCOPE_FULL,
    SCOPE_MODEL,
    InvalidAgentToken,
    mint_worker_token,
    parse_worker_token,
)
from serving.schemas_agent_jobs import (
    DEFAULT_JOB_BUDGET_USD,
    EVENT_TYPE_PATTERN,
    AgentConfigResponse,
    AgentJobArtifactResponse,
    AgentJobCancelResponse,
    AgentJobCreate,
    AgentJobEvent,
    AgentJobEventsResponse,
    AgentJobListResponse,
    AgentJobResponse,
    GitHubConnectionResponse,
    GitHubConnectRequest,
    RepoBranchesResponse,
    WorkerAckResponse,
    WorkerArtifactRequest,
    WorkerArtifactResponse,
    WorkerClaimRequest,
    WorkerClaimResponse,
    WorkerEventRequest,
    WorkerEventResponse,
    WorkerFinishRequest,
    WorkerHeartbeatRequest,
    WorkerHeartbeatResponse,
)
from serving.servers.auth import verify_api_key
from serving.servers.deps import (
    get_agent_app_credentials,
    get_agent_job_store,
    get_log_store,
    verify_admin_access,
)
from serving.storage.agent_job_store import RUNNING, TERMINAL_STATES
from serving.utils.logging import get_logger

if TYPE_CHECKING:
    from serving.storage.agent_job_store import AgentJobStore

logger = get_logger(__name__)
router = APIRouter(prefix="/v1/agent")

# Same anti-buffering headers the completions SSE path uses: `no-transform`
# stops intermediary CDNs from buffering to compress, `X-Accel-Buffering: no`
# disables nginx buffering.
_SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}

_POLL_INTERVAL_S = 1.0
_KEEPALIVE_EVERY_N_POLLS = 15
_EVENT_PAGE_SIZE = 500

# The schema constrains event_type on the way in, but the SSE writer must be
# safe on its own: rows written before that constraint existed (or by any
# future non-HTTP writer) must not be able to break out of the ``event:`` field
# and inject frames into the owner's stream.
_SAFE_EVENT_TYPE = re.compile(EVENT_TYPE_PATTERN)

# The clone credential is narrowed at the point it is minted, not merely by
# convention: the App also holds `contents: write` for the publisher, and an
# installation token inherits every permission the App has unless it is asked
# for less. Without this the runner would be handed push rights it must not
# have, on a host that runs untrusted repository code.
_CLONE_SCOPE = {"contents": "read"}


def _sse_frame(event: dict[str, Any]) -> str:
    """Render one stored event as an SSE frame with a safe event name."""
    event_type = event["event_type"]
    # fullmatch, not match: `$` also matches before a trailing newline, so
    # `match()` would accept "message\n" — exactly the value this guard
    # exists to reject, since the newline splits the SSE frame.
    if not _SAFE_EVENT_TYPE.fullmatch(event_type or ""):
        event_type = "malformed"
    data = json.dumps(_event_response(event).model_dump(), separators=(",", ":"))
    return f"id: {event['id']}\nevent: {event_type}\ndata: {data}\n\n"


def _require_store(store: AgentJobStore | None) -> AgentJobStore:
    """Return the store or fail with a clear 503 when the DB is absent."""
    if store is None:
        raise HTTPException(
            status_code=503,
            detail={
                "error": {
                    "type": "unavailable",
                    "message": "Agent jobs require a configured database.",
                }
            },
        )
    return store


def _iso(value: Any) -> str | None:
    """Render a timestamp column as ISO-8601 (or None)."""
    return value.isoformat() if value is not None else None


async def _job_usage(log_store: Any, job_id: str) -> dict[str, Any]:
    """Read one job's spend and token totals from the billing ledger.

    Deliberately the ledger and not the agent's own report: a run that
    misstates its usage — which is a thing models do — cannot change what the
    owner is shown, and it is the same source the budget check already trusts.
    Absent on a deployment with no log store, rather than a fabricated zero.
    """
    usage: dict[str, Any] = {}
    cost_getter = getattr(log_store, "get_agent_job_cost", None)
    if cost_getter is not None:
        with contextlib.suppress(Exception):
            usage["spent_usd"] = await cost_getter(job_id)
    usage_getter = getattr(log_store, "get_agent_job_usage", None)
    if usage_getter is not None:
        with contextlib.suppress(Exception):
            totals = await usage_getter(job_id)
            usage["tokens_in"] = int(totals.get("tokens_in", 0))
            usage["tokens_out"] = int(totals.get("tokens_out", 0))
            usage["model_calls"] = int(totals.get("calls", 0))
    return usage


def _egress_tiers() -> tuple[str | None, str | None]:
    """Report the deployment's per-phase egress posture, if it has one."""
    try:
        policy = build_egress_policy()
    except EgressPolicyError:
        # A misconfigured policy is the runner's problem to refuse at preflight,
        # not a reason to fail an owner reading their own job.
        return None, None
    return policy.tier_for("setup").value, policy.tier_for("agent").value


def _job_response(job: dict[str, Any], usage: dict[str, Any] | None = None) -> AgentJobResponse:
    """Shape a store job row for the owner-facing API."""
    usage = usage or {}
    setup_tier, agent_tier = _egress_tiers()
    return AgentJobResponse(
        id=job["id"],
        repo=job["repo"],
        task_prompt=job["task_prompt"],
        runtime=job["runtime"],
        model=job["model"],
        base_sha=job["base_sha"],
        state=job["state"],
        cancel_requested=job["cancel_requested"],
        current_attempt_id=job["current_attempt_id"],
        published_pr_url=job["published_pr_url"],
        detail=job["detail"],
        budget_usd=job.get("budget_usd"),
        metadata=job["metadata"],
        created_at=_iso(job["created_at"]),
        updated_at=_iso(job["updated_at"]),
        spent_usd=usage.get("spent_usd"),
        tokens_in=usage.get("tokens_in"),
        tokens_out=usage.get("tokens_out"),
        model_calls=usage.get("model_calls"),
        setup_egress_tier=setup_tier,
        agent_egress_tier=agent_tier,
    )


def _event_response(event: dict[str, Any]) -> AgentJobEvent:
    """Shape a store event row for the API."""
    return AgentJobEvent(
        id=event["id"],
        attempt_id=event["attempt_id"],
        seq=event["seq"],
        event_type=event["event_type"],
        payload=event["payload"],
        created_at=_iso(event["created_at"]),
    )


async def _owned_job(
    store: AgentJobStore,
    job_id: str,
    user: dict[str, Any],
) -> dict[str, Any]:
    """Fetch a job the caller owns, or raise 404.

    A job owned by someone else is reported as 404 rather than 403 so job ids
    cannot be probed for existence across accounts.
    """
    job = await store.get_job(job_id)
    if job is None or job["user_id"] != user["user_id"]:
        raise HTTPException(
            status_code=404,
            detail={"error": {"type": "not_found", "message": f"No such agent job: {job_id}"}},
        )
    return job


# ── Owner endpoints ────────────────────────────────────────────────────


@router.get("/config", response_model=AgentConfigResponse)
async def get_agent_config(
    user: dict[str, Any] = Depends(verify_api_key),
    store: AgentJobStore | None = Depends(get_agent_job_store),
    app_credentials: Any | None = Depends(get_agent_app_credentials),
) -> AgentConfigResponse:
    """What this deployment will actually accept.

    The composer showed a repository, a branch, a runtime and a model as static
    labels while submitting different hardcoded values, so the UI described a
    job nobody was running. A picker has to be built from the same answers the
    create endpoint enforces, or it is decoration.
    """
    setup_tier, agent_tier = _egress_tiers()
    # This user's repositories, not the deployment's: what the picker offers
    # has to be what the create endpoint will accept for *them*.
    repos = await repos_for_user(user["user_id"], store=store, app_credentials=app_credentials)
    return AgentConfigResponse(
        repos=repos,
        runtimes=registered_runtimes(),
        default_budget_usd=DEFAULT_JOB_BUDGET_USD,
        setup_egress_tier=setup_tier,
        agent_egress_tier=agent_tier,
        # Both halves, and read from what is actually wired rather than from
        # the environment: an App the platform can mint tokens from, and at
        # least one repository this user may work on. Either alone leaves a
        # composer that cannot produce a runnable job.
        github_connected=bool(app_credentials is not None and repos),
        github_install_url=os.getenv("AGENT_GITHUB_APP_INSTALL_URL") or None,
    )


@router.post("/github/connect", response_model=GitHubConnectionResponse)
async def connect_github(
    body: GitHubConnectRequest,
    user: dict[str, Any] = Depends(verify_api_key),
    store: AgentJobStore | None = Depends(get_agent_job_store),
    app_credentials: Any | None = Depends(get_agent_app_credentials),
) -> GitHubConnectionResponse:
    """Record the installations this user proved they can reach.

    The browser sends the code GitHub handed it; the platform exchanges it for
    a token that speaks *as that user* and asks GitHub which installations they
    can see. Nothing the browser asserts is trusted — an installation id posted
    directly would be exactly the confused deputy this whole path exists to
    prevent, so the id is only ever taken from GitHub's own answer.
    """
    job_store = _require_store(store)
    if app_credentials is None:
        raise HTTPException(
            status_code=503,
            detail={
                "error": {
                    "type": "unavailable",
                    "message": "No GitHub App is configured for this deployment.",
                }
            },
        )
    try:
        user_token = await app_credentials.exchange_user_code(body.code)
        installations = await app_credentials.installations_for_user(user_token)
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail={"error": {"type": "github_connect_failed", "message": str(exc)}},
        ) from exc

    for installation in installations:
        await job_store.record_repo_grant(
            user_id=user["user_id"],
            installation_id=installation["installation_id"],
            account_login=installation.get("account_login"),
        )
    logger.info(
        "agent_github_connected",
        extra={"event": "agent_github_connected", "installations": len(installations)},
    )
    return GitHubConnectionResponse(
        connections=await job_store.list_repo_grants(user_id=user["user_id"]),
        repos=await repos_for_user(
            user["user_id"], store=job_store, app_credentials=app_credentials
        ),
    )


@router.delete("/github/connect/{installation_id}", response_model=GitHubConnectionResponse)
async def disconnect_github(
    installation_id: int,
    user: dict[str, Any] = Depends(verify_api_key),
    store: AgentJobStore | None = Depends(get_agent_job_store),
    app_credentials: Any | None = Depends(get_agent_app_credentials),
) -> GitHubConnectionResponse:
    """Drop one connection. Uninstalling on GitHub is the other half."""
    job_store = _require_store(store)
    await job_store.revoke_repo_grant(user_id=user["user_id"], installation_id=installation_id)
    return GitHubConnectionResponse(
        connections=await job_store.list_repo_grants(user_id=user["user_id"]),
        repos=await repos_for_user(
            user["user_id"], store=job_store, app_credentials=app_credentials
        ),
    )


@router.get("/branches", response_model=RepoBranchesResponse)
async def list_repo_branches(
    repo: str = Query(..., description="owner/name"),
    user: dict[str, Any] = Depends(verify_api_key),
    store: AgentJobStore | None = Depends(get_agent_job_store),
    app_credentials: Any | None = Depends(get_agent_app_credentials),
) -> RepoBranchesResponse:
    """List a repository's branches, for the composer's branch picker.

    Entitlement first: this reads a repository through the platform's own
    installation, so without the check it would be a way to enumerate branches
    of any repository the App happens to cover.
    """
    job_store = _require_store(store)
    try:
        await require_entitled_repo(
            repo, user["user_id"], store=job_store, app_credentials=app_credentials
        )
    except RepoNotAllowed as exc:
        raise HTTPException(
            status_code=403,
            detail={"error": {"type": "repo_not_allowed", "message": str(exc)}},
        ) from exc
    if app_credentials is None:
        return RepoBranchesResponse()
    try:
        found = await app_credentials.branches_for_repo(repo)
    except Exception:
        logger.warning("agent_branches_unavailable", extra={"event": "agent_branches_unavailable"})
        return RepoBranchesResponse()
    return RepoBranchesResponse(default=found.get("default"), branches=found.get("branches", []))


@router.post("/jobs", response_model=AgentJobResponse, status_code=201)
async def create_agent_job(
    body: AgentJobCreate,
    user: dict[str, Any] = Depends(verify_api_key),
    store: AgentJobStore | None = Depends(get_agent_job_store),
    app_credentials: Any | None = Depends(get_agent_app_credentials),
) -> AgentJobResponse:
    """Queue a new agent job for the authenticated user."""
    job_store = _require_store(store)
    # The requester chooses the repository and the platform later mints a real
    # installation token for it. Without this the two combine into a confused
    # deputy: name any repository the App reaches, and read it back through
    # your own job's events and patch.
    try:
        await require_entitled_repo(
            body.repo,
            user["user_id"],
            store=job_store,
            app_credentials=app_credentials,
        )
    except RepoNotAllowed as exc:
        raise HTTPException(
            status_code=403,
            detail={"error": {"type": "repo_not_allowed", "message": str(exc)}},
        ) from exc
    # A branch is resolved to the commit it points at *now*. The job stores the
    # sha: a branch moves, so a job that recorded "dev" would silently mean a
    # different tree by the time it ran, and the publisher applies its patch
    # onto a pinned commit.
    base_sha = body.base_sha
    if base_sha is None and body.base_ref and app_credentials is not None:
        try:
            base_sha = await app_credentials.resolve_ref(body.repo, body.base_ref)
        except Exception as exc:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": {
                        "type": "invalid_request",
                        "message": f"could not resolve {body.base_ref!r} in {body.repo}: {exc}",
                    }
                },
            ) from exc

    job = await job_store.create_job(
        user_id=user["user_id"],
        repo=body.repo,
        task_prompt=body.task_prompt,
        runtime=body.runtime,
        model=body.model,
        base_sha=base_sha,
        budget_usd=body.budget_usd,
        metadata=body.metadata,
    )
    logger.info(
        "agent_job_created",
        extra={"event": "agent_job_created", "job_id": job["id"], "runtime": body.runtime},
    )
    return _job_response(job)


@router.get("/jobs", response_model=AgentJobListResponse)
async def list_agent_jobs(
    limit: int = Query(50, ge=1, le=200),
    user: dict[str, Any] = Depends(verify_api_key),
    store: AgentJobStore | None = Depends(get_agent_job_store),
) -> AgentJobListResponse:
    """List the authenticated user's agent jobs, newest first."""
    job_store = _require_store(store)
    jobs = await job_store.list_jobs(user_id=user["user_id"], limit=limit)
    return AgentJobListResponse(jobs=[_job_response(job) for job in jobs])


@router.get("/jobs/{job_id}", response_model=AgentJobResponse)
async def get_agent_job(
    job_id: str,
    user: dict[str, Any] = Depends(verify_api_key),
    store: AgentJobStore | None = Depends(get_agent_job_store),
    log_store=Depends(get_log_store),
) -> AgentJobResponse:
    """Fetch one of the caller's agent jobs, with what it has spent so far."""
    job_store = _require_store(store)
    job = await _owned_job(job_store, job_id, user)
    return _job_response(job, await _job_usage(log_store, job_id))


@router.post("/jobs/{job_id}/cancel", response_model=AgentJobCancelResponse)
async def cancel_agent_job(
    job_id: str,
    user: dict[str, Any] = Depends(verify_api_key),
    store: AgentJobStore | None = Depends(get_agent_job_store),
) -> AgentJobCancelResponse:
    """Request cancellation of one of the caller's agent jobs.

    Queued jobs cancel immediately; a running job is flagged and the worker
    performs the fenced terminal transition when it next heartbeats.
    """
    job_store = _require_store(store)
    await _owned_job(job_store, job_id, user)
    state = await job_store.request_cancel(job_id=job_id, user_id=user["user_id"])
    job = await job_store.get_job(job_id)
    return AgentJobCancelResponse(
        id=job_id,
        state=state or (job or {}).get("state", "unknown"),
        cancel_requested=bool((job or {}).get("cancel_requested")),
    )


@router.get("/jobs/{job_id}/events", response_model=AgentJobEventsResponse)
async def list_agent_job_events(
    job_id: str,
    after: int = Query(0, ge=0, description="Return events with a global id greater than this."),
    limit: int = Query(_EVENT_PAGE_SIZE, ge=1, le=1000),
    user: dict[str, Any] = Depends(verify_api_key),
    store: AgentJobStore | None = Depends(get_agent_job_store),
) -> AgentJobEventsResponse:
    """Page through a job's append-only event log (non-streaming)."""
    job_store = _require_store(store)
    await _owned_job(job_store, job_id, user)
    events = await job_store.list_events_after(job_id=job_id, after_id=after, limit=limit)
    return AgentJobEventsResponse(
        events=[_event_response(event) for event in events],
        next_cursor=events[-1]["id"] if events else after,
    )


@router.get("/jobs/{job_id}/artifacts/{kind}", response_model=AgentJobArtifactResponse)
async def get_agent_job_artifact(
    job_id: str,
    kind: str,
    user: dict[str, Any] = Depends(verify_api_key),
    store: AgentJobStore | None = Depends(get_agent_job_store),
) -> AgentJobArtifactResponse:
    """Fetch the latest artifact of a kind (e.g. ``patch``) for a job."""
    job_store = _require_store(store)
    await _owned_job(job_store, job_id, user)
    artifact = await job_store.get_artifact(job_id=job_id, kind=kind)
    if artifact is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error": {"type": "not_found", "message": f"No '{kind}' artifact for {job_id}"}
            },
        )
    return AgentJobArtifactResponse(
        job_id=artifact["job_id"],
        attempt_id=artifact["attempt_id"],
        kind=artifact["kind"],
        content=artifact["content"],
        created_at=_iso(artifact["created_at"]),
    )


@router.get("/jobs/{job_id}/stream")
async def stream_agent_job_events(
    job_id: str,
    request: Request,
    last_event_id: str | None = Header(None, alias="Last-Event-ID"),
    after: int = Query(0, ge=0),
    user: dict[str, Any] = Depends(verify_api_key),
    store: AgentJobStore | None = Depends(get_agent_job_store),
) -> StreamingResponse:
    """Stream a job's events as SSE, resumable via ``Last-Event-ID``.

    Each SSE frame carries the event's global id, so a client that reconnects
    with ``Last-Event-ID`` resumes exactly where it left off — which is what
    makes the finite stream cap (applied by the timeout middleware to
    ``text/event-stream``) safe for jobs that outlive one connection.
    """
    job_store = _require_store(store)
    await _owned_job(job_store, job_id, user)

    cursor = after
    if last_event_id:
        with contextlib.suppress(ValueError):
            cursor = int(last_event_id)

    async def event_source():
        nonlocal cursor
        idle_polls = 0
        try:
            while True:
                if await request.is_disconnected():
                    return
                events = await job_store.list_events_after(
                    job_id=job_id, after_id=cursor, limit=_EVENT_PAGE_SIZE
                )
                if events:
                    idle_polls = 0
                    for event in events:
                        cursor = event["id"]
                        yield _sse_frame(event)
                else:
                    idle_polls += 1
                    if idle_polls % _KEEPALIVE_EVERY_N_POLLS == 0:
                        # Comment frame: keeps proxies from reaping an idle
                        # connection without polluting the event sequence.
                        yield ": keepalive\n\n"

                job = await job_store.get_job(job_id)
                if job is None:
                    return
                if job["state"] in TERMINAL_STATES:
                    # Drain to exhaustion before announcing the end. A single
                    # extra page is not enough: a client attaching to a
                    # finished job with more than two pages of backlog would
                    # see job_finished, close, and lose the rest forever.
                    while True:
                        tail = await job_store.list_events_after(
                            job_id=job_id, after_id=cursor, limit=_EVENT_PAGE_SIZE
                        )
                        if not tail:
                            break
                        for event in tail:
                            cursor = event["id"]
                            yield _sse_frame(event)
                    final = json.dumps(
                        {
                            "state": job["state"],
                            "published_pr_url": job["published_pr_url"],
                            "last_event_id": cursor,
                        },
                        separators=(",", ":"),
                    )
                    yield f"event: job_finished\ndata: {final}\n\n"
                    return

                await asyncio.sleep(_POLL_INTERVAL_S)
        except asyncio.CancelledError:
            # Client disconnect or the stream cap firing: nothing to clean up,
            # the event log is durable and the client can resume by cursor.
            raise

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )


# ── Worker endpoints (capability-token authenticated) ──────────────────


def _worker_claims(authorization: str | None) -> dict[str, Any]:
    """Parse and verify the worker capability token from the auth header."""
    token = authorization[7:] if authorization and authorization.startswith("Bearer ") else None
    if not token:
        raise HTTPException(
            status_code=401,
            detail={"error": {"type": "unauthorized", "message": "Missing worker token."}},
        )
    try:
        claims = parse_worker_token(token)
    except InvalidAgentToken as exc:
        raise HTTPException(
            status_code=401,
            detail={"error": {"type": "unauthorized", "message": f"Invalid worker token: {exc}"}},
        ) from exc
    if claims.get("scope") != SCOPE_FULL:
        # A model-scoped token is what lives inside the sandbox. Reaching these
        # endpoints with it means the credential escaped its intended use.
        raise HTTPException(
            status_code=403,
            detail={
                "error": {
                    "type": "insufficient_scope",
                    "message": (
                        "This credential may only be used for model calls, not for "
                        "reporting job state."
                    ),
                }
            },
        )
    return claims


def _lease_lost() -> HTTPException:
    """The 409 every fenced worker write raises when its lease is gone."""
    return HTTPException(
        status_code=409,
        detail={
            "error": {
                "type": "lease_lost",
                "message": (
                    "This attempt no longer holds the job lease; stop all work immediately."
                ),
            }
        },
    )


def _match_job(claims: dict[str, Any], job_id: str) -> None:
    """Reject a token minted for a different job."""
    if claims["job_id"] != job_id:
        raise _lease_lost()


@router.post("/worker/claim", response_model=WorkerClaimResponse | None)
async def worker_claim(
    body: WorkerClaimRequest,
    _dispatcher: str = Depends(verify_admin_access),
    store: AgentJobStore | None = Depends(get_agent_job_store),
    app_credentials: Any | None = Depends(get_agent_app_credentials),
) -> WorkerClaimResponse | None:
    """Claim the next queued job and mint this attempt's capability token.

    Returns ``null`` (HTTP 200) when the queue is empty.

    **Dispatcher-only.** ``claim_job`` takes the oldest queued job across all
    tenants, and the response carries that job's repo, prompt, and metadata
    plus a working capability token for it — so ordinary API-key auth here
    would let any customer dequeue and read another customer's job, and drain
    the queue besides.

    ``verify_admin_access`` is the right gate rather than a role check on a
    user key: this is a machine-to-machine endpoint, and that dependency
    accepts the shared ``ADMIN_TOKEN`` a dispatcher can actually hold (as well
    as an admin JWT). It also has no "auth disabled" bypass, so the endpoint
    does not fall open in a deployment running with user auth off. The
    dispatcher credential stays outside the sandbox; only the returned
    per-attempt token goes in.
    """
    job_store = _require_store(store)
    claim = await job_store.claim_job(
        worker_id=body.worker_id, lease_ttl_seconds=body.lease_ttl_seconds
    )
    if claim is None:
        return None
    fence = {
        "job_id": claim["id"],
        "attempt_id": claim["attempt_id"],
        "lease_generation": claim["lease_generation"],
    }
    # Two credentials with different powers. The runner keeps the full one and
    # passes only the model-scoped one into the sandbox, so a credential that
    # leaks from inside the agent can spend the job's capped budget but cannot
    # touch its event log, artifacts, or terminal state.
    token = mint_worker_token(**fence, scope=SCOPE_FULL)
    sandbox_token = mint_worker_token(**fence, scope=SCOPE_MODEL)

    # A third credential, weaker than either: read-only, this repository only,
    # one hour. The runner needs it to check the repository out — a runner that
    # cannot clone runs the agent in an empty directory — and it is deliberately
    # not the publisher's token, which can write. It stays in the runner and
    # never enters the sandbox. Absent (null) is a working configuration: a
    # public repository clones without any credential at all.
    # Re-checked at mint time, not just at create time. A row written before
    # this check existed — or while the allowlist was wider — must not be able
    # to produce a credential now.
    try:
        require_allowed_repo(claim["repo"])
    except RepoNotAllowed as exc:
        await job_store.release_claim(
            job_id=claim["id"],
            attempt_id=claim["attempt_id"],
            lease_generation=claim["lease_generation"],
        )
        logger.warning(
            "agent_job_repo_not_allowed",
            extra={"event": "agent_job_repo_not_allowed", "job_id": claim["id"]},
        )
        raise HTTPException(
            status_code=403,
            detail={"error": {"type": "repo_not_allowed", "message": str(exc)}},
        ) from exc

    clone_token: str | None = None
    if app_credentials is not None:
        try:
            clone_token = await app_credentials.token_for(
                claim["repo"], permissions=_CLONE_SCOPE, repository_scoped=True
            )
        except AppNotInstalled:
            # A settled answer: the App does not cover this repository. Hand
            # the job over without a credential — a public repository clones
            # anonymously, and a private one fails with a message the owner can
            # act on ("install the App"), which retrying would not improve.
            logger.info(
                "agent_job_clone_token_unavailable",
                extra={"event": "agent_job_clone_token_unavailable", "job_id": claim["id"]},
            )
        except Exception as exc:
            # Anything else is GitHub or the network having a bad minute.
            # Returning no token here would send the runner off to clone
            # anonymously, fail on a private repository, and mark the job
            # *terminally* failed — turning a momentary outage into the owner's
            # problem.
            #
            # Hand the claim back rather than just letting the lease lapse. A
            # lapsed lease still spends a retry, so a GitHub outage lasting
            # across three claim cycles would fail every queued private-repo
            # job outright, without an agent ever having started — the very
            # outcome this branch exists to avoid, only slower.
            await job_store.release_claim(
                job_id=claim["id"],
                attempt_id=claim["attempt_id"],
                lease_generation=claim["lease_generation"],
            )
            logger.warning(
                "agent_job_clone_token_error",
                extra={"event": "agent_job_clone_token_error", "job_id": claim["id"]},
            )
            raise HTTPException(
                status_code=503,
                detail={
                    "error": {
                        "type": "credential_unavailable",
                        "message": (
                            "Could not mint a repository credential for this job; it has "
                            "been returned to the queue and no attempt was spent."
                        ),
                    }
                },
            ) from exc

    return WorkerClaimResponse(
        job_id=claim["id"],
        attempt_id=claim["attempt_id"],
        attempt_no=claim["attempt_no"],
        repo=claim["repo"],
        base_sha=claim["base_sha"],
        task_prompt=claim["task_prompt"],
        runtime=claim["runtime"],
        model=claim["model"],
        worker_token=token,
        sandbox_token=sandbox_token,
        clone_token=clone_token,
        metadata=claim["metadata"],
    )


@router.post("/worker/jobs/{job_id}/heartbeat", response_model=WorkerHeartbeatResponse)
async def worker_heartbeat(
    job_id: str,
    body: WorkerHeartbeatRequest,
    authorization: str | None = Header(None),
    store: AgentJobStore | None = Depends(get_agent_job_store),
) -> WorkerHeartbeatResponse:
    """Renew the attempt's lease and learn about pending cancellation."""
    job_store = _require_store(store)
    claims = _worker_claims(authorization)
    _match_job(claims, job_id)
    beat = await job_store.heartbeat(
        attempt_id=claims["attempt_id"],
        lease_generation=claims["lease_generation"],
        lease_ttl_seconds=body.lease_ttl_seconds,
    )
    if not beat["ok"]:
        raise _lease_lost()
    return WorkerHeartbeatResponse(
        ok=True, state=beat["state"], cancel_requested=beat["cancel_requested"]
    )


@router.post("/worker/jobs/{job_id}/events", response_model=WorkerEventResponse, status_code=201)
async def worker_append_event(
    job_id: str,
    body: WorkerEventRequest,
    authorization: str | None = Header(None),
    store: AgentJobStore | None = Depends(get_agent_job_store),
) -> WorkerEventResponse:
    """Append one normalized event to the job's append-only log."""
    job_store = _require_store(store)
    claims = _worker_claims(authorization)
    _match_job(claims, job_id)
    event_id = await job_store.append_event(
        attempt_id=claims["attempt_id"],
        lease_generation=claims["lease_generation"],
        event_type=body.event_type,
        payload=body.payload,
    )
    if event_id is None:
        raise _lease_lost()
    return WorkerEventResponse(event_id=event_id)


@router.post(
    "/worker/jobs/{job_id}/artifacts", response_model=WorkerArtifactResponse, status_code=201
)
async def worker_save_artifact(
    job_id: str,
    body: WorkerArtifactRequest,
    authorization: str | None = Header(None),
    store: AgentJobStore | None = Depends(get_agent_job_store),
) -> WorkerArtifactResponse:
    """Store (or replace) an artifact produced by this attempt."""
    job_store = _require_store(store)
    claims = _worker_claims(authorization)
    _match_job(claims, job_id)
    artifact_id = await job_store.save_artifact(
        attempt_id=claims["attempt_id"],
        lease_generation=claims["lease_generation"],
        kind=body.kind,
        content=body.content,
    )
    if artifact_id is None:
        raise _lease_lost()
    return WorkerArtifactResponse(artifact_id=artifact_id)


@router.post("/worker/jobs/{job_id}/finish", response_model=WorkerAckResponse)
async def worker_finish(
    job_id: str,
    body: WorkerFinishRequest,
    authorization: str | None = Header(None),
    store: AgentJobStore | None = Depends(get_agent_job_store),
) -> WorkerAckResponse:
    """Drive the fenced terminal transition for this attempt."""
    job_store = _require_store(store)
    claims = _worker_claims(authorization)
    _match_job(claims, job_id)
    if body.state not in TERMINAL_STATES:
        raise HTTPException(
            status_code=400,
            detail={
                "error": {
                    "type": "invalid_request",
                    "message": f"state must be one of {list(TERMINAL_STATES)}",
                }
            },
        )
    ok = await job_store.transition(
        job_id=job_id,
        attempt_id=claims["attempt_id"],
        lease_generation=claims["lease_generation"],
        from_states=(RUNNING,),
        to_state=body.state,
        detail=body.detail,
        base_sha=body.base_sha,
    )
    if not ok:
        raise _lease_lost()
    return WorkerAckResponse(ok=True, state=body.state)
