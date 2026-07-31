"""Agent-sandbox job API (``/v1/agent/jobs``) — issue #1041, P0.

Two audiences, two authentication schemes:

- **Owners** (users, via a browser JWT or normal API key): create, inspect,
  list, cancel jobs, read artifacts, and subscribe to the event stream. Every
  read and mutation is scoped to ``user_id`` so one user can never touch
  another's job. P0 remains dogfood-only, so the same dependency also requires
  the ``internal`` role (admins satisfy it through the normal role hierarchy).
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
import secrets
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query, Request
from fastapi.responses import StreamingResponse

from serving.agent_jobs.egress import (
    EgressPolicyError,
    build_policy_from_env as build_egress_policy,
)
from serving.agent_jobs.entitlement import (
    REPO_PATTERN,
    RepoNotAllowed,
    repos_for_user,
    require_entitled_repo,
)
from serving.agent_jobs.github_app import AppNotInstalled, GitHubAppError
from serving.agent_jobs.model_auth import looks_like_agent_token
from serving.agent_jobs.patch_gate import branch_name_for
from serving.agent_jobs.runtimes import registered_runtimes
from serving.agent_jobs.source_control import (
    OAuthStateError,
    SourceControlError,
    consume_oauth_state,
    github_authorization_url,
    issue_oauth_state,
)
from serving.agent_jobs.terminal_coordination import resume_settled_terminal
from serving.agent_jobs.tokens import (
    SCOPE_FULL,
    SCOPE_MODEL,
    InvalidAgentToken,
    mint_worker_token,
    parse_worker_token,
)
from serving.agent_jobs.visible_models import agent_model_resolvable, agent_visible_models
from serving.agent_jobs.workspace_broker_client import (
    WorkspaceBrokerClient,
    WorkspaceBrokerError,
    workspace_broker_from_env,
)
from serving.agent_jobs.workspace_browser import (
    WorkspacePathError,
    WorkspaceSnapshotError,
    github_file_response,
    merge_directory_entries,
    normalize_workspace_path,
    overlay_has_directory,
    parse_workspace_snapshot,
    snapshot_file_response,
)
from serving.schemas_agent_jobs import (
    DEFAULT_JOB_BUDGET_USD,
    EVENT_TYPE_PATTERN,
    AgentConfigResponse,
    AgentFollowUpRequest,
    AgentGitWorkspaceResponse,
    AgentJobArtifactResponse,
    AgentJobCancelResponse,
    AgentJobCreate,
    AgentJobEvent,
    AgentJobEventsResponse,
    AgentJobListResponse,
    AgentJobResponse,
    AgentProject,
    AgentProjectListResponse,
    AgentRestartRequest,
    AgentTerminalRequest,
    AgentTerminalResponse,
    AgentTerminalSessionCreateRequest,
    AgentTerminalSessionInputRequest,
    AgentTerminalSessionListResponse,
    AgentTerminalSessionResizeRequest,
    AgentTerminalSessionResponse,
    AgentThreadArchiveResponse,
    AgentThreadMessageResponse,
    AgentThreadPinResponse,
    AgentThreadResponse,
    AgentWorkspaceResponse,
    AgentWorkspaceWriteRequest,
    GitHubConnectionResponse,
    GitHubConnectRequest,
    OAuthConnectRequest,
    RepoBranchesResponse,
    SourceControlAccount,
    SourceControlIntegrationsResponse,
    SourceControlProviderResponse,
    SourceControlRepository,
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
    WorkerTerminalSuspendRequest,
)
from serving.servers.auth import verify_api_key
from serving.servers.deps import (
    get_agent_app_credentials,
    get_agent_gitlab_oauth,
    get_agent_job_store,
    get_current_user,
    get_log_store,
    get_model_visibility_resolver,
    get_operational_store,
    get_router,
    verify_admin_access,
)
from serving.storage.agent_job_store import PUBLISHING, RUNNING, TERMINAL_STATES
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
_TERMINAL_ID_PATTERN = r"^term_[A-Za-z0-9_-]{1,80}$"

# The clone credential is narrowed at the point it is minted, not merely by
# convention: the App also holds `contents: write` for the publisher, and an
# installation token inherits every permission the App has unless it is asked
# for less. Without this the runner would be handed push rights it must not
# have, on a host that runs untrusted repository code.
_CLONE_SCOPE = {"contents": "read"}
_BASE_REF_METADATA_KEY = "_agent_base_ref"


def _looks_like_browser_jwt(authorization: str | None) -> bool:
    """Distinguish a web access JWT from API-key and sandbox credentials.

    Browser login tokens are compact JWTs (three dot-separated segments).
    Agent capability tokens are also structured, so exclude their dedicated
    namespace before using the JWT dependency. Normal API keys, including
    legacy opaque keys, continue through ``verify_api_key``.
    """
    if not authorization or not authorization.startswith("Bearer "):
        return False
    token = authorization[7:]
    return token.count(".") == 2 and not looks_like_agent_token(token)


async def authenticate_agent_owner(
    request: Request,
    authorization: str | None = Header(None),
    x_api_key: str | None = Header(None, alias="X-API-Key"),
    op_store=Depends(get_operational_store),
    log_store=Depends(get_log_store),
    agent_job_store=Depends(get_agent_job_store),
) -> dict[str, Any]:
    """Authenticate an Agent owner from either web JWT or normal API key.

    The Agents web app sends the access JWT returned by ``/auth/login``. API
    clients send ``hyi-*`` keys. Passing both credential types to
    ``verify_api_key`` made the web JWT look like an invalid API key, leaving
    an otherwise logged-in user unable to load config or start a job.

    Credential namespaces are selected before validation rather than by
    catching one validator and falling back to the other: an expired or forged
    JWT must remain a JWT authentication failure, not get reinterpreted as an
    API key.
    """
    if _looks_like_browser_jwt(authorization):
        return await get_current_user(authorization=authorization, op_store=op_store)

    return await verify_api_key(
        request=request,
        authorization=authorization,
        x_api_key=x_api_key,
        op_store=op_store,
        log_store=log_store,
        agent_job_store=agent_job_store,
    )


async def require_agent_owner(
    user: dict[str, Any] = Depends(authenticate_agent_owner),
) -> dict[str, Any]:
    """Keep the P0 Agent surface restricted to internal/admin dogfood users."""
    from serving.config.settings import has_role

    if not has_role(user.get("role", "free"), "internal"):
        raise HTTPException(status_code=403, detail="Agent access requires role 'internal'.")
    return user


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
    metadata = dict(job.get("metadata") or {})
    base_ref = metadata.pop(_BASE_REF_METADATA_KEY, None)
    if not isinstance(base_ref, str):
        base_ref = None
    return AgentJobResponse(
        id=job["id"],
        thread_id=job.get("thread_id"),
        parent_job_id=job.get("parent_job_id"),
        turn_no=job.get("turn_no") or 1,
        repo=job["repo"],
        task_prompt=job["task_prompt"],
        runtime=job["runtime"],
        model=job["model"],
        base_ref=base_ref,
        base_sha=job["base_sha"],
        output_branch=branch_name_for(job.get("thread_id") or job["id"]),
        state=job["state"],
        cancel_requested=job["cancel_requested"],
        current_attempt_id=job["current_attempt_id"],
        published_pr_url=job["published_pr_url"],
        published_commit_sha=job.get("published_commit_sha"),
        detail=job["detail"],
        budget_usd=job.get("budget_usd"),
        forked_from_job_id=job.get("fork_source_job_id"),
        metadata=metadata or None,
        created_at=_iso(job["created_at"]),
        updated_at=_iso(job["updated_at"]),
        pinned_at=_iso(job.get("pinned_at")),
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
    user: dict[str, Any] = Depends(require_agent_owner),
    store: AgentJobStore | None = Depends(get_agent_job_store),
    app_credentials: Any | None = Depends(get_agent_app_credentials),
    router_exec: Any = Depends(get_router),
    model_visibility_resolver: Any = Depends(get_model_visibility_resolver),
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
        # Same predicate the create endpoint enforces and the sandbox's calls
        # will hit — not /v1/models, which answers for the *browsing* user and
        # offered models whose first agent call then 404ed.
        models=await agent_visible_models(
            router_exec, visibility_resolver=model_visibility_resolver, user_ctx=user
        ),
        default_budget_usd=DEFAULT_JOB_BUDGET_USD,
        setup_egress_tier=setup_tier,
        agent_egress_tier=agent_tier,
        # Both halves, and read from what is actually wired rather than from
        # the environment: an App the platform can mint tokens from, and at
        # least one repository this user may work on. Either alone leaves a
        # composer that cannot produce a runnable job.
        github_connected=bool(app_credentials is not None and repos),
        # OAuth must start on the authenticated integrations page so the
        # server can issue state bound to this user. Never expose the old raw
        # operator URL here: it had no CSRF binding.
        github_install_url="/agents/integrations" if app_credentials is not None else None,
    )


def _oauth_error(exc: Exception) -> HTTPException:
    """Map OAuth failures to a stable response without leaking credentials."""
    if isinstance(exc, OAuthStateError):
        message = str(exc)
        error_type = "oauth_state_invalid"
    else:
        message = "The source-control authorization could not be completed. Please try again."
        error_type = "source_control_connect_failed"
    return HTTPException(
        status_code=400,
        detail={"error": {"type": error_type, "message": message}},
    )


async def _connect_github_for_user(
    *,
    body: GitHubConnectRequest | OAuthConnectRequest,
    user_id: str,
    store: AgentJobStore,
    app_credentials: Any,
) -> GitHubConnectionResponse:
    await consume_oauth_state(
        store,
        state=body.state,
        user_id=user_id,
        provider="github",
    )
    user_token = await app_credentials.exchange_user_code(body.code)
    installations = await app_credentials.installations_for_user(user_token)
    install_url = None
    if not installations:
        github_install_url = (os.getenv("AGENT_GITHUB_APP_INSTALL_URL") or "").strip()
        if github_install_url:
            install_state = await issue_oauth_state(store, user_id=user_id, provider="github")
            install_url = github_authorization_url(github_install_url, state=install_state)
    for installation in installations:
        await store.record_repo_grant(
            user_id=user_id,
            installation_id=installation["installation_id"],
            account_login=installation.get("account_login"),
        )
    logger.info(
        "agent_github_connected",
        extra={"event": "agent_github_connected", "installations": len(installations)},
    )
    return GitHubConnectionResponse(
        connections=await store.list_repo_grants(user_id=user_id),
        repos=await repos_for_user(user_id, store=store, app_credentials=app_credentials),
        install_url=install_url,
    )


async def _source_control_providers(
    *,
    user_id: str,
    store: AgentJobStore,
    app_credentials: Any | None,
    gitlab_oauth: Any | None,
) -> list[SourceControlProviderResponse]:
    """Build user-scoped provider status with no credential material."""
    github_install_url = (os.getenv("AGENT_GITHUB_APP_INSTALL_URL") or "").strip()
    github_configured = (
        app_credentials is not None
        and bool(github_install_url)
        and bool(getattr(app_credentials, "user_authorization_configured", True))
    )
    github_connect_url = None
    github_error = None
    if github_configured:
        try:
            state = await issue_oauth_state(store, user_id=user_id, provider="github")
            github_connect_url = app_credentials.user_authorization_url(state)
        except (GitHubAppError, SourceControlError) as exc:
            github_configured = False
            github_error = str(exc)

    grants = await store.list_repo_grants(user_id=user_id)
    # The install URL is the wrong way to *start* a connection — GitHub sends
    # an already-installed App there straight to its settings page instead of
    # through the callback — but that settings page is the only route to
    # adding or removing the App's repositories, and GitHub routes user and
    # organization installations to the right one without us having to know
    # which this is. State rides along because a changed installation comes
    # back through the same callback, and it is issued separately from the
    # connect state so that following one link cannot invalidate the other.
    github_manage_url = None
    if github_configured and grants:
        try:
            manage_state = await issue_oauth_state(store, user_id=user_id, provider="github")
            github_manage_url = github_authorization_url(github_install_url, state=manage_state)
        except SourceControlError as exc:
            # A malformed install URL no longer breaks connecting, so it must
            # not surface as a connection error — but an operator still needs
            # to hear that the management link is missing because of it.
            logger.warning(
                "agent_github_manage_url_unavailable",
                extra={"event": "agent_github_manage_url_unavailable", "reason": str(exc)},
            )

    github_repos = await repos_for_user(
        user_id, store=store, app_credentials=app_credentials, env={"AGENT_REPO_ALLOWLIST": ""}
    )
    providers = [
        SourceControlProviderResponse(
            provider="github",
            configured=github_configured,
            connected=bool(grants),
            connect_url=github_connect_url,
            manage_url=github_manage_url,
            capabilities=["Agent checkout", "branch discovery", "draft PR publishing"],
            accounts=[
                SourceControlAccount(
                    id=str(grant["installation_id"]),
                    label=grant.get("account_login") or f"Installation {grant['installation_id']}",
                )
                for grant in grants
            ],
            repositories=[
                SourceControlRepository(
                    id=repo,
                    name=repo,
                    web_url=f"https://github.com/{repo}",
                )
                for repo in github_repos
            ],
            error=github_error,
        )
    ]

    gitlab_connection = await store.get_gitlab_connection(user_id=user_id)
    gitlab_projects: list[dict[str, Any]] = []
    gitlab_error = None
    gitlab_connect_url = None
    if gitlab_oauth is not None:
        try:
            gitlab_connect_url = await gitlab_oauth.authorization_url(store, user_id=user_id)
            if gitlab_connection:
                gitlab_projects = await gitlab_oauth.projects_for_connection(
                    store, gitlab_connection
                )
        except SourceControlError:
            if gitlab_connection:
                gitlab_error = "GitLab repositories are temporarily unavailable."
            else:
                gitlab_error = "GitLab authorization is temporarily unavailable."
    providers.append(
        SourceControlProviderResponse(
            provider="gitlab",
            configured=gitlab_oauth is not None,
            connected=gitlab_connection is not None,
            connect_url=gitlab_connect_url,
            capabilities=["project discovery"],
            accounts=(
                [
                    SourceControlAccount(
                        id=str(gitlab_connection["id"]),
                        label=gitlab_connection["username"],
                        web_url=gitlab_connection.get("web_url"),
                    )
                ]
                if gitlab_connection
                else []
            ),
            repositories=[SourceControlRepository(**project) for project in gitlab_projects],
            error=gitlab_error,
        )
    )
    return providers


@router.get("/integrations", response_model=SourceControlIntegrationsResponse)
async def list_source_control_integrations(
    user: dict[str, Any] = Depends(require_agent_owner),
    store: AgentJobStore | None = Depends(get_agent_job_store),
    app_credentials: Any | None = Depends(get_agent_app_credentials),
    gitlab_oauth: Any | None = Depends(get_agent_gitlab_oauth),
) -> SourceControlIntegrationsResponse:
    """Return configured/connected state for GitHub and GitLab.com."""
    job_store = _require_store(store)
    return SourceControlIntegrationsResponse(
        providers=await _source_control_providers(
            user_id=user["user_id"],
            store=job_store,
            app_credentials=app_credentials,
            gitlab_oauth=gitlab_oauth,
        )
    )


@router.post("/integrations/github/connect", response_model=GitHubConnectionResponse)
async def connect_github_integration(
    body: OAuthConnectRequest,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: AgentJobStore | None = Depends(get_agent_job_store),
    app_credentials: Any | None = Depends(get_agent_app_credentials),
) -> GitHubConnectionResponse:
    """Complete a GitHub App authorization protected by one-time state."""
    job_store = _require_store(store)
    if app_credentials is None:
        raise HTTPException(status_code=503, detail="GitHub App is not configured")
    try:
        return await _connect_github_for_user(
            body=body,
            user_id=user["user_id"],
            store=job_store,
            app_credentials=app_credentials,
        )
    except Exception as exc:
        if not isinstance(exc, (OAuthStateError, SourceControlError)):
            logger.warning("agent_github_connect_failed", exc_info=True)
        raise _oauth_error(exc) from exc


@router.post("/integrations/gitlab/connect", response_model=SourceControlProviderResponse)
async def connect_gitlab_integration(
    body: OAuthConnectRequest,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: AgentJobStore | None = Depends(get_agent_job_store),
    gitlab_oauth: Any | None = Depends(get_agent_gitlab_oauth),
) -> SourceControlProviderResponse:
    """Verify a GitLab user and persist only encrypted, refreshable tokens."""
    job_store = _require_store(store)
    if gitlab_oauth is None:
        raise HTTPException(status_code=503, detail="GitLab OAuth is not configured")
    try:
        verifier = await consume_oauth_state(
            job_store,
            state=body.state,
            user_id=user["user_id"],
            provider="gitlab",
            cipher=gitlab_oauth.cipher,
        )
        if verifier is None:
            raise OAuthStateError("The source-control authorization expired or is invalid.")
        token = await gitlab_oauth.exchange_code(body.code, verifier)
        profile = await gitlab_oauth.verify_user(token["access_token"])
        projects = await gitlab_oauth.list_projects(token["access_token"])
        previous_connection = await job_store.get_gitlab_connection(user_id=user["user_id"])
        connection_id = await job_store.upsert_gitlab_connection(
            user_id=user["user_id"],
            external_user_id=profile["id"],
            username=profile["username"],
            display_name=profile.get("name"),
            web_url=profile.get("web_url"),
            access_token_ciphertext=gitlab_oauth.cipher.encrypt(token["access_token"]),
            refresh_token_ciphertext=gitlab_oauth.cipher.encrypt(token["refresh_token"]),
            expires_at=gitlab_oauth.token_expiry(token),
        )
        if previous_connection is not None:
            try:
                await gitlab_oauth.revoke(
                    gitlab_oauth.cipher.decrypt(previous_connection["access_token_ciphertext"])
                )
            except Exception:
                logger.warning("agent_gitlab_previous_token_revoke_failed", exc_info=True)
    except Exception as exc:
        if not isinstance(exc, (OAuthStateError, SourceControlError)):
            logger.warning("agent_gitlab_connect_failed", exc_info=True)
        raise _oauth_error(exc) from exc
    logger.info("agent_gitlab_connected", extra={"event": "agent_gitlab_connected"})
    return SourceControlProviderResponse(
        provider="gitlab",
        configured=True,
        connected=True,
        capabilities=["project discovery"],
        accounts=[
            SourceControlAccount(
                id=str(connection_id), label=profile["username"], web_url=profile.get("web_url")
            )
        ],
        repositories=[SourceControlRepository(**project) for project in projects],
    )


@router.delete(
    "/integrations/gitlab/connections/{connection_id}",
    response_model=SourceControlIntegrationsResponse,
)
async def disconnect_gitlab_integration(
    connection_id: int,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: AgentJobStore | None = Depends(get_agent_job_store),
    app_credentials: Any | None = Depends(get_agent_app_credentials),
    gitlab_oauth: Any | None = Depends(get_agent_gitlab_oauth),
) -> SourceControlIntegrationsResponse:
    """Revoke GitLab best-effort, then always remove the local credential."""
    job_store = _require_store(store)
    connection = await job_store.get_gitlab_connection(user_id=user["user_id"])
    if connection is None or connection["id"] != connection_id:
        raise HTTPException(status_code=404, detail="No such GitLab connection")
    if gitlab_oauth is not None:
        try:
            access_token = await gitlab_oauth.access_token_for_connection(job_store, connection)
            await gitlab_oauth.revoke(access_token)
        except Exception:
            logger.warning("agent_gitlab_revoke_failed", exc_info=True)
    await job_store.delete_gitlab_connection(user_id=user["user_id"], connection_id=connection_id)
    return SourceControlIntegrationsResponse(
        providers=await _source_control_providers(
            user_id=user["user_id"],
            store=job_store,
            app_credentials=app_credentials,
            gitlab_oauth=gitlab_oauth,
        )
    )


@router.delete(
    "/integrations/github/connections/{installation_id}",
    response_model=GitHubConnectionResponse,
)
async def disconnect_github_integration(
    installation_id: int,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: AgentJobStore | None = Depends(get_agent_job_store),
    app_credentials: Any | None = Depends(get_agent_app_credentials),
) -> GitHubConnectionResponse:
    """Drop only the authenticated user's GitHub installation grant."""
    job_store = _require_store(store)
    await job_store.revoke_repo_grant(user_id=user["user_id"], installation_id=installation_id)
    return GitHubConnectionResponse(
        connections=await job_store.list_repo_grants(user_id=user["user_id"]),
        repos=await repos_for_user(
            user["user_id"], store=job_store, app_credentials=app_credentials
        ),
    )


