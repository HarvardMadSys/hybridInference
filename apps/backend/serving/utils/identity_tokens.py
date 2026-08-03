"""Authorization codes and identity tokens for cross-service SSO.

The flow this implements is OAuth 2.0 authorization code with PKCE, trimmed to
the one thing the gateway actually needs: letting a detached service — the cloud
agent — learn who the user is without holding a credential of ours.

    browser ──▶ /authorize (our frontend, user already signed in)
             ──▶ POST /v1/identity/code      ── one-time code ──▶ redirect
    service ──▶ POST /v1/identity/token      ── identity JWT

What is deliberately absent:

- **No refresh token.** The consuming service exchanges the identity token for
  its own session immediately and never comes back. Issuing a long-lived
  credential to a service that does not need one only creates something to
  steal.
- **No client secret.** The web client is a public client; a secret shipped to a
  browser is not a secret. PKCE is what binds the code to the caller instead.
- **No ``plain`` challenge method.** ``S256`` only. ``plain`` offers no
  protection against an attacker who already intercepted the authorization
  request, which is the attack PKCE exists for.

The code is random and stored **hashed**, so a read of the table does not yield
usable codes. It is claimed with a single atomic statement rather than
read-then-mark, because "was it used?" and "mark it used" as two statements is a
race that lets one code be exchanged twice.

**One judgement call, recorded as one.** The code is consumed before the PKCE
verifier is checked, so a failed exchange burns it. That is the common
implementation choice and it makes an intercepted code good for at most one
attempt — but it is a trade, not a theorem. Someone who can read a code without
being able to use it (from a referrer header, browser history, or a proxy log)
can deny the legitimate exchange by racing it. The alternative — verify first,
then claim — removes that at the cost of letting a stolen code be probed
repeatedly inside its lifetime. Both are defensible; the DoS is bounded by a
60-second window and a retry, which is why it reads as the lesser cost here. If
that reasoning stops holding, this is the line to change.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt

from serving.config.settings import settings
from serving.utils.identity_keys import (
    ALGORITHM,
    IdentityKeyUnavailable,
    signing_key,
    signing_kid,
)

#: The only client this gateway issues identity tokens for. A second consumer
#: is a deliberate act, not a configuration change.
CLOUD_AGENT_CLIENT_ID = "cloud-agent"

#: Long enough to survive a redirect, short enough that a leaked code from a
#: proxy log or browser history is almost certainly already dead.
CODE_TTL_SECONDS = 60

#: The consuming service swaps this for its own session on receipt. Ten minutes
#: covers a slow exchange without leaving a usable credential lying around.
TOKEN_TTL_SECONDS = 600

CHALLENGE_METHOD = "S256"

ENV_ISSUER = "IDENTITY_ISSUER"
ENV_ALLOWED_REDIRECTS = "IDENTITY_ALLOWED_REDIRECTS"


class IdentityNotConfigured(Exception):
    """Raised when issuance is not configured on this deployment.

    Distinct from :class:`~serving.utils.identity_keys.IdentityKeyUnavailable`
    because publishing a verification key and issuing tokens are separately
    switchable: JWKS needs only the key, issuance also needs an issuer identity
    and a redirect allowlist.
    """


class InvalidAuthorizationRequest(Exception):
    """Raised when an authorization or exchange request is not acceptable."""


def issuer() -> str:
    """Return the ``iss`` value for identity tokens.

    Returns:
        The configured issuer URL, without a trailing slash.

    Raises:
        IdentityNotConfigured: If no issuer is configured.
    """
    value = (os.environ.get(ENV_ISSUER) or settings.base_url or "").strip()
    if not value:
        # Minting with an empty issuer would produce tokens that every correct
        # consumer rejects, and it would look like a verification bug rather
        # than the missing configuration it is.
        raise IdentityNotConfigured(
            f"{ENV_ISSUER} (or base_url) must be set to issue identity tokens"
        )
    return value.rstrip("/")


def allowed_redirects() -> tuple[str, ...]:
    """Return the exact redirect URIs this gateway will hand a code to.

    Returns:
        The configured redirect URIs.

    Raises:
        IdentityNotConfigured: If none are configured.
    """
    raw = os.environ.get(ENV_ALLOWED_REDIRECTS, "")
    values = tuple(item.strip() for item in raw.split(",") if item.strip())
    if not values:
        raise IdentityNotConfigured(f"{ENV_ALLOWED_REDIRECTS} must list at least one redirect URI")
    return values


def assert_issuance_configured() -> None:
    """Check that every part of issuance is configured, not just some.

    Called **before** an authorization code is created. A deployment with a
    redirect allowlist but no usable signing key would otherwise hand out a code
    that ``/token`` consumes and then fails to redeem — and since the failed
    exchange burns the code, the caller retries into the same wall with no
    indication of why. Partial configuration must refuse at the first step.

    Raises:
        IdentityNotConfigured: If the issuer or redirect allowlist is missing.
        IdentityKeyUnavailable: If no signing key is configured.
        IdentityKeyMisconfigured: If the signing key is configured but unusable.
    """
    issuer()
    allowed_redirects()
    signing_kid()


def issuance_enabled() -> bool:
    """Report whether this deployment can issue identity tokens.

    Raises:
        IdentityKeyMisconfigured: If a signing key is present but unusable —
            that is not "issuance is off", and must not be reported as such.
    """
    try:
        assert_issuance_configured()
    except (IdentityNotConfigured, IdentityKeyUnavailable):
        return False
    return True


def validate_authorization_request(*, client_id: str, redirect_uri: str, method: str) -> None:
    """Check a code request against the configured client and redirects.

    Args:
        client_id: The client identifier presented by the caller.
        redirect_uri: Where the caller wants the code delivered.
        method: The PKCE challenge method.

    Raises:
        InvalidAuthorizationRequest: If any of them is unacceptable.
        IdentityNotConfigured: If issuance is not configured.
    """
    if client_id != CLOUD_AGENT_CLIENT_ID:
        raise InvalidAuthorizationRequest(f"unknown client_id {client_id!r}")
    if method != CHALLENGE_METHOD:
        raise InvalidAuthorizationRequest(f"code_challenge_method must be {CHALLENGE_METHOD}")
    # Exact match, never a prefix or suffix test: "startswith(allowed)" lets
    # https://agents.example.com.attacker.test through, and that is precisely
    # the redirect an attacker would register.
    if redirect_uri not in allowed_redirects():
        raise InvalidAuthorizationRequest("redirect_uri is not allowed")


def new_code() -> tuple[str, str]:
    """Mint an authorization code.

    Returns:
        The code to hand to the caller, and the hash to store.
    """
    code = secrets.token_urlsafe(32)
    return code, hash_code(code)


def hash_code(code: str) -> str:
    """Return the stored form of an authorization code."""
    return hashlib.sha256(code.encode()).hexdigest()


def verify_pkce(*, verifier: str, challenge: str) -> bool:
    """Check a PKCE verifier against the stored S256 challenge.

    Args:
        verifier: The ``code_verifier`` presented at exchange.
        challenge: The ``code_challenge`` recorded when the code was issued.

    Returns:
        Whether they match.
    """
    digest = hashlib.sha256(verifier.encode()).digest()
    computed = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return hmac.compare_digest(computed, challenge)


def expires_at(*, seconds: int) -> datetime:
    """Return an absolute expiry ``seconds`` from now, in UTC."""
    return datetime.now(UTC) + timedelta(seconds=seconds)


def mint_identity_token(*, user_id: str, email: str | None, role: str) -> str:
    """Mint the identity token handed to the consuming service.

    Args:
        user_id: The gateway's user identifier, carried as ``sub``.
        email: The user's email, if known.
        role: The user's role — this deployment has no separate plan concept,
            so entitlement decisions downstream key off this.

    Returns:
        A signed RS256 JWT.

    Raises:
        IdentityNotConfigured: If no issuer is configured.
        IdentityKeyUnavailable: If no signing key is configured.
    """
    now = datetime.now(UTC)
    claims: dict[str, Any] = {
        "iss": issuer(),
        "aud": CLOUD_AGENT_CLIENT_ID,
        "sub": user_id,
        "role": role,
        "iat": now,
        "exp": now + timedelta(seconds=TOKEN_TTL_SECONDS),
    }
    if email:
        claims["email"] = email
    return jwt.encode(
        claims,
        signing_key(),
        algorithm=ALGORITHM,
        headers={"kid": signing_kid()},
    )
