"""Tests for cross-service authorization codes and identity tokens."""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from fastapi.testclient import TestClient

from serving.config.settings import settings
from serving.servers.deps import get_current_user, get_operational_store
from serving.servers.middleware.error import install_error_handlers
from serving.servers.routers import identity
from serving.utils import identity_keys, identity_tokens
from serving.utils.identity_keys import ENV_PRIVATE_KEY
from serving.utils.identity_tokens import (
    CLOUD_AGENT_CLIENT_ID,
    ENV_ALLOWED_REDIRECTS,
    ENV_ISSUER,
    IdentityNotConfigured,
    InvalidAuthorizationRequest,
)

REDIRECT = "https://agents.staging.freeinference.org/auth/callback"
ISSUER = "https://staging.freeinference.org"
VERIFIER = "a" * 64


def _challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


CHALLENGE = _challenge(VERIFIER)


@pytest.fixture(scope="module")
def rsa_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    identity_keys.reset_cache()
    monkeypatch.delenv(ENV_ISSUER, raising=False)
    monkeypatch.delenv(ENV_ALLOWED_REDIRECTS, raising=False)
    monkeypatch.setattr(settings, "base_url", "", raising=False)
    yield
    identity_keys.reset_cache()


@pytest.fixture
def configured(monkeypatch, rsa_pem: str) -> None:
    monkeypatch.setenv(ENV_PRIVATE_KEY, rsa_pem)
    monkeypatch.setenv(ENV_ISSUER, ISSUER)
    monkeypatch.setenv(ENV_ALLOWED_REDIRECTS, f"{REDIRECT},https://agents.freeinference.org/cb")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def test_issuer_prefers_the_explicit_setting(monkeypatch) -> None:
    monkeypatch.setenv(ENV_ISSUER, ISSUER)
    monkeypatch.setattr(settings, "base_url", "https://wrong.example", raising=False)
    assert identity_tokens.issuer() == ISSUER


def test_issuer_falls_back_to_base_url(monkeypatch) -> None:
    monkeypatch.setattr(settings, "base_url", ISSUER + "/", raising=False)
    assert identity_tokens.issuer() == ISSUER  # trailing slash stripped


def test_issuer_unset_is_not_configured() -> None:
    with pytest.raises(IdentityNotConfigured):
        identity_tokens.issuer()


def test_allowed_redirects_parses_and_trims(monkeypatch) -> None:
    monkeypatch.setenv(ENV_ALLOWED_REDIRECTS, f" {REDIRECT} , ,https://b.example/cb ")
    assert identity_tokens.allowed_redirects() == (REDIRECT, "https://b.example/cb")


def test_allowed_redirects_empty_is_not_configured(monkeypatch) -> None:
    monkeypatch.setenv(ENV_ALLOWED_REDIRECTS, " , ")
    with pytest.raises(IdentityNotConfigured):
        identity_tokens.allowed_redirects()


def test_issuance_enabled_requires_key_issuer_and_redirects(monkeypatch, rsa_pem: str) -> None:
    assert identity_tokens.issuance_enabled() is False
    monkeypatch.setenv(ENV_PRIVATE_KEY, rsa_pem)
    assert identity_tokens.issuance_enabled() is False
    monkeypatch.setenv(ENV_ISSUER, ISSUER)
    assert identity_tokens.issuance_enabled() is False
    monkeypatch.setenv(ENV_ALLOWED_REDIRECTS, REDIRECT)
    assert identity_tokens.issuance_enabled() is True


# ---------------------------------------------------------------------------
# Request validation
# ---------------------------------------------------------------------------


def test_unknown_client_is_rejected(configured) -> None:
    with pytest.raises(InvalidAuthorizationRequest, match="client_id"):
        identity_tokens.validate_authorization_request(
            client_id="somebody-else", redirect_uri=REDIRECT, method="S256"
        )


def test_plain_challenge_method_is_rejected(configured) -> None:
    with pytest.raises(InvalidAuthorizationRequest, match="S256"):
        identity_tokens.validate_authorization_request(
            client_id=CLOUD_AGENT_CLIENT_ID, redirect_uri=REDIRECT, method="plain"
        )


def test_unlisted_redirect_is_rejected(configured) -> None:
    with pytest.raises(InvalidAuthorizationRequest, match="redirect_uri"):
        identity_tokens.validate_authorization_request(
            client_id=CLOUD_AGENT_CLIENT_ID,
            redirect_uri="https://evil.example/cb",
            method="S256",
        )


