"""API key authorization: callbacks, PKCE codes and the three endpoints."""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from serving.config.settings import get_settings
from serving.exceptions import APIKeyLimitReachedError
from serving.servers.deps import get_current_user, get_db_logger, get_operational_store
from serving.servers.middleware.error import install_error_handlers
from serving.servers.routers import key_authorization as router_module, user_routes
from serving.utils import key_authorization, key_exchange_rate_limit
from serving.utils.identity_tokens import hash_code
from serving.utils.key_authorization import (
    CODE_TTL_SECONDS,
    ENV_CALLBACKS,
    InvalidKeyAuthorizationRequest,
    KeyAuthorizationNotConfigured,
    account_refusal,
    allowed_callback,
    check_request,
    parse_callback,
    validate_callbacks_setting,
    with_parameter,
)
from serving.utils.request_ip import ClientIpInfo

CALLBACK = "https://app.example.org/model-connections/callback"
LOCAL_CALLBACK = "http://localhost:3000/callback"
VERIFIER = "v" * 64


def _challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


CHALLENGE = _challenge(VERIFIER)


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    """The allowlist every test starts from; a test that wants it off clears it."""
    monkeypatch.setenv(ENV_CALLBACKS, f" {CALLBACK} , ,{LOCAL_CALLBACK} ")
    key_exchange_rate_limit.reset_key_exchange_rate_limit_state()
    yield
    key_exchange_rate_limit.reset_key_exchange_rate_limit_state()


def _check(callback_url: str, *, challenge: str = CHALLENGE, method: str = "S256"):
    return check_request(callback_url=callback_url, code_challenge=challenge, method=method)


# ---------------------------------------------------------------------------
# The allowlist
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("callback", "host"),
    [
        (CALLBACK, "app.example.org"),
        # The application's own query is allowed and ignored for matching.
        (CALLBACK + "?state=s-123&provider=x", "app.example.org"),
        (CALLBACK + "?", "app.example.org"),
        # Host names are case-insensitive; a browser lower-cases them too.
        ("https://APP.Example.ORG/model-connections/callback", "app.example.org"),
        # The scheme's default port, written out, is the same origin.
        ("https://app.example.org:443/model-connections/callback", "app.example.org"),
        (LOCAL_CALLBACK + "?state=dev", "localhost:3000"),
    ],
)
def test_a_listed_callback_is_accepted(callback: str, host: str) -> None:
    accepted = _check(callback)
    assert accepted.url == callback
    assert accepted.host == host


@pytest.mark.parametrize(
    "callback",
    [
        # Near misses: equality of scheme, host, port and path, never a prefix.
        CALLBACK + "/extra",
        CALLBACK + "x",
        CALLBACK + "/",
        "https://app.example.org/model-connections/Callback",
        "https://app.example.org/model-connections/callback/../callback",
        "https://app.example.org/model-connections/%63allback",
        "https://app.example.org.attacker.test/model-connections/callback",
        "https://evilapp.example.org/model-connections/callback",
        "https://example.org/model-connections/callback",
        # Port and scheme are part of the match.
        "https://app.example.org:8443/model-connections/callback",
        "http://app.example.org/model-connections/callback",
        "https://localhost:3000/callback",
        "http://localhost:3001/callback",
        "http://127.0.0.1:3000/callback",
        # Never user information or a fragment, even around a listed callback.
        "https://app.example.org@attacker.test/model-connections/callback",
        "https://user@app.example.org/model-connections/callback",
        "https://user:pass@app.example.org/model-connections/callback",
        CALLBACK + "#fragment",
        CALLBACK + "#",
        CALLBACK + "?state=x#frag",
        # Nothing a browser parses differently.
        "https://app.example.org\\@attacker.test/model-connections/callback",
        "https://app.example.org/model-connections/callback\\",
        "https://app.example.org/model-\nconnections/callback",
        "https://app.example.org/model-connections/callback?state=a b",
        "https://app.exämple.org/model-connections/callback",
        "https:/app.example.org/model-connections/callback",
        "https:app.example.org/model-connections/callback",
        "//app.example.org/model-connections/callback",
        "/model-connections/callback",
        "javascript:alert(1)//app.example.org/model-connections/callback",
        # The parameters the gateway appends are its own.
        CALLBACK + "?code=planted",
        CALLBACK + "?state=x&error=access_denied",
        "",
        "not a url",
        "https://app.example.org:99999/model-connections/callback",
        CALLBACK + "?state=" + "s" * 2048,
    ],
)
def test_anything_else_is_refused(callback: str) -> None:
    with pytest.raises(InvalidKeyAuthorizationRequest):
        _check(callback)


