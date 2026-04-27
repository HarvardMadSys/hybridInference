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
    APIKeyDeleteResponse,
    APIKeyInfo,
    APIKeyListItem,
    APIKeyListResponse,
    APIKeyRegenerateResponse,
    APIKeyResponse,
    ChangeEmailRequest,
    ChangeEmailResponse,
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
from serving.servers.auth import decrypt_api_key, encrypt_api_key, generate_api_key, hash_api_key
from serving.servers.deps import get_current_user, get_db_logger
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


def mask_key_prefix(key_prefix: str) -> str:
    """Mask an API key using the stored prefix."""
    return f"{key_prefix}{'*' * 20}"


@router.get("/me", response_model=UserInfo)
async def get_current_user_info(
    current_user=Depends(get_current_user),
    db_logger=Depends(get_db_logger),
) -> UserInfo:
    """Get current user information.

    Returns user profile including email, tier, role, status, and account creation date.
    """
    if not db_logger or not db_logger.pool:
        raise HTTPException(status_code=500, detail="Database not available")

    async with db_logger.pool.acquire() as conn:
        user_row = await conn.fetchrow(
            """
            SELECT id, email, user_name, role, status, email_verified, created_at, last_login_at
            FROM users
            WHERE id = $1
            """,
            current_user["user_id"],
        )

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
    db_logger=Depends(get_db_logger),
) -> LLMProberLayoutResponse:
    """Return the current user's saved llm-prober layout."""
    if not db_logger or not db_logger.pool:
        raise HTTPException(status_code=500, detail="Database not available")

    async with db_logger.pool.acquire() as conn:
        user_row = await conn.fetchrow(
            "SELECT preferences FROM users WHERE id = $1",
            current_user["user_id"],
        )

    if not user_row:
        raise UserNotFoundError(current_user["user_id"])

    preferences = _coerce_preferences(user_row["preferences"])
    return LLMProberLayoutResponse(layout=_extract_llm_prober_layout(preferences))


@router.put("/preferences/llm-prober-layout", response_model=LLMProberLayoutResponse)
async def update_llm_prober_layout(
    body: LLMProberLayoutState,
    current_user=Depends(get_current_user),
    db_logger=Depends(get_db_logger),
) -> LLMProberLayoutResponse:
    """Persist the current user's preferred llm-prober layout."""
    if not db_logger or not db_logger.pool:
        raise HTTPException(status_code=500, detail="Database not available")

    async with db_logger.pool.acquire() as conn, conn.transaction():
        user_row = await conn.fetchrow(
            "SELECT preferences FROM users WHERE id = $1 FOR UPDATE",
            current_user["user_id"],
        )
        if not user_row:
            raise UserNotFoundError(current_user["user_id"])

        preferences = _coerce_preferences(user_row["preferences"])
        preferences[LLM_PROBER_LAYOUT_KEY] = body.model_dump()
        await conn.execute(
            "UPDATE users SET preferences = $1::jsonb WHERE id = $2",
            json.dumps(preferences),
            current_user["user_id"],
        )

    logger.info("llm_prober_layout_updated user_id=%s", current_user["user_id"])
    return LLMProberLayoutResponse(layout=body)


@router.delete("/preferences/llm-prober-layout", response_model=LLMProberLayoutResponse)
async def reset_llm_prober_layout(
    current_user=Depends(get_current_user),
    db_logger=Depends(get_db_logger),
) -> LLMProberLayoutResponse:
    """Delete the saved llm-prober layout for the current user."""
    if not db_logger or not db_logger.pool:
        raise HTTPException(status_code=500, detail="Database not available")

    async with db_logger.pool.acquire() as conn, conn.transaction():
        user_row = await conn.fetchrow(
            "SELECT preferences FROM users WHERE id = $1 FOR UPDATE",
            current_user["user_id"],
        )
        if not user_row:
            raise UserNotFoundError(current_user["user_id"])

        preferences = _coerce_preferences(user_row["preferences"])
        preferences.pop(LLM_PROBER_LAYOUT_KEY, None)
        await conn.execute(
            "UPDATE users SET preferences = $1::jsonb WHERE id = $2",
            json.dumps(preferences),
            current_user["user_id"],
        )

    logger.info("llm_prober_layout_reset user_id=%s", current_user["user_id"])
    return LLMProberLayoutResponse(layout=LLMProberLayoutState())


