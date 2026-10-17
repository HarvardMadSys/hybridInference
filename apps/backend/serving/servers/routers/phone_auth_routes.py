"""Optional phone registration, login, and explicit account binding."""

import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response
from starlette.concurrency import run_in_threadpool

from serving.auth.browser_sessions import complete_sign_in, hash_refresh_token, prepare_sign_in
from serving.auth.signup_policy import allowlist_is_empty, is_public_signup_enabled
from serving.config.settings import get_signup_notify_emails, settings
from serving.exceptions import AccountSuspendedError
from serving.schemas_auth import (
    LoginResponse,
    PhoneCodeRequest,
    PhoneCodeResponse,
    PhoneSignupRequest,
    PhoneSignupResponse,
    PhoneVerifyRequest,
    UserInfo,
)
from serving.servers.auth import log_admin_action
from serving.servers.deps import get_current_user, get_db_logger, get_operational_store
from serving.storage.phone_auth import OWNER_PURPOSES
from serving.utils.email import is_email_enabled, send_new_registration_admin_email
from serving.utils.jwt import (
    create_access_token,
    create_refresh_token,
    generate_session_id,
    generate_ulid,
    get_access_token_expire_minutes,
    get_refresh_token_expire_days,
)
from serving.utils.logging import get_logger
from serving.utils.phone_auth import hash_phone_code, phone_identity_unverified
from serving.utils.request_ip import get_client_ip
from serving.utils.signup_rate_limit import check_and_record_signup
from serving.utils.sms import is_sms_enabled, send_verification_sms
from serving.utils.turnstile import verify_turnstile_token

router = APIRouter(prefix="/phone")
logger = get_logger(__name__)


def _require_enabled(op_store) -> None:
    if not is_sms_enabled():
        raise HTTPException(503, "Phone authentication is currently unavailable.")
    if not op_store:
        raise HTTPException(503, "Database not available")


async def _require_signup() -> None:
    if not await is_public_signup_enabled():
        raise HTTPException(
            403, "Public signup is currently disabled. Please contact administrator."
        )


async def verify_phone_code(
    request: Request, body: PhoneVerifyRequest, purpose: str, op_store, user_id=None
) -> None:
    """Spend one code for *purpose*, raising 429 or 400 when it does not verify."""
    result = await op_store.consume_phone_code(
        phone=body.phone,
        purpose=purpose,
        user_id=user_id,
        code_hash=hash_phone_code(body.phone, purpose, user_id, body.code),
        ip=get_client_ip(request),
        max_attempts=settings.sms_otp_max_attempts,
        phone_limit=settings.sms_otp_verify_per_phone_15min,
        ip_limit=settings.sms_otp_verify_per_ip_hour,
    )
    if result == "rate_limited":
        raise HTTPException(
            429,
            "Too many verification attempts. Please try again later.",
            headers={"Retry-After": "3600"},
        )
    if result != "ok":
        raise HTTPException(400, "Invalid or expired verification code.")


