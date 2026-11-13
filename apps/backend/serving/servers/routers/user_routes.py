"""User dashboard routes for API key management and usage statistics."""

import json
import time
from collections import OrderedDict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from serving.config.runtime_settings import get_runtime_settings_instance
from serving.config.settings import get_settings
from serving.config.site_identity import get_site_identity

if TYPE_CHECKING:
    from serving.config.runtime_settings import RuntimeSettings

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response

from serving.audit import AuditTarget
from serving.exceptions import (
    AmbiguousAPIKeyError,
    APIKeyLimitReachedError,
    HardDeleteStateChanged,
    PreferencesTooLargeError,
    ProjectNotFoundError,
    UserNotFoundError,
    generic_message_for_status,
    user_safe_error_for_log,
)
from serving.model_access import get_disabled_models_from_preferences, route_canonical_model_id
from serving.quota import DEFAULT_DAILY_QUOTA_USD
from serving.schemas import ModelList
from serving.schemas_auth import (
    APIKeyCreateRequest,
    APIKeyDeleteResponse,
    APIKeyInfo,
    APIKeyListItem,
    APIKeyListResponse,
    APIKeyRegenerateResponse,
    APIKeyResponse,
    APIKeyRevealRequest,
    APIKeyRevealResponse,
    APIKeyUpdateRequest,
    APIKeyUpdateResponse,
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
    user_audit_actor,
)
from serving.servers.routers.models import _build_model_list_async
from serving.servers.routers.reauth import confirm_account_owner
from serving.storage.base import USER_PREFERENCES_MAX_BYTES
from serving.storage.utils import coerce_json_object
from serving.utils import password as password_utils
from serving.utils.email import is_email_enabled, send_password_changed_email
from serving.utils.logging import get_logger
from serving.utils.login_rate_limit import refusal_worth_recording
from serving.utils.phone_auth import phone_identity_unverified
from serving.utils.request_ip import get_client_ip
from serving.utils.token_utils import optional_token_count

router = APIRouter(prefix="/user", tags=["User Dashboard"])
logger = get_logger(__name__)
LLM_PROBER_LAYOUT_KEY = "llm_prober_layout"
LOCALE_PREFERENCE_KEY = "locale"


def _account_being_deleted() -> HTTPException:
    """The 409 for a write refused because the account is being erased.

    A pending hard-delete claim refuses every write to the account, so a
    request that was authenticated before the erasure started finds the claim
    in force when it writes. Nothing was written; the account is being
    permanently deleted and cannot be changed.
    """
    return HTTPException(
        status_code=409,
        detail="This account is being permanently deleted and cannot be changed.",
    )


#: A safety ceiling on an account's active API keys, not a product limit: the
#: console does not show it and ordinary use never reaches it. Every key spends
#: the same account quota, so it is not about spend; it bounds what a runaway
#: or hostile script can write through the unthrottled create route, and what
#: the listing reads.
MAX_ACTIVE_API_KEYS_PER_ACCOUNT = 100

#: The statuses of a key whose saved value its owner may still reveal. A
#: revoked key stays revealable for reference; showing it does not make it
#: valid again.
_REVEALABLE_STATUSES = ("active", "revoked")


def _clean_key_name(name: str | None) -> str | None:
    """A key label as stored: surrounding whitespace dropped, blank meaning none."""
    if name is None:
        return None
    return name.strip() or None


def _quota_contact() -> str:
    """Support address surfaced with quota info; empty when unconfigured."""
    return get_site_identity().support_email


#: How far back ``/user/recent-requests`` reaches: the account's newest this
#: many requests. A page lies within them, the total counts no further, and a
#: filter matches among them only, so whatever a caller asks for reads at most
#: this many of its rows. The console shows 20 a page: 500 pages.
RECENT_REQUESTS_WINDOW = 10_000

#: How long one of its statements may run, in seconds. Reading the window takes
#: a fraction of that; a statement still running then is stopped and the caller
#: told to retry, rather than holding a general-pool connection for a minute.
RECENT_REQUESTS_STATEMENT_TIMEOUT_SEC = 5.0

# Set for its reads, in their read-only transaction: the statement timeout, and
# the one plan that keeps a read inside the window. The window is the newest
# rows by (timestamp, cost_usd), the order of ``idx_api_logs_user_cost`` read
# backwards within the account; with sorts and bitmap scans off, nothing else
# can produce it. Left to its estimates, the planner walks ``idx_api_logs_timestamp``
# instead for a heavy account -- across every account's rows back to its last
# request, half the table for one that stopped a month ago -- and reads all of
# a light account's rows to sort them.
_RECENT_REQUESTS_READ_SETTINGS = (
    f"SET LOCAL statement_timeout = '{int(RECENT_REQUESTS_STATEMENT_TIMEOUT_SEC * 1000)}ms'; "
    "SET LOCAL enable_sort = off; "
    "SET LOCAL enable_incremental_sort = off; "
    "SET LOCAL enable_bitmapscan = off"
)

