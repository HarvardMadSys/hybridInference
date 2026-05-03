"""Unit tests for cost calculation helpers."""

from __future__ import annotations

import pytest

from serving.storage.database import calculate_cost


def test_cost_calculation_basic() -> None:
    usage = {"prompt_tokens": 1000, "completion_tokens": 500}
    pricing = {"prompt": "0.15", "completion": "1.25"}

    cost = calculate_cost(usage, pricing)

    assert cost == pytest.approx(0.000775)


def test_cost_calculation_with_cache_and_reasoning() -> None:
    # OpenAI semantic: prompt_tokens is the *total* input including the cached
    # subset (here 6000 = 1000 uncached + 5000 cache_read). calculate_cost must
    # subtract the cached portion before applying prompt_price so cache is not
    # double-billed.
    usage = {
        "prompt_tokens": 6000,
        "completion_tokens": 500,
        "reasoning_tokens": 200,
        "cache_read_tokens": 5000,
    }
    pricing = {
        "prompt": "0.28",
        "completion": "0.42",
        "input_cache_reads": "0.028",
    }

    cost = calculate_cost(usage, pricing)

    expected = (
        (1000 * 0.28 / 1_000_000)
        + (500 * 0.42 / 1_000_000)
        + (200 * 0.42 / 1_000_000)
        + (5000 * 0.028 / 1_000_000)
    )
    assert cost == pytest.approx(expected)


def test_cost_calculation_subtracts_cache_from_prompt() -> None:
    """Issue #338: with prompt_tokens=10000 and cache_read_tokens=8000, the
    prompt-rate charge must apply to 2000 tokens (uncached delta), not 10000."""
    usage = {
        "prompt_tokens": 10000,
        "completion_tokens": 0,
        "cache_read_tokens": 8000,
        "cache_write_tokens": 0,
    }
    pricing = {
        "prompt": "3.0",
        "completion": "15.0",
        "input_cache_reads": "0.30",
        "input_cache_writes": "3.75",
    }

    cost = calculate_cost(usage, pricing)

    expected = (2000 * 3.0 / 1_000_000) + (8000 * 0.30 / 1_000_000)
    assert cost == pytest.approx(expected)


def test_cost_calculation_clamps_when_cache_exceeds_prompt() -> None:
    """Malformed usage where cache > prompt must not produce a negative
    prompt-rate charge; billable prompt tokens clamp to 0."""
    usage = {
        "prompt_tokens": 100,
        "completion_tokens": 0,
        "cache_read_tokens": 500,
    }
    pricing = {
        "prompt": "3.0",
        "completion": "15.0",
        "input_cache_reads": "0.30",
    }

    cost = calculate_cost(usage, pricing)

    # billable_prompt clamps to 0; only cache is charged
    expected = 500 * 0.30 / 1_000_000
    assert cost == pytest.approx(expected)


def test_cost_calculation_no_cache_price_falls_back_to_prompt_rate() -> None:
    """Issue #338 review: if cache tokens are reported but no cache-specific
    price is configured (default 0), cached tokens must be billed at the
    regular prompt rate rather than silently dropped — otherwise models
    that report cache tokens but lack cache pricing get under-billed."""
    usage = {
        "prompt_tokens": 10000,
        "completion_tokens": 0,
        "cache_read_tokens": 8000,
        "cache_write_tokens": 0,
    }
    pricing = {"prompt": "3.0", "completion": "15.0"}  # no input_cache_reads/writes

    cost = calculate_cost(usage, pricing)

    # billable_prompt stays at 10000 (cache not subtracted because no cache price);
    # cache_read contributes 0 itself. Total = 10000 * $3 / 1M.
    expected = 10000 * 3.0 / 1_000_000
    assert cost == pytest.approx(expected)


def test_cost_calculation_missing_pricing_returns_none() -> None:
    usage = {"prompt_tokens": 1000}

    assert calculate_cost(usage, None) is None


def test_cost_calculation_invalid_types_returns_none() -> None:
    usage = {"prompt_tokens": "invalid"}
    pricing = {"prompt": "0.15"}

    assert calculate_cost(usage, pricing) is None


def test_cost_calculation_zero_pricing() -> None:
    usage = {"prompt_tokens": 1000, "completion_tokens": 500}
    pricing = {"prompt": "0", "completion": "0"}

    assert calculate_cost(usage, pricing) == 0.0


# --- End-to-end producer → calculate_cost regression tests ----------------
#
# These tests pipe a realistic upstream usage payload through the adapter-layer
# normalizers and assert that calculate_cost produces the expected billing.
# They catch contract drift: if a producer reverts to the old "prompt_tokens
# excludes cache" semantic, billable_prompt collapses to (often) 0 and cost
# under-bills silently.


