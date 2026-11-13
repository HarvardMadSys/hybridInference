"""Single sign-on through an external identity provider (``/auth/sso/*``).

The browser leaves for the provider and comes back, so two things carry a
sign-in across that trip: a row in ``sso_flows`` found by the hash of the
``state`` value, and a cookie that binds the row to the browser that started
it. Whatever the answer, the callback ends in a redirect to a console page
with at most an error code; nothing the provider said travels in a URL.

The console's access token lives in page memory and travels in the
``Authorization`` header, which a navigation cannot carry. So every write here
that acts for a signed-in account is a ``POST`` with that header; the callback,
a navigation, acts only on what the flow row recorded when it was started.
"""

from __future__ import annotations

import hmac
import secrets
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, urlsplit

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse

from serving.audit import AuditActor, AuditTarget
from serving.auth import oidc, sso_accounts
from serving.auth.browser_sessions import prepare_sign_in
from serving.config import sso as sso_config
from serving.config.settings import get_settings, get_signup_notify_emails
from serving.schemas_auth import (
    SsoDiscoverRequest,
    SsoMessageResponse,
    SsoPendingLinkResponse,
    SsoStartResponse,
)
from serving.servers.auth import log_admin_action
from serving.servers.deps import (
    get_current_user,
    get_db_logger,
    get_operational_store,
    user_audit_actor,
)
from serving.servers.routers.auth_routes import start_user_session
from serving.setup_state import refresh_setup_state
from serving.storage import sso as sso_store
from serving.utils.email import is_email_enabled, send_new_registration_admin_email
from serving.utils.jwt import generate_ulid
from serving.utils.logging import get_logger
from serving.utils.request_ip import get_client_ip, get_client_ip_info
from serving.utils.sso_rate_limit import check_and_record_sso_start

if TYPE_CHECKING:
    from serving.config.sso import SsoProvider

router = APIRouter(prefix="/auth/sso", tags=["Authentication"])
logger = get_logger(__name__)

#: How long a redirect to a provider, and an identity waiting to be linked, stay good.
FLOW_LIFETIME = timedelta(minutes=10)
PENDING_LINK_LIFETIME = timedelta(minutes=10)

#: The path both cookies are scoped to: only these routes ever read them.
_COOKIE_PATH = "/auth/sso"
_DEFAULT_RETURN = "/dashboard"
_LOGIN_PAGE = "/login"


# -- small helpers --------------------------------------------------------------


def _flow_cookie() -> str:
    return f"{get_settings().refresh_token_cookie_name}_sso_flow"


def _link_cookie() -> str:
    return f"{get_settings().refresh_token_cookie_name}_sso_link"


def _set_cookie(response: Response, name: str, value: str, lifetime: timedelta) -> None:
    settings = get_settings()
    # Always Lax, whatever the site-wide setting: the provider sends the browser
    # back in a cross-site navigation, which a Strict cookie would not join.
    response.set_cookie(
        key=name,
        value=value,
        max_age=int(lifetime.total_seconds()),
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        domain=settings.cookie_domain,
        path=_COOKIE_PATH,
    )


def _clear_cookie(response: Response, name: str) -> None:
    settings = get_settings()
    response.delete_cookie(
        key=name,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        domain=settings.cookie_domain,
        path=_COOKIE_PATH,
    )


def internal_path(raw: str | None, fallback: str = _DEFAULT_RETURN) -> str:
    """Return *raw* when it is a path inside the console, else *fallback*.

    The rule the console applies to ``?next=`` (``internalPathOr``): a path,
    with nothing a URL parser would rewrite into another origin. Browsers read
    a backslash as a slash and strip control characters before parsing, so
    both are refused outright rather than modelled.
    """
    if not raw or not raw.startswith("/") or raw.startswith("//"):
        return fallback
    if "\\" in raw or any(ord(char) <= 0x20 for char in raw):
        return fallback
    parts = urlsplit("https://sentinel.invalid" + raw)
    if parts.scheme != "https" or parts.netloc != "sentinel.invalid":
        return fallback
    return raw


def _with_param(path: str, name: str, value: str) -> str:
    base, _, fragment = path.partition("#")
    separator = "&" if "?" in base else "?"
    return f"{base}{separator}{name}={quote(value, safe='')}" + (f"#{fragment}" if fragment else "")


