"""Security and protocol tests for source-control OAuth connections."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from serving.agent_jobs.source_control import (
    GitLabOAuthClient,
    GitLabOAuthConfig,
    OAuthStateError,
    SourceControlCipher,
    SourceControlError,
    consume_oauth_state,
    github_authorization_url,
    issue_oauth_state,
)


class MemoryStore:
    """Small faithful stand-in for the store's atomic ownership checks."""

    def __init__(self) -> None:
        self.states: dict[str, dict] = {}
        self.token_update: dict | None = None

    async def create_oauth_state(self, **row) -> None:
        self.states[row["state_hash"]] = {**row, "consumed": False}

    async def consume_oauth_state(self, *, state_hash, user_id, provider):
        row = self.states.get(state_hash)
        if (
            row is None
            or row["user_id"] != user_id
            or row["provider"] != provider
            or row["consumed"]
            or row["expires_at"] <= datetime.now(timezone.utc)
        ):
            return None
        row["consumed"] = True
        return {"code_verifier_ciphertext": row["code_verifier_ciphertext"]}

    async def update_gitlab_tokens(self, **values) -> None:
        self.token_update = values


@pytest.mark.asyncio
async def test_oauth_state_is_hashed_bound_and_single_use() -> None:
    store = MemoryStore()
    cipher = SourceControlCipher("encryption-key")
    state = await issue_oauth_state(
        store,
        user_id="user-a",
        provider="gitlab",
        cipher=cipher,
        code_verifier="verifier-secret",
    )

    persisted = next(iter(store.states.values()))
    assert state not in store.states
    assert persisted["code_verifier_ciphertext"] != "verifier-secret"

    with pytest.raises(OAuthStateError):
        await consume_oauth_state(
            store,
            state=state,
            user_id="user-b",
            provider="gitlab",
            cipher=cipher,
        )
    # A wrong user does not burn the rightful user's state.
    assert (
        await consume_oauth_state(
            store,
            state=state,
            user_id="user-a",
            provider="gitlab",
            cipher=cipher,
        )
        == "verifier-secret"
    )
    with pytest.raises(OAuthStateError):
        await consume_oauth_state(
            store,
            state=state,
            user_id="user-a",
            provider="gitlab",
            cipher=cipher,
        )