@router.post("/github/connect", response_model=GitHubConnectionResponse)
async def connect_github(
    body: GitHubConnectRequest,
    user: dict[str, Any] = Depends(require_agent_owner),
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
        return await _connect_github_for_user(
            body=body,
            user_id=user["user_id"],
            store=job_store,
            app_credentials=app_credentials,
        )
    except Exception as exc:
        if not isinstance(exc, (OAuthStateError, SourceControlError)):
            logger.warning("agent_github_connect_failed", exc_info=True)
        raise _oauth_error(exc) from exc


@router.delete("/github/connect/{installation_id}", response_model=GitHubConnectionResponse)
async def disconnect_github(
    installation_id: int,
    user: dict[str, Any] = Depends(require_agent_owner),
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
    user: dict[str, Any] = Depends(require_agent_owner),
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


async def _require_resolvable_model(
    model: str,
    *,
    router_exec: Any,
    model_visibility_resolver: Any,
    user: dict[str, Any],
) -> None:
    """Fail-fast on a model the job's first call would 404 on.

    Rejecting here costs a 400. Accepting costs a claim, a clone, and an
    attempt before the sandbox's first model call dies with an error that
    reads like a broken model.
    """
    if await agent_model_resolvable(
        model, router_exec, visibility_resolver=model_visibility_resolver, user_ctx=user
    ):
        return
    visible = await agent_visible_models(
        router_exec, visibility_resolver=model_visibility_resolver, user_ctx=user
    )
    raise HTTPException(
        status_code=400,
        detail={
            "error": {
                "type": "model_not_available",
                "message": (
                    f"Model {model!r} is not available to agent jobs on this deployment. "
                    f"Available: {', '.join(visible[:20]) or 'none'}."
                ),
            }
        },
    )


@router.post("/jobs", response_model=AgentJobResponse, status_code=201)
async def create_agent_job(
    body: AgentJobCreate,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: AgentJobStore | None = Depends(get_agent_job_store),
    app_credentials: Any | None = Depends(get_agent_app_credentials),
    router_exec: Any = Depends(get_router),
    model_visibility_resolver: Any = Depends(get_model_visibility_resolver),
) -> AgentJobResponse:
    """Queue a new agent job for the authenticated user."""
    job_store = _require_store(store)
    await _require_resolvable_model(
        body.model,
        router_exec=router_exec,
        model_visibility_resolver=model_visibility_resolver,
        user=user,
    )
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

    metadata = dict(body.metadata or {})
    # Reserved display fact: caller metadata must not be able to claim the
    # job was pinned from a different branch than the one the API resolved.
    metadata.pop(_BASE_REF_METADATA_KEY, None)
    if body.base_ref:
        metadata[_BASE_REF_METADATA_KEY] = body.base_ref
    job = await job_store.create_job(
        user_id=user["user_id"],
        repo=body.repo,
        task_prompt=body.task_prompt,
        runtime=body.runtime,
        model=body.model,
        base_sha=base_sha,
        setup_script=body.setup_script,
        budget_usd=body.budget_usd,
        metadata=metadata or None,
    )
    logger.info(
        "agent_job_created",
        extra={"event": "agent_job_created", "job_id": job["id"], "runtime": body.runtime},
    )
    return _job_response(job)


@router.get("/jobs", response_model=AgentJobListResponse)
async def list_agent_jobs(
    limit: int = Query(50, ge=1, le=200),
    archived: bool = Query(False),
    repo: str | None = Query(None, pattern=REPO_PATTERN, max_length=140),
    user: dict[str, Any] = Depends(require_agent_owner),
    store: AgentJobStore | None = Depends(get_agent_job_store),
) -> AgentJobListResponse:
    """List jobs from the authenticated user's active or archived threads.

    ``repo`` narrows the page to one project. It is a filter over what the
    caller already owns, not an access grant, so an unentitled repo is not an
    error here — it simply matches none of their rows.
    """
    job_store = _require_store(store)
    jobs = await job_store.list_jobs(
        user_id=user["user_id"], limit=limit, archived=archived, repo=repo
    )
    return AgentJobListResponse(jobs=[_job_response(job) for job in jobs])


@router.get("/projects", response_model=AgentProjectListResponse)
async def list_agent_projects(
    archived: bool = Query(False),
    user: dict[str, Any] = Depends(require_agent_owner),
    store: AgentJobStore | None = Depends(get_agent_job_store),
) -> AgentProjectListResponse:
    """Summarize the caller's projects for the sidebar's task tree."""
    job_store = _require_store(store)
    projects = await job_store.list_projects(user_id=user["user_id"], archived=archived)
    return AgentProjectListResponse(
        projects=[
            AgentProject(
                repo=project["repo"],
                task_count=project["task_count"],
                active_count=project["active_count"],
                last_activity_at=_iso(project["last_activity_at"]),
                pinned_count=project.get("pinned_count", 0),
                pinned_at=_iso(project.get("pinned_at")),
            )
            for project in projects
        ]
    )


@router.get("/jobs/{job_id}", response_model=AgentJobResponse)
async def get_agent_job(
    job_id: str,
    user: dict[str, Any] = Depends(require_agent_owner),
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
    user: dict[str, Any] = Depends(require_agent_owner),
    store: AgentJobStore | None = Depends(get_agent_job_store),
) -> AgentJobCancelResponse:
    """Request cancellation of one of the caller's agent jobs.

    Queued jobs cancel immediately; a running job is flagged and the worker
    performs the fenced terminal transition when it next heartbeats.
    """
    job_store = _require_store(store)
    await _owned_job(job_store, job_id, user)
    state = await job_store.request_cancel(job_id=job_id, user_id=user["user_id"])
    if state in TERMINAL_STATES and await resume_settled_terminal(job_id):
        await job_store.mark_terminal_resume_complete(job_id=job_id)
    job = await job_store.get_job(job_id)
    return AgentJobCancelResponse(
        id=job_id,
        state=state or (job or {}).get("state", "unknown"),
        cancel_requested=bool((job or {}).get("cancel_requested")),
    )


@router.post("/jobs/{job_id}/restart", response_model=AgentJobResponse, status_code=201)
async def restart_agent_job(
    job_id: str,
    body: AgentRestartRequest,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: AgentJobStore | None = Depends(get_agent_job_store),
    app_credentials: Any | None = Depends(get_agent_app_credentials),
    router_exec: Any = Depends(get_router),
    model_visibility_resolver: Any = Depends(get_model_visibility_resolver),
) -> AgentJobResponse:
    """Start a new root task from an owned job's original pinned base."""
    job_store = _require_store(store)
    source = await _owned_job(job_store, job_id, user)
    await _require_resolvable_model(
        source["model"],
        router_exec=router_exec,
        model_visibility_resolver=model_visibility_resolver,
        user=user,
    )
    try:
        await require_entitled_repo(
            source["repo"],
            user["user_id"],
            store=job_store,
            app_credentials=app_credentials,
        )
    except RepoNotAllowed as exc:
        raise HTTPException(
            status_code=403,
            detail={"error": {"type": "repo_not_allowed", "message": str(exc)}},
        ) from exc

    restarted = await job_store.create_job(
        user_id=user["user_id"],
        repo=source["repo"],
        task_prompt=body.prompt,
        runtime=source["runtime"],
        model=source["model"],
        base_sha=source.get("base_sha"),
        setup_script=source.get("setup_script"),
        budget_usd=source.get("budget_usd"),
        metadata=dict(source.get("metadata") or {}) or None,
    )
    logger.info(
        "agent_job_restarted",
        extra={
            "event": "agent_job_restarted",
            "job_id": restarted["id"],
            "source_job_id": job_id,
        },
    )
    return _job_response(restarted)


async def _set_agent_thread_archived(
    *,
    job_id: str,
    archived: bool,
    user: dict[str, Any],
    store: AgentJobStore | None,
) -> AgentThreadArchiveResponse:
    """Set archive state without exposing whether another user's job exists."""
    job_store = _require_store(store)
    result = await job_store.set_thread_archived(
        job_id=job_id,
        user_id=user["user_id"],
        archived=archived,
    )
    if result is None:
        raise HTTPException(
            status_code=404,
            detail={"error": {"type": "not_found", "message": f"No such agent job: {job_id}"}},
        )
    return AgentThreadArchiveResponse(
        thread_id=result["thread_id"],
        archived=archived,
        archived_at=_iso(result["archived_at"]),
    )


@router.post("/jobs/{job_id}/archive", response_model=AgentThreadArchiveResponse)
async def archive_agent_thread(
    job_id: str,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: AgentJobStore | None = Depends(get_agent_job_store),
) -> AgentThreadArchiveResponse:
    """Archive the entire task thread containing an owned job."""
    return await _set_agent_thread_archived(job_id=job_id, archived=True, user=user, store=store)


@router.delete("/jobs/{job_id}/archive", response_model=AgentThreadArchiveResponse)
async def restore_agent_thread(
    job_id: str,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: AgentJobStore | None = Depends(get_agent_job_store),
) -> AgentThreadArchiveResponse:
    """Restore the entire task thread containing an owned job."""
    return await _set_agent_thread_archived(job_id=job_id, archived=False, user=user, store=store)


async def _set_agent_thread_pinned(
    *,
    job_id: str,
    pinned: bool,
    user: dict[str, Any],
    store: AgentJobStore | None,
) -> AgentThreadPinResponse:
    """Set pin state without exposing whether another user's job exists."""
    job_store = _require_store(store)
    result = await job_store.set_thread_pinned(
        job_id=job_id,
        user_id=user["user_id"],
        pinned=pinned,
    )
    if result is None:
        raise HTTPException(
            status_code=404,
            detail={"error": {"type": "not_found", "message": f"No such agent job: {job_id}"}},
        )
    return AgentThreadPinResponse(
        thread_id=result["thread_id"],
        pinned=pinned,
        pinned_at=_iso(result["pinned_at"]),
    )


@router.post("/jobs/{job_id}/pin", response_model=AgentThreadPinResponse)
async def pin_agent_thread(
    job_id: str,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: AgentJobStore | None = Depends(get_agent_job_store),
) -> AgentThreadPinResponse:
    """Pin the entire task thread containing an owned job."""
    return await _set_agent_thread_pinned(job_id=job_id, pinned=True, user=user, store=store)


@router.delete("/jobs/{job_id}/pin", response_model=AgentThreadPinResponse)
async def unpin_agent_thread(
    job_id: str,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: AgentJobStore | None = Depends(get_agent_job_store),
) -> AgentThreadPinResponse:
    """Unpin the entire task thread containing an owned job."""
    return await _set_agent_thread_pinned(job_id=job_id, pinned=False, user=user, store=store)


@router.post("/jobs/{job_id}/follow-ups", response_model=AgentJobResponse, status_code=201)
async def create_agent_follow_up(
    job_id: str,
    body: AgentFollowUpRequest,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: AgentJobStore | None = Depends(get_agent_job_store),
    app_credentials: Any | None = Depends(get_agent_app_credentials),
    router_exec: Any = Depends(get_router),
    model_visibility_resolver: Any = Depends(get_model_visibility_resolver),
) -> AgentJobResponse:
    """Append a turn to a task thread and queue its next isolated run."""
    job_store = _require_store(store)
    parent = await _owned_job(job_store, job_id, user)
    # A follow-up may switch models; the switched-to model gets the same
    # fail-fast as a fresh job. An omitted model inherits the parent's, which
    # was validated when it was chosen.
    if body.model:
        await _require_resolvable_model(
            body.model,
            router_exec=router_exec,
            model_visibility_resolver=model_visibility_resolver,
            user=user,
        )
    try:
        await require_entitled_repo(
            parent["repo"],
            user["user_id"],
            store=job_store,
            app_credentials=app_credentials,
        )
    except RepoNotAllowed as exc:
        raise HTTPException(
            status_code=403,
            detail={"error": {"type": "repo_not_allowed", "message": str(exc)}},
        ) from exc
    job = await job_store.create_follow_up(
        parent_job_id=job_id,
        user_id=user["user_id"],
        prompt=body.prompt,
        runtime=body.runtime,
        model=body.model,
        budget_usd=body.budget_usd,
    )
    if job is None:
        raise HTTPException(
            status_code=404,
            detail={"error": {"type": "not_found", "message": f"No such agent job: {job_id}"}},
        )
    # Jobs published before thread support have no recorded commit SHA. Their
    # migrated thread id preserves the old branch name, so resolve that branch
    # once and pin the new turn to it before any worker can claim the turn.
    effective_parent = await job_store.get_job(job.get("parent_job_id") or "")
    if (
        job["state"] == "waiting"
        and effective_parent is not None
        and effective_parent.get("published_pr_url")
        and not effective_parent.get("published_commit_sha")
    ):
        try:
            if app_credentials is None:
                raise RuntimeError("the GitHub App is unavailable")
            branch_sha = await app_credentials.resolve_ref(
                job["repo"], branch_name_for(job.get("thread_id") or effective_parent["id"])
            )
            await job_store.resolve_legacy_published_commit(
                parent_job_id=effective_parent["id"], commit_sha=branch_sha
            )
        except Exception as exc:
            await job_store.fail_waiting_follow_up(
                job_id=job["id"],
                detail=f"could not resume the existing draft PR branch: {exc}",
            )
        job = await job_store.get_job(job["id"]) or job
    logger.info(
        "agent_follow_up_created",
        extra={
            "event": "agent_follow_up_created",
            "job_id": job["id"],
            "thread_id": job.get("thread_id"),
            "parent_job_id": job.get("parent_job_id"),
        },
    )
    return _job_response(job)


@router.post("/jobs/{job_id}/fork", response_model=AgentJobResponse, status_code=201)
async def fork_agent_job(
    job_id: str,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: AgentJobStore | None = Depends(get_agent_job_store),
) -> AgentJobResponse:
    """Copy the conversation up to this settled turn into a new thread.

    The fork is a duplicate of durable history only: nothing is queued and
    nothing is published until the owner sends the next turn there, and a
    still-running turn cannot be an anchor — its output is not history yet.
    Returns the copied anchor turn, which is where the caller navigates.
    """
    job_store = _require_store(store)
    job = await _owned_job(job_store, job_id, user)
    not_settled = HTTPException(
        status_code=409,
        detail={
            "error": {
                "type": "not_settled",
                "message": "This turn is still active; fork an earlier turn or stop the run first.",
            }
        },
    )
    if job["state"] not in TERMINAL_STATES:
        raise not_settled
    forked = await job_store.fork_thread(source_job_id=job_id, user_id=user["user_id"])
    if forked is None:
        # The settled-check above raced a concurrent transition; same answer.
        raise not_settled
    logger.info(
        "agent_job_forked",
        extra={
            "event": "agent_job_forked",
            "job_id": forked["id"],
            "thread_id": forked.get("thread_id"),
            "source_job_id": job_id,
        },
    )
    return _job_response(forked)


@router.get("/jobs/{job_id}/thread", response_model=AgentThreadResponse)
async def get_agent_thread(
    job_id: str,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: AgentJobStore | None = Depends(get_agent_job_store),
) -> AgentThreadResponse:
    """Return the durable conversation containing one of the caller's jobs."""
    job_store = _require_store(store)
    await _owned_job(job_store, job_id, user)
    thread = await job_store.get_thread_for_job(job_id=job_id, user_id=user["user_id"])
    if thread is None:
        raise HTTPException(
            status_code=404,
            detail={"error": {"type": "not_found", "message": f"No thread for {job_id}"}},
        )
    return AgentThreadResponse(
        thread_id=thread["id"],
        repo=thread["repo"],
        title=thread["title"],
        messages=[
            AgentThreadMessageResponse(
                id=message["id"],
                role=message["role"],
                content=message["content"],
                job_id=message["job_id"],
                created_at=_iso(message["created_at"]),
            )
            for message in thread["messages"]
        ],
        jobs=[_job_response(job) for job in thread["jobs"]],
        created_at=_iso(thread["created_at"]),
        updated_at=_iso(thread["updated_at"]),
    )


@router.get("/jobs/{job_id}/events", response_model=AgentJobEventsResponse)
async def list_agent_job_events(
    job_id: str,
    after: int = Query(0, ge=0, description="Return events with a global id greater than this."),
    limit: int = Query(_EVENT_PAGE_SIZE, ge=1, le=1000),
    user: dict[str, Any] = Depends(require_agent_owner),
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
    user: dict[str, Any] = Depends(require_agent_owner),
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


def _workspace_id(job: dict[str, Any]) -> str:
    """Return the durable directory identity for this job's worktree."""
    return str(job["id"])


def _workspace_broker_http_error(exc: WorkspaceBrokerError) -> HTTPException:
    """Map private broker failures onto bounded owner-facing errors."""
    status = exc.status_code if exc.status_code in {400, 404, 408, 409, 413, 429} else 503
    return HTTPException(
        status_code=status,
        detail={"error": {"type": "workspace_unavailable", "message": exc.message}},
    )


def _terminal_broker_http_error(exc: WorkspaceBrokerError) -> HTTPException:
    """Preserve terminal errors while presenting a missing worktree as a conflict."""
    if exc.status_code == 404 and exc.message == "workspace is not materialized":
        return HTTPException(
            status_code=409,
            detail={
                "error": {
                    "type": "workspace_unavailable",
                    "message": "This older job has no live terminal workspace.",
                }
            },
        )
    return _workspace_broker_http_error(exc)


async def _require_workspace_entitlement(
    *,
    job: dict[str, Any],
    user: dict[str, Any],
    store: Any,
    app_credentials: Any | None,
) -> None:
    """Recheck that the owner may still access the workspace repository."""
    try:
        await require_entitled_repo(
            job["repo"],
            user["user_id"],
            store=store,
            app_credentials=app_credentials,
        )
    except RepoNotAllowed as exc:
        raise HTTPException(
            status_code=403,
            detail={"error": {"type": "repo_not_allowed", "message": str(exc)}},
        ) from exc


async def _terminal_owner_workspace(
    *,
    job_id: str,
    user: dict[str, Any],
    store: AgentJobStore | None,
    app_credentials: Any | None,
    require_ready: bool = False,
    recheck_entitlement: bool = True,
) -> tuple[dict[str, Any], Any]:
    """Authorize one terminal operation and return its private broker.

    Creating or attaching to a terminal rechecks repository entitlement. Once
    attached, the opaque terminal id acts as a short-lived capability scoped
    to the already-authenticated job owner. Input and resize must not enumerate
    GitHub installations for every keystroke; they still recheck ownership and
    the broker-side workspace/session binding. Delete skips entitlement as well
    so a revoked owner can always clean up a live process.
    """
    job_store = _require_store(store)
    job = await _owned_job(job_store, job_id, user)
    if require_ready and job["state"] not in {*TERMINAL_STATES, PUBLISHING}:
        eligible = job["state"] == RUNNING and job["current_attempt_id"] is not None
        ready = eligible and await job_store.terminal_workspace_ready(
            attempt_id=job["current_attempt_id"]
        )
        if not ready:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": {
                        "type": "workspace_not_ready",
                        "message": "The terminal workspace is still being prepared.",
                    }
                },
            )
    if recheck_entitlement:
        await _require_workspace_entitlement(
            job=job,
            user=user,
            store=job_store,
            app_credentials=app_credentials,
        )
    broker = workspace_broker_from_env()
    if broker is None:
        raise HTTPException(
            status_code=409,
            detail={
                "error": {
                    "type": "workspace_unavailable",
                    "message": "This deployment has no live workspace broker.",
                }
            },
        )
    if job.get("terminal_resume_pending"):
        if not await resume_settled_terminal(_workspace_id(job)):
            raise HTTPException(
                status_code=503,
                detail={
                    "error": {
                        "type": "workspace_unavailable",
                        "message": "The settled terminal workspace is still resuming.",
                    }
                },
            )
        await job_store.mark_terminal_resume_complete(job_id=job_id)
        job["terminal_resume_pending"] = False
    return job, broker


