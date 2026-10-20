"""How ``verify_api_key`` treats an agent grant token, and the optional lookup.

``authenticate_grant_model_call`` is tested on its own; what wraps it was not.
That wrapper is the scope: ``verify_api_key`` is shared with owner-facing routes
(``/v1/agent/jobs`` and friends), so a grant must be refused anywhere but an
inference route *before* it is resolved to its owner. It also runs ahead of the
auth-disabled shortcut, so a grant is metered even where API keys are not, and
it turns the grant errors into the HTTP answers the direct path gives.

``optional_verify_api_key`` serves ``/v1/models``: a bad or missing key is an
anonymous caller there, but an unreachable database must not be.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from serving import grants, quota
from serving.grant_auth import AgentModelAuthError, AgentQuotaExceeded
from serving.servers import auth
from serving.utils import context as req_ctx

pytestmark = pytest.mark.asyncio

# Built at runtime: a literal in the token's shape would be flagged as a secret.
GRANT = f"{grants._TOKEN_PREFIX}." + "x" * 24


def _request(path: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": [],
            "scheme": "http",
            "server": ("test", 80),
            "client": ("203.0.113.9", 4000),
        }
    )


@pytest.fixture
def grant_calls(monkeypatch):
    """Record grant resolutions; answer with an owner context by default."""
    calls: list[str] = []
    outcome: dict[str, Any] = {"ctx": {"user_id": "owner-1", "role": "pro", "authenticated": True}}

    async def resolve(token, *, op_store):
        calls.append(token)
        if isinstance(outcome.get("raise"), Exception):
            raise outcome["raise"]
        return dict(outcome["ctx"])

    monkeypatch.setattr(auth, "authenticate_grant_model_call", resolve)
    req_ctx.reset_request_scope()
    return calls, outcome


async def _verify(path: str, *, bearer: str | None = GRANT, x_api_key: str | None = None):
    return await auth.verify_api_key(
        _request(path),
        authorization=f"Bearer {bearer}" if bearer else None,
        x_api_key=x_api_key,
        x_on_behalf_of=None,
        op_store=object(),
        log_store=None,
    )


@pytest.mark.parametrize(
    "path",
    [
        "/v1/agent/jobs",
        "/v1/agent/jobs/ajob_1/cancel",
        "/v1/embeddings",  # its models are not grant-scoped
        "/v1/models",
        "/v1/user/keys",
        "/v1/chat/completions/extra",
    ],
)
async def test_a_grant_is_refused_off_the_inference_routes_before_it_is_resolved(grant_calls, path):
    calls, _ = grant_calls

    with pytest.raises(HTTPException) as caught:
        await _verify(path)

    assert caught.value.status_code == 403
    assert caught.value.detail["error"]["type"] == "insufficient_scope"
    assert calls == []  # never resolved to its owner


@pytest.mark.parametrize(
    "path",
    [
        "/v1/chat/completions",
        "/v1/chat/completions/",
        "/v1/completions",
        "/v1/messages",
        "/v1/messages/count_tokens",
        "/v1/responses",
        "/anthropic/v1/messages",
    ],
)
async def test_a_grant_on_an_inference_route_resolves_to_its_owner(grant_calls, path):
    calls, _ = grant_calls

    ctx = await _verify(path)

    assert ctx["user_id"] == "owner-1"
    assert calls == [GRANT]
    assert req_ctx.get()[req_ctx.USER_ROLE] == "pro"  # tiered key pools read this


async def test_a_grant_sent_as_x_api_key_is_scoped_the_same_way(grant_calls):
    calls, _ = grant_calls

    with pytest.raises(HTTPException) as caught:
        await _verify("/v1/agent/jobs", bearer=None, x_api_key=GRANT)

    assert caught.value.status_code == 403
    assert calls == []
    assert (await _verify("/v1/messages", bearer=None, x_api_key=GRANT))["user_id"] == "owner-1"


async def test_a_grant_is_metered_even_where_api_keys_are_not_required(grant_calls, monkeypatch):
    """Auth disabled makes every caller an anonymous admin -- but not a grant."""
    calls, _ = grant_calls
    monkeypatch.setattr(auth, "is_user_auth_enabled", lambda: False)

    ctx = await _verify("/v1/chat/completions")

    assert calls == [GRANT]
    assert ctx["user_id"] == "owner-1"
    with pytest.raises(HTTPException):
        await _verify("/v1/agent/jobs")
    # An ordinary key on the same deployment still takes the shortcut.
    anonymous = await _verify("/v1/chat/completions", bearer="hyi-ordinary")
    assert anonymous["user_id"] == "anonymous"


async def test_a_spent_quota_gets_the_direct_paths_429(grant_calls, monkeypatch):
    _, outcome = grant_calls
    outcome["raise"] = AgentQuotaExceeded(quota_usd=5.0, spent_usd=5.25)
    monkeypatch.setattr(
        quota, "next_utc_midnight", lambda now=None: datetime(2026, 10, 20, tzinfo=UTC)
    )
    monkeypatch.setattr(quota, "seconds_until_utc_midnight", lambda now=None: 43_200)
    monkeypatch.setattr(auth, "_quota_contact", lambda: "quota@example.test")

    with pytest.raises(HTTPException) as caught:
        await _verify("/v1/messages")

    body, headers = quota.exceeded_payload(
        quota_usd=5.0, spent_usd=5.25, contact_email="quota@example.test"
    )
    assert caught.value.status_code == 429
    assert caught.value.detail == body
    assert caught.value.headers == headers
    assert body["remaining_usd"] == 0 and headers["Retry-After"] == "43200"


@pytest.mark.parametrize("status", [401, 403, 503])
async def test_other_grant_refusals_keep_their_status(grant_calls, status):
    _, outcome = grant_calls
    outcome["raise"] = AgentModelAuthError("grant revoked", status_code=status)

    with pytest.raises(HTTPException) as caught:
        await _verify("/v1/chat/completions")

    assert caught.value.status_code == status
    assert caught.value.detail == {"error": {"type": "agent_job_auth", "message": "grant revoked"}}


async def test_an_ordinary_key_never_takes_the_grant_path(grant_calls, monkeypatch):
    calls, _ = grant_calls
    monkeypatch.setattr(auth, "is_user_auth_enabled", lambda: False)

    await _verify("/v1/agent/jobs", bearer="hyi-ordinary")

    assert calls == []


# --- optional_verify_api_key -------------------------------------------------------------


class LightStore:
    def __init__(self, row=None, fail: bool = False):
        self.row = row
        self.fail = fail
        self.looked_up: list[str] = []

    async def get_auth_context_lightweight(self, key_hash):
        self.looked_up.append(key_hash)
        if self.fail:
            raise RuntimeError("database down")
        return self.row


def _row(**overrides):
    row = {
        "user_id": "u1",
        "role": "pro",
        "email": "u1@example.com",
        "email_verified": True,
        "phone": None,
        "phone_verified": False,
        "preferences": {"disabled_models": ["m-off"]},
        "max_concurrent_requests": 3,
    }
    row.update(overrides)
    return row


@pytest.fixture
def keyed(monkeypatch):
    monkeypatch.setenv("API_KEY_SECRET", "unit-test-secret")
    monkeypatch.setattr(auth, "is_user_auth_enabled", lambda: True)


async def _optional(store, *, bearer=None, x_api_key=None):
    return await auth.optional_verify_api_key(
        _request("/v1/models"),
        authorization=f"Bearer {bearer}" if bearer else None,
        x_api_key=x_api_key,
        op_store=store,
    )


async def test_optional_lookup_resolves_a_key_from_either_header(keyed):
    store = LightStore(_row())

    by_bearer = await _optional(store, bearer="hyi-abc")
    by_header = await _optional(store, x_api_key="hyi-abc")

    assert by_bearer == by_header
    assert by_bearer["user_id"] == "u1"
    assert by_bearer["is_admin"] is False
    assert by_bearer["disabled_models"] == ["m-off"]
    assert by_bearer["auth_key_hash"] == auth.hash_api_key("hyi-abc")
    assert store.looked_up == [auth.hash_api_key("hyi-abc")] * 2


async def test_optional_lookup_treats_a_missing_or_unknown_key_as_anonymous(keyed):
    assert await _optional(LightStore(_row())) is None
    assert await _optional(LightStore(None), bearer="hyi-unknown") is None


async def test_optional_lookup_does_not_recognise_an_unverified_account(keyed, monkeypatch):
    monkeypatch.setattr(auth.get_settings(), "signup_require_email_verification", True)

    def not_initialised():
        raise RuntimeError("not initialised")

    # The runtime override is unreadable here, so the environment setting decides.
    monkeypatch.setattr(
        "serving.config.runtime_settings.get_runtime_settings_instance", not_initialised
    )

    assert await _optional(LightStore(_row(email_verified=False)), bearer="hyi-abc") is None


async def test_optional_lookup_never_turns_a_database_problem_into_anonymous(keyed):
    with pytest.raises(HTTPException) as no_store:
        await _optional(None, bearer="hyi-abc")
    with pytest.raises(HTTPException) as failing:
        await _optional(LightStore(fail=True), bearer="hyi-abc")

    assert no_store.value.status_code == 503
    assert failing.value.status_code == 500


async def test_optional_lookup_without_auth_is_an_anonymous_admin(monkeypatch):
    monkeypatch.setattr(auth, "is_user_auth_enabled", lambda: False)

    ctx = await _optional(LightStore(), bearer="anything")

    assert ctx == {
        "user_id": "anonymous",
        "role": "admin",
        "authenticated": False,
        "is_admin": True,
    }
