"""API key authorization: an application creates an API key on a user's own account.

Three endpoints, in the order they are used. The rules they apply live in
:mod:`serving.utils.key_authorization`.

- ``POST /v1/auth/keys/check`` — public. The console's consent page asks
  whether a request is acceptable before it shows anything, and learns the
  name to show (the callback's host) and where Cancel goes.
- ``POST /v1/auth/keys/code`` — the signed-in user pressed Allow. Returns a
  one-time code and the callback URL, with the code appended, to send the
  browser to.
- ``POST /v1/auth/keys`` — the application's server exchanges the code and its
  PKCE verifier for an API key on the user's account. Public: PKCE binds the
  code to whoever started the flow.

The first two answer in the gateway's error envelope, which the console reads.
The exchange answers in OAuth's (``{"error": "invalid_grant",
"error_description": ...}``), which an application's OAuth client reads. Every
message is static: which part of a request was wrong goes to the log, never
to the caller.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from serving.audit import AuditActor, AuditTarget
from serving.exceptions import APIKeyLimitReachedError, ProjectNotFoundError
from serving.schemas_auth import API_KEY_NAME_MAX_LENGTH
from serving.servers.auth import log_admin_action
from serving.servers.deps import get_current_user, get_db_logger, get_operational_store
from serving.servers.routers import user_routes
from serving.utils.identity_tokens import expires_at, hash_code, new_code, verify_pkce
from serving.utils.key_authorization import (
    CHALLENGE_METHOD,
    CODE_TTL_SECONDS,
    Callback,
    InvalidKeyAuthorizationRequest,
    KeyAuthorizationNotConfigured,
    account_refusal,
    allowed_callback,
    check_request,
    is_configured,
    key_metadata,
    key_notes,
    valid_verifier,
    with_parameter,
)
from serving.utils.key_exchange_rate_limit import exchange_refused, record_exchange_failure
from serving.utils.logging import get_logger
from serving.utils.request_ip import get_client_ip, get_client_ip_info

logger = get_logger(__name__)

router = APIRouter(prefix="/v1/auth/keys", tags=["API key authorization"])

_NOT_CONFIGURED = "API key authorization is not offered on this deployment."
_INVALID_REQUEST = (
    "The authorization request is not acceptable. Check callback_url, code_challenge "
    "and code_challenge_method."
)
_INVALID_EXCHANGE = (
    "The exchange request is not acceptable. Send code, a code_verifier of 43 to 128 "
    "characters, and code_challenge_method S256."
)
_INVALID_GRANT = (
    "The code is invalid, expired or already used, or the code_verifier does not match."
)
_UNVERIFIED = "Verify your email address to continue."
_UNAVAILABLE = "This account may not create API keys."
_RATE_LIMITED = "Too many failed exchanges from this address. Try again later."

#: A response that carries a code or a key must never be stored on the way.
_NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}


def _error(status_code: int, error_type: str, message: str) -> HTTPException:
    """An error in the gateway's envelope, for the console's two endpoints."""
    return HTTPException(
        status_code=status_code,
        detail={"error": {"type": error_type, "message": message}},
    )


def _exchange_error(
    status_code: int, error: str, description: str, headers: dict[str, str] | None = None
) -> HTTPException:
    """An error in OAuth's token-endpoint shape (RFC 6749 section 5.2), for the exchange."""
    return HTTPException(
        status_code=status_code,
        detail={"error": error, "error_description": description},
        headers={**_NO_STORE, **(headers or {})},
    )


def _key_limit_message() -> str:
    limit = user_routes.MAX_ACTIVE_API_KEYS_PER_ACCOUNT
    return (
        f"This account already holds {limit} active API keys, the most it may. "
        "Revoke one, then try again."
    )


class KeyAuthorizationRequest(BaseModel):
    """What the consent page was opened with."""

    callback_url: str = Field(max_length=8192)
    code_challenge: str = Field(max_length=512)
    code_challenge_method: str = Field(default=CHALLENGE_METHOD, max_length=32)


class KeyExchangeRequest(BaseModel):
    """An application redeeming a code for its API key.

    The fields are optional here so that a missing one is answered like any
    other malformed exchange, in OAuth's shape (``invalid_request``), rather
    than as a validation error.
    """

    code: str | None = Field(default=None, max_length=512)
    code_verifier: str | None = Field(default=None, max_length=512)
    code_challenge_method: str | None = Field(default=None, max_length=32)