@router.get("/jobs/{job_id}/files", response_model=AgentWorkspaceResponse)
async def get_agent_job_workspace_file(
    job_id: str,
    path: str = Query("", max_length=4096),
    user: dict[str, Any] = Depends(require_agent_owner),
    store: AgentJobStore | None = Depends(get_agent_job_store),
    app_credentials: Any | None = Depends(get_agent_app_credentials),
) -> AgentWorkspaceResponse:
    """Browse a live job worktree or an older job's archived snapshot.

    Live reads go through the private workspace broker after owner and current
    repository entitlement checks. For jobs created before durable worktrees,
    GitHub is read lazily at the pinned SHA and merged with the runner's bounded
    changed-file snapshot.
    """
    job_store = _require_store(store)
    job = await _owned_job(job_store, job_id, user)
    try:
        safe_path = normalize_workspace_path(path)
    except WorkspacePathError as exc:
        raise HTTPException(
            status_code=400,
            detail={"error": {"type": "invalid_path", "message": str(exc)}},
        ) from exc

    await _require_workspace_entitlement(
        job=job, user=user, store=job_store, app_credentials=app_credentials
    )

    # New self-hosted runners retain one real worktree per job. Prefer it
    # before consulting GitHub/snapshots; only a pre-broker job falls through
    # to the archived view.
    broker = workspace_broker_from_env()
    if broker is not None:
        try:
            return AgentWorkspaceResponse(**(await broker.files(_workspace_id(job), safe_path)))
        except WorkspaceBrokerError as exc:
            if exc.status_code != 404 or exc.message != "workspace is not materialized":
                raise _workspace_broker_http_error(exc) from exc

    base_sha = job.get("base_sha")
    if not base_sha:
        raise HTTPException(
            status_code=409,
            detail={
                "error": {
                    "type": "workspace_unavailable",
                    "message": "This job does not yet have a pinned base commit.",
                }
            },
        )
    if app_credentials is None:
        raise HTTPException(
            status_code=503,
            detail={
                "error": {
                    "type": "workspace_unavailable",
                    "message": "Repository browsing requires the GitHub App.",
                }
            },
        )

    snapshot_artifact = await job_store.get_artifact(job_id=job_id, kind="workspace_snapshot")
    try:
        overlay = parse_workspace_snapshot(
            snapshot_artifact["content"] if snapshot_artifact else None
        )
    except WorkspaceSnapshotError as exc:
        logger.warning(
            "agent_workspace_snapshot_invalid",
            extra={"event": "agent_workspace_snapshot_invalid", "job_id": job_id},
        )
        raise HTTPException(
            status_code=503,
            detail={
                "error": {
                    "type": "workspace_unavailable",
                    "message": "The changed-files snapshot could not be read.",
                }
            },
        ) from exc

    changed_file = snapshot_file_response(overlay, safe_path) if safe_path else None
    replaced_file_with_directory = bool(
        changed_file is not None
        and changed_file.get("status") == "deleted"
        and overlay_has_directory(overlay, safe_path)
    )
    if changed_file is not None and not replaced_file_with_directory:
        return AgentWorkspaceResponse(**changed_file)

    if replaced_file_with_directory:
        baseline: dict[str, Any] | list[dict[str, Any]] = []
    else:
        try:
            baseline = await app_credentials.repository_contents(
                job["repo"], path=safe_path, ref=base_sha
            )
        except GitHubAppError as exc:
            if exc.status == 404 and overlay_has_directory(overlay, safe_path):
                baseline = []
            elif exc.status == 404:
                raise HTTPException(
                    status_code=404,
                    detail={
                        "error": {
                            "type": "not_found",
                            "message": f"No such workspace path: {safe_path}",
                        }
                    },
                ) from exc
            else:
                raise HTTPException(
                    status_code=502,
                    detail={
                        "error": {
                            "type": "repository_unavailable",
                            "message": "The pinned repository contents are temporarily unavailable.",
                        }
                    },
                ) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail={
                    "error": {
                        "type": "repository_unavailable",
                        "message": "The pinned repository contents are temporarily unavailable.",
                    }
                },
            ) from exc

    if isinstance(baseline, list):
        return AgentWorkspaceResponse(
            path=safe_path,
            kind="directory",
            entries=merge_directory_entries(path=safe_path, baseline=baseline, overlay=overlay),
            writable=False,
            source="snapshot",
        )
    try:
        return AgentWorkspaceResponse(**github_file_response(safe_path, baseline))
    except WorkspaceSnapshotError as exc:
        raise HTTPException(
            status_code=502,
            detail={
                "error": {
                    "type": "repository_unavailable",
                    "message": "GitHub returned malformed file content.",
                }
            },
        ) from exc


