"""User dashboard routes for API key management and usage statistics."""

import json
import os
import time
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from serving.config.runtime_settings import get_runtime_settings_instance

if TYPE_CHECKING:
    from serving.config.runtime_settings import RuntimeSettings
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from serving.exceptions import (
    UserNotFoundError,
)
from serving.model_access import get_disabled_models_from_preferences
from serving.schemas import ModelList
from serving.schemas_auth import (
    APIKeyDeleteResponse,
    APIKeyInfo,
    APIKeyListItem,
    APIKeyListResponse,
    APIKeyRegenerateResponse,
    APIKeyResponse,
    ChangePasswordRequest,
    ChangePasswordResponse,
    LLMProberLayoutResponse,
    LLMProberLayoutState,
    QuotaInfo,
    RecentRequestItem,
    RecentRequestsResponse,
    UsageResponse,
    UsageStats,
    UserInfo,
    UserProfileUpdate,
)
from serving.servers.auth import (
    generate_api_key,
    hash_api_key,
    log_admin_action,
)
from serving.servers.deps import (
    get_current_user,
    get_db_logger,
    get_embedding_adapters,
    get_log_store,
    get_model_visibility_resolver,
    get_operational_store,
    get_router,
)
from serving.servers.routers.models import _build_model_list_async
from serving.storage.utils import coerce_json_object
from serving.utils import password as password_utils
from serving.utils.logging import get_logger
from serving.utils.request_ip import get_client_ip

router = APIRouter(prefix="/user", tags=["User Dashboard"])
logger = get_logger(__name__)
LLM_PROBER_LAYOUT_KEY = "llm_prober_layout"
QUOTA_CONTACT_EMAIL = "admin@freeinference.org"

# Bounded in-process TTL cache for the per-user ``api_logs`` row count
# powering ``/user/recent-requests``. The dashboard polls every 60s and the
# COUNT(*) scales with history size, so caching it per (user, model) keeps
# the hot path to just the paginated SELECT.
#
# OrderedDict gives us LRU eviction once ``_MAX_ENTRIES`` is reached, which
# bounds memory regardless of how many distinct users hit the endpoint. No
# lock: cache misses for the same key may run a duplicate COUNT under
# concurrent load, which is preferable to serializing all unrelated callers.
_RECENT_REQUESTS_COUNT_TTL_SECONDS: float = 60.0
_RECENT_REQUESTS_COUNT_CACHE_MAX_ENTRIES: int = 4096
_RECENT_REQUESTS_COUNT_CACHE: OrderedDict[tuple[str, str | None], tuple[float, int]] = OrderedDict()


def _build_user_recent_requests_filters(
    user_id: str, model_id: str | None
) -> tuple[str, list[Any]]:
    """Build the shared WHERE clause + bind params for /user/recent-requests.

    Used by both the COUNT cache helper and the paginated SELECT so the two
    queries cannot drift if a future filter is added.
    """
    where_clauses = ["user_id = $1"]
    params: list[Any] = [user_id]
    if model_id:
        params.append(model_id)
        where_clauses.append(f"model_id = ${len(params)}")
    return " AND ".join(where_clauses), params


async def _get_cached_user_request_count(conn: Any, user_id: str, model_id: str | None) -> int:
    """Return the cached or freshly-queried ``api_logs`` row count for *user_id*.

    Cached for ``_RECENT_REQUESTS_COUNT_TTL_SECONDS`` to keep heavy-history
    users from paying a full COUNT(*) on every 60s dashboard poll.
    """
    key = (user_id, model_id)
    now = time.monotonic()
    cached = _RECENT_REQUESTS_COUNT_CACHE.get(key)
    if cached is not None and (now - cached[0]) < _RECENT_REQUESTS_COUNT_TTL_SECONDS:
        _RECENT_REQUESTS_COUNT_CACHE.move_to_end(key)
        return cached[1]

    where_sql, params = _build_user_recent_requests_filters(user_id, model_id)
    count_row = await conn.fetchrow(
        f"""
        SELECT COUNT(*) as total
        FROM api_logs
        WHERE {where_sql}
        """,
        *params,
    )
    total = int(count_row["total"] or 0) if count_row else 0
    _RECENT_REQUESTS_COUNT_CACHE[key] = (time.monotonic(), total)
    _RECENT_REQUESTS_COUNT_CACHE.move_to_end(key)
    while len(_RECENT_REQUESTS_COUNT_CACHE) > _RECENT_REQUESTS_COUNT_CACHE_MAX_ENTRIES:
        _RECENT_REQUESTS_COUNT_CACHE.popitem(last=False)
    return total


