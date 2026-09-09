"""Internal endpoints the cloud agent's control plane calls to get capability.

**Model inference only.** A grant carried an MCP scope until the ownership
amendment moved the MCP registry, its credentials and its proxy to the cloud
agent — which is where the job, its requested servers and the attempt fence
already live, so nothing here had the state to decide MCP access with. A
sandbox now reaches tools with a separate credential this gateway neither mints
nor accepts.

Not public API. These sit behind a shared dispatch token and exist because the
control plane, after the split, cannot answer "may this user call this model"
on its own — it has no user table and no model registry. It asks; this gateway
decides. The reasoning behind each clamp lives in :mod:`serving.grants`.

Everything here is scope, never spend: a grant says *what* an attempt may call,
and the gateway's existing per-user quota continues to govern *how much*.
"""

from __future__ import annotations

import base64
import binascii
import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, Field

from serving import grants
from serving.model_access import get_disabled_models_from_preferences
from serving.model_catalog import agent_visible_models
from serving.servers.deps import (
    get_log_store,
    get_model_visibility_resolver,
    get_operational_store,
    get_router,
)
from serving.servers.routers.internal_auth import (
    error as _error,
    require_dispatch_token,
    require_store as _require_store,
)
from serving.utils.logging import get_logger

logger = get_logger(__name__)

#: The dispatch token guards the router, not each route in turn.
#:
#: Every route here needs it, and a per-route dependency is a thing to forget:
#: the one that forgets is unguarded, and nothing about the file looks wrong.
#: Declared once, a new route is guarded by existing rather than by being
#: remembered — which is what lets the reverse proxy forward this whole prefix
#: instead of enumerating paths and silently 404ing the next one added.
router = APIRouter(
    prefix="/internal/agent-grants",
    tags=["internal"],
    dependencies=[Depends(require_dispatch_token)],
)

#: A status outside "active" is refused rather than defaulted.
_ACTIVE_STATUS = "active"

#: A renewal buys the same bounded step a mint does.
MAX_RENEW_TTL_S = grants.DEFAULT_GRANT_TTL_S


class MintGrantRequest(BaseModel):
    """One attempt's requested capability."""

    user_id: str = Field(max_length=128)
    external_job_id: str = Field(max_length=128)
    external_attempt_id: str = Field(max_length=128)
    #: ``None`` means "everything this user's role can reach".
    allowed_models: list[str] | None = None
    ttl_seconds: int | None = None


def _grant_response(row: dict[str, Any], *, token: str | None = None) -> dict[str, Any]:
    """Shape a grant row for the control plane.

    Returns the **effective** scope and lifetime, not what was asked for: a
    caller that requested more must be able to see what it actually got, or it
    will build a job around capability it does not have.
    """
    body: dict[str, Any] = {
        "grant_id": row["grant_id"],
        "user_id": row["user_id"],
        "external_job_id": row["external_job_id"],
        "external_attempt_id": row["external_attempt_id"],
        "allowed_models": row["allowed_models"],
        "expires_at": row["expires_at"].isoformat(),
        "revoked_at": row["revoked_at"].isoformat() if row.get("revoked_at") else None,
    }
    if token is not None:
        body["token"] = token
    return body


async def _active_user(store: Any, user_id: str) -> dict[str, Any]:
    """Load the grant's subject, refusing anyone who may not use the platform.

    Raises:
        HTTPException: 403 for unknown or non-active accounts.
    """
    user = await store.get_user_by_id(user_id)
    if user is None or user.get("status") != _ACTIVE_STATUS:
        # One message for both cases: whether a given user id exists is not
        # something an internal caller needs, and saying so enumerates accounts.
        raise _error(
            status.HTTP_403_FORBIDDEN,
            "subject_unavailable",
            "That user cannot be granted capability.",
        )
    return user


