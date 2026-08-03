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
guaranteed to change when the key does.

**Rotation.** One signing key, plus any number of retiring keys published for
verification only. A consumer holding a token signed before the rotation can
still verify it, which a single-key JWKS cannot offer — swapping the key there
invalidates every unexpired token at once, and the failure looks like a broken
signature rather than a rotation. Retiring keys are configured as *public* PEMs,
so retiring a key removes its ability to sign in the same act that keeps it able
to verify.

Keep a key in ``IDENTITY_JWT_RETIRING_PUBLIC_KEYS`` for at least the maximum
token lifetime plus the JWKS cache window, then drop it.

**Unset and broken are different answers.** No key at all means this deployment
does not offer cross-service identity — a valid configuration, reported as 404.
A key that is present but unusable is a misconfiguration, and must be loud:
conflating the two lets a production deploy with a mangled PEM look like a
feature that was simply never switched on.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from typing import Any

from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

#: Below this an RSA signature is not worth the bytes it travels in.
MIN_KEY_SIZE_BITS = 2048

ENV_PRIVATE_KEY = "IDENTITY_JWT_PRIVATE_KEY"
ENV_RETIRING_PUBLIC_KEYS = "IDENTITY_JWT_RETIRING_PUBLIC_KEYS"

ALGORITHM = "RS256"

#: PEM armour is self-delimiting, so several keys concatenated in one variable
#: split cleanly on this. That is also how every PEM bundle on disk looks.
_PEM_BEGIN = "-----BEGIN"

#: Parsed keys by PEM digest. Parsing RSA keys is expensive enough to matter on
#: a per-request endpoint, and the digest keeps the PEM itself out of the key.
_PRIVATE_CACHE: dict[str, tuple[rsa.RSAPrivateKey, dict[str, Any]]] = {}
_PUBLIC_CACHE: dict[str, dict[str, Any]] = {}


class IdentityKeyUnavailable(Exception):
    """Raised when no identity signing key is configured at all.

    Means "this deployment does not offer cross-service identity", which is a
    valid configuration and is reported as 404. Never used for a key that is
    present but unusable — see :class:`IdentityKeyMisconfigured`.
    """


class IdentityKeyMisconfigured(Exception):
    """Raised when a configured identity key cannot be used.

    A malformed PEM, a non-RSA key, an undersized key, an encrypted key, or an
    algorithm this build of ``cryptography`` does not support. This is an
    operator error and is reported as a server error, deliberately not as the
    404 that means "not offered here".
    """


def _b64url(raw: bytes) -> str:
    """Base64url-encode without padding, as JOSE requires."""
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64url_uint(value: int) -> str:
    """Base64url-encode a non-negative integer as a JOSE big-endian octet string."""
    length = (value.bit_length() + 7) // 8 or 1
    return _b64url(value.to_bytes(length, "big"))


def _normalize(pem: str) -> str:
    r"""Return a PEM with escaped newlines restored.

    A key pasted into an environment variable usually arrives with literal
    ``\n`` sequences, which PEM parsing rejects in a way that is tedious to
    diagnose. ``AppConfig.from_env`` in ``serving.agent_jobs.github_app`` does
    the same for the GitHub App key, so this is an established provisioning
    shape in this deployment rather than a convenience.
    """
    return pem.replace("\\n", "\n").strip()


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


def _jwk_from_public(public: rsa.RSAPublicKey) -> dict[str, Any]:
    """Build the published JWK for an RSA public key."""
    numbers = public.public_numbers()
    modulus = _b64url_uint(numbers.n)
    exponent = _b64url_uint(numbers.e)
    return {
        "kty": "RSA",
        "use": "sig",
        "alg": ALGORITHM,
        "kid": _thumbprint(modulus, exponent),
        "n": modulus,
        "e": exponent,
    }


def _require_usable(key: Any, *, env: str) -> None:
    """Reject keys that are the wrong type or too small.

    Raises:
        IdentityKeyMisconfigured: If the key cannot be used for RS256.
    """
    if not isinstance(key, rsa.RSAPrivateKey | rsa.RSAPublicKey):
        raise IdentityKeyMisconfigured(
            f"{env} must be an RSA key for {ALGORITHM}, got {type(key).__name__}"
        )
    if key.key_size < MIN_KEY_SIZE_BITS:
        raise IdentityKeyMisconfigured(
            f"{env} is {key.key_size} bits; minimum is {MIN_KEY_SIZE_BITS}"
        )