@pytest.mark.parametrize(
    "attempt",
    [
        REDIRECT + ".attacker.test",
        REDIRECT + "/../../evil",
        REDIRECT + "?next=https://evil.example",
        "https://agents.staging.freeinference.org.attacker.test/auth/callback",
    ],
)
def test_redirect_match_is_exact_not_prefix(configured, attempt: str) -> None:
    """A near-miss redirect is the whole point of an allowlist.

    Anything looser than equality — prefix, host suffix, "starts with" — admits
    a domain the attacker controls, and those are the ones they would register.
    """
    with pytest.raises(InvalidAuthorizationRequest):
        identity_tokens.validate_authorization_request(
            client_id=CLOUD_AGENT_CLIENT_ID, redirect_uri=attempt, method="S256"
        )


# ---------------------------------------------------------------------------
# PKCE and token minting
# ---------------------------------------------------------------------------


def test_pkce_accepts_the_matching_verifier() -> None:
    assert identity_tokens.verify_pkce(verifier=VERIFIER, challenge=CHALLENGE) is True


def test_pkce_rejects_a_different_verifier() -> None:
    assert identity_tokens.verify_pkce(verifier="b" * 64, challenge=CHALLENGE) is False


def test_minted_token_verifies_against_the_published_key(configured) -> None:
    token = identity_tokens.mint_identity_token(user_id="user_1", email="a@b.test", role="pro")
    (published,) = identity_keys.public_jwks()["keys"]
    assert jwt.get_unverified_header(token)["kid"] == published["kid"]

    claims = jwt.decode(
        token,
        jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(published)),
        algorithms=["RS256"],
        audience=CLOUD_AGENT_CLIENT_ID,
        issuer=ISSUER,
    )
    assert claims["sub"] == "user_1"
    assert claims["email"] == "a@b.test"
    assert claims["role"] == "pro"
    assert claims["exp"] - claims["iat"] == identity_tokens.TOKEN_TTL_SECONDS


def test_minted_token_omits_email_when_unknown(configured) -> None:
    token = identity_tokens.mint_identity_token(user_id="user_1", email=None, role="free")
    assert "email" not in jwt.decode(token, options={"verify_signature": False})


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


class FakeStore:
    """The two code methods plus user lookup, with real single-use semantics."""

    def __init__(self) -> None:
        self.codes: dict[str, dict[str, Any]] = {}
        self.users: dict[str, dict[str, Any]] = {}

    async def create_identity_auth_code(
        self,
        *,
        code_hash: str,
        user_id: str,
        client_id: str,
        redirect_uri: str,
        code_challenge: str,
        expires_at: datetime,
    ) -> None:
        self.codes[code_hash] = {
            "user_id": user_id,
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "code_challenge": code_challenge,
            "expires_at": expires_at,
            "used_at": None,
        }

    async def consume_identity_auth_code(self, code_hash: str) -> dict[str, Any] | None:
        row = self.codes.get(code_hash)
        if row is None or row["used_at"] is not None or row["expires_at"] <= datetime.now(UTC):
            return None
        row["used_at"] = datetime.now(UTC)
        return {k: row[k] for k in ("user_id", "client_id", "redirect_uri", "code_challenge")}

    async def get_user_by_id(self, user_id: str) -> dict[str, Any] | None:
        return self.users.get(user_id)


@pytest.fixture
def store() -> FakeStore:
    fake = FakeStore()
    fake.users["user_1"] = {
        "id": "user_1",
        "email": "a@b.test",
        "role": "pro",
        "status": "active",
    }
    return fake


@pytest.fixture
def client(store: FakeStore) -> TestClient:
    app = FastAPI()
    app.include_router(identity.router)
    install_error_handlers(app)
    app.dependency_overrides[get_operational_store] = lambda: store
    app.dependency_overrides[get_current_user] = lambda: {"user_id": "user_1", "role": "pro"}
    return TestClient(app)


def _request_code(client: TestClient, **overrides: Any) -> Any:
    body = {
        "client_id": CLOUD_AGENT_CLIENT_ID,
        "redirect_uri": REDIRECT,
        "code_challenge": CHALLENGE,
        "code_challenge_method": "S256",
    }
    body.update(overrides)
    return client.post("/v1/identity/code", json=body)


def _exchange(client: TestClient, code: str, **overrides: Any) -> Any:
    body = {
        "code": code,
        "code_verifier": VERIFIER,
        "client_id": CLOUD_AGENT_CLIENT_ID,
        "redirect_uri": REDIRECT,
    }
    body.update(overrides)
    return client.post("/v1/identity/token", json=body)


