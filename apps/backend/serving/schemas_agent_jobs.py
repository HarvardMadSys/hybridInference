"""Request/response schemas for the agent-sandbox job API (issue #1041)."""

from __future__ import annotations

import base64
import binascii
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from serving.agent_jobs.entitlement import REPO_PATTERN

# A lease is the only thing that lets the reaper take a job back from a stuck
# or malicious worker. If the worker could pick the TTL, it could pick one long
# enough that the lease never expires — and then the capability token bound to
# that attempt would be neither self-revoking nor cancellable by the owner. The
# server therefore caps it, and 15 minutes is far above any legitimate gap
# between heartbeats.
MAX_LEASE_TTL_SECONDS = 900.0

# Every job carries a spending cap. The default is small enough that a
# misconfigured or runaway job is an annoyance rather than a bill, and the
# ceiling stops a typo (or a hostile caller) from requesting an unbounded one.
DEFAULT_JOB_BUDGET_USD = 5.0
MAX_JOB_BUDGET_USD = 500.0

# Normalized event kinds (issue #1041) plus the control events the platform
# appends. The pattern is the security-relevant part: an event type is
# interpolated into the SSE ``event:`` field, so anything containing a newline
# would let a worker inject arbitrary frames into the owner's stream. Keeping
# the charset to lowercase/digits/underscore makes that structurally impossible
# while still letting runtime adapters introduce new kinds.
EVENT_TYPE_PATTERN = r"^[a-z][a-z0-9_]{0,63}$"


class AgentJobCreate(BaseModel):
    """Request body for creating an agent job."""

    repo: str = Field(
        ...,
        # Shape-checked here as well as against the entitlement allowlist: the
        # value is interpolated into a clone URL and handed to git, which reads
        # a leading `-` as an option wherever it appears.
        pattern=REPO_PATTERN,
        max_length=140,
        description="Target repository, e.g. 'owner/name'.",
    )
    task_prompt: str = Field(..., description="What the agent should do.")
    runtime: str = Field("claude-code", description="Agent runtime id.")
    model: str = Field(..., description="Gateway model id the runtime should use.")
    setup_script: str | None = Field(
        None,
        max_length=8000,
        description=(
            "Shell run before the agent, under the setup egress tier. Its result is "
            "cached per repository and script, so a retry does not reinstall."
        ),
    )
    base_ref: str | None = Field(
        None,
        max_length=255,
        description=(
            "Branch to work from. Resolved to a commit at creation and stored as "
            "base_sha — a branch moves, and the publisher applies onto a pinned commit."
        ),
    )
    base_sha: str | None = Field(
        None,
        # A bare commit hash, enforced here as well as in the publisher: git
        # reads a leading `-` as an option even where an operand is expected,
        # so an unconstrained ref would be an argument injection into the
        # trusted process that holds the repository credential.
        pattern=r"^[0-9a-fA-F]{7,64}$",
        description="Commit SHA to work from.",
    )
    budget_usd: float = Field(
        DEFAULT_JOB_BUDGET_USD,
        gt=0,
        le=MAX_JOB_BUDGET_USD,
        # Always present and bounded: an absent budget would mean a live
        # sandbox credential with no spending limit at all.
        description="Cap on this job's model spend, in USD.",
    )
    metadata: dict[str, Any] | None = Field(None, description="Opaque caller metadata.")


class AgentJobResponse(BaseModel):
    """One agent job as returned to its owner."""

    id: str
    thread_id: str | None = None
    parent_job_id: str | None = None
    turn_no: int = 1
    repo: str
    task_prompt: str
    runtime: str
    model: str
    base_ref: str | None = None
    base_sha: str | None = None
    output_branch: str | None = None
    state: str
    cancel_requested: bool = False
    current_attempt_id: int | None = None
    published_pr_url: str | None = None
    published_commit_sha: str | None = None
    detail: str | None = None
    budget_usd: float | None = None
    # Set on turns created by a fork: the original turn this row copies.
    forked_from_job_id: str | None = None
    metadata: dict[str, Any] | None = None
    created_at: str | None = None
    updated_at: str | None = None
    pinned_at: str | None = None
    # Read from the billing ledger, never from anything the agent reports about
    # itself — the same rule the budget check already follows.
    spent_usd: float | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    model_calls: int | None = None
    # The deployment's egress posture for this job's two phases, so the owner
    # can see what the sandbox could reach rather than take it on trust.
    setup_egress_tier: str | None = None
    agent_egress_tier: str | None = None
    # Source control connection state. The composer is gated on this rather
    # than offering pickers that submit something else: with no App installed
    # there is no repository to work on, and a task box that looks ready is a
    # worse answer than one that says what is missing.
    github_connected: bool = False
    github_install_url: str | None = None


