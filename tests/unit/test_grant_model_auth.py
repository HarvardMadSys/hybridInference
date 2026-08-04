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
        # Deliberately still here: a deployment upgraded in place keeps the
        # physical `allowed_mcp` column, so a row read from it carries the key.
        # Nothing on the grant path may notice — which is what this row proves
        # every time it is used, since every case below reads it.
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


async def test_the_grant_context_carries_the_owners_own_controls(store, token: str) -> None:
    """**A reader is only as good as the writer that fills its field.**

    The inference path denies a disabled model by reading `disabled_models`
    from the user context, and applies a concurrency cap by reading
    `max_concurrent_requests`. The grant path set neither, so both per-user
    controls silently stopped applying the moment a call arrived from an agent
    — and no test noticed, because the gate's own tests inject a context rather
    than obtaining one from here.

    That is the same shape as the bug this file exists for: a field with a
    writer and no reader. This one is a reader with no writer.
    """
    store.users["user_1"]["preferences"] = {"disabled_models": ["glm-5.1"]}
    store.users["user_1"]["max_concurrent_requests"] = 3

    ctx = await authenticate_grant_model_call(token, op_store=store)

    assert ctx["disabled_models"] == ["glm-5.1"]
    assert ctx["max_concurrent_requests"] == 3


async def test_an_owner_with_no_preferences_gets_an_empty_denylist(store, token: str) -> None:
    """The common case must not raise or produce None."""
    ctx = await authenticate_grant_model_call(token, op_store=store)

    assert ctx["disabled_models"] == []


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
# Tool calls belong to the cloud agent now
# ---------------------------------------------------------------------------
#
# Three cases lived here — a tool call is not metered, it honours the fence, it
# refuses an inactive account — against `authenticate_grant_tool_call`. That
# function is gone: the ownership amendment moved the MCP registry, its
# credentials and its proxy to the cloud agent, which is where the job, its
# requested servers and the attempt fence already are. This gateway never had
# the state to decide a tool call; it only had the token.
#
# The three properties did not disappear with the function, they moved: the
# cloud agent's relay checks its own fence and its own server list, and its
# tests are where those cases live now. Deleting them here without saying so
# would read as three properties dropped.


async def test_the_grant_path_offers_no_tool_authorization(store, token) -> None:
    """**The tripwire.**

    A grant authorizes models. If a tool-call entry point reappears on this
    module, a sandbox credential would once again be able to buy tools from the
    gateway — and the cloud agent's relay, which owns the registry and the
    credentials, would no longer be the only door to them.
    """
    import serving.agent_jobs.model_auth as module

    grant_tool_paths = [
        name for name in dir(module) if "grant" in name.lower() and "tool" in name.lower()
    ]
    assert not grant_tool_paths, grant_tool_paths
    # The legacy per-attempt path stays until H4: the deployed agent still
    # carries those tokens, and removing it now would break every running job
    # for a boundary the cloud agent's relay is not yet serving.
    assert hasattr(module, "authenticate_agent_tool_call")


# ---------------------------------------------------------------------------
# No budget survives
# ---------------------------------------------------------------------------


async def test_the_grant_context_carries_no_budget(store, token) -> None:
    context = await authenticate_grant_model_call(token, op_store=store)
    assert not [key for key in context if "budget" in key.lower()]