#: The window's order, newest first.
_RECENT_ORDER_SQL = "timestamp DESC, cost_usd DESC"

# Bounded in-process TTL cache for the per-user ``api_logs`` row count
# powering ``/user/recent-requests``. The dashboard polls every 60s, so caching
# it per (user, key) keeps the hot path to just the paginated SELECT. A count
# filtered by model is not cached: the model is whatever the caller typed, and
# entries for every string anyone sends would evict everyone else's.
#
# OrderedDict gives us LRU eviction once ``_MAX_ENTRIES`` is reached, which
# bounds memory regardless of how many distinct users hit the endpoint. No
# lock: cache misses for the same key may run a duplicate COUNT under
# concurrent load, which is preferable to serializing all unrelated callers.
_RECENT_REQUESTS_COUNT_TTL_SECONDS: float = 60.0
_RECENT_REQUESTS_COUNT_CACHE_MAX_ENTRIES: int = 4096
_RECENT_REQUESTS_COUNT_CACHE: OrderedDict[
    tuple[str, int | None], tuple[float, tuple[int, bool]]
] = OrderedDict()


def _escape_ilike_substring_term(term: str) -> str:
    """Escape LIKE wildcards so ``ILIKE`` performs literal substring matching."""
    return term.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")


#: The model a request's row shows its user: the auto model it asked for, when
#: an auto model served it (serving/auto_mode.py), and the logged model
#: otherwise. A user is never shown which model served an auto request, so the
#: row's provider, routing detail and error text are withheld with it.
_SHOWN_MODEL_SQL = "COALESCE(metadata->'auto'->>'requested', model_id)"

#: The columns a page shows and filters on, read from the window.
_RECENT_ROW_COLUMNS = (
    "request_id, user_id, model_id, provider, timestamp, status_code, latency_ms, "
    "ttft_ms, stream, prompt_tokens, completion_tokens, reasoning_tokens, "
    "cache_read_tokens, cache_write_tokens, total_tokens, cost_usd, error, metadata"
)


def _recent_rows_sql(columns: str) -> str:
    """*columns* of the account's (``$1``) newest :data:`RECENT_REQUESTS_WINDOW` rows, as ``recent``.

    One range of ``idx_api_logs_user_cost`` (``user_id``, ``timestamp``,
    ``cost_usd``), read newest first and never past the window, under
    :data:`_RECENT_REQUESTS_READ_SETTINGS`.
    """
    return (
        f"(SELECT {columns} FROM api_logs WHERE user_id = $1 "
        f"ORDER BY {_RECENT_ORDER_SQL} LIMIT {RECENT_REQUESTS_WINDOW}) AS recent"
    )


def _build_user_recent_requests_filters(
    user_id: str, model_id: str | None, api_key_id: int | None = None
) -> tuple[str, list[Any]]:
    """Build the shared WHERE clause + bind params for /user/recent-requests.

    Used by both the COUNT cache helper and the paginated SELECT so the two
    queries cannot drift if a future filter is added. ``$1`` is the account;
    the clause filters its window (:func:`_recent_rows_sql`).

    The model filter is a case-insensitive substring of the model the row
    shows, and the key filter reads ``metadata->>'api_key_id'``: the key is not
    a column of its own, so that a log write never depends on a migration of
    ``api_logs``. Neither can use an index, and neither needs one: both read the
    window's rows only, so no filter can make a read reach past it.
    """
    where_clauses = ["user_id = $1"]
    params: list[Any] = [user_id]
    if model_id:
        params.append(_escape_ilike_substring_term(model_id))
        where_clauses.append(f"{_SHOWN_MODEL_SQL} ILIKE '%' || ${len(params)} || '%' ESCAPE '\\'")
    if api_key_id is not None:
        params.append(str(api_key_id))
        where_clauses.append(f"metadata->>'api_key_id' = ${len(params)}")
    return " AND ".join(where_clauses), params


