"""Request/response schemas for the agent-sandbox job API (issue #1041)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class AgentJobCreate(BaseModel):
    """Request body for creating an agent job."""

    repo: str = Field(..., description="Target repository, e.g. 'owner/name'.")
    task_prompt: str = Field(..., description="What the agent should do.")
    runtime: str = Field("claude-code", description="Agent runtime id.")
    model: str = Field(..., description="Gateway model id the runtime should use.")
    base_sha: str | None = Field(None, description="Commit SHA to work from.")
    metadata: dict[str, Any] | None = Field(None, description="Opaque caller metadata.")


class AgentJobResponse(BaseModel):
    """One agent job as returned to its owner."""

    id: str
    repo: str
    task_prompt: str
    runtime: str
    model: str
    base_sha: str | None = None
    state: str
    cancel_requested: bool = False
    current_attempt_id: int | None = None
    published_pr_url: str | None = None
    detail: str | None = None
    metadata: dict[str, Any] | None = None
    created_at: str | None = None
    updated_at: str | None = None


class AgentJobListResponse(BaseModel):
    """A page of agent jobs."""

    jobs: list[AgentJobResponse]


class AgentJobEvent(BaseModel):
    """One normalized event from an agent job's append-only stream."""

    id: int
    attempt_id: int
    seq: int
    event_type: str
    payload: dict[str, Any] | None = None
    created_at: str | None = None


class AgentJobEventsResponse(BaseModel):
    """A page of events plus the cursor to resume from."""

    events: list[AgentJobEvent]
    next_cursor: int


class AgentJobCancelResponse(BaseModel):
    """Result of requesting cancellation."""

    id: str
    state: str
    cancel_requested: bool


class AgentJobArtifactResponse(BaseModel):
    """One stored artifact (e.g. the produced patch)."""

    job_id: str
    attempt_id: int
    kind: str
    content: str
    created_at: str | None = None


# ── Worker-facing (capability-token authenticated) ─────────────────────


class WorkerClaimRequest(BaseModel):
    """Worker request to claim the next queued job."""

    worker_id: str = Field(..., description="Stable identifier of the claiming worker.")
    lease_ttl_seconds: float = Field(
        120.0, gt=0, description="How long the lease is valid without a heartbeat."
    )


class WorkerClaimResponse(BaseModel):
    """A claimed job plus the capability token scoped to this attempt."""

    job_id: str
    attempt_id: int
    attempt_no: int
    repo: str
    base_sha: str | None = None
    task_prompt: str
    runtime: str
    model: str
    worker_token: str
    metadata: dict[str, Any] | None = None


class WorkerHeartbeatRequest(BaseModel):
    """Worker lease renewal."""

    lease_ttl_seconds: float = Field(120.0, gt=0)


class WorkerHeartbeatResponse(BaseModel):
    """Lease renewal result, carrying any pending cancellation."""

    ok: bool
    state: str
    cancel_requested: bool


class WorkerEventRequest(BaseModel):
    """One normalized event reported by a worker."""

    event_type: str = Field(..., description="thinking|message|tool_use|...|lifecycle")
    payload: dict[str, Any] | None = None


class WorkerEventResponse(BaseModel):
    """Accepted event id (the SSE cursor value)."""

    event_id: int


class WorkerArtifactRequest(BaseModel):
    """An artifact produced by a worker (e.g. the git patch)."""

    kind: str = Field(..., description="Artifact kind, e.g. 'patch'.")
    content: str


class WorkerArtifactResponse(BaseModel):
    """Stored artifact id."""

    artifact_id: int


class WorkerFinishRequest(BaseModel):
    """Terminal transition reported by a worker."""

    state: str = Field(..., description="succeeded|failed|cancelled")
    detail: str | None = None


class WorkerPublishRequest(BaseModel):
    """Publisher result recorded against the one-shot publish transition."""

    pr_url: str


class WorkerAckResponse(BaseModel):
    """Generic worker acknowledgement."""

    ok: bool
    state: str | None = None