@router.put("/jobs/{job_id}/files", response_model=AgentWorkspaceResponse)
async def write_agent_job_workspace_file(
    job_id: str,
    body: AgentWorkspaceWriteRequest,
    path: str = Query(..., min_length=1, max_length=4096),
    user: dict[str, Any] = Depends(require_agent_owner),
    store: AgentJobStore | None = Depends(get_agent_job_store),
    app_credentials: Any | None = Depends(get_agent_app_credentials),
) -> AgentWorkspaceResponse:
    """Save one text file directly into the job's live worktree."""
    job_store = _require_store(store)
    job = await _owned_job(job_store, job_id, user)
    try:
        safe_path = normalize_workspace_path(path)
    except WorkspacePathError as exc:
        raise HTTPException(
            status_code=400,
            detail={"error": {"type": "invalid_path", "message": str(exc)}},
        ) from exc
    if not safe_path:
        raise HTTPException(status_code=400, detail="A file path is required.")
    await _require_workspace_entitlement(
        job=job, user=user, store=job_store, app_credentials=app_credentials
    )
    broker = workspace_broker_from_env()
    if broker is None:
        raise HTTPException(
            status_code=409,
            detail={
                "error": {
                    "type": "workspace_unavailable",
                    "message": "This deployment has no live workspace broker.",
                }
            },
        )
    try:
        return AgentWorkspaceResponse(
            **(await broker.write_file(_workspace_id(job), safe_path, body.content))
        )
    except WorkspaceBrokerError as exc:
        if exc.status_code == 404 and exc.message == "workspace is not materialized":
            raise HTTPException(
                status_code=409,
                detail={
                    "error": {
                        "type": "workspace_unavailable",
                        "message": "This older job has only an archived workspace snapshot.",
                    }
                },
            ) from exc
        raise _workspace_broker_http_error(exc) from exc