def _get_daily_quota_reset_at() -> datetime:
    """Return the next daily quota reset timestamp."""
    now = datetime.now(timezone.utc)
    return (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)


def _get_usage_period_start(period: str, user_timezone: str) -> datetime | None:
    """Return the UTC start timestamp for a user-visible usage period."""
    if period == "all":
        return None
    if period == "week":
        return datetime.now(timezone.utc) - timedelta(days=7)

    try:
        tz = ZoneInfo(user_timezone)
    except ZoneInfoNotFoundError as exc:
        raise HTTPException(status_code=400, detail="Invalid timezone") from exc

    now = datetime.now(tz)
    if period == "today":
        local_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "month":
        local_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    else:
        return None

    return local_start.astimezone(timezone.utc)


def _coerce_preferences(value: Any) -> dict[str, Any]:
    """Return a mutable preferences mapping from a DB JSONB value.

    asyncpg may return JSONB columns as either a dict (if a codec is
    registered) or a raw JSON string.  Handle both cases.
    """
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, TypeError):
            pass
    return {}


def _extract_llm_prober_layout(preferences: dict[str, Any]) -> LLMProberLayoutState:
    """Parse the persisted llm-prober layout or fall back to defaults."""
    raw_layout = preferences.get(LLM_PROBER_LAYOUT_KEY, {})
    try:
        return LLMProberLayoutState.model_validate(raw_layout)
    except Exception:
        return LLMProberLayoutState()


async def get_default_daily_quota_for_role(
    role: str,
    runtime_settings: "RuntimeSettings | None",
) -> Decimal:
    """Return the default daily USD quota seeded onto a new API key.

    Reads the ``user_daily_quota_<role>`` runtime setting if present.
    Falls back to ``SIGNUP_DEFAULT_DAILY_QUOTA_USD`` env var (default 100.00)
    if the role is unknown or runtime settings are unavailable (e.g. early
    bootstrap).
    """
    from serving.config.runtime_settings import RUNTIME_SETTINGS_REGISTRY

    key = f"user_daily_quota_{role}"
    if runtime_settings is not None:
        if key in RUNTIME_SETTINGS_REGISTRY:
            val = await runtime_settings.get_float(key)
            return Decimal(str(val))
        logger.warning("No quota runtime setting for role %r — falling back to env var", role)
    quota_str = os.getenv("SIGNUP_DEFAULT_DAILY_QUOTA_USD", "100.00")
    return Decimal(quota_str)


async def get_user_concurrency_for_role(
    role: str,
    runtime_settings: "RuntimeSettings | None",
    *,
    is_admin: bool = False,
) -> int:
    """Return the per-user concurrency cap for ``role``.

    Reads the ``user_concurrency_<role>`` runtime setting if registered.
    Falls back to ``_FALLBACK_LIMITS`` from ``serving.servers.concurrency``
    when runtime settings are unavailable or the role has no registered
    setting. An unknown role degrades to the ``free`` fallback.

    Mirrors ``UserConcurrencyLimiter._limit_for``: when ``is_admin`` is
    true, the admin cap is used regardless of ``role`` so the dashboard
    matches what the limiter actually enforces.
    """
    from serving.config.runtime_settings import RUNTIME_SETTINGS_REGISTRY
    from serving.servers.concurrency import _FALLBACK_LIMITS

    role_key = "admin" if is_admin else (role or "free").lower()
    setting_key = f"user_concurrency_{role_key}"

    if runtime_settings is not None and setting_key in RUNTIME_SETTINGS_REGISTRY:
        return await runtime_settings.get_int(setting_key)

    if role_key not in _FALLBACK_LIMITS:
        logger.warning(
            "No concurrency runtime setting for role %r — falling back to free-tier cap",
            role,
        )
    return _FALLBACK_LIMITS.get(role_key, _FALLBACK_LIMITS["free"])


def mask_key_prefix(key_prefix: str) -> str:
    """Mask an API key using the stored prefix."""
    return f"{key_prefix}{'*' * 20}"


