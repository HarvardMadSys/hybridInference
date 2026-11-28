"""API key authorization: an application creates an API key on a user's own account.

This is the OAuth PKCE *key* flow: a user who is signed in to the console
approves an application, and the application receives an ordinary API key on
that user's account, which it then holds and uses. No credential of the
application is involved; PKCE binds the one-time code to whoever started the
flow.

    browser ──▶ /authorize/key?callback_url=…&code_challenge=…&code_challenge_method=S256
                (the console's consent page)
             ──▶ POST /v1/auth/keys/check   public: is this request acceptable here?
             ──▶ POST /v1/auth/keys/code    the signed-in user pressed Allow
             ──▶ callback_url?…&code=<one-time code>
    app     ──▶ POST /v1/auth/keys          code + code_verifier ──▶ {key, user_id}

The application is identified by nothing but its callback URL, so the callback
is the security boundary and the rules for it are strict:

- It must equal an entry of :data:`ENV_CALLBACKS` in scheme, host, port and
  path. Its query string is the application's own (it carries the
  application's ``state``), is ignored for matching and is handed back
  unchanged, with the gateway's ``code`` or ``error`` appended.
- ``https`` only; ``http`` only for ``localhost`` and ``127.0.0.1``.
- No user information, no fragment, nothing a browser would parse
  differently from this module: printable ASCII only, no backslash, and a host
  of letters, digits, hyphens and dots. The page that redirects is a browser,
  and a URL two parsers read differently is how an open redirect is built.
- No ``code`` or ``error`` parameter of its own, so the answer the application
  reads is always the gateway's.

An empty list means the feature is off: the consent page and every endpoint
refuse.

Codes reuse the identity flow's primitives (:mod:`serving.utils.identity_tokens`):
random, stored only as a SHA-256 hash, claimed by one atomic statement, and
consumed before the verifier is checked, so each code gets exactly one
exchange attempt. They live :data:`CODE_TTL_SECONDS`, long enough for an
application's server to receive the redirect and redeem it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal
from urllib.parse import parse_qsl, quote, urlsplit

from serving.config.app_config import config_value
from serving.utils.logging import get_logger
from serving.utils.phone_auth import phone_identity_unverified

if TYPE_CHECKING:
    from collections.abc import Mapping
    from typing import Any

logger = get_logger(__name__)

#: The Admin setting listing the callback URLs a code may be delivered to.
ENV_CALLBACKS = "API_KEY_AUTHORIZATION_CALLBACKS"

#: Long enough for the application's server to receive the redirect and
#: redeem the code, short enough that a code left in a log is already dead.
CODE_TTL_SECONDS = 180

CHALLENGE_METHOD = "S256"

#: The longest callback URL accepted, query included.
MAX_CALLBACK_LENGTH = 2048

#: An S256 challenge: base64url of a SHA-256 digest, without padding.
_CHALLENGE = re.compile(r"[A-Za-z0-9_-]{43}")
#: A PKCE verifier (RFC 7636 section 4.1).
_VERIFIER = re.compile(r"[A-Za-z0-9._~-]{43,128}")
#: One label of a host name; an IPv4 address is four of them.
_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")

_DEFAULT_PORTS = {"https": 443, "http": 80}
#: Hosts a development callback may use plain ``http`` on.
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1"})
#: Query parameters the gateway appends to the callback, which therefore must
#: not already be there.
_RESPONSE_PARAMETERS = frozenset({"code", "error"})

#: Why an account may not authorize a key; see :func:`account_refusal`.
AccountRefusal = Literal["inactive", "unverified"]


class KeyAuthorizationNotConfigured(Exception):
    """Raised when this deployment offers no API key authorization: no callback is listed."""


class InvalidKeyAuthorizationRequest(Exception):
    """Raised when an authorization request is not acceptable.

    The message says which part was wrong. It goes to the log only: callers
    answer with one generic message, so a refusal is not an oracle.
    """


@dataclass(frozen=True)
class Callback:
    """A callback URL, parsed the way the allowlist compares it."""

    #: The URL as given, query included.
    url: str
    scheme: str
    #: Lower-cased.
    hostname: str
    #: The effective port: the scheme's default when none is written.
    port: int
    #: ``/`` when the URL has none.
    path: str
    #: The host as a browser shows it (``URL.host``): the host name, with the
    #: port only when it is not the scheme's default. What the consent page
    #: names the application by, and what its key is called.
    host: str

    @property
    def target(self) -> tuple[str, str, int, str]:
        """Scheme, host, port and path: what must equal an allowlist entry."""
        return (self.scheme, self.hostname, self.port, self.path)

    @property
    def without_query(self) -> str:
        """The URL up to its query string."""
        return self.url.split("?", 1)[0]


def parse_callback(url: str, *, allow_query: bool = True) -> Callback:
    """Parse a callback URL, refusing anything a browser could read differently.

    Args:
        url: The callback URL.
        allow_query: Whether a query string is allowed. Allowlist entries have
            none; a callback's own query is the application's ``state``.

    Returns:
        The parsed callback.

    Raises:
        ValueError: The URL is not acceptable as a callback; the message says why.
    """
    if not isinstance(url, str) or not url:
        raise ValueError("must be a non-empty string")
    if len(url) > MAX_CALLBACK_LENGTH:
        raise ValueError(f"must be at most {MAX_CALLBACK_LENGTH} characters")
    # A browser strips tab and newline before parsing and treats a backslash as
    # a slash: refusing them, and anything outside printable ASCII, leaves one
    # reading of the URL rather than a model of how each parser rewrites it.
    if any(not 0x21 <= ord(char) <= 0x7E for char in url):
        raise ValueError("must be printable ASCII without spaces")
    if "\\" in url:
        raise ValueError("must not contain a backslash")
    if "#" in url:
        raise ValueError("must not have a fragment")
    parts = urlsplit(url)
    scheme = parts.scheme
    if scheme not in _DEFAULT_PORTS or url[len(scheme) : len(scheme) + 3] != "://":
        raise ValueError("must be an absolute https URL")
    if "@" in parts.netloc:
        raise ValueError("must not carry user information")
    hostname = parts.hostname or ""
    if not hostname or not all(_LABEL.fullmatch(label) for label in hostname.split(".")):
        raise ValueError("must name a host of letters, digits, hyphens and dots")
    try:
        explicit_port = parts.port
    except ValueError as exc:
        raise ValueError("has an invalid port") from exc
    if scheme == "http" and hostname not in _LOOPBACK_HOSTS:
        raise ValueError("must use https (http is accepted for localhost only)")
    query = parts.query
    if not allow_query and (query or "?" in url):
        raise ValueError("must not have a query string")
    if any(name in _RESPONSE_PARAMETERS for name, _ in parse_qsl(query, keep_blank_values=True)):
        raise ValueError("must not carry a code or error parameter of its own")
    default_port = _DEFAULT_PORTS[scheme]
    port = explicit_port if explicit_port is not None else default_port
    host = hostname if port == default_port else f"{hostname}:{port}"
    return Callback(
        url=url,
        scheme=scheme,
        hostname=hostname,
        port=port,
        path=parts.path or "/",
        host=host,
    )


def _entries(raw: str) -> list[str]:
    """The comma-separated items of a callback list, trimmed, blanks dropped."""
    return [item.strip() for item in raw.split(",") if item.strip()]


def validate_callbacks_setting(raw: str) -> None:
    """Refuse a value of :data:`ENV_CALLBACKS` with an entry that can never match.

    The registry's validator: Admin refuses the value rather than storing an
    entry the gateway would ignore.

    Raises:
        ValueError: An entry is not an acceptable callback URL.
    """
    for number, item in enumerate(_entries(raw), start=1):
        try:
            parse_callback(item, allow_query=False)
        except ValueError as exc:
            raise ValueError(f"callback {number} {exc}") from None


def allowed_callbacks() -> frozenset[tuple[str, str, int, str]]:
    """Return the scheme, host, port and path of every configured callback.

    An entry that does not parse is skipped and logged; the validator keeps
    Admin from storing one, but an environment value is not validated there.
    """
    targets = set()
    for item in _entries(config_value(ENV_CALLBACKS) or ""):
        try:
            targets.add(parse_callback(item, allow_query=False).target)
        except ValueError as exc:
            logger.warning(
                "api_key_authorization_callback_ignored",
                extra={"reason": f"{ENV_CALLBACKS} has an entry that {exc}"},
            )
    return frozenset(targets)


def is_configured() -> bool:
    """Whether this deployment offers API key authorization at all."""
    return bool(allowed_callbacks())


def check_request(*, callback_url: str, code_challenge: str, method: str) -> Callback:
    """Check an authorization request against the configuration.

    Args:
        callback_url: Where the application wants the answer delivered.
        code_challenge: The application's PKCE challenge.
        method: The PKCE challenge method.

    Returns:
        The accepted callback.

    Raises:
        KeyAuthorizationNotConfigured: No callback is configured.
        InvalidKeyAuthorizationRequest: The request is not acceptable.
    """
    allowed = allowed_callbacks()
    if not allowed:
        raise KeyAuthorizationNotConfigured(f"{ENV_CALLBACKS} lists no callback")
    if method != CHALLENGE_METHOD:
        raise InvalidKeyAuthorizationRequest(f"code_challenge_method must be {CHALLENGE_METHOD}")
    if not isinstance(code_challenge, str) or not _CHALLENGE.fullmatch(code_challenge):
        raise InvalidKeyAuthorizationRequest(
            "code_challenge must be 43 characters of unpadded base64url"
        )
    try:
        callback = parse_callback(callback_url)
    except ValueError as exc:
        raise InvalidKeyAuthorizationRequest(f"callback_url {exc}") from exc
    # Equality of scheme, host, port and path, never a prefix: a prefix test
    # admits https://app.example.org.attacker.test, which is the one an
    # attacker would register.
    if callback.target not in allowed:
        raise InvalidKeyAuthorizationRequest("callback_url is not on the allowlist")
    return callback


def allowed_callback(url: str) -> Callback | None:
    """Return *url* parsed when it is still an allowed callback, else ``None``.

    For the exchange, which re-checks the callback a code was issued for: an
    entry removed meanwhile must stop its outstanding codes too.
    """
    try:
        callback = parse_callback(url)
    except ValueError:
        return None
    return callback if callback.target in allowed_callbacks() else None


def valid_verifier(verifier: str) -> bool:
    """Whether *verifier* is a well-formed PKCE code verifier."""
    return isinstance(verifier, str) and bool(_VERIFIER.fullmatch(verifier))


def with_parameter(url: str, name: str, value: str) -> str:
    """Append one query parameter to *url*, leaving what is there untouched.

    The application's own query (its ``state``) must come back exactly as it
    sent it, so nothing is re-encoded: the pair is added with ``?`` when there
    is no query yet and with ``&`` otherwise. Callbacks never have a fragment.
    """
    pair = f"{quote(name, safe='')}={quote(value, safe='')}"
    if "?" not in url:
        return f"{url}?{pair}"
    if url.endswith(("?", "&")):
        return f"{url}{pair}"
    return f"{url}&{pair}"


def account_refusal(user: Mapping[str, Any] | None) -> AccountRefusal | None:
    """Return why *user* may not authorize an API key, or ``None`` when it may.

    The account must be active and must have proven its address: an email
    address it has must be verified, and a phone-only account's phone. This is
    stricter than the sign-in gate, which waives the email check while the
    deployment does not require verification: the key is handed to another
    application, so the account it is made for must be one whose owner has
    been reached.

    Args:
        user: A user row, or the current-user context of a session.
    """
    if user is None or user.get("status") != "active":
        return "inactive"
    if (user.get("email") and not user.get("email_verified")) or phone_identity_unverified(
        dict(user)
    ):
        return "unverified"
    return None


def key_notes(callback: Callback) -> str:
    """The note stored on a key created by authorization."""
    return f"Created by API key authorization for {callback.host}"


def key_metadata(callback: Callback) -> dict[str, str]:
    """The metadata stored on a key created by authorization."""
    return {"created_by": "api_key_authorization", "callback_host": callback.host}