@router.post("/jobs/{job_id}/terminal", response_model=AgentTerminalResponse)
async def run_agent_job_terminal_command(
    job_id: str,
    body: AgentTerminalRequest,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: AgentJobStore | None = Depends(get_agent_job_store),
    app_credentials: Any | None = Depends(get_agent_app_credentials),
) -> AgentTerminalResponse:
    """Run an owner-entered command in a disposable workspace sandbox."""
    job_store = _require_store(store)
    job = await _owned_job(job_store, job_id, user)
    await _require_workspace_entitlement(
        job=job, user=user, store=job_store, app_credentials=app_credentials
    )
    broker = workspace_broker_from_env()
    if broker is None:
        raise HTTPException(status_code=409, detail="This deployment has no live workspace broker.")
    try:
        result = await broker.terminal(
            _workspace_id(job),
            command=body.command,
            cwd=body.cwd,
            timeout_seconds=body.timeout_seconds,
        )
        return AgentTerminalResponse(**result)
    except WorkspaceBrokerError as exc:
        if exc.status_code == 404 and exc.message == "workspace is not materialized":
            raise HTTPException(
                status_code=409,
                detail="This older job has no live terminal workspace.",
            ) from exc
        raise _workspace_broker_http_error(exc) from exc


@router.post(
    "/jobs/{job_id}/terminals",
    response_model=AgentTerminalSessionResponse,
)
async def create_agent_job_terminal(
    job_id: str,
    body: AgentTerminalSessionCreateRequest,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: AgentJobStore | None = Depends(get_agent_job_store),
    app_credentials: Any | None = Depends(get_agent_app_credentials),
) -> AgentTerminalSessionResponse:
    """Open an interactive PTY in the agent's workspace."""
    job, broker = await _terminal_owner_workspace(
        job_id=job_id,
        user=user,
        store=store,
        app_credentials=app_credentials,
        require_ready=True,
    )
    try:
        return AgentTerminalSessionResponse(
            **(
                await broker.create_terminal(
                    _workspace_id(job),
                    rows=body.rows,
                    cols=body.cols,
                )
            )
        )
    except WorkspaceBrokerError as exc:
        raise _terminal_broker_http_error(exc) from exc


