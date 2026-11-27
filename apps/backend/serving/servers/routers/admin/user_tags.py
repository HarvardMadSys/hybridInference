"""Admin endpoints for user tags: the tags, their limits and rules, and their members.

A tag groups accounts; its limits -- a daily and a monthly cost quota, a
model allowlist, a cap on concurrent requests -- are ceilings its members are
held to on top of their own (:mod:`serving.user_tags`). A tag with an
automatic rule is also given to every account that sent requests in at least
``auto_min_active_hours`` of the last ``auto_window_hours`` hours; the gateway
ships one, ``unattended``.

Amounts are in the ledger currency (``currency`` on every tag). A write that
sets one names the currency it was read in, and is refused (409) when the
ledger's has changed since, as every other money write is.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Any

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from serving.accounting import get_accounting_policy
from serving.audit import AuditTarget
from serving.model_access import route_canonical_model_id
from serving.schemas_admin import (
    AddUserTagMembersRequest,
    AddUserTagMembersResponse,
    CreateUserTagRequest,
    ListUserTagMembersResponse,
    ListUserTagsResponse,
    RemoveUserTagMemberResponse,
    UpdateUserTagRequest,
    UserTagDetectResponse,
    UserTagItem,
    UserTagMatch,
    UserTagMember,
    UserTagMemberSource,
    UserTagPreviewResponse,
    UserTagWriteFields,
)
from serving.servers.auth import log_admin_action
from serving.servers.deps import (
    get_admin_audit_actor,
    get_admin_user_id,
    get_embedding_adapters,
    get_operational_store,
    get_router,
    verify_admin_access,
)
from serving.storage.user_tags import TAG_WRITABLE_COLUMNS
from serving.user_tags import MAX_WINDOW_HOURS, combine_tag_limits, ledger_amount
from serving.utils.logging import get_logger

if TYPE_CHECKING:
    from serving.audit import AuditActor

logger = get_logger(__name__)
router = APIRouter(prefix="/admin")

# ── helpers ──────────────────────────────────────────────────────────────────


def _require_store(op_store: Any) -> Any:
    if not op_store:
        raise HTTPException(500, "Database not configured")
    return op_store


def _tag_item(row: dict[str, Any]) -> UserTagItem:
    currency = row.get("accounting_currency")
    return UserTagItem(
        name=row["name"],
        description=row.get("description") or "",
        system=bool(row.get("system")),
        quota_daily_cost=ledger_amount(row.get("quota_daily_cost"), currency),
        quota_monthly_cost=ledger_amount(row.get("quota_monthly_cost"), currency),
        currency=get_accounting_policy().currency,
        max_concurrent_requests=row.get("max_concurrent_requests"),
        allowed_models=(
            list(row["allowed_models"]) if row.get("allowed_models") is not None else None
        ),
        auto_enabled=bool(row.get("auto_enabled")),
        auto_window_hours=int(row["auto_window_hours"]),
        auto_min_active_hours=int(row["auto_min_active_hours"]),
        manual_members=int(row.get("manual_members") or 0),
        auto_members=int(row.get("auto_members") or 0),
        dismissed_members=int(row.get("dismissed_members") or 0),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


async def _require_tag(op_store: Any, name: str) -> dict[str, Any]:
    row = await op_store.get_user_tag(name)
    if row is None:
        raise HTTPException(404, f"No user tag named {name!r}")
    return row


def _canonical_models(
    models: list[str], router_exec: Any, embedding_adapters: dict[str, Any] | None
) -> list[str]:
    """An allowlist as canonical ids, sorted and unique; 422 naming any unknown model.

    An alias stands for its model: limits, like every other model rule, are
    keyed by the canonical id. A chat model is known by the router, an
    embedding model by the embedding adapters. Without a router (a deployment
    still starting) the ids are taken as given.
    """
    routes = getattr(router_exec, "routes", None)
    embeddings = embedding_adapters or {}
    canonical: set[str] = set()
    unknown: list[str] = []
    for raw in models:
        model = raw.strip()
        if not model:
            raise HTTPException(422, "A model id in allowed_models is empty")
        route = routes.get(model) if routes is not None else None
        if route is not None:
            canonical.add(route_canonical_model_id(route, model))
        elif model in embeddings:
            config = getattr(embeddings[model], "config", None)
            canonical.add(getattr(config, "id", None) or model)
        elif routes is None:
            canonical.add(model)
        else:
            unknown.append(model)
    if unknown:
        raise HTTPException(422, f"Unknown models in allowed_models: {', '.join(sorted(unknown))}")
    return sorted(canonical)


def _column_values(
    payload: UserTagWriteFields,
    *,
    current: dict[str, Any] | None,
    router_exec: Any,
    embedding_adapters: dict[str, Any] | None,
) -> dict[str, Any]:
    """The columns a write sets, validated against the tag as it would become."""
    # The name is a create's key, and the currency only qualifies the amounts.
    sent = payload.model_fields_set - {"currency", "name"}
    for field in ("description", "auto_enabled", "auto_window_hours", "auto_min_active_hours"):
        if field in sent and getattr(payload, field) is None:
            raise HTTPException(422, f"{field} cannot be null")
    values: dict[str, Any] = {field: getattr(payload, field) for field in sent}
    if values.get("description") is not None:
        values["description"] = values["description"].strip()
    if values.get("allowed_models") is not None:
        values["allowed_models"] = _canonical_models(
            values["allowed_models"], router_exec, embedding_adapters
        )
    if {"quota_daily_cost", "quota_monthly_cost"} & sent:
        ledger = get_accounting_policy().currency
        if payload.currency is None:
            raise HTTPException(422, "Name the currency the amounts were read in")
        if payload.currency != ledger:
            raise HTTPException(409, "Accounting currency changed; reload before saving")
        values["accounting_currency"] = ledger
        # The other amount, stored in an earlier currency, is re-written in this one.
        for field in ("quota_daily_cost", "quota_monthly_cost"):
            if field not in values and current is not None:
                values[field] = ledger_amount(
                    current.get(field), current.get("accounting_currency")
                )
    window = values.get("auto_window_hours", (current or {}).get("auto_window_hours", 12))
    minimum = values.get("auto_min_active_hours", (current or {}).get("auto_min_active_hours", 12))
    if minimum > window:
        raise HTTPException(422, "auto_min_active_hours cannot be more than auto_window_hours")
    unknown_columns = set(values) - set(TAG_WRITABLE_COLUMNS)
    if unknown_columns:  # pragma: no cover - the schema and the table agree
        raise HTTPException(500, f"Unwritable tag fields: {sorted(unknown_columns)}")
    return values


def _audit_details(values: dict[str, Any]) -> dict[str, Any]:
    return {
        key: (str(value) if isinstance(value, Decimal) else value) for key, value in values.items()
    }


async def _audit(
    op_store: Any, actor: AuditActor, action: str, target: AuditTarget, details: dict[str, Any]
) -> None:
    try:
        await log_admin_action(op_store, action=action, details=details, actor=actor, target=target)
    except Exception:
        logger.warning("audit log failed for %s %s", action, target.id, exc_info=True)


# ── tags ─────────────────────────────────────────────────────────────────────


@router.get("/user-tags", response_model=ListUserTagsResponse)
async def list_user_tags(
    _admin_id: str = Depends(verify_admin_access),
    op_store=Depends(get_operational_store),
) -> ListUserTagsResponse:
    """List every user tag with its limits, rule and member counts, built-in first.

    Requires: Admin authentication (JWT or ADMIN_TOKEN)
    """
    rows = await _require_store(op_store).list_user_tags()
    return ListUserTagsResponse(
        tags=[_tag_item(row) for row in rows], currency=get_accounting_policy().currency
    )


@router.post("/user-tags", response_model=UserTagItem, status_code=201)
async def create_user_tag(
    payload: CreateUserTagRequest,
    actor: AuditActor = Depends(get_admin_audit_actor),
    op_store=Depends(get_operational_store),
    router_exec=Depends(get_router),
    embedding_adapters: dict[str, Any] = Depends(get_embedding_adapters),
) -> UserTagItem:
    """Create a user tag. 409 when one of that name exists.

    Requires: Admin authentication (JWT or ADMIN_TOKEN)
    """
    op_store = _require_store(op_store)
    values = _column_values(
        payload, current=None, router_exec=router_exec, embedding_adapters=embedding_adapters
    )
    try:
        row = await op_store.create_user_tag(payload.name, values)
    except asyncpg.CheckViolationError as exc:
        raise HTTPException(422, "The tag's settings are out of range") from exc
    if row is None:
        raise HTTPException(409, f"A user tag named {payload.name!r} exists")
    await _audit(
        op_store,
        actor,
        "user_tag.create",
        AuditTarget("user_tag", payload.name),
        _audit_details(values),
    )
    return _tag_item(row)


@router.patch("/user-tags/{name}", response_model=UserTagItem)
async def update_user_tag(
    name: str,
    payload: UpdateUserTagRequest,
    actor: AuditActor = Depends(get_admin_audit_actor),
    op_store=Depends(get_operational_store),
    router_exec=Depends(get_router),
    embedding_adapters: dict[str, Any] = Depends(get_embedding_adapters),
) -> UserTagItem:
    """Change a tag's description, limits or rule; only what is sent changes.

    A change to the limits applies to every member on its next request (other
    gateway processes within half a minute).

    Requires: Admin authentication (JWT or ADMIN_TOKEN)
    """
    op_store = _require_store(op_store)
    current = await _require_tag(op_store, name)
    values = _column_values(
        payload, current=current, router_exec=router_exec, embedding_adapters=embedding_adapters
    )
    try:
        row = await op_store.update_user_tag(name, values)
    except asyncpg.CheckViolationError as exc:
        raise HTTPException(422, "The tag's settings are out of range") from exc
    if row is None:
        raise HTTPException(404, f"No user tag named {name!r}")
    if values:
        await _audit(
            op_store,
            actor,
            "user_tag.update",
            AuditTarget("user_tag", name),
            _audit_details(values),
        )
    return _tag_item(row)


@router.delete("/user-tags/{name}", status_code=204)
async def delete_user_tag(
    name: str,
    actor: AuditActor = Depends(get_admin_audit_actor),
    op_store=Depends(get_operational_store),
) -> Response:
    """Delete a tag and every membership of it. The built-in tags cannot be deleted (409).

    Requires: Admin authentication (JWT or ADMIN_TOKEN)
    """
    op_store = _require_store(op_store)
    current = await _require_tag(op_store, name)
    if current.get("system"):
        raise HTTPException(
            409, f"{name!r} is built in and cannot be deleted; turn its rule off instead"
        )
    if not await op_store.delete_user_tag(name):
        raise HTTPException(404, f"No user tag named {name!r}")
    await _audit(
        op_store,
        actor,
        "user_tag.delete",
        AuditTarget("user_tag", name),
        {
            "members": int(current.get("manual_members") or 0)
            + int(current.get("auto_members") or 0)
        },
    )
    return Response(status_code=204)


# ── members ──────────────────────────────────────────────────────────────────


@router.get("/user-tags/{name}/members", response_model=ListUserTagMembersResponse)
async def list_user_tag_members(
    name: str,
    source: list[UserTagMemberSource] | None = Query(None),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    _admin_id: str = Depends(verify_admin_access),
    op_store=Depends(get_operational_store),
) -> ListUserTagMembersResponse:
    """List a tag's memberships, applied ones first, most recently given first.

    ``source`` (repeatable) narrows them to ``manual``, ``auto`` or
    ``dismissed`` ones.

    Requires: Admin authentication (JWT or ADMIN_TOKEN)
    """
    op_store = _require_store(op_store)
    await _require_tag(op_store, name)
    total, rows = await op_store.list_user_tag_members(
        name, sources=source, limit=limit, offset=offset
    )
    return ListUserTagMembersResponse(total=total, members=[UserTagMember(**row) for row in rows])


@router.post("/user-tags/{name}/members", response_model=AddUserTagMembersResponse)
async def add_user_tag_members(
    name: str,
    payload: AddUserTagMembersRequest,
    request: Request,
    actor: AuditActor = Depends(get_admin_audit_actor),
    op_store=Depends(get_operational_store),
) -> AddUserTagMembersResponse:
    """Give a tag to accounts by hand.

    Each becomes a manual member, including one that carried the tag
    automatically or was dismissed from it; the tag's limits apply on its
    next request. Unknown ids are reported, not refused.

    Requires: Admin authentication (JWT or ADMIN_TOKEN)
    """
    op_store = _require_store(op_store)
    await _require_tag(op_store, name)
    wanted = [user_id.strip() for user_id in payload.user_ids if user_id.strip()]
    if not wanted:
        raise HTTPException(422, "Name at least one account")
    added, unknown = await op_store.add_user_tag_members(
        name, wanted, assigned_by=get_admin_user_id(request)
    )
    unchanged = [
        user_id
        for user_id in dict.fromkeys(wanted)
        if user_id not in added and user_id not in unknown
    ]
    if added:
        await _audit(
            op_store,
            actor,
            "user_tag.add_members",
            AuditTarget("user_tag", name),
            {"user_ids": added},
        )
    return AddUserTagMembersResponse(added=added, unchanged=unchanged, unknown=unknown)


@router.delete("/user-tags/{name}/members/{user_id}", response_model=RemoveUserTagMemberResponse)
async def remove_user_tag_member(
    name: str,
    user_id: str,
    request: Request,
    actor: AuditActor = Depends(get_admin_audit_actor),
    op_store=Depends(get_operational_store),
) -> RemoveUserTagMemberResponse:
    """Take a tag away from an account.

    Under a tag with an automatic rule the membership is kept as dismissed:
    the rule gives the tag back only if the account's requests from now on
    match it. Removing a dismissed membership forgets the dismissal. 404 when
    the account has no membership of the tag.

    Requires: Admin authentication (JWT or ADMIN_TOKEN)
    """
    op_store = _require_store(op_store)
    tag = await _require_tag(op_store, name)
    previous = await op_store.remove_user_tag_member(
        name, user_id, removed_by=get_admin_user_id(request)
    )
    if previous is None:
        raise HTTPException(404, f"That account has no membership of {name!r}")
    dismissed = previous != "dismissed" and bool(tag.get("auto_enabled"))
    await _audit(
        op_store,
        actor,
        "user_tag.remove_member",
        AuditTarget.user(user_id),
        {"tag": name, "previous_source": previous, "dismissed": dismissed},
    )
    return RemoveUserTagMemberResponse(
        user_id=user_id, previous_source=previous, dismissed=dismissed
    )


# ── the automatic rule ───────────────────────────────────────────────────────


@router.get("/user-tags/{name}/preview", response_model=UserTagPreviewResponse)
async def preview_user_tag_rule(
    name: str,
    window_hours: int | None = Query(None, ge=1, le=MAX_WINDOW_HOURS),
    min_active_hours: int | None = Query(None, ge=1, le=MAX_WINDOW_HOURS),
    limit: int = Query(200, ge=1, le=1000),
    _admin_id: str = Depends(verify_admin_access),
    op_store=Depends(get_operational_store),
) -> UserTagPreviewResponse:
    """The active accounts a rule matches now, most active first; changes nothing.

    The tag's own bounds unless ``window_hours`` / ``min_active_hours`` are
    given, so a change can be tried before it is saved. A dismissed member is
    matched on its requests since the dismissal.

    Requires: Admin authentication (JWT or ADMIN_TOKEN)
    """
    op_store = _require_store(op_store)
    tag = await _require_tag(op_store, name)
    window = window_hours if window_hours is not None else int(tag["auto_window_hours"])
    minimum = (
        min_active_hours if min_active_hours is not None else int(tag["auto_min_active_hours"])
    )
    if minimum > window:
        raise HTTPException(422, "min_active_hours cannot be more than window_hours")
    rows = await op_store.preview_user_tag_members(
        name, window_hours=window, min_active_hours=minimum, limit=limit + 1
    )
    return UserTagPreviewResponse(
        window_hours=window,
        min_active_hours=minimum,
        matched=[UserTagMatch(**row) for row in rows[:limit]],
        truncated=len(rows) > limit,
    )


@router.post("/user-tags/{name}/detect", response_model=UserTagDetectResponse)
async def run_user_tag_rule(
    name: str,
    actor: AuditActor = Depends(get_admin_audit_actor),
    op_store=Depends(get_operational_store),
) -> UserTagDetectResponse:
    """Run a tag's automatic rule now rather than at its next scheduled run.

    409 when the tag's rule is off.

    Requires: Admin authentication (JWT or ADMIN_TOKEN)
    """
    op_store = _require_store(op_store)
    tag = await _require_tag(op_store, name)
    if not tag.get("auto_enabled"):
        raise HTTPException(409, f"{name!r} has no automatic rule turned on")
    added = await op_store.detect_user_tag_members(
        name,
        window_hours=int(tag["auto_window_hours"]),
        min_active_hours=int(tag["auto_min_active_hours"]),
    )
    await _audit(
        op_store, actor, "user_tag.detect", AuditTarget("user_tag", name), {"added": added}
    )
    return UserTagDetectResponse(added=added)


# ── for the Users tab ────────────────────────────────────────────────────────


def tag_memberships_payload(
    rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """An account's memberships for its detail view, and the limits the applied ones set.

    Returns ``(memberships, limits)``: each membership's tag, source and when
    and by whom it was given; and the combined limits of the tags that apply
    (``quota_daily_cost``, ``quota_monthly_cost``, ``max_concurrent_requests``,
    ``allowed_models``, ``currency``), each null when no tag sets it.
    """
    memberships = [
        {
            "tag": row["name"],
            "source": row["source"],
            "assigned_at": row["assigned_at"],
            "assigned_by": row.get("assigned_by"),
            "system": bool(row.get("system")),
        }
        for row in rows
    ]
    limits = combine_tag_limits(row for row in rows if row.get("source") in ("manual", "auto"))
    return memberships, {
        "quota_daily_cost": limits.quota_daily_cost,
        "quota_monthly_cost": limits.quota_monthly_cost,
        "max_concurrent_requests": limits.max_concurrent_requests,
        "allowed_models": None if limits.allowed_models is None else list(limits.allowed_models),
        "currency": get_accounting_policy().currency,
    }


__all__ = ["router", "tag_memberships_payload"]
