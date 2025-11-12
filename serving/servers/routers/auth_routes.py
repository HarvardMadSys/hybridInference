"""Authentication routes for user signup, login, logout, and email verification."""

import hashlib
import os
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response

from serving.schemas_auth import (
    LoginRequest,
    LoginResponse,
    LogoutResponse,
    RefreshResponse,
    SignupRequest,
    SignupResponse,
    UserInfo,
    VerifyEmailResponse,
)
from serving.servers.deps import get_current_user, get_db_logger
from serving.utils.email import is_email_enabled, send_verification_email
from serving.utils.jwt import (
    create_access_token,
    create_refresh_token,
    generate_session_id,
    generate_ulid,
    get_access_token_expire_minutes,
    get_refresh_token_expire_days,
)
from serving.utils.logging import get_logger
from serving.utils.password import hash_password, validate_password_strength, verify_password

router = APIRouter(prefix="/auth", tags=["Authentication"])
logger = get_logger(__name__)


def get_base_url(request: Request) -> str:
    """Get base URL from request or environment variable."""
    base_url = os.getenv("BASE_URL")
    if base_url:
        return base_url.rstrip("/")
    # Fallback to request URL
    return f"{request.url.scheme}://{request.url.netloc}"


def hash_refresh_token(token: str) -> str:
    """Hash refresh token for storage."""
    return hashlib.sha256(token.encode()).hexdigest()


@router.post("/signup", response_model=SignupResponse, status_code=201)
async def signup(
    request: Request,
    body: SignupRequest,
    db_logger=Depends(get_db_logger),
) -> SignupResponse:
    """Register a new user account.

    Creates a new user with email and password. Sends verification email if SMTP is configured.
    User must verify email before they can generate an API key.

    Rate limits:
    - 5 signups per hour per IP
    - 10 signups per day per IP
    """
    # Check if signup is enabled
    if os.getenv("SIGNUP_ENABLED", "1") != "1":
        raise HTTPException(
            status_code=403,
            detail="Public signup is currently disabled. Please contact administrator.",
        )

    # Validate password strength
    is_valid, error_msg = validate_password_strength(body.password)
    if not is_valid:
        raise HTTPException(status_code=400, detail=error_msg)

    # Check database availability
    if not db_logger or not db_logger.pool:
        raise HTTPException(
            status_code=500,
            detail="Database not available",
        )

    # Check if email already exists
    async with db_logger.pool.acquire() as conn:
        existing_user = await conn.fetchrow(
            "SELECT id FROM users WHERE email = $1",
            body.email.lower(),
        )

    if existing_user:
        raise HTTPException(
            status_code=409,
            detail="Email already registered. Please login or use password reset.",
        )

    # Create user
    user_id = generate_ulid()
    password_hash_str = hash_password(body.password)

    async with db_logger.pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO users (id, email, password_hash, user_name, email_verified, status)
            VALUES ($1, $2, $3, $4, $5, $6)
            """,
            user_id,
            body.email.lower(),
            password_hash_str,
            body.user_name,
            False,  # Email not verified yet
            "active",
        )

    # Send verification email if SMTP is configured
    if is_email_enabled():
        # Generate verification token
        verification_token = secrets.token_urlsafe(32)
        expires_at = datetime.now(timezone.utc) + timedelta(hours=24)

        async with db_logger.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO email_verification_tokens (token, user_id, expires_at)
                VALUES ($1, $2, $3)
                """,
                verification_token,
                user_id,
                expires_at,
            )

        # Send email
        base_url = get_base_url(request)
        email_sent = send_verification_email(body.email, verification_token, base_url)

        if not email_sent:
            logger.warning(f"Failed to send verification email to {body.email}")

    logger.info(f"New user registered: {user_id} ({body.email})")

    return SignupResponse(
        message="Account created successfully. Please check your email to verify your account.",
        email=body.email,
        user_id=user_id,
    )