def _redirect(path: str) -> RedirectResponse:
    return RedirectResponse(path, status_code=302)


def _login_error(code: str) -> RedirectResponse:
    return _redirect(_with_param(_LOGIN_PAGE, "sso_error", code))


def _redirect_uri(provider: SsoProvider) -> str | None:
    """The callback address registered at the provider, or None when it cannot be built.

    From ``FRONTEND_URL``, the console's public address: the session cookies
    the callback sets must land on the console's own host, and a ``Host``
    header is not to be trusted with where a provider sends a signed-in user.
    """
    base = (get_settings().frontend_url or "").strip().rstrip("/")
    parts = urlsplit(base)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    return f"{base}/auth/sso/{provider.id}/callback"


def _oidc_settings(provider: SsoProvider) -> oidc.OidcSettings | None:
    secret = sso_config.client_secret(provider)
    if not secret:
        return None
    return oidc.OidcSettings(
        issuer=provider.issuer,
        client_id=provider.client_id,
        client_secret=secret,
        client_auth_method=provider.client_auth_method,
        scopes=tuple(provider.scopes),
    )


class _Unavailable(Exception):
    """Single sign-on at a provider cannot be started; the reason is for the log."""


async def _start_flow(
    op_store: Any,
    response: Response,
    provider: SsoProvider,
    *,
    purpose: str,
    return_to: str,
    login_hint: str | None = None,
    user_id: str | None = None,
    sid: str | None = None,
    reauth_purpose: str | None = None,
) -> str:
    """Record a flow, bind it to this browser, and return the provider's sign-in URL."""
    redirect_uri = _redirect_uri(provider)
    settings = _oidc_settings(provider)
    if redirect_uri is None:
        raise _Unavailable("FRONTEND_URL is not an absolute URL")
    if settings is None:
        raise _Unavailable(f"the client secret of provider {provider.id} is not set")
    state = secrets.token_urlsafe(32)
    binding = secrets.token_urlsafe(32)
    try:
        url, protocol_state = await oidc.begin(
            settings,
            redirect_uri=redirect_uri,
            state=state,
            login_hint=login_hint,
            fresh_login=purpose == "reauth",
        )
    except oidc.SsoProtocolError as exc:
        raise _Unavailable(f"provider {provider.id}: {exc.reason}") from exc
    await op_store.create_sso_flow(
        state_hash=oidc.hash_value(state),
        binding_hash=oidc.hash_value(binding),
        provider_id=provider.id,
        purpose=purpose,
        return_to=return_to,
        protocol_state=protocol_state,
        expires_at=datetime.now(UTC) + FLOW_LIFETIME,
        user_id=user_id,
        sid=sid,
        reauth_purpose=reauth_purpose,
    )
    _set_cookie(response, _flow_cookie(), binding, FLOW_LIFETIME)
    return url


async def _allow_start(request: Request) -> bool:
    return await check_and_record_sso_start(get_client_ip_info(request))


async def _record_login(
    op_store: Any,
    request: Request,
    *,
    provider: SsoProvider,
    email: str | None,
    outcome: str,
    failure_reason: str | None,
    user_id: str | None,
) -> None:
    """Best-effort write to ``login_events``; never breaks a sign-in on failure."""
    try:
        await op_store.record_login_event(
            email=email or f"sso:{provider.id}",
            outcome=outcome,
            failure_reason=failure_reason,
            user_id=user_id,
            ip=get_client_ip(request),
            user_agent=request.headers.get("user-agent"),
        )
    except Exception:
        logger.exception(
            "login_event_write_failed (outcome=%s reason=%s)", outcome, failure_reason or "-"
        )


# -- starting a sign-in -----------------------------------------------------------


