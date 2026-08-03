"""Tests for the RS256 identity keys and the JWKS endpoint."""

from __future__ import annotations

import base64
import hashlib
import json

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from fastapi import FastAPI
from fastapi.testclient import TestClient

from serving.servers.middleware.error import install_error_handlers
from serving.servers.routers import identity
from serving.utils import identity_keys
from serving.utils.identity_keys import (
    ENV_PRIVATE_KEY,
    ENV_RETIRING_PUBLIC_KEYS,
    IdentityKeyMisconfigured,
    IdentityKeyUnavailable,
)

_PRIVATE_JWK_MEMBERS = ("d", "p", "q", "dp", "dq", "qi")


def _pem(key) -> str:
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()


def _public_pem(private_pem: str) -> str:
    key = serialization.load_pem_private_key(private_pem.encode(), password=None)
    return (
        key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )


@pytest.fixture(scope="module")
def rsa_pem() -> str:
    return _pem(rsa.generate_private_key(public_exponent=65537, key_size=2048))


@pytest.fixture(scope="module")
def other_rsa_pem() -> str:
    return _pem(rsa.generate_private_key(public_exponent=65537, key_size=2048))


@pytest.fixture(autouse=True)
def _clear_cache(monkeypatch):
    monkeypatch.delenv(ENV_RETIRING_PUBLIC_KEYS, raising=False)
    identity_keys.reset_cache()
    yield
    identity_keys.reset_cache()


@pytest.fixture
def configured(monkeypatch, rsa_pem: str) -> str:
    monkeypatch.setenv(ENV_PRIVATE_KEY, rsa_pem)
    return rsa_pem


# ---------------------------------------------------------------------------
# Unset means "not offered"
# ---------------------------------------------------------------------------


def test_unset_key_is_unavailable(monkeypatch) -> None:
    monkeypatch.delenv(ENV_PRIVATE_KEY, raising=False)
    assert identity_keys.identity_enabled() is False
    with pytest.raises(IdentityKeyUnavailable):
        identity_keys.public_jwks()


def test_blank_key_is_unavailable(monkeypatch) -> None:
    monkeypatch.setenv(ENV_PRIVATE_KEY, "   \n  ")
    with pytest.raises(IdentityKeyUnavailable):
        identity_keys.public_jwks()


# ---------------------------------------------------------------------------
# Configured but unusable means "misconfigured" — a different answer
# ---------------------------------------------------------------------------


def test_non_pem_text_is_misconfigured(monkeypatch) -> None:
    monkeypatch.setenv(ENV_PRIVATE_KEY, "definitely not a key")
    with pytest.raises(IdentityKeyMisconfigured):
        identity_keys.public_jwks()


def test_corrupt_pem_body_is_misconfigured(monkeypatch, rsa_pem: str) -> None:
    """A PEM-shaped value with a damaged body — truncation, bad copy-paste.

    The armour lines come from a generated key rather than being written out
    here: a literal PEM header in the source is indistinguishable from a real
    leaked key to the release export scanner, and it should stay that way.
    """
    lines = rsa_pem.strip().splitlines()
    monkeypatch.setenv(ENV_PRIVATE_KEY, "\n".join([lines[0], "!!! not base64 !!!", lines[-1]]))
    with pytest.raises(IdentityKeyMisconfigured):
        identity_keys.public_jwks()


def test_non_rsa_key_is_misconfigured(monkeypatch) -> None:
    monkeypatch.setenv(ENV_PRIVATE_KEY, _pem(ec.generate_private_key(ec.SECP256R1())))
    with pytest.raises(IdentityKeyMisconfigured, match="RSA"):
        identity_keys.public_jwks()


def test_undersized_key_is_misconfigured(monkeypatch) -> None:
    monkeypatch.setenv(ENV_PRIVATE_KEY, _pem(rsa.generate_private_key(65537, 1024)))
    with pytest.raises(IdentityKeyMisconfigured, match="minimum"):
        identity_keys.public_jwks()


