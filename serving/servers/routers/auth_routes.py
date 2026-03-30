"""Authentication routes for user signup, login, logout, and email verification."""

import hashlib
import os
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response

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
from serving.servers.deps import get_current_user, get_operational_store
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
    op_store=Depends(get_operational_store),
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
        raise HTTPException(
            status_code=400, detail=error_msg or "Password does not meet security requirements"
        )

    if not op_store:
        raise HTTPException(status_code=500, detail="Database not available")

    # Check if email already exists
    existing_user = await op_store.get_user_by_email(body.email)
    if existing_user:
        raise HTTPException(status_code=409, detail=f"Email {body.email} already registered")

    # Determine initial status based on approval setting
    require_approval = os.getenv("SIGNUP_REQUIRE_APPROVAL", "0") == "1"
    initial_status = "pending_approval" if require_approval else "active"

    # Create user
    user_id = generate_ulid()
    password_hash_str = password_utils.hash_password(body.password)

    await op_store.create_user(
        user_id=user_id,
        email=body.email,
        password_hash=password_hash_str,
        user_name=body.user_name,
        email_verified=False,
        status=initial_status,
    )

    # Send verification email if SMTP is configured
    if is_email_enabled():
        verification_token = secrets.token_urlsafe(32)
        expires_at = datetime.now(timezone.utc) + timedelta(hours=24)
        await op_store.create_verification_token(
            token=verification_token, user_id=user_id, expires_at=expires_at
        )

        base_url = get_base_url(request)
        email_sent = send_verification_email(body.email, verification_token, base_url)
        if not email_sent:
            logger.warning(f"Failed to send verification email to {body.email}")

    # Notify admins of new registration when approval is required
    if require_approval and is_email_enabled():
        admin_emails = [e.strip() for e in settings.admin_emails.split(",") if e.strip()]
        for admin_email in admin_emails:
            send_new_registration_admin_email(
                to_email=admin_email,
                user_email=body.email,
                user_name=body.user_name,
                user_id=user_id,
            )

    logger.info(f"New user registered: {user_id} ({body.email}) [status={initial_status}]")

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
    op_store=Depends(get_operational_store),
) -> LoginResponse:
    """Login with email and password.

    Returns access token (15 min) and sets refresh token as HttpOnly cookie (30 days).

    Rate limits:
    - 5 attempts per 15 minutes per email
    - 20 attempts per hour per IP
    """
    if not op_store:
        raise HTTPException(status_code=500, detail="Database not available")

    # Find user by email
    user_row = await op_store.get_user_by_email(body.email)

    if not user_row:
        raise HTTPException(status_code=401, detail="Invalid email or password")

    # Verify password
    if not password_utils.verify_password(body.password, user_row["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid email or password")

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
    await op_store.update_user_last_login(user_row["id"])
    key_row = await op_store.get_key_by_account_or_user(user_row["id"])

    # Create session and tokens
    session_id = generate_session_id()
    user_role = user_row["role"] or "free"
    user_tier = (key_row["tier"] if key_row else None) or "free"

    # Bootstrap seed: promote ADMIN_EMAILS users to admin when their role is
    # still at the default 'free'.
    if is_admin_email(user_row["email"]) and user_role == "free":
        user_role = "admin"
        await op_store.update_user_fields(user_row["id"], role="admin")
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
    await op_store.create_session(
        session_id=generate_ulid(),
        user_id=user_row["id"],
        refresh_token_hash=refresh_token_hash_str,
        jti=jti,
        sid=session_id,
        expires_at=session_expires,
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
    op_store=Depends(get_operational_store),
) -> LogoutResponse:
    """Logout current user.

    Revokes the refresh token session and clears the cookie.
    Access tokens will expire naturally (15 minutes).
    """
    if not op_store:
        raise HTTPException(status_code=500, detail="Database not available")

    # Revoke refresh token session if provided
    if refresh_token:
        refresh_token_hash_str = hash_refresh_token(refresh_token)
        session_row = await op_store.get_session_by_token_hash(refresh_token_hash_str)
        if session_row and session_row["user_id"] == current_user["user_id"]:
            await op_store.revoke_session(session_row["id"])

    # Clear refresh token cookie
    response.delete_cookie(key="refresh_token", path="/")

    logger.info(f"User logged out: {current_user['user_id']}")

    return LogoutResponse(message="Logged out successfully")


@router.post("/refresh", response_model=RefreshResponse)
async def refresh(
    response: Response,
    refresh_token: str | None = Cookie(None),
    op_store=Depends(get_operational_store),
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

    if not op_store:
        raise HTTPException(status_code=500, detail="Database not available")

    # Verify refresh token
    refresh_token_hash_str = hash_refresh_token(refresh_token)
    session_row = await op_store.get_session_by_token_hash(refresh_token_hash_str)

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
    user_row = await op_store.get_user_by_id(session_row["user_id"])

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
    key_row = await op_store.get_key_by_account_or_user(user_row["id"])
    user_tier = (key_row["tier"] if key_row else None) or "free"

    # Create new access token
    user_role = user_row["role"] or "free"

    # Bootstrap seed on refresh (same logic as login — only when role is 'free')
    if is_admin_email(user_row["email"]) and user_role == "free":
        user_role = "admin"
        await op_store.update_user_fields(user_row["id"], role="admin")
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
    await op_store.rotate_session(
        session_row["id"],
        new_refresh_token_hash=new_refresh_token_hash,
        new_jti=jti,
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
    op_store=Depends(get_operational_store),
) -> VerifyEmailResponse:
    """Verify user email address using token from email.

    Marks user's email as verified, allowing them to generate API keys.
    """
    if not op_store:
        raise HTTPException(status_code=500, detail="Database not available")

    token_row = await op_store.get_verification_token(token)

    if not token_row:
        raise HTTPException(status_code=400, detail="Invalid verification token.")

    if token_row["used_at"]:
        raise HTTPException(status_code=400, detail="Verification token has already been used.")

    if token_row["expires_at"] < datetime.now(timezone.utc):
        raise HTTPException(
            status_code=400,
            detail="Verification token has expired. Please request a new one.",
        )

    # Mark email as verified and token as used
    await op_store.mark_user_email_verified(token_row["user_id"])
    await op_store.mark_verification_used(token)

    logger.info(f"Email verified for user: {token_row['user_id']}")

    return VerifyEmailResponse(
        message="Email verified successfully. You can now generate your API key.",
        email_verified=True,
    )


@router.post("/forgot-password", response_model=PasswordResetResponse)
async def forgot_password(
    request: Request,
    body: ForgotPasswordRequest,
    op_store=Depends(get_operational_store),
) -> PasswordResetResponse:
    """Request password reset email.

    Sends a password reset link to the user's email if the account exists.
    Always returns success to prevent email enumeration.
    """
    if not op_store:
        raise HTTPException(status_code=500, detail="Database not available")

    user_row = await op_store.get_user_by_email(body.email)

    # Always return success to prevent email enumeration
    if not user_row:
        logger.info(f"Password reset requested for non-existent email: {body.email}")
        return PasswordResetResponse(
            message="If an account exists with this email, a password reset link has been sent."
        )

    # Generate reset token
    reset_token = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
    await op_store.create_reset_token(
        token=reset_token, user_id=user_row["id"], expires_at=expires_at
    )

    # Send reset email if SMTP is configured
    if is_email_enabled():
        from serving.utils.email import send_password_reset_email

        base_url = get_base_url(request)
        email_sent = send_password_reset_email(user_row["email"], reset_token, base_url)
        if not email_sent:
            logger.warning(f"Failed to send password reset email to {user_row['email']}")

    logger.info(f"Password reset requested for user: {user_row['id']}")

    return PasswordResetResponse(
        message="If an account exists with this email, a password reset link has been sent."
    )


@router.post("/reset-password", response_model=PasswordResetResponse)
async def reset_password(
    body: ResetPasswordRequest,
    op_store=Depends(get_operational_store),
) -> PasswordResetResponse:
    """Reset password using token from email.

    Validates the reset token and updates the user's password.
    """
    if not op_store:
        raise HTTPException(status_code=500, detail="Database not available")

    # Validate password strength
    is_valid, error_msg = password_utils.validate_password_strength(body.new_password)
    if not is_valid:
        raise HTTPException(status_code=400, detail=error_msg)

    token_row = await op_store.get_reset_token(body.token)

    if not token_row:
        raise HTTPException(status_code=400, detail="Invalid or expired reset token.")

    if token_row["used_at"]:
        raise HTTPException(status_code=400, detail="This reset link has already been used.")

    if token_row["expires_at"] < datetime.now(timezone.utc):
        raise HTTPException(
            status_code=400, detail="Reset link has expired. Please request a new one."
        )

    # Update password
    password_hash_str = password_utils.hash_password(body.new_password)
    await op_store.update_user_fields(token_row["user_id"], password_hash=password_hash_str)

    # Mark token as used
    await op_store.mark_reset_used(body.token)

    # Revoke all existing sessions for security
    await op_store.delete_user_sessions(token_row["user_id"])

    logger.info(f"Password reset completed for user: {token_row['user_id']}")

    return PasswordResetResponse(
        message="Password has been reset successfully. Please login with your new password."
    )


@router.post("/resend-verification", response_model=ResendVerificationResponse)
async def resend_verification(
    request: Request,
    body: ResendVerificationRequest,
    op_store=Depends(get_operational_store),
) -> ResendVerificationResponse:
    """Resend email verification link.

    Sends a new verification email if the user exists and is not verified.
    """
    if not op_store:
        raise HTTPException(status_code=500, detail="Database not available")

    user_row = await op_store.get_user_by_email(body.email)

    if not user_row:
        raise HTTPException(status_code=404, detail="No account found with this email.")

    if user_row["email_verified"]:
        raise HTTPException(status_code=400, detail="Email is already verified.")

    # Generate new verification token
    verification_token = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + timedelta(hours=24)
    await op_store.create_verification_token(
        token=verification_token, user_id=user_row["id"], expires_at=expires_at
    )

    # Send email
    if is_email_enabled():
        base_url = get_base_url(request)
        email_sent = send_verification_email(user_row["email"], verification_token, base_url)
        if not email_sent:
            logger.warning(f"Failed to resend verification email to {user_row['email']}")

    logger.info(f"Verification email resent for user: {user_row['id']}")

    return ResendVerificationResponse(
        message="Verification email has been sent. Please check your inbox."
    )