@router.post("/code", response_model=PhoneCodeResponse)
async def send_phone_code(
    request: Request, body: PhoneCodeRequest, op_store=Depends(get_operational_store)
) -> PhoneCodeResponse:
    """Send a purpose-bound code after policy, captcha, and durable quota checks."""
    _require_enabled(op_store)
    user_id = None
    if body.purpose == "signup":
        await _require_signup()
    elif body.purpose == "bind" or body.purpose in OWNER_PURPOSES:
        current_user = await get_current_user(request.headers.get("authorization"), op_store)
        user_id = current_user["user_id"]
    client_ip = get_client_ip(request)
    if not await verify_turnstile_token(body.turnstile_token, client_ip):
        raise HTTPException(400, "Captcha verification failed")

    existing_user = await op_store.get_user_by_phone(body.phone)
    if body.purpose == "login" and existing_user:
        user_id = existing_user["id"]
    code = f"{secrets.randbelow(1_000_000):06d}"
    challenge_id = generate_ulid()
    retry_after = await op_store.reserve_phone_code(
        phone=body.phone,
        purpose=body.purpose,
        user_id=user_id,
        challenge_id=challenge_id,
        code_hash=hash_phone_code(body.phone, body.purpose, user_id, code),
        ip=client_ip,
        limits={
            "cooldown": settings.sms_otp_send_cooldown_seconds,
            "expire": settings.sms_otp_expire_seconds,
            "phone_hour": settings.sms_otp_send_per_phone_hour,
            "phone_day": settings.sms_otp_send_per_phone_day,
            "ip_hour": settings.sms_otp_send_per_ip_hour,
            "ip_day": settings.sms_otp_send_per_ip_day,
        },
    )
    if retry_after:
        raise HTTPException(
            429,
            "Too many code requests. Please try again later.",
            headers={"Retry-After": str(retry_after)},
        )

    eligible = True
    if body.purpose == "login":
        eligible = bool(existing_user)
    elif body.purpose == "signup":
        eligible = existing_user is None
    elif body.purpose in OWNER_PURPOSES:
        # Only the number verified on this very account can confirm its owner.
        eligible = bool(
            existing_user and existing_user["id"] == user_id and existing_user.get("phone_verified")
        )
    elif existing_user and existing_user["id"] != user_id:
        eligible = False
    if eligible:
        try:
            sent = await run_in_threadpool(send_verification_sms, body.phone, code)
        except Exception:
            # The provider may expose credentials or OTP values in exceptions.
            sent = False
        if not sent:
            await op_store.invalidate_phone_code(challenge_id)
            raise HTTPException(503, "Could not send verification code. Please try again later.")
        if not await op_store.activate_phone_code(challenge_id):
            raise HTTPException(503, "Verification code expired. Please request another code.")
    else:
        await op_store.invalidate_phone_code(challenge_id)
    return PhoneCodeResponse(
        message="If this phone number is eligible, a verification code has been sent.",
        retry_after=settings.sms_otp_send_cooldown_seconds,
    )


@router.post("/signup", response_model=PhoneSignupResponse, status_code=201)
async def phone_signup(
    request: Request,
    body: PhoneSignupRequest,
    background_tasks: BackgroundTasks,
    op_store=Depends(get_operational_store),
    db_logger=Depends(get_db_logger),
) -> PhoneSignupResponse:
    """Create a verified phone identity while respecting registration approval policy."""
    _require_enabled(op_store)
    await _require_signup()
    client_ip = get_client_ip(request)
    allowed, reason = await check_and_record_signup(client_ip)
    if not allowed:
        raise HTTPException(
            429,
            "Too many signup attempts. Please try again later.",
            headers={"Retry-After": "3600" if reason == "hour" else "86400"},
        )
    if not body.accepted_tos:
        raise HTTPException(400, "You must agree to the Terms of Service to create an account")
    await verify_phone_code(request, body, "signup", op_store)
    status = "active" if await allowlist_is_empty(op_store) else "pending_approval"
    user_id = generate_ulid()
    created = await op_store.create_phone_user(
        user_id=user_id,
        phone=body.phone,
        user_name=body.user_name,
        status=status,
        signup_reason=(body.use_case or "").strip() or None,
    )
    if not created:
        raise HTTPException(409, "Registration failed. Please try again.")
    notify_admins = settings.signup_admin_notify_enabled
    try:
        from serving.config.runtime_settings import get_runtime_settings_instance

        notify_admins = await get_runtime_settings_instance().get_bool(
            "signup_admin_notify_enabled"
        )
    except (RuntimeError, KeyError):
        pass
    if status == "pending_approval" and notify_admins and is_email_enabled():
        for notify_email in get_signup_notify_emails():
            background_tasks.add_task(
                send_new_registration_admin_email,
                to_email=notify_email,
                user_email=body.phone,
                user_name=body.user_name,
                user_id=user_id,
                use_case=body.use_case,
            )
    await log_admin_action(
        db_logger,
        client_ip,
        "create_user",
        user_id,
        {
            "phone": body.phone,
            "user_name": body.user_name,
            "status": status,
            "requires_approval": status == "pending_approval",
            "use_case": body.use_case or None,
        },
    )
    return PhoneSignupResponse(
        message="Account created successfully. Your registration is pending admin approval."
        if status == "pending_approval"
        else "Account created successfully. You can now log in.",
        user_id=user_id,
        phone=body.phone,
        requires_approval=status == "pending_approval",
    )