async def _get_cached_user_request_count(
    conn: Any, user_id: str, model_id: str | None, api_key_id: int | None = None
) -> tuple[int, bool]:
    """Return how many of *user_id*'s window match the filters, and whether there is more.

    The second value is True when the account has requests older than the
    window (:data:`RECENT_REQUESTS_WINDOW`), so the count is a lower bound.
    Both read at most the window: run on *conn* inside the transaction that
    set :data:`_RECENT_REQUESTS_READ_SETTINGS`. Cached for
    ``_RECENT_REQUESTS_COUNT_TTL_SECONDS``, except a count by model.
    """
    key = (user_id, api_key_id)
    cacheable = not model_id
    now = time.monotonic()
    cached = _RECENT_REQUESTS_COUNT_CACHE.get(key) if cacheable else None
    if cached is not None and (now - cached[0]) < _RECENT_REQUESTS_COUNT_TTL_SECONDS:
        _RECENT_REQUESTS_COUNT_CACHE.move_to_end(key)
        return cached[1]

    where_sql, params = _build_user_recent_requests_filters(user_id, model_id, api_key_id)
    # Without a filter on the row's contents, the index alone answers.
    columns = "user_id, model_id, metadata" if len(params) > 1 else "user_id"
    count_row = await conn.fetchrow(
        f"""
        SELECT (SELECT COUNT(*) FROM {_recent_rows_sql(columns)} WHERE {where_sql}) AS total,
               EXISTS (SELECT 1 FROM api_logs WHERE user_id = $1
                       ORDER BY {_RECENT_ORDER_SQL} OFFSET {RECENT_REQUESTS_WINDOW}) AS capped
        """,
        *params,
    )
    total = int(count_row["total"] or 0) if count_row else 0
    counted = (total, bool(count_row and count_row.get("capped")))
    if cacheable:
        _RECENT_REQUESTS_COUNT_CACHE[key] = (time.monotonic(), counted)
        _RECENT_REQUESTS_COUNT_CACHE.move_to_end(key)
        while len(_RECENT_REQUESTS_COUNT_CACHE) > _RECENT_REQUESTS_COUNT_CACHE_MAX_ENTRIES:
            _RECENT_REQUESTS_COUNT_CACHE.popitem(last=False)
    return counted


def _get_daily_quota_reset_at() -> datetime:
    """Return the next daily quota reset timestamp."""
    now = datetime.now(UTC)
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
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


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
    """Persist the current user's preferred llm-prober layout.

    Refused with 413, nothing stored, when it would take the account's saved
    preferences over ``USER_PREFERENCES_MAX_BYTES``.
    """
    if not op_store:
        raise HTTPException(status_code=500, detail="Database not available")

    try:
        found = await op_store.set_user_preference(
            current_user["user_id"],
            LLM_PROBER_LAYOUT_KEY,
            body.model_dump(),
            max_total_bytes=USER_PREFERENCES_MAX_BYTES,
        )
    except HardDeleteStateChanged:
        raise _account_being_deleted() from None
    except PreferencesTooLargeError:
        raise HTTPException(
            status_code=413,
            detail=(
                "The layout is too large to save: an account's saved preferences "
                f"may take at most {USER_PREFERENCES_MAX_BYTES} bytes"
            ),
        ) from None
    if not found:
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

    try:
        deleted = await op_store.delete_user_preference(
            current_user["user_id"], LLM_PROBER_LAYOUT_KEY
        )
    except HardDeleteStateChanged:
        raise _account_being_deleted() from None
    if not deleted:
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
    now = datetime.now(UTC)
    chosen_at = now if body.chosen_at is None else min(_as_utc(body.chosen_at), now)
    choice = LocalePreference(value=body.locale, chosen_at=chosen_at)
    value = {"value": choice.value, "chosen_at": _iso_utc(chosen_at)}

    try:
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
    except HardDeleteStateChanged:
        raise _account_being_deleted() from None

    logger.info(
        "locale_preference_updated user_id=%s locale=%s kept=%s", user_id, body.locale, kept.value
    )
    return kept


@router.post("/api-keys", response_model=APIKeyResponse, status_code=201)
async def create_api_key(
    request: Request,
    payload: APIKeyCreateRequest | None = None,
    current_user=Depends(get_current_user),
    op_store=Depends(get_operational_store),
    db_logger=Depends(get_db_logger),
) -> APIKeyResponse:
    """Generate a new API key for the current user.

    Only available after email verification. An account may hold several
    active keys (up to the :data:`MAX_ACTIVE_API_KEYS_PER_ACCOUNT` safety
    ceiling), all spending the same account quota: a key added to an account
    takes its quota, and only the first key is seeded with the role default.
    The body is optional and may
    give the key a ``name``.
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

    name = _clean_key_name(payload.name if payload else None)
    project_id = payload.project_id if payload else None
    if project_id:
        project = await op_store.get_project(current_user["user_id"], project_id)
    else:
        project = await op_store.get_default_project(current_user["user_id"])
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    project_id = project["id"]

    # Generate new API key
    api_key = generate_api_key()
    key_hash = hash_api_key(api_key)
    key_prefix = api_key[:12]
    try:
        rt = get_runtime_settings_instance()
    except RuntimeError:
        rt = None
    default_quota = await get_default_daily_quota_for_role(current_user["role"], rt)

    try:
        row = await op_store.create_key(
            key_hash=key_hash,
            key_prefix=key_prefix,
            user_id=current_user["user_id"],
            account_id=current_user["user_id"],
            quota_daily_cost_usd=default_quota,
            api_key_encrypted=encrypt_api_key(api_key),
            inherit_account_quota=True,
            name=name,
            max_active_keys=MAX_ACTIVE_API_KEYS_PER_ACCOUNT,
            project_id=project_id,
        )
    except ProjectNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Project not found") from exc
    except APIKeyLimitReachedError as exc:
        raise HTTPException(
            status_code=409,
            detail=(
                f"You already have {MAX_ACTIVE_API_KEYS_PER_ACCOUNT} active API keys, the "
                "most an account may hold. Revoke one, or contact the operator of this "
                "deployment if you need more."
            ),
        ) from exc

    logger.info(f"API key created for user: {current_user['user_id']}")
    await log_admin_action(
        db_logger,
        action="create_key",
        details={
            "actor": "user",
            "key_prefix": key_prefix,
            "name": name,
        },
        actor=user_audit_actor(request, current_user),
        target=AuditTarget.user(current_user["user_id"]),
    )

    created_at = row.get("created_at") if isinstance(row, dict) else None
    return APIKeyResponse(
        api_key=api_key,
        key_prefix=key_prefix,
        name=name,
        project_id=project_id,
        project_name=project["name"],
        warning="Save this API key now. It will not be shown again.",
        created_at=created_at or datetime.now(UTC),
    )


@router.get("/api-keys", response_model=APIKeyInfo)
async def get_api_key_info(
    current_user=Depends(get_current_user),
    op_store=Depends(get_operational_store),
) -> APIKeyInfo:
    """Get current user's newest active API key information.

    Kept for clients written when an account held one key; the console lists
    every key through ``GET /user/api-keys/all``. Full keys are only returned
    at creation/regeneration time.
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
        project_id=key_row.get("project_id"),
        project_name=key_row.get("project_name"),
    )