def _parse_private(pem: str) -> tuple[rsa.RSAPrivateKey, dict[str, Any]]:
    """Parse a PEM private key and derive its public JWK.

    Raises:
        IdentityKeyMisconfigured: If the PEM is unusable.
    """
    try:
        key = serialization.load_pem_private_key(pem.encode(), password=None)
    except (ValueError, TypeError, UnsupportedAlgorithm) as exc:
        # TypeError covers an encrypted key handed a null password, and
        # UnsupportedAlgorithm an algorithm this build cannot load. Both are
        # operator errors, not "identity is switched off".
        raise IdentityKeyMisconfigured(
            f"{ENV_PRIVATE_KEY} is not a usable PEM private key"
        ) from exc

    _require_usable(key, env=ENV_PRIVATE_KEY)
    assert isinstance(key, rsa.RSAPrivateKey)  # narrowed by _require_usable
    return key, _jwk_from_public(key.public_key())


def _parse_public(pem: str) -> dict[str, Any]:
    """Parse a PEM public key into its JWK.

    Raises:
        IdentityKeyMisconfigured: If the PEM is unusable.
    """
    try:
        key = serialization.load_pem_public_key(pem.encode())
    except (ValueError, TypeError, UnsupportedAlgorithm) as exc:
        raise IdentityKeyMisconfigured(
            f"{ENV_RETIRING_PUBLIC_KEYS} contains an unreadable PEM public key"
        ) from exc

    _require_usable(key, env=ENV_RETIRING_PUBLIC_KEYS)
    assert isinstance(key, rsa.RSAPublicKey)  # narrowed by _require_usable
    return _jwk_from_public(key)


def _split_pems(blob: str) -> list[str]:
    """Split concatenated PEM blocks into individual PEMs."""
    normalized = _normalize(blob)
    if not normalized:
        return []
    chunks = normalized.split(_PEM_BEGIN)
    return [f"{_PEM_BEGIN}{chunk}".strip() for chunk in chunks if chunk.strip()]


def _load_signing() -> tuple[rsa.RSAPrivateKey, dict[str, Any]]:
    """Load the signing key.

    Raises:
        IdentityKeyUnavailable: If no key is configured.
        IdentityKeyMisconfigured: If the configured key is unusable.
    """
    pem = _normalize(os.environ.get(ENV_PRIVATE_KEY, ""))
    if not pem:
        raise IdentityKeyUnavailable(f"{ENV_PRIVATE_KEY} is not set")

    digest = hashlib.sha256(pem.encode()).hexdigest()
    cached = _PRIVATE_CACHE.get(digest)
    if cached is None:
        cached = _parse_private(pem)
        _PRIVATE_CACHE[digest] = cached
    return cached


def _load_retiring() -> list[dict[str, Any]]:
    """Load the verification-only keys kept for rotation.

    Raises:
        IdentityKeyMisconfigured: If any configured key is unusable. Fails
            closed rather than skipping it: a retiring key silently dropped is
            a rotation that quietly stopped working.
    """
    jwks: list[dict[str, Any]] = []
    for pem in _split_pems(os.environ.get(ENV_RETIRING_PUBLIC_KEYS, "")):
        digest = hashlib.sha256(pem.encode()).hexdigest()
        cached = _PUBLIC_CACHE.get(digest)
        if cached is None:
            cached = _parse_public(pem)
            _PUBLIC_CACHE[digest] = cached
        jwks.append(cached)
    return jwks


def identity_enabled() -> bool:
    """Report whether a signing key is configured.

    Returns:
        Whether cross-service identity is offered here.

    Raises:
        IdentityKeyMisconfigured: If a key is configured but unusable — that is
            not the same as "off", and must not be reported as such.
    """
    try:
        _load_signing()
    except IdentityKeyUnavailable:
        return False
    return True


def signing_key() -> rsa.RSAPrivateKey:
    """Return the private key used to sign identity tokens.

    Raises:
        IdentityKeyUnavailable: If no key is configured.
        IdentityKeyMisconfigured: If the configured key is unusable.
    """
    return _load_signing()[0]


def signing_kid() -> str:
    """Return the key ID to place in the JWT header.

    Raises:
        IdentityKeyUnavailable: If no key is configured.
        IdentityKeyMisconfigured: If the configured key is unusable.
    """
    return _load_signing()[1]["kid"]


def public_jwks() -> dict[str, list[dict[str, Any]]]:
    """Return the JWKS document served to identity consumers.

    The signing key comes first, followed by any retiring keys still accepted
    for verification. Duplicates are collapsed, so listing the current key among
    the retiring ones is harmless rather than confusing.

    Raises:
        IdentityKeyUnavailable: If no signing key is configured.
        IdentityKeyMisconfigured: If any configured key is unusable.
    """
    signing = _load_signing()[1]
    keys = [dict(signing)]
    seen = {signing["kid"]}
    for jwk in _load_retiring():
        if jwk["kid"] in seen:
            continue
        seen.add(jwk["kid"])
        keys.append(dict(jwk))
    return {"keys": keys}


def reset_cache() -> None:
    """Drop parsed keys. For tests that swap the configured keys."""
    _PRIVATE_CACHE.clear()
    _PUBLIC_CACHE.clear()
