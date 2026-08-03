"""Grant-authenticated inference, and the metering that was missing.

Before grants, an agent token returned from ``verify_api_key`` *above* the
quota gate: authenticated, then never metered. The per-task budget hid that,
and removing the budget without closing the bypass would have turned a bounded
spend into an unbounded one. These tests are what say it is closed.

The load-bearing one is ``test_a_spent_custom_quota_refuses_a_grant_model_call``
— written against a **configured** quota, not the 1000.0 default, because a
test that passes on the default would pass with no gate at all.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from serving import quota
from serving.agent_jobs import model_auth
from serving.agent_jobs.model_auth import (
    AgentModelAuthError,
    AgentQuotaExceeded,
    authenticate_grant_model_call,
    authenticate_grant_tool_call,
    looks_like_agent_token,
)
from serving.config.settings import get_settings
from serving.grants import mint_grant_token

CUSTOM_QUOTA = 5.0


@pytest.fixture(autouse=True)
def _signing_secret(monkeypatch):
    monkeypatch.setenv("API_KEY_SECRET", "test-api-key-secret")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class FakeStore:
    """Just the four reads the grant path makes."""

    def __init__(self) -> None:
        self.users = {"user_1": {"id": "user_1", "role": "pro", "status": "active"}}
        self.keys: list[dict[str, Any]] = [
            {"id": 1, "user_id": "user_1", "quota_daily_cost_usd": CUSTOM_QUOTA, "role": "pro"}
        ]
        self.spend = 0.0
        self.grant: dict[str, Any] | None = None
        self.fail: str | None = None

    async def get_agent_grant(self, grant_id: str):
        if self.fail == "grant":
            raise RuntimeError("database down")
        if self.grant and self.grant["grant_id"] == grant_id:
            return self.grant
        return None

    async def get_user_by_id(self, user_id: str):
        if self.fail == "user":
            raise RuntimeError("database down")
        return self.users.get(user_id)

    async def get_quota_context_for_user(self, user_id: str):
        if self.fail == "keys":
            raise RuntimeError("database down")
        return [k for k in self.keys if k["user_id"] == user_id]

    async def get_user_cost_today(self, user_id: str) -> float:
        if self.fail == "spend":
            raise RuntimeError("database down")
        return self.spend


@pytest.fixture
def store() -> FakeStore:
    fake = FakeStore()
    fake.grant = {
        "grant_id": "agr_1",
        "user_id": "user_1",
        "external_job_id": "job_1",
        "external_attempt_id": "1",
        "allowed_models": ["glm-5.1"],
        "allowed_mcp": ["deepwiki"],
        "expires_at": datetime.now(UTC) + timedelta(seconds=300),
        "revoked_at": None,
    }
    return fake


@pytest.fixture
def token() -> str:
    return mint_grant_token("agr_1")


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def test_both_credential_kinds_route_to_the_agent_path(token: str) -> None:
    """A grant must not fall through to the API-key path and read as garbage."""
    assert looks_like_agent_token(token) is True
    assert looks_like_agent_token("ajt.abc.def") is True
    assert looks_like_agent_token("hyi-something") is False
    assert looks_like_agent_token(None) is False


# ---------------------------------------------------------------------------
# The bypass this task exists to close
# ---------------------------------------------------------------------------


async def test_a_spent_custom_quota_refuses_a_grant_model_call(store, token) -> None:
    """The test that fails without the gate, and proves the bypass is closed.

    Written against a configured quota rather than the 1000.0 default: a test
    that spends past the default would need a thousand dollars of fixture and
    would pass even if no gate existed.
    """
    store.spend = CUSTOM_QUOTA
    with pytest.raises(AgentQuotaExceeded) as caught:
        await authenticate_grant_model_call(token, op_store=store)
    assert caught.value.status_code == 429
    assert caught.value.quota_usd == CUSTOM_QUOTA
    assert caught.value.spent_usd == CUSTOM_QUOTA


async def test_removing_the_quota_lookup_makes_that_test_fail(store, token, monkeypatch) -> None:
    """Mutation check: the gate must be reachable, not merely present.

    A quota gate can sit in the diff and never run — that is exactly the state
    this task fixes. Neutralising the lookup must break the 429 above; if this
    call still raises, the assertion there was measuring something else.
    """
    store.spend = CUSTOM_QUOTA

    async def _no_gate(_op_store, _user_id):
        return (float("inf"), 0.0)

    monkeypatch.setattr(model_auth, "_quota_context", _no_gate)
    context = await authenticate_grant_model_call(token, op_store=store)
    assert context["user_id"] == "user_1"


async def test_an_unspent_quota_allows_the_call(store, token) -> None:
    store.spend = 1.0
    context = await authenticate_grant_model_call(token, op_store=store)
    assert context["user_id"] == "user_1"
    assert context["role"] == "pro"
    assert context["agent_job_id"] == "job_1"
    assert context["agent_grant_id"] == "agr_1"


async def test_the_429_body_and_headers_match_the_direct_path(store, token) -> None:
    """A caller must not be able to tell which door it came through."""
    store.spend = CUSTOM_QUOTA
    with pytest.raises(AgentQuotaExceeded) as caught:
        await authenticate_grant_model_call(token, op_store=store)

    body, headers = quota.exceeded_payload(
        quota_usd=caught.value.quota_usd, spent_usd=caught.value.spent_usd
    )
    assert body["error"] == "Daily cost quota exceeded"
    assert body["quota_usd"] == CUSTOM_QUOTA
    assert set(headers) == {
        "Retry-After",
        "X-RateLimit-Limit-Cost",
        "X-RateLimit-Remaining-Cost",
        "X-RateLimit-Reset",
    }


async def test_a_null_quota_still_means_the_documented_default(store, token) -> None:
    """Same reading as the direct path; diverging would split one account's rules."""
    store.keys[0]["quota_daily_cost_usd"] = None
    store.spend = quota.DEFAULT_DAILY_QUOTA_USD
    with pytest.raises(AgentQuotaExceeded) as caught:
        await authenticate_grant_model_call(token, op_store=store)
    assert caught.value.quota_usd == quota.DEFAULT_DAILY_QUOTA_USD


