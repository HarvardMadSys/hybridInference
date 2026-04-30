"""Authentication routes for user signup, login, logout, and email verification."""

import hashlib
import os
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, BackgroundTasks, Cookie, Depends, HTTPException, Request, Response

from serving.config.settings import is_admin_email, settings
from serving.schemas_auth import (
    ForgotPasswordRequest,
    LoginRequest,
    LoginResponse,
    LogoutResponse,
    PasswordResetResponse,
    RefreshResponse,
    ResendVerificationRequest,
    ResendVerificationResponse,
    ResetPasswordRequest,
    SignupRequest,
    SignupResponse,
    UserInfo,
    VerifyEmailResponse,
)
from serving.servers.auth import log_admin_action
from serving.servers.deps import get_current_user, get_db_logger
from serving.utils import password as password_utils
from serving.utils.email import (
    is_email_enabled,
    send_new_registration_admin_email,
    send_verification_email,
)
from serving.utils.jwt import (
    create_access_token,
    create_refresh_token,
    generate_session_id,
    generate_ulid,
    get_access_token_expire_minutes,
    get_refresh_token_expire_days,
)
from serving.utils.logging import get_logger
from serving.utils.request_ip import get_client_ip

router = APIRouter(prefix="/auth", tags=["Authentication"])
logger = get_logger(__name__)
REFRESH_TOKEN_COOKIE = "refresh_token"


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


def _env_flag(name: str, default: str = "0") -> bool:
    """Read a boolean-like environment flag."""
    return os.getenv(name, default).lower() in {"1", "true", "yes", "on"}


def _refresh_cookie_options() -> dict[str, object]:
    """Return shared options for refresh-token cookie operations."""
    return {
        "httponly": True,
        "secure": _env_flag("COOKIE_SECURE"),
        "samesite": os.getenv("COOKIE_SAMESITE", "lax"),
        "domain": os.getenv("COOKIE_DOMAIN"),
        "path": "/",
    }


def set_refresh_token_cookie(response: Response, refresh_token: str) -> None:
    """Set the persistent refresh-token cookie."""
    refresh_token_max_age = get_refresh_token_expire_days() * 24 * 60 * 60
    response.set_cookie(
        key=REFRESH_TOKEN_COOKIE,
        value=refresh_token,
        max_age=refresh_token_max_age,
        expires=datetime.now(timezone.utc) + timedelta(seconds=refresh_token_max_age),
        **_refresh_cookie_options(),
    )


def delete_refresh_token_cookie(response: Response) -> None:
    """Delete the refresh-token cookie using the same domain/path settings."""
    response.delete_cookie(
        key=REFRESH_TOKEN_COOKIE,
        **_refresh_cookie_options(),
    )