@router.get(
    "/jobs/{job_id}/terminals",
    response_model=AgentTerminalSessionListResponse,
)
async def list_agent_job_terminals(
    job_id: str,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: AgentJobStore | None = Depends(get_agent_job_store),
    app_credentials: Any | None = Depends(get_agent_app_credentials),
) -> AgentTerminalSessionListResponse:
    """List interactive terminals, including exited sessions kept for replay."""
    job, broker = await _terminal_owner_workspace(
        job_id=job_id,
        user=user,
        store=store,
        app_credentials=app_credentials,
        require_ready=True,
    )
    try:
        return AgentTerminalSessionListResponse(**(await broker.list_terminals(_workspace_id(job))))
    except WorkspaceBrokerError as exc:
        raise _terminal_broker_http_error(exc) from exc


@router.get("/jobs/{job_id}/terminals/{terminal_id}/stream")
async def stream_agent_job_terminal(
    job_id: str,
    request: Request,
    terminal_id: str = Path(..., pattern=_TERMINAL_ID_PATTERN),
    after: int = Query(0, ge=0, le=2**63 - 1),
    user: dict[str, Any] = Depends(require_agent_owner),
    store: AgentJobStore | None = Depends(get_agent_job_store),
    app_credentials: Any | None = Depends(get_agent_app_credentials),
) -> StreamingResponse:
    """Relay a resumable private PTY stream without exposing its broker token."""
    job, broker = await _terminal_owner_workspace(
        job_id=job_id,
        user=user,
        store=store,
        app_credentials=app_credentials,
    )
    try:
        upstream = await broker.stream_terminal(_workspace_id(job), terminal_id, after=after)
    except WorkspaceBrokerError as exc:
        raise _terminal_broker_http_error(exc) from exc

    async def relay():
        try:
            async for chunk in upstream:
                if await request.is_disconnected():
                    break
                yield chunk
        finally:
            await upstream.aclose()

    return StreamingResponse(
        relay(),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )


