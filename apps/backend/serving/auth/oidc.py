"""OpenID Connect sign-in at an external identity provider, as a relying party.

This module is the whole of the protocol and nothing else: it builds the
redirect to the provider and turns the provider's answer into an
:class:`ExternalIdentity`. It reads no database and knows no account; what it
needs between the two halves of a sign-in is a small JSON value the caller
stores with the flow. Everything after it -- which account an identity signs
in to, linking, creating accounts -- works from the identity alone, so another
protocol can be added beside this one without touching that logic.

Authorization code flow with PKCE (RFC 7636) and a nonce; the ID token is
validated as OpenID Connect Core 1.0 section 3.1.3.7 requires.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import quote, urlencode, urlsplit

import httpx
import jwt

from serving.utils.logging import get_logger

logger = get_logger(__name__)

#: Signature algorithms accepted on an ID token: asymmetric only. ``none`` and
#: the HMAC family are refused, the latter because its key would be the client
#: secret, which makes anyone holding the secret able to mint identities.
ALLOWED_ALGORITHMS = frozenset(
    {"RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512"}
)

#: Clock difference tolerated between this gateway and the provider.
CLOCK_SKEW_SECONDS = 60

_HTTP_TIMEOUT_SECONDS = 10.0
_DISCOVERY_TTL_SECONDS = 3600.0
_JWKS_TTL_SECONDS = 3600.0
#: An unknown ``kid`` refetches the keys at most this often, so a stream of
#: forged tokens cannot make the gateway hammer the provider.
_JWKS_REFETCH_INTERVAL_SECONDS = 60.0
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

#: Replaced in tests with an ``httpx.MockTransport`` standing in for a provider.
_transport: httpx.AsyncBaseTransport | None = None

_discovery_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_jwks_cache: dict[str, tuple[float, list[jwt.PyJWK]]] = {}
#: One lock for each JWKS address, held while its keys are fetched: a provider
#: that answers slowly holds up only the sign-ins that need its own keys.
_jwks_locks: dict[str, asyncio.Lock] = {}


class SsoProtocolError(Exception):
    """A sign-in at the identity provider could not be started or completed.

    ``code`` is what the user is told: ``"unavailable"`` when the provider
    cannot be reached or is misconfigured, ``"failed"`` when its answer was
    refused. ``reason`` is for the log and never carries a token, a code or a
    secret.
    """

    def __init__(self, code: Literal["unavailable", "failed"], reason: str) -> None:
        """Record the user-facing code and the reason to log."""
        super().__init__(reason)
        self.code = code
        self.reason = reason


@dataclass(frozen=True)
class OidcSettings:
    """What this module needs to know about one provider."""

    issuer: str
    client_id: str
    client_secret: str
    client_auth_method: str = "client_secret_basic"
    scopes: tuple[str, ...] = ("openid", "email", "profile")
    #: Claims the ID token must carry, as (name, value) pairs.
    required_claims: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class ExternalIdentity:
    """Who an identity provider says signed in. Nothing in it is protocol-specific.

    ``email_verified`` is None when the provider did not say. ``authenticated_at``
    is when the user last entered their credentials at the provider, when the
    provider reported it.
    """

    issuer: str
    subject: str
    email: str | None = None
    email_verified: bool | None = None
    name: str | None = None
    authenticated_at: datetime | None = None


def is_secure_url(url: str) -> bool:
    """Whether *url* is https, or http to this machine (local development)."""
    parts = urlsplit(url)
    if parts.scheme == "https" and parts.hostname:
        return True
    return parts.scheme == "http" and parts.hostname in _LOCAL_HOSTS


def hash_value(value: str) -> str:
    """SHA-256 of a one-time value, for storage and comparison."""
    return hashlib.sha256(value.encode()).hexdigest()


def clear_caches() -> None:
    """Forget every provider's discovery document and keys."""
    _discovery_cache.clear()
    _jwks_cache.clear()
    _jwks_locks.clear()