def _accept(body: KeyAuthorizationRequest, endpoint: str) -> Callback:
    """Return the request's callback, or refuse the request.

    Raises:
        HTTPException: 404 when no callback is configured, 400 when the request
            is not acceptable.
    """
    try:
        return check_request(
            callback_url=body.callback_url,
            code_challenge=body.code_challenge,
            method=body.code_challenge_method,
        )
    except KeyAuthorizationNotConfigured as exc:
        raise _error(status.HTTP_404_NOT_FOUND, "not_configured", _NOT_CONFIGURED) from exc
    except InvalidKeyAuthorizationRequest as exc:
        logger.warning(
            "api_key_authorization_rejected",
            extra={"path": f"{router.prefix}/{endpoint}", "reason": str(exc)},
        )
        raise _error(status.HTTP_400_BAD_REQUEST, "invalid_request", _INVALID_REQUEST) from exc


@router.post("/check")
async def check_key_authorization(
    body: KeyAuthorizationRequest,
    store=Depends(get_operational_store),
) -> dict[str, str]:
    """Say whether an authorization request can be served here, before anyone signs in.

    The consent page calls this first, signed in or not, so a link that can
    never work is refused on the page instead of after a sign-in, and so the
    page never sends the browser to a callback the gateway has not accepted.

    Args:
        body: The callback and PKCE challenge the page was opened with.
        store: Operational store; without one there are no accounts.

    Returns:
        ``app``, the callback's host, which is the only name the page shows for
        the application, and ``cancel_url``, the callback with
        ``error=access_denied`` appended.

    Raises:
        HTTPException: 400 for an unacceptable request, 404 when this deployment
            offers no API key authorization.
    """
    if store is None:
        raise _error(status.HTTP_404_NOT_FOUND, "not_configured", _NOT_CONFIGURED)
    callback = _accept(body, "check")
    return {
        "app": callback.host,
        "cancel_url": with_parameter(callback.url, "error", "access_denied"),
    }


@router.post("/code")
async def create_key_authorization_code(
    body: KeyAuthorizationRequest,
    response: Response,
    current_user: dict[str, Any] = Depends(get_current_user),
    store=Depends(get_operational_store),
) -> dict[str, Any]:
    """Issue a one-time code after the signed-in user approved the application.

    Everything the exchange will need is checked now, so a user is not sent
    back to the application with a code that cannot become a key: the request,
    the account, and the account's room for one more key.

    Args:
        body: The callback and PKCE challenge the page was opened with.
        response: The outgoing response, for its cache headers.
        current_user: Resolved from the caller's session token.
        store: Operational store.

    Returns:
        ``code``; ``redirect_url``, the callback with ``code`` appended and its
        own query kept; ``expires_in``; and ``app``.

    Raises:
        HTTPException: 400 for an unacceptable request; 403 for an account that
            must verify its email first (``email_not_verified``) or may not
            create keys (``account_unavailable``); 404 when this deployment
            offers no API key authorization; 409 when the account already holds
            the most active keys it may.
    """
    if store is None:
        raise _error(status.HTTP_404_NOT_FOUND, "not_configured", _NOT_CONFIGURED)
    callback = _accept(body, "code")

    refusal = account_refusal(current_user)
    if refusal == "unverified":
        raise _error(status.HTTP_403_FORBIDDEN, "email_not_verified", _UNVERIFIED)
    if refusal is not None:
        raise _error(status.HTTP_403_FORBIDDEN, "account_unavailable", _UNAVAILABLE)
    # Refused now, not at the exchange: a code the exchange must refuse would
    # send the user to the application with nothing it can use.
    active = await store.count_active_keys(current_user["user_id"])
    if active >= user_routes.MAX_ACTIVE_API_KEYS_PER_ACCOUNT:
        raise _error(status.HTTP_409_CONFLICT, "key_limit_reached", _key_limit_message())

    code, code_hash = new_code()
    await store.create_api_key_authorization_code(
        code_hash=code_hash,
        user_id=current_user["user_id"],
        callback_url=callback.without_query,
        code_challenge=body.code_challenge,
        expires_at=expires_at(seconds=CODE_TTL_SECONDS),
    )
    response.headers.update(_NO_STORE)
    return {
        "code": code,
        "redirect_url": with_parameter(callback.url, "code", code),
        "expires_in": CODE_TTL_SECONDS,
        "app": callback.host,
    }


