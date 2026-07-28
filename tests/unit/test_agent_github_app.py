"""Tests for GitHub App credentials.

An App exists to make the publisher's credential short-lived and scoped, so
the cases below are the ones where getting it wrong quietly restores the
properties a PAT had: a token cached past its life, a repo resolved to the
wrong installation, or a private key leaking into a log line.
"""

from __future__ import annotations

import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from serving.agent_jobs.github_app import (
    AppConfig,
    GitHubAppCredentials,
    GitHubAppError,
    build_app_jwt,
)


@pytest.fixture(scope="module")
def keypair() -> tuple[str, str]:
    """Generate a throwaway RSA key so signing is exercised for real."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_pem = (
        key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    return private_pem, public_pem


@pytest.fixture()
def config(keypair) -> AppConfig:
    """An App config backed by the generated key."""
    return AppConfig(app_id="12345", private_key=keypair[0], api_base="https://api.test")


def _client_returning(handler):
    """Patch httpx.AsyncClient onto a mock transport."""
    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient

    class Patched(original):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = transport
            super().__init__(*args, **kwargs)

    return original, Patched


def test_config_absent_is_none_not_an_error():
    """A deployment without the App should not publish, not fail to boot."""
    assert AppConfig.from_env({}) is None
    assert AppConfig.from_env({"AGENT_GITHUB_APP_ID": "1"}) is None


def test_config_unescapes_a_pasted_key(keypair):
    """A key pasted into an env var arrives with literal \\n sequences."""
    private_pem, _ = keypair
    config = AppConfig.from_env(
        {
            "AGENT_GITHUB_APP_ID": "42",
            "AGENT_GITHUB_APP_PRIVATE_KEY": private_pem.replace("\n", "\\n"),
        }
    )
    assert config is not None
    assert "\\n" not in config.private_key
    assert config.private_key.startswith("-----BEGIN")


def test_jwt_is_signed_verifiably_and_short_lived(config, keypair):
    """The JWT must verify against the public key and expire within ten minutes."""
    _, public_pem = keypair
    now = int(time.time())
    token = build_app_jwt(config, now=now)

    claims = jwt.decode(token, public_pem, algorithms=["RS256"], options={"verify_aud": False})
    assert claims["iss"] == "12345"
    # Backdated so a slightly fast clock is not rejected by GitHub.
    assert claims["iat"] <= now
    # GitHub refuses anything over ten minutes.
    assert 0 < claims["exp"] - now <= 600


def test_unusable_private_key_is_a_clear_error():
    """A malformed key must fail with a message, not a stack trace."""
    with pytest.raises(GitHubAppError):
        build_app_jwt(AppConfig(app_id="1", private_key="not a key"))


async def test_token_is_scoped_to_the_repository_installation(config):
    """The repo resolves to its installation, and that installation's token."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(f"{request.method} {request.url.path}")
        if request.url.path == "/repos/o/n/installation":
            return httpx.Response(200, json={"id": 777})
        if request.url.path == "/app/installations/777/access_tokens":
            return httpx.Response(
                201,
                json={"token": "ghs_scoped", "expires_at": "2099-01-01T00:00:00Z"},
            )
        return httpx.Response(404, json={})

    original, patched = _client_returning(handler)
    httpx.AsyncClient = patched
    try:
        creds = GitHubAppCredentials(config)
        assert await creds.token_for("o/n") == "ghs_scoped"
    finally:
        httpx.AsyncClient = original

    assert seen == [
        "GET /repos/o/n/installation",
        "POST /app/installations/777/access_tokens",
    ]


async def test_token_is_cached_but_not_past_its_life(config):
    """A live token is reused; an expiring one is re-minted.

    Caching forever would quietly restore the property the App exists to
    remove — a credential that outlives its window.
    """
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/installation"):
            return httpx.Response(200, json={"id": 1})
        calls["n"] += 1
        return httpx.Response(
            201,
            json={
                "token": f"ghs_{calls['n']}",
                # Already inside the refresh margin, so the next call re-mints.
                "expires_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 60)),
            },
        )

    original, patched = _client_returning(handler)
    httpx.AsyncClient = patched
    try:
        creds = GitHubAppCredentials(config)
        first = await creds.token_for("o/n")
        second = await creds.token_for("o/n")
    finally:
        httpx.AsyncClient = original

    assert (first, second) == ("ghs_1", "ghs_2")
    assert calls["n"] == 2


async def test_a_long_lived_token_is_reused(config):
    """Within its window the token is served from cache, not re-minted."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/installation"):
            return httpx.Response(200, json={"id": 1})
        calls["n"] += 1
        return httpx.Response(201, json={"token": "ghs_x", "expires_at": "2099-01-01T00:00:00Z"})

    original, patched = _client_returning(handler)
    httpx.AsyncClient = patched
    try:
        creds = GitHubAppCredentials(config)
        await creds.token_for("o/n")
        await creds.token_for("o/n")
    finally:
        httpx.AsyncClient = original

    assert calls["n"] == 1


async def test_uninstalled_repository_says_so(config):
    """A repo without an installation is a clear message, not a retry loop."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "Not Found"})

    original, patched = _client_returning(handler)
    httpx.AsyncClient = patched
    try:
        creds = GitHubAppCredentials(config)
        with pytest.raises(GitHubAppError):
            await creds.token_for("o/uninstalled")
    finally:
        httpx.AsyncClient = original