@router.get("/api-keys/all", response_model=APIKeyListResponse)
async def list_api_keys(
    current_user=Depends(get_current_user),
    op_store=Depends(get_operational_store),
) -> APIKeyListResponse:
    """List all API keys owned by the current user, live ones first.

    Every key is masked. A bearer token is all this takes, and one lifted from
    a browser is good for minutes while an API key lasts until revoked, so the
    full value of a saved key comes only from
    ``POST /user/api-keys/{key_prefix}/reveal``, which asks for the password;
    ``revealable`` says which keys have one saved.
    """
    if not op_store:
        raise HTTPException(status_code=500, detail="Database not available")

    rows = await op_store.list_account_keys(current_user["user_id"])

    keys: list[APIKeyListItem] = []
    for row in rows:
        keys.append(
            APIKeyListItem(
                key_prefix=row["key_prefix"],
                key_masked=mask_key_prefix(row["key_prefix"]),
                revealable=row["status"] in _REVEALABLE_STATUSES
                and bool(row.get("api_key_encrypted")),
                name=row.get("name"),
                created_at=row["created_at"],
                last_used_at=row["last_used_at"],
                status=row["status"],
                spent_today_usd=float(row.get("spent_today_usd") or 0),
                spent_month_usd=float(row.get("spent_month_usd") or 0),
                project_id=row.get("project_id"),
                project_name=row.get("project_name"),
                **_key_limits(row),
            )
        )
    return APIKeyListResponse(keys=keys)


@router.post("/api-keys/{key_prefix}/reveal", response_model=APIKeyRevealResponse)
async def reveal_api_key(
    request: Request,
    response: Response,
    key_prefix: str,
    body: APIKeyRevealRequest,
    current_user=Depends(get_current_user),
    op_store=Depends(get_operational_store),
    db_logger=Depends(get_db_logger),
) -> APIKeyRevealResponse:
    """Show one of the current user's saved keys in full, once its owner gives the password.

    The session alone is not enough: its token proves a browser, not a
    person, and the key it would hand out outlives it. The password is checked
    as :func:`confirm_account_owner` checks it for every sensitive change,
    each attempt counting against the sign-in limits. An account without a
    password (one made by phone) is refused: it can set one, or regenerate the
    key, which shows the new value once. Every reveal is audited, and so is
    every one refused for a wrong password -- what a stolen session trying
    its luck looks like; refusals past the sign-in limit, which come as fast
    as they are sent, once a minute per address.

    Raises:
        HTTPException: 400 without the right password, or for an account
            without one; 404 when the account has no such key with a saved
            value; 409 when the saved value cannot be read; 429 when
            rate-limited.
    """
    if not op_store:
        raise HTTPException(status_code=500, detail="Database not available")

    user_id = current_user["user_id"]
    user_row = await op_store.get_user_by_id(user_id)
    if not user_row:
        raise HTTPException(status_code=404, detail="User not found")
    if not user_row.get("password_hash"):
        raise HTTPException(
            status_code=400,
            detail="Showing a saved key needs your password, and this account has none. "
            "Set one in Settings, or regenerate the key to see a new one.",
        )

    row = next(
        (
            key
            for key in await op_store.list_account_keys(user_id)
            if key["key_prefix"] == key_prefix
        ),
        None,
    )
    if row is None or row["status"] not in _REVEALABLE_STATUSES or not row["api_key_encrypted"]:
        raise HTTPException(status_code=404, detail="API key not found")

    try:
        await confirm_account_owner(
            request,
            op_store,
            user_row,
            password=body.password,
            phone_code=None,
            purpose="reveal_api_key",
        )
    except HTTPException as exc:
        limited = exc.status_code == 429
        if not limited or refusal_worth_recording(get_client_ip(request), "reveal_key"):
            await _audit_refused_reveal(
                request,
                current_user,
                db_logger,
                key_prefix,
                "rate_limited" if limited else "wrong_password",
            )
        raise

    try:
        api_key = decrypt_api_key(row["api_key_encrypted"])
    except Exception:
        logger.warning("api_key_decrypt_failed key_prefix=%s", key_prefix)
        raise HTTPException(
            status_code=409,
            detail="This key's saved value cannot be read. Regenerate the key to get a new one.",
        ) from None

    await log_admin_action(
        db_logger,
        action="reveal_key",
        details={"actor": "user", "key_prefix": key_prefix},
        actor=user_audit_actor(request, current_user),
        target=AuditTarget.user(user_id),
    )
    response.headers["Cache-Control"] = "no-store"
    return APIKeyRevealResponse(key_prefix=key_prefix, api_key=api_key)