async def begin(
    settings: OidcSettings,
    *,
    redirect_uri: str,
    state: str,
    login_hint: str | None = None,
    fresh_login: bool = False,
) -> tuple[str, dict[str, Any]]:
    """Build the redirect to the provider's authorization endpoint.

    Args:
        settings: The provider.
        redirect_uri: Where the provider sends the browser back; registered there.
        state: The caller's unguessable value, echoed back by the provider.
        login_hint: An email to prefill at the provider.
        fresh_login: Ask the provider to make the user sign in again, and
            require proof of it on the way back (re-authentication).

    Returns:
        The URL to send the browser to, and the protocol state the caller must
        store with the flow and hand back to :func:`complete`.

    Raises:
        SsoProtocolError: ``unavailable`` when the provider's configuration
            cannot be fetched or is unusable.
    """
    document = await _discovery(settings.issuer)
    nonce = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
    params = {
        "response_type": "code",
        "client_id": settings.client_id,
        "redirect_uri": redirect_uri,
        "scope": " ".join(settings.scopes),
        "state": state,
        "nonce": nonce,
        "code_challenge": challenge.rstrip(b"=").decode(),
        "code_challenge_method": "S256",
    }
    if login_hint:
        params["login_hint"] = login_hint
    if fresh_login:
        # prompt=login is a request the provider SHOULD honour; max_age makes
        # auth_time a REQUIRED claim, which is what complete() checks.
        params["prompt"] = "login"
        params["max_age"] = "0"
    endpoint = document["authorization_endpoint"]
    separator = "&" if urlsplit(endpoint).query else "?"
    protocol_state = {
        "nonce_hash": hash_value(nonce),
        "code_verifier": verifier,
        "fresh_login": fresh_login,
    }
    return f"{endpoint}{separator}{urlencode(params)}", protocol_state


async def complete(
    settings: OidcSettings,
    *,
    redirect_uri: str,
    code: str,
    protocol_state: dict[str, Any],
) -> ExternalIdentity:
    """Exchange the authorization code and validate who signed in.

    Args:
        settings: The provider.
        redirect_uri: The same value :func:`begin` was given.
        code: The authorization code from the callback.
        protocol_state: What :func:`begin` returned for this flow.

    Returns:
        The identity the provider vouches for.

    Raises:
        SsoProtocolError: ``unavailable`` when the provider cannot be reached,
            ``failed`` when its answer does not validate.
    """
    document = await _discovery(settings.issuer)
    tokens = await _exchange_code(
        settings,
        document["token_endpoint"],
        redirect_uri=redirect_uri,
        code=code,
        verifier=str(protocol_state.get("code_verifier") or ""),
    )
    claims = await _validate_id_token(settings, document, tokens["id_token"])

    nonce = claims.get("nonce")
    expected = str(protocol_state.get("nonce_hash") or "")
    if not isinstance(nonce, str) or not hmac.compare_digest(hash_value(nonce), expected):
        raise SsoProtocolError("failed", "the ID token's nonce does not belong to this flow")

    # An issuer shared by many organizations says which one an account is in
    # with a claim. An account that lacks it is someone else's, however valid.
    for name, expected in settings.required_claims:
        if claims.get(name) != expected:
            raise SsoProtocolError("failed", f"the ID token's {name} claim is not the required one")

    authenticated_at = _auth_time(claims)
    if protocol_state.get("fresh_login") and authenticated_at is None:
        raise SsoProtocolError("failed", "the provider did not report when the user signed in")

    subject = claims["sub"]
    email = _text(claims.get("email"))
    email_verified = _flag(claims.get("email_verified"))
    name = _text(claims.get("name"))
    if email is None and document.get("userinfo_endpoint") and tokens.get("access_token"):
        info = await _userinfo(document["userinfo_endpoint"], tokens["access_token"])
        if info is not None:
            # A UserInfo response for another subject must not be used (Core 5.3.2).
            if info.get("sub") != subject:
                raise SsoProtocolError("failed", "the UserInfo response is for another subject")
            email = _text(info.get("email"))
            email_verified = _flag(info.get("email_verified"))
            name = name or _text(info.get("name"))

    return ExternalIdentity(
        issuer=settings.issuer,
        subject=subject,
        email=email.lower() if email else None,
        email_verified=email_verified,
        name=name,
        authenticated_at=authenticated_at,
    )