def test_http_is_accepted_only_for_localhost_and_127_0_0_1(monkeypatch) -> None:
    validate_callbacks_setting("http://localhost:8080/cb,http://127.0.0.1/cb")
    for entry in ("http://app.example.org/cb", "http://10.0.0.5/cb", "http://[::1]/cb"):
        with pytest.raises(ValueError):
            validate_callbacks_setting(entry)
    monkeypatch.setenv(ENV_CALLBACKS, "http://127.0.0.1/cb")
    assert _check("http://127.0.0.1:80/cb?state=x").host == "127.0.0.1"


@pytest.mark.parametrize("raw", ["", "  ", " , ,"])
def test_an_empty_allowlist_turns_the_feature_off(monkeypatch, raw: str) -> None:
    monkeypatch.setenv(ENV_CALLBACKS, raw)
    assert not key_authorization.is_configured()
    with pytest.raises(KeyAuthorizationNotConfigured):
        _check(CALLBACK)


def test_the_setting_refuses_an_entry_that_could_never_match() -> None:
    validate_callbacks_setting(f"{CALLBACK}, {LOCAL_CALLBACK}")
    validate_callbacks_setting("")
    for bad in (
        CALLBACK + "?state=fixed",
        CALLBACK + "#x",
        "https://user@app.example.org/cb",
        "ftp://app.example.org/cb",
        "app.example.org/cb",
    ):
        with pytest.raises(ValueError, match="callback 2"):
            validate_callbacks_setting(f"{CALLBACK},{bad}")


def test_an_entry_from_the_environment_that_does_not_parse_is_ignored(monkeypatch) -> None:
    monkeypatch.setenv(ENV_CALLBACKS, f"{CALLBACK}?state=x, not-a-url")
    assert not key_authorization.is_configured()
    monkeypatch.setenv(ENV_CALLBACKS, f"not-a-url, {CALLBACK}")
    assert _check(CALLBACK).host == "app.example.org"


def test_allowed_callback_follows_the_current_allowlist(monkeypatch) -> None:
    assert allowed_callback(CALLBACK) is not None
    monkeypatch.setenv(ENV_CALLBACKS, LOCAL_CALLBACK)
    assert allowed_callback(CALLBACK) is None
    assert allowed_callback("garbage") is None


def test_a_non_default_port_is_part_of_the_name() -> None:
    assert parse_callback("https://app.example.org:8443/cb").host == "app.example.org:8443"
    assert parse_callback("https://app.example.org/cb").without_query == (
        "https://app.example.org/cb"
    )
    assert parse_callback("https://app.example.org/cb?state=1").without_query == (
        "https://app.example.org/cb"
    )


# ---------------------------------------------------------------------------
# PKCE request fields
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("challenge", "method"),
    [
        (CHALLENGE, "plain"),
        (CHALLENGE, "s256"),
        (CHALLENGE, ""),
        (CHALLENGE + "=", "S256"),
        (CHALLENGE[:-1], "S256"),
        (CHALLENGE + "A", "S256"),
        ("+" + CHALLENGE[1:], "S256"),
        ("/" + CHALLENGE[1:], "S256"),
        ("", "S256"),
    ],
)
def test_only_an_s256_challenge_of_43_base64url_characters_is_accepted(
    challenge: str, method: str
) -> None:
    with pytest.raises(InvalidKeyAuthorizationRequest):
        _check(CALLBACK, challenge=challenge, method=method)


