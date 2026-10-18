"""One browser, several signed-in accounts.

The console keeps one HttpOnly cookie per browser. It used to be a single
account's refresh token; it now names a *browser session*, which lists every
account signed in on that browser and which of them is active. Each account is
still signed in through its own ``auth_sessions`` row, so it expires, is revoked
by a password reset and is removed with its user exactly as a single sign-in
always was. The cookie keeps its name and its attributes, so the console's
proxies, which strip it before relaying a request, need no change.

The active account is the browser's, not a tab's: a refresh always answers for
it, and a tab showing another account learns it has changed from the token it
gets back (see ``apps/frontend/src/lib/api/client.ts``).

A cookie that still holds one account's refresh token, issued before browser
sessions existed, is upgraded the first time it is presented: a browser session
is created around that account's session, and the old value is retired.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException, Request, Response

from serving.agent_access import AgentAccessPolicyUnavailable, resolve_agent_access_permissions
from serving.config.settings import get_settings, is_admin_email
from serving.schemas_auth import AccountSummary, UserInfo
from serving.utils.jwt import (
    create_access_token,
    create_refresh_token,
    generate_ulid,
    get_refresh_token_expire_days,
)
from serving.utils.logging import get_logger
from serving.utils.phone_auth import phone_identity_unverified

logger = get_logger(__name__)

#: How many accounts one browser may hold at once, as Instagram and X allow.
MAX_ACCOUNTS_PER_BROWSER = 5


def hash_refresh_token(token: str) -> str:
    """Hash a cookie value with SHA-256 for storage; the raw value is never kept."""
    return hashlib.sha256(token.encode()).hexdigest()


def get_refresh_token_cookie(request: Request) -> str | None:
    """Read the deployment-scoped session cookie from an incoming request."""
    return request.cookies.get(get_settings().refresh_token_cookie_name)


def _refresh_cookie_options() -> dict[str, object]:
    """Return shared options for session-cookie operations.

    Reads from validated `settings` (not raw env) so tests and Pydantic
    field defaults are the single source of truth. Defaults: secure=True,
    samesite=lax, domain unset.
    """
    settings = get_settings()
    return {
        "httponly": True,
        "secure": settings.cookie_secure,
        "samesite": settings.cookie_samesite,
        "domain": settings.cookie_domain,
        "path": "/",
    }


def set_refresh_token_cookie(response: Response, refresh_token: str) -> None:
    """Set the persistent session cookie."""
    refresh_token_max_age = get_refresh_token_expire_days() * 24 * 60 * 60
    response.set_cookie(
        key=get_settings().refresh_token_cookie_name,
        value=refresh_token,
        max_age=refresh_token_max_age,
        expires=datetime.now(timezone.utc) + timedelta(seconds=refresh_token_max_age),
        **_refresh_cookie_options(),
    )


def delete_refresh_token_cookie(response: Response) -> None:
    """Delete the session cookie using the same domain/path settings."""
    response.delete_cookie(
        key=get_settings().refresh_token_cookie_name,
        **_refresh_cookie_options(),
    )


async def email_verification_required() -> bool:
    """Whether an account must have verified its email to stay signed in."""
    require_verification = get_settings().signup_require_email_verification
    try:
        from serving.config.runtime_settings import get_runtime_settings_instance

        rs = get_runtime_settings_instance()
        require_verification = await rs.get_bool("signup_require_email_verification")
    except (RuntimeError, KeyError):
        pass
    return require_verification


def identity_unverified(account: dict[str, Any], *, require_verification: bool) -> bool:
    """Whether an account has an address it still has to prove before signing in."""
    return bool(
        require_verification and account.get("email") and not account.get("email_verified")
    ) or phone_identity_unverified(account)


def session_live(account: dict[str, Any], *, now: datetime | None = None) -> bool:
    """Whether the account's own session in this browser can still be used.

    False once the session row is revoked, expired or gone (a password reset
    deletes them), and once the user is no longer active.
    """
    now = now or datetime.now(timezone.utc)
    expires_at = account.get("session_expires_at")
    return bool(
        account.get("sid")
        and not account.get("session_revoked")
        and expires_at is not None
        and expires_at > now
        and account.get("status") == "active"
    )


@dataclass
class BrowserSession:
    """A browser's session row and the accounts signed in on it."""

    id: str
    secret_hash: str
    active_user_id: str | None
    accounts: list[dict[str, Any]] = field(default_factory=list)
    # A cookie value this response has to deliver: set when a legacy refresh
    # cookie was upgraded, whose old value no longer resolves to anything.
    pending_secret: str | None = None

    def account(self, user_id: str | None) -> dict[str, Any] | None:
        """Return the account row for *user_id* when it is signed in here."""
        if not user_id:
            return None
        return next((row for row in self.accounts if row["user_id"] == user_id), None)


