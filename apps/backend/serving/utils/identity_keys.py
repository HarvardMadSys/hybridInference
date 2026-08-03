"""RS256 identity keys for cross-service SSO.

The gateway is the identity issuer for detached services — first of them the
cloud agent, which lives in its own repository and must not share a database or
a signing secret with us. Those services verify our tokens with the **public**
half published at ``/v1/identity/jwks``; only the gateway ever holds the private
half.

That is the whole reason this is RS256 rather than the HS256 used for ordinary
session tokens (``serving.utils.jwt``): a symmetric key cannot be published, and
handing a copy to every consumer would make each of them able to mint tokens as
us.

The key ID is an RFC 7638 JWK thumbprint rather than a configured string, so it
is derived from the key itself: stable across restarts and redeploys, and
guaranteed to change when the key does. A consumer that caches by ``kid`` picks
up a rotation without being told about it.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

#: Below this an RSA signature is not worth the bytes it travels in.
MIN_KEY_SIZE_BITS = 2048

ENV_PRIVATE_KEY = "IDENTITY_JWT_PRIVATE_KEY"

ALGORITHM = "RS256"

#: Parsed keys by PEM digest. Parsing RSA keys is expensive enough to matter on
#: a per-request endpoint, and the digest keeps the PEM itself out of the key.
_CACHE: dict[str, tuple[rsa.RSAPrivateKey, dict[str, Any]]] = {}


class IdentityKeyUnavailable(Exception):
    """Raised when the identity signing key is unset, malformed, or unusable.

    Callers map this to "identity is not configured on this deployment" — a
    404 from the JWKS endpoint — never to a 500. A gateway without cross-service
    identity configured is a valid deployment, not a broken one.
    """


def _b64url(raw: bytes) -> str:
    """Base64url-encode without padding, as JOSE requires."""
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64url_uint(value: int) -> str:
    """Base64url-encode a non-negative integer as a JOSE big-endian octet string."""
    length = (value.bit_length() + 7) // 8 or 1
    return _b64url(value.to_bytes(length, "big"))


def _thumbprint(modulus: str, exponent: str) -> str:
    """Return the RFC 7638 thumbprint of an RSA public key.

    Args:
        modulus: The base64url-encoded modulus (``n``).
        exponent: The base64url-encoded public exponent (``e``).

    Returns:
        The base64url-encoded SHA-256 thumbprint, used as the ``kid``.
    """
    # RFC 7638 is exact about this: only the required members, lexicographic
    # order, no whitespace. Any deviation yields a different — and wrong — kid.
    canonical = json.dumps(
        {"e": exponent, "kty": "RSA", "n": modulus},
        separators=(",", ":"),
        sort_keys=True,
    )
    return _b64url(hashlib.sha256(canonical.encode()).digest())


def _parse(pem: str) -> tuple[rsa.RSAPrivateKey, dict[str, Any]]:
    """Parse a PEM private key and derive its public JWK.

    Args:
        pem: PEM-encoded RSA private key.

    Returns:
        The parsed private key and its public JWK.

    Raises:
        IdentityKeyUnavailable: If the PEM is malformed, not RSA, or too small.
    """
    try:
        key = serialization.load_pem_private_key(pem.encode(), password=None)
    except (ValueError, TypeError) as exc:
        raise IdentityKeyUnavailable(
            f"{ENV_PRIVATE_KEY} is not a readable PEM private key"
        ) from exc

    if not isinstance(key, rsa.RSAPrivateKey):
        raise IdentityKeyUnavailable(
            f"{ENV_PRIVATE_KEY} must be an RSA key for {ALGORITHM}, got {type(key).__name__}"
        )
    if key.key_size < MIN_KEY_SIZE_BITS:
        raise IdentityKeyUnavailable(
            f"{ENV_PRIVATE_KEY} is {key.key_size} bits; minimum is {MIN_KEY_SIZE_BITS}"
        )

    numbers = key.public_key().public_numbers()
    modulus = _b64url_uint(numbers.n)
    exponent = _b64url_uint(numbers.e)
    jwk = {
        "kty": "RSA",
        "use": "sig",
        "alg": ALGORITHM,
        "kid": _thumbprint(modulus, exponent),
        "n": modulus,
        "e": exponent,
    }
    return key, jwk


def _load() -> tuple[rsa.RSAPrivateKey, dict[str, Any]]:
    """Load the configured key, using the parse cache.

    Returns:
        The parsed private key and its public JWK.

    Raises:
        IdentityKeyUnavailable: If the key is unset or unusable.
    """
    pem = os.environ.get(ENV_PRIVATE_KEY, "").strip()
    if not pem:
        raise IdentityKeyUnavailable(f"{ENV_PRIVATE_KEY} is not set")

    digest = hashlib.sha256(pem.encode()).hexdigest()
    cached = _CACHE.get(digest)
    if cached is None:
        cached = _parse(pem)
        _CACHE[digest] = cached
    return cached


def identity_enabled() -> bool:
    """Report whether a usable identity signing key is configured."""
    try:
        _load()
    except IdentityKeyUnavailable:
        return False
    return True


def signing_key() -> rsa.RSAPrivateKey:
    """Return the private key used to sign identity tokens.

    Returns:
        The configured RSA private key.

    Raises:
        IdentityKeyUnavailable: If the key is unset or unusable.
    """
    return _load()[0]


def signing_kid() -> str:
    """Return the key ID to place in the JWT header.

    Returns:
        The RFC 7638 thumbprint of the signing key.

    Raises:
        IdentityKeyUnavailable: If the key is unset or unusable.
    """
    return _load()[1]["kid"]


def public_jwks() -> dict[str, list[dict[str, Any]]]:
    """Return the JWKS document served to identity consumers.

    Returns:
        A JWKS containing only the public half of the signing key.

    Raises:
        IdentityKeyUnavailable: If the key is unset or unusable.
    """
    # dict(...) so a consumer mutating the response cannot poison the cache.
    return {"keys": [dict(_load()[1])]}


def reset_cache() -> None:
    """Drop parsed keys. For tests that swap the configured key."""
    _CACHE.clear()