@router.post("")
async def mint_grant(
    body: MintGrantRequest,
    store=Depends(get_operational_store),
    router_exec=Depends(get_router),
    visibility_resolver: Any = Depends(get_model_visibility_resolver),
) -> dict[str, Any]:
    """Mint (or return) the grant for one attempt.

    Args:
        body: The requested subject, scope and lifetime.
        store: Operational store.
        router_exec: Model registry, for the role clamp.
        visibility_resolver: Runtime visibility overrides, so the clamp agrees
            with what the inference path would allow.

    Returns:
        The effective grant, with its bearer token.

    Raises:
        HTTPException: 400 for an unknown MCP server, 403 for an unusable
            subject, 404 when internal endpoints are not configured.
    """
    store = _require_store(store)
    user = await _active_user(store, body.user_id)

    # Models are clamped: a narrower list still runs. The user context mirrors
    # what the inference path builds, so the answer here and there agree.
    visible = await agent_visible_models(
        router_exec,
        visibility_resolver=visibility_resolver,
        user_ctx={
            "role": user.get("role") or "free",
            "user_id": user["id"],
            # Without this the resolver's denylist check reads an absent
            # key and passes, so a model the owner disabled resolves here
            # as visible — and a grant gets minted for it.
            "disabled_models": get_disabled_models_from_preferences(user.get("preferences")),
        },
    )
    effective_models = grants.clamp_models(body.allowed_models, visible=visible)

    ttl = grants.clamp_ttl(body.ttl_seconds)
    row = await store.upsert_agent_grant(
        grant_id=grants.new_grant_id(),
        user_id=user["id"],
        external_job_id=body.external_job_id,
        external_attempt_id=body.external_attempt_id,
        allowed_models=effective_models,
        expires_at=grants.expiry_from(ttl),
    )

    # The row may be one that already existed: `(external_job_id,
    # external_attempt_id)` is unique, so a retried mint returns the first
    # grant rather than making a second. That is the behaviour we want, but it
    # is only *idempotent* when the request was the same request.
    #
    # Returning the old grant for a different subject or a different scope
    # would be a privilege decision made by whichever call happened to arrive
    # first — a caller asking for a narrower scope would silently be handed the
    # wider one, and a caller naming a different user would be handed a
    # capability belonging to someone else. The key is the control plane's own
    # id pair, so a collision here means its state and ours disagree; refusing
    # is the only answer that does not resolve that disagreement by guessing.
    if row["user_id"] != user["id"] or list(row["allowed_models"]) != list(effective_models):
        raise _error(
            status.HTTP_409_CONFLICT,
            "grant_conflict",
            "A grant already exists for this attempt with a different subject or scope.",
        )

    # The token is minted from what the store returned rather than from the id
    # generated above, which is not the id that won on a retry.
    return _grant_response(row, token=grants.mint_grant_token(row["grant_id"]))


@router.post("/{grant_id}/renew")
async def renew_grant(
    grant_id: str,
    store=Depends(get_operational_store),
) -> dict[str, Any]:
    """Extend a live grant by another bounded step.

    Deliberately consults no attempt or lease state: after the split this
    gateway holds none. The control plane stops renewing when its own store
    says the attempt was superseded, and the grant then dies of its TTL whether
    or not a revoke call ever succeeds.

    Args:
        grant_id: The grant to extend.
        store: Operational store.

    Returns:
        The renewed grant, without a token — the caller already has it.

    Raises:
        HTTPException: 403 if the subject is no longer active, 404 if the grant
            is unknown, revoked, or already expired.
    """
    store = _require_store(store)
    existing = await store.get_agent_grant(grant_id)
    if existing is None:
        raise _error(status.HTTP_404_NOT_FOUND, "unknown_grant", "No such grant.")

    # Re-checked on every renewal: a user suspended mid-job must stop being
    # able to spend, and renewal is the only recurring moment to notice.
    await _active_user(store, existing["user_id"])

    row = await store.renew_agent_grant(grant_id, expires_at=grants.expiry_from(MAX_RENEW_TTL_S))
    if row is None:
        raise _error(
            status.HTTP_404_NOT_FOUND,
            "grant_not_live",
            "That grant is revoked or expired and cannot be renewed.",
        )
    return _grant_response(row)


#: Shape version of the windowed usage response. Bumped for any change a
#: consumer could not detect from the fields themselves.
USAGE_SCHEMA_VERSION = 1
USAGE_DEFAULT_LIMIT = 100
USAGE_MAX_LIMIT = 500
_USAGE_DETAILS = ("totals", "requests")
_USAGE_METRICS = ("tokens_in", "tokens_out", "cache_read", "cache_write", "reasoning", "spent_usd")