async def resolve_browser_session(
    op_store: Any, request: Request, *, upgrade: bool = True
) -> BrowserSession | None:
    """Return the browser session the request's cookie names, or None.

    With *upgrade*, a cookie that is still a single account's refresh token is
    turned into a browser session holding that account; the caller must then
    deliver the new cookie (``rotate_cookie`` or ``send_pending_cookie``).
    """
    secret = get_refresh_token_cookie(request)
    if not secret:
        return None
    secret_hash = hash_refresh_token(secret)
    now = datetime.now(timezone.utc)

    row = await op_store.get_browser_session(secret_hash)
    if row is not None:
        if row["revoked"] or row["expires_at"] <= now:
            return None
        return BrowserSession(
            id=row["id"],
            secret_hash=secret_hash,
            active_user_id=row["active_user_id"],
            accounts=await op_store.list_browser_session_accounts(row["id"]),
        )

    if not upgrade:
        return None
    legacy = await op_store.get_session_by_token_hash(secret_hash)
    if legacy is None or legacy["revoked"] or legacy["expires_at"] <= now:
        return None

    new_secret = create_refresh_token()
    browser_session_id = generate_ulid()
    await op_store.create_browser_session(
        browser_session_id=browser_session_id,
        secret_hash=hash_refresh_token(new_secret),
        expires_at=legacy["expires_at"],
    )
    await op_store.attach_browser_session_account(
        browser_session_id, user_id=legacy["user_id"], auth_session_id=legacy["id"]
    )
    # The old cookie value must stop resolving, or presenting it again would
    # upgrade the same session into a second browser.
    await op_store.rotate_session(
        legacy["id"],
        new_refresh_token_hash=hash_refresh_token(create_refresh_token()),
        new_jti=generate_ulid(),
    )
    logger.info(
        "Upgraded a single-account session cookie to browser session %s", browser_session_id
    )
    return BrowserSession(
        id=browser_session_id,
        secret_hash=hash_refresh_token(new_secret),
        active_user_id=legacy["user_id"],
        accounts=await op_store.list_browser_session_accounts(browser_session_id),
        pending_secret=new_secret,
    )


async def rotate_cookie(
    op_store: Any, response: Response, browser: BrowserSession, *, extend: bool = False
) -> None:
    """Replace the browser's cookie value, as every refresh and sign-in does.

    Raises:
        HTTPException: 401 when another request rotated it first, which leaves
            the value this one presented retired.
    """
    secret = create_refresh_token()
    secret_hash = hash_refresh_token(secret)
    expires_at = (
        datetime.now(timezone.utc) + timedelta(days=get_refresh_token_expire_days())
        if extend
        else None
    )
    rotated = await op_store.rotate_browser_session(
        browser.id,
        current_secret_hash=browser.secret_hash,
        new_secret_hash=secret_hash,
        expires_at=expires_at,
    )
    if not rotated:
        raise HTTPException(status_code=401, detail="Session has changed. Please login again.")
    browser.secret_hash = secret_hash
    browser.pending_secret = None
    set_refresh_token_cookie(response, secret)


def send_pending_cookie(response: Response, browser: BrowserSession | None) -> None:
    """Deliver an upgraded cookie when this request produced one and rotates nothing."""
    if browser is not None and browser.pending_secret:
        set_refresh_token_cookie(response, browser.pending_secret)
        browser.pending_secret = None


async def prepare_sign_in(op_store: Any, request: Request, user_id: str) -> BrowserSession | None:
    """Resolve the browser a sign-in joins, refusing one that is already full.

    Called before the new session is created, so a refused sign-in leaves
    nothing behind.

    Raises:
        HTTPException: 409 when the browser already holds the maximum number of
            other accounts.
    """
    browser = await resolve_browser_session(op_store, request)
    if (
        browser is not None
        and browser.account(user_id) is None
        and len(browser.accounts) >= MAX_ACCOUNTS_PER_BROWSER
    ):
        raise HTTPException(
            status_code=409,
            detail=(
                f"This browser is already signed in to {MAX_ACCOUNTS_PER_BROWSER} accounts. "
                "Sign one of them out before adding another."
            ),
        )
    return browser