@router.get("/me", response_model=UserInfo)
async def get_current_user_info(
    current_user=Depends(get_current_user),
    op_store=Depends(get_operational_store),
) -> UserInfo:
    """Get current user information.

    Returns user profile including email, role, status, and account creation date.
    """
    if not op_store:
        raise HTTPException(status_code=500, detail="Database not available")

    user_row = await op_store.get_user_by_id(current_user["user_id"])

    if not user_row:
        raise UserNotFoundError(current_user["user_id"])

    return UserInfo(
        id=user_row["id"],
        email=user_row["email"],
        user_name=user_row["user_name"],
        role=user_row["role"] or "free",
        status=user_row["status"],
        email_verified=user_row["email_verified"],
        is_admin=current_user.get("is_admin", False),
        created_at=user_row["created_at"],
        last_login_at=user_row["last_login_at"],
    )


@router.get("/models", response_model=ModelList)
async def get_user_models(
    current_user=Depends(get_current_user),
    router_exec=Depends(get_router),
    embedding_adapters: dict[str, Any] = Depends(get_embedding_adapters),
    model_visibility_resolver=Depends(get_model_visibility_resolver),
    op_store=Depends(get_operational_store),
) -> ModelList:
    """List models available to the current dashboard user."""
    disabled_models: list[str] = []
    if op_store:
        disabled_models = get_disabled_models_from_preferences(
            await op_store.get_user_preferences(current_user["user_id"])
        )
    return await _build_model_list_async(
        router_exec=router_exec,
        embedding_adapters=embedding_adapters,
        user_role=current_user.get("role", "free"),
        model_visibility_resolver=model_visibility_resolver,
        user_ctx={**current_user, "disabled_models": disabled_models},
    )


@router.get("/preferences/llm-prober-layout", response_model=LLMProberLayoutResponse)
async def get_llm_prober_layout(
    current_user=Depends(get_current_user),
    op_store=Depends(get_operational_store),
) -> LLMProberLayoutResponse:
    """Return the current user's saved llm-prober layout."""
    if not op_store:
        raise HTTPException(status_code=500, detail="Database not available")

    preferences = await op_store.get_user_preferences(current_user["user_id"])
    if not preferences and not await op_store.get_user_by_id(current_user["user_id"]):
        raise UserNotFoundError(current_user["user_id"])

    return LLMProberLayoutResponse(layout=_extract_llm_prober_layout(preferences))


@router.put("/preferences/llm-prober-layout", response_model=LLMProberLayoutResponse)
async def update_llm_prober_layout(
    body: LLMProberLayoutState,
    current_user=Depends(get_current_user),
    op_store=Depends(get_operational_store),
) -> LLMProberLayoutResponse:
    """Persist the current user's preferred llm-prober layout."""
    if not op_store:
        raise HTTPException(status_code=500, detail="Database not available")

    preferences = await op_store.get_user_preferences(current_user["user_id"])
    if not preferences and not await op_store.get_user_by_id(current_user["user_id"]):
        raise UserNotFoundError(current_user["user_id"])

    preferences[LLM_PROBER_LAYOUT_KEY] = body.model_dump()
    await op_store.update_user_preferences(current_user["user_id"], preferences)

    logger.info("llm_prober_layout_updated user_id=%s", current_user["user_id"])
    return LLMProberLayoutResponse(layout=body)


@router.delete("/preferences/llm-prober-layout", response_model=LLMProberLayoutResponse)
async def reset_llm_prober_layout(
    current_user=Depends(get_current_user),
    op_store=Depends(get_operational_store),
) -> LLMProberLayoutResponse:
    """Delete the saved llm-prober layout for the current user."""
    if not op_store:
        raise HTTPException(status_code=500, detail="Database not available")

    preferences = await op_store.get_user_preferences(current_user["user_id"])
    if not preferences and not await op_store.get_user_by_id(current_user["user_id"]):
        raise UserNotFoundError(current_user["user_id"])

    preferences.pop(LLM_PROBER_LAYOUT_KEY, None)
    await op_store.update_user_preferences(current_user["user_id"], preferences)

    logger.info("llm_prober_layout_reset user_id=%s", current_user["user_id"])
    return LLMProberLayoutResponse(layout=LLMProberLayoutState())