async def _audit_refused_reveal(
    request: Request,
    current_user: dict[str, Any],
    db_logger: Any,
    key_prefix: str,
    reason: str,
) -> None:
    """Audit a reveal refused at the owner check; a failed write never hides the refusal."""
    try:
        await log_admin_action(
            db_logger,
            action="reveal_key",
            details={"actor": "user", "key_prefix": key_prefix, "refused": reason},
            success=False,
            actor=user_audit_actor(request, current_user),
            target=AuditTarget.user(current_user["user_id"]),
        )
    except Exception:
        logger.exception("reveal_key_refusal_audit_failed key_prefix=%s", key_prefix)


def _key_limits(row: dict[str, Any]) -> dict[str, Any]:
    """A key row's own limits as the API shows them."""

    def usd(value: Any) -> float | None:
        return None if value is None else float(value)

    allowed = row.get("allowed_models")
    return {
        "budget_daily_usd": usd(row.get("budget_daily_usd")),
        "budget_monthly_usd": usd(row.get("budget_monthly_usd")),
        "allowed_models": None if allowed is None else list(allowed),
    }


async def _canonical_allowlist(
    requested: list[str],
    *,
    current_user: dict[str, Any],
    router_exec: Any,
    embedding_adapters: dict[str, Any],
    model_visibility_resolver: Any,
    op_store: Any,
) -> list[str]:
    """The allowlist as stored: canonical ids the account may use, sorted and unique.

    An alias is stored as its model's id, since every access check reads the
    canonical id. Chat and embedding models live in different route tables
    (embedding models never enter ``routes``), so an alias is looked up in
    both. A model is accepted only if the console's own model list shows it
    to this account (``GET /user/models``): that list already applies the
    account's role, model visibility and the models it disabled.

    Raises:
        HTTPException: 422 naming the ids the account cannot use.
    """
    available = await get_user_models(
        current_user=current_user,
        router_exec=router_exec,
        embedding_adapters=embedding_adapters,
        model_visibility_resolver=model_visibility_resolver,
        op_store=op_store,
    )
    usable = {model.id for model in available.data}
    tables = (
        (router_exec.routes, getattr(router_exec, "embedding_routes", None) or {})
        if router_exec is not None
        else ()
    )
    canonical: set[str] = set()
    unknown: list[str] = []
    for model_id in requested:
        route = next((t[model_id] for t in tables if model_id in t), None)
        resolved = route_canonical_model_id(route, model_id) if route is not None else model_id
        if resolved in usable:
            canonical.add(resolved)
        else:
            unknown.append(model_id)
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=f"Not models this account can use: {', '.join(sorted(set(unknown)))}",
        )
    return sorted(canonical)


@router.patch("/api-keys/{key_prefix}", response_model=APIKeyUpdateResponse)
async def update_api_key(
    request: Request,
    key_prefix: str,
    payload: APIKeyUpdateRequest,
    current_user=Depends(get_current_user),
    op_store=Depends(get_operational_store),
    db_logger=Depends(get_db_logger),
    router_exec=Depends(get_router),
    embedding_adapters: dict[str, Any] = Depends(get_embedding_adapters),
    model_visibility_resolver=Depends(get_model_visibility_resolver),
) -> APIKeyUpdateResponse:
    """Change the name or the limits of one of the current user's unrevoked keys.

    Only the fields sent change; ``null`` clears one. A blank name clears it
    too. The limits apply from the key's next request.
    """
    if not op_store:
        raise HTTPException(status_code=500, detail="Database not available")
    sent = payload.model_fields_set
    if not sent:
        raise HTTPException(status_code=422, detail="Nothing to change")

    changes: dict[str, Any] = {}
    if "name" in sent:
        changes["name"] = _clean_key_name(payload.name)
    for field in ("budget_daily_usd", "budget_monthly_usd"):
        if field in sent:
            changes[field] = getattr(payload, field)
    if "allowed_models" in sent:
        changes["allowed_models"] = (
            None
            if payload.allowed_models is None
            else await _canonical_allowlist(
                payload.allowed_models,
                current_user=current_user,
                router_exec=router_exec,
                embedding_adapters=embedding_adapters,
                model_visibility_resolver=model_visibility_resolver,
                op_store=op_store,
            )
        )

    changed = await op_store.update_key_settings(current_user["user_id"], key_prefix, changes)
    if changed is None:
        raise HTTPException(status_code=404, detail="API key not found")
    before, after = changed

    def shown(row: dict[str, Any]) -> dict[str, Any]:
        return {"name": row.get("name"), **_key_limits(row)}

    old, new = shown(before), shown(after)
    await log_admin_action(
        db_logger,
        # A rename alone keeps the action it has always been audited as.
        action="rename_key" if set(changes) == {"name"} else "update_key_settings",
        details={
            "actor": "user",
            "key_prefix": key_prefix,
            **({"name": new["name"]} if "name" in changes else {}),
            "changed": {field: {"from": old[field], "to": new[field]} for field in changes},
        },
        actor=user_audit_actor(request, current_user),
        target=AuditTarget.user(current_user["user_id"]),
    )
    return APIKeyUpdateResponse(key_prefix=key_prefix, **new)


