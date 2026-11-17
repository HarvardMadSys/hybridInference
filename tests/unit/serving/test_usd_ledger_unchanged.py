"""A USD ledger charges, rounds, admits and refuses exactly as it did before native ledgers.

The reference functions below are frozen copies of the arithmetic the gateway
ran before :mod:`serving.accounting` existed (``dev`` at ``91c73f0c2``). Each
test runs today's code under the default USD policy on many synthetic inputs,
including the halfway and sub-micro-dollar cases, and requires the same result
to the bit: the same float, the same rounded decimal, the same refusal. Any
difference would change what a USD deployment charges.
"""

from __future__ import annotations

import math
import random
import time
import typing
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import pytest
from pydantic import BaseModel

from serving import quota
from serving.accounting import AccountingPolicy, bind_accounting_policy, get_accounting_policy
from serving.accounting_api import MONEY_FIELDS, MoneyResponse
from serving.servers.routers.completions_cost import CostTracker
from serving.servers.routers.routing_info import Pricing
from serving.shared_spend.store import quantized_amount
from serving.spend_ledger import SpendCheck, counted_amount, decide_admission
from serving.storage.utils import billable_output_tokens, calculate_cost

pytestmark = pytest.mark.unit

_MICRO = Decimal("0.000001")


@pytest.fixture(autouse=True)
def usd_policy():
    bind_accounting_policy(None)
    assert not get_accounting_policy().native
    yield
    bind_accounting_policy(None)


# -- the frozen references ----------------------------------------------------


def _reference_calculate_cost(usage: dict[str, Any], pricing: dict[str, str]) -> float | None:
    try:
        prompt_raw = usage.get("prompt_tokens")
        prompt_tokens = float(prompt_raw) if prompt_raw is not None else 0.0
        completion_raw = usage.get("completion_tokens")
        completion_tokens = float(completion_raw) if completion_raw is not None else 0.0
        reasoning_raw = usage.get("reasoning_tokens")
        reasoning_tokens = float(reasoning_raw) if reasoning_raw is not None else 0.0
        total_raw = usage.get("total_tokens")
        total_tokens = float(total_raw) if total_raw is not None else None
        cache_read_raw = usage.get("cache_read_tokens")
        cache_write_raw = usage.get("cache_write_tokens")
        cache_read_tokens = float(cache_read_raw) if cache_read_raw is not None else 0.0
        cache_write_tokens = float(cache_write_raw) if cache_write_raw is not None else 0.0
        prompt_price = float(pricing.get("prompt", "0"))
        completion_price = float(pricing.get("completion", "0"))
        cache_read_price = float(pricing.get("input_cache_reads", "0"))
        cache_write_price = float(pricing.get("input_cache_writes", "0"))
        billable_prompt_tokens = prompt_tokens
        if cache_read_price > 0:
            billable_prompt_tokens -= cache_read_tokens
        if cache_write_price > 0:
            billable_prompt_tokens -= cache_write_tokens
        billable_prompt_tokens = max(billable_prompt_tokens, 0.0)
        output_tokens = billable_output_tokens(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            reasoning_tokens=reasoning_tokens,
            total_tokens=total_tokens,
        )
        return (
            (billable_prompt_tokens * prompt_price / 1_000_000)
            + (output_tokens * completion_price / 1_000_000)
            + (cache_read_tokens * cache_read_price / 1_000_000)
            + (cache_write_tokens * cache_write_price / 1_000_000)
        )
    except (ValueError, TypeError):
        return None


def _reference_counted(cost: float) -> Decimal:
    return Decimal(cost).quantize(_MICRO, rounding=ROUND_HALF_UP)


def _reference_estimate(prompt_tokens, output_tokens, prompt_price, completion_price) -> float:
    estimate = (
        max(prompt_tokens, 0) * max(prompt_price, 0.0)
        + max(output_tokens, 0) * max(completion_price, 0.0)
    ) / 1_000_000
    if not math.isfinite(estimate):
        return 5.0
    return min(max(estimate, 0.01), 5.0)


def _reference_over(limit: float | None, spent: float) -> bool:
    return limit is not None and spent + 0.01 > limit


def _reference_refusal(check: SpendCheck, extra) -> tuple | None:
    day, month, key_day, key_month = extra
    levels = (
        ("key", "day", check.key_budget_daily_usd, check.key_spent_today_usd + key_day),
        ("key", "month", check.key_budget_monthly_usd, check.key_spent_month_usd + key_month),
        ("account", "day", check.account_quota_usd, check.account_spent_usd + day),
        (
            "account",
            "month",
            check.account_quota_monthly_usd,
            check.account_spent_month_usd + month,
        ),
    )
    for scope, window, limit, spent in levels:
        if _reference_over(limit, spent):
            return scope, window, float(limit), spent
    return None