class AgentJobListResponse(BaseModel):
    """A page of agent jobs."""

    jobs: list[AgentJobResponse]


class AgentProject(BaseModel):
    """One repo the caller has run tasks in, summarized for the task tree."""

    repo: str
    # Conversations, not turns — the sidebar shows one row per conversation.
    task_count: int
    # Non-terminal jobs, so a collapsed project can still show live work.
    active_count: int
    last_activity_at: str | None = None
    pinned_count: int = 0
    pinned_at: str | None = None


class AgentProjectListResponse(BaseModel):
    """Every project the caller has tasks in, most recently active first."""

    projects: list[AgentProject]


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


class AgentThreadArchiveResponse(BaseModel):
    """Archive state for the thread containing a requested job."""

    thread_id: str
    archived: bool
    archived_at: str | None = None


class AgentThreadPinResponse(BaseModel):
    """Pin state for the thread containing a requested job."""

    thread_id: str
    pinned: bool
    pinned_at: str | None = None


class AgentFollowUpRequest(BaseModel):
    """A new user turn appended to an existing task thread."""

    prompt: str = Field(..., min_length=1, max_length=100_000)
    runtime: str | None = Field(None, description="Optional harness override for this turn.")
    model: str | None = Field(None, description="Optional model override for this turn.")
    budget_usd: float | None = Field(None, gt=0, le=MAX_JOB_BUDGET_USD)

    @field_validator("prompt")
    @classmethod
    def validate_prompt(cls, value: str) -> str:
        """Reject visually empty turns and persist the user's trimmed text."""
        prompt = value.strip()
        if not prompt:
            raise ValueError("prompt must not be blank")
        return prompt


class AgentRestartRequest(BaseModel):
    """Replacement prompt for a fresh task rooted at an existing job's base."""

    prompt: str = Field(..., min_length=1, max_length=100_000)

    @field_validator("prompt")
    @classmethod
    def validate_prompt(cls, value: str) -> str:
        """Reject visually empty prompts and persist the user's trimmed text."""
        prompt = value.strip()
        if not prompt:
            raise ValueError("prompt must not be blank")
        return prompt


class AgentThreadMessageResponse(BaseModel):
    """One durable user or assistant message in a task thread."""

    id: int
    role: str
    content: str
    job_id: str
    created_at: str | None = None


class AgentThreadResponse(BaseModel):
    """Conversation context and runs for the thread containing a job."""

    thread_id: str
    repo: str
    title: str
    messages: list[AgentThreadMessageResponse]
    jobs: list[AgentJobResponse]
    created_at: str | None = None
    updated_at: str | None = None


class AgentJobArtifactResponse(BaseModel):
    """One stored artifact (e.g. the produced patch)."""

    job_id: str
    attempt_id: int
    kind: str
    content: str
    created_at: str | None = None


class AgentWorkspaceEntry(BaseModel):
    """One safe child in an agent job's read-only workspace browser."""

    name: str
    path: str
    kind: Literal["file", "directory", "symlink"]
    size: int | None = None
    binary: bool = False
    truncated: bool = False
    status: Literal["added", "modified", "deleted"] | None = None
    omitted_reason: str | None = None


class AgentWorkspaceResponse(BaseModel):
    """A directory listing or bounded text-file preview from a job workspace."""

    path: str
    kind: Literal["file", "directory", "symlink"]
    entries: list[AgentWorkspaceEntry] | None = None
    content: str | None = None
    size: int | None = None
    binary: bool = False
    truncated: bool = False
    status: Literal["added", "modified", "deleted"] | None = None
    omitted_reason: str | None = None
    writable: bool = False
    source: Literal["workspace", "snapshot"] = "snapshot"


class AgentWorkspaceWriteRequest(BaseModel):
    """Replace one UTF-8 file in a live worktree."""

    content: str = Field(..., max_length=2 * 1024 * 1024)


class AgentTerminalRequest(BaseModel):
    """Execute one user-entered shell command in the workspace sandbox."""

    command: str = Field(..., min_length=1, max_length=8000)
    cwd: str = Field("/workspace", min_length=1, max_length=4096)
    timeout_seconds: float = Field(60.0, gt=0, le=120)


class AgentTerminalResponse(BaseModel):
    """Bounded output and resulting working directory for one command."""

    output: str
    stderr: str
    exit_code: int
    cwd: str