async def _new_key_daily_quota(current_user: dict[str, Any]) -> Decimal:
    """The daily quota the current user's account would give a key it starts afresh."""
    try:
        runtime_settings = get_runtime_settings_instance()
    except RuntimeError:
        runtime_settings = None
    return await get_default_daily_quota_for_role(current_user["role"], runtime_settings)


@router.delete("/api-keys/{key_prefix}", response_model=APIKeyDeleteResponse)
async def delete_api_key(
    request: Request,
    key_prefix: str,
    current_user=Depends(get_current_user),
    db_logger=Depends(get_db_logger),
    op_store=Depends(get_operational_store),
) -> APIKeyDeleteResponse:
    """Revoke an active key or remove a revoked key owned by the current user.

    A revoked key is not removed (409) while it holds the account's quota for
    the next key -- the account has no unrevoked key -- and that quota is
    tighter than a new key's.
    """
    if not db_logger or not db_logger.pool:
        raise HTTPException(status_code=500, detail="Database not available")

    existing = None
    response: APIKeyDeleteResponse | None = None
    audit_action: str | None = None
    audit_details: dict[str, Any] | None = None
    # Whose cached auth the change evicts: the key's owner, which is the
    # account it was found under.
    key_owner: str = current_user["user_id"]

    async with db_logger.pool.acquire() as conn:
        revoked = await conn.fetchrow(
            """
            UPDATE api_keys
            SET status = 'revoked'
            WHERE account_id = $1 AND key_prefix = $2 AND status = 'active'
            RETURNING key_prefix, status, user_id
            """,
            current_user["user_id"],
            key_prefix,
        )
        if revoked:
            key_owner = revoked.get("user_id") or key_owner
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
                # With no unrevoked key, the revoked keys are where the account's
                # quota is kept: the next key takes it from them (the store's
                # ``create_key``). One that holds a tighter limit than a new key
                # would start with stays until the account has a live key again,
                # or removing it would reset a limit an admin set.
                deleted = await conn.fetchrow(
                    """
                    DELETE FROM api_keys AS k
                    WHERE k.account_id = $1 AND k.key_prefix = $2 AND k.status = 'revoked'
                      AND (
                        EXISTS (
                          SELECT 1 FROM api_keys AS live
                          WHERE live.user_id = k.user_id AND live.status <> 'revoked'
                        )
                        OR (
                          k.quota_monthly_cost_usd IS NULL
                          AND COALESCE(k.quota_daily_cost_usd, $4::numeric) >= $3::numeric
                        )
                      )
                    RETURNING k.key_prefix, k.user_id
                    """,
                    current_user["user_id"],
                    key_prefix,
                    await _new_key_daily_quota(current_user),
                    Decimal(str(DEFAULT_DAILY_QUOTA_USD)),
                )
                if deleted is None:
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            "This revoked key holds your account's limits while you have no "
                            "other key. Create a new key first; this one can be removed then."
                        ),
                    )
                key_owner = deleted.get("user_id") or key_owner
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
            action=audit_action,
            details=audit_details,
            actor=user_audit_actor(request, current_user),
            target=AuditTarget.user(current_user["user_id"]),
        )
        # Both branches write api_keys directly, bypassing
        # CachedOperationalStore, so the auth cache is evicted here rather than
        # by the store. Revoking, so the key cannot be used afterwards; and
        # deleting, because the audit lookup caches revoked rows the auth
        # lookups never did (see ``get_key_owner_for_audit``) and would go on
        # naming the former owner of a key that no longer exists. Only the
        # owner's entries: every one of them is tagged with the owner.
        if op_store:
            await op_store.invalidate_user_auth_caches(key_owner)
        return response

    if not existing:
        raise HTTPException(status_code=404, detail="API key not found")
    raise HTTPException(status_code=409, detail="Only active or revoked API keys can be deleted")


