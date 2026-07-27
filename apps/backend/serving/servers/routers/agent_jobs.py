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
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from serving.agent_jobs.tokens import InvalidAgentToken, mint_worker_token, parse_worker_token
from serving.schemas_agent_jobs import (
    AgentJobArtifactResponse,
    AgentJobCancelResponse,
    AgentJobCreate,
    AgentJobEvent,
    AgentJobEventsResponse,
    AgentJobListResponse,
    AgentJobResponse,
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
    WorkerPublishRequest,
)
from serving.servers.auth import verify_api_key
from serving.servers.deps import get_agent_job_store
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


def _job_response(job: dict[str, Any]) -> AgentJobResponse:
    """Shape a store job row for the owner-facing API."""
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
        metadata=job["metadata"],
        created_at=_iso(job["created_at"]),
        updated_at=_iso(job["updated_at"]),
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


@router.post("/jobs", response_model=AgentJobResponse, status_code=201)
async def create_agent_job(
    body: AgentJobCreate,
    user: dict[str, Any] = Depends(verify_api_key),
    store: AgentJobStore | None = Depends(get_agent_job_store),
) -> AgentJobResponse:
    """Queue a new agent job for the authenticated user."""
    job_store = _require_store(store)
    job = await job_store.create_job(
        user_id=user["user_id"],
        repo=body.repo,
        task_prompt=body.task_prompt,
        runtime=body.runtime,
        model=body.model,
        base_sha=body.base_sha,
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
) -> AgentJobResponse:
    """Fetch one of the caller's agent jobs."""
    job_store = _require_store(store)
    return _job_response(await _owned_job(job_store, job_id, user))


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
                        data = json.dumps(
                            _event_response(event).model_dump(), separators=(",", ":")
                        )
                        yield f"id: {event['id']}\nevent: {event['event_type']}\ndata: {data}\n\n"
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
                    # Drain anything appended between the last page and the
                    # terminal transition, then close cleanly.
                    tail = await job_store.list_events_after(
                        job_id=job_id, after_id=cursor, limit=_EVENT_PAGE_SIZE
                    )
                    for event in tail:
                        cursor = event["id"]
                        data = json.dumps(
                            _event_response(event).model_dump(), separators=(",", ":")
                        )
                        yield f"id: {event['id']}\nevent: {event['event_type']}\ndata: {data}\n\n"
                    final = json.dumps(
                        {"state": job["state"], "published_pr_url": job["published_pr_url"]},
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
        return parse_worker_token(token)
    except InvalidAgentToken as exc:
        raise HTTPException(
            status_code=401,
            detail={"error": {"type": "unauthorized", "message": f"Invalid worker token: {exc}"}},
        ) from exc


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
    _admin: dict[str, Any] = Depends(verify_api_key),
    store: AgentJobStore | None = Depends(get_agent_job_store),
) -> WorkerClaimResponse | None:
    """Claim the next queued job and mint this attempt's capability token.

    Returns ``null`` (HTTP 200) when the queue is empty. The claim endpoint
    itself is authenticated with a normal API key — that key belongs to the
    dispatcher, not to the sandbox; only the returned per-attempt token ever
    reaches the agent environment.
    """
    job_store = _require_store(store)
    claim = await job_store.claim_job(
        worker_id=body.worker_id, lease_ttl_seconds=body.lease_ttl_seconds
    )
    if claim is None:
        return None
    token = mint_worker_token(
        job_id=claim["id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
    )
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
    )
    if not ok:
        raise _lease_lost()
    return WorkerAckResponse(ok=True, state=body.state)


@router.post("/worker/jobs/{job_id}/publish/begin", response_model=WorkerAckResponse)
async def worker_begin_publish(
    job_id: str,
    authorization: str | None = Header(None),
    store: AgentJobStore | None = Depends(get_agent_job_store),
) -> WorkerAckResponse:
    """Enter the one-shot publish phase (``running -> publishing``)."""
    job_store = _require_store(store)
    claims = _worker_claims(authorization)
    _match_job(claims, job_id)
    ok = await job_store.begin_publish(
        job_id=job_id,
        attempt_id=claims["attempt_id"],
        lease_generation=claims["lease_generation"],
    )
    if not ok:
        raise _lease_lost()
    return WorkerAckResponse(ok=True, state="publishing")


@router.post("/worker/jobs/{job_id}/publish/complete", response_model=WorkerAckResponse)
async def worker_complete_publish(
    job_id: str,
    body: WorkerPublishRequest,
    authorization: str | None = Header(None),
    store: AgentJobStore | None = Depends(get_agent_job_store),
) -> WorkerAckResponse:
    """Record the published PR URL and finish the job (exactly once)."""
    job_store = _require_store(store)
    claims = _worker_claims(authorization)
    _match_job(claims, job_id)
    ok = await job_store.complete_publish(
        job_id=job_id,
        attempt_id=claims["attempt_id"],
        lease_generation=claims["lease_generation"],
        pr_url=body.pr_url,
    )
    if not ok:
        raise _lease_lost()
    return WorkerAckResponse(ok=True, state="succeeded")
