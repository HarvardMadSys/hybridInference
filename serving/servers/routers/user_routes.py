"""User dashboard routes for API key management and usage statistics."""

import os
from datetime import datetime, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException

from serving.schemas_auth import (
    APIKeyInfo,
    APIKeyRegenerateResponse,
    APIKeyResponse,
    QuotaInfo,
    UsageResponse,
    UsageStats,
    UserInfo,
    UserProfileUpdate,
)
from serving.servers.auth import generate_api_key, hash_api_key
from serving.servers.deps import get_current_user, get_db_logger
from serving.utils.logging import get_logger

router = APIRouter(prefix="/user", tags=["User Dashboard"])
logger = get_logger(__name__)


def get_default_daily_quota() -> Decimal:
    """Get default daily quota for new users from environment."""
    quota_str = os.getenv("SIGNUP_DEFAULT_DAILY_QUOTA_USD", "100.00")
    return Decimal(quota_str)


@router.get("/me", response_model=UserInfo)
async def get_current_user_info(
    current_user=Depends(get_current_user),
    db_logger=Depends(get_db_logger),
) -> UserInfo:
    """Get current user information.

    Returns user profile including email, tier, status, and account creation date.
    """
    if not db_logger or not db_logger.pool:
        raise HTTPException(status_code=500, detail="Database not available")

    async with db_logger.pool.acquire() as conn:
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
        raise HTTPException(
            status_code=403,
            detail="Email not verified. Please verify your email before generating an API key.",
        )

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
        raise HTTPException(
            status_code=409,
            detail="You already have an active API key. Use regenerate endpoint to create a new one.",
        )

    # Generate new API key
    api_key = generate_api_key()
    key_hash = hash_api_key(api_key)
    key_prefix = api_key[:12]  # hyi-xxxxxxxx

    # Get default quota
    default_quota = get_default_daily_quota()

    # Insert API key into database
    async with db_logger.pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO api_keys (
                key_hash, key_prefix, user_id, account_id,
                status, quota_daily_cost_usd, tier
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            """,
            key_hash,
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
        warning="Save this key now. It cannot be retrieved later.",
        created_at=datetime.now(timezone.utc),
    )


@router.get("/api-keys", response_model=APIKeyInfo)
async def get_api_key_info(
    current_user=Depends(get_current_user),
    db_logger=Depends(get_db_logger),
) -> APIKeyInfo:
    """Get current user's API key information (masked).

    Never returns the full API key after creation.
    """
    if not db_logger or not db_logger.pool:
        raise HTTPException(status_code=500, detail="Database not available")

    async with db_logger.pool.acquire() as conn:
        key_row = await conn.fetchrow(
            """
            SELECT key_prefix, created_at, last_used_at, status
            FROM api_keys
            WHERE account_id = $1 AND status = 'active'
            """,
            current_user["user_id"],
        )

    if not key_row:
        raise HTTPException(
            status_code=404,
            detail="No active API key found. Use POST /user/api-keys to create one.",
        )

    # Mask the key (show prefix + asterisks)
    key_masked = key_row["key_prefix"] + ("*" * 32)

    return APIKeyInfo(
        key_prefix=key_row["key_prefix"],
        key_masked=key_masked,
        created_at=key_row["created_at"],
        last_used_at=key_row["last_used_at"],
        status=key_row["status"],
    )


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
            SELECT id, key_prefix
            FROM api_keys
            WHERE account_id = $1 AND status = 'active'
            """,
            current_user["user_id"],
        )

    if not old_key_row:
        raise HTTPException(
            status_code=404,
            detail="No active API key found. Use POST /user/api-keys to create one.",
        )

    # Generate new API key
    api_key = generate_api_key()
    key_hash = hash_api_key(api_key)
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
                    key_hash, key_prefix, user_id, account_id,
                    status, quota_daily_cost_usd, tier
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                """,
            key_hash,
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
        warning="Save this key now. It cannot be retrieved later.",
        old_key_prefix=old_key_row["key_prefix"],
    )


@router.get("/usage", response_model=UsageResponse)
async def get_usage_stats(
    period: str = "today",
    current_user=Depends(get_current_user),
    db_logger=Depends(get_db_logger),
) -> UsageResponse:
    """Get usage statistics for current user.

    Query parameters:
    - period: today, month, or all (default: today)
    """
    if not db_logger or not db_logger.pool:
        raise HTTPException(status_code=500, detail="Database not available")

    # Get user's quota
    async with db_logger.pool.acquire() as conn:
        key_row = await conn.fetchrow(
            """
            SELECT quota_daily_cost_usd, quota_monthly_cost_usd
            FROM api_keys
            WHERE account_id = $1 AND status = 'active'
            """,
            current_user["user_id"],
        )

    if not key_row:
        raise HTTPException(
            status_code=404,
            detail="No active API key found.",
        )

    daily_limit = float(key_row["quota_daily_cost_usd"] or 0)
    monthly_limit = (
        float(key_row["quota_monthly_cost_usd"]) if key_row["quota_monthly_cost_usd"] else None
    )

    # Get usage stats based on period
    if period == "today":
        time_filter = "timestamp >= date_trunc('day', NOW() AT TIME ZONE 'UTC')"
    elif period == "month":
        time_filter = "timestamp >= date_trunc('month', NOW() AT TIME ZONE 'UTC')"
    else:  # all
        time_filter = "TRUE"

    async with db_logger.pool.acquire() as conn:
        usage_row = await conn.fetchrow(
            f"""
            SELECT
                COUNT(*) as requests,
                COALESCE(SUM(prompt_tokens), 0) as prompt_tokens,
                COALESCE(SUM(completion_tokens), 0) as completion_tokens,
                COALESCE(SUM(cost_usd), 0) as cost_usd
            FROM api_logs
            WHERE user_id = $1 AND {time_filter}
            """,
            current_user["user_id"],
        )

        # Get today's spending
        today_row = await conn.fetchrow(
            """
            SELECT COALESCE(SUM(cost_usd), 0) as cost_spent
            FROM api_logs
            WHERE user_id = $1
              AND timestamp >= date_trunc('day', NOW() AT TIME ZONE 'UTC')
            """,
            current_user["user_id"],
        )

        # Get month's spending
        month_row = await conn.fetchrow(
            """
            SELECT COALESCE(SUM(cost_usd), 0) as cost_spent
            FROM api_logs
            WHERE user_id = $1
              AND timestamp >= date_trunc('month', NOW() AT TIME ZONE 'UTC')
            """,
            current_user["user_id"],
        )

    spent_today = float(today_row["cost_spent"]) if today_row else 0.0
    spent_month = float(month_row["cost_spent"]) if month_row else 0.0

    return UsageResponse(
        period=period,
        quota=QuotaInfo(
            daily_limit_usd=daily_limit,
            monthly_limit_usd=monthly_limit,
            spent_today_usd=spent_today,
            spent_month_usd=spent_month,
            remaining_today_usd=max(0, daily_limit - spent_today),
        ),
        usage=UsageStats(
            requests=int(usage_row["requests"]) if usage_row else 0,
            prompt_tokens=int(usage_row["prompt_tokens"]) if usage_row else 0,
            completion_tokens=int(usage_row["completion_tokens"]) if usage_row else 0,
            cost_usd=float(usage_row["cost_usd"]) if usage_row else 0.0,
        ),
    )


@router.patch("/profile", response_model=UserInfo)
async def update_profile(
    body: UserProfileUpdate,
    current_user=Depends(get_current_user),
    db_logger=Depends(get_db_logger),
) -> UserInfo:
    """Update user profile information.

    Currently only supports updating user_name.
    Email changes require re-verification (future phase).
    """
    if not db_logger or not db_logger.pool:
        raise HTTPException(status_code=500, detail="Database not available")

    # Check if any field is provided for update
    update_data = body.model_dump(exclude_unset=True)
    if not update_data:
        raise HTTPException(status_code=400, detail="No fields provided for update")

    # Update user profile
    async with db_logger.pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE users
            SET user_name = $1
            WHERE id = $2
            """,
            body.user_name,
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