async def _rotate_api_key(
    request: Request,
    current_user: dict[str, Any],
    old_key_prefix: str | None,
    op_store: Any,
    db_logger: Any,
) -> APIKeyRegenerateResponse:
    """Give one of the current user's active keys a new secret; the old one stops at once.

    It stays the same key: its name, limits, the account's quota (a rotation
    is not a reason to reset a limit an admin granted), what it has spent
    against its budgets and its request history all carry on. An *old_key_prefix* of
    None means the account's only active key, which the store picks under
    the account lock; 409 when there are several. 404 when the account has
    no such active key.
    """
    api_key = generate_api_key()
    key_prefix = api_key[:12]
    try:
        rotated = await op_store.rotate_key(
            current_user["user_id"],
            old_key_prefix,
            new_key_hash=hash_api_key(api_key),
            new_key_prefix=key_prefix,
            api_key_encrypted=encrypt_api_key(api_key),
        )
    except AmbiguousAPIKeyError as exc:
        raise HTTPException(
            status_code=409,
            detail="You have several active API keys; regenerate one by its prefix.",
        ) from exc
    if not rotated:
        raise HTTPException(status_code=404, detail="No active API key found")
    old_key_prefix = rotated["old_key_prefix"]

    logger.info(f"API key regenerated for user: {current_user['user_id']}")
    await log_admin_action(
        db_logger,
        action="regenerate_key",
        details={
            "actor": "user",
            "old_key_prefix": old_key_prefix,
            "new_key_prefix": key_prefix,
        },
        actor=user_audit_actor(request, current_user),
        target=AuditTarget.user(current_user["user_id"]),
    )

    return APIKeyRegenerateResponse(
        api_key=api_key,
        key_prefix=key_prefix,
        warning="Save this API key now. It will not be shown again.",
        old_key_prefix=old_key_prefix,
    )


@router.post("/api-keys/regenerate", response_model=APIKeyRegenerateResponse)
async def regenerate_api_key(
    request: Request,
    current_user=Depends(get_current_user),
    op_store=Depends(get_operational_store),
    db_logger=Depends(get_db_logger),
) -> APIKeyRegenerateResponse:
    """Regenerate the current user's only active API key.

    Kept for clients written when an account held one key. With several
    active keys it cannot tell which one is meant and answers 409; use
    ``POST /user/api-keys/{key_prefix}/regenerate`` instead.
    """
    if not op_store:
        raise HTTPException(status_code=500, detail="Database not available")

    return await _rotate_api_key(request, current_user, None, op_store, db_logger)