_MAX_TERMINAL_INPUT_BYTES = 64 * 1024
_MAX_TERMINAL_INPUT_BASE64_CHARS = 4 * ((_MAX_TERMINAL_INPUT_BYTES + 2) // 3)


class AgentTerminalSessionCreateRequest(BaseModel):
    """Open one interactive PTY after an agent run has settled."""

    rows: int = Field(24, ge=2, le=200)
    cols: int = Field(80, ge=20, le=500)


class AgentTerminalSessionInputRequest(BaseModel):
    """Base64-encoded bytes to write to an interactive PTY."""

    data: str = Field(..., min_length=1, max_length=_MAX_TERMINAL_INPUT_BASE64_CHARS)

    @field_validator("data")
    @classmethod
    def valid_bounded_base64(cls, value: str) -> str:
        """Reject malformed or oversized input before it reaches the broker."""
        try:
            decoded = base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("data must be valid base64") from exc
        if len(decoded) > _MAX_TERMINAL_INPUT_BYTES:
            raise ValueError("decoded terminal input must be at most 65536 bytes")
        return value


class AgentTerminalSessionResizeRequest(BaseModel):
    """Resize an interactive PTY."""

    rows: int = Field(..., ge=2, le=200)
    cols: int = Field(..., ge=20, le=500)


class AgentTerminalSessionResponse(BaseModel):
    """Owner-safe metadata for one interactive terminal session."""

    id: str = Field(..., pattern=r"^term_[A-Za-z0-9_-]{1,80}$")
    shell: str = Field(..., min_length=1, max_length=4096)
    state: str = Field(..., min_length=1, max_length=32)
    cwd: str = Field(..., min_length=1, max_length=4096)
    rows: int = Field(..., ge=2, le=200)
    cols: int = Field(..., ge=20, le=500)
    last_seq: int = Field(..., ge=0)


class AgentTerminalSessionListResponse(BaseModel):
    """Interactive terminal sessions retained by one live workspace."""

    terminals: list[AgentTerminalSessionResponse] = Field(default_factory=list)


class AgentGitChange(BaseModel):
    """One worktree status row."""

    code: str
    path: str


class AgentGitCommit(BaseModel):
    """One recent commit visible from the worktree."""

    sha: str
    short_sha: str
    subject: str
    author: str
    authored_at: str


class AgentGitWorkspaceResponse(BaseModel):
    """Live diff/status/commit data, or an unavailable legacy-workspace marker."""

    available: bool
    branch: str = ""
    changes: list[AgentGitChange] = Field(default_factory=list)
    patch: str = ""
    commits: list[AgentGitCommit] = Field(default_factory=list)


# ── Worker-facing (capability-token authenticated) ─────────────────────


class WorkerClaimRequest(BaseModel):
    """Worker request to claim the next queued job."""

    worker_id: str = Field(..., description="Stable identifier of the claiming worker.")
    lease_ttl_seconds: float = Field(
        120.0,
        gt=0,
        le=MAX_LEASE_TTL_SECONDS,
        description="How long the lease is valid without a heartbeat.",
    )
    # Bounded and charset-checked because it is a primary key an admin reads
    # off a page and clicks: an unconstrained field here lets a runner write
    # whatever it likes into the machine list.
    host: str | None = Field(
        None,
        max_length=253,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
        description=(
            "Machine this runner sits on, shared by its replicas. Joins the "
            "pool an operator can pin agent jobs to. Omitted by runners that "
            "predate host reporting."
        ),
    )


class WorkerClaimResponse(BaseModel):
    """A claimed job plus the capability token scoped to this attempt."""

    job_id: str
    thread_id: str | None = None
    parent_job_id: str | None = None
    turn_no: int = 1
    attempt_id: int
    attempt_no: int
    repo: str
    base_sha: str | None = None
    task_prompt: str
    setup_script: str | None = None
    runtime: str
    model: str
    worker_token: str
    sandbox_token: str = Field(
        "",
        description="Model-scoped credential; the only one that enters the sandbox.",
    )
    clone_token: str | None = Field(
        None,
        description=(
            "Short-lived read-only credential for this one repository, for the runner to "
            "check it out with. Stays in the runner; never enters the sandbox. Null when "
            "no GitHub App is configured, which is enough for a public repository."
        ),
    )
    context_messages: list[dict[str, str]] = Field(
        default_factory=list,
        description="Prior platform conversation turns, excluding the current prompt.",
    )
    context_patch: str | None = Field(
        None,
        description="Successful parent patch to rehydrate before this follow-up runs.",
    )
    metadata: dict[str, Any] | None = None


class WorkerHeartbeatRequest(BaseModel):
    """Worker lease renewal."""

    lease_ttl_seconds: float = Field(120.0, gt=0, le=MAX_LEASE_TTL_SECONDS)


class WorkerHeartbeatResponse(BaseModel):
    """Lease renewal result, carrying any pending cancellation."""

    ok: bool
    state: str
    cancel_requested: bool


class WorkerEventRequest(BaseModel):
    """One normalized event reported by a worker."""

    event_type: str = Field(
        ...,
        pattern=EVENT_TYPE_PATTERN,
        description="thinking|message|tool_use|...|lifecycle",
    )
    payload: dict[str, Any] | None = None


class WorkerEventResponse(BaseModel):
    """Accepted event id (the SSE cursor value)."""

    event_id: int


class WorkerTerminalSuspendRequest(BaseModel):
    """Protected workspace phase that requires every owner PTY to pause."""

    phase: Literal["workspace_preparing", "workspace_finalizing"]


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
    setup_script: str | None = Field(
        None,
        max_length=8000,
        description=(
            "Shell run before the agent, under the setup egress tier. Its result is "
            "cached per repository and script, so a retry does not reinstall."
        ),
    )
    base_ref: str | None = Field(
        None,
        max_length=255,
        description=(
            "Branch to work from. Resolved to a commit at creation and stored as "
            "base_sha — a branch moves, and the publisher applies onto a pinned commit."
        ),
    )
    base_sha: str | None = Field(
        None,
        pattern=r"^[0-9a-fA-F]{7,64}$",
        description=(
            "The commit the agent actually worked from. Recorded only when the job "
            "did not already carry one — the publisher cannot apply a patch without "
            "knowing its base, and a job may be submitted without naming a commit."
        ),
    )


class WorkerPublishRequest(BaseModel):
    """Publisher result recorded against the one-shot publish transition."""

    pr_url: str


class WorkerAckResponse(BaseModel):
    """Generic worker acknowledgement."""

    ok: bool
    state: str | None = None


class AgentConfigResponse(BaseModel):
    """What this deployment will actually accept, for the task composer.

    The composer used to show a repository, a branch, a runtime and a model as
    static labels while submitting different hardcoded values — so the UI
    described a job nobody was running. These are the real answers.
    """

    repos: list[str] = Field(
        default_factory=list,
        description="Repositories this deployment is entitled to work on. Empty means none.",
    )
    runtimes: list[str] = Field(default_factory=list, description="Runtime ids that can run here.")
    models: list[str] = Field(
        default_factory=list,
        description=(
            "Model ids an agent job can actually call for this user — the same "
            "predicate the create endpoint enforces, not the /v1/models list."
        ),
    )
    default_budget_usd: float = Field(
        DEFAULT_JOB_BUDGET_USD, description="Per-job spend cap applied when none is given."
    )
    setup_egress_tier: str | None = None
    agent_egress_tier: str | None = None
    # Source control connection state. The composer is gated on this rather
    # than offering pickers that submit something else: with no App installed
    # there is no repository to work on, and a task box that looks ready is a
    # worse answer than one that says what is missing.
    github_connected: bool = False
    github_install_url: str | None = None


class GitHubConnectRequest(BaseModel):
    """The callback values GitHub hands back after the user authorizes."""

    code: str = Field(..., min_length=1, max_length=512)
    state: str = Field(..., min_length=16, max_length=512)


class GitHubConnectionResponse(BaseModel):
    """Which GitHub installations this user has connected."""

    connections: list[dict[str, Any]] = Field(default_factory=list)
    repos: list[str] = Field(default_factory=list)
    install_url: str | None = None


class SourceControlAccount(BaseModel):
    """Public identity metadata for a connected provider account."""

    id: str
    label: str
    web_url: str | None = None


class SourceControlRepository(BaseModel):
    """A provider-attested repository safe to render in the UI."""

    id: str
    name: str
    web_url: str | None = None


class SourceControlProviderResponse(BaseModel):
    """Connection and capability status for one supported provider."""

    provider: Literal["github", "gitlab"]
    configured: bool
    connected: bool
    connect_url: str | None = None
    manage_url: str | None = None
    capabilities: list[str] = Field(default_factory=list)
    accounts: list[SourceControlAccount] = Field(default_factory=list)
    repositories: list[SourceControlRepository] = Field(default_factory=list)
    error: str | None = None


class SourceControlIntegrationsResponse(BaseModel):
    """All source-control providers available to the current user."""

    providers: list[SourceControlProviderResponse]


class OAuthConnectRequest(BaseModel):
    """Authorization callback values supplied by GitHub or GitLab."""

    code: str = Field(..., min_length=1, max_length=2048)
    state: str = Field(..., min_length=16, max_length=512)


class RepoBranchesResponse(BaseModel):
    """Branches of one repository the caller is entitled to."""

    default: str | None = None
    branches: list[str] = Field(default_factory=list)