# ---------------------------------------------------------------------------
# Refusals — every one fails closed
# ---------------------------------------------------------------------------


async def test_an_account_with_no_active_key_is_refused_not_defaulted(store, token) -> None:
    """No configured limit must never read as no ceiling."""
    store.keys.clear()
    with pytest.raises(AgentModelAuthError) as caught:
        await authenticate_grant_model_call(token, op_store=store)
    assert caught.value.status_code == 403


async def test_two_active_keys_refuse_rather_than_pick_a_winner(store, token) -> None:
    """The schema forbids this; reaching it means an index is gone.

    Choosing one would settle a spending question by papering over a schema
    failure, so the call is refused and the event logged.
    """
    store.keys.append({"id": 2, "user_id": "user_1", "quota_daily_cost_usd": 999.0, "role": "pro"})
    with pytest.raises(AgentModelAuthError) as caught:
        await authenticate_grant_model_call(token, op_store=store)
    assert caught.value.status_code == 403


@pytest.mark.parametrize("status_value", ["suspended", "deleted", "pending_approval"])
async def test_an_inactive_account_cannot_call_models(store, token, status_value) -> None:
    """Checked per call, not per mint: a grant outlives a mid-job suspension."""
    store.users["user_1"]["status"] = status_value
    with pytest.raises(AgentModelAuthError) as caught:
        await authenticate_grant_model_call(token, op_store=store)
    assert caught.value.status_code == 403


async def test_a_revoked_grant_is_refused(store, token) -> None:
    store.grant["revoked_at"] = datetime.now(UTC)
    with pytest.raises(AgentModelAuthError) as caught:
        await authenticate_grant_model_call(token, op_store=store)
    assert caught.value.status_code == 401


async def test_an_expired_grant_is_refused(store, token) -> None:
    store.grant["expires_at"] = datetime.now(UTC) - timedelta(seconds=1)
    with pytest.raises(AgentModelAuthError):
        await authenticate_grant_model_call(token, op_store=store)


async def test_an_unknown_grant_is_refused_the_same_way(store, token) -> None:
    """Unknown, revoked and expired are one answer — no state leaks out."""
    store.grant = None
    with pytest.raises(AgentModelAuthError) as caught:
        await authenticate_grant_model_call(token, op_store=store)
    assert caught.value.status_code == 401


async def test_a_forged_token_is_refused(store) -> None:
    with pytest.raises(AgentModelAuthError):
        await authenticate_grant_model_call("agr.forged.signature", op_store=store)


async def test_no_store_refuses(token) -> None:
    with pytest.raises(AgentModelAuthError):
        await authenticate_grant_model_call(token, op_store=None)


@pytest.mark.parametrize("failing_read", ["grant", "user", "keys", "spend"])
async def test_every_store_failure_refuses_rather_than_proceeding(
    store, token, failing_read
) -> None:
    """Fail closed throughout.

    A read that errors must refuse. This is the one place where a permissive
    default is unbounded spend, so the absence of an answer can never be read
    as the absence of a ceiling.
    """
    store.fail = failing_read
    with pytest.raises(AgentModelAuthError) as caught:
        await authenticate_grant_model_call(token, op_store=store)
    assert caught.value.status_code in (401, 403, 503)


# ---------------------------------------------------------------------------
# Tool calls
# ---------------------------------------------------------------------------


async def test_a_tool_call_is_not_charged_against_model_quota(store, token) -> None:
    """It invokes no inference provider, so there is nothing to meter."""
    store.spend = CUSTOM_QUOTA * 10
    context = await authenticate_grant_tool_call(token, op_store=store)
    assert context["agent_allowed_mcp"] == ["deepwiki"]
    assert context["agent_job_id"] == "job_1"


async def test_a_tool_call_still_honours_the_fence(store, token) -> None:
    store.grant["revoked_at"] = datetime.now(UTC)
    with pytest.raises(AgentModelAuthError):
        await authenticate_grant_tool_call(token, op_store=store)


async def test_a_tool_call_still_refuses_an_inactive_account(store, token) -> None:
    store.users["user_1"]["status"] = "suspended"
    with pytest.raises(AgentModelAuthError):
        await authenticate_grant_tool_call(token, op_store=store)


# ---------------------------------------------------------------------------
# No budget survives
# ---------------------------------------------------------------------------


async def test_the_grant_context_carries_no_budget(store, token) -> None:
    context = await authenticate_grant_model_call(token, op_store=store)
    assert not [key for key in context if "budget" in key.lower()]
