"""User dashboard routes for API key management and usage statistics."""

import json
import time
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from serving.accounting import get_accounting_policy
from serving.accounting_api import primary_amounts
from serving.config.runtime_settings import get_runtime_settings_instance
from serving.config.settings import get_settings
from serving.config.site_identity import get_site_identity

if TYPE_CHECKING:
    from serving.config.runtime_settings import RuntimeSettings

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request

from serving.exceptions import (
    UserNotFoundError,
    generic_message_for_status,
    user_safe_error_for_log,
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
    LocalePreference,
    LocalePreferenceUpdate,
    QuotaInfo,
    RecentRequestItem,
    RecentRequestsResponse,
    UsageResponse,
    UsageStats,
    UserInfo,
    UserProfileUpdate,
)
from serving.servers.auth import (
    decrypt_api_key,
    encrypt_api_key,
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
from serving.servers.routers.reauth import confirm_account_owner
from serving.storage.utils import coerce_json_object
from serving.utils import password as password_utils
from serving.utils.email import is_email_enabled, send_password_changed_email
from serving.utils.logging import get_logger
from serving.utils.phone_auth import phone_identity_unverified
from serving.utils.request_ip import get_client_ip

router = APIRouter(prefix="/user", tags=["User Dashboard"])
logger = get_logger(__name__)
LLM_PROBER_LAYOUT_KEY = "llm_prober_layout"
LOCALE_PREFERENCE_KEY = "locale"


def _quota_contact() -> str:
    """Support address surfaced with quota info; empty when unconfigured."""
    return get_site_identity().support_email


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


def _escape_ilike_substring_term(term: str) -> str:
    """Escape LIKE wildcards so ``ILIKE`` performs literal substring matching."""
    return term.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")


#: The model a request's row shows its user: the auto model it asked for, when
#: an auto model served it (serving/auto_mode.py), and the logged model
#: otherwise. A user is never shown which model served an auto request, so the
#: row's provider, routing detail and error text are withheld with it.
_SHOWN_MODEL_SQL = "COALESCE(metadata->'auto'->>'requested', model_id)"


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
        params.append(_escape_ilike_substring_term(model_id))
        where_clauses.append(f"{_SHOWN_MODEL_SQL} ILIKE '%' || ${len(params)} || '%' ESCAPE '\\'")
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


def _extract_locale_preference(preferences: dict[str, Any]) -> LocalePreference | None:
    """Parse the stored interface language, or ``None`` when there is none to show.

    A value this build cannot render (a language since removed, a malformed
    entry) reads as "never chose", so the console falls back to the browser's
    language instead of failing the profile request.
    """
    raw = preferences.get(LOCALE_PREFERENCE_KEY)
    if raw is None:
        return None
    try:
        return LocalePreference.model_validate(raw)
    except Exception:
        return None


def _as_utc(moment: datetime) -> datetime:
    """Return *moment* as an aware UTC datetime; a naive one is taken as UTC."""
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _iso_utc(moment: datetime) -> str:
    """ISO 8601 in UTC with a fixed width, e.g. ``2026-10-17T08:00:00.000000Z``."""
    return _as_utc(moment).isoformat(timespec="microseconds").replace("+00:00", "Z")


async def get_default_daily_quota_for_role(
    role: str,
    runtime_settings: "RuntimeSettings | None",
) -> Decimal:
    """Return the default daily USD quota seeded onto a new API key.

    Reads the ``user_daily_quota_<role>`` runtime setting if present.
    Falls back to the ``SIGNUP_DEFAULT_DAILY_QUOTA_USD`` setting (default
    100.00) if the role is unknown or runtime settings are unavailable (e.g.
    early bootstrap).
    """
    from serving.config.runtime_settings import RUNTIME_SETTINGS_REGISTRY

    key = f"user_daily_quota_{role}"
    if runtime_settings is not None:
        if key in RUNTIME_SETTINGS_REGISTRY:
            val = await runtime_settings.get_float(key)
            return Decimal(str(val))
        logger.warning(
            "No quota runtime setting for role %r — falling back to SIGNUP_DEFAULT_DAILY_QUOTA_USD",
            role,
        )
    return Decimal(str(get_settings().signup_default_daily_quota_usd))


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
        login_name=user_row.get("login_name"),
        phone=user_row.get("phone"),
        phone_verified=user_row.get("phone_verified", False),
        has_password=bool(user_row.get("password_hash")),
        user_name=user_row["user_name"],
        avatar_url=await op_store.get_user_avatar(user_row["id"]),
        role=user_row["role"] or "free",
        status=user_row["status"],
        email_verified=user_row["email_verified"],
        is_admin=current_user.get("is_admin", False),
        created_at=user_row["created_at"],
        last_login_at=user_row["last_login_at"],
        locale=_extract_locale_preference(_coerce_preferences(user_row.get("preferences"))),
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

    if not await op_store.set_user_preference(
        current_user["user_id"], LLM_PROBER_LAYOUT_KEY, body.model_dump()
    ):
        raise UserNotFoundError(current_user["user_id"])

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

    if not await op_store.delete_user_preference(current_user["user_id"], LLM_PROBER_LAYOUT_KEY):
        raise UserNotFoundError(current_user["user_id"])

    logger.info("llm_prober_layout_reset user_id=%s", current_user["user_id"])
    return LLMProberLayoutResponse(layout=LLMProberLayoutState())


@router.put("/preferences/locale", response_model=LocalePreference)
async def update_locale_preference(
    body: LocalePreferenceUpdate,
    current_user=Depends(get_current_user),
    op_store=Depends(get_operational_store),
) -> LocalePreference:
    """Store the account's interface language; the most recent choice wins.

    A choice carried in from a signed-out browser keeps the time it was made,
    never later than now. If the account already holds a choice at least as
    recent — made on another browser meanwhile — that one is kept and
    returned, and the console shows it instead. The comparison happens in the
    database, in the same statement as the write, so overlapping requests
    cannot leave the older choice stored; and only this one key is written,
    so the account's other preferences are untouched.
    """
    if not op_store:
        raise HTTPException(status_code=500, detail="Database not available")

    user_id = current_user["user_id"]
    now = datetime.now(timezone.utc)
    chosen_at = now if body.chosen_at is None else min(_as_utc(body.chosen_at), now)
    choice = LocalePreference(value=body.locale, chosen_at=chosen_at)
    value = {"value": choice.value, "chosen_at": _iso_utc(chosen_at)}

    stored = await op_store.set_user_preference_if_newer(
        user_id, LOCALE_PREFERENCE_KEY, value, chosen_at
    )
    if stored is None:
        raise UserNotFoundError(user_id)

    kept = _extract_locale_preference({LOCALE_PREFERENCE_KEY: stored})
    if kept is None:
        # A newer entry this build cannot read (written outside the API) is
        # "no choice" everywhere else; replace it rather than report it.
        await op_store.set_user_preference(user_id, LOCALE_PREFERENCE_KEY, value)
        kept = choice

    logger.info(
        "locale_preference_updated user_id=%s locale=%s kept=%s", user_id, body.locale, kept.value
    )
    return kept


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
    require_verification = get_settings().signup_require_email_verification
    try:
        rs = get_runtime_settings_instance()
        require_verification = await rs.get_bool("signup_require_email_verification")
    except (RuntimeError, KeyError):
        pass
    if (
        require_verification
        and current_user.get("email")
        and not current_user.get("email_verified")
    ) or phone_identity_unverified(current_user):
        raise HTTPException(status_code=403, detail="Email is not verified.")

    # Check if user already has an active API key. Use the account-or-user
    # lookup so legacy keys with account_id IS NULL are also detected — the DB
    # uniqueness is on user_id, so checking account_id alone would miss them and
    # let the INSERT fail with a 500 instead of a clean 409.
    existing = await op_store.get_key_by_account_or_user(current_user["user_id"])
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
        api_key_encrypted=encrypt_api_key(api_key),
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
            SELECT key_prefix, created_at, last_used_at, status, api_key_encrypted
            FROM api_keys
            WHERE account_id = $1
            ORDER BY (status = 'active') DESC, created_at DESC
            """,
            current_user["user_id"],
        )

    keys: list[APIKeyListItem] = []
    for row in rows:
        full_key: str | None = None
        if row["status"] == "active" and row["api_key_encrypted"]:
            # Display-only decryption: never break the listing on a bad
            # ciphertext — log and fall back to a masked (api_key=None) row.
            try:
                full_key = decrypt_api_key(row["api_key_encrypted"])
            except Exception:
                logger.warning("api_key_decrypt_failed key_prefix=%s", row["key_prefix"])
        keys.append(
            APIKeyListItem(
                api_key=full_key,
                key_prefix=row["key_prefix"],
                key_masked=mask_key_prefix(row["key_prefix"]),
                created_at=row["created_at"],
                last_used_at=row["last_used_at"],
                status=row["status"],
            )
        )
    return APIKeyListResponse(keys=keys)


@router.delete("/api-keys/{key_prefix}", response_model=APIKeyDeleteResponse)
async def delete_api_key(
    request: Request,
    key_prefix: str,
    current_user=Depends(get_current_user),
    db_logger=Depends(get_db_logger),
    op_store=Depends(get_operational_store),
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
        # Both branches write api_keys directly, bypassing
        # CachedOperationalStore, so the auth cache is evicted here rather than
        # by the store. Revoking, so the key cannot be used afterwards; and
        # deleting, because the audit lookup caches revoked rows the auth
        # lookups never did (see ``get_key_owner_for_audit``) and would go on
        # naming the former owner of a key that no longer exists.
        if op_store:
            await op_store.invalidate_auth_caches()
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

    # Account-or-user lookup so legacy keys with account_id IS NULL are found
    # (matches the user_id uniqueness constraint); otherwise regenerate would
    # 404 for these users even though they have a working key.
    old_key_row = await op_store.get_key_by_account_or_user(current_user["user_id"])
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
        api_key_encrypted=encrypt_api_key(api_key),
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
    current_user=Depends(get_current_user),
    op_store=Depends(get_operational_store),
    log_store=Depends(get_log_store),
) -> UsageResponse:
    """Get user's usage statistics and quota information.

    Supports periods: today, week, month, all. ``today`` and ``month`` start at
    00:00 UTC, when the daily quota resets; ``week`` is the last 7 days.
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
                contact_email=_quota_contact(),
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
    all_usage = {}
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
    native_quota = {}
    policy = get_accounting_policy()
    if policy.currency == "CNY":
        native_daily = key_row.get("quota_daily_cost_amount")
        if native_daily is None:
            native_daily = policy.from_usd(daily_limit)
        native_spent = await op_store.get_user_cost_today_amount(current_user["user_id"])
        native_quota = {
            "daily_limit": native_daily,
            "spent_today": native_spent,
            "remaining_today": max(0, native_daily - native_spent),
            "spent_month": (
                all_usage.get("month", {}).get("cost_amount")
                if log_store
                else policy.from_usd(spent_month)
            ),
        }

    return UsageResponse(
        period=period,
        quota=QuotaInfo(
            has_key=True,
            daily_limit_usd=daily_limit,
            monthly_limit_usd=monthly_limit,
            spent_today_usd=spent_today,
            spent_month_usd=spent_month,
            remaining_today_usd=remaining_today,
            **native_quota,
            max_concurrency=max_concurrency,
            reset_at=quota_reset_at,
            reset_timezone="UTC",
            contact_email=_quota_contact(),
        ),
        usage=UsageStats(
            requests=int(period_data.get("requests") or 0),
            prompt_tokens=int(period_data.get("prompt_tokens") or 0),
            completion_tokens=int(period_data.get("completion_tokens") or 0),
            cost_usd=float(period_data.get("cost_usd") or 0),
            **primary_amounts(period_data),
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

    if "avatar_url" in update_data:
        await op_store.set_user_avatar(current_user["user_id"], update_data["avatar_url"] or None)

    user_row = await op_store.get_user_by_id(current_user["user_id"])
    if not user_row:
        raise HTTPException(status_code=404, detail="User not found")

    logger.info(f"Profile updated for user: {current_user['user_id']}")

    return UserInfo(
        id=user_row["id"],
        email=user_row["email"],
        login_name=user_row.get("login_name"),
        phone=user_row.get("phone"),
        phone_verified=user_row.get("phone_verified", False),
        has_password=bool(user_row.get("password_hash")),
        user_name=user_row["user_name"],
        avatar_url=await op_store.get_user_avatar(user_row["id"]),
        role=user_row["role"] or "free",
        status=user_row["status"],
        email_verified=user_row["email_verified"],
        is_admin=(user_row["role"] or "free") == "admin",
        created_at=user_row["created_at"],
        last_login_at=user_row["last_login_at"],
    )


@router.post("/change-password", response_model=ChangePasswordResponse)
async def change_password(
    request: Request,
    body: ChangePasswordRequest,
    background_tasks: BackgroundTasks,
    current_user=Depends(get_current_user),
    op_store=Depends(get_operational_store),
) -> ChangePasswordResponse:
    """Change the signed-in account's password, or give it a first one.

    An account with a password must give it, and the attempt counts against
    the login rate limit. One without (a phone account) must give a
    ``set_password`` code sent to its verified phone.

    Every other session of the account is then revoked, as a password reset
    does, and its verified email is told.
    """
    if not op_store:
        raise HTTPException(status_code=500, detail="Database not available")

    is_valid, error_msg = password_utils.validate_password_strength(body.new_password)
    if not is_valid:
        raise HTTPException(status_code=400, detail=error_msg)

    user_row = await op_store.get_user_by_id(current_user["user_id"])
    if not user_row:
        raise HTTPException(status_code=404, detail="User not found")

    first_password = not user_row.get("password_hash")
    # The current password, or for a first password (a new way in, which the
    # session alone cannot add) a code sent to the account's phone.
    await confirm_account_owner(
        request,
        op_store,
        user_row,
        password=body.old_password,
        phone_code=body.phone_code,
        purpose="set_password",
    )
    if not first_password and body.new_password == body.old_password:
        raise HTTPException(
            status_code=400, detail="New password must be different from current password."
        )

    new_password_hash = password_utils.hash_password(body.new_password)
    await op_store.update_user_fields(current_user["user_id"], password_hash=new_password_hash)
    # Whoever else holds a session (the reason to change a password, often)
    # loses it; this one stays signed in.
    revoked = await op_store.revoke_user_sessions(
        current_user["user_id"], except_sid=current_user.get("sid")
    )

    if user_row.get("email") and user_row.get("email_verified") and is_email_enabled():
        background_tasks.add_task(
            send_password_changed_email, user_row["email"], first_password=first_password
        )

    logger.info(
        f"Password changed for user: {current_user['user_id']} (revoked {revoked} other sessions)"
    )

    return ChangePasswordResponse(
        message="Password changed successfully. Other devices have been signed out."
    )


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
                    request_id, {_SHOWN_MODEL_SQL} AS model_id, provider, timestamp,
                    status_code, latency_ms, ttft_ms, stream,
                    prompt_tokens, completion_tokens, reasoning_tokens,
                    cache_read_tokens, cache_write_tokens,
                    total_tokens, cost_usd, error,
                    metadata->'routewise' AS routewise,
                    metadata->>'request_type' AS request_type,
                    metadata->'auto'->>'requested' IS NOT NULL AS is_auto
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
            provider="auto" if row["is_auto"] else row["provider"],
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
            error=(
                generic_message_for_status(row["status_code"])
                if row["is_auto"] and row["error"] and row["status_code"] is not None
                else user_safe_error_for_log(row["error"], row["status_code"])
            ),
            routewise=None if row["is_auto"] else coerce_json_object(row["routewise"]),
            request_type=row["request_type"],
            **primary_amounts(row),
        )
        for row in rows
    ]

    return RecentRequestsResponse(requests=requests, total=total, limit=limit, offset=offset)