@pytest.mark.asyncio
async def test_oauth_state_expiry_and_provider_tampering_fail_closed() -> None:
    store = MemoryStore()
    cipher = SourceControlCipher("encryption-key")
    state = await issue_oauth_state(store, user_id="user-a", provider="github", cipher=cipher)
    with pytest.raises(OAuthStateError):
        await consume_oauth_state(
            store,
            state=state,
            user_id="user-a",
            provider="gitlab",
            cipher=cipher,
        )

    persisted = next(iter(store.states.values()))
    persisted["expires_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)
    with pytest.raises(OAuthStateError):
        await consume_oauth_state(
            store,
            state=state,
            user_id="user-a",
            provider="github",
            cipher=cipher,
        )


@pytest.mark.asyncio
async def test_github_state_does_not_require_the_gitlab_encryption_key() -> None:
    """GitHub persists no token or verifier, so an unset cipher must not disable it."""
    store = MemoryStore()
    state = await issue_oauth_state(store, user_id="user-a", provider="github")

    assert (
        await consume_oauth_state(
            store,
            state=state,
            user_id="user-a",
            provider="github",
        )
        is None
    )


def test_source_control_cipher_is_domain_separated_and_detects_tampering() -> None:
    cipher = SourceControlCipher("server-secret")
    encrypted = cipher.encrypt("glpat-raw-token")
    assert "glpat-raw-token" not in encrypted
    assert cipher.decrypt(encrypted) == "glpat-raw-token"

    with pytest.raises(SourceControlError):
        cipher.decrypt(encrypted[:-2] + "aa")
    with pytest.raises(SourceControlError):
        SourceControlCipher("")


def test_github_authorization_url_cannot_be_an_open_redirect() -> None:
    url = github_authorization_url(
        "https://github.com/login/oauth/authorize?client_id=abc", state="state-value"
    )
    assert parse_qs(urlparse(url).query)["state"] == ["state-value"]
    with pytest.raises(SourceControlError):
        github_authorization_url("https://evil.example/oauth", state="state-value")


def test_gitlab_config_requires_complete_credentials_and_safe_redirect() -> None:
    assert GitLabOAuthConfig.from_env({}) is None
    with pytest.raises(SourceControlError):
        GitLabOAuthConfig.from_env({"AGENT_GITLAB_OAUTH_CLIENT_ID": "only-one-value"})
    with pytest.raises(SourceControlError):
        GitLabOAuthConfig.from_env(
            {
                "AGENT_GITLAB_OAUTH_CLIENT_ID": "id",
                "AGENT_GITLAB_OAUTH_CLIENT_SECRET": "secret",
                "AGENT_GITLAB_OAUTH_REDIRECT_URI": "http://evil.example/callback",
            }
        )
    assert (
        GitLabOAuthConfig.from_env(
            {
                "AGENT_GITLAB_OAUTH_CLIENT_ID": "id",
                "AGENT_GITLAB_OAUTH_CLIENT_SECRET": "secret",
                "AGENT_GITLAB_OAUTH_REDIRECT_URI": "http://localhost:3001/callback",
            }
        )
        is not None
    )


@pytest.mark.asyncio
async def test_gitlab_pkce_exchange_identity_projects_refresh_and_revoke() -> None:
    calls: list[tuple[str, str, dict[str, list[str]]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        form = parse_qs(request.content.decode()) if request.content else {}
        calls.append((request.method, request.url.path, form))
        if request.url.path == "/oauth/token" and form.get("grant_type") == ["authorization_code"]:
            assert form["client_secret"] == ["client-secret"]
            assert form["code_verifier"][0]
            return httpx.Response(
                200,
                json={
                    "access_token": "access-one",
                    "refresh_token": "refresh-one",
                    "created_at": 1_700_000_000,
                    "expires_in": 7200,
                },
            )
        if request.url.path == "/oauth/token" and form.get("grant_type") == ["refresh_token"]:
            assert form["refresh_token"] == ["refresh-one"]
            return httpx.Response(
                200,
                json={
                    "access_token": "access-two",
                    "refresh_token": "refresh-two",
                    "created_at": datetime.now(timezone.utc).timestamp(),
                    "expires_in": 7200,
                },
            )
        if request.url.path == "/api/v4/user":
            assert request.headers["Authorization"] == "Bearer access-one"
            return httpx.Response(
                200,
                json={
                    "id": 42,
                    "username": "octo",
                    "name": "Octo Cat",
                    "web_url": "https://gitlab.com/octo",
                },
            )
        if request.url.path == "/api/v4/projects":
            assert request.url.params["membership"] == "true"
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 9,
                        "path_with_namespace": "octo/project",
                        "web_url": "https://gitlab.com/octo/project",
                    }
                ],
            )
        if request.url.path == "/oauth/revoke":
            assert form["token"] == ["access-two"]
            return httpx.Response(200, json={})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    cipher = SourceControlCipher("server-secret")
    store = MemoryStore()
    client = GitLabOAuthClient(
        GitLabOAuthConfig(
            client_id="client-id",
            client_secret="client-secret",
            redirect_uri="https://example.com/agents/connected?provider=gitlab",
        ),
        cipher=cipher,
        transport=httpx.MockTransport(handler),
    )

    authorization_url = await client.authorization_url(store, user_id="user-a")
    query = parse_qs(urlparse(authorization_url).query)
    assert query["scope"] == ["read_user read_api"]
    assert query["code_challenge_method"] == ["S256"]
    verifier = await consume_oauth_state(
        store,
        state=query["state"][0],
        user_id="user-a",
        provider="gitlab",
        cipher=cipher,
    )
    token = await client.exchange_code("callback-code", verifier or "")
    assert (await client.verify_user(token["access_token"]))["id"] == 42
    assert (await client.list_projects(token["access_token"]))[0]["name"] == "octo/project"

    connection = {
        "id": 7,
        "user_id": "user-a",
        "access_token_ciphertext": cipher.encrypt("access-one"),
        "refresh_token_ciphertext": cipher.encrypt("refresh-one"),
        "expires_at": datetime.now(timezone.utc) - timedelta(seconds=1),
    }
    refreshed = await client.access_token_for_connection(store, connection)
    assert refreshed == "access-two"
    assert store.token_update is not None
    assert "access-two" not in store.token_update["access_token_ciphertext"]
    assert "refresh-two" not in store.token_update["refresh_token_ciphertext"]
    await client.revoke(refreshed)
    assert any(path == "/oauth/revoke" for _, path, _ in calls)


@pytest.mark.asyncio
async def test_gitlab_network_failures_are_safe_integration_errors() -> None:
    """A provider outage must not escape as an unhandled HTTP client exception."""

    def unavailable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("provider unavailable", request=request)

    client = GitLabOAuthClient(
        GitLabOAuthConfig("client", "secret", "https://example.com/callback"),
        cipher=SourceControlCipher("server-secret"),
        transport=httpx.MockTransport(unavailable),
    )

    with pytest.raises(SourceControlError, match="GitLab API request failed"):
        await client.verify_user("secret-access-token")