@router.post("/api-keys/{key_prefix}/regenerate", response_model=APIKeyRegenerateResponse)
async def regenerate_api_key_by_prefix(
    request: Request,
    key_prefix: str,
    current_user=Depends(get_current_user),
    op_store=Depends(get_operational_store),
    db_logger=Depends(get_db_logger),
) -> APIKeyRegenerateResponse:
    """Replace one of the current user's active API keys with a new one."""
    if not op_store:
        raise HTTPException(status_code=500, detail="Database not available")
    return await _rotate_api_key(request, current_user, key_prefix, op_store, db_logger)


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
    from serving.storage.pools import DatabaseBusyError

    if not op_store:
        raise HTTPException(status_code=500, detail="Database not available")

    # Resolve per-user concurrency cap (applies regardless of API-key state).
    # A per-user override wins over the role's cap, as in
    # ``UserConcurrencyLimiter.try_acquire``, so the dashboard shows the cap
    # the limiter enforces.
    max_concurrency = current_user.get("max_concurrent_requests")
    if max_concurrency is None:
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
    try:
        if log_store:
            period_key = {"today": "today", "week": "week", "month": "month"}.get(period, "alltime")
            # The period shown, and the month, whose cost is the quota's
            # spent_month_usd; nothing else is read.
            all_usage = await log_store.get_user_usage_detail(
                current_user["user_id"], windows=(period_key, "month")
            )
            period_data = all_usage.get(period_key, _zero)
            spent_month = all_usage.get("month", _zero).get("cost_usd", 0.0)
        else:
            period_data = _zero
            spent_month = 0.0
    except DatabaseBusyError:
        # No connection for it in time: a 503 the console retries, not zeros.
        raise
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
            contact_email=_quota_contact(),
        ),
        usage=UsageStats(
            requests=int(period_data.get("requests") or 0),
            prompt_tokens=int(period_data.get("prompt_tokens") or 0),
            completion_tokens=int(period_data.get("completion_tokens") or 0),
            cache_read_tokens=int(period_data.get("cache_read_tokens") or 0),
            cache_write_tokens=int(period_data.get("cache_write_tokens") or 0),
            reasoning_tokens=optional_token_count(period_data.get("reasoning_tokens")),
            reasoning_tokens_known_requests=int(
                period_data.get("reasoning_tokens_known_requests") or 0
            ),
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
        try:
            await op_store.update_user_fields(
                current_user["user_id"], user_name=update_data["user_name"]
            )
        except HardDeleteStateChanged:
            # A hard-delete claim took the account between the session check
            # and this write. Nothing was written; the account is being
            # erased, so the profile cannot be changed.
            raise _account_being_deleted() from None

    if "avatar_url" in update_data:
        try:
            await op_store.set_user_avatar(
                current_user["user_id"], update_data["avatar_url"] or None
            )
        except HardDeleteStateChanged:
            # A hard-delete claim took the account between the session check
            # and this write. Nothing was written; the account is being
            # erased, so its portrait cannot be changed either.
            raise _account_being_deleted() from None

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

    # A signed-in owner, confirmed just above: not refused for a busy queue.
    new_password_hash = await password_utils.hash_password_async(body.new_password, shed=False)
    try:
        await op_store.update_user_fields(current_user["user_id"], password_hash=new_password_hash)
    except HardDeleteStateChanged:
        # A hard-delete claim took the account between the session check and
        # this write. The password was not changed.
        raise _account_being_deleted() from None
    # Whoever else holds a session (the reason to change a password, often)
    # loses it; this one stays signed in.
    revoked = await op_store.revoke_user_sessions(
        current_user["user_id"], except_sid=current_user.get("sid")
    )
    # So does an outstanding reset link: another way back in.
    await op_store.delete_user_reset_tokens(current_user["user_id"])

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
    key_prefix: str | None = None,
    current_user=Depends(get_current_user),
    db_logger=Depends(get_db_logger),
    op_store=Depends(get_operational_store),
) -> RecentRequestsResponse:
    """Get the current user's recent API requests.

    Returns a paginated list of recent requests with metadata, token usage,
    and cost information. Supports optional filtering by model_id, and by
    ``key_prefix``: one of the account's keys, any status, 404 otherwise. Each
    request names the key it was made with, when it was attributed to one.

    Reaches back over the account's newest :data:`RECENT_REQUESTS_WINDOW`
    requests: ``offset`` must be below that, ``total`` counts no further, and
    ``total_capped`` says the account has older ones. Each read runs on the
    general pool as a history read (:func:`serving.storage.pools.history_read_connection`):
    when none can start in time, or one runs past
    :data:`RECENT_REQUESTS_STATEMENT_TIMEOUT_SEC`, the answer is a 503 with
    ``Retry-After``.
    """
    import asyncpg

    from serving.storage.pools import DatabaseBusyError, history_read_connection

    if not db_logger or not db_logger.pool:
        raise HTTPException(status_code=500, detail="Database not available")

    # Clamp limit to prevent excessive queries
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    if offset >= RECENT_REQUESTS_WINDOW:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Recent requests reach back over your newest {RECENT_REQUESTS_WINDOW} "
                f"requests: offset must be less than {RECENT_REQUESTS_WINDOW}."
            ),
        )
    limit = min(limit, RECENT_REQUESTS_WINDOW - offset)

    # The account's keys, to resolve the filter and to name each row's key.
    prefixes: dict[str, str] = {}
    if op_store:
        prefixes = {
            str(row["id"]): row["key_prefix"]
            for row in await op_store.list_account_keys(current_user["user_id"])
        }
    api_key_id: int | None = None
    if key_prefix is not None:
        matches = [int(i) for i, prefix in prefixes.items() if prefix == key_prefix]
        if not matches:
            raise HTTPException(status_code=404, detail="API key not found")
        api_key_id = matches[0]

    async with history_read_connection(db_logger.pool) as conn:
        try:
            where_sql, params = _build_user_recent_requests_filters(
                current_user["user_id"], model_id, api_key_id
            )
            limit_idx = len(params) + 1
            offset_idx = len(params) + 2
            page_params = [*params, limit, offset]

            async with conn.transaction(readonly=True):
                await conn.execute(_RECENT_REQUESTS_READ_SETTINGS)
                # Cached COUNT(*) — see ``_get_cached_user_request_count``.
                total, total_capped = await _get_cached_user_request_count(
                    conn, current_user["user_id"], model_id, api_key_id
                )

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
                        metadata->'auto'->>'requested' IS NOT NULL AS is_auto,
                        metadata->>'api_key_id' AS api_key_id
                    FROM {_recent_rows_sql(_RECENT_ROW_COLUMNS)}
                    WHERE {where_sql}
                    ORDER BY {_RECENT_ORDER_SQL}
                    LIMIT ${limit_idx} OFFSET ${offset_idx}
                    """,
                    *page_params,
                )
        except asyncpg.QueryCanceledError as exc:
            # Stopped at its statement timeout: the database is too slow just now.
            raise DatabaseBusyError(
                f"recent requests read ran past {RECENT_REQUESTS_STATEMENT_TIMEOUT_SEC:g}s"
            ) from exc
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
            key_prefix=prefixes.get(row["api_key_id"]) if row["api_key_id"] else None,
        )
        for row in rows
    ]

    return RecentRequestsResponse(
        requests=requests, total=total, total_capped=total_capped, limit=limit, offset=offset
    )