@router.post("/login", response_model=LoginResponse)
async def login(
    response: Response,
    body: LoginRequest,
    db_logger=Depends(get_db_logger),
) -> LoginResponse:
    """Login with email and password.

    Returns access token (15 min) and sets refresh token as HttpOnly cookie (30 days).

    Rate limits:
    - 5 attempts per 15 minutes per email
    - 20 attempts per hour per IP
    """
    if not db_logger or not db_logger.pool:
        raise HTTPException(status_code=500, detail="Database not available")

    # Find user by email
    async with db_logger.pool.acquire() as conn:
        user_row = await conn.fetchrow(
            """
            SELECT id, email, password_hash, user_name, status, email_verified, created_at
            FROM users
            WHERE email = $1
            """,
            body.email.lower(),
        )

    if not user_row:
        raise HTTPException(
            status_code=401,
            detail="Invalid email or password",
        )

    # Verify password
    if not verify_password(body.password, user_row["password_hash"]):
        raise HTTPException(
            status_code=401,
            detail="Invalid email or password",
        )

    # Check account status
    if user_row["status"] != "active":
        raise HTTPException(
            status_code=403,
            detail=f"Account is {user_row['status']}. Please contact support.",
        )

    # Update last login timestamp
    async with db_logger.pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET last_login_at = NOW() WHERE id = $1",
            user_row["id"],
        )

    # Create session and tokens
    session_id = generate_session_id()
    access_token, jti = create_access_token(
        user_id=user_row["id"],
        email=user_row["email"],
        tier="free",  # TODO: Get from user record
        session_id=session_id,
    )
    refresh_token = create_refresh_token()
    refresh_token_hash_str = hash_refresh_token(refresh_token)

    # Store refresh token in database
    session_expires = datetime.now(timezone.utc) + timedelta(days=get_refresh_token_expire_days())
    async with db_logger.pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO auth_sessions (id, user_id, refresh_token_hash, jti, sid, expires_at, revoked)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            """,
            generate_ulid(),
            user_row["id"],
            refresh_token_hash_str,
            jti,
            session_id,
            session_expires,
            False,
        )

    # Set refresh token as HttpOnly cookie
    cookie_secure = os.getenv("COOKIE_SECURE", "0") == "1"
    cookie_domain = os.getenv("COOKIE_DOMAIN")
    cookie_samesite = os.getenv("COOKIE_SAMESITE", "lax")

    response.set_cookie(
        key="refresh_token",
        value=refresh_token,
        httponly=True,
        secure=cookie_secure,
        samesite=cookie_samesite,
        domain=cookie_domain,
        max_age=get_refresh_token_expire_days() * 24 * 60 * 60,
        path="/",
    )

    logger.info(f"User logged in: {user_row['id']} ({user_row['email']})")

    return LoginResponse(
        access_token=access_token,
        token_type="bearer",
        expires_in=get_access_token_expire_minutes() * 60,
        user=UserInfo(
            id=user_row["id"],
            email=user_row["email"],
            user_name=user_row["user_name"],
            tier="free",
            status=user_row["status"],
            email_verified=user_row["email_verified"],
            created_at=user_row["created_at"],
            last_login_at=datetime.now(timezone.utc),
        ),
    )


@router.post("/logout", response_model=LogoutResponse)
async def logout(
    response: Response,
    refresh_token: str | None = Cookie(None),
    current_user=Depends(get_current_user),
    db_logger=Depends(get_db_logger),
) -> LogoutResponse:
    """Logout current user.

    Revokes the refresh token session and clears the cookie.
    Access tokens will expire naturally (15 minutes).
    """
    if not db_logger or not db_logger.pool:
        raise HTTPException(status_code=500, detail="Database not available")

    # Revoke refresh token session if provided
    if refresh_token:
        refresh_token_hash_str = hash_refresh_token(refresh_token)
        async with db_logger.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE auth_sessions
                SET revoked = TRUE
                WHERE refresh_token_hash = $1 AND user_id = $2
                """,
                refresh_token_hash_str,
                current_user["user_id"],
            )

    # Clear refresh token cookie
    response.delete_cookie(key="refresh_token", path="/")

    logger.info(f"User logged out: {current_user['user_id']}")

    return LogoutResponse(message="Logged out successfully")