@router.post("/api-keys", response_model=APIKeyResponse, status_code=201)
async def create_api_key(
    request: Request,
    current_user=Depends(get_current_user),
    op_store=Depends(get_operational_store),
    db_logger=Depends(get_db_logger),
) -> APIKeyResponse:
    """Generate a new API key for the current user.

    Only available after email verification.
    Users can only have one active API key at a time.
    """
    if not op_store:
        raise HTTPException(status_code=500, detail="Database not available")

    # Check if email is verified
    require_verification = os.getenv("SIGNUP_REQUIRE_EMAIL_VERIFICATION", "1") == "1"
    try:
        rs = get_runtime_settings_instance()
        require_verification = await rs.get_bool("signup_require_email_verification")
    except (RuntimeError, KeyError):
        pass
    if require_verification and not current_user.get("email_verified"):
        raise HTTPException(status_code=403, detail="Email is not verified.")

    # Check if user already has an active API key
    existing = await op_store.get_active_key_by_account(current_user["user_id"])
    if existing:
        raise HTTPException(status_code=409, detail="You already have an active API key")

    # Generate new API key
    api_key = generate_api_key()
    key_hash = hash_api_key(api_key)
    key_prefix = api_key[:12]
    try:
        rt = get_runtime_settings_instance()
    except RuntimeError:
        rt = None
    default_quota = await get_default_daily_quota_for_role(current_user["role"], rt)

    await op_store.create_key(
        key_hash=key_hash,
        key_prefix=key_prefix,
        user_id=current_user["user_id"],
        account_id=current_user["user_id"],
        quota_daily_cost_usd=default_quota,
    )

    logger.info(f"API key created for user: {current_user['user_id']}")
    await log_admin_action(
        db_logger,
        get_client_ip(request),
        "create_key",
        current_user["user_id"],
        {
            "actor": "user",
            "key_prefix": key_prefix,
        },
    )

    return APIKeyResponse(
        api_key=api_key,
        key_prefix=key_prefix,
        warning="Save this API key now. It will not be shown again.",
        created_at=datetime.now(timezone.utc),
    )


@router.get("/api-keys", response_model=APIKeyInfo)
async def get_api_key_info(
    current_user=Depends(get_current_user),
    op_store=Depends(get_operational_store),
) -> APIKeyInfo:
    """Get current user's active API key information.

    Full keys are only returned at creation/regeneration time.
    """
    if not op_store:
        raise HTTPException(status_code=500, detail="Database not available")

    key_row = await op_store.get_active_key_by_account(current_user["user_id"])

    if not key_row:
        raise HTTPException(status_code=404, detail="No active API key found")

    return APIKeyInfo(
        has_key=True,
        api_key=None,
        key_prefix=key_row["key_prefix"],
        key_masked=mask_key_prefix(key_row["key_prefix"]),
        created_at=key_row["created_at"],
        last_used_at=key_row["last_used_at"],
        status=key_row["status"],
    )