def test_the_verifier_follows_rfc_7636() -> None:
    assert key_authorization.valid_verifier("a" * 43)
    assert key_authorization.valid_verifier("aZ09-._~" * 16)
    assert not key_authorization.valid_verifier("a" * 42)
    assert not key_authorization.valid_verifier("a" * 129)
    assert not key_authorization.valid_verifier("a" * 42 + "+")


# ---------------------------------------------------------------------------
# Building the redirect
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("callback", "expected"),
    [
        (CALLBACK, CALLBACK + "?code=c0de"),
        (CALLBACK + "?", CALLBACK + "?code=c0de"),
        (CALLBACK + "?state=a%2Bb%20c&x=", CALLBACK + "?state=a%2Bb%20c&x=&code=c0de"),
        (CALLBACK + "?state=s&", CALLBACK + "?state=s&code=c0de"),
    ],
)
def test_the_code_is_appended_and_the_query_kept_exactly(callback: str, expected: str) -> None:
    assert with_parameter(callback, "code", "c0de") == expected


def test_an_appended_value_is_encoded() -> None:
    assert with_parameter(CALLBACK, "error", "a&b=c") == CALLBACK + "?error=a%26b%3Dc"


# ---------------------------------------------------------------------------
# Who may authorize a key
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("user", "refusal"),
    [
        ({"status": "active", "email": "a@example.org", "email_verified": True}, None),
        ({"status": "active", "email": "a@example.org", "email_verified": False}, "unverified"),
        (
            {"status": "pending_approval", "email": "a@example.org", "email_verified": True},
            "inactive",
        ),
        ({"status": "suspended", "email": "a@example.org", "email_verified": True}, "inactive"),
        ({"status": "deleted", "email": "a@example.org", "email_verified": True}, "inactive"),
        ({"status": "suspended", "email": "a@example.org", "email_verified": False}, "inactive"),
        # Accounts without an email: a login name, or a proven phone.
        ({"status": "active", "email": None, "login_name": "admin"}, None),
        ({"status": "active", "email": None, "phone": "+15550100", "phone_verified": True}, None),
        (
            {"status": "active", "email": None, "phone": "+15550100", "phone_verified": False},
            "unverified",
        ),
        (None, "inactive"),
    ],
)
def test_the_account_must_be_active_and_verified(user, refusal) -> None:
    assert account_refusal(user) == refusal


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


class FakeStore:
    """Codes with real single-use semantics, users, and keys with the account ceiling."""

    def __init__(self) -> None:
        self.codes: dict[str, dict[str, Any]] = {}
        self.users: dict[str, dict[str, Any]] = {}
        self.keys: list[dict[str, Any]] = []
        self.active_keys: dict[str, int] = {}

    async def create_api_key_authorization_code(
        self,
        *,
        code_hash: str,
        user_id: str,
        callback_url: str,
        code_challenge: str,
        expires_at: datetime,
    ) -> None:
        self.codes[code_hash] = {
            "user_id": user_id,
            "callback_url": callback_url,
            "code_challenge": code_challenge,
            "expires_at": expires_at,
            "consumed_at": None,
        }

    async def consume_api_key_authorization_code(self, code_hash: str) -> dict[str, Any] | None:
        row = self.codes.get(code_hash)
        if row is None or row["consumed_at"] is not None or row["expires_at"] <= datetime.now(UTC):
            return None
        row["consumed_at"] = datetime.now(UTC)
        return {k: row[k] for k in ("user_id", "callback_url", "code_challenge")}

    async def get_user_by_id(self, user_id: str) -> dict[str, Any] | None:
        return self.users.get(user_id)

    async def count_active_keys(self, user_id: str) -> int:
        return self.active_keys.get(user_id, 0)

    async def get_default_project(self, user_id: str) -> dict[str, Any] | None:
        return {"id": f"proj-{user_id}", "name": "Default"} if user_id in self.users else None

    async def get_project(self, user_id: str, project_id: str) -> dict[str, Any] | None:
        return None

    async def create_key(self, **kwargs: Any) -> dict[str, Any]:
        user_id = kwargs["user_id"]
        if self.active_keys.get(user_id, 0) >= kwargs["max_active_keys"]:
            raise APIKeyLimitReachedError(kwargs["max_active_keys"])
        self.active_keys[user_id] = self.active_keys.get(user_id, 0) + 1
        self.keys.append(kwargs)
        return {"id": len(self.keys), "created_at": datetime.now(UTC)}


