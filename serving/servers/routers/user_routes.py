"""User dashboard routes for API key management and usage statistics."""

import json
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from serving.exceptions import (
    UserNotFoundError,
)
from serving.schemas_auth import (
    APIKeyInfo,
    APIKeyRegenerateResponse,
    APIKeyResponse,
    ChangeEmailRequest,
    ChangeEmailResponse,
    ChangePasswordRequest,
    ChangePasswordResponse,
    LLMProberLayoutResponse,
    LLMProberLayoutState,
    QuotaInfo,
    UsageResponse,
    UsageStats,
    UserInfo,
    UserProfileUpdate,
)
from serving.servers.auth import generate_api_key, hash_api_key
from serving.servers.deps import get_current_user, get_log_store, get_operational_store
from serving.utils import password as password_utils
from serving.utils.email import is_email_enabled
from serving.utils.logging import get_logger

router = APIRouter(prefix="/user", tags=["User Dashboard"])
logger = get_logger(__name__)
LLM_PROBER_LAYOUT_KEY = "llm_prober_layout"


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


def get_default_daily_quota() -> Decimal:
    """Get default daily quota for new users from environment."""
    quota_str = os.getenv("SIGNUP_DEFAULT_DAILY_QUOTA_USD", "100.00")
    return Decimal(quota_str)


