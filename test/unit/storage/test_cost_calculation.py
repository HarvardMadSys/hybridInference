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