@router.get("/api-keys/all", response_model=APIKeyListResponse)
async def list_api_keys(
    current_user=Depends(get_current_user),
    db_logger=Depends(get_db_logger),
) -> APIKeyListResponse:
    """List all API keys owned by the current user.

    Only masked identifiers are returned after creation/regeneration.
    """
    if not db_logger or not db_logger.pool:
        raise HTTPException(status_code=500, detail="Database not available")

    async with db_logger.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT key_prefix, created_at, last_used_at, status
            FROM api_keys
            WHERE account_id = $1
            ORDER BY (status = 'active') DESC, created_at DESC
            """,
            current_user["user_id"],
        )

    keys = [
        APIKeyListItem(
            api_key=None,
            key_prefix=row["key_prefix"],
            key_masked=mask_key_prefix(row["key_prefix"]),
            created_at=row["created_at"],
            last_used_at=row["last_used_at"],
            status=row["status"],
        )
        for row in rows
    ]
    return APIKeyListResponse(keys=keys)


@router.delete("/api-keys/{key_prefix}", response_model=APIKeyDeleteResponse)
async def delete_api_key(
    request: Request,
    key_prefix: str,
    current_user=Depends(get_current_user),
    db_logger=Depends(get_db_logger),
) -> APIKeyDeleteResponse:
    """Revoke an active key or remove a revoked key owned by the current user."""
    if not db_logger or not db_logger.pool:
        raise HTTPException(status_code=500, detail="Database not available")

    existing = None
    response: APIKeyDeleteResponse | None = None
    audit_action: str | None = None
    audit_details: dict[str, Any] | None = None

    async with db_logger.pool.acquire() as conn:
        revoked = await conn.fetchrow(
            """
            UPDATE api_keys
            SET status = 'revoked'
            WHERE account_id = $1 AND key_prefix = $2 AND status = 'active'
            RETURNING key_prefix, status
            """,
            current_user["user_id"],
            key_prefix,
        )
        if revoked:
            logger.info(
                "API key revoked for user: %s key_prefix=%s",
                current_user["user_id"],
                key_prefix,
            )
            response = APIKeyDeleteResponse(
                key_prefix=revoked["key_prefix"],
                status=revoked["status"],
                message="API key revoked.",
            )
            audit_action = "revoke_key"
            audit_details = {"actor": "user", "key_prefix": revoked["key_prefix"]}
        else:
            existing = await conn.fetchrow(
                """
                SELECT status
                FROM api_keys
                WHERE account_id = $1 AND key_prefix = $2
                """,
                current_user["user_id"],
                key_prefix,
            )
            if existing and existing["status"] == "revoked":
                deleted = await conn.fetchrow(
                    """
                    DELETE FROM api_keys
                    WHERE account_id = $1 AND key_prefix = $2 AND status = 'revoked'
                    RETURNING key_prefix
                    """,
                    current_user["user_id"],
                    key_prefix,
                )
                logger.info(
                    "Revoked API key removed for user: %s key_prefix=%s",
                    current_user["user_id"],
                    key_prefix,
                )
                response = APIKeyDeleteResponse(
                    key_prefix=deleted["key_prefix"],
                    status="deleted",
                    message="Revoked API key removed.",
                )
                audit_action = "delete_key"
                audit_details = {"actor": "user", "key_prefix": deleted["key_prefix"]}

    if response and audit_action:
        await log_admin_action(
            db_logger,
            get_client_ip(request),
            audit_action,
            current_user["user_id"],
            audit_details,
        )
        return response

    if not existing:
        raise HTTPException(status_code=404, detail="API key not found")
    raise HTTPException(status_code=409, detail="Only active or revoked API keys can be deleted")


@router.post("/api-keys/regenerate", response_model=APIKeyRegenerateResponse)
async def regenerate_api_key(
    request: Request,
    current_user=Depends(get_current_user),
    op_store=Depends(get_operational_store),
    db_logger=Depends(get_db_logger),
) -> APIKeyRegenerateResponse:
    """Regenerate API key for current user.

    Immediately invalidates the old key and creates a new one.
    """
    if not op_store:
        raise HTTPException(status_code=500, detail="Database not available")

    old_key_row = await op_store.get_active_key_by_account(current_user["user_id"])
    if not old_key_row:
        raise HTTPException(status_code=404, detail="No active API key found")

    # Generate new API key
    api_key = generate_api_key()
    key_hash = hash_api_key(api_key)
    key_prefix = api_key[:12]
    try:
        rt = get_runtime_settings_instance()
    except RuntimeError:
        rt = None
    default_quota = await get_default_daily_quota_for_role(current_user["role"], rt)

    # Revoke old key via store, then create new one
    await op_store.revoke_key(current_user["user_id"])
    await op_store.create_key(
        key_hash=key_hash,
        key_prefix=key_prefix,
        user_id=current_user["user_id"],
        account_id=current_user["user_id"],
        quota_daily_cost_usd=default_quota,
    )

    logger.info(f"API key regenerated for user: {current_user['user_id']}")
    await log_admin_action(
        db_logger,
        get_client_ip(request),
        "regenerate_key",
        current_user["user_id"],
        {
            "actor": "user",
            "old_key_prefix": old_key_row["key_prefix"],
            "new_key_prefix": key_prefix,
        },
    )

    return APIKeyRegenerateResponse(
        api_key=api_key,
        key_prefix=key_prefix,
        warning="Save this API key now. It will not be shown again.",
        old_key_prefix=old_key_row["key_prefix"],
    )


@router.get("/usage", response_model=UsageResponse)
async def get_usage(
    period: str = "today",
    timezone_name: str = Query("UTC", alias="timezone"),
    current_user=Depends(get_current_user),
    op_store=Depends(get_operational_store),
    log_store=Depends(get_log_store),
) -> UsageResponse:
    """Get user's usage statistics and quota information.

    Supports periods: today, week, month, all
    """
    if not op_store:
        raise HTTPException(status_code=500, detail="Database not available")

    # Resolve per-user concurrency cap (applies regardless of API-key state).
    try:
        rt = get_runtime_settings_instance()
    except RuntimeError:
        rt = None
    max_concurrency = await get_user_concurrency_for_role(
        current_user.get("role") or "free",
        rt,
        is_admin=bool(current_user.get("is_admin", False)),
    )

    # Get user's quota
    key_row = await op_store.get_active_key_by_account(current_user["user_id"])

    if not key_row:
        return UsageResponse(
            period=period,
            quota=QuotaInfo(
                has_key=False,
                daily_limit_usd=None,
                monthly_limit_usd=None,
                spent_today_usd=None,
                spent_month_usd=None,
                remaining_today_usd=None,
                max_concurrency=max_concurrency,
                reset_at=_get_daily_quota_reset_at(),
                reset_timezone="UTC",
                contact_email=QUOTA_CONTACT_EMAIL,
            ),
            usage=UsageStats(
                requests=0,
                prompt_tokens=0,
                completion_tokens=0,
                cost_usd=0.0,
            ),
        )

    quota_daily_cost_usd = key_row.get("quota_daily_cost_usd")
    daily_limit = 1000.0 if quota_daily_cost_usd is None else float(quota_daily_cost_usd)
    monthly_limit = None
    quota_reset_at = _get_daily_quota_reset_at()

    # Fetch usage from log store (for period breakdown) and op store (for today's quota counter)
    _zero = {"cost_usd": 0.0, "requests": 0, "prompt_tokens": 0, "completion_tokens": 0}
    try:
        if log_store:
            all_usage = await log_store.get_user_usage_detail(current_user["user_id"])
            period_key = {"today": "today", "week": "week", "month": "month"}.get(period, "alltime")
            period_data = all_usage.get(period_key, _zero)
            spent_month = all_usage.get("month", _zero).get("cost_usd", 0.0)
        else:
            period_data = _zero
            spent_month = 0.0
    except Exception as exc:
        logger.warning(
            "Failed to query usage stats for user_id=%s: %s",
            current_user["user_id"],
            exc,
        )
        period_data = _zero
        spent_month = 0.0

    # Read today's spend from the op store counter — same source used by quota enforcement
    try:
        spent_today = await op_store.get_user_cost_today(current_user["user_id"])
    except Exception as exc:
        logger.warning(
            "Failed to query daily cost counter for user_id=%s: %s",
            current_user["user_id"],
            exc,
        )
        spent_today = 0.0

    remaining_today = max(0, daily_limit - spent_today)

    return UsageResponse(
        period=period,
        quota=QuotaInfo(
            has_key=True,
            daily_limit_usd=daily_limit,
            monthly_limit_usd=monthly_limit,
            spent_today_usd=spent_today,
            spent_month_usd=spent_month,
            remaining_today_usd=remaining_today,
            max_concurrency=max_concurrency,
            reset_at=quota_reset_at,
            reset_timezone="UTC",
            contact_email=QUOTA_CONTACT_EMAIL,
        ),
        usage=UsageStats(
            requests=int(period_data.get("requests") or 0),
            prompt_tokens=int(period_data.get("prompt_tokens") or 0),
            completion_tokens=int(period_data.get("completion_tokens") or 0),
            cost_usd=float(period_data.get("cost_usd") or 0),
        ),
    )


@router.patch("/profile", response_model=UserInfo)
async def update_profile(
    body: UserProfileUpdate,
    current_user=Depends(get_current_user),
    op_store=Depends(get_operational_store),
) -> UserInfo:
    """Update user profile information.

    Currently supports updating user_name only.
    """
    if not op_store:
        raise HTTPException(status_code=500, detail="Database not available")

    update_data = body.model_dump(exclude_unset=True)
    if not update_data:
        raise HTTPException(status_code=400, detail="No fields provided for update")

    if "user_name" in update_data:
        await op_store.update_user_fields(
            current_user["user_id"], user_name=update_data["user_name"]
        )

    user_row = await op_store.get_user_by_id(current_user["user_id"])
    if not user_row:
        raise HTTPException(status_code=404, detail="User not found")

    logger.info(f"Profile updated for user: {current_user['user_id']}")

    return UserInfo(
        id=user_row["id"],
        email=user_row["email"],
        user_name=user_row["user_name"],
        role=user_row["role"] or "free",
        status=user_row["status"],
        email_verified=user_row["email_verified"],
        is_admin=(user_row["role"] or "free") == "admin",
        created_at=user_row["created_at"],
        last_login_at=user_row["last_login_at"],
    )


@router.post("/change-password", response_model=ChangePasswordResponse)
async def change_password(
    body: ChangePasswordRequest,
    current_user=Depends(get_current_user),
    op_store=Depends(get_operational_store),
) -> ChangePasswordResponse:
    """Change password for logged-in user.

    Requires old password verification for security.
    """
    if not op_store:
        raise HTTPException(status_code=500, detail="Database not available")

    is_valid, error_msg = password_utils.validate_password_strength(body.new_password)
    if not is_valid:
        raise HTTPException(status_code=400, detail=error_msg)

    user_row = await op_store.get_user_by_id(current_user["user_id"])
    if not user_row:
        raise HTTPException(status_code=404, detail="User not found")

    if not password_utils.verify_password(body.old_password, user_row["password_hash"]):
        raise HTTPException(status_code=400, detail="Current password is incorrect.")

    if body.new_password == body.old_password:
        raise HTTPException(
            status_code=400, detail="New password must be different from current password."
        )

    new_password_hash = password_utils.hash_password(body.new_password)
    await op_store.update_user_fields(current_user["user_id"], password_hash=new_password_hash)

    logger.info(f"Password changed for user: {current_user['user_id']}")

    return ChangePasswordResponse(message="Password changed successfully.")


@router.get("/recent-requests", response_model=RecentRequestsResponse)
async def get_recent_requests(
    limit: int = 50,
    offset: int = 0,
    model_id: str | None = None,
    current_user=Depends(get_current_user),
    db_logger=Depends(get_db_logger),
) -> RecentRequestsResponse:
    """Get the current user's recent API requests.

    Returns a paginated list of recent requests with metadata, token usage,
    and cost information. Supports optional filtering by model_id.
    """
    if not db_logger or not db_logger.pool:
        raise HTTPException(status_code=500, detail="Database not available")

    # Clamp limit to prevent excessive queries
    limit = max(1, min(limit, 200))
    offset = max(0, offset)

    async with db_logger.pool.acquire() as conn:
        try:
            where_sql, params = _build_user_recent_requests_filters(
                current_user["user_id"], model_id
            )
            limit_idx = len(params) + 1
            offset_idx = len(params) + 2
            page_params = [*params, limit, offset]

            # Cached COUNT(*) — see ``_get_cached_user_request_count``.
            total = await _get_cached_user_request_count(conn, current_user["user_id"], model_id)

            # Get paginated recent requests
            rows = await conn.fetch(
                f"""
                SELECT
                    request_id, model_id, provider, timestamp,
                    status_code, latency_ms, ttft_ms, stream,
                    prompt_tokens, completion_tokens, reasoning_tokens,
                    cache_read_tokens, cache_write_tokens,
                    total_tokens, cost_usd, error,
                    metadata->'routewise' AS routewise
                FROM api_logs
                WHERE {where_sql}
                ORDER BY timestamp DESC
                LIMIT ${limit_idx} OFFSET ${offset_idx}
                """,
                *page_params,
            )
        except Exception as exc:
            logger.warning(
                "Failed to query recent requests for user_id=%s: %s",
                current_user["user_id"],
                exc,
            )
            return RecentRequestsResponse(requests=[], total=0, limit=limit, offset=offset)

    requests = [
        RecentRequestItem(
            request_id=row["request_id"],
            model_id=row["model_id"],
            provider=row["provider"],
            timestamp=row["timestamp"],
            status_code=row["status_code"],
            latency_ms=row["latency_ms"],
            ttft_ms=row["ttft_ms"],
            stream=row["stream"],
            prompt_tokens=row["prompt_tokens"],
            completion_tokens=row["completion_tokens"],
            reasoning_tokens=row["reasoning_tokens"],
            cache_read_tokens=row["cache_read_tokens"],
            cache_write_tokens=row["cache_write_tokens"],
            total_tokens=row["total_tokens"],
            cost_usd=float(row["cost_usd"]) if row["cost_usd"] is not None else None,
            error=row["error"],
            routewise=coerce_json_object(row["routewise"]),
        )
        for row in rows
    ]

    return RecentRequestsResponse(requests=requests, total=total, limit=limit, offset=offset)