def _invalid_query(message: str) -> Any:
    return _error(status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid_usage_query", message)


def _parse_instant(value: str, *, name: str) -> datetime:
    """Parse one window bound: ISO 8601 with an explicit offset, kept in UTC."""
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise _invalid_query(f"`{name}` must be an ISO 8601 timestamp.") from None
    if parsed.tzinfo is None:
        # A naive bound would be read in the database session's zone, which
        # is not a thing the caller chose.
        raise _invalid_query(f"`{name}` must carry a UTC offset.")
    return parsed.astimezone(UTC)


def _encode_cursor(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(cursor: str, *, expected: dict[str, Any]) -> tuple[datetime, str]:
    """Reject a cursor minted for another grant, window or ordering.

    The cursor is opaque to the caller but not to us: it carries the query it
    belongs to, so a page fetched with different parameters cannot silently
    continue from the wrong place.
    """
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode()))
        after_started_at, after_request_id = payload["a"]
        bound = {key: payload[key] for key in expected}
        started_at = datetime.fromisoformat(after_started_at)
    except (binascii.Error, ValueError, KeyError, TypeError):
        raise _error(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid_cursor", "That cursor is not valid."
        ) from None
    if bound != expected or started_at.tzinfo is None or not isinstance(after_request_id, str):
        raise _error(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "invalid_cursor",
            "That cursor belongs to a different query.",
        )
    return started_at.astimezone(UTC), after_request_id


def _money(value: Any) -> str | None:
    """Render a ledger amount as a fixed-point decimal string, never a float."""
    if value is None:
        return None
    return format(Decimal(str(value)), "f")


def _integer(value: Any) -> int | None:
    return None if value is None else int(value)


def _instant(value: datetime | None) -> str | None:
    return None if value is None else value.astimezone(UTC).isoformat()


def _request_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "request_id": row["request_id"],
        "request_started_at": _instant(row["request_started_at"]),
        "logged_at": _instant(row["logged_at"]),
        "model": row["model_id"],
        "served_model_id": row["served_model_id"],
        "tokens_in": _integer(row["prompt_tokens"]),
        "tokens_out": _integer(row["completion_tokens"]),
        "cache_read": _integer(row["cache_read_tokens"]),
        "cache_write": _integer(row["cache_write_tokens"]),
        "reasoning": _integer(row["reasoning_tokens"]),
        "spent_usd": _money(row["cost_usd"]),
        "ttft_ms": _integer(row["ttft_ms"]),
        "latency_ms": _integer(row["latency_ms"]),
        "status_code": _integer(row["status_code"]),
        "usage_estimated": bool(row["usage_estimated"]),
    }