@router.post("/refresh", response_model=RefreshResponse)
async def refresh(
    response: Response,
    refresh_token: str | None = Cookie(None),
    db_logger=Depends(get_db_logger),
) -> RefreshResponse:
    """Refresh access token using refresh token from cookie.

    Issues a new access token with 15-minute expiration.
    Optionally rotates the refresh token for enhanced security.
    """
    if not refresh_token:
        raise HTTPException(
            status_code=401,
            detail="Missing refresh token. Please login again.",
        )

    if not db_logger or not db_logger.pool:
        raise HTTPException(status_code=500, detail="Database not available")

    # Verify refresh token
    refresh_token_hash_str = hash_refresh_token(refresh_token)
    async with db_logger.pool.acquire() as conn:
        session_row = await conn.fetchrow(
            """
            SELECT id, user_id, sid, expires_at, revoked
            FROM auth_sessions
            WHERE refresh_token_hash = $1
            """,
            refresh_token_hash_str,
        )

    if not session_row:
        raise HTTPException(
            status_code=401,
            detail="Invalid refresh token. Please login again.",
        )

    if session_row["revoked"]:
        raise HTTPException(
            status_code=401,
            detail="Refresh token has been revoked. Please login again.",
        )

    if session_row["expires_at"] < datetime.now(timezone.utc):
        raise HTTPException(
            status_code=401,
            detail="Refresh token has expired. Please login again.",
        )

    # Get user info
    async with db_logger.pool.acquire() as conn:
        user_row = await conn.fetchrow(
            """
            SELECT id, email, status
            FROM users
            WHERE id = $1
            """,
            session_row["user_id"],
        )

    if not user_row or user_row["status"] != "active":
        raise HTTPException(
            status_code=401,
            detail="User account is not active.",
        )

    # Create new access token
    access_token, jti = create_access_token(
        user_id=user_row["id"],
        email=user_row["email"],
        tier="free",
        session_id=session_row["sid"],
    )

    # Generate new refresh token for rotation
    new_refresh_token = create_refresh_token()
    new_refresh_token_hash = hash_refresh_token(new_refresh_token)

    # Update session with new refresh token hash and jti
    async with db_logger.pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE auth_sessions
            SET last_used_at = NOW(), jti = $1, refresh_token_hash = $2
            WHERE id = $3
            """,
            jti,
            new_refresh_token_hash,
            session_row["id"],
        )

    # Set new refresh token as HttpOnly cookie
    cookie_secure = os.getenv("COOKIE_SECURE", "0") == "1"
    cookie_domain = os.getenv("COOKIE_DOMAIN")
    cookie_samesite = os.getenv("COOKIE_SAMESITE", "lax")

    response.set_cookie(
        key="refresh_token",
        value=new_refresh_token,
        httponly=True,
        secure=cookie_secure,
        samesite=cookie_samesite,
        domain=cookie_domain,
        max_age=get_refresh_token_expire_days() * 24 * 60 * 60,
        path="/",
    )

    logger.info(f"Token refreshed for user: {user_row['id']}")

    return RefreshResponse(
        access_token=access_token,
        token_type="bearer",
        expires_in=get_access_token_expire_minutes() * 60,
    )


@router.get("/verify-email", response_model=VerifyEmailResponse)
async def verify_email(
    token: str,
    db_logger=Depends(get_db_logger),
) -> VerifyEmailResponse:
    """Verify user email address using token from email.

    Marks user's email as verified, allowing them to generate API keys.
    """
    if not db_logger or not db_logger.pool:
        raise HTTPException(status_code=500, detail="Database not available")

    # Find verification token
    async with db_logger.pool.acquire() as conn:
        token_row = await conn.fetchrow(
            """
            SELECT user_id, expires_at, used_at
            FROM email_verification_tokens
            WHERE token = $1
            """,
            token,
        )

    if not token_row:
        raise HTTPException(
            status_code=400,
            detail="Invalid verification token.",
        )

    if token_row["used_at"]:
        raise HTTPException(
            status_code=400,
            detail="Verification token has already been used.",
        )

    if token_row["expires_at"] < datetime.now(timezone.utc):
        raise HTTPException(
            status_code=400,
            detail="Verification token has expired. Please request a new one.",
        )

    # Mark email as verified
    async with db_logger.pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE users
            SET email_verified = TRUE
            WHERE id = $1
            """,
            token_row["user_id"],
        )

        # Mark token as used
        await conn.execute(
            """
            UPDATE email_verification_tokens
            SET used_at = NOW()
            WHERE token = $1
            """,
            token,
        )

    logger.info(f"Email verified for user: {token_row['user_id']}")

    return VerifyEmailResponse(
        message="Email verified successfully. You can now generate your API key.",
        email_verified=True,
    )