@router.post(
    "/jobs/{job_id}/terminals/{terminal_id}/input",
    response_model=AgentTerminalSessionResponse,
)
async def write_agent_job_terminal_input(
    job_id: str,
    body: AgentTerminalSessionInputRequest,
    terminal_id: str = Path(..., pattern=_TERMINAL_ID_PATTERN),
    user: dict[str, Any] = Depends(require_agent_owner),
    store: AgentJobStore | None = Depends(get_agent_job_store),
    app_credentials: Any | None = Depends(get_agent_app_credentials),
) -> AgentTerminalSessionResponse:
    """Write bounded base64 input to an active interactive terminal."""
    job, broker = await _terminal_owner_workspace(
        job_id=job_id,
        user=user,
        store=store,
        app_credentials=app_credentials,
        require_ready=True,
        recheck_entitlement=False,
    )
    try:
        return AgentTerminalSessionResponse(
            **(await broker.terminal_input(_workspace_id(job), terminal_id, data=body.data))
        )
    except WorkspaceBrokerError as exc:
        raise _terminal_broker_http_error(exc) from exc


@router.post(
    "/jobs/{job_id}/terminals/{terminal_id}/resize",
    response_model=AgentTerminalSessionResponse,
)
async def resize_agent_job_terminal(
    job_id: str,
    body: AgentTerminalSessionResizeRequest,
    terminal_id: str = Path(..., pattern=_TERMINAL_ID_PATTERN),
    user: dict[str, Any] = Depends(require_agent_owner),
    store: AgentJobStore | None = Depends(get_agent_job_store),
    app_credentials: Any | None = Depends(get_agent_app_credentials),
) -> AgentTerminalSessionResponse:
    """Resize an active interactive terminal."""
    job, broker = await _terminal_owner_workspace(
        job_id=job_id,
        user=user,
        store=store,
        app_credentials=app_credentials,
        recheck_entitlement=False,
    )
    try:
        return AgentTerminalSessionResponse(
            **(
                await broker.resize_terminal(
                    _workspace_id(job), terminal_id, rows=body.rows, cols=body.cols
                )
            )
        )
    except WorkspaceBrokerError as exc:
        raise _terminal_broker_http_error(exc) from exc


@router.delete(
    "/jobs/{job_id}/terminals/{terminal_id}",
    response_model=AgentTerminalSessionResponse,
)
async def delete_agent_job_terminal(
    job_id: str,
    terminal_id: str = Path(..., pattern=_TERMINAL_ID_PATTERN),
    user: dict[str, Any] = Depends(require_agent_owner),
    store: AgentJobStore | None = Depends(get_agent_job_store),
    app_credentials: Any | None = Depends(get_agent_app_credentials),
) -> AgentTerminalSessionResponse:
    """Idempotently kill an interactive terminal, even while a new job runs."""
    job, broker = await _terminal_owner_workspace(
        job_id=job_id,
        user=user,
        store=store,
        app_credentials=app_credentials,
        recheck_entitlement=False,
    )
    try:
        return AgentTerminalSessionResponse(
            **(await broker.delete_terminal(_workspace_id(job), terminal_id))
        )
    except WorkspaceBrokerError as exc:
        raise _terminal_broker_http_error(exc) from exc


@router.get("/jobs/{job_id}/git", response_model=AgentGitWorkspaceResponse)
async def get_agent_job_git_workspace(
    job_id: str,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: AgentJobStore | None = Depends(get_agent_job_store),
    app_credentials: Any | None = Depends(get_agent_app_credentials),
) -> AgentGitWorkspaceResponse:
    """Read live worktree status, diff and recent commits."""
    job_store = _require_store(store)
    job = await _owned_job(job_store, job_id, user)
    await _require_workspace_entitlement(
        job=job, user=user, store=job_store, app_credentials=app_credentials
    )
    broker = workspace_broker_from_env()
    if broker is None:
        return AgentGitWorkspaceResponse(available=False)
    try:
        return AgentGitWorkspaceResponse(
            **(await broker.git(_workspace_id(job), base_sha=job.get("base_sha")))
        )
    except WorkspaceBrokerError as exc:
        if exc.status_code == 404 and exc.message == "workspace is not materialized":
            return AgentGitWorkspaceResponse(available=False)
        raise _workspace_broker_http_error(exc) from exc


