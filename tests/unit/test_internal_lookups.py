"""Tests for the three lookups the control plane loses at the split.

The assertion that matters most is negative: the MCP registry endpoint must
never return a server's URL or headers. The headers carry this deployment's
upstream credential, and keeping them on this side is a large part of what the
split is for.
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from serving.agent_jobs.mcp_registry import McpRegistry, McpServer
from serving.servers.deps import (
    get_model_visibility_resolver,
    get_operational_store,
    get_router,
)
from serving.servers.middleware.error import install_error_handlers
from serving.servers.routers import internal_lookups
from serving.servers.routers.internal_auth import ENV_DISPATCH_TOKEN

DISPATCH = "dispatch-secret-value"
AUTH = {"Authorization": f"Bearer {DISPATCH}"}

UPSTREAM_URL = "https://github.example/mcp"
UPSTREAM_CREDENTIAL = "upstream-bearer-credential"


class FakeStore:
    def __init__(self) -> None:
        self.users: dict[str, dict[str, Any]] = {
            "user_1": {
                "id": "user_1",
                "role": "pro",
                "status": "active",
                "email": "a@b.test",
            }
        }

    async def get_user_by_id(self, user_id: str):
        return self.users.get(user_id)


@pytest.fixture
def store() -> FakeStore:
    return FakeStore()


@pytest.fixture(autouse=True)
def _dispatch_token(monkeypatch) -> None:
    monkeypatch.setenv(ENV_DISPATCH_TOKEN, DISPATCH)


@pytest.fixture(autouse=True)
def _registry(monkeypatch) -> None:
    registry = McpRegistry(
        servers={
            "github": McpServer(
                name="github",
                url=UPSTREAM_URL,
                headers={"Authorization": f"Bearer {UPSTREAM_CREDENTIAL}"},
                tools=frozenset({"search", "issues"}),
                description="Repository search and issues",
                default=True,
            )
        }
    )
    monkeypatch.setattr(internal_lookups, "get_registry", lambda: registry)


@pytest.fixture(autouse=True)
def _visible_models(monkeypatch) -> None:
    async def _visible(_router, **kwargs):
        role = (kwargs.get("user_ctx") or {}).get("role")
        return ["glm-5.1", "kimi-k2"] if role == "pro" else ["glm-5.1"]

    monkeypatch.setattr(internal_lookups, "agent_visible_models", _visible)


class _FakeRoute:
    def __init__(self, canonical: str) -> None:
        self.canonical_model_id = canonical


class _FakeRouter:
    """A route table shaped like the real one: aliases and canonicals are peers.

    `glm-5.1` and `kimi-k2` are the visible pair the catalog fixture returns.
    The rest are the three cases the alias map has to get right.
    """

    routes: ClassVar[dict[str, _FakeRoute]] = {
        "glm-5.1": _FakeRoute("glm-5.1"),
        "glm-latest": _FakeRoute("glm-5.1"),
        "kimi-k2": _FakeRoute("kimi-k2"),
        # Points at a model this user cannot see.
        "secret-alias": _FakeRoute("admin-only-model"),
        "admin-only-model": _FakeRoute("admin-only-model"),
        # The shadowing shape, as it actually appears: some model declared
        # `glm-5.1` as its own alias, so its entry overwrote glm-5.1's own —
        # which is still reachable through `glm-latest`, and so still shows up
        # as a canonical id that this key is now pointing away from.
        "glm-5.1-conflicted": _FakeRoute("kimi-k2"),
    }


@pytest.fixture
def client(store: FakeStore) -> TestClient:
    app = FastAPI()
    app.include_router(internal_lookups.router)
    install_error_handlers(app)
    app.dependency_overrides[get_operational_store] = lambda: store
    app.dependency_overrides[get_router] = lambda: _FakeRouter()
    app.dependency_overrides[get_model_visibility_resolver] = lambda: None
    return TestClient(app)


# ---------------------------------------------------------------------------
# Authorization
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/internal/mcp-registry",
        "/internal/model-catalog?user_id=user_1",
        "/internal/users/x/status",
    ],
)
def test_every_lookup_requires_the_dispatch_token(client: TestClient, path: str) -> None:
    assert client.get(path).status_code == 401


def test_lookups_are_absent_when_the_token_is_unconfigured(client, monkeypatch) -> None:
    monkeypatch.delenv(ENV_DISPATCH_TOKEN, raising=False)
    assert client.get("/internal/mcp-registry", headers=AUTH).status_code == 404


# ---------------------------------------------------------------------------
# MCP registry — the credential must not travel
# ---------------------------------------------------------------------------


def test_the_registry_never_returns_a_url_or_a_header(client: TestClient) -> None:
    """The whole point: upstream credentials stay on this side of the split.

    Asserted against the serialized body, not just the parsed fields, so a
    credential smuggled into any nested structure still fails the test.
    """
    body = client.get("/internal/mcp-registry", headers=AUTH).json()
    raw = json.dumps(body)
    assert UPSTREAM_CREDENTIAL not in raw
    assert UPSTREAM_URL not in raw
    assert "headers" not in raw
    (server,) = body["servers"]
    assert set(server) == {"name", "description", "default", "tools", "unfiltered"}


def test_the_registry_returns_what_a_picker_needs(client: TestClient) -> None:
    (server,) = client.get("/internal/mcp-registry", headers=AUTH).json()["servers"]
    assert server["name"] == "github"
    assert server["description"] == "Repository search and issues"
    assert server["default"] is True
    assert server["tools"] == ["issues", "search"]


# ---------------------------------------------------------------------------
# Model catalog
# ---------------------------------------------------------------------------


def test_the_catalog_is_resolved_for_the_named_user_s_role(client: TestClient) -> None:
    body = client.get("/internal/model-catalog?user_id=user_1", headers=AUTH).json()
    assert body["role"] == "pro"
    assert body["models"] == ["glm-5.1", "kimi-k2"]


def test_the_catalog_is_not_the_anonymous_one(client: TestClient, store: FakeStore) -> None:
    """The reason this is not GET /v1/models.

    That route cannot authenticate an identity JWT, so a control plane calling
    it would receive the anonymous catalog and every non-free user would
    silently lose models their role can reach. A free user seeing fewer models
    than a pro user is what proves the role actually reached the resolver.
    """
    pro = client.get("/internal/model-catalog?user_id=user_1", headers=AUTH).json()
    store.users["user_1"]["role"] = "free"
    free = client.get("/internal/model-catalog?user_id=user_1", headers=AUTH).json()
    assert len(free["models"]) < len(pro["models"])


def test_the_catalog_hides_a_model_the_owner_disabled(client, store, monkeypatch) -> None:
    """The catalog is what the composer offers, so a model the owner turned off
    must not appear in it — and this endpoint resolves visibility the same way
    the mint does, with the same omission.

    Two consequences if it is missing: the picker offers a model the user
    disabled, and the grant minted from that choice carries it.
    """
    seen: dict = {}

    async def _visible(_router, **kwargs):
        seen.update(kwargs)
        denied = set((kwargs.get("user_ctx") or {}).get("disabled_models") or [])
        return [m for m in ["glm-5.1", "kimi-k2"] if m not in denied]

    monkeypatch.setattr(internal_lookups, "agent_visible_models", _visible)
    store.users["user_1"]["preferences"] = {"disabled_models": ["glm-5.1"]}

    body = client.get("/internal/model-catalog", params={"user_id": "user_1"}, headers=AUTH).json()

    assert seen["user_ctx"]["disabled_models"] == ["glm-5.1"]
    assert body["models"] == ["kimi-k2"]


@pytest.mark.parametrize("status_value", ["suspended", "deleted"])
def test_the_catalog_refuses_an_inactive_account(client, store, status_value: str) -> None:
    store.users["user_1"]["status"] = status_value
    assert client.get("/internal/model-catalog?user_id=user_1", headers=AUTH).status_code == 403


def test_the_catalog_refuses_an_unknown_user(client: TestClient) -> None:
    assert client.get("/internal/model-catalog?user_id=ghost", headers=AUTH).status_code == 403


# ---------------------------------------------------------------------------
# User status
# ---------------------------------------------------------------------------


def test_user_status_reports_an_active_account(client: TestClient) -> None:
    body = client.get("/internal/users/user_1/status", headers=AUTH).json()
    assert body == {
        "user_id": "user_1",
        "exists": True,
        "active": True,
        "status": "active",
        "role": "pro",
        "email": "a@b.test",
    }


def test_user_status_distinguishes_gone_from_suspended(client, store) -> None:
    """The caller archives a thread for one and shows a message for the other.

    Collapsing both into a 403 would make those indistinguishable, which is why
    this endpoint answers 200 with a flag rather than refusing.
    """
    store.users["user_1"]["status"] = "suspended"
    suspended = client.get("/internal/users/user_1/status", headers=AUTH).json()
    assert suspended["exists"] is True
    assert suspended["active"] is False
    assert suspended["status"] == "suspended"

    gone = client.get("/internal/users/ghost/status", headers=AUTH).json()
    assert gone["exists"] is False
    assert gone["active"] is False


def test_user_status_carries_no_password_or_key_material(client: TestClient, store) -> None:
    """The control plane gets an answer, not a copy of the users table."""
    store.users["user_1"]["password_hash"] = "$argon2id$fake"
    body = client.get("/internal/users/user_1/status", headers=AUTH).json()
    assert "password_hash" not in json.dumps(body)


# ── The alias translation table ────────────────────────────────────────
#
# Before the split the control plane read this registry in-process and got
# alias resolution for free. Afterwards it asks over HTTP, and a contract of
# canonical ids alone silently stopped accepting every alias a user had been
# typing. The table is how it resolves once and works in canonical ids from
# there — nothing downstream, grant or scope check, ever sees an alias.


def test_the_catalog_carries_the_translation_table(client: TestClient) -> None:
    body = client.get("/internal/model-catalog", params={"user_id": "user_1"}, headers=AUTH).json()

    assert body["aliases"]["glm-latest"] == "glm-5.1"
    assert body["aliases"]["glm-latest"] in body["models"]


def test_an_alias_for_an_invisible_model_is_not_offered(client: TestClient) -> None:
    """It would resolve to a model the catalog does not list, and the caller
    would be refused a moment later with nothing to explain it."""
    body = client.get("/internal/model-catalog", params={"user_id": "user_1"}, headers=AUTH).json()

    assert "secret-alias" not in body["aliases"]
    assert "admin-only-model" not in body["models"]


def test_an_alias_spelled_like_a_canonical_id_is_dropped(client: TestClient) -> None:
    """**The one that would shadow a real model.**

    A consumer resolves with `aliases.get(name, name)`. An entry keyed on some
    model's canonical id would send every request for *that* model somewhere
    else — and the request would look perfectly valid at both ends.
    """
    body = client.get("/internal/model-catalog", params={"user_id": "user_1"}, headers=AUTH).json()

    body_aliases = body["aliases"]
    assert not (set(body_aliases) & set(body["models"])), (
        "an alias is spelled like a model this catalog lists"
    )


def test_every_alias_target_is_a_model_the_catalog_lists(client: TestClient) -> None:
    """The invariant a consumer is entitled to assume, stated as a whole."""
    body = client.get("/internal/model-catalog", params={"user_id": "user_1"}, headers=AUTH).json()

    assert set(body["aliases"].values()) <= set(body["models"])