def _reference_headroom(check: SpendCheck, extra) -> float:
    day, month, key_day, key_month = extra
    left = [float(check.account_quota_usd) - (check.account_spent_usd + day)]
    if check.account_quota_monthly_usd is not None:
        left.append(
            float(check.account_quota_monthly_usd) - (check.account_spent_month_usd + month)
        )
    if check.key_budget_daily_usd is not None:
        left.append(float(check.key_budget_daily_usd) - (check.key_spent_today_usd + key_day))
    if check.key_budget_monthly_usd is not None:
        left.append(float(check.key_budget_monthly_usd) - (check.key_spent_month_usd + key_month))
    return min(left)


def _reference_admission(check: SpendCheck, amount: float, extra) -> tuple:
    refusal = _reference_refusal(check, extra)
    if refusal is not None:
        return None, refusal
    if amount > 0.01:
        amount = max(0.01, min(amount, _reference_headroom(check, extra)))
    return amount, None


# -- the comparisons ----------------------------------------------------------


def _price(rng: random.Random) -> str:
    return rng.choice(
        [
            "0",
            "0.000001",
            "0.0000005",
            "0.15",
            "0.27",
            "1.1",
            "2.5",
            "15",
            "75",
            f"{rng.uniform(0, 100):.6f}",
            f"{rng.uniform(0, 1):.9f}",
        ]
    )


def _usage(rng: random.Random) -> dict[str, Any]:
    prompt = rng.choice([0, 1, 7, 999, 12_345, 128_000, rng.randrange(0, 2_000_000)])
    completion = rng.choice([0, 1, 3, 512, rng.randrange(0, 200_000)])
    reasoning = rng.choice([0, 0, 64, rng.randrange(0, 50_000)])
    total = rng.choice([None, prompt + completion, prompt + completion + reasoning])
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "reasoning_tokens": reasoning,
        "total_tokens": total,
        "cache_read_tokens": rng.choice([None, 0, min(prompt, rng.randrange(0, 100_000))]),
        "cache_write_tokens": rng.choice([None, 0, min(prompt, rng.randrange(0, 10_000))]),
    }


def test_a_request_costs_the_same_float():
    rng = random.Random(20261117)
    for _ in range(5000):
        usage = _usage(rng)
        pricing = {
            "prompt": _price(rng),
            "completion": _price(rng),
            "input_cache_reads": _price(rng),
            "input_cache_writes": rng.choice(["0", _price(rng)]),
        }
        expected = _reference_calculate_cost(usage, pricing)
        got = calculate_cost(usage, pricing)
        assert type(got) is type(expected) and got == expected, (usage, pricing)
        typed = CostTracker._compute_cost(
            prompt_tokens=usage["prompt_tokens"],
            completion_tokens=usage["completion_tokens"],
            total_tokens=usage["total_tokens"],
            cache_read_tokens=usage["cache_read_tokens"],
            cache_write_tokens=usage["cache_write_tokens"],
            reasoning_tokens=usage["reasoning_tokens"],
            pricing=Pricing(
                prompt_price=float(pricing["prompt"]),
                completion_price=float(pricing["completion"]),
                cache_read_price=float(pricing["input_cache_reads"]),
                cache_write_price=float(pricing["input_cache_writes"]),
            ),
        )
        assert type(typed) is float and typed == expected


@pytest.mark.parametrize(
    "cost",
    [0.0000005, 0.0000015, 0.0000025, 2.675e-6, 1e-7, 4.9999999e-7, 0.1 + 0.2, 1000.0],
)
def test_an_increment_rounds_the_same_at_halfway_and_below_a_micro_dollar(cost):
    assert counted_amount(cost) == _reference_counted(cost)
    # The shared ledger rounds its costs and reservations the same way.
    assert quantized_amount(cost) == _reference_counted(cost)


def test_increments_round_the_same():
    rng = random.Random(7)
    for _ in range(20000):
        cost = rng.choice([rng.uniform(0, 1e-5), rng.uniform(0, 1), rng.uniform(0, 100)])
        assert counted_amount(cost) == _reference_counted(cost)
        assert quantized_amount(cost) == _reference_counted(cost)


