"""Authentication routes for user signup, login, logout, and email verification."""

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, BackgroundTasks, Cookie, Depends, HTTPException, Request, Response

from serving.auth.signup_policy import allowlist_is_empty, is_domain_allowed
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
from serving.servers.deps import get_current_user, get_db_logger, get_operational_store
from serving.utils import password as password_utils
from serving.utils.email import (
    is_email_enabled,
    send_new_registration_admin_email,
    send_verification_email,
)
from serving.utils.email_blocklist import is_email_domain_blocked
from serving.utils.jwt import (
    create_access_token,
    create_refresh_token,
    generate_session_id,
    generate_ulid,
    get_access_token_expire_minutes,
    get_refresh_token_expire_days,
)
from serving.utils.logging import get_logger
from serving.utils.login_rate_limit import check_and_record_login
from serving.utils.request_ip import get_client_ip
from serving.utils.signup_rate_limit import check_and_record_signup
from serving.utils.turnstile import verify_turnstile_token

router = APIRouter(prefix="/auth", tags=["Authentication"])
logger = get_logger(__name__)
REFRESH_TOKEN_COOKIE = "refresh_token"


def get_base_url(request: Request) -> str:
    """Return the configured ``BASE_URL`` or fall back to the request scheme+host."""
    base_url = settings.base_url
    if base_url:
        return base_url.rstrip("/")
    return f"{request.url.scheme}://{request.url.netloc}"


def hash_refresh_token(token: str) -> str:
    """Hash a refresh token with SHA-256 for storage in the sessions table."""
    return hashlib.sha256(token.encode()).hexdigest()


