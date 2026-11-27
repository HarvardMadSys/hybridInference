"""A second factor for an account: a code from an authenticator app.

A password and a session are both things that can be copied: the password from
wherever it was typed or stored, the session from the browser that holds it.
A time-based one-time password (TOTP, RFC 6238) is a third thing, kept on a
device the account's owner carries: the gateway and the authenticator app share
a secret once, at enrollment, and from then on each derives the same six digits
from that secret and the current half-minute. Any authenticator app reads the
enrollment QR code (Duo Mobile, Google Authenticator, 1Password, ...); no
outside service takes part.

This module is the arithmetic and the policy that needs no database:

- the codes themselves (:func:`totp_at`, :func:`matching_step`) and the
  enrollment link an app scans (:func:`provisioning_uri`);
- recovery codes, for an owner whose device is gone;
- how the shared secret is kept at rest (:func:`encrypt_secret`);
- what a session's *environment* is (:func:`client_environment`), so a session
  verified in one browser is not taken as verified when it turns up in another
  client;
- what a session's verification is worth right now (:class:`SessionTwoFactor`).

The rows live in ``user_two_factor``, ``two_factor_recovery_codes`` and two
columns of ``auth_sessions`` (migration ``0019_two_factor``); the routes are in
``serving.servers.routers.two_factor_routes``. Operators: ``docs/developer/deployment.md``
(Requiring two-factor authentication of administrators).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from cryptography.fernet import Fernet, InvalidToken

from serving.config.settings import get_settings
from serving.exceptions import TwoFactorRequiredError

if TYPE_CHECKING:
    from fastapi import Request

#: RFC 6238 parameters. These are the ones every authenticator app assumes
#: when the enrollment link names none, so the link names none.
TOTP_DIGITS = 6
TOTP_PERIOD_SECONDS = 30
#: Steps either side of the current one a code is accepted for: a phone's
#: clock that is half a minute off, or a code typed as it rolls over.
TOTP_SKEW_STEPS = 1
#: 160 bits, the size RFC 4226 recommends for the HMAC-SHA-1 key.
_SECRET_BYTES = 20

#: Wrong codes in a row before the account's codes are held off, and for how
#: long: the first hold, doubling with each further run, up to the ceiling.
#: Three of a million codes are valid at any moment, so what protects six
#: digits is how many guesses a holder of the session gets. These allow five,
#: then five more a quarter-hour later, and from the second day five a day:
#: about 180 guesses over a thirty-day session, one chance in two thousand.
LOCK_EVERY_ATTEMPTS = 5
LOCK_SECONDS = 15 * 60
MAX_LOCK_SECONDS = 24 * 60 * 60

#: How long a secret that was shown waits for its first code, and how many
#: codes may be tried at it. Confirming a setup hands over the account's second
#: factor and its recovery codes, so the same arithmetic applies as above: a
#: setup that could be guessed at without limit would be worth waiting for.
#: Every new setup costs the password or a fresh code, and buys five tries.
SETUP_SECONDS = 10 * 60
SETUP_ATTEMPTS = 5

RECOVERY_CODE_COUNT = 10
#: Two groups of five base32 characters: 50 bits each, which a hash protects
#: without a salt or a slow function, since there is nothing to guess from.
_RECOVERY_GROUP_CHARS = 5
_RECOVERY_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I

_SECRET_KEY_CONTEXT = b"two-factor-secret-v1\x00"


def generate_secret() -> str:
    """Return a new shared secret, base32 without padding as apps expect it."""
    return base64.b32encode(secrets.token_bytes(_SECRET_BYTES)).decode().rstrip("=")


def _secret_bytes(secret: str) -> bytes:
    padded = secret.upper() + "=" * (-len(secret) % 8)
    return base64.b32decode(padded)


def current_step(now: float | None = None) -> int:
    """The half-minute *now* falls in, counted from the Unix epoch."""
    return int((time.time() if now is None else now) // TOTP_PERIOD_SECONDS)


def totp_at(secret: str, step: int) -> str:
    """The code *secret* yields in time step *step* (RFC 6238 over RFC 4226)."""
    digest = hmac.new(_secret_bytes(secret), struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(value % 10**TOTP_DIGITS).zfill(TOTP_DIGITS)


def matching_step(
    secret: str, code: str, *, now: float | None = None, after_step: int | None = None
) -> int | None:
    """Return the time step *code* is valid for, or None.

    Steps at or before *after_step* are not considered: a code is good once,
    so one read over a shoulder or off the wire cannot be entered again inside
    the same minute. Every candidate is compared, in constant time each, so
    the answer's timing does not say which step came close.
    """
    if not looks_like_totp(code):
        return None
    step_now = current_step(now)
    matched: int | None = None
    for step in range(step_now - TOTP_SKEW_STEPS, step_now + TOTP_SKEW_STEPS + 1):
        if hmac.compare_digest(totp_at(secret, step), code) and (
            after_step is None or step > after_step
        ):
            matched = step
    return matched


def looks_like_totp(code: str) -> bool:
    """Whether *code* has the shape of an authenticator code, not a recovery code."""
    return len(code) == TOTP_DIGITS and code.isascii() and code.isdigit()


def normalize_code(raw: str) -> str:
    """Drop the spaces and dashes people and apps put into a code."""
    return "".join(ch for ch in raw.strip() if ch not in " -").upper()


def provisioning_uri(secret: str, *, account: str, issuer: str) -> str:
    """The ``otpauth://`` link an authenticator app reads from a QR code.

    The issuer appears both as the label prefix and as a parameter, which is
    what the Key URI format asks for and what keeps older apps showing it.
    """
    label = quote(f"{issuer}:{account}", safe="")
    return f"otpauth://totp/{label}?secret={secret}&issuer={quote(issuer, safe='')}"


def generate_recovery_codes() -> list[str]:
    """Return a fresh set of one-time recovery codes, as shown to the owner."""
    return [
        "-".join(
            "".join(secrets.choice(_RECOVERY_ALPHABET) for _ in range(_RECOVERY_GROUP_CHARS))
            for _ in range(2)
        )
        for _ in range(RECOVERY_CODE_COUNT)
    ]


def hash_recovery_code(code: str) -> str:
    """Hash a recovery code for storage; only the hash is ever kept."""
    return hashlib.sha256(b"two-factor-recovery-v1\x00" + normalize_code(code).encode()).hexdigest()


def _secret_cipher() -> Fernet:
    # Derived from API_KEY_SECRET under its own label, so a value encrypted
    # here is not one the saved-API-key cipher can open, and the reverse.
    master = get_settings().api_key_secret.encode()
    if not master:
        raise ValueError("API_KEY_SECRET must be set in environment")
    key = hashlib.sha256(_SECRET_KEY_CONTEXT + master).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt_secret(secret: str) -> str:
    """Encrypt a shared secret for storage.

    It cannot be hashed: the gateway needs the secret itself to compute the
    code it compares against. A copy of the database alone therefore does not
    yield working codes; the deployment's ``API_KEY_SECRET`` is needed too.
    """
    return _secret_cipher().encrypt(secret.encode()).decode()


def decrypt_secret(encrypted: str) -> str | None:
    """Return the shared secret, or None when it cannot be opened.

    None after ``API_KEY_SECRET`` changed: the account's codes can no longer
    be checked, which the caller answers as a wrong code, never as a pass.
    """
    try:
        return _secret_cipher().decrypt(encrypted.encode()).decode()
    except (InvalidToken, ValueError):
        return None


# -- the environment a session was verified in --------------------------------

# Checked in order: a browser's User-Agent names the engines it descends from
# (Edge and Opera say "Chrome" and "Safari", Chrome says "Safari"), so the most
# specific token has to be asked about first.
_CLIENT_TOKENS: tuple[tuple[str, str], ...] = (
    ("edg/", "edge"),
    ("edga/", "edge"),
    ("edgios/", "edge"),
    ("opr/", "opera"),
    ("firefox/", "firefox"),
    ("fxios/", "firefox"),
    ("chrome/", "chrome"),
    ("crios/", "chrome"),
    ("safari/", "safari"),
    ("curl/", "curl"),
    ("wget/", "wget"),
    ("python-requests/", "python"),
    ("python-httpx/", "python"),
    ("aiohttp/", "python"),
    ("python-urllib/", "python"),
    ("node", "node"),
    ("undici", "node"),
    ("axios/", "node"),
    ("go-http-client/", "go"),
    ("okhttp/", "java"),
    ("java/", "java"),
    ("postmanruntime/", "postman"),
)
_PLATFORM_TOKENS: tuple[tuple[str, str], ...] = (
    ("iphone", "ios"),
    ("ipad", "ios"),
    ("android", "android"),
    ("windows", "windows"),
    ("cros", "chromeos"),
    ("mac os x", "macos"),
    ("macintosh", "macos"),
    ("linux", "linux"),
)
_UNKNOWN = "other"


def client_environment(user_agent: str | None) -> str:
    """Name the kind of client a request came from: ``chrome/macos``, ``curl/other``.

    The family of the client and of its platform, without versions, so a
    browser that updates itself is still the same environment while a session
    that moves from a browser to a script is not. A User-Agent is whatever the
    caller sends: this tells a copied session from its owner's only while the
    copier does not also copy the header, and is one check among several, not
    proof of anything.
    """
    agent = (user_agent or "").lower()
    client = next((name for token, name in _CLIENT_TOKENS if token in agent), _UNKNOWN)
    platform = next((name for token, name in _PLATFORM_TOKENS if token in agent), _UNKNOWN)
    return f"{client}/{platform}"


# -- what a session's verification is worth -----------------------------------

#: Why a request is refused for want of a code. ``enrollment`` is the only one
#: a code cannot answer: the account has no authenticator yet.
REASON_ENROLLMENT = "enrollment"
REASON_UNVERIFIED = "unverified"
REASON_ENVIRONMENT = "environment_changed"
REASON_STALE = "stale"


@dataclass(frozen=True)
class SessionTwoFactor:
    """An account's second factor and what one of its sessions has proved.

    Attributes:
        enrolled: The account has a confirmed authenticator.
        verified_at: When this session last gave a code; None if never.
        environment: The :func:`client_environment` it gave that code from.
    """

    enrolled: bool = False
    verified_at: datetime | None = None
    environment: str | None = None

    @classmethod
    def from_row(cls, row: Any) -> SessionTwoFactor:
        """Read the store's answer; anything that is not a row is "nothing proved"."""
        if not isinstance(row, Mapping):
            return cls()
        verified_at = row.get("verified_at")
        environment = row.get("environment")
        return cls(
            enrolled=row.get("enrolled") is True,
            verified_at=verified_at if isinstance(verified_at, datetime) else None,
            environment=environment if isinstance(environment, str) else None,
        )

    def refusal(
        self, environment: str, *, within: timedelta | None = None, now: datetime | None = None
    ) -> str | None:
        """Return why this session may not proceed from *environment*, or None.

        With *within*, the code must also be that recent: what a change that
        outlasts the session asks for, where signing in asks only that the
        session gave a code once.
        """
        if not self.enrolled:
            return REASON_ENROLLMENT
        if self.verified_at is None:
            return REASON_UNVERIFIED
        if self.environment != environment:
            return REASON_ENVIRONMENT
        if within is not None and (now or datetime.now(UTC)) - self.verified_at > within:
            return REASON_STALE
        return None