@router.post("/signup", response_model=SignupResponse, status_code=201)
async def signup(
    request: Request,
    body: SignupRequest,
    background_tasks: BackgroundTasks,
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
    is_valid, error_msg = password_utils.validate_password_strength(body.password)
    if not is_valid:
        # Return standard validation error for tests (HTTP 400)
        raise HTTPException(
            status_code=400, detail=error_msg or "Password does not meet security requirements"
        )

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
        # Return conflict using standard HTTPException for test apps without exception handlers
        raise HTTPException(status_code=409, detail=f"Email {body.email} already registered")

    # Determine initial status based on approval setting
    require_approval = os.getenv("SIGNUP_REQUIRE_APPROVAL", "0") == "1"
    initial_status = "pending_approval" if require_approval else "active"

    # Create user
    user_id = generate_ulid()
    password_hash_str = password_utils.hash_password(body.password)

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
            initial_status,
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

        # Send email in background so signup returns even if SMTP is slow
        base_url = get_base_url(request)
        background_tasks.add_task(send_verification_email, body.email, verification_token, base_url)

    # Notify admins of new registration when approval is required
    if require_approval and is_email_enabled():
        admin_emails = [e.strip() for e in settings.admin_emails.split(",") if e.strip()]
        for admin_email in admin_emails:
            background_tasks.add_task(
                send_new_registration_admin_email,
                to_email=admin_email,
                user_email=body.email,
                user_name=body.user_name,
                user_id=user_id,
            )

    logger.info(f"New user registered: {user_id} ({body.email}) [status={initial_status}]")
    await log_admin_action(
        db_logger,
        get_client_ip(request),
        "create_user",
        user_id,
        {
            "email": body.email.lower(),
            "user_name": body.user_name,
            "status": initial_status,
            "requires_approval": require_approval,
        },
    )

    if require_approval:
        message = (
            "Account created successfully. Your registration is pending admin approval. "
            "You will receive an email once your account is approved."
        )
    else:
        message = "Account created successfully. Please check your email to verify your account."

    return SignupResponse(
        message=message,
        email=body.email,
        user_id=user_id,
        requires_approval=require_approval,
    )


@router.post("/login", response_model=LoginResponse)
async def login(
    response: Response,
    body: LoginRequest,
    db_logger=Depends(get_db_logger),
) -> LoginResponse:
    """Login with email and password.

    Returns access token (15 min) and sets refresh token as HttpOnly cookie (365 days by default).

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
            SELECT id, email, password_hash, user_name, status, email_verified, created_at, role
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
    if not password_utils.verify_password(body.password, user_row["password_hash"]):
        raise HTTPException(
            status_code=401,
            detail="Invalid email or password",
        )

    # Check if email verification is required and if email is verified
    require_verification = os.getenv("SIGNUP_REQUIRE_EMAIL_VERIFICATION", "1") == "1"
    if require_verification and not user_row["email_verified"]:
        raise HTTPException(
            status_code=403,
            detail="Email not verified. Please check your email for the verification link.",
        )

    # Check account status
    if user_row["status"] == "pending_approval":
        raise HTTPException(
            status_code=403,
            detail="Your registration is pending admin approval. You will receive an email once approved.",
        )

    if user_row["status"] == "rejected":
        raise HTTPException(
            status_code=403,
            detail="Your registration was not approved. Please contact support for details.",
        )

    if user_row["status"] != "active":
        raise HTTPException(
            status_code=403,
            detail=f"Account is {user_row['status']}. Please contact support.",
        )

    # Update last login timestamp and fetch API key tier
    async with db_logger.pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET last_login_at = NOW() WHERE id = $1",
            user_row["id"],
        )
        key_row = await conn.fetchrow(
            "SELECT tier FROM api_keys WHERE (account_id = $1 OR user_id = $1) AND status = 'active' LIMIT 1",
            user_row["id"],
        )

    # Create session and tokens
    session_id = generate_session_id()
    user_role = user_row["role"] or "free"
    user_tier = (key_row["tier"] if key_row else None) or "free"

    # Bootstrap seed: promote ADMIN_EMAILS users to admin when their role is
    # still at the default 'free' (i.e. never explicitly assigned a higher
    # role).  Users demoted to internal will NOT be re-promoted.  Edge case:
    # demotion back to 'free' while email remains in ADMIN_EMAILS will
    # trigger re-promotion — remove the email from the env to prevent this.
    if is_admin_email(user_row["email"]) and user_role == "free":
        user_role = "admin"
        async with db_logger.pool.acquire() as conn:
            await conn.execute(
                "UPDATE users SET role = 'admin' WHERE id = $1",
                user_row["id"],
            )
        logger.info(f"Bootstrap-seeded user {user_row['id']} to admin (ADMIN_EMAILS)")

    is_admin = user_role == "admin"
    access_token, jti = create_access_token(
        user_id=user_row["id"],
        email=user_row["email"],
        tier=user_tier,
        session_id=session_id,
        is_admin=is_admin,
        role=user_role,
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

    set_refresh_token_cookie(response, refresh_token)

    logger.info(f"User logged in: {user_row['id']} ({user_row['email']})")

    return LoginResponse(
        access_token=access_token,
        token_type="bearer",
        expires_in=get_access_token_expire_minutes() * 60,
        user=UserInfo(
            id=user_row["id"],
            email=user_row["email"],
            user_name=user_row["user_name"],
            tier=user_tier,
            role=user_role,
            status=user_row["status"],
            email_verified=user_row["email_verified"],
            created_at=user_row["created_at"],
            last_login_at=datetime.now(timezone.utc),
            is_admin=is_admin,
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

    delete_refresh_token_cookie(response)

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

    # Get user info and API key tier
    async with db_logger.pool.acquire() as conn:
        user_row = await conn.fetchrow(
            """
            SELECT id, email, status, email_verified, role
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

    # Check if email verification is required and if email is verified
    require_verification = os.getenv("SIGNUP_REQUIRE_EMAIL_VERIFICATION", "1") == "1"
    if require_verification and not user_row["email_verified"]:
        raise HTTPException(
            status_code=403,
            detail="Email not verified. Please verify your email to continue.",
        )

    # Fetch API key tier
    async with db_logger.pool.acquire() as conn:
        key_row = await conn.fetchrow(
            "SELECT tier FROM api_keys WHERE (account_id = $1 OR user_id = $1) AND status = 'active' LIMIT 1",
            user_row["id"],
        )
    user_tier = (key_row["tier"] if key_row else None) or "free"

    # Create new access token
    user_role = user_row["role"] or "free"

    # Bootstrap seed on refresh (same logic as login — only when role is 'free')
    if is_admin_email(user_row["email"]) and user_role == "free":
        user_role = "admin"
        async with db_logger.pool.acquire() as conn:
            await conn.execute(
                "UPDATE users SET role = 'admin' WHERE id = $1",
                user_row["id"],
            )
        logger.info(f"Bootstrap-seeded user {user_row['id']} to admin on refresh (ADMIN_EMAILS)")

    is_admin = user_role == "admin"
    access_token, jti = create_access_token(
        user_id=user_row["id"],
        email=user_row["email"],
        tier=user_tier,
        session_id=session_row["sid"],
        is_admin=is_admin,
        role=user_role,
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

    set_refresh_token_cookie(response, new_refresh_token)

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


@router.post("/forgot-password", response_model=PasswordResetResponse)
async def forgot_password(
    request: Request,
    body: ForgotPasswordRequest,
    background_tasks: BackgroundTasks,
    db_logger=Depends(get_db_logger),
) -> PasswordResetResponse:
    """Request password reset email.

    Sends a password reset link to the user's email if the account exists.
    Always returns success to prevent email enumeration.
    """
    if not db_logger or not db_logger.pool:
        raise HTTPException(status_code=500, detail="Database not available")

    # Find user by email
    async with db_logger.pool.acquire() as conn:
        user_row = await conn.fetchrow(
            "SELECT id, email FROM users WHERE email = $1",
            body.email.lower(),
        )

    # Always return success to prevent email enumeration
    if not user_row:
        logger.info(f"Password reset requested for non-existent email: {body.email}")
        return PasswordResetResponse(
            message="If an account exists with this email, a password reset link has been sent."
        )

    # Generate reset token
    reset_token = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + timedelta(hours=1)  # 1 hour expiry

    async with db_logger.pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO password_reset_tokens (token, user_id, expires_at)
            VALUES ($1, $2, $3)
            """,
            reset_token,
            user_row["id"],
            expires_at,
        )

    # Send reset email if SMTP is configured
    if is_email_enabled():
        from serving.utils.email import send_password_reset_email

        base_url = get_base_url(request)
        background_tasks.add_task(
            send_password_reset_email, user_row["email"], reset_token, base_url
        )

    logger.info(f"Password reset requested for user: {user_row['id']}")

    return PasswordResetResponse(
        message="If an account exists with this email, a password reset link has been sent."
    )


@router.post("/reset-password", response_model=PasswordResetResponse)
async def reset_password(
    body: ResetPasswordRequest,
    db_logger=Depends(get_db_logger),
) -> PasswordResetResponse:
    """Reset password using token from email.

    Validates the reset token and updates the user's password.
    """
    if not db_logger or not db_logger.pool:
        raise HTTPException(status_code=500, detail="Database not available")

    # Validate password strength
    is_valid, error_msg = password_utils.validate_password_strength(body.new_password)
    if not is_valid:
        raise HTTPException(status_code=400, detail=error_msg)

    # Find reset token
    async with db_logger.pool.acquire() as conn:
        token_row = await conn.fetchrow(
            """
            SELECT user_id, expires_at, used_at
            FROM password_reset_tokens
            WHERE token = $1
            """,
            body.token,
        )

    if not token_row:
        raise HTTPException(
            status_code=400,
            detail="Invalid or expired reset token.",
        )

    if token_row["used_at"]:
        raise HTTPException(
            status_code=400,
            detail="This reset link has already been used.",
        )

    if token_row["expires_at"] < datetime.now(timezone.utc):
        raise HTTPException(
            status_code=400,
            detail="Reset link has expired. Please request a new one.",
        )

    # Update password
    password_hash_str = password_utils.hash_password(body.new_password)

    async with db_logger.pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE users
            SET password_hash = $1
            WHERE id = $2
            """,
            password_hash_str,
            token_row["user_id"],
        )

        # Mark token as used
        await conn.execute(
            """
            UPDATE password_reset_tokens
            SET used_at = NOW()
            WHERE token = $1
            """,
            body.token,
        )

        # Revoke all existing sessions for security
        await conn.execute(
            """
            UPDATE auth_sessions
            SET revoked = TRUE
            WHERE user_id = $1
            """,
            token_row["user_id"],
        )

    logger.info(f"Password reset completed for user: {token_row['user_id']}")

    return PasswordResetResponse(
        message="Password has been reset successfully. Please login with your new password."
    )


@router.post("/resend-verification", response_model=ResendVerificationResponse)
async def resend_verification(
    request: Request,
    body: ResendVerificationRequest,
    background_tasks: BackgroundTasks,
    db_logger=Depends(get_db_logger),
) -> ResendVerificationResponse:
    """Resend email verification link.

    Sends a new verification email if the user exists and is not verified.
    """
    if not db_logger or not db_logger.pool:
        raise HTTPException(status_code=500, detail="Database not available")

    generic_response = ResendVerificationResponse(
        message="If this email requires verification, a verification email has been sent."
    )

    # Find user
    async with db_logger.pool.acquire() as conn:
        user_row = await conn.fetchrow(
            "SELECT id, email, email_verified FROM users WHERE email = $1",
            body.email.lower(),
        )

    if not user_row:
        return generic_response

    if user_row["email_verified"]:
        return generic_response

    # Generate new verification token
    verification_token = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + timedelta(hours=24)

    async with db_logger.pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO email_verification_tokens (token, user_id, expires_at)
            VALUES ($1, $2, $3)
            """,
            verification_token,
            user_row["id"],
            expires_at,
        )

    # Send email in background so the request returns even if SMTP is slow
    if is_email_enabled():
        base_url = get_base_url(request)
        background_tasks.add_task(
            send_verification_email, user_row["email"], verification_token, base_url
        )
    # If email is not enabled, still return success to avoid leaking state

    logger.info(f"Verification email resent for user: {user_row['id']}")

    return generic_response