def test_encrypted_key_is_misconfigured(monkeypatch) -> None:
    """A passphrase-protected key is an operator error, not a disabled feature."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    encrypted = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.BestAvailableEncryption(b"passphrase"),
    ).decode()
    monkeypatch.setenv(ENV_PRIVATE_KEY, encrypted)
    with pytest.raises(IdentityKeyMisconfigured):
        identity_keys.public_jwks()


def test_identity_enabled_does_not_report_a_broken_key_as_off(monkeypatch) -> None:
    """The distinction has to survive the convenience accessor too.

    Returning False here would make a mangled PEM indistinguishable from a
    deployment that never enabled identity, which is exactly the confusion the
    two exception types exist to prevent.
    """
    monkeypatch.setenv(ENV_PRIVATE_KEY, "definitely not a key")
    with pytest.raises(IdentityKeyMisconfigured):
        identity_keys.identity_enabled()


def test_escaped_newlines_are_accepted(monkeypatch, rsa_pem: str) -> None:
    """A key pasted into an env var arrives with literal backslash-n."""
    monkeypatch.setenv(ENV_PRIVATE_KEY, rsa_pem.strip().replace("\n", "\\n"))
    (key,) = identity_keys.public_jwks()["keys"]
    assert key["kid"]


# ---------------------------------------------------------------------------
# JWKS document
# ---------------------------------------------------------------------------


def test_jwks_shape(configured: str) -> None:
    document = identity_keys.public_jwks()
    assert list(document) == ["keys"]
    (key,) = document["keys"]
    assert key["kty"] == "RSA"
    assert key["use"] == "sig"
    assert key["alg"] == "RS256"
    assert key["kid"] and key["n"] and key["e"]


def test_jwks_never_leaks_private_material(configured: str) -> None:
    (key,) = identity_keys.public_jwks()["keys"]
    assert not [m for m in _PRIVATE_JWK_MEMBERS if m in key]


def test_jwks_result_is_not_shared_state(configured: str) -> None:
    first = identity_keys.public_jwks()
    first["keys"][0]["kid"] = "tampered"
    assert identity_keys.public_jwks()["keys"][0]["kid"] != "tampered"


# ---------------------------------------------------------------------------
# Rotation
# ---------------------------------------------------------------------------


def test_retiring_keys_are_published_after_the_signing_key(
    monkeypatch, rsa_pem: str, other_rsa_pem: str
) -> None:
    monkeypatch.setenv(ENV_PRIVATE_KEY, rsa_pem)
    monkeypatch.setenv(ENV_RETIRING_PUBLIC_KEYS, _public_pem(other_rsa_pem))

    keys = identity_keys.public_jwks()["keys"]
    assert len(keys) == 2
    assert keys[0]["kid"] == identity_keys.signing_kid()
    assert not [m for k in keys for m in _PRIVATE_JWK_MEMBERS if m in k]


def test_several_retiring_keys_can_be_concatenated(
    monkeypatch, rsa_pem: str, other_rsa_pem: str
) -> None:
    third = _pem(rsa.generate_private_key(public_exponent=65537, key_size=2048))
    monkeypatch.setenv(ENV_PRIVATE_KEY, rsa_pem)
    monkeypatch.setenv(
        ENV_RETIRING_PUBLIC_KEYS, _public_pem(other_rsa_pem) + "\n" + _public_pem(third)
    )
    assert len(identity_keys.public_jwks()["keys"]) == 3


def test_the_signing_key_listed_as_retiring_is_not_published_twice(
    monkeypatch, rsa_pem: str
) -> None:
    monkeypatch.setenv(ENV_PRIVATE_KEY, rsa_pem)
    monkeypatch.setenv(ENV_RETIRING_PUBLIC_KEYS, _public_pem(rsa_pem))
    assert len(identity_keys.public_jwks()["keys"]) == 1


def test_an_unreadable_retiring_key_fails_closed(monkeypatch, rsa_pem: str) -> None:
    """Skipping it silently would be a rotation that quietly stopped working."""
    monkeypatch.setenv(ENV_PRIVATE_KEY, rsa_pem)
    monkeypatch.setenv(ENV_RETIRING_PUBLIC_KEYS, "-----BEGIN PUBLIC KEY-----\nx\n")
    with pytest.raises(IdentityKeyMisconfigured):
        identity_keys.public_jwks()


def test_a_token_signed_before_rotation_still_verifies(
    monkeypatch, rsa_pem: str, other_rsa_pem: str
) -> None:
    """The point of publishing more than one key.

    A consumer holding a token issued minutes before a rotation must still be
    able to verify it. With a single-key JWKS the rotation invalidates every
    unexpired token at once, and it surfaces as a bad signature rather than as
    the rotation it is.
    """
    # Signed while `other_rsa_pem` was current.
    monkeypatch.setenv(ENV_PRIVATE_KEY, other_rsa_pem)
    old_token = jwt.encode(
        {"sub": "user_1", "aud": "cloud-agent"},
        identity_keys.signing_key(),
        algorithm="RS256",
        headers={"kid": identity_keys.signing_kid()},
    )
    old_kid = identity_keys.signing_kid()

    # Rotate: new signing key, old one retained for verification only.
    monkeypatch.setenv(ENV_PRIVATE_KEY, rsa_pem)
    monkeypatch.setenv(ENV_RETIRING_PUBLIC_KEYS, _public_pem(other_rsa_pem))

    published = {k["kid"]: k for k in identity_keys.public_jwks()["keys"]}
    assert identity_keys.signing_kid() != old_kid
    assert old_kid in published

    claims = jwt.decode(
        old_token,
        jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(published[old_kid])),
        algorithms=["RS256"],
        audience="cloud-agent",
    )
    assert claims["sub"] == "user_1"


# ---------------------------------------------------------------------------
# Key ID
# ---------------------------------------------------------------------------


def test_kid_is_stable_across_calls(configured: str) -> None:
    assert identity_keys.signing_kid() == identity_keys.signing_kid()


def test_kid_changes_with_the_key(monkeypatch, rsa_pem: str, other_rsa_pem: str) -> None:
    monkeypatch.setenv(ENV_PRIVATE_KEY, rsa_pem)
    first = identity_keys.signing_kid()
    monkeypatch.setenv(ENV_PRIVATE_KEY, other_rsa_pem)
    assert identity_keys.signing_kid() != first


def test_kid_is_the_rfc7638_thumbprint(configured: str) -> None:
    (key,) = identity_keys.public_jwks()["keys"]
    canonical = json.dumps(
        {"e": key["e"], "kty": "RSA", "n": key["n"]}, separators=(",", ":"), sort_keys=True
    )
    expected = (
        base64.urlsafe_b64encode(hashlib.sha256(canonical.encode()).digest()).decode().rstrip("=")
    )
    assert key["kid"] == expected


def test_published_key_verifies_tokens_signed_by_the_signing_key(configured: str) -> None:
    """Everything else here is shape-checking; this is the load-bearing one.

    It fails if the endpoint ever publishes a key we do not sign with — the
    failure that would break every consumer at once, silently.
    """
    (published,) = identity_keys.public_jwks()["keys"]
    token = jwt.encode(
        {"sub": "user_1", "aud": "cloud-agent"},
        identity_keys.signing_key(),
        algorithm="RS256",
        headers={"kid": identity_keys.signing_kid()},
    )

    assert jwt.get_unverified_header(token)["kid"] == published["kid"]
    decoded = jwt.decode(
        token,
        jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(published)),
        algorithms=["RS256"],
        audience="cloud-agent",
    )
    assert decoded["sub"] == "user_1"


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(identity.router)
    # The gateway's error handlers unwrap HTTPException.detail into the standard
    # {"error": {...}} envelope. Without them this app would answer in a shape no
    # real consumer ever sees, and the assertions below would pin the harness
    # rather than the contract.
    install_error_handlers(app)
    return TestClient(app, raise_server_exceptions=False)


def test_endpoint_serves_the_jwks(client: TestClient, configured: str) -> None:
    response = client.get("/v1/identity/jwks")
    assert response.status_code == 200
    assert response.json() == identity_keys.public_jwks()
    assert response.headers["cache-control"] == "public, max-age=300"


def test_endpoint_404s_when_identity_is_not_configured(client: TestClient, monkeypatch) -> None:
    """Unconfigured must read as "not offered here", never as a broken gateway."""
    monkeypatch.delenv(ENV_PRIVATE_KEY, raising=False)
    response = client.get("/v1/identity/jwks")
    assert response.status_code == 404
    assert response.json()["error"]["type"] == "identity_not_configured"


def test_endpoint_500s_when_the_key_is_misconfigured(client: TestClient, monkeypatch) -> None:
    """And a broken key must not read as "not offered here"."""
    monkeypatch.setenv(ENV_PRIVATE_KEY, "definitely not a key")
    response = client.get("/v1/identity/jwks")
    assert response.status_code == 500
    assert response.json()["error"]["type"] == "identity_key_misconfigured"
