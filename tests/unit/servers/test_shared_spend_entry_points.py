"""The grant and playground doors in the shared accounting mode.

Both reach the same shared ledger as the API keys' door, through the
production store wrapper, and charge a request what its billing point recorded:
one it could not price is given back whole, as in the per-process mode.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from serving import grants
from serving.config.settings import get_settings
from serving.grant_auth import authenticate_grant_model_call
from serving.shared_spend import mode as spend_mode
from serving.shared_spend.ledger import SharedSpendLedger
from serving.spend_ledger import SPEND_CHECK_KEY, ledger_for
from serving.storage.cache import CachedOperationalStore, InMemoryCache
from serving.utils import context as req_ctx
from tests.fixtures.shared_spend import FakeSharedBackend
from tests.unit.servers.test_playground_access import INTERNAL, _chat, _client, _Deployment, _Store

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _shared(monkeypatch):
    monkeypatch.setenv("API_KEY_SECRET", "test-api-key-secret")
    get_settings.cache_clear()
    monkeypatch.setattr(spend_mode, "_mode", "shared")
    yield
    get_settings.cache_clear()


class _GrantStore:
    """The grant path's reads, on a store that keeps the shared accounting."""

    def __init__(self) -> None:
        self.backend = FakeSharedBackend()
        self.grant = {
            "grant_id": "agr_1",
            "user_id": "user_1",
            "external_job_id": "job_1",
            "external_attempt_id": "1",
            "allowed_models": ["m"],
            "expires_at": datetime.now(UTC) + timedelta(seconds=300),
            "revoked_at": None,
        }

    def shared_spend_root(self):
        return self

    def shared_spend_backend(self):
        return self.backend

    async def get_agent_grant(self, grant_id: str):
        return self.grant if grant_id == "agr_1" else None

    async def get_user_by_id(self, user_id: str):
        return {"id": user_id, "role": "pro", "status": "active"}

    async def get_quota_context_for_user(self, user_id: str):
        return [{"id": 1, "user_id": user_id, "quota_daily_cost_usd": 5.0}]

    async def get_user_cost_today(self, user_id: str) -> float:
        return 0.0

    async def get_user_cost_period(self, user_id: str, period: str) -> float:
        return 0.0


async def test_a_grant_admits_through_the_wrapper_into_the_same_ledger_and_gives_it_back():
    inner = _GrantStore()
    store = CachedOperationalStore(inner, InMemoryCache())
    context = await authenticate_grant_model_call(grants.mint_grant_token("agr_1"), op_store=store)
    check = context[SPEND_CHECK_KEY]
    assert isinstance(check.ledger, SharedSpendLedger)
    assert check.ledger is ledger_for(inner) is ledger_for(store)

    reservation, refusal = await check.reserve(0.30)
    assert refusal is None and reservation.reservation_id in inner.backend.rows
    with req_ctx.push(**{req_ctx.SPEND_RESERVATION: reservation}):
        from serving.adapters.dispatch_watch import report_sent
        from serving.batch.attempts import observe_billing

        report_sent()
        observe_billing(cost_usd=None)
    reservation.release()
    assert await check.ledger.flush(5)
    assert [m.kind for b in inner.backend.batches for m in b.members] == ["settle"]
    assert inner.backend.rows == {} and check.ledger.fully_settled()


class _SharedStore(_Store):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.backend = FakeSharedBackend()

    def shared_spend_root(self):
        return self

    def shared_spend_backend(self):
        return self.backend


async def test_a_priced_playground_turn_is_settled_with_its_cost():
    deployment = _Deployment()
    deployment.lan.config.pricing = {"prompt": "1", "completion": "1"}
    deployment.lan.usage = {"prompt_tokens": 1000, "completion_tokens": 1000}
    store = _SharedStore()
    async with _client(deployment, INTERNAL, store=store) as client:
        response = await _chat(client, "open-model")
    assert response.status_code == 200
    ledger = ledger_for(store)
    assert await ledger.flush(5)
    (batch,) = store.backend.batches
    (member,) = batch.members
    assert member.kind == "settle"
    assert sum(line.cost_amount for line in member.costs) == Decimal("0.002")
    assert batch.increments == ()  # attached to the turn's reservation, not loose
    assert ledger.fully_settled()


async def test_an_unpriced_playground_turn_is_given_back_and_charged_nothing():
    deployment = _Deployment()
    deployment.lan.config.pricing = None
    deployment.lan.usage = {"prompt_tokens": 1000, "completion_tokens": 1000}
    store = _SharedStore()
    async with _client(deployment, INTERNAL, store=store) as client:
        response = await _chat(client, "open-model")
    assert response.status_code == 200
    ledger = ledger_for(store)
    assert await ledger.flush(5)
    assert [m.kind for b in store.backend.batches for m in b.members] == ["settle"]
    assert store.backend.rows == {} and ledger.fully_settled()
    assert store.backend.counters == {}  # no estimate charged


class _Upstream502(Exception):
    """What an adapter raises when the upstream answers 502."""

    status_code = 502


async def test_playground_turns_the_upstream_fails_do_not_use_up_the_quota():
    # Through the real playground route and its real billing: room for two
    # $0.01 estimates, and two turns in a row that the upstream fails after the
    # call left the gateway (two, so the route's circuit breaker stays closed).
    # Neither is refused for the quota, nothing is charged or left held, and
    # the third turn is served: held as liabilities, the two failures would
    # have left it no room.
    from serving.adapters.dispatch_watch import report_sent

    deployment = _Deployment()
    working = deployment.lan.stream_chat_completion

    async def failing(messages, **params):
        report_sent()
        raise _Upstream502("the upstream answered 502")
        yield ""  # pragma: no cover - makes this an async generator

    deployment.lan.stream_chat_completion = failing
    deployment.staging.stream_chat_completion = failing
    store = _SharedStore(daily_quota=0.025)
    ledger = ledger_for(store)
    async with _client(deployment, INTERNAL, store=store) as client:
        for _ in range(2):
            failed = await _chat(client, "open-model")
            assert failed.status_code != 429
            assert "ok" not in failed.text
            assert await ledger.flush(5)
            assert store.backend.rows == {}
        deployment.lan.stream_chat_completion = working
        served = await _chat(client, "open-model")
    assert served.status_code == 200 and "ok" in served.text
    assert await ledger.flush(5)
    assert store.backend.rows == {} and store.backend.counters == {}
    assert ledger.fully_settled()
