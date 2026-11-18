"""Every billing point charges a native ledger the exact native amount its prices produce.

Chat (streaming and not, which share :class:`CostTracker`, including the
synchronous charge a stream torn down by a cancellation makes), embeddings and
Anthropic ``/v1/messages`` each price a request and record it in the spend
ledger. On a CNY ledger (synthetic fixed rate) the amount recorded is the exact
decimal of the configured CNY prices -- never a float, never a USD round trip
-- and a price far below a fen per token accumulates without loss.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from serving import accounting
from serving.accounting import AccountingPolicy
from serving.servers.routers import anthropic_messages, embeddings
from serving.servers.routers.completions_cost import CostTracker, PricingLookup
from serving.servers.routers.routing_info import RoutingInfo
from serving.spend_ledger import ledger_for

pytestmark = pytest.mark.asyncio

POLICY = AccountingPolicy("CNY", Decimal("6.7180"), "2026-11-05", "synthetic fixed rate")
#: Native CNY prices per million tokens, one of them far below a fen per token.
PRICES = {"prompt": "0.0003", "completion": "0.0123456789", "input_cache_reads": "0"}


@pytest.fixture(autouse=True)
def cny(monkeypatch: pytest.MonkeyPatch) -> AccountingPolicy:
    monkeypatch.setattr(accounting, "_policy", POLICY)
    return POLICY


class _Store:
    atomic_cost_increments = True

    def __init__(self) -> None:
        self.increments: list[Any] = []

    async def increment_user_costs(self, increments: list[Any]) -> None:
        self.increments.extend(increments)


async def _written(store: _Store) -> Decimal:
    assert await ledger_for(store).flush()
    assert all(increment.cost_amount is not None for increment in store.increments)
    return sum((increment.cost_amount for increment in store.increments), Decimal(0))


def _exact(prompt: int, completion: int) -> Decimal:
    return (
        prompt * Decimal(PRICES["prompt"]) + completion * Decimal(PRICES["completion"])
    ) / 1_000_000


async def test_chat_charges_the_exact_native_amount():
    store = _Store()
    tracker = CostTracker(op_store=store, pricing=PricingLookup(router=None))
    routing = RoutingInfo(request_id="r", model="m", extra={"pricing": dict(PRICES)})
    result = await tracker.schedule_increment(
        user_id="u", routing=routing, prompt_tokens=7, completion_tokens=3
    )
    await asyncio.gather(*tracker._background_tasks)
    expected = _exact(7, 3)
    assert await _written(store) == POLICY.quantize(expected)
    assert result.upstream_cost_usd == pytest.approx(float(expected / POLICY.usd_to_currency_rate))


async def test_a_cancelled_stream_charges_synchronously_and_exactly():
    store = _Store()
    tracker = CostTracker(op_store=store, pricing=PricingLookup(router=None))
    routing = RoutingInfo(request_id="r", model="m", extra={"pricing": dict(PRICES)})
    # ``charge`` is what a stream being torn down calls: it must not suspend.
    tracker.charge(user_id="u", routing=routing, prompt_tokens=1000, completion_tokens=17)
    assert await _written(store) == POLICY.quantize(_exact(1000, 17))


async def test_embeddings_charge_the_exact_native_amount():
    store = _Store()
    usage = {"prompt_tokens": 12345, "total_tokens": 12345}
    charged = embeddings._schedule_cost_increment(store, "u", usage, dict(PRICES))
    assert charged == _exact(12345, 0)
    assert isinstance(charged, Decimal)
    assert await _written(store) == POLICY.quantize(_exact(12345, 0))


async def test_anthropic_messages_charge_the_exact_native_amount():
    store = _Store()
    usage = {"prompt_tokens": 4096, "completion_tokens": 256, "total_tokens": 4352}
    charged = anthropic_messages._schedule_messages_cost_increment(store, "u", usage, dict(PRICES))
    assert charged == _exact(4096, 256)
    assert await _written(store) == POLICY.quantize(_exact(4096, 256))


async def test_sub_fen_prices_accumulate_without_loss():
    """Ten thousand requests at 0.0003 CNY per million tokens add up exactly."""
    store = _Store()
    tracker = CostTracker(op_store=store, pricing=PricingLookup(router=None))
    for index in range(10_000):
        tracker.charge(
            user_id="u",
            routing=RoutingInfo(request_id=f"r{index}", model="m", extra={"pricing": dict(PRICES)}),
            prompt_tokens=7,
            completion_tokens=0,
        )
    # 7 tokens at 0.0003 per million is 2.1e-9 CNY a request, held exactly.
    assert await _written(store) == Decimal("0.000021000000")


async def test_a_usd_tariff_is_charged_at_the_fixed_rate_once():
    store = _Store()
    tracker = CostTracker(op_store=store, pricing=PricingLookup(router=None))
    config = SimpleNamespace(pricing={"prompt": "1", "completion": "2"}, pricing_currency="USD")
    from serving.pricing import effective_pricing

    native = effective_pricing(config)
    assert native == {"prompt": "6.7180", "completion": "13.4360"}
    tracker.charge(
        user_id="u",
        routing=RoutingInfo(request_id="r", model="m", extra={"pricing": native}),
        prompt_tokens=1_000_000,
        completion_tokens=1_000_000,
    )
    assert await _written(store) == Decimal("20.154000000000")