def test_code_endpoint_issues_a_code(client: TestClient, configured) -> None:
    response = _request_code(client)
    assert response.status_code == 200
    assert response.json()["expires_in"] == identity_tokens.CODE_TTL_SECONDS
    assert response.json()["code"]


def test_code_is_stored_hashed_never_in_the_clear(
    client: TestClient, store: FakeStore, configured
) -> None:
    """A read of the table must not yield anything exchangeable."""
    code = _request_code(client).json()["code"]
    assert code not in store.codes
    assert identity_tokens.hash_code(code) in store.codes


@pytest.mark.parametrize(
    "overrides",
    [
        {"client_id": "somebody-else"},
        {"redirect_uri": "https://evil.example/cb"},
        {"code_challenge_method": "plain"},
    ],
)
def test_code_endpoint_rejects_bad_requests(
    client: TestClient, configured, overrides: dict[str, Any]
) -> None:
    response = _request_code(client, **overrides)
    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_request"


def test_code_endpoint_404s_when_identity_is_not_configured(client: TestClient) -> None:
    response = _request_code(client)
    assert response.status_code == 404
    assert response.json()["error"]["type"] == "identity_not_configured"


def test_exchange_returns_a_verifiable_identity_token(client: TestClient, configured) -> None:
    code = _request_code(client).json()["code"]
    response = _exchange(client, code)
    assert response.status_code == 200
    payload = response.json()
    assert payload["token_type"] == "Bearer"
    assert payload["expires_in"] == identity_tokens.TOKEN_TTL_SECONDS

    (published,) = identity_keys.public_jwks()["keys"]
    claims = jwt.decode(
        payload["access_token"],
        jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(published)),
        algorithms=["RS256"],
        audience=CLOUD_AGENT_CLIENT_ID,
        issuer=ISSUER,
    )
    assert claims["sub"] == "user_1"
    assert claims["role"] == "pro"


def test_a_code_cannot_be_exchanged_twice(client: TestClient, configured) -> None:
    code = _request_code(client).json()["code"]
    assert _exchange(client, code).status_code == 200
    replay = _exchange(client, code)
    assert replay.status_code == 400
    assert replay.json()["error"]["type"] == "invalid_grant"


def test_a_failed_exchange_burns_the_code(client: TestClient, configured) -> None:
    """The security property that makes an intercepted code useless.

    If a wrong verifier left the code usable, an attacker who stole it could
    keep trying, and the legitimate client's later success would hide that it
    happened. One attempt, whatever the outcome.
    """
    code = _request_code(client).json()["code"]
    assert _exchange(client, code, code_verifier="b" * 64).status_code == 400
    assert _exchange(client, code).status_code == 400


@pytest.mark.parametrize(
    "overrides",
    [
        {"code_verifier": "b" * 64},
        {"client_id": "somebody-else"},
        {"redirect_uri": "https://agents.freeinference.org/cb"},
    ],
)
def test_exchange_rejects_mismatches(
    client: TestClient, configured, overrides: dict[str, Any]
) -> None:
    code = _request_code(client).json()["code"]
    response = _exchange(client, code, **overrides)
    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_grant"


def test_exchange_rejects_an_unknown_code(client: TestClient, configured) -> None:
    assert _exchange(client, "never-issued").status_code == 400


def test_exchange_rejects_an_expired_code(client: TestClient, store: FakeStore, configured) -> None:
    code = _request_code(client).json()["code"]
    store.codes[identity_tokens.hash_code(code)]["expires_at"] = datetime.now(UTC) - timedelta(
        seconds=1
    )
    assert _exchange(client, code).status_code == 400


@pytest.mark.parametrize("status_value", ["suspended", "deleted", "pending_approval"])
def test_exchange_refuses_an_account_that_may_not_sign_in(
    client: TestClient, store: FakeStore, configured, status_value: str
) -> None:
    """Status is re-read at exchange, not trusted from when the code was issued.

    A suspension in that window has to take effect now, not after the identity
    token expires wherever it was sent.
    """
    code = _request_code(client).json()["code"]
    store.users["user_1"]["status"] = status_value
    response = _exchange(client, code)
    assert response.status_code == 403
    assert response.json()["error"]["type"] == "account_unavailable"


def test_exchange_refuses_a_vanished_account(
    client: TestClient, store: FakeStore, configured
) -> None:
    code = _request_code(client).json()["code"]
    store.users.clear()
    assert _exchange(client, code).status_code == 403