@router.post("/login", response_model=LoginResponse)
async def phone_login(
    request: Request,
    response: Response,
    body: PhoneVerifyRequest,
    op_store=Depends(get_operational_store),
) -> LoginResponse:
    """Consume a login code and add the account to the browser's session."""
    _require_enabled(op_store)
    user = await op_store.get_user_by_phone(body.phone)

    async def record(outcome: str, reason: str | None):
        try:
            await op_store.record_phone_login_event(
                phone=body.phone,
                email=user.get("email") if user else None,
                outcome=outcome,
                failure_reason=reason,
                user_id=user["id"] if user else None,
                ip=get_client_ip(request),
                user_agent=request.headers.get("user-agent"),
            )
        except Exception:
            logger.warning("Phone login event could not be recorded (outcome=%s).", outcome)

    try:
        await verify_phone_code(request, body, "login", op_store, user["id"] if user else None)
    except HTTPException as exc:
        await record("failure", "rate_limited" if exc.status_code == 429 else "invalid_code")
        raise
    if not user:
        await record("failure", "user_not_found")
        raise HTTPException(401, "Invalid phone number or verification code.")
    if user["status"] != "active":
        await record(
            "failure",
            "account_pending_approval"
            if user["status"] == "pending_approval"
            else "account_inactive",
        )
        if user["status"] == "suspended":
            raise AccountSuspendedError(user["status"], user.get("suspension_message"))
        if user["status"] == "pending_approval":
            raise HTTPException(403, "Your registration is pending admin approval.")
        raise HTTPException(403, "Account is not active. Please contact support.")
    require_verification = settings.signup_require_email_verification
    try:
        from serving.config.runtime_settings import get_runtime_settings_instance

        require_verification = await get_runtime_settings_instance().get_bool(
            "signup_require_email_verification"
        )
    except (RuntimeError, KeyError):
        pass
    if (
        require_verification and user.get("email") and not user.get("email_verified")
    ) or phone_identity_unverified(user):
        await record("failure", "identity_unverified")
        raise HTTPException(403, "Please verify your account's email before logging in.")
    if not user.get("phone_verified"):
        await record("failure", "phone_unverified")
        raise HTTPException(403, "Phone number is not verified.")
    try:
        browser = await prepare_sign_in(op_store, request, user["id"])
    except HTTPException:
        await record("failure", "browser_account_limit")
        raise
    user_role = user.get("role") or "free"
    sid = generate_session_id()
    token, jti = create_access_token(
        user_id=user["id"],
        email=user.get("email"),
        session_id=sid,
        is_admin=user_role == "admin",
        role=user_role,
    )
    auth_session_id = generate_ulid()
    created = await op_store.create_phone_session(
        session_id=auth_session_id,
        user_id=user["id"],
        phone=body.phone,
        ip=get_client_ip(request),
        user_agent=request.headers.get("user-agent"),
        # The browser's cookie names its browser session, never this one.
        refresh_token_hash=hash_refresh_token(create_refresh_token()),
        jti=jti,
        sid=sid,
        expires_at=datetime.now(timezone.utc) + timedelta(days=get_refresh_token_expire_days()),
    )
    if not created:
        await record("failure", "account_inactive")
        raise HTTPException(403, "Account is not active. Please contact support.")
    await complete_sign_in(
        op_store, response, browser, user_id=user["id"], auth_session_id=auth_session_id
    )
    return LoginResponse(
        access_token=token,
        expires_in=get_access_token_expire_minutes() * 60,
        user=UserInfo(
            id=user["id"],
            email=user.get("email"),
            login_name=user.get("login_name"),
            phone=user.get("phone"),
            phone_verified=user.get("phone_verified", False),
            has_password=bool(user.get("password_hash")),
            user_name=user.get("user_name"),
            role=user_role,
            status=user["status"],
            email_verified=user.get("email_verified", False),
            is_admin=user_role == "admin",
            created_at=user["created_at"],
            last_login_at=datetime.now(timezone.utc),
        ),
    )


@router.post("/bind")
async def bind_phone(
    request: Request,
    body: PhoneVerifyRequest,
    current_user=Depends(get_current_user),
    op_store=Depends(get_operational_store),
    db_logger=Depends(get_db_logger),
) -> dict[str, str]:
    """Bind a phone explicitly to the authenticated account without merging users."""
    _require_enabled(op_store)
    await verify_phone_code(request, body, "bind", op_store, current_user["user_id"])
    if not await op_store.bind_user_phone(current_user["user_id"], body.phone):
        raise HTTPException(409, "Phone number cannot be bound to this account.")
    await log_admin_action(
        db_logger,
        get_client_ip(request),
        "bind_phone",
        current_user["user_id"],
        {"phone": body.phone},
    )
    return {"message": "Phone number bound successfully."}
