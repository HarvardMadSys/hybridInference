"""Unit tests for authenticating sandbox model calls with a worker token."""

from __future__ import annotations

from typing import Any

import pytest

from serving.agent_jobs.model_auth import (
    AgentModelAuthError,
    authenticate_agent_model_call,
    looks_like_agent_token,
)
from serving.agent_jobs.tokens import mint_worker_token

pytestmark = pytest.mark.asyncio

_FENCE = {"job_id": "ajob_abc", "attempt_id": 5, "lease_generation": 2}


@pytest.fixture(autouse=True)
def _api_key_secret(monkeypatch):
    """Provide the signing secret the tokens are derived from."""
    monkeypatch.setenv("API_KEY_SECRET", "model-auth-secret")
    from serving.config.settings import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class FakeJobStore:
    """Resolves exactly one live fence, mirroring the real SQL predicate."""

    def __init__(
        self, *, live: bool = True, budget: float | None = None, role: str | None = None
    ) -> None:
        self.live = live
        self.budget = budget
        self.role = role

    async def resolve_model_credential(
        self, *, job_id: str, attempt_id: int, lease_generation: int
    ) -> dict[str, Any] | None:
        matches = (job_id, attempt_id, lease_generation) == (
            _FENCE["job_id"],
            _FENCE["attempt_id"],
            _FENCE["lease_generation"],
        )
        if not (self.live and matches):
            return None
        return {
            "user_id": "owner-1",
            "role": self.role,
            "job_id": job_id,
            "budget_usd": self.budget,
            "model": "glm-5.1",
        }


class FakeLogStore:
    """Reports a fixed spend for the job."""

    def __init__(self, spent: float = 0.0) -> None:
        self.spent = spent

    async def get_agent_job_cost(self, agent_job_id: str) -> float:
        return self.spent


def _token() -> str:
    """Mint a token for the live fence."""
    return mint_worker_token(**_FENCE)


@pytest.mark.asyncio(loop_scope="function")
async def test_prefix_detection_does_not_claim_user_keys():
    """Only ``ajt.`` credentials are routed to the agent path."""
    assert looks_like_agent_token("ajt.abc.def") is True
    assert looks_like_agent_token("hyi-abcdef") is False
    assert looks_like_agent_token("") is False
    assert looks_like_agent_token(None) is False


async def test_live_fence_resolves_to_the_job_owner():
    """A valid token bills the job's owner and carries the job attribution."""
    context = await authenticate_agent_model_call(
        _token(), job_store=FakeJobStore(budget=5.0), log_store=FakeLogStore()
    )
    assert context["user_id"] == "owner-1"
    assert context["agent_job_id"] == "ajob_abc"
    assert context["is_admin"] is False


async def test_model_calls_run_at_the_owner_role():
    """The sandbox can call exactly the models its owner can call directly.

    A narrower role made the composer offer models whose first agent call
    404ed (staging: 15 listed, 2 resolvable). The blast radius of a leaked
    token is bounded by the budget, not the model tier.
    """
    context = await authenticate_agent_model_call(
        _token(), job_store=FakeJobStore(budget=5.0, role="internal"), log_store=FakeLogStore()
    )
    assert context["role"] == "internal"


async def test_missing_owner_role_falls_back_to_free():
    """A store that cannot report the owner's role must not grant more."""
    context = await authenticate_agent_model_call(
        _token(), job_store=FakeJobStore(budget=5.0), log_store=FakeLogStore()
    )
    assert context["role"] == "free"


async def test_revocation_is_automatic_when_the_fence_moves():
    """A superseded/finished attempt stops buying inference, with no key to revoke.

    This is the whole revocation story: the store's fence is the authority, so
    there is nothing to remember to revoke and no cache to invalidate.
    """
    with pytest.raises(AgentModelAuthError) as excinfo:
        await authenticate_agent_model_call(
            _token(), job_store=FakeJobStore(live=False), log_store=FakeLogStore()
        )
    assert excinfo.value.status_code == 401


async def test_token_for_a_different_attempt_is_rejected():
    """A token whose generation no longer matches cannot buy inference."""
    stale = mint_worker_token(job_id="ajob_abc", attempt_id=5, lease_generation=1)
    with pytest.raises(AgentModelAuthError):
        await authenticate_agent_model_call(
            stale, job_store=FakeJobStore(), log_store=FakeLogStore()
        )


async def test_forged_token_is_rejected():
    """A token not signed with the server secret never resolves."""
    with pytest.raises(AgentModelAuthError):
        await authenticate_agent_model_call(
            "ajt.aaaa.bbbb", job_store=FakeJobStore(), log_store=FakeLogStore()
        )


async def test_budget_exhaustion_is_a_429():
    """Spend at or over budget stops the job rather than billing on."""
    with pytest.raises(AgentModelAuthError) as excinfo:
        await authenticate_agent_model_call(
            _token(),
            job_store=FakeJobStore(budget=1.0),
            log_store=FakeLogStore(spent=1.0),
        )
    assert excinfo.value.status_code == 429


async def test_spend_under_budget_is_allowed():
    """Below budget the call proceeds and reports the cap."""
    context = await authenticate_agent_model_call(
        _token(),
        job_store=FakeJobStore(budget=5.0),
        log_store=FakeLogStore(spent=1.25),
    )
    assert context["agent_job_budget_usd"] == 5.0


async def test_unmeasurable_budget_fails_closed():
    """A log store that cannot report spend must not grant unlimited budget."""

    class NoCostLogStore:
        pass

    with pytest.raises(AgentModelAuthError) as excinfo:
        await authenticate_agent_model_call(
            _token(), job_store=FakeJobStore(budget=1.0), log_store=NoCostLogStore()
        )
    assert excinfo.value.status_code == 503


async def test_missing_job_store_is_rejected():
    """Without a database an agent token cannot be honoured."""
    with pytest.raises(AgentModelAuthError):
        await authenticate_agent_model_call(_token(), job_store=None, log_store=None)


async def test_missing_budget_fails_closed():
    """A job with no configured cap must not buy uncapped inference.

    "No budget" previously meant "no limit", which turned an omitted field on
    the create request into an unbounded spending credential inside a sandbox.
    """
    with pytest.raises(AgentModelAuthError) as excinfo:
        await authenticate_agent_model_call(
            _token(),
            job_store=FakeJobStore(budget=None),
            log_store=FakeLogStore(spent=0.0),
        )
    assert excinfo.value.status_code == 403


async def test_request_headroom_is_required_not_just_being_under():
    """A call starting just below the cap is refused, not admitted.

    Admitting it lets one large completion cross a cap it was already at the
    edge of; the check therefore requires room for another request.
    """
    with pytest.raises(AgentModelAuthError) as excinfo:
        await authenticate_agent_model_call(
            _token(),
            job_store=FakeJobStore(budget=5.0),
            log_store=FakeLogStore(spent=4.99),
        )
    assert excinfo.value.status_code == 429