def has_authenticator(row: Any) -> bool:
    """Whether a ``user_two_factor`` row is a confirmed authenticator.

    False for no row, and for one that has only been shown a secret.
    """
    return isinstance(row, Mapping) and row.get("confirmed_at") is not None


def admin_two_factor_required() -> bool:
    """Whether this deployment gives administrator access only to a verified session."""
    return bool(get_settings().admin_two_factor_required)


def step_up_window() -> timedelta:
    """How long a code keeps answering for changes that outlast the session."""
    return timedelta(minutes=get_settings().two_factor_step_up_minutes)


def request_environment(request: Request) -> str:
    """The :func:`client_environment` of the client that sent *request*."""
    return client_environment(request.headers.get("user-agent"))


async def read_session_two_factor(op_store: Any, user_id: str, sid: str | None) -> SessionTwoFactor:
    """What session *sid* of *user_id* has proved; nothing, for a token without one."""
    if not sid:
        return SessionTwoFactor()
    return SessionTwoFactor.from_row(await op_store.get_session_two_factor(user_id, sid))


async def require_session_two_factor(
    request: Request,
    op_store: Any,
    user_id: str,
    sid: str | None,
    *,
    within: timedelta | None = None,
    action: str | None = None,
) -> None:
    """Raise unless this session has given a code, from the client now using it.

    With *within*, the code must also be that recent.

    Raises:
        TwoFactorRequiredError: 403 naming what is missing, which the console
            answers by asking for a code (or for enrollment) and retrying.
    """
    state = await read_session_two_factor(op_store, user_id, sid)
    reason = state.refusal(request_environment(request), within=within)
    if reason is not None:
        raise TwoFactorRequiredError(reason, action)