async def complete_sign_in(
    op_store: Any,
    response: Response,
    browser: BrowserSession | None,
    *,
    user_id: str,
    auth_session_id: str,
) -> None:
    """Add a freshly created session to the browser and make its account active.

    A browser with no session gets one, and so does one whose session was
    retired after it was resolved (its last account signed out meanwhile).
    One that already has a session for this account has it replaced, and
    revoked. Either way the cookie value changes, because what the browser
    can act as just did.
    """
    if browser is not None and await op_store.attach_browser_session_account(
        browser.id, user_id=user_id, auth_session_id=auth_session_id
    ):
        await rotate_cookie(op_store, response, browser, extend=True)
        return
    secret = create_refresh_token()
    browser_session_id = generate_ulid()
    await op_store.create_browser_session(
        browser_session_id=browser_session_id,
        secret_hash=hash_refresh_token(secret),
        expires_at=datetime.now(timezone.utc) + timedelta(days=get_refresh_token_expire_days()),
    )
    await op_store.attach_browser_session_account(
        browser_session_id, user_id=user_id, auth_session_id=auth_session_id
    )
    set_refresh_token_cookie(response, secret)


async def issue_account_token(op_store: Any, account: dict[str, Any]) -> tuple[str, str]:
    """Mint an access token for one signed-in account; return it and the role it carries."""
    user_role = account["role"] or "free"
    # Bootstrap seed, as at login: only an ADMIN_EMAILS account still at 'free'.
    if account["email"] and is_admin_email(account["email"]) and user_role == "free":
        user_role = "admin"
        await op_store.update_user_fields(account["user_id"], role="admin")
        logger.info(f"Bootstrap-seeded user {account['user_id']} to admin (ADMIN_EMAILS)")
    access_token, jti = create_access_token(
        user_id=account["user_id"],
        email=account["email"],
        session_id=account["sid"],
        is_admin=user_role == "admin",
        role=user_role,
    )
    # Its refresh_token_hash is never sent to a browser any more; rotating it
    # keeps last_used_at and the jti current, as a refresh always has.
    await op_store.rotate_session(
        account["auth_session_id"],
        new_refresh_token_hash=hash_refresh_token(create_refresh_token()),
        new_jti=jti,
    )
    return access_token, user_role


def account_user_info(account: dict[str, Any], *, role: str | None = None) -> UserInfo:
    """Describe a signed-in account the way the login response does."""
    user_role = role or account["role"] or "free"
    return UserInfo(
        id=account["user_id"],
        email=account["email"],
        login_name=account.get("login_name"),
        phone=account.get("phone"),
        phone_verified=account.get("phone_verified") or False,
        has_password=bool(account.get("has_password")),
        user_name=account.get("user_name"),
        role=user_role,
        status=account["status"],
        email_verified=account.get("email_verified") or False,
        is_admin=user_role == "admin",
        created_at=account["created_at"],
        last_login_at=account.get("last_login_at"),
    )


def _agent_access(account: dict[str, Any]) -> bool | None:
    """Whether the account may use the Cloud Agent; None when that cannot be told."""
    try:
        permissions = resolve_agent_access_permissions(
            user_id=account["user_id"], role=account["role"] or "free"
        )
    except AgentAccessPolicyUnavailable:
        return None
    return "agent.use" in permissions


def account_summaries(
    browser: BrowserSession | None, *, require_verification: bool
) -> list[AccountSummary]:
    """List a browser's accounts for its account chooser, oldest first.

    An account whose user row is gone is left out; one whose session ended is
    listed as signed out, so the chooser can ask for its password again.
    """
    if browser is None:
        return []
    now = datetime.now(timezone.utc)
    summaries = []
    for account in browser.accounts:
        if account.get("status") is None:
            continue
        signed_in = session_live(account, now=now) and not identity_unverified(
            account, require_verification=require_verification
        )
        summaries.append(
            AccountSummary(
                id=account["user_id"],
                email=account["email"],
                login_name=account.get("login_name"),
                phone=account.get("phone"),
                user_name=account.get("user_name"),
                role=account["role"] or "free",
                active=account["user_id"] == browser.active_user_id,
                signed_in=signed_in,
                agent_access=_agent_access(account) if signed_in else None,
            )
        )
    return summaries