@router.post("/discover", response_model=SsoStartResponse)
async def sso_discover(
    request: Request,
    response: Response,
    body: SsoDiscoverRequest,
    op_store=Depends(get_operational_store),
) -> SsoStartResponse:
    """Find the provider that answers for an email's domain and start a sign-in there.

    The sign-in page's single sign-on entry. The email arrives in the body, so
    it reaches no access log, and no account is looked up: the answer is the
    same whether or not the address has an account here.

    Raises:
        HTTPException: 429 when this address started too many sign-ins; 503
            when the provider for the email cannot be reached or is not fully
            configured, or while first-run setup is pending.
    """
    # A first sign-in may create an account, and the first account must be the
    # administrator first-run setup creates: nothing starts before that.
    if await refresh_setup_state():
        raise HTTPException(status_code=503, detail="This deployment has not been set up yet.")
    if not op_store:
        raise HTTPException(status_code=500, detail="Database not available")
    if not await _allow_start(request):
        raise HTTPException(
            status_code=429,
            detail="Too many sign-in attempts. Please try again later.",
            headers={"Retry-After": "3600"},
        )
    provider = sso_config.provider_for_email(body.email)
    if provider is None:
        return SsoStartResponse(available=False)
    try:
        url = await _start_flow(
            op_store,
            response,
            provider,
            purpose="login",
            return_to=internal_path(body.return_to),
            login_hint=str(body.email).lower(),
        )
    except _Unavailable as exc:
        logger.warning(f"Single sign-on unavailable: {exc}")
        raise HTTPException(
            status_code=503, detail="Single sign-on is unavailable right now."
        ) from None
    return SsoStartResponse(authorization_url=url)


@router.get("/{provider_id}/start")
async def sso_start(
    provider_id: str,
    request: Request,
    return_to: str | None = None,
    op_store=Depends(get_operational_store),
) -> RedirectResponse:
    """Go straight to one provider, without asking for an email first.

    A plain link: an organization can put it on its own portal, and the
    account chooser uses it for an account that signs in this way. It is not
    shown on the sign-in page. A provider that is not enabled, or cannot be
    reached, sends the browser to the sign-in page with an error code.
    """
    provider = sso_config.provider_by_id(provider_id)
    if provider is None or not op_store or await refresh_setup_state():
        return _login_error("unavailable")
    if not await _allow_start(request):
        return _login_error("rate_limited")
    redirect = _redirect("/")
    try:
        url = await _start_flow(
            op_store, redirect, provider, purpose="login", return_to=internal_path(return_to)
        )
    except _Unavailable as exc:
        logger.warning(f"Single sign-on unavailable: {exc}")
        return _login_error("unavailable")
    redirect.headers["location"] = url
    return redirect


# -- the provider's answer ---------------------------------------------------------


def _flow_error(flow: dict[str, Any] | None, code: str) -> RedirectResponse:
    """Send the browser back with an error code, to where the flow was started from."""
    if flow is None or flow["purpose"] == "login":
        response = _login_error(code)
    else:
        # Started by a signed-in user from a console page: the sign-in page
        # would only send them on, and the code with it.
        response = _redirect(_with_param(flow["return_to"], "sso_error", code))
    _clear_cookie(response, _flow_cookie())
    return response


@router.get("/{provider_id}/callback")
async def sso_callback(
    provider_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    op_store=Depends(get_operational_store),
    db_logger=Depends(get_db_logger),
) -> RedirectResponse:
    """Receive the browser back from the provider and finish what the flow started.

    Always answers with a redirect to a console page. The flow is claimed
    first, exactly once, and only by the browser that started it at this
    provider; then the code is exchanged and the identity validated; then the
    flow's purpose decides what the identity is used for.
    """
    if not op_store or await refresh_setup_state():
        return _flow_error(None, "unavailable")
    flow = await op_store.consume_sso_flow(oidc.hash_value(state)) if state else None
    binding = request.cookies.get(_flow_cookie())
    if (
        flow is None
        or not binding
        or not hmac.compare_digest(oidc.hash_value(binding), flow["binding_hash"])
        # A flow started at one provider is never completed by another's answer.
        or flow["provider_id"] != provider_id
    ):
        return _flow_error(None, "expired")
    if error:
        # The provider's description of the error is not shown or logged in full.
        logger.info(f"Single sign-on at {provider_id} returned error {error[:64]!r}")
        return _flow_error(flow, "cancelled" if error == "access_denied" else "failed")
    provider = sso_config.provider_by_id(provider_id)
    redirect_uri = _redirect_uri(provider) if provider else None
    settings = _oidc_settings(provider) if provider else None
    if provider is None or redirect_uri is None or settings is None or not code:
        return _flow_error(flow, "unavailable" if code else "failed")
    try:
        identity = await oidc.complete(
            settings,
            redirect_uri=redirect_uri,
            code=code,
            protocol_state=flow["protocol_state"],
        )
    except oidc.SsoProtocolError as exc:
        logger.warning(f"Single sign-on at {provider.id} did not complete: {exc.reason}")
        return _flow_error(flow, exc.code)

    handler = _PURPOSES.get(flow["purpose"])
    if handler is None:
        return _flow_error(flow, "failed")
    response = await handler(
        request=request,
        background_tasks=background_tasks,
        op_store=op_store,
        db_logger=db_logger,
        provider=provider,
        flow=flow,
        identity=identity,
    )
    _clear_cookie(response, _flow_cookie())
    return response


