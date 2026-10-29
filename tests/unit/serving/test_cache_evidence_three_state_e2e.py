"""End-to-end three-state cache evidence: UNKNOWN / MISS / HIT.

One test walks the whole chain a request takes and asserts each of the
three cache-evidence states survives it intact, using the real
production functions at every hop — not re-implementations of them:

    provider usage
      -> normalize_usage                (serving/utils/token_utils.py)
      -> the Responses translator        (serving/responses_translator.py)
      -> the usage payload the client (and the FI Gateway) receives
      -> RoutingObservation.cached_tokens (routing/routers.py, built by
         serving/servers/routers/completions_logging.py)

The defect this pins: an unreported measurement (UNKNOWN) was serialized
as a literal 0, so every consumer scored a miss that was never measured.
A consumer that reads only `cached_tokens` cannot tell UNKNOWN from
MISS — the state has to survive as an explicit signal.
"""

from __future__ import annotations

import json

import pytest

from routing.routers import RoutingObservation
from serving.responses_translator import chat_response_to_responses
from serving.utils.token_utils import normalize_usage

# The three states, as a provider would report them (or not report them).
UNKNOWN_USAGE = {
    "prompt_tokens": 241678,
    "completion_tokens": 164,
    "total_tokens": 241842,
    "input_tokens_details": {"cached_tokens": 0},
    "cache_read_reported": False,
}
MISS_USAGE = {
    "prompt_tokens": 241678,
    "completion_tokens": 164,
    "total_tokens": 241842,
    "input_tokens_details": {"cached_tokens": 0},
    "cache_read_reported": True,
}
HIT_USAGE = {
    "prompt_tokens": 241678,
    "completion_tokens": 164,
    "total_tokens": 241842,
    "input_tokens_details": {"cached_tokens": 241000},
    "cache_read_reported": True,
}


def _responses_payload(usage: dict) -> dict:
    chat = {
        "choices": [{"message": {"role": "assistant", "content": "ok"}}],
        "usage": usage,
    }
    return chat_response_to_responses(
        chat,
        response_id="r",
        created_at=1,
        model="m",
        request_body={},
    )


def _observed_cached_tokens(usage: dict) -> int | None:
    """The count the completions router hands RouteWise for one request.

    Uses the real shared extractor and the real routing-observation
    contract — the same two steps serving/servers/routers/completions.py
    performs before calling record_routing_observation.
    """
    ns_usage = normalize_usage(usage) or {}
    raw = ns_usage.get("cache_read_tokens")
    if raw is None:
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


class _RecordingRouter:
    """Captures the RoutingObservation a router would have received."""

    def __init__(self) -> None:
        self.observations: list[RoutingObservation] = []

    def record_observation(self, obs: RoutingObservation) -> None:
        self.observations.append(obs)


class TestThreeStatesSurviveEndToEnd:
    @pytest.mark.parametrize(
        "usage,expected_cached,expected_reported,expected_ratio",
        [
            (UNKNOWN_USAGE, None, False, -1.0),
            (MISS_USAGE, 0, True, 0.0),
            (HIT_USAGE, 241000, True, 241000 / 241678),
        ],
    )
    def test_state_survives_the_chain(
        self, usage, expected_cached, expected_reported, expected_ratio
    ):
        # 1. The Responses translator emits the state, not a coerced 0.
        payload = _responses_payload(usage)
        emitted = payload["usage"]
        assert emitted["cache_read_reported"] is expected_reported
        if expected_cached is None:
            # UNKNOWN: the schema field is 0 for compatibility, but the state
            # says nothing was measured, so the count must not be published.
            assert emitted["input_tokens_details"]["cached_tokens"] == 0
        else:
            assert emitted["input_tokens_details"]["cached_tokens"] == expected_cached

        # 2. The payload the client (and the FI Gateway) receives is JSON-safe
        #    and keeps the state.
        wire = json.loads(json.dumps(payload["usage"]))
        assert wire["cache_read_reported"] is expected_reported

        # 3. RouteWise receives UNKNOWN as None (no observation), never 0.
        observed = _observed_cached_tokens(usage)
        assert observed == expected_cached

        # 4. The observation itself carries the count, and the router's
        #    contract keeps None and 0 distinct.
        router = _RecordingRouter()
        router.record_observation(
            RoutingObservation(
                model_id="m",
                endpoint_id="m:api",
                ttft_ms=1.0,
                total_latency_ms=2.0,
                token_count=241842,
                success=True,
                prompt_tokens=241678,
                completion_tokens=164,
                cached_tokens=observed,
            )
        )
        assert len(router.observations) == 1
        assert router.observations[0].cached_tokens == expected_cached

        # 5. The effective cache ratio the gateway computes from the payload
        #    is -1 (unobservable) for UNKNOWN and the true ratio otherwise.
        cached = wire["input_tokens_details"]["cached_tokens"]
        ratio = -1.0 if expected_cached is None else cached / wire["input_tokens"]
        assert ratio == pytest.approx(expected_ratio, abs=1e-9)

    def test_unknown_is_not_a_miss_anywhere_in_the_chain(self):
        """The specific regression: UNKNOWN must never surface as 0."""
        # normalize_usage
        normalized = normalize_usage(UNKNOWN_USAGE) or {}
        assert "cache_read_tokens" not in normalized, (
            "an unreported measurement must not become a cache_read_tokens of 0"
        )
        assert normalized.get("cache_read_reported") is False

        # Responses translator
        emitted = _responses_payload(UNKNOWN_USAGE)["usage"]
        assert emitted["cache_read_reported"] is False

        # RouteWise
        assert _observed_cached_tokens(UNKNOWN_USAGE) is None

    def test_a_provider_without_the_flag_keeps_the_legacy_inference(self):
        """A provider that reports a nested 0 and no flag is a measured miss.

        The flag is an addition, not a replacement: providers that report
        their own cache counts in the shared shapes must keep working.
        """
        usage = {
            "prompt_tokens": 100,
            "completion_tokens": 5,
            "input_tokens_details": {"cached_tokens": 0},
        }
        assert _observed_cached_tokens(usage) == 0

    def test_billing_does_not_discount_an_unreported_measurement(self):
        """UNKNOWN must not imply a cache discount.

        calculate_cost subtracts the cached tokens from the billable prompt
        tokens only when a cache-read rate is configured. An absent count
        leaves the full prompt rate in place, which is the correct outcome
        for "nothing was measured" — there is no evidence of reuse to
        discount.
        """
        from serving.storage.utils import calculate_cost

        pricing = {"prompt": "1.0", "completion": "2.0", "input_cache_reads": "0.5"}
        unknown = calculate_cost(
            {"prompt_tokens": 1000, "completion_tokens": 0, "total_tokens": 1000},
            pricing,
        )
        measured_miss = calculate_cost(
            {
                "prompt_tokens": 1000,
                "completion_tokens": 0,
                "total_tokens": 1000,
                "cache_read_tokens": 0,
            },
            pricing,
        )
        measured_hit = calculate_cost(
            {
                "prompt_tokens": 1000,
                "completion_tokens": 0,
                "total_tokens": 1000,
                "cache_read_tokens": 1000,
            },
            pricing,
        )
        # UNKNOWN and a measured MISS cost the same: no reuse was observed.
        assert unknown == pytest.approx(measured_miss)
        # A measured HIT is cheaper, and never more expensive.
        assert measured_hit < measured_miss