@router.get("/jobs/{job_id}/stream")
async def stream_agent_job_events(
    job_id: str,
    request: Request,
    last_event_id: str | None = Header(None, alias="Last-Event-ID"),
    after: int = Query(0, ge=0),
    user: dict[str, Any] = Depends(require_agent_owner),
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


async def verify_dispatcher_access(
    request: Request,
    authorization: str | None = Header(None),
    op_store=Depends(get_operational_store),
) -> str:
    """Gate for the machine-to-machine worker claim.

    Prefers a dedicated ``AGENT_DISPATCHER_TOKEN``: the runner host executes
    untrusted repository code next door, and the credential it holds should
    open exactly one door — claiming work — not the whole admin surface, which
    is what handing it ``ADMIN_TOKEN`` did (the design's own blast-radius rule
    applied to our side of the fence). ``ADMIN_TOKEN`` (via
    ``verify_admin_access``) still works, both for migration and because admin
    legitimately outranks dispatcher; the point is the runner no longer *needs*
    it.

    The dedicated token opens nothing else: no other route reads it, and to
    every other authenticator it is just an invalid credential.
    """
    configured = (os.environ.get("AGENT_DISPATCHER_TOKEN") or "").strip()
    if configured and authorization and authorization.startswith("Bearer "):
        presented = authorization[7:].strip()
        if presented and secrets.compare_digest(presented, configured):
            return "dispatcher"
    return await verify_admin_access(
        request=request, authorization=authorization, op_store=op_store
    )


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


async def _restore_terminal_suspension(
    broker: WorkspaceBrokerClient | None,
    *,
    job_id: str,
    lease_generation: int,
) -> None:
    """Best-effort compensation after a worker fails to finalize a resume."""
    if broker is None:
        return
    try:
        await broker.suspend_terminals(
            job_id,
            lease_generation=lease_generation,
        )
    except WorkspaceBrokerError:
        logger.warning(
            "agent_terminal_resume_compensation_failed",
            exc_info=True,
            extra={
                "event": "agent_terminal_resume_compensation_failed",
                "job_id": job_id,
                "lease_generation": lease_generation,
            },
        )


@router.post("/worker/claim", response_model=WorkerClaimResponse | None)
async def worker_claim(
    body: WorkerClaimRequest,
    _dispatcher: str = Depends(verify_dispatcher_access),
    store: AgentJobStore | None = Depends(get_agent_job_store),
    app_credentials: Any | None = Depends(get_agent_app_credentials),
) -> WorkerClaimResponse | None:
    """Claim the next queued job and mint this attempt's capability token.

    Returns ``null`` (HTTP 200) when the queue is empty, and equally when an
    operator has pinned agent jobs to a different host than the one this runner
    reports. The two are deliberately the same answer on the wire: a runner
    that has been switched away from should idle exactly as it does when there
    is no work, not treat it as an error worth retrying differently. Which host
    is active is an admin question, answered on the admin surface.

    **Dispatcher-only.** ``claim_job`` takes the oldest queued job across all
    tenants, and the response carries that job's repo, prompt, and metadata
    plus a working capability token for it — so ordinary API-key auth here
    would let any customer dequeue and read another customer's job, and drain
    the queue besides.

    ``verify_dispatcher_access`` prefers the dedicated
    ``AGENT_DISPATCHER_TOKEN`` (so the runner host holds a credential that
    opens only this door) and falls back to ``verify_admin_access`` — a
    machine-to-machine gate with no "auth disabled" bypass, so the endpoint
    does not fall open in a deployment running with user auth off. The
    dispatcher credential stays outside the sandbox; only the returned
    per-attempt token goes in.
    """
    job_store = _require_store(store)
    claim = await job_store.claim_job(
        worker_id=body.worker_id,
        lease_ttl_seconds=body.lease_ttl_seconds,
        host=body.host,
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
    context = await job_store.follow_up_context(job_id=claim["id"])

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
        # The same check create used. The allowlist-only variant here meant a
        # job entitled by its owner's own GitHub connection passed creation and
        # was then refused at claim — the two gates disagreeing about what the
        # word entitled means.
        await require_entitled_repo(
            claim["repo"],
            claim["user_id"],
            store=job_store,
            app_credentials=app_credentials,
        )
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
        thread_id=claim.get("thread_id"),
        parent_job_id=claim.get("parent_job_id"),
        turn_no=claim.get("turn_no") or 1,
        attempt_id=claim["attempt_id"],
        attempt_no=claim["attempt_no"],
        repo=claim["repo"],
        base_sha=claim["base_sha"],
        task_prompt=claim["task_prompt"],
        setup_script=claim.get("setup_script"),
        runtime=claim["runtime"],
        model=claim["model"],
        worker_token=token,
        sandbox_token=sandbox_token,
        clone_token=clone_token,
        context_messages=context["messages"],
        context_patch=context["patch"],
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


@router.post("/worker/jobs/{job_id}/terminals/suspend", response_model=WorkerAckResponse)
async def worker_suspend_terminals(
    job_id: str,
    body: WorkerTerminalSuspendRequest,
    authorization: str | None = Header(None),
    store: AgentJobStore | None = Depends(get_agent_job_store),
) -> WorkerAckResponse:
    """Fence terminal writes, then freeze retained PTYs for a protected phase."""
    job_store = _require_store(store)
    claims = _worker_claims(authorization)
    _match_job(claims, job_id)
    event_id = await job_store.append_event(
        attempt_id=claims["attempt_id"],
        lease_generation=claims["lease_generation"],
        event_type="lifecycle",
        payload={"phase": body.phase},
    )
    if event_id is None:
        raise _lease_lost()
    broker = workspace_broker_from_env()
    if broker is not None:
        try:
            await broker.suspend_terminals(
                job_id,
                lease_generation=claims["lease_generation"],
            )
        except WorkspaceBrokerError as exc:
            raise HTTPException(
                status_code=503,
                detail={
                    "error": {
                        "type": "workspace_unavailable",
                        "message": exc.message,
                    }
                },
            ) from exc
    return WorkerAckResponse(ok=True)


@router.post("/worker/jobs/{job_id}/terminals/resume", response_model=WorkerAckResponse)
async def worker_resume_terminals(
    job_id: str,
    authorization: str | None = Header(None),
    store: AgentJobStore | None = Depends(get_agent_job_store),
) -> WorkerAckResponse:
    """Resume retained PTYs, then atomically publish workspace readiness."""
    job_store = _require_store(store)
    claims = _worker_claims(authorization)
    _match_job(claims, job_id)
    fenced = await job_store.fence_terminal_workspace(
        attempt_id=claims["attempt_id"],
        lease_generation=claims["lease_generation"],
    )
    if not fenced:
        raise _lease_lost()
    broker = workspace_broker_from_env()
    if broker is not None:
        try:
            await broker.resume_terminals(
                job_id,
                lease_generation=claims["lease_generation"],
            )
        except WorkspaceBrokerError as exc:
            await _restore_terminal_suspension(
                broker,
                job_id=job_id,
                lease_generation=claims["lease_generation"],
            )
            raise HTTPException(
                status_code=503,
                detail={
                    "error": {
                        "type": "workspace_unavailable",
                        "message": exc.message,
                    }
                },
            ) from exc
    try:
        event_id = await job_store.append_event(
            attempt_id=claims["attempt_id"],
            lease_generation=claims["lease_generation"],
            event_type="lifecycle",
            payload={"phase": "workspace_ready"},
        )
    except Exception:
        await _restore_terminal_suspension(
            broker,
            job_id=job_id,
            lease_generation=claims["lease_generation"],
        )
        raise
    if event_id is None:
        await _restore_terminal_suspension(
            broker,
            job_id=job_id,
            lease_generation=claims["lease_generation"],
        )
        raise _lease_lost()
    return WorkerAckResponse(ok=True)


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
    fenced = await job_store.fence_terminal_workspace(
        attempt_id=claims["attempt_id"],
        lease_generation=claims["lease_generation"],
    )
    if not fenced:
        raise _lease_lost()
    broker = workspace_broker_from_env()
    if broker is not None:
        try:
            await broker.resume_terminals(
                job_id,
                lease_generation=claims["lease_generation"],
            )
        except WorkspaceBrokerError as exc:
            await _restore_terminal_suspension(
                broker,
                job_id=job_id,
                lease_generation=claims["lease_generation"],
            )
            raise HTTPException(
                status_code=503,
                detail={
                    "error": {
                        "type": "workspace_unavailable",
                        "message": exc.message,
                    }
                },
            ) from exc
    try:
        ok = await job_store.transition(
            job_id=job_id,
            attempt_id=claims["attempt_id"],
            lease_generation=claims["lease_generation"],
            from_states=(RUNNING,),
            to_state=body.state,
            detail=body.detail,
            base_sha=body.base_sha,
        )
    except Exception:
        await _restore_terminal_suspension(
            broker,
            job_id=job_id,
            lease_generation=claims["lease_generation"],
        )
        raise
    if not ok:
        await _restore_terminal_suspension(
            broker,
            job_id=job_id,
            lease_generation=claims["lease_generation"],
        )
        raise _lease_lost()
    return WorkerAckResponse(ok=True, state=body.state)