@router.post("/api-keys", response_model=APIKeyResponse, status_code=201)
async def create_api_key(
    current_user=Depends(get_current_user),
    db_logger=Depends(get_db_logger),
) -> APIKeyResponse:
    """Generate a new API key for the current user.

    Only available after email verification.
    Users can only have one active API key at a time.
    """
    if not db_logger or not db_logger.pool:
        raise HTTPException(status_code=500, detail="Database not available")

    # Check if email is verified
    require_verification = os.getenv("SIGNUP_REQUIRE_EMAIL_VERIFICATION", "1") == "1"
    if require_verification and not current_user.get("email_verified"):
        # In lightweight test apps without exception handlers, return standard HTTP error
        raise HTTPException(status_code=403, detail="Email is not verified.")

    # Check if user already has an active API key
    async with db_logger.pool.acquire() as conn:
        existing_key = await conn.fetchrow(
            """
            SELECT id FROM api_keys
            WHERE account_id = $1 AND status = 'active'
            """,
            current_user["user_id"],
        )

    if existing_key:
        # Use HTTPException for compatibility with test app
        raise HTTPException(status_code=409, detail="You already have an active API key")

    # Generate new API key
    api_key = generate_api_key()
    key_hash = hash_api_key(api_key)
    api_key_encrypted = encrypt_api_key(api_key)
    key_prefix = api_key[:12]  # hyi-xxxxxxxx

    # Get default quota
    default_quota = get_default_daily_quota()

    # Insert API key into database
    async with db_logger.pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO api_keys (
                key_hash, api_key_encrypted, key_prefix, user_id, account_id,
                status, quota_daily_cost_usd, tier
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            """,
            key_hash,
            api_key_encrypted,
            key_prefix,
            current_user["user_id"],  # user_id = account_id for self-registered users
            current_user["user_id"],  # account_id links to users table
            "active",
            default_quota,
            current_user.get("tier", "free"),
        )

    logger.info(f"API key created for user: {current_user['user_id']}")

    return APIKeyResponse(
        api_key=api_key,
        key_prefix=key_prefix,
        warning="You can view this key later from the dashboard.",
        created_at=datetime.now(timezone.utc),
    )


@router.get("/api-keys", response_model=APIKeyInfo)
async def get_api_key_info(
    current_user=Depends(get_current_user),
    db_logger=Depends(get_db_logger),
) -> APIKeyInfo:
    """Get current user's active API key information.

    Full keys are returned for rows created after encrypted storage was added.
    """
    if not db_logger or not db_logger.pool:
        raise HTTPException(status_code=500, detail="Database not available")

    async with db_logger.pool.acquire() as conn:
        key_row = await conn.fetchrow(
            """
            SELECT api_key_encrypted, key_prefix, created_at, last_used_at, status
            FROM api_keys
            WHERE account_id = $1 AND status = 'active'
            """,
            current_user["user_id"],
        )

    if not key_row:
        # For test expectations, return 404 when no active key exists
        raise HTTPException(status_code=404, detail="No active API key found")

    return APIKeyInfo(
        has_key=True,
        api_key=decrypt_api_key(key_row["api_key_encrypted"]),
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

    Full keys are returned for rows created after encrypted storage was added.
    """
    if not db_logger or not db_logger.pool:
        raise HTTPException(status_code=500, detail="Database not available")

    async with db_logger.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT api_key_encrypted, key_prefix, created_at, last_used_at, status
            FROM api_keys
            WHERE account_id = $1
            ORDER BY (status = 'active') DESC, created_at DESC
            """,
            current_user["user_id"],
        )

    keys = [
        APIKeyListItem(
            api_key=decrypt_api_key(row["api_key_encrypted"]),
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
    key_prefix: str,
    current_user=Depends(get_current_user),
    db_logger=Depends(get_db_logger),
) -> APIKeyDeleteResponse:
    """Revoke an active key or remove a revoked key owned by the current user."""
    if not db_logger or not db_logger.pool:
        raise HTTPException(status_code=500, detail="Database not available")

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
            return APIKeyDeleteResponse(
                key_prefix=revoked["key_prefix"],
                status=revoked["status"],
                message="API key revoked.",
            )

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
            return APIKeyDeleteResponse(
                key_prefix=deleted["key_prefix"],
                status="deleted",
                message="Revoked API key removed.",
            )

    if not existing:
        raise HTTPException(status_code=404, detail="API key not found")
    raise HTTPException(status_code=409, detail="Only active or revoked API keys can be deleted")


@router.post("/api-keys/regenerate", response_model=APIKeyRegenerateResponse)
async def regenerate_api_key(
    current_user=Depends(get_current_user),
    db_logger=Depends(get_db_logger),
) -> APIKeyRegenerateResponse:
    """Regenerate API key for current user.

    Immediately invalidates the old key and creates a new one.
    """
    if not db_logger or not db_logger.pool:
        raise HTTPException(status_code=500, detail="Database not available")

    # Get old key info
    async with db_logger.pool.acquire() as conn:
        old_key_row = await conn.fetchrow(
            """
            SELECT id, key_prefix FROM api_keys
            WHERE account_id = $1 AND status = 'active'
            """,
            current_user["user_id"],
        )

    if not old_key_row:
        # Use HTTPException for compatibility with test app
        raise HTTPException(status_code=404, detail="No active API key found")

    # Generate new API key
    api_key = generate_api_key()
    key_hash = hash_api_key(api_key)
    api_key_encrypted = encrypt_api_key(api_key)
    key_prefix = api_key[:12]

    # Get default quota
    default_quota = get_default_daily_quota()

    # Revoke old key and create new one in a transaction
    async with db_logger.pool.acquire() as conn, conn.transaction():
        # Revoke old key
        await conn.execute(
            """
                UPDATE api_keys
                SET status = 'revoked'
                WHERE id = $1
                """,
            old_key_row["id"],
        )

        # Create new key
        await conn.execute(
            """
                INSERT INTO api_keys (
                    key_hash, api_key_encrypted, key_prefix, user_id, account_id,
                    status, quota_daily_cost_usd, tier
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                """,
            key_hash,
            api_key_encrypted,
            key_prefix,
            current_user["user_id"],
            current_user["user_id"],
            "active",
            default_quota,
            current_user.get("tier", "free"),
        )

    logger.info(f"API key regenerated for user: {current_user['user_id']}")

    return APIKeyRegenerateResponse(
        api_key=api_key,
        key_prefix=key_prefix,
        warning="You can view this key later from the dashboard.",
        old_key_prefix=old_key_row["key_prefix"],
    )


@router.get("/usage", response_model=UsageResponse)
async def get_usage(
    period: str = "today",
    current_user=Depends(get_current_user),
    db_logger=Depends(get_db_logger),
) -> UsageResponse:
    """Get user's usage statistics and quota information.

    Supports periods: today, week, month, all
    """
    if not db_logger or not db_logger.pool:
        raise HTTPException(status_code=500, detail="Database not available")

    # Get user's quota
    async with db_logger.pool.acquire() as conn:
        key_row = await conn.fetchrow(
            """
            SELECT quota_daily_cost_usd, tier
            FROM api_keys
            WHERE account_id = $1 AND status = 'active'
            """,
            current_user["user_id"],
        )

    if not key_row:
        # User has no API key yet - return empty usage
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

    daily_limit = float(key_row["quota_daily_cost_usd"] or 0)
    monthly_limit = None  # TODO: Add monthly quota support

    # Calculate date range based on period.
    # Usage data in api_logs is tracked by UTC timestamps.
    if period == "today":
        date_filter = "timestamp >= date_trunc('day', NOW() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'"
    elif period == "week":
        date_filter = "timestamp >= NOW() - INTERVAL '7 days'"
    elif period == "month":
        date_filter = (
            "timestamp >= date_trunc('month', NOW() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'"
        )
    else:  # all
        date_filter = "TRUE"

    # Get usage statistics, tolerate missing logging table in minimal test DB
    async with db_logger.pool.acquire() as conn:
        try:
            usage_row = await conn.fetchrow(
                f"""
                SELECT
                    COUNT(*) as requests,
                    COALESCE(SUM(prompt_tokens), 0) as prompt_tokens,
                    COALESCE(SUM(completion_tokens), 0) as completion_tokens,
                    COALESCE(SUM(cost_usd), 0) as cost_usd
                FROM api_logs
                WHERE user_id = $1 AND {date_filter}
                """,
                current_user["user_id"],
            )

            # Get today's spending
            today_row = await conn.fetchrow(
                """
                SELECT COALESCE(SUM(cost_usd), 0) as spent_today
                FROM api_logs
                WHERE user_id = $1
                  AND timestamp >= date_trunc('day', NOW() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'
                """,
                current_user["user_id"],
            )

            # Get month's spending
            month_row = await conn.fetchrow(
                """
                SELECT COALESCE(SUM(cost_usd), 0) as spent_month
                FROM api_logs
                WHERE user_id = $1
                  AND timestamp >= date_trunc('month', NOW() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'
                """,
                current_user["user_id"],
            )
        except Exception as exc:
            # Missing api_logs table or other query issues - return zeroed stats.
            logger.warning(
                "Failed to query usage stats for user_id=%s: %s",
                current_user["user_id"],
                exc,
            )
            usage_row = {"requests": 0, "prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0}
            today_row = {"spent_today": 0.0}
            month_row = {"spent_month": 0.0}

    spent_today = float(today_row["spent_today"] or 0)
    spent_month = float(month_row["spent_month"] or 0)
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
            requests=int(usage_row["requests"] or 0),
            prompt_tokens=int(usage_row["prompt_tokens"] or 0),
            completion_tokens=int(usage_row["completion_tokens"] or 0),
            cost_usd=float(usage_row["cost_usd"] or 0),
        ),
    )


@router.patch("/profile", response_model=UserInfo)
async def update_profile(
    body: UserProfileUpdate,
    current_user=Depends(get_current_user),
    db_logger=Depends(get_db_logger),
) -> UserInfo:
    """Update user profile information.

    Currently supports updating user_name only.
    """
    if not db_logger or not db_logger.pool:
        raise HTTPException(status_code=500, detail="Database not available")

    # Check if any field is provided for update
    update_data = body.model_dump(exclude_unset=True)
    if not update_data:
        raise HTTPException(status_code=400, detail="No fields provided for update")

    # Update user profile
    async with db_logger.pool.acquire() as conn:
        if "user_name" in update_data:
            await conn.execute(
                "UPDATE users SET user_name = $1 WHERE id = $2",
                update_data["user_name"],
                current_user["user_id"],
            )

        # Fetch updated user info
        user_row = await conn.fetchrow(
            """
            SELECT id, email, user_name, status, email_verified, created_at, last_login_at
            FROM users
            WHERE id = $1
            """,
            current_user["user_id"],
        )

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
    db_logger=Depends(get_db_logger),
) -> ChangePasswordResponse:
    """Change password for logged-in user.

    Requires old password verification for security.
    """
    if not db_logger or not db_logger.pool:
        raise HTTPException(status_code=500, detail="Database not available")

    # Validate new password strength
    is_valid, error_msg = password_utils.validate_password_strength(body.new_password)
    if not is_valid:
        raise HTTPException(status_code=400, detail=error_msg)

    # Get current password hash
    async with db_logger.pool.acquire() as conn:
        user_row = await conn.fetchrow(
            "SELECT password_hash FROM users WHERE id = $1",
            current_user["user_id"],
        )

    if not user_row:
        raise HTTPException(status_code=404, detail="User not found")

    # Verify old password
    if not password_utils.verify_password(body.old_password, user_row["password_hash"]):
        raise HTTPException(
            status_code=400,
            detail="Current password is incorrect.",
        )

    # Check if new password is same as old
    if body.new_password == body.old_password:
        raise HTTPException(
            status_code=400,
            detail="New password must be different from current password.",
        )

    # Update password
    new_password_hash = password_utils.hash_password(body.new_password)

    async with db_logger.pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET password_hash = $1 WHERE id = $2",
            new_password_hash,
            current_user["user_id"],
        )

    logger.info(f"Password changed for user: {current_user['user_id']}")

    return ChangePasswordResponse(message="Password changed successfully.")


@router.post("/change-email", response_model=ChangeEmailResponse)
async def change_email(
    request: Request,
    body: ChangeEmailRequest,
    current_user=Depends(get_current_user),
    db_logger=Depends(get_db_logger),
) -> ChangeEmailResponse:
    """Change email address for logged-in user.

    Requires password verification and sends verification email to new address.
    """
    if not db_logger or not db_logger.pool:
        raise HTTPException(status_code=500, detail="Database not available")

    # Get current user info
    async with db_logger.pool.acquire() as conn:
        user_row = await conn.fetchrow(
            "SELECT email, password_hash FROM users WHERE id = $1",
            current_user["user_id"],
        )

    if not user_row:
        raise HTTPException(status_code=404, detail="User not found")

    # Verify password
    if not password_utils.verify_password(body.password, user_row["password_hash"]):
        raise HTTPException(
            status_code=400,
            detail="Password is incorrect.",
        )

    # Check if new email is same as current
    if body.new_email.lower() == user_row["email"]:
        raise HTTPException(
            status_code=400,
            detail="New email must be different from current email.",
        )

    # Check if new email is already in use
    async with db_logger.pool.acquire() as conn:
        existing_user = await conn.fetchrow(
            "SELECT id FROM users WHERE email = $1",
            body.new_email.lower(),
        )

    if existing_user:
        raise HTTPException(
            status_code=409,
            detail="This email is already registered.",
        )

    # Update email and mark as unverified
    async with db_logger.pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE users
            SET email = $1, email_verified = FALSE
            WHERE id = $2
            """,
            body.new_email.lower(),
            current_user["user_id"],
        )

    # Send verification email to new address
    if is_email_enabled():
        import secrets

        from serving.utils.email import send_verification_email

        verification_token = secrets.token_urlsafe(32)
        expires_at = datetime.now(timezone.utc) + timedelta(hours=24)

        async with db_logger.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO email_verification_tokens (token, user_id, expires_at)
                VALUES ($1, $2, $3)
                """,
                verification_token,
                current_user["user_id"],
                expires_at,
            )

        base_url = os.getenv("BASE_URL") or f"{request.url.scheme}://{request.url.netloc}"
        email_sent = send_verification_email(body.new_email, verification_token, base_url)

        if not email_sent:
            logger.warning(f"Failed to send verification email to {body.new_email}")
            # Do not fail if email fails; continue to return success

    logger.info(f"Email changed for user: {current_user['user_id']} to {body.new_email}")

    return ChangeEmailResponse(
        message="Email changed successfully. Please verify your new email address.",
        new_email=body.new_email,
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
            # Build model filter clause
            model_filter = ""
            params: list = [current_user["user_id"], limit, offset]
            if model_id:
                model_filter = "AND model_id = $4"
                params.append(model_id)

            # Get total count
            count_row = await conn.fetchrow(
                f"""
                SELECT COUNT(*) as total
                FROM api_logs
                WHERE user_id = $1 {model_filter}
                """,
                *([current_user["user_id"]] + ([model_id] if model_id else [])),
            )
            total = int(count_row["total"] or 0) if count_row else 0

            # Get paginated recent requests
            rows = await conn.fetch(
                f"""
                SELECT
                    request_id, model_id, provider, timestamp,
                    status_code, latency_ms, ttft_ms, stream,
                    prompt_tokens, completion_tokens, reasoning_tokens,
                    cache_read_tokens, cache_write_tokens,
                    total_tokens, cost_usd, error
                FROM api_logs
                WHERE user_id = $1 {model_filter}
                ORDER BY timestamp DESC
                LIMIT $2 OFFSET $3
                """,
                *params,
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
        )
        for row in rows
    ]

    return RecentRequestsResponse(requests=requests, total=total, limit=limit, offset=offset)
