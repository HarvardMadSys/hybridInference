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
from serving.utils.identity_keys import ENV_PRIVATE_KEY, IdentityKeyUnavailable

_PRIVATE_JWK_MEMBERS = ("d", "p", "q", "dp", "dq", "qi")


def _pem(key) -> str:
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()


@pytest.fixture(scope="module")
def rsa_pem() -> str:
    return _pem(rsa.generate_private_key(public_exponent=65537, key_size=2048))


@pytest.fixture(scope="module")
def other_rsa_pem() -> str:
    return _pem(rsa.generate_private_key(public_exponent=65537, key_size=2048))


@pytest.fixture(autouse=True)
def _clear_cache():
    identity_keys.reset_cache()
    yield
    identity_keys.reset_cache()


@pytest.fixture
def configured(monkeypatch, rsa_pem: str) -> str:
    monkeypatch.setenv(ENV_PRIVATE_KEY, rsa_pem)
    return rsa_pem


# ---------------------------------------------------------------------------
# Key loading
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


def test_non_pem_text_raises_our_error_not_a_crypto_error(monkeypatch) -> None:
    monkeypatch.setenv(ENV_PRIVATE_KEY, "definitely not a key")
    with pytest.raises(IdentityKeyUnavailable):
        identity_keys.public_jwks()


def test_corrupt_pem_body_raises_our_error_not_a_crypto_error(monkeypatch, rsa_pem: str) -> None:
    """A PEM-shaped value with a damaged body — truncation, bad copy-paste.

    The armour lines come from a generated key rather than being written out
    here: a literal PEM header in the source is indistinguishable from a real
    leaked key to the release export scanner, and it should stay that way.
    """
    lines = rsa_pem.strip().splitlines()
    monkeypatch.setenv(ENV_PRIVATE_KEY, "\n".join([lines[0], "!!! not base64 !!!", lines[-1]]))
    with pytest.raises(IdentityKeyUnavailable):
        identity_keys.public_jwks()


def test_non_rsa_key_is_rejected(monkeypatch) -> None:
    monkeypatch.setenv(ENV_PRIVATE_KEY, _pem(ec.generate_private_key(ec.SECP256R1())))
    with pytest.raises(IdentityKeyUnavailable, match="RSA"):
        identity_keys.public_jwks()


def test_undersized_key_is_rejected(monkeypatch) -> None:
    monkeypatch.setenv(ENV_PRIVATE_KEY, _pem(rsa.generate_private_key(65537, 1024)))
    with pytest.raises(IdentityKeyUnavailable, match="minimum"):
        identity_keys.public_jwks()


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


# ---------------------------------------------------------------------------
# The property that actually matters
# ---------------------------------------------------------------------------


def test_published_key_verifies_tokens_signed_by_the_signing_key(configured: str) -> None:
    """A token signed with the private key verifies against the published JWK.

    Every other assertion here is shape-checking. This is the one that fails if
    the endpoint ever publishes a key that is not the one we sign with — the
    failure mode that would break every consumer at once, silently, at rotation
    time.
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
    return TestClient(app)


def test_endpoint_serves_the_jwks(client: TestClient, configured: str) -> None:
    response = client.get("/v1/identity/jwks")
    assert response.status_code == 200
    assert response.json() == identity_keys.public_jwks()
    assert "max-age" in response.headers["cache-control"]


def test_endpoint_404s_when_identity_is_not_configured(client: TestClient, monkeypatch) -> None:
    """Unconfigured must read as "not offered here", never as a broken gateway."""
    monkeypatch.delenv(ENV_PRIVATE_KEY, raising=False)
    response = client.get("/v1/identity/jwks")
    assert response.status_code == 404
    assert response.json()["error"]["type"] == "identity_not_configured"