class AuditLog:
    """Stands in for the audit store: records each action."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    async def log_admin_action(self, **kwargs: Any) -> None:
        self.rows.append(kwargs)


ACCOUNT = {
    "id": "user_1",
    "email": "owner@example.org",
    "email_verified": True,
    "role": "pro",
    "status": "active",
}


@pytest.fixture
def store() -> FakeStore:
    fake = FakeStore()
    fake.users["user_1"] = dict(ACCOUNT)
    return fake


@pytest.fixture
def audit() -> AuditLog:
    return AuditLog()


@pytest.fixture
def session() -> dict[str, Any]:
    """The signed-in account, as get_current_user resolves it."""
    return {
        "user_id": "user_1",
        "email": "owner@example.org",
        "email_verified": True,
        "role": "pro",
        "status": "active",
    }


@pytest.fixture
def client(monkeypatch, store: FakeStore, audit: AuditLog, session: dict[str, Any]) -> TestClient:
    monkeypatch.setenv("API_KEY_SECRET", "unit-test-secret")
    get_settings.cache_clear()
    app = FastAPI()
    app.include_router(router_module.router)
    install_error_handlers(app)
    app.dependency_overrides[get_operational_store] = lambda: store
    app.dependency_overrides[get_current_user] = lambda: session
    app.dependency_overrides[get_db_logger] = lambda: audit
    yield TestClient(app)
    get_settings.cache_clear()


def _body(**overrides: Any) -> dict[str, Any]:
    body = {
        "callback_url": CALLBACK + "?state=s-123",
        "code_challenge": CHALLENGE,
        "code_challenge_method": "S256",
    }
    body.update(overrides)
    return body


def _code(client: TestClient, **overrides: Any) -> Any:
    return client.post("/v1/auth/keys/code", json=_body(**overrides))


def _exchange(client: TestClient, code: str, **overrides: Any) -> Any:
    body = {"code": code, "code_verifier": VERIFIER, "code_challenge_method": "S256"}
    body.update(overrides)
    return client.post("/v1/auth/keys", json=body)


def _issued_code(client: TestClient) -> str:
    response = _code(client)
    assert response.status_code == 200, response.text
    return response.json()["code"]


# --- check --------------------------------------------------------------------


def test_check_names_the_application_by_its_host_and_says_where_cancel_goes(client) -> None:
    response = client.post("/v1/auth/keys/check", json=_body())
    assert response.status_code == 200
    assert response.json() == {
        "app": "app.example.org",
        "cancel_url": CALLBACK + "?state=s-123&error=access_denied",
    }


@pytest.mark.parametrize(
    "overrides",
    [
        {"callback_url": "https://attacker.test/model-connections/callback"},
        {"callback_url": CALLBACK + "#x"},
        {"code_challenge_method": "plain"},
        {"code_challenge": "short"},
    ],
)
def test_check_refuses_an_unacceptable_request_without_saying_why(client, overrides) -> None:
    response = client.post("/v1/auth/keys/check", json=_body(**overrides))
    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_request"
    assert "attacker" not in response.text


def test_check_says_when_the_feature_is_off(client, monkeypatch) -> None:
    monkeypatch.setenv(ENV_CALLBACKS, "")
    response = client.post("/v1/auth/keys/check", json=_body())
    assert response.status_code == 404
    assert response.json()["error"]["type"] == "not_configured"


def test_without_a_database_nothing_is_offered(client, monkeypatch) -> None:
    client.app.dependency_overrides[get_operational_store] = lambda: None
    assert client.post("/v1/auth/keys/check", json=_body()).status_code == 404
    assert _code(client).status_code == 404
    assert _exchange(client, "anything").status_code == 404


# --- code ---------------------------------------------------------------------


def test_a_code_is_issued_for_the_signed_in_account(client, store: FakeStore) -> None:
    response = _code(client)

    assert response.status_code == 200
    payload = response.json()
    assert payload["expires_in"] == CODE_TTL_SECONDS == 180
    assert payload["app"] == "app.example.org"
    # The application's query comes back exactly, with the code after it.
    assert payload["redirect_url"] == f"{CALLBACK}?state=s-123&code={payload['code']}"
    assert response.headers["cache-control"] == "no-store"

    # Stored hashed, never in the clear, without the application's state.
    assert payload["code"] not in store.codes
    row = store.codes[hash_code(payload["code"])]
    assert row["user_id"] == "user_1"
    assert row["callback_url"] == CALLBACK
    assert row["code_challenge"] == CHALLENGE
    lifetime = row["expires_at"] - datetime.now(UTC)
    assert timedelta(seconds=170) < lifetime <= timedelta(seconds=180)


def test_the_code_is_appended_with_a_question_mark_when_the_callback_has_no_query(client) -> None:
    payload = _code(client, callback_url=CALLBACK).json()
    assert payload["redirect_url"] == f"{CALLBACK}?code={payload['code']}"


def test_an_agent_style_callback_round_trips_its_opaque_state(client, monkeypatch) -> None:
    """The shape a consuming web app sends: its settings page, with ``state`` in the query."""
    listed = "https://agent.example.org/settings/models/connected"
    callback = f"{listed}?state=Ab-9_x.y~z%2Bq%2F%3D"
    monkeypatch.setenv(ENV_CALLBACKS, listed)

    check = client.post("/v1/auth/keys/check", json=_body(callback_url=callback)).json()
    issued = _code(client, callback_url=callback).json()

    assert check == {"app": "agent.example.org", "cancel_url": f"{callback}&error=access_denied"}
    assert issued["redirect_url"] == f"{callback}&code={issued['code']}"
    assert _exchange(client, issued["code"]).status_code == 200


@pytest.mark.parametrize(
    "overrides",
    [
        {"callback_url": "https://attacker.test/cb"},
        {"callback_url": "https://user@app.example.org/model-connections/callback"},
        {"code_challenge_method": "plain"},
        {"code_challenge": CHALLENGE + "="},
    ],
)
def test_the_code_endpoint_revalidates_the_request(client, store, overrides) -> None:
    response = _code(client, **overrides)
    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_request"
    assert store.codes == {}


def test_no_code_while_the_feature_is_off(client, store, monkeypatch) -> None:
    monkeypatch.setenv(ENV_CALLBACKS, " ")
    response = _code(client)
    assert response.status_code == 404
    assert response.json()["error"]["type"] == "not_configured"
    assert store.codes == {}


def test_an_unverified_account_is_asked_to_verify_its_email(client, store, session) -> None:
    session["email_verified"] = False
    response = _code(client)
    assert response.status_code == 403
    assert response.json()["error"]["type"] == "email_not_verified"
    assert "verify your email" in response.json()["error"]["message"].lower()
    assert store.codes == {}


@pytest.mark.parametrize("status", ["pending_approval", "suspended", "deleted"])
def test_an_inactive_account_is_refused(client, store, session, status: str) -> None:
    session["status"] = status
    response = _code(client)
    assert response.status_code == 403
    assert response.json()["error"]["type"] == "account_unavailable"
    assert store.codes == {}


def test_an_account_at_the_key_ceiling_is_refused_before_a_code_exists(
    client, store, monkeypatch
) -> None:
    monkeypatch.setattr(user_routes, "MAX_ACTIVE_API_KEYS_PER_ACCOUNT", 3)
    store.active_keys["user_1"] = 3
    response = _code(client)
    assert response.status_code == 409
    assert response.json()["error"]["type"] == "key_limit_reached"
    assert "3" in response.json()["error"]["message"]
    assert store.codes == {}


# --- exchange -----------------------------------------------------------------


def test_the_exchange_creates_an_ordinary_key_on_the_account(client, store, audit) -> None:
    code = _issued_code(client)

    response = _exchange(client, code)

    assert response.status_code == 200, response.text
    payload = response.json()
    assert set(payload) == {"key", "user_id"}
    assert payload["user_id"] == "user_1"
    assert payload["key"].startswith("hyi-")
    assert response.headers["cache-control"] == "no-store"

    (created,) = store.keys
    # The same path as POST /user/api-keys: Default project, the account's
    # quota, the ceiling, a revealable copy.
    assert created["user_id"] == created["account_id"] == "user_1"
    assert created["project_id"] == "proj-user_1"
    assert created["inherit_account_quota"] is True
    assert created["max_active_keys"] == user_routes.MAX_ACTIVE_API_KEYS_PER_ACCOUNT
    assert created["key_prefix"] == payload["key"][:12]
    assert created["api_key_encrypted"] and payload["key"] not in created["api_key_encrypted"]
    # Named after the application, and recorded as made by authorization.
    assert created["name"] == "app.example.org"
    assert created["notes"] == "Created by API key authorization for app.example.org"
    assert json.loads(created["metadata"]) == {
        "created_by": "api_key_authorization",
        "callback_host": "app.example.org",
    }

    (row,) = audit.rows
    assert row["action"] == "create_key"
    assert row["details"]["actor"] == "api_key_authorization"
    assert row["details"]["callback_host"] == "app.example.org"
    assert row["details"]["key_prefix"] == created["key_prefix"]
    assert payload["key"] not in json.dumps(row["details"])


def test_a_code_is_exchanged_once(client, store) -> None:
    code = _issued_code(client)
    assert _exchange(client, code).status_code == 200
    replay = _exchange(client, code)
    assert replay.status_code == 400
    assert replay.json() == {
        "error": "invalid_grant",
        "error_description": router_module._INVALID_GRANT,
    }
    assert len(store.keys) == 1


def test_a_wrong_verifier_is_refused_and_burns_the_code(client, store) -> None:
    code = _issued_code(client)
    wrong = _exchange(client, code, code_verifier="w" * 64)
    assert wrong.status_code == 400
    assert wrong.json()["error"] == "invalid_grant"
    # One attempt per code, whatever its outcome.
    assert _exchange(client, code).status_code == 400
    assert store.keys == []


def test_an_unknown_and_an_expired_code_get_the_same_answer(client, store) -> None:
    code = _issued_code(client)
    store.codes[hash_code(code)]["expires_at"] = datetime.now(UTC) - timedelta(seconds=1)
    expired = _exchange(client, code)
    unknown = _exchange(client, "never-issued")
    assert expired.status_code == unknown.status_code == 400
    assert expired.json() == unknown.json()
    assert store.keys == []


@pytest.mark.parametrize(
    "overrides",
    [
        {"code_challenge_method": "plain"},
        {"code_verifier": "too-short"},
        {"code_verifier": "v" * 129},
        {"code_verifier": "v" * 42 + "!"},
    ],
)
def test_a_malformed_exchange_is_refused_without_spending_the_code(
    client, store, overrides
) -> None:
    code = _issued_code(client)
    refused = _exchange(client, code, **overrides)
    assert refused.status_code == 400
    assert refused.json()["error"] == "invalid_request"
    # It said nothing about the code, so the code is still good.
    assert _exchange(client, code).status_code == 200


def test_the_challenge_method_may_be_left_out(client) -> None:
    code = _issued_code(client)
    response = client.post("/v1/auth/keys", json={"code": code, "code_verifier": VERIFIER})
    assert response.status_code == 200


@pytest.mark.parametrize(
    "body",
    [{}, {"code": "c"}, {"code_verifier": VERIFIER}, {"code": "", "code_verifier": VERIFIER}],
)
def test_a_missing_field_is_an_oauth_invalid_request(client, body) -> None:
    response = client.post("/v1/auth/keys", json=body)
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_request"
    assert response.json()["error_description"]


def test_a_callback_taken_off_the_allowlist_stops_its_codes(client, store, monkeypatch) -> None:
    code = _issued_code(client)
    monkeypatch.setenv(ENV_CALLBACKS, LOCAL_CALLBACK)
    response = _exchange(client, code)
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_grant"
    assert store.keys == []


def test_switching_the_feature_off_refuses_without_burning_codes(
    client, store, monkeypatch
) -> None:
    code = _issued_code(client)
    monkeypatch.setenv(ENV_CALLBACKS, "")
    off = _exchange(client, code)
    assert off.status_code == 404
    assert off.json()["error"] == "not_configured"
    monkeypatch.setenv(ENV_CALLBACKS, CALLBACK)
    assert _exchange(client, code).status_code == 200


@pytest.mark.parametrize(
    "change",
    [
        {"status": "suspended"},
        {"status": "pending_approval"},
        {"status": "deleted"},
        {"email_verified": False},
    ],
)
def test_an_account_that_may_no_longer_create_keys_is_refused(client, store, change) -> None:
    code = _issued_code(client)
    store.users["user_1"].update(change)
    response = _exchange(client, code)
    assert response.status_code == 403
    assert response.json()["error"] == "access_denied"
    assert store.keys == []


def test_an_account_gone_since_the_approval_is_refused(client, store) -> None:
    code = _issued_code(client)
    del store.users["user_1"]
    assert _exchange(client, code).status_code == 403


def test_the_key_ceiling_applies_at_the_exchange(client, store, monkeypatch) -> None:
    monkeypatch.setattr(user_routes, "MAX_ACTIVE_API_KEYS_PER_ACCOUNT", 2)
    code = _issued_code(client)
    store.active_keys["user_1"] = 2
    response = _exchange(client, code)
    assert response.status_code == 409
    assert response.json()["error"] == "key_limit_reached"
    assert store.keys == []


def test_a_long_host_is_cut_to_the_key_name_limit(client, store, monkeypatch) -> None:
    host = ".".join(["a" * 60] * 3) + ".example.org"
    callback = f"https://{host}/cb"
    monkeypatch.setenv(ENV_CALLBACKS, callback)
    code = _code(client, callback_url=callback).json()["code"]
    assert _exchange(client, code).status_code == 200
    (created,) = store.keys
    assert created["name"] == host[:64]
    assert json.loads(created["metadata"])["callback_host"] == host


# --- rate limit ---------------------------------------------------------------


def _from(monkeypatch, address: str, *, resolved: bool = True) -> None:
    info = ClientIpInfo(
        client_ip=address if resolved else "unknown",
        peer_ip=address,
        source="peer",
        trusted_proxy_headers=False,
        resolved=resolved,
    )
    monkeypatch.setattr(router_module, "get_client_ip_info", lambda request: info)


def test_failed_exchanges_are_limited_per_address(client, store, monkeypatch) -> None:
    monkeypatch.setattr(key_exchange_rate_limit, "FAILURES_PER_HOUR", 3)
    _from(monkeypatch, "203.0.113.7")
    code = _issued_code(client)
    for _ in range(3):
        assert _exchange(client, "guess").status_code == 400

    limited = _exchange(client, code)
    assert limited.status_code == 429
    assert limited.json()["error"] == "rate_limited"
    assert limited.headers["retry-after"] == "3600"

    # Refused before the code was looked up: it still works from elsewhere.
    _from(monkeypatch, "198.51.100.9")
    assert _exchange(client, code).status_code == 200


def test_successful_exchanges_are_not_counted(client, monkeypatch) -> None:
    monkeypatch.setattr(key_exchange_rate_limit, "FAILURES_PER_HOUR", 2)
    _from(monkeypatch, "203.0.113.7")
    for _ in range(4):
        assert _exchange(client, _issued_code(client)).status_code == 200


def test_unresolved_clients_are_not_counted_together(client, monkeypatch) -> None:
    monkeypatch.setattr(key_exchange_rate_limit, "FAILURES_PER_HOUR", 1)
    _from(monkeypatch, "10.0.0.1", resolved=False)
    for _ in range(3):
        assert _exchange(client, "guess").status_code == 400
    assert _exchange(client, _issued_code(client)).status_code == 200