# -- provider metadata and keys -------------------------------------------------


def _client() -> httpx.AsyncClient:
    # Redirects are never followed: a token request that is redirected would
    # carry the client secret and the code to wherever the provider points.
    return httpx.AsyncClient(
        timeout=_HTTP_TIMEOUT_SECONDS, follow_redirects=False, transport=_transport
    )


async def _get_json(url: str, *, what: str, headers: dict[str, str] | None = None) -> dict:
    try:
        async with _client() as client:
            response = await client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        raise SsoProtocolError(
            "unavailable", f"{what} request failed: {type(exc).__name__}"
        ) from exc
    if response.status_code != 200:
        raise SsoProtocolError("unavailable", f"{what} answered {response.status_code}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise SsoProtocolError("unavailable", f"{what} is not JSON") from exc
    if not isinstance(payload, dict):
        raise SsoProtocolError("unavailable", f"{what} is not a JSON object")
    return payload


async def _discovery(issuer: str) -> dict[str, Any]:
    cached = _discovery_cache.get(issuer)
    if cached and time.monotonic() - cached[0] < _DISCOVERY_TTL_SECONDS:
        return cached[1]
    if not is_secure_url(issuer):
        raise SsoProtocolError("unavailable", "the issuer is not an https URL")
    document = await _get_json(
        issuer.rstrip("/") + "/.well-known/openid-configuration", what="discovery"
    )
    # The issuer a provider names for itself is what its ID tokens carry: a
    # document for another issuer must not be trusted for this one.
    if document.get("issuer") != issuer:
        raise SsoProtocolError("unavailable", "the discovery document names another issuer")
    for name in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
        if not isinstance(document.get(name), str) or not is_secure_url(document[name]):
            raise SsoProtocolError("unavailable", f"the discovery document has no usable {name}")
    userinfo = document.get("userinfo_endpoint")
    if userinfo is not None and not (isinstance(userinfo, str) and is_secure_url(userinfo)):
        document = {**document, "userinfo_endpoint": None}
    _discovery_cache[issuer] = (time.monotonic(), document)
    return document


def _cached_keys(jwks_uri: str, *, refetch: bool) -> list[jwt.PyJWK] | None:
    """The cached keys that still answer this request; None when they must be fetched."""
    cached = _jwks_cache.get(jwks_uri)
    if cached is None:
        return None
    age = time.monotonic() - cached[0]
    if (not refetch and age < _JWKS_TTL_SECONDS) or age < _JWKS_REFETCH_INTERVAL_SECONDS:
        return cached[1]
    return None


async def _keys(jwks_uri: str, *, refetch: bool = False) -> list[jwt.PyJWK]:
    keys = _cached_keys(jwks_uri, refetch=refetch)
    if keys is not None:
        return keys
    async with _jwks_locks.setdefault(jwks_uri, asyncio.Lock()):
        # Sign-ins that arrived together fetch once: the ones that waited
        # read what the first one stored.
        keys = _cached_keys(jwks_uri, refetch=refetch)
        if keys is not None:
            return keys
        payload = await _get_json(jwks_uri, what="JWKS")
        keys = []
        for entry in payload.get("keys") or []:
            if not isinstance(entry, dict) or entry.get("use") == "enc":
                continue
            try:
                keys.append(jwt.PyJWK.from_dict(entry))
            except jwt.PyJWTError:
                # A key in a format this library cannot use; the others may do.
                continue
        _jwks_cache[jwks_uri] = (time.monotonic(), keys)
        return keys


def _pick_key(keys: list[jwt.PyJWK], kid: str | None) -> jwt.PyJWK | None:
    if kid is not None:
        return next((key for key in keys if key.key_id == kid), None)
    # No kid in the token: unambiguous only when the provider publishes one key.
    return keys[0] if len(keys) == 1 else None


async def _validate_id_token(
    settings: OidcSettings, document: dict[str, Any], id_token: str
) -> dict[str, Any]:
    try:
        header = jwt.get_unverified_header(id_token)
    except jwt.PyJWTError as exc:
        raise SsoProtocolError("failed", "the ID token is malformed") from exc
    algorithm = header.get("alg")
    if algorithm not in ALLOWED_ALGORITHMS:
        raise SsoProtocolError("failed", "the ID token uses a signature algorithm that is refused")
    kid = header.get("kid")
    key = _pick_key(await _keys(document["jwks_uri"]), kid)
    if key is None:
        # The provider may have rotated its keys since they were cached.
        key = _pick_key(await _keys(document["jwks_uri"], refetch=True), kid)
    if key is None:
        raise SsoProtocolError("failed", "no published key matches the ID token")
    try:
        claims = jwt.decode(
            id_token,
            key=key.key,
            algorithms=[algorithm],
            audience=settings.client_id,
            issuer=settings.issuer,
            leeway=CLOCK_SKEW_SECONDS,
            options={"require": ["exp", "iat", "sub", "aud", "iss"]},
        )
    except jwt.PyJWTError as exc:
        raise SsoProtocolError("failed", f"the ID token was refused: {type(exc).__name__}") from exc
    if not isinstance(claims.get("sub"), str) or not claims["sub"]:
        raise SsoProtocolError("failed", "the ID token has no subject")
    audience = claims.get("aud")
    authorized_party = claims.get("azp")
    several = isinstance(audience, list) and len(audience) > 1
    if (several and authorized_party is None) or (
        authorized_party is not None and authorized_party != settings.client_id
    ):
        raise SsoProtocolError("failed", "the ID token was issued to another client")
    return claims


# -- token and UserInfo requests -------------------------------------------------


async def _exchange_code(
    settings: OidcSettings, endpoint: str, *, redirect_uri: str, code: str, verifier: str
) -> dict[str, Any]:
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "code_verifier": verifier,
    }
    headers = {"Accept": "application/json"}
    if settings.client_auth_method == "client_secret_post":
        form["client_id"] = settings.client_id
        form["client_secret"] = settings.client_secret
    else:
        # RFC 6749 2.3.1: both parts are form-encoded before Basic encoding.
        pair = f"{quote(settings.client_id, safe='')}:{quote(settings.client_secret, safe='')}"
        headers["Authorization"] = "Basic " + base64.b64encode(pair.encode()).decode()
    try:
        async with _client() as client:
            response = await client.post(endpoint, data=form, headers=headers)
    except httpx.HTTPError as exc:
        raise SsoProtocolError(
            "unavailable", f"token request failed: {type(exc).__name__}"
        ) from exc
    if response.status_code != 200:
        # The body may quote the code or the secret back; only the status is logged.
        raise SsoProtocolError("failed", f"the token endpoint answered {response.status_code}")
    try:
        tokens = response.json()
    except ValueError as exc:
        raise SsoProtocolError("failed", "the token response is not JSON") from exc
    if not isinstance(tokens, dict) or not isinstance(tokens.get("id_token"), str):
        raise SsoProtocolError("failed", "the token response has no ID token")
    return tokens


async def _userinfo(endpoint: str, access_token: str) -> dict[str, Any] | None:
    # The ID token already proved who signed in; UserInfo only adds the email.
    # When it cannot be read the identity simply has none, which still lets an
    # identity that is already linked sign in.
    try:
        return await _get_json(
            endpoint, what="UserInfo", headers={"Authorization": f"Bearer {access_token}"}
        )
    except SsoProtocolError as exc:
        logger.warning("Single sign-on: %s; continuing without an email", exc.reason)
        return None


# -- claims -----------------------------------------------------------------------


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _flag(value: Any) -> bool | None:
    # Some providers send the boolean as a string.
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in ("true", "false"):
        return value.lower() == "true"
    return None


def _auth_time(claims: dict[str, Any]) -> datetime | None:
    value = claims.get("auth_time")
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        return datetime.fromtimestamp(value, UTC)
    except (OverflowError, OSError, ValueError):
        return None