@router.post("")
async def exchange_key_authorization_code(
    body: KeyExchangeRequest,
    request: Request,
    response: Response,
    store=Depends(get_operational_store),
    db_logger=Depends(get_db_logger),
) -> dict[str, str]:
    """Exchange a one-time code and its PKCE verifier for an API key.

    The key is an ordinary key on the account that approved the request,
    created the way the account's owner creates one (Default project, the
    account's quota, the per-account ceiling), named after the callback's host.
    Its full value is returned here and nowhere else.

    The code is claimed before the verifier is checked, so each code gets one
    attempt whatever its outcome; see :mod:`serving.utils.identity_tokens` for
    that trade. Failed attempts are counted per client address
    (:mod:`serving.utils.key_exchange_rate_limit`).

    Args:
        body: The code, its verifier, and the challenge method.
        request: The incoming request, for the client address.
        response: The outgoing response, for its cache headers.
        store: Operational store.
        db_logger: Audit log writer.

    Returns:
        ``key``, the full API key, and ``user_id``, the account it belongs to.

    Raises:
        HTTPException: 400 ``invalid_request`` for a malformed exchange and
            ``invalid_grant`` for a code that is unknown, expired, used or
            issued for a callback no longer allowed, or a verifier that does not
            match; 403 ``access_denied`` for an account that may no longer
            create keys; 404 ``not_configured``; 409 ``key_limit_reached``;
            429 ``rate_limited``.
    """
    # Checked before the code is touched, as the identity exchange does: a
    # deployment with the feature switched off must not burn codes that would
    # work again once it is switched back on.
    if store is None or not is_configured():
        raise _exchange_error(status.HTTP_404_NOT_FOUND, "not_configured", _NOT_CONFIGURED)

    client = get_client_ip_info(request)
    if await exchange_refused(client):
        raise _exchange_error(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "rate_limited",
            _RATE_LIMITED,
            headers={"Retry-After": "3600"},
        )

    # The request's own shape, before the code is claimed: it says nothing
    # about the code, so refusing it leaves the code to the caller that has it.
    method = body.code_challenge_method or CHALLENGE_METHOD
    code, verifier = body.code, body.code_verifier
    if method != CHALLENGE_METHOD or not code or verifier is None or not valid_verifier(verifier):
        await record_exchange_failure(client)
        raise _exchange_error(status.HTTP_400_BAD_REQUEST, "invalid_request", _INVALID_EXCHANGE)

    claim = await store.consume_api_key_authorization_code(hash_code(code))
    # One answer for every way a claimed code can fail: which part of a guess
    # was right is an oracle.
    if claim is None:
        await record_exchange_failure(client)
        raise _exchange_error(status.HTTP_400_BAD_REQUEST, "invalid_grant", _INVALID_GRANT)
    # A callback taken off the allowlist since the code was issued stops it too.
    callback = allowed_callback(claim["callback_url"])
    if callback is None or not verify_pkce(verifier=verifier, challenge=claim["code_challenge"]):
        await record_exchange_failure(client)
        raise _exchange_error(status.HTTP_400_BAD_REQUEST, "invalid_grant", _INVALID_GRANT)

    # Read again now: a suspension since the approval takes effect before a
    # key exists, not after.
    user = await store.get_user_by_id(claim["user_id"])
    if user is None or account_refusal(user) is not None:
        raise _exchange_error(status.HTTP_403_FORBIDDEN, "access_denied", _UNAVAILABLE)

    name = callback.host[:API_KEY_NAME_MAX_LENGTH]
    try:
        issued = await user_routes.issue_account_key(
            store,
            user_id=user["id"],
            role=user.get("role") or "free",
            name=name,
            notes=key_notes(callback),
            metadata=key_metadata(callback),
        )
    except APIKeyLimitReachedError as exc:
        raise _exchange_error(
            status.HTTP_409_CONFLICT, "key_limit_reached", _key_limit_message()
        ) from exc
    except ProjectNotFoundError as exc:
        # The account's Default project is provisioned on demand, so this is
        # an account that went away mid-exchange.
        raise _exchange_error(status.HTTP_403_FORBIDDEN, "access_denied", _UNAVAILABLE) from exc

    logger.info(
        f"API key created by API key authorization for {callback.host}",
        extra={"user_id": user["id"], "key_prefix": issued.key_prefix},
    )
    await log_admin_action(
        db_logger,
        action="create_key",
        details={
            "actor": "api_key_authorization",
            "key_prefix": issued.key_prefix,
            "name": name,
            "callback_host": callback.host,
        },
        # The account approved the key; the address is the application's
        # server, which redeemed the code.
        actor=AuditActor.user(user["id"], get_client_ip(request)),
        target=AuditTarget.user(user["id"]),
    )
    response.headers.update(_NO_STORE)
    return {"key": issued.api_key, "user_id": user["id"]}