def _refresh_cookie_options() -> dict[str, object]:
    """Return shared options for refresh-token cookie operations.

    Reads from validated `settings` (not raw env) so tests and Pydantic
    field defaults are the single source of truth. Defaults: secure=True,
    samesite=lax, domain unset.
    """
    return {
        "httponly": True,
        "secure": settings.cookie_secure,
        "samesite": settings.cookie_samesite,
        "domain": settings.cookie_domain,
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
    op_store=Depends(get_operational_store),
    db_logger=Depends(get_db_logger),
) -> SignupResponse:
    """Register a new user account.

    Creates a new user with email and password. Sends verification email if SMTP is configured.
    User must verify email before they can generate an API key.

    Per-IP signup rate limits are configurable via
    settings.signup_rate_limit_per_hour and signup_rate_limit_per_day.
    """
    # Check if signup is enabled
    if not settings.signup_enabled:
        raise HTTPException(
            status_code=403,
            detail="Public signup is currently disabled. Please contact administrator.",
        )

    # Record on entry so probing with varied payloads cannot bypass the limit.
    client_ip = get_client_ip(request)
    allowed, reason = await check_and_record_signup(client_ip)
    if not allowed:
        retry_after = "3600" if reason == "hour" else "86400"
        raise HTTPException(
            status_code=429,
            detail="Too many signup attempts. Please try again later.",
            headers={"Retry-After": retry_after},
        )

    if not await verify_turnstile_token(body.turnstile_token, client_ip):
        raise HTTPException(status_code=400, detail="Captcha verification failed")

    if is_email_domain_blocked(body.email):
        raise HTTPException(status_code=400, detail="This email domain is not allowed")

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
        raise HTTPException(status_code=409, detail="Registration failed. Please try again.")

    # Determine initial status from the admin-editable signup domain
    # allowlist (replaces the legacy SIGNUP_REQUIRE_APPROVAL env var).
    # Empty allowlist = all signups auto-approve. Otherwise, only emails
    # whose domain is on the allowlist (exact or wildcard suffix) auto-
    # approve; everyone else lands in pending_approval.
    require_verification = settings.signup_require_email_verification
    if await allowlist_is_empty(op_store) or await is_domain_allowed(body.email, op_store):
        initial_status = "active"
    else:
        initial_status = "pending_approval"
    require_approval = initial_status == "pending_approval"

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

    # Send verification email only when verification is required and SMTP is configured.
    # Skipping when SIGNUP_REQUIRE_EMAIL_VERIFICATION=false avoids burning SMTP quota.
    if require_verification and is_email_enabled():
        verification_token = secrets.token_urlsafe(32)
        expires_at = datetime.now(timezone.utc) + timedelta(hours=24)
        await op_store.create_verification_token(
            token=verification_token, user_id=user_id, expires_at=expires_at
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
        client_ip,
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
    elif require_verification:
        message = "Account created successfully. Please check your email to verify your account."
    else:
        message = "Account created successfully. You can now log in."

    return SignupResponse(
        message=message,
        email=body.email,
        user_id=user_id,
        requires_approval=require_approval,
    )


@router.post("/login", response_model=LoginResponse)
async def login(
    request: Request,
    response: Response,
    body: LoginRequest,
    op_store=Depends(get_operational_store),
) -> LoginResponse:
    """Login with email and password.

    Returns access token (15 min) and sets refresh token as HttpOnly cookie
    (30 days by default).

    Rate limits (configurable via settings.login_rate_limit_per_15min and
    settings.login_rate_limit_per_hour_per_ip):
    - 5 attempts per 15 minutes per email
    - 20 attempts per hour per IP
    """
    if not op_store:
        raise HTTPException(status_code=500, detail="Database not available")

    # Record on entry so probing varied passwords cannot bypass the limit.
    client_ip = get_client_ip(request)
    allowed, reason = await check_and_record_login(body.email, client_ip)
    if not allowed:
        retry_after = "3600" if reason == "ip" else "900"
        raise HTTPException(
            status_code=429,
            detail="Too many login attempts. Please try again later.",
            headers={"Retry-After": retry_after},
        )

    # Find user by email
    user_row = await op_store.get_user_by_email(body.email)

    if not user_row:
        raise HTTPException(status_code=401, detail="Invalid email or password")

    # Verify password
    if not password_utils.verify_password(body.password, user_row["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    # Check if email verification is required and if email is verified
    require_verification = settings.signup_require_email_verification
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

    # Update last login timestamp
    await op_store.update_user_last_login(user_row["id"])

    # Create session and tokens
    session_id = generate_session_id()
    user_role = user_row["role"] or "free"

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

    delete_refresh_token_cookie(response)

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

    user_row = await op_store.get_user_by_id(session_row["user_id"])

    if not user_row or user_row["status"] != "active":
        raise HTTPException(
            status_code=401,
            detail="User account is not active.",
        )

    # Check if email verification is required and if email is verified
    require_verification = settings.signup_require_email_verification
    if require_verification and not user_row["email_verified"]:
        raise HTTPException(
            status_code=403,
            detail="Email not verified. Please verify your email to continue.",
        )

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
    background_tasks: BackgroundTasks,
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
    background_tasks: BackgroundTasks,
    op_store=Depends(get_operational_store),
) -> ResendVerificationResponse:
    """Resend email verification link.

    Sends a new verification email if the user exists and is not verified.
    """
    if not op_store:
        raise HTTPException(status_code=500, detail="Database not available")

    generic_response = ResendVerificationResponse(
        message="If this email requires verification, a verification email has been sent."
    )

    user_row = await op_store.get_user_by_email(body.email)

    if not user_row:
        return generic_response

    if user_row["email_verified"]:
        return generic_response

    # Generate new verification token
    verification_token = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + timedelta(hours=24)
    await op_store.create_verification_token(
        token=verification_token, user_id=user_row["id"], expires_at=expires_at
    )

    # Send email in background so the request returns even if SMTP is slow
    if is_email_enabled():
        base_url = get_base_url(request)
        background_tasks.add_task(
            send_verification_email, user_row["email"], verification_token, base_url
        )

    logger.info(f"Verification email resent for user: {user_row['id']}")

    return generic_response