async def test_malformed_repo_is_rejected_before_any_request(config):
    """'owner/name' is required; anything else never reaches GitHub."""
    creds = GitHubAppCredentials(config)
    with pytest.raises(GitHubAppError, match="owner/name"):
        await creds.installation_id_for("not-a-repo")


async def test_forget_drops_cached_state(config):
    """Uninstalling the App must not leave a usable cached token behind."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/installation"):
            return httpx.Response(200, json={"id": 5})
        return httpx.Response(201, json={"token": "ghs_y", "expires_at": "2099-01-01T00:00:00Z"})

    original, patched = _client_returning(handler)
    httpx.AsyncClient = patched
    try:
        creds = GitHubAppCredentials(config)
        await creds.token_for("o/n")
        assert creds._tokens
        creds.forget("o/n")
        assert not creds._tokens
    finally:
        httpx.AsyncClient = original


def test_private_key_never_appears_in_an_error(config):
    """A failure message must not carry the key it failed to use."""
    broken = AppConfig(app_id="1", private_key="-----BEGIN PRIVATE KEY-----\nbroken\n")
    with pytest.raises(GitHubAppError) as excinfo:
        build_app_jwt(broken)
    assert "broken" not in str(excinfo.value)
    assert "BEGIN PRIVATE KEY" not in str(excinfo.value)


async def test_a_narrowed_token_asks_github_for_less(config):
    """An installation token inherits every App permission unless it asks for less.

    The App holds `contents: write` so the publisher can push. The runner only
    needs to read, and it runs on the host that executes untrusted repository
    code — so its token must be requested narrower, not merely used narrowly.
    """
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/installation"):
            return httpx.Response(200, json={"id": 1})
        import json as _json

        bodies.append(_json.loads(request.content) if request.content else {})
        return httpx.Response(201, json={"token": "ghs_ro", "expires_at": "2099-01-01T00:00:00Z"})

    original, patched = _client_returning(handler)
    httpx.AsyncClient = patched
    try:
        creds = GitHubAppCredentials(config)
        await creds.token_for("o/n", permissions={"contents": "read"}, repository_scoped=True)
    finally:
        httpx.AsyncClient = original

    assert bodies[0]["permissions"] == {"contents": "read"}
    assert bodies[0]["repositories"] == ["n"]


async def test_a_narrowed_token_is_not_served_from_the_full_token_cache(config):
    """Sharing one cache slot would silently undo the narrowing, in both directions."""
    minted: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/installation"):
            return httpx.Response(200, json={"id": 1})
        import json as _json

        payload = _json.loads(request.content) if request.content else {}
        minted.append(payload)
        label = "ro" if payload.get("permissions") else "rw"
        return httpx.Response(
            201, json={"token": f"ghs_{label}", "expires_at": "2099-01-01T00:00:00Z"}
        )

    original, patched = _client_returning(handler)
    httpx.AsyncClient = patched
    try:
        creds = GitHubAppCredentials(config)
        write = await creds.token_for("o/n")
        read = await creds.token_for("o/n", permissions={"contents": "read"})
        # And each is still cached within its own scope.
        read_again = await creds.token_for("o/n", permissions={"contents": "read"})
    finally:
        httpx.AsyncClient = original

    assert (write, read) == ("ghs_rw", "ghs_ro")
    assert read_again == "ghs_ro"
    assert len(minted) == 2, "the second read should have come from cache"


async def test_an_uninstalled_repository_is_distinguishable_from_an_outage(config):
    """404 is a settled answer; 503 is GitHub having a bad minute.

    Collapsing the two is what lets a momentary outage be handled as "no
    credential needed" and terminally fail a private repository's job.
    """
    from serving.agent_jobs.github_app import AppNotInstalled

    def not_found(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "Not Found"})

    def unavailable(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"message": "Service Unavailable"})

    original, patched = _client_returning(not_found)
    httpx.AsyncClient = patched
    try:
        with pytest.raises(AppNotInstalled):
            await GitHubAppCredentials(config).token_for("o/n")
    finally:
        httpx.AsyncClient = original

    original, patched = _client_returning(unavailable)
    httpx.AsyncClient = patched
    try:
        with pytest.raises(GitHubAppError) as excinfo:
            await GitHubAppCredentials(config).token_for("o/n")
    finally:
        httpx.AsyncClient = original

    assert not isinstance(excinfo.value, AppNotInstalled), (
        "a 503 must not be reported as 'the App is not installed here'"
    )
    assert excinfo.value.status == 503