async def _finish_login(
    *,
    request: Request,
    background_tasks: BackgroundTasks,
    op_store: Any,
    db_logger: Any,
    provider: SsoProvider,
    flow: dict[str, Any],
    identity: oidc.ExternalIdentity,
) -> RedirectResponse:
    """Sign the identity in, hold it for linking, or refuse it."""
    resolution = await sso_accounts.resolve_sign_in(op_store, provider, identity)
    client_ip = get_client_ip(request)

    if resolution.created is not None:
        created = resolution.created
        await log_admin_action(
            db_logger,
            action="create_user",
            actor=AuditActor.anonymous(client_ip),
            target=AuditTarget.user(created["id"]),
            details={
                "email": created.get("email"),
                "user_name": created.get("user_name"),
                "status": created.get("status"),
                "requires_approval": created.get("status") == "pending_approval",
                "sso_provider": provider.id,
            },
        )
        if created.get("status") == "pending_approval" and is_email_enabled():
            notify = get_settings().signup_admin_notify_enabled
            try:
                from serving.config.runtime_settings import get_runtime_settings_instance

                notify = await get_runtime_settings_instance().get_bool(
                    "signup_admin_notify_enabled"
                )
            except (RuntimeError, KeyError):
                pass
            if notify:
                for notify_email in get_signup_notify_emails():
                    background_tasks.add_task(
                        send_new_registration_admin_email,
                        to_email=notify_email,
                        user_email=created.get("email") or "",
                        user_name=created.get("user_name"),
                        user_id=created["id"],
                        use_case=f"Single sign-on ({provider.display_name})",
                    )

    if resolution.refused is not None:
        await _record_login(
            op_store,
            request,
            provider=provider,
            email=identity.email,
            outcome="failure",
            failure_reason=f"sso_{resolution.refused}",
            user_id=resolution.created["id"] if resolution.created else None,
        )
        return _login_error(resolution.refused)

    if resolution.pending_link:
        token = secrets.token_urlsafe(32)
        await op_store.create_sso_pending_link(
            token_hash=oidc.hash_value(token),
            provider_id=provider.id,
            issuer=identity.issuer,
            subject=identity.subject,
            email=identity.email or "",
            expires_at=datetime.now(UTC) + PENDING_LINK_LIFETIME,
        )
        response = _redirect(_with_param(_LOGIN_PAGE, "sso", "link"))
        _set_cookie(response, _link_cookie(), token, PENDING_LINK_LIFETIME)
        return response

    user = resolution.user or {}
    try:
        browser = await prepare_sign_in(op_store, request, user["id"])
    except HTTPException:
        await _record_login(
            op_store,
            request,
            provider=provider,
            email=identity.email,
            outcome="failure",
            failure_reason="browser_account_limit",
            user_id=user["id"],
        )
        return _login_error("browser_full")

    await op_store.update_user_last_login(user["id"])
    if resolution.identity_id:
        await op_store.touch_user_identity(resolution.identity_id)
    await _record_login(
        op_store,
        request,
        provider=provider,
        email=identity.email or user.get("email"),
        outcome="success",
        failure_reason=None,
        user_id=user["id"],
    )
    target = _with_param(_LOGIN_PAGE, "sso", "complete")
    response = _redirect(_with_param(target, "next", flow["return_to"]))
    await start_user_session(
        op_store, response, browser, user, user_role=user.get("role") or "free", request=request
    )
    logger.info(f"User signed in through single sign-on: {user['id']} ({provider.id})")
    return response


#: What each flow purpose does with the identity the provider returned.
_PURPOSES = {"login": _finish_login}


