"""Device cookies: recognize a browser that has signed in to an account before.

OWASP's "Slow down online guessing attacks with device cookies". After a
successful password sign-in the browser is given a signed entry naming the
account, and a later sign-in to that account presenting it is rate limited on
its own window (see :func:`serving.utils.login_rate_limit.check_and_record_login`),
apart from every other client. Without one, whoever knows a login name can keep
the account locked out wherever the backend cannot tell clients apart -- every
browser behind the console's proxy can share one client address -- while with
one, the administrator's own browser keeps signing in.

The cookie is stateless. It holds up to :data:`MAX_ENTRIES` entries, newest
first, one per account signed in on the browser. An entry is a random nonce, an
expiry and, for each of the account's sign-in identifiers (its email address
and its login name, whichever exist, normalized as the login limiter normalizes
them), the HMAC-SHA256 of nonce, expiry and identifier under a key derived from
``JWT_SECRET_KEY`` for this use alone. Only the backend can make one, so an
entry the backend did not issue, an expired one or one for another account is
passed over without a word, and that sign-in is limited like any other. The
nonce keys the entry's rate-limit window.

An entry carries no authority beyond that window -- the password is checked
exactly as always -- so nothing revokes one. Signing out leaves the cookie in
place: a signed-out administrator is still recognized, which is the point.
Neither a password reset nor deleting the account removes entries either; they
expire on their own (:data:`LIFETIME_SECONDS`). Changing ``JWT_SECRET_KEY``
invalidates every entry, and those browsers share the address windows again
until their next sign-in.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import secrets
import struct
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from serving.config.settings import settings
from serving.utils.jwt import get_jwt_secret

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

    from fastapi import Request, Response

#: Entries one cookie keeps: as many accounts as one browser may hold
#: (``serving.auth.browser_sessions.MAX_ACCOUNTS_PER_BROWSER``).
MAX_ENTRIES = 5
#: How long an entry, and the cookie, last after the sign-in that issued them.
LIFETIME_SECONDS = 180 * 24 * 60 * 60
#: Sent to the sign-in endpoints only, never with any other request.
COOKIE_PATH = "/auth"

# Domain separation: no other use of JWT_SECRET_KEY computes this HMAC.
_KEY_LABEL = b"hybridinference/login-device-cookie/v1"
_NONCE_BYTES = 16
_TAG_BYTES = hashlib.sha256().digest_size
_EXPIRY = struct.Struct(">I")
# An account signs in with an email address, a login name, or either of both.
_MAX_TAGS = 2
_HEADER_BYTES = _NONCE_BYTES + _EXPIRY.size
# Unpadded base64url of the largest entry: four characters per three bytes.
_MAX_ENTRY_CHARS = -(-(_HEADER_BYTES + _MAX_TAGS * _TAG_BYTES) * 4 // 3)


@dataclass(frozen=True)
class _Entry:
    nonce: bytes
    expires: int
    tags: tuple[bytes, ...]


def device_cookie_name() -> str:
    """Return the cookie's name, the session cookie's with ``_device`` added.

    Two deployments on one host already give their session cookies different
    names (``REFRESH_TOKEN_COOKIE_NAME``), and so keep their device cookies apart.
    """
    return f"{settings.refresh_token_cookie_name}_device"


def _now() -> int:
    return int(time.time())


def _key() -> bytes | None:
    """Derive the entry key from ``JWT_SECRET_KEY``, or None when it is not set."""
    try:
        secret = get_jwt_secret()
    except ValueError:
        return None
    return hmac.new(secret.encode(), _KEY_LABEL, hashlib.sha256).digest()


def _normalize(identifier: str) -> str:
    return identifier.strip().lower()


def _tag(key: bytes, nonce: bytes, expires: int, identifier: str) -> bytes:
    # Nonce and expiry are fixed-length, so the identifier is the remainder.
    message = nonce + _EXPIRY.pack(expires) + identifier.encode()
    return hmac.new(key, message, hashlib.sha256).digest()


def _matches(key: bytes, entry: _Entry, identifier: str) -> bool:
    """Whether *entry* was issued for *identifier*, compared in constant time."""
    expected = _tag(key, entry.nonce, entry.expires, identifier)
    matched = False
    for tag in entry.tags:
        matched |= hmac.compare_digest(expected, tag)
    return matched


def _encode(entry: _Entry) -> str:
    raw = entry.nonce + _EXPIRY.pack(entry.expires) + b"".join(entry.tags)
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _decode(text: str) -> _Entry | None:
    """Parse one entry, or return None when it is not one."""
    if not text or len(text) > _MAX_ENTRY_CHARS:
        return None
    try:
        raw = base64.b64decode(text + "=" * (-len(text) % 4), altchars=b"-_", validate=True)
    except (binascii.Error, ValueError):
        return None
    tags = raw[_HEADER_BYTES:]
    if not tags or len(tags) % _TAG_BYTES or len(tags) > _MAX_TAGS * _TAG_BYTES:
        return None
    (expires,) = _EXPIRY.unpack_from(raw, _NONCE_BYTES)
    return _Entry(
        nonce=raw[:_NONCE_BYTES],
        expires=expires,
        tags=tuple(tags[i : i + _TAG_BYTES] for i in range(0, len(tags), _TAG_BYTES)),
    )


def _entries(value: str | None) -> Iterator[_Entry]:
    """Yield the well-formed entries of a cookie value, reading at most MAX_ENTRIES."""
    if not value:
        return
    for text in value.split(".", MAX_ENTRIES)[:MAX_ENTRIES]:
        entry = _decode(text)
        if entry is not None:
            yield entry


def recognized_device(request: Request, identifier: str) -> str | None:
    """Return the rate-limit key of the request's valid entry for *identifier*, if any.

    *identifier* is what the client is signing in with, an email address or a
    login name. Returns the entry's nonce, hex-encoded, or None when the
    request carries no unexpired entry the backend issued for that identifier.
    """
    value = request.cookies.get(device_cookie_name())
    key = _key() if value else None
    if key is None:
        return None
    now = _now()
    wanted = _normalize(identifier)
    for entry in _entries(value):
        if entry.expires > now and _matches(key, entry, wanted):
            return entry.nonce.hex()
    return None


def remember_device(
    response: Response, account: Mapping[str, Any], request: Request | None = None
) -> None:
    """Set the cookie with a fresh entry for *account* ahead of the browser's others.

    Called on every successful sign-in, so the account's entry is rotated: a
    new nonce and a new expiry replace any entry the request carried for it.
    The request's other unexpired entries follow, newest first, up to
    :data:`MAX_ENTRIES`. Without *request* the cookie holds the new entry
    alone. Nothing is set for an account with neither an email address nor a
    login name, or while ``JWT_SECRET_KEY`` is unset.
    """
    identifiers = list(
        dict.fromkeys(
            _normalize(value)
            for value in (account.get("email"), account.get("login_name"))
            if value and value.strip()
        )
    )
    key = _key()
    if not identifiers or key is None:
        return
    now = _now()
    expires = now + LIFETIME_SECONDS
    nonce = secrets.token_bytes(_NONCE_BYTES)
    entries = [_Entry(nonce, expires, tuple(_tag(key, nonce, expires, i) for i in identifiers))]
    if request is not None:
        for entry in _entries(request.cookies.get(device_cookie_name())):
            if entry.expires > now and not any(_matches(key, entry, i) for i in identifiers):
                entries.append(entry)
    response.set_cookie(
        key=device_cookie_name(),
        value=".".join(_encode(entry) for entry in entries[:MAX_ENTRIES]),
        max_age=LIFETIME_SECONDS,
        expires=datetime.now(timezone.utc) + timedelta(seconds=LIFETIME_SECONDS),
        path=COOKIE_PATH,
        domain=settings.cookie_domain,
        secure=settings.cookie_secure,
        httponly=True,
        samesite=settings.cookie_samesite,
    )
