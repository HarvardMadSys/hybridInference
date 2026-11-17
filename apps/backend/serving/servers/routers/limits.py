"""API-key endpoint for the concurrency limits a client should plan against.

SDKs and unattended scripts authenticate with an inference API key, so they
read their allowance through the same credential they call models with. The
authentication is the read-only balance path: it never applies the cost quota
gate and never writes ``last_used_at``, so a client can ask for its limits
while already at its quota.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from serving.config.runtime_settings import get_runtime_settings_instance
from serving.schemas_auth import (
    ConcurrencyLimit,
    ConcurrencyLimitsResponse,
    OrgConcurrencyLimit,
    ProjectConcurrencyLimit,
    UserConcurrencyLimit,
)
from serving.servers.auth import verify_api_key_for_balance
from serving.servers.concurrency import (
    COUNT_TOKENS_USER_CONCURRENCY_LIMIT,
    EXEMPT_MODEL_USER_CONCURRENCY_LIMIT,
    effective_user_concurrency,
)

router = APIRouter()


def _special_budget(override: int | None, cap: int) -> int:
    """Return the separate budget an exempt surface enforces, as ``_slot_for_canonical`` does."""
    return cap if override is None else min(int(override), cap)


@router.get("/v1/limits", response_model=ConcurrencyLimitsResponse)
async def get_limits(
    user_ctx: dict[str, Any] = Depends(verify_api_key_for_balance),
) -> ConcurrencyLimitsResponse:
    """Return the concurrency caps the gateway enforces for this API key.

    ``max_concurrency`` is a count of in-flight requests; ``0`` means
    unlimited. A ``null`` project or org cap means that shared pool declares
    no cap of its own. ``project`` and ``org`` are ``null`` when the key is not
    attributed to one; the API-key auth lookup currently fills in project
    attribution only, so ``org`` is ``null`` for every key today.
    """
    try:
        rt = get_runtime_settings_instance()
    except RuntimeError:
        rt = None

    override = user_ctx.get("max_concurrent_requests")
    user_cap = await effective_user_concurrency(user_ctx, rt)

    project_id = user_ctx.get("project_id")
    project_cap = user_ctx.get("project_max_concurrent_requests")
    org_id = user_ctx.get("org_id")
    org_cap = user_ctx.get("org_max_concurrent_requests")

    return ConcurrencyLimitsResponse(
        user=UserConcurrencyLimit(
            role=user_ctx.get("role") or "free",
            source="override" if override is not None else "role",
            max_concurrency=user_cap,
        ),
        project=(
            None
            if project_id is None
            else ProjectConcurrencyLimit(
                id=project_id,
                name=user_ctx.get("project_name"),
                max_concurrency=None if project_cap is None else int(project_cap),
            )
        ),
        org=(
            None
            if org_id is None
            else OrgConcurrencyLimit(
                id=org_id,
                max_concurrency=None if org_cap is None else int(org_cap),
            )
        ),
        exempt_models=ConcurrencyLimit(
            max_concurrency=_special_budget(override, EXEMPT_MODEL_USER_CONCURRENCY_LIMIT)
        ),
        count_tokens=ConcurrencyLimit(
            max_concurrency=_special_budget(override, COUNT_TOKENS_USER_CONCURRENCY_LIMIT)
        ),
    )