def test_claude_parse_usage_to_calculate_cost_disjoint_billing() -> None:
    """Anthropic upstream → claude_format.parse_usage → calculate_cost.

    With Anthropic's disjoint fields (input=21, cache_read=100, cache_write=50),
    billable_prompt should be 21 (just input_tokens), and cache_read/write
    bill at their own rates. Total cost matches the expected disjoint sum.
    """
    from serving.adapters.claude_format import parse_usage

    info = parse_usage(
        {
            "input_tokens": 21,
            "cache_read_input_tokens": 100,
            "cache_creation_input_tokens": 50,
            "output_tokens": 30,
        }
    )

    # producer contract: prompt_tokens is cache-inclusive total input
    assert info.prompt_tokens == 21 + 100 + 50
    assert info.cache_read_tokens == 100
    assert info.cache_write_tokens == 50

    usage = {
        "prompt_tokens": info.prompt_tokens,
        "completion_tokens": info.completion_tokens,
        "cache_read_tokens": info.cache_read_tokens,
        "cache_write_tokens": info.cache_write_tokens,
    }
    pricing = {
        "prompt": "3.0",
        "completion": "15.0",
        "input_cache_reads": "0.30",
        "input_cache_writes": "3.75",
    }

    cost = calculate_cost(usage, pricing)

    expected = (
        (21 * 3.0 / 1_000_000)
        + (30 * 15.0 / 1_000_000)
        + (100 * 0.30 / 1_000_000)
        + (50 * 3.75 / 1_000_000)
    )
    assert cost == pytest.approx(expected)


def test_claude_build_final_usage_to_calculate_cost_disjoint_billing() -> None:
    """Streaming path: claude_format.build_final_usage → calculate_cost.

    Same upstream numbers as the non-streaming path; cost should match.
    """
    from serving.adapters.claude_format import build_final_usage

    usage = build_final_usage(
        input_tokens=21,
        output_tokens=30,
        cache_read_input_tokens=100,
        cache_creation_input_tokens=50,
    )

    assert usage["prompt_tokens"] == 21 + 100 + 50
    assert usage["total_tokens"] == 21 + 100 + 50 + 30

    pricing = {
        "prompt": "3.0",
        "completion": "15.0",
        "input_cache_reads": "0.30",
        "input_cache_writes": "3.75",
    }

    cost = calculate_cost(usage, pricing)

    expected = (
        (21 * 3.0 / 1_000_000)
        + (30 * 15.0 / 1_000_000)
        + (100 * 0.30 / 1_000_000)
        + (50 * 3.75 / 1_000_000)
    )
    assert cost == pytest.approx(expected)


def test_deepseek_normalize_usage_to_calculate_cost() -> None:
    """DeepSeek upstream → profiles.normalize_usage_deepseek → calculate_cost.

    DeepSeek already reports cache-inclusive prompt_tokens (OpenAI-style)
    plus prompt_cache_hit/miss. Normalizer must keep prompt_tokens as-is so
    calculate_cost subtracts only the cache_read portion, leaving the miss
    portion (200) billed at the prompt rate.
    """
    from serving.adapters.profiles import normalize_usage_deepseek

    info = normalize_usage_deepseek(
        {
            "prompt_tokens": 1000,
            "prompt_cache_hit_tokens": 800,
            "prompt_cache_miss_tokens": 200,
            "completion_tokens": 50,
        }
    )

    assert info.prompt_tokens == 1000
    assert info.cache_read_tokens == 800

    usage = {
        "prompt_tokens": info.prompt_tokens,
        "completion_tokens": info.completion_tokens,
        "cache_read_tokens": info.cache_read_tokens,
    }
    pricing = {
        "prompt": "0.27",
        "completion": "1.10",
        "input_cache_reads": "0.07",
    }

    cost = calculate_cost(usage, pricing)

    # billable_prompt = 1000 - 800 = 200 (the miss portion)
    expected = (200 * 0.27 / 1_000_000) + (50 * 1.10 / 1_000_000) + (800 * 0.07 / 1_000_000)
    assert cost == pytest.approx(expected)


def test_anthropic_messages_log_usage_to_calculate_cost() -> None:
    """Anthropic Messages router log shape → calculate_cost.

    Mirrors the dict that _schedule_log_store_task in
    serving/servers/routers/anthropic_messages.py builds before passing to
    log_request, ensuring the storage-layer billing matches the producer's
    cache-inclusive prompt_tokens.
    """
    input_tokens, cache_read, cache_write, output_tokens = 50, 100_000, 0, 200
    prompt_tokens = input_tokens + cache_read + cache_write
    usage = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": output_tokens,
        "total_tokens": prompt_tokens + output_tokens,
        "cache_read_tokens": cache_read,
        "cache_write_tokens": cache_write,
    }
    pricing = {
        "prompt": "3.0",
        "completion": "15.0",
        "input_cache_reads": "0.30",
        "input_cache_writes": "3.75",
    }

    cost = calculate_cost(usage, pricing)

    # cached should be << prompt (the bug from #338 is the reverse)
    assert cache_read < prompt_tokens
    expected = (50 * 3.0 / 1_000_000) + (200 * 15.0 / 1_000_000) + (100_000 * 0.30 / 1_000_000)
    assert cost == pytest.approx(expected)
