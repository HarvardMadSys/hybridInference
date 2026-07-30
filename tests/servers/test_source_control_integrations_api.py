"""HTTP contracts for the authenticated source-control integrations page."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from serving.agent_jobs.source_control import (
    GitLabOAuthClient,
    GitLabOAuthConfig,
    SourceControlCipher,
    github_authorization_url,
)
from serving.servers.deps import (
    get_agent_app_credentials,
    get_agent_gitlab_oauth,
    get_agent_job_store,
)
from serving.servers.routers.agent_jobs import require_agent_owner, router

pytestmark = pytest.mark.asyncio


class IntegrationStore:
    def __init__(self) -> None:
        self.states: dict[str, dict] = {}
        self.grants: dict[int, str | None] = {}
        self.gitlab: dict | None = None
        self.next_connection_id = 1

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

    async def record_repo_grant(self, *, user_id, installation_id, account_login=None):
        assert user_id == "user-owner"
        self.grants[installation_id] = account_login

    async def list_repo_grants(self, *, user_id):
        assert user_id == "user-owner"
        return [
            {"installation_id": installation_id, "account_login": account}
            for installation_id, account in self.grants.items()
        ]

    async def revoke_repo_grant(self, *, user_id, installation_id):
        assert user_id == "user-owner"
        return self.grants.pop(installation_id, None) is not None

    async def get_gitlab_connection(self, *, user_id):
        assert user_id == "user-owner"
        return self.gitlab

    async def upsert_gitlab_connection(self, **row):
        self.gitlab = {"id": self.next_connection_id, **row}
        return self.next_connection_id

    async def update_gitlab_tokens(self, *, connection_id, user_id, **tokens):
        assert self.gitlab and self.gitlab["id"] == connection_id
        assert self.gitlab["user_id"] == user_id
        self.gitlab.update(tokens)
        return True

    async def delete_gitlab_connection(self, *, user_id, connection_id):
        if self.gitlab and self.gitlab["user_id"] == user_id and self.gitlab["id"] == connection_id:
            self.gitlab = None
            return True
        return False


class FakeGitHubApp:
    user_authorization_configured = True

    def user_authorization_url(self, state: str) -> str:
        return github_authorization_url(
            "https://github.com/login/oauth/authorize?client_id=client-id",
            state=state,
        )

    async def exchange_user_code(self, code: str) -> str:
        assert code == "github-code"
        return "ephemeral-user-token"

    async def installations_for_user(self, token: str):
        assert token == "ephemeral-user-token"
        return [{"installation_id": 77, "account_login": "acme"}]

    async def repositories_for_installation(self, installation_id: int):
        assert installation_id == 77
        return ["acme/service"]


def build_app(store, *, github=None, gitlab=None) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_agent_owner] = lambda: {
        "user_id": "user-owner",
        "role": "internal",
    }
    app.dependency_overrides[get_agent_job_store] = lambda: store
    app.dependency_overrides[get_agent_app_credentials] = lambda: github
    app.dependency_overrides[get_agent_gitlab_oauth] = lambda: gitlab
    return app


async def test_github_status_connect_replay_and_disconnect(monkeypatch) -> None:
    monkeypatch.setenv("API_KEY_SECRET", "api-test-secret")
    monkeypatch.setenv(
        "AGENT_GITHUB_APP_INSTALL_URL",
        "https://github.com/apps/freeinference/installations/new",
    )
    store = IntegrationStore()
    app = build_app(store, github=FakeGitHubApp())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        status = await client.get("/v1/agent/integrations")
        github = status.json()["providers"][0]
        state = parse_qs(urlparse(github["connect_url"]).query)["state"][0]
        connected = await client.post(
            "/v1/agent/integrations/github/connect",
            json={"code": "github-code", "state": state},
        )
        replay = await client.post(
            "/v1/agent/integrations/github/connect",
            json={"code": "github-code", "state": state},
        )
        disconnected = await client.delete("/v1/agent/integrations/github/connections/77")
        reconnect_status = await client.get("/v1/agent/integrations")

    assert status.status_code == 200
    assert github["configured"] is True
    connect_url = urlparse(github["connect_url"])
    assert connect_url.path == "/login/oauth/authorize"
    assert parse_qs(connect_url.query)["client_id"] == ["client-id"]
    assert connected.json()["repos"] == ["acme/service"]
    assert replay.status_code == 400
    assert replay.json()["detail"]["error"]["type"] == "oauth_state_invalid"
    assert disconnected.json()["connections"] == []
    reconnect_url = urlparse(reconnect_status.json()["providers"][0]["connect_url"])
    assert reconnect_url.path == "/login/oauth/authorize"


async def test_github_oauth_without_installation_offers_install_step(monkeypatch) -> None:
    class GitHubAppWithoutInstallation(FakeGitHubApp):
        async def installations_for_user(self, token: str):
            assert token == "ephemeral-user-token"
            return []

    monkeypatch.setenv("API_KEY_SECRET", "api-test-secret")
    monkeypatch.setenv(
        "AGENT_GITHUB_APP_INSTALL_URL",
        "https://github.com/apps/freeinference/installations/new",
    )
    store = IntegrationStore()
    app = build_app(store, github=GitHubAppWithoutInstallation())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        status = await client.get("/v1/agent/integrations")
        connect_url = urlparse(status.json()["providers"][0]["connect_url"])
        oauth_state = parse_qs(connect_url.query)["state"][0]
        connected = await client.post(
            "/v1/agent/integrations/github/connect",
            json={"code": "github-code", "state": oauth_state},
        )

    assert connected.status_code == 200
    install_url = urlparse(connected.json()["install_url"])
    assert install_url.path == "/apps/freeinference/installations/new"
    assert parse_qs(install_url.query)["state"][0] != oauth_state
    assert connected.json()["connections"] == []


async def test_gitlab_connect_lists_projects_stores_ciphertext_and_revokes(monkeypatch) -> None:
    monkeypatch.setenv("API_KEY_SECRET", "api-test-secret")
    revoked: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        form = parse_qs(request.content.decode()) if request.content else {}
        if request.url.path == "/oauth/token":
            return httpx.Response(
                200,
                json={
                    "access_token": "gitlab-access-token",
                    "refresh_token": "gitlab-refresh-token",
                    "created_at": datetime.now(timezone.utc).timestamp(),
                    "expires_in": 7200,
                },
            )
        if request.url.path == "/api/v4/user":
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
            revoked.extend(form["token"])
            return httpx.Response(200, json={})
        raise AssertionError(f"unexpected request {request.url}")

    store = IntegrationStore()
    gitlab = GitLabOAuthClient(
        GitLabOAuthConfig(
            client_id="client-id",
            client_secret="client-secret",
            redirect_uri="https://example.com/agents/connected?provider=gitlab",
        ),
        cipher=SourceControlCipher("api-test-secret"),
        transport=httpx.MockTransport(handler),
    )
    app = build_app(store, gitlab=gitlab)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        status = await client.get("/v1/agent/integrations")
        gitlab_status = status.json()["providers"][1]
        state = parse_qs(urlparse(gitlab_status["connect_url"]).query)["state"][0]
        connected = await client.post(
            "/v1/agent/integrations/gitlab/connect",
            json={"code": "gitlab-code", "state": state},
        )
        persisted_tokens = {
            "access": store.gitlab["access_token_ciphertext"],
            "refresh": store.gitlab["refresh_token_ciphertext"],
        }
        wrong_connection = await client.delete("/v1/agent/integrations/gitlab/connections/999")
        disconnected = await client.delete("/v1/agent/integrations/gitlab/connections/1")

    assert connected.status_code == 200
    assert connected.json()["repositories"][0]["name"] == "octo/project"
    assert "gitlab-access-token" not in json.dumps(connected.json())
    assert "gitlab-access-token" not in persisted_tokens["access"]
    assert "gitlab-refresh-token" not in persisted_tokens["refresh"]
    assert store.gitlab is None
    persisted_state = next(iter(store.states))
    assert persisted_state != parse_qs(urlparse(gitlab_status["connect_url"]).query)["state"][0]
    # Raw tokens were present only transiently; what reached persistence was ciphertext.
    # The local row is deleted now, while revocation still received the decrypted access token.
    assert wrong_connection.status_code == 404
    assert disconnected.status_code == 200
    assert revoked == ["gitlab-access-token"]


async def test_gitlab_callback_state_is_bound_to_authenticated_user(monkeypatch) -> None:
    monkeypatch.setenv("API_KEY_SECRET", "api-test-secret")
    store = IntegrationStore()
    state = "wrong-user-state-value"
    await store.create_oauth_state(
        state_hash=hashlib.sha256(state.encode()).hexdigest(),
        user_id="user-other",
        provider="gitlab",
        code_verifier_ciphertext=SourceControlCipher("api-test-secret").encrypt("verifier"),
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    gitlab = GitLabOAuthClient(
        GitLabOAuthConfig("client", "secret", "https://example.com/callback"),
        cipher=SourceControlCipher("api-test-secret"),
        transport=httpx.MockTransport(lambda request: pytest.fail("network must not be called")),
    )
    app = build_app(store, gitlab=gitlab)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/agent/integrations/gitlab/connect",
            json={"code": "code", "state": state},
        )
    assert response.status_code == 400
    assert response.json()["detail"]["error"]["type"] == "oauth_state_invalid"