@router.get("/{grant_id}/usage")
async def grant_usage(
    grant_id: str,
    since: str | None = Query(None),
    until: str | None = Query(None),
    detail: str | None = Query(None),
    limit: int | None = Query(None),
    cursor: str | None = Query(None),
    store=Depends(get_operational_store),
    log_store=Depends(get_log_store),
) -> dict[str, Any]:
    """Report what the grant's job has spent so far.

    **Informational attribution, not a limit.** Nothing consults this to decide
    whether a call may proceed — that is the account's daily quota, enforced in
    ``model_auth``. This exists so the control plane's UI can show an owner
    what a job cost, and reading it as a ceiling would reintroduce the per-job
    budget the design removed.

    Numbers come from the billing ledger, so they reflect what the gateway
    actually billed rather than anything an agent reports about itself.

    Two shapes share the route. Without query parameters it answers as it
    always has: the job's lifetime totals keyed by ``external_job_id``. With
    ``since`` and ``detail`` it answers for one grant over one window of
    request start times — what a retained thread needs to attribute a single
    turn, where one grant outlives many turns and one job id outlives many
    grants — with each metric summed over the rows that reported it and the
    rows that did not counted beside it (``unknown_rows``). ``finalized`` is
    always false: the ledger is written after the response completes with no
    settlement barrier, so a later read may see more rows.

    Args:
        grant_id: The grant whose spend to report.
        since: Inclusive lower bound (ISO 8601 with offset). Required with
            ``detail``.
        until: Exclusive upper bound; defaults to now and is echoed back.
        detail: ``totals`` or ``requests``; the latter adds a page of rows.
        limit: Page size for ``requests`` (1..500, default 100).
        cursor: Continuation from a previous ``requests`` page.
        store: Operational store.
        log_store: Billing ledger.

    Returns:
        Spend, call count and token totals for the grant's job, or the
        windowed report described above.

    Raises:
        HTTPException: 404 for an unknown grant, 422 for an unusable window,
            page size or cursor, 503 without a ledger.
    """
    store = _require_store(store)
    row = await store.get_agent_grant(grant_id)
    if row is None:
        raise _error(status.HTTP_404_NOT_FOUND, "unknown_grant", "No such grant.")
    if log_store is None:
        # An empty report would read as "this job spent nothing", which is a
        # different and much more reassuring claim than "we cannot tell".
        raise _error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "ledger_unavailable",
            "Usage cannot be reported right now.",
        )

    job_id = row["external_job_id"]
    windowed = any(value is not None for value in (since, until, detail, limit, cursor))
    if not windowed:
        usage = await log_store.get_agent_job_usage(job_id)
        return {
            "grant_id": grant_id,
            "external_job_id": job_id,
            "spent_usd": float(await log_store.get_agent_job_cost(job_id)),
            "request_count": int(usage.get("calls", 0)),
            "tokens_in": int(usage.get("tokens_in", 0)),
            "tokens_out": int(usage.get("tokens_out", 0)),
        }

    if detail not in _USAGE_DETAILS:
        raise _invalid_query("`detail` must be `totals` or `requests`.")
    if since is None:
        raise _invalid_query("`since` is required for a windowed report.")
    as_of = datetime.now(UTC)
    window_since = _parse_instant(since, name="since")
    window_until = _parse_instant(until, name="until") if until is not None else as_of
    if window_since >= window_until:
        raise _invalid_query("`since` must be earlier than `until`.")
    if limit is not None and detail != "requests":
        raise _invalid_query("`limit` applies to `detail=requests` only.")
    if cursor is not None and detail != "requests":
        raise _invalid_query("`cursor` applies to `detail=requests` only.")
    page_size = USAGE_DEFAULT_LIMIT if limit is None else limit
    if not 1 <= page_size <= USAGE_MAX_LIMIT:
        raise _invalid_query(f"`limit` must be between 1 and {USAGE_MAX_LIMIT}.")

    cursor_scope = {
        "v": USAGE_SCHEMA_VERSION,
        "g": grant_id,
        "s": window_since.isoformat(),
        "u": window_until.isoformat(),
    }
    after = _decode_cursor(cursor, expected=cursor_scope) if cursor is not None else None

    summary = await log_store.get_agent_grant_usage(
        agent_job_id=job_id, grant_id=grant_id, since=window_since, until=window_until
    )
    metrics = summary["metrics"]
    totals: dict[str, Any] = {"calls": int(summary["calls"])}
    unknown_rows: dict[str, int] = {}
    for name in _USAGE_METRICS:
        metric = metrics[name]
        value = metric["value"]
        totals[name] = _money(value) if name == "spent_usd" else _integer(value)
        unknown_rows[name] = int(metric["unknown_rows"])

    body: dict[str, Any] = {
        "schema_version": USAGE_SCHEMA_VERSION,
        "source": "gateway_api_logs",
        "grant_id": grant_id,
        "external_job_id": job_id,
        "attribution": "grant_time_window",
        "window": {
            "since": window_since.isoformat(),
            "until": window_until.isoformat(),
            "basis": "request_started_at",
        },
        "as_of": as_of.isoformat(),
        "finalized": False,
        "totals": totals,
        "unknown_rows": unknown_rows,
        "estimated_calls": int(summary["estimated_calls"]),
    }
    if detail == "requests":
        # One row past the page tells us whether a next page exists without a
        # second count query, and is not returned.
        rows = await log_store.list_agent_grant_requests(
            agent_job_id=job_id,
            grant_id=grant_id,
            since=window_since,
            until=window_until,
            limit=page_size + 1,
            after=after,
        )
        page = rows[:page_size]
        body["requests"] = [_request_row(r) for r in page]
        body["next_cursor"] = (
            _encode_cursor(
                {
                    **cursor_scope,
                    "a": [
                        page[-1]["request_started_at"].astimezone(UTC).isoformat(),
                        page[-1]["request_id"],
                    ],
                }
            )
            if len(rows) > page_size
            else None
        )
    return body


@router.post("/{grant_id}/revoke")
async def revoke_grant(
    grant_id: str,
    store=Depends(get_operational_store),
) -> dict[str, Any]:
    """Withdraw a grant now, rather than waiting for its TTL.

    Args:
        grant_id: The grant to revoke.
        store: Operational store.

    Returns:
        Whether this call performed the revocation.

    Raises:
        HTTPException: 404 if the grant is unknown.
    """
    store = _require_store(store)
    if await store.get_agent_grant(grant_id) is None:
        raise _error(status.HTTP_404_NOT_FOUND, "unknown_grant", "No such grant.")
    revoked = await store.revoke_agent_grant(grant_id)
    return {"grant_id": grant_id, "revoked": True, "already_revoked": not revoked}
