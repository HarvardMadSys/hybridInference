"""A hedge is not sent where the backup can only repeat the primary's work.

- A long prompt is slow on every endpoint, because prefill grows with it. The
  hedge timing read the window's TTFTs, which describe ordinary prompts, so a
  long prompt that was merely prefilling looked stuck at every checkpoint and a
  backup prefilled the whole prompt again -- both legs billed upstream, the
  user billed once.
- A primary that refused the request as malformed (400, 404, 413, 422) started
  a backup at once, which refused it again.
- RouteWise's re-solves had no bound on such refusals; FixedRouter stops after
  two (``request_refusals_end_fallback``).
- The stream race deadline (``HedgeStreamRaceTimeout``) was charged to both
  legs' breakers; see ``test_routewise_hedging`` for that one.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import pytest

from routing.routewise.config import RouteWiseConfig
from routing.routewise.latency import PREFILL_FIT_MIN_SAMPLES, ProviderProfile
from routing.routewise.router import RouteWiseRouter
from tests.unit.routing.test_routewise_router import _FakeRouteTable, _make_adapter


class _Status(RuntimeError):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"upstream answered {status_code}")
        self.status_code = status_code


def _content(text: str) -> str:
    return "data: " + json.dumps({"choices": [{"delta": {"content": text}}]}) + "\n\n"


# ---------------------------------------------------------------------------
# The prefill model
# ---------------------------------------------------------------------------


def _linear_profile(slope_sec_per_ktok: float = 0.01, base_ms: float = 20.0) -> ProviderProfile:
    profile = ProviderProfile(endpoint_id="ep", window_sec=10_000.0)
    for i in range(100):
        prompt = 1000 + (i % 10) * 1000  # 1k .. 10k tokens, mean 5.5k
        profile.record(100.0, base_ms + slope_sec_per_ktok * 1000.0 * prompt / 1000.0, None, prompt)
    return profile


@pytest.mark.unit
def test_the_prefill_model_recovers_the_slope():
    profile = _linear_profile(slope_sec_per_ktok=0.01)

    # 100k tokens is 94.5k above the window's mean, at 10 ms per thousand.
    assert profile.expected_extra_ttft_sec(100_000, 100.0) == pytest.approx(0.945)
    # Not longer than usual: nothing to expect.
    assert profile.expected_extra_ttft_sec(5_000, 100.0) == 0.0
    assert profile.expected_extra_ttft_sec(0, 100.0) == 0.0


@pytest.mark.unit
def test_no_prefill_model_without_evidence_for_one():
    unknown_size = ProviderProfile(endpoint_id="ep", window_sec=10_000.0)
    same_size = ProviderProfile(endpoint_id="ep", window_sec=10_000.0)
    too_few = ProviderProfile(endpoint_id="ep", window_sec=10_000.0)
    falling = ProviderProfile(endpoint_id="ep", window_sec=10_000.0)
    for i in range(100):
        unknown_size.record(100.0, 50.0 + i)
        same_size.record(100.0, 50.0 + i, None, 2000)
        falling.record(100.0, 500.0 - (i % 10) * 10.0, None, 1000 + (i % 10) * 1000)
    for i in range(PREFILL_FIT_MIN_SAMPLES - 1):
        too_few.record(100.0, 20.0 + i * 10.0, None, 1000 + i * 1000)

    for profile in (unknown_size, same_size, too_few, falling):
        assert profile.expected_extra_ttft_sec(1_000_000, 100.0) == 0.0


@pytest.mark.unit
def test_the_prefill_model_follows_the_window():
    profile = _linear_profile()
    assert profile.expected_extra_ttft_sec(100_000, 100.0) > 0.0

    assert profile.expected_extra_ttft_sec(100_000, 100.0 + 20_000.0) == 0.0
    assert profile._fit_n == 0
    assert profile._fit_sum_kk == 0.0


@pytest.mark.unit
def test_live_and_replayed_samples_carry_their_prompt_size():
    a = _make_adapter(endpoint_id="m:a")
    table = _FakeRouteTable()
    table.add("m", [(a, 1.0)])
    router = RouteWiseRouter(route_table=table, config=RouteWiseConfig())
    now = time.time()

    router.bootstrap_from_log_rows(
        [
            {
                "timestamp": now,
                "model_id": "m",
                "endpoint_id": "m:a",
                "status_code": 200,
                "ttft_ms": 300,
                "prompt_tokens": 4000,
            }
        ],
        include_envelope=False,
    )

    assert router._latency_profiles["m:a"]._events[-1][3] == 4000


# ---------------------------------------------------------------------------
# Long prompts
# ---------------------------------------------------------------------------


def _hedging_router(seen: dict[str, list[int]]) -> tuple[RouteWiseRouter, Any, Any]:
    a = _make_adapter(endpoint_id="m:a", prompt_price="0.15", completion_price="1.15")
    b = _make_adapter(endpoint_id="m:b", prompt_price="0.3", completion_price="1.2")

    def leg(name: str, ttft_sec: float):
        async def stream(messages, **params):
            seen[name].append(sum(len(m["content"]) for m in messages))
            await asyncio.sleep(ttft_sec)
            yield _content(name)
            yield "data: [DONE]\n\n"

        return stream

    a.stream_chat_completion = leg("m:a", 0.6)
    b.stream_chat_completion = leg("m:b", 0.01)
    table = _FakeRouteTable()
    table.add("m", [(a, 1.0), (b, 1.0)])
    router = RouteWiseRouter(
        route_table=table,
        config=RouteWiseConfig(
            random_seed=4,
            budget_alpha=0.0,
            latency_hedge_mode="probability_target",
            latency_slo_sec=0.4,
        ),
    )
    now = time.time()
    for endpoint in ("m:a", "m:b"):
        for i in range(200):
            # Ordinary prompts of 1k to 10k tokens: 20 ms plus 2 ms per
            # thousand tokens of prefill.
            prompt = 1000 + (i % 10) * 1000
            router._latency_profiles[endpoint].record(now, 20.0 + 2.0 * prompt / 1000, None, prompt)
    return router, a, b


@pytest.mark.unit
async def test_a_long_prompt_is_not_prefilled_twice():
    seen: dict[str, list[int]] = {"m:a": [], "m:b": []}
    router, _a, _b = _hedging_router(seen)
    # About 400k tokens: some 0.8 s of expected extra prefill on either leg,
    # well past the 0.4 s SLO, so a backup could not beat the primary.
    long_prompt = [{"role": "user", "content": "word " * 400_000}]

    chunks = [chunk async for chunk in router.stream_chat_completion("m", long_prompt)]

    assert '"content": "m:a"' in "".join(chunks)
    assert len(seen["m:a"]) == 1
    assert seen["m:b"] == []


@pytest.mark.unit
async def test_an_ordinary_prompt_still_hedges():
    seen: dict[str, list[int]] = {"m:a": [], "m:b": []}
    router, _a, _b = _hedging_router(seen)
    prompt = [{"role": "user", "content": "word " * 5000}]

    chunks = [chunk async for chunk in router.stream_chat_completion("m", prompt)]

    assert '"content": "m:b"' in "".join(chunks)
    assert len(seen["m:a"]) == len(seen["m:b"]) == 1


# ---------------------------------------------------------------------------
# Refusals that describe the request
# ---------------------------------------------------------------------------


def _priced_router(endpoints: list[tuple[str, str]], **config: Any) -> tuple[Any, list[Any]]:
    adapters = [
        _make_adapter(endpoint_id=endpoint, prompt_price=price, completion_price=price)
        for endpoint, price in endpoints
    ]
    table = _FakeRouteTable()
    table.add("m", [(adapter, 1.0) for adapter in adapters])
    router = RouteWiseRouter(
        route_table=table,
        config=RouteWiseConfig(random_seed=2, budget_alpha=0.0, **config),
    )
    now = time.time()
    for endpoint, _price in endpoints:
        for _ in range(100):
            router._latency_profiles[endpoint].record(now, 10.0)
    return router, adapters


def _failing(calls: dict[str, int], name: str, exc: BaseException):
    async def chat(messages, **params):
        calls[name] = calls.get(name, 0) + 1
        raise exc

    return chat


@pytest.mark.unit
@pytest.mark.parametrize("status", [400, 404, 413, 422])
async def test_a_request_refusal_is_not_sent_to_a_backup(status):
    router, (a, b) = _priced_router(
        [("m:a", "0.3"), ("m:b", "0.4")],
        latency_hedge_mode="probability_target",
        latency_slo_sec=3.0,
    )
    calls: dict[str, int] = {}
    a.chat_completion = _failing(calls, "m:a", _Status(status))
    b.chat_completion = _failing(calls, "m:b", _Status(status))

    with pytest.raises(_Status):
        await router.chat_completion("m", [{"role": "user", "content": "x"}])

    assert calls == {"m:a": 1}


@pytest.mark.unit
async def test_an_endpoint_fault_still_starts_the_backup_at_once():
    router, (a, b) = _priced_router(
        [("m:a", "0.3"), ("m:b", "0.4")],
        latency_hedge_mode="probability_target",
        latency_slo_sec=3.0,
    )
    calls: dict[str, int] = {}
    a.chat_completion = _failing(calls, "m:a", _Status(503))

    async def served(messages, **params):
        calls["m:b"] = calls.get("m:b", 0) + 1
        return {"choices": [{"message": {"content": "hi"}}], "usage": {}}

    b.chat_completion = served
    started = time.monotonic()
    response = await router.chat_completion("m", [{"role": "user", "content": "x"}])

    assert response["_routing"]["endpoint_id"] == "m:b"
    assert calls == {"m:a": 1, "m:b": 1}
    # Immediately, not at the first checkpoint 0.75 s in.
    assert time.monotonic() - started < 0.5


@pytest.mark.unit
async def test_re_solves_stop_after_two_endpoints_refuse_the_request():
    """FixedRouter's bound, applied across RouteWise's attempts and hedge legs."""
    router, adapters = _priced_router(
        [("m:a", "0.1"), ("m:b", "0.2"), ("m:c", "0.3"), ("m:d", "0.4"), ("m:e", "0.5")],
        latency_hedge_mode="probability_target",
        latency_slo_sec=3.0,
    )
    a, b, c, d, e = adapters
    calls: dict[str, int] = {}
    # Each attempt's primary fails as an endpoint fault and its backup refuses
    # the request as malformed: two attempts, two refusals.
    a.chat_completion = _failing(calls, "m:a", _Status(503))
    b.chat_completion = _failing(calls, "m:b", _Status(400))
    c.chat_completion = _failing(calls, "m:c", _Status(503))
    d.chat_completion = _failing(calls, "m:d", _Status(400))
    e.chat_completion = _failing(calls, "m:e", _Status(400))

    with pytest.raises(_Status):
        await router.chat_completion("m", [{"role": "user", "content": "x"}])

    assert calls == {"m:a": 1, "m:b": 1, "m:c": 1, "m:d": 1}