# -- linking an identity to an account that already exists -----------------------


def _provider_name(provider_id: str) -> str:
    provider = sso_config.provider_by_id(provider_id)
    return provider.display_name if provider else provider_id


@router.get("/link", response_model=SsoPendingLinkResponse)
async def get_pending_link(
    request: Request,
    op_store=Depends(get_operational_store),
) -> SsoPendingLinkResponse:
    """Describe the identity this browser's last sign-in left waiting to be linked.

    Read from the browser's cookie, so the email never travels in a URL.

    Raises:
        HTTPException: 404 when nothing is waiting, or it expired.
    """
    token = request.cookies.get(_link_cookie())
    pending = (
        await op_store.get_sso_pending_link(oidc.hash_value(token)) if token and op_store else None
    )
    if pending is None:
        raise HTTPException(status_code=404, detail="No single sign-on is waiting to be linked.")
    return SsoPendingLinkResponse(
        provider_id=pending["provider_id"],
        provider_name=_provider_name(pending["provider_id"]),
        email=pending["email"],
    )


@router.post("/link/confirm", response_model=SsoMessageResponse)
async def confirm_pending_link(
    request: Request,
    response: Response,
    current_user: dict[str, Any] = Depends(get_current_user),
    op_store=Depends(get_operational_store),
    db_logger=Depends(get_db_logger),
) -> SsoMessageResponse:
    """Link the waiting identity to the signed-in account, at its owner's request.

    The account must carry the email the identity was matched by: on a shared
    computer, someone signing in to their own account must not be able to
    accept an identity another person left waiting.

    Raises:
        HTTPException: 404 when nothing is waiting or it expired; 409 when the
            signed-in account has another email, already holds an identity
            from the same provider, or the identity was linked elsewhere
            meanwhile; 403 when the account is not active.
    """
    token = request.cookies.get(_link_cookie())
    token_hash = oidc.hash_value(token) if token else None
    pending = await op_store.get_sso_pending_link(token_hash) if token_hash else None
    if pending is None:
        raise HTTPException(status_code=404, detail="No single sign-on is waiting to be linked.")
    if (current_user.get("email") or "").lower() != pending["email"].lower():
        raise HTTPException(
            status_code=409,
            detail="This single sign-on belongs to a different email address than the account "
            "you are signed in to.",
        )
    if await op_store.consume_sso_pending_link(token_hash) is None:
        raise HTTPException(status_code=404, detail="No single sign-on is waiting to be linked.")
    _clear_cookie(response, _link_cookie())
    outcome = await op_store.link_user_identity(
        identity_id=generate_ulid(),
        user_id=current_user["user_id"],
        provider_id=pending["provider_id"],
        issuer=pending["issuer"],
        subject=pending["subject"],
        email=pending["email"],
    )
    _raise_unless_linked(outcome)
    await log_admin_action(
        db_logger,
        action="link_sso_identity",
        details={"sso_provider": pending["provider_id"], "email": pending["email"]},
        actor=user_audit_actor(request, current_user),
        target=AuditTarget.user(current_user["user_id"]),
    )
    return SsoMessageResponse(message="Single sign-on connected.")


@router.post("/link/cancel", response_model=SsoMessageResponse)
async def cancel_pending_link(
    request: Request,
    response: Response,
    op_store=Depends(get_operational_store),
) -> SsoMessageResponse:
    """Drop the identity this browser left waiting; nothing is linked."""
    token = request.cookies.get(_link_cookie())
    if token and op_store:
        await op_store.delete_sso_pending_link(oidc.hash_value(token))
    _clear_cookie(response, _link_cookie())
    return SsoMessageResponse(message="Single sign-on was not connected.")


def _raise_unless_linked(outcome: str) -> None:
    if outcome == sso_store.LINKED:
        return
    if outcome == sso_store.ACCOUNT_INACTIVE:
        raise HTTPException(status_code=403, detail="Account is not active.")
    if outcome == sso_store.ISSUER_CONFLICT:
        raise HTTPException(
            status_code=409,
            detail="This account is already connected to a different identity from the same "
            "organization. Ask an administrator to remove it first.",
        )
    raise HTTPException(
        status_code=409,
        detail="This single sign-on is already connected to another account.",
    )