def test_a_reservation_estimate_is_the_same():
    rng = random.Random(11)
    specials = [0.0, -1.0, math.inf, -math.inf, math.nan]
    for _ in range(5000):
        prompt = rng.choice([0, 10, rng.randrange(0, 10**7)])
        output = rng.choice([0, 1, rng.randrange(0, 10**6)])
        prices = [rng.choice([*specials, rng.uniform(0, 100)]) for _ in range(2)]
        expected = _reference_estimate(prompt, output, *prices)
        got = quota.estimate_request_cost(
            prompt_tokens=prompt,
            output_tokens=output,
            prompt_price=prices[0],
            completion_price=prices[1],
        )
        assert type(got) is float
        assert got == expected or (math.isnan(got) and math.isnan(expected))


def test_admission_and_refusal_are_the_same():
    rng = random.Random(3)
    for _ in range(5000):

        def maybe(value: float) -> float | None:
            return None if rng.random() < 0.4 else value

        check = SpendCheck(
            ledger=None,
            user_id="u",
            key_id=1,
            read_at=time.monotonic(),
            account_quota_usd=rng.choice([0.0, 0.01, 1.0, 20.0, rng.uniform(0, 50)]),
            account_spent_usd=rng.uniform(0, 50),
            account_quota_monthly_usd=maybe(rng.uniform(0, 500)),
            account_spent_month_usd=rng.uniform(0, 500),
            key_budget_daily_usd=maybe(rng.uniform(0, 30)),
            key_budget_monthly_usd=maybe(rng.uniform(0, 300)),
            key_spent_today_usd=rng.uniform(0, 30),
            key_spent_month_usd=rng.uniform(0, 300),
        )
        extra = tuple(rng.choice([0.0, rng.uniform(0, 1)]) for _ in range(4))
        amount = rng.choice([0.01, 0.02, rng.uniform(0.01, 5.0)])
        expected_amount, expected_refusal = _reference_admission(check, amount, extra)
        got_amount, refusal = decide_admission(check, amount, extra)
        if expected_refusal is None:
            assert refusal is None
            assert type(got_amount) is float and got_amount == expected_amount
        else:
            assert refusal is not None and got_amount is None
            assert (refusal.scope, refusal.window, refusal.limit_usd, refusal.spent_usd) == (
                expected_refusal
            )
            assert refusal.limit_amount is None and refusal.spent_amount is None


def test_a_refusal_answers_with_the_same_body():
    refusal = quota.BudgetRefusal("key", "day", 1.5, 1.495, "hyi-abc", '"ci"')
    body, headers = quota.refusal_payload(refusal)
    assert set(body) == {
        "error",
        "code",
        "scope",
        "window",
        "key_prefix",
        "quota_usd",
        "spent_usd",
        "remaining_usd",
        "reset_at",
        "contact_email",
        "message",
        "retry_after",
    }
    assert "$1.50" in body["message"]
    # Only the headers say, additively, what the amounts are in.
    assert headers["X-Accounting-Currency"] == "USD"
    assert headers["X-RateLimit-Cost-Currency"] == "USD"
    day, _ = quota.refusal_payload(quota.BudgetRefusal("account", "day", 10.0, 10.0))
    assert not {"quota", "spent", "remaining", "currency"} & set(day)


def _money_responses(cls: type = MoneyResponse) -> list[type[MoneyResponse]]:
    found = []
    for sub in cls.__subclasses__():
        found.append(sub)
        found.extend(_money_responses(sub))
    return found


def _constructed(model: type[BaseModel]) -> BaseModel:
    """An instance of *model* with every field None, its nested models constructed alike."""
    values: dict[str, Any] = {}
    for name, field in model.model_fields.items():
        nested = [
            arg
            for arg in (field.annotation, *typing.get_args(field.annotation))
            if isinstance(arg, type) and issubclass(arg, BaseModel)
        ]
        values[name] = _constructed(nested[0]) if nested else None
    return model.model_construct(**values)


def test_every_money_response_keeps_its_usd_shape():
    """On a USD ledger no response grows a ``currency`` or a native alias."""
    import serving.schemas_admin
    import serving.schemas_auth  # noqa: F401  (the responses live here)

    responses = _money_responses()
    assert len(responses) > 10
    for response in responses:
        fields = set(response.model_fields)
        aliases = {
            legacy.removesuffix("_usd")
            for legacy in MONEY_FIELDS & fields
            if legacy.removesuffix("_usd") in fields
        }
        shown = set(_constructed(response).model_dump())
        assert shown == fields - aliases - {"currency"}, response.__name__


def test_the_default_policy_is_usd_at_the_identity_rate():
    policy = AccountingPolicy()
    assert (policy.currency, policy.usd_to_currency_rate, policy.native) == (
        "USD",
        Decimal(1),
        False,
    )
    assert policy.quantum == _MICRO
    assert policy.in_ledger(0.01) == 0.01 and type(policy.in_ledger(0.01)) is float
    assert policy.as_usd(0.1 + 0.2) == 0.1 + 0.2