@router.get("/me", response_model=UserInfo)
async def get_current_user_info(
    current_user=Depends(get_current_user),
    op_store=Depends(get_operational_store),
) -> UserInfo:
    """Get current user information.

    Returns user profile including email, tier, role, status, and account creation date.
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
        tier=current_user.get("tier", "free"),
        role=user_row["role"] or "free",
        status=user_row["status"],
        email_verified=user_row["email_verified"],
        is_admin=current_user.get("is_admin", False),
        created_at=user_row["created_at"],
        last_login_at=user_row["last_login_at"],
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
    current_user=Depends(get_current_user),
    op_store=Depends(get_operational_store),
) -> APIKeyResponse:
    """Generate a new API key for the current user.

    Only available after email verification.
    Users can only have one active API key at a time.
    """
    if not op_store:
        raise HTTPException(status_code=500, detail="Database not available")

    # Check if email is verified
    require_verification = os.getenv("SIGNUP_REQUIRE_EMAIL_VERIFICATION", "1") == "1"
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

    default_quota = get_default_daily_quota()

    await op_store.create_key(
        key_hash=key_hash,
        key_prefix=key_prefix,
        user_id=current_user["user_id"],
        account_id=current_user["user_id"],
        tier=current_user.get("tier", "free"),
        quota_daily_cost_usd=default_quota,
    )

    logger.info(f"API key created for user: {current_user['user_id']}")

    return APIKeyResponse(
        api_key=api_key,
        key_prefix=key_prefix,
        warning="Save this key now. It cannot be retrieved later.",
        created_at=datetime.now(timezone.utc),
    )


@router.get("/api-keys", response_model=APIKeyInfo)
async def get_api_key_info(
    current_user=Depends(get_current_user),
    op_store=Depends(get_operational_store),
) -> APIKeyInfo:
    """Get current user's API key information (masked).

    Never returns the full API key after creation.
    """
    if not op_store:
        raise HTTPException(status_code=500, detail="Database not available")

    key_row = await op_store.get_active_key_by_account(current_user["user_id"])

    if not key_row:
        raise HTTPException(status_code=404, detail="No active API key found")

    key_masked = f"{key_row['key_prefix']}{'*' * 20}"

    return APIKeyInfo(
        has_key=True,
        key_prefix=key_row["key_prefix"],
        key_masked=key_masked,
        created_at=key_row["created_at"],
        last_used_at=key_row["last_used_at"],
        status=key_row["status"],
    )


@router.post("/api-keys/regenerate", response_model=APIKeyRegenerateResponse)
async def regenerate_api_key(
    current_user=Depends(get_current_user),
    op_store=Depends(get_operational_store),
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
    default_quota = get_default_daily_quota()

    # Revoke old key via store, then create new one
    await op_store.revoke_key(current_user["user_id"])
    await op_store.create_key(
        key_hash=key_hash,
        key_prefix=key_prefix,
        user_id=current_user["user_id"],
        account_id=current_user["user_id"],
        tier=current_user.get("tier", "free"),
        quota_daily_cost_usd=default_quota,
    )

    logger.info(f"API key regenerated for user: {current_user['user_id']}")

    return APIKeyRegenerateResponse(
        api_key=api_key,
        key_prefix=key_prefix,
        warning="Save this key now. It cannot be retrieved later.",
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

    Supports periods: today, week, month, all
    """
    if not op_store:
        raise HTTPException(status_code=500, detail="Database not available")

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
            ),
            usage=UsageStats(
                requests=0,
                prompt_tokens=0,
                completion_tokens=0,
                cost_usd=0.0,
            ),
        )

    daily_limit = float(key_row.get("quota_daily_cost_usd") or 0)
    monthly_limit = None

    # Fetch usage from log store
    _zero = {"cost_usd": 0.0, "requests": 0}
    try:
        if log_store:
            all_usage = await log_store.get_user_usage_detail(current_user["user_id"])
            # Map period name to the dict returned by get_user_usage_detail
            period_key = {"today": "today", "week": "week", "month": "month"}.get(period, "alltime")
            period_data = all_usage.get(period_key, _zero)
            spent_today = all_usage.get("today", _zero).get("cost_usd", 0.0)
            spent_month = all_usage.get("month", _zero).get("cost_usd", 0.0)
        else:
            period_data = _zero
            spent_today = 0.0
            spent_month = 0.0
    except Exception as exc:
        logger.warning(
            "Failed to query usage stats for user_id=%s: %s",
            current_user["user_id"],
            exc,
        )
        period_data = _zero
        spent_today = 0.0
        spent_month = 0.0

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
        ),
        usage=UsageStats(
            requests=int(period_data.get("requests") or 0),
            prompt_tokens=0,  # Detail-level token breakdown requires separate query
            completion_tokens=0,
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
        tier=current_user.get("tier", "free"),
        status=user_row["status"],
        email_verified=user_row["email_verified"],
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


@router.post("/change-email", response_model=ChangeEmailResponse)
async def change_email(
    request: Request,
    body: ChangeEmailRequest,
    current_user=Depends(get_current_user),
    op_store=Depends(get_operational_store),
) -> ChangeEmailResponse:
    """Change email address for logged-in user.

    Requires password verification and sends verification email to new address.
    """
    if not op_store:
        raise HTTPException(status_code=500, detail="Database not available")

    user_row = await op_store.get_user_by_id(current_user["user_id"])
    if not user_row:
        raise HTTPException(status_code=404, detail="User not found")

    if not password_utils.verify_password(body.password, user_row["password_hash"]):
        raise HTTPException(status_code=400, detail="Password is incorrect.")

    if body.new_email.lower() == user_row["email"]:
        raise HTTPException(
            status_code=400, detail="New email must be different from current email."
        )

    existing_user = await op_store.get_user_by_email(body.new_email)
    if existing_user:
        raise HTTPException(status_code=409, detail="This email is already registered.")

    # Update email and mark as unverified
    await op_store.update_user_fields(
        current_user["user_id"], email=body.new_email.lower(), email_verified=False
    )

    # Send verification email to new address
    if is_email_enabled():
        import secrets

        from serving.utils.email import send_verification_email

        verification_token = secrets.token_urlsafe(32)
        expires_at = datetime.now(timezone.utc) + timedelta(hours=24)
        await op_store.create_verification_token(
            token=verification_token, user_id=current_user["user_id"], expires_at=expires_at
        )

        base_url = os.getenv("BASE_URL") or f"{request.url.scheme}://{request.url.netloc}"
        email_sent = send_verification_email(body.new_email, verification_token, base_url)
        if not email_sent:
            logger.warning(f"Failed to send verification email to {body.new_email}")

    logger.info(f"Email changed for user: {current_user['user_id']} to {body.new_email}")

    return ChangeEmailResponse(
        message="Email changed successfully. Please verify your new email address.",
        new_email=body.new_email,
    )
