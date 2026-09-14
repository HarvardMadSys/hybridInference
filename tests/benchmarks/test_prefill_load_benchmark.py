"""Deterministic benchmark for prefill-load-aware RouteWise routing.

Compares baseline (feature disabled) vs patched (feature enabled) under
controlled conditions with fixed random seeds.

Usage:
    python -m pytest tests/benchmarks/test_prefill_load_benchmark.py -v
    OR
    python tests/benchmarks/test_prefill_load_benchmark.py
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from routing.route_table import EffectiveRoute
from routing.routewise.config import RouteWiseConfig
from routing.routewise.router import RouteWiseRouter

# ---------------------------------------------------------------------------
# Fake route table
# ---------------------------------------------------------------------------


class _FakeRouteTable:
    def __init__(self) -> None:
        self._routes: dict[str, tuple[tuple[Any, float], ...]] = {}
        self.weight_overrides: dict[str, float] = {}

    def add(self, model_id: str, adapters_with_weights: list[tuple[Any, float]]) -> None:
        self._routes[model_id] = tuple(adapters_with_weights)

    def iter_effective_routes(self) -> tuple[EffectiveRoute, ...]:
        return tuple(
            EffectiveRoute(
                route_key=model_id,
                canonical_model_id=model_id,
                adapters=tuple(
                    (
                        adapter,
                        float(self.weight_overrides.get(adapter.config.endpoint_id, weight)),
                    )
                    for adapter, weight in adapters
                ),
            )
            for model_id, adapters in self._routes.items()
        )

    def canonical_id(self, model_id: str) -> str:
        return model_id


class _FakeAdapter:
    def __init__(
        self, endpoint_id: str, prompt_price: str = "1.0", completion_price: str = "1.0"
    ) -> None:
        self.config = type(
            "Config",
            (),
            {
                "endpoint_id": endpoint_id,
                "provider": "test",
                "base_url": f"https://{endpoint_id}.example/v1",
                "pricing": {"prompt": prompt_price, "completion": completion_price},
            },
        )()


# ---------------------------------------------------------------------------
# Benchmark scenarios
# ---------------------------------------------------------------------------


def _make_router(
    endpoints: list[tuple[str, str, str]],
    prefill_backlog: dict[str, int],
    *,
    feature_enabled: bool,
    seed: int = 42,
) -> RouteWiseRouter:
    """Build a router with given endpoints and prefill backlog.

    Args:
        endpoints: List of (endpoint_id, prompt_price, completion_price)
        prefill_backlog: Map of endpoint_id -> outstanding prefill tokens
        feature_enabled: Whether to enable prefill-load routing
        seed: Random seed for reproducibility
    """
    adapters = [
        _FakeAdapter(ep_id, prompt_price, completion_price)
        for ep_id, prompt_price, completion_price in endpoints
    ]
    fr = _FakeRouteTable()
    fr.add("test-model", [(adapter, 1.0 / len(adapters)) for adapter in adapters])

    config = RouteWiseConfig(
        prefill_load_routing_enabled=feature_enabled,
        random_seed=seed,
    )
    router = RouteWiseRouter(route_table=fr, config=config)

    # Set prefill backlog
    for ep_id, tokens in prefill_backlog.items():
        router._prefill_load._backlog[ep_id] = tokens

    return router


def _run_selections(router: RouteWiseRouter, n: int = 100) -> dict[str, int]:
    """Run n selections and return endpoint counts."""
    counts: dict[str, int] = {}
    for i in range(n):
        decision = router._select_decision(
            "test-model", {"prompt_tokens": 1000, "request_id": f"req-{i}"}
        )
        if decision is not None:
            ep_id = decision.adapter.config.endpoint_id
            counts[ep_id] = counts.get(ep_id, 0) + 1
    return counts


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------


BENCHMARK_SCENARIOS = {
    "core_proof": {
        "description": "A has lower base latency (100ms) but 250K backlog. B has higher base (300ms) but 20K backlog. Prefill penalty should overcome base latency advantage.",
        "endpoints": [
            ("test-model:a", "1.0", "1.0"),
            ("test-model:b", "1.0", "1.0"),
        ],
        "prefill_backlog": {
            "test-model:a": 250_000,
            "test-model:b": 20_000,
        },
        "latency": {
            "test-model:a": 100.0,
            "test-model:b": 300.0,
        },
        "expected_patched_preference": "test-model:b",
        "expected_baseline_preference": "test-model:a",
    },
    "materially_better_wins": {
        "description": "A has high load but much lower base TTFT.",
        "endpoints": [
            ("test-model:a", "1.0", "1.0"),
            ("test-model:b", "1.0", "1.0"),
        ],
        "prefill_backlog": {
            "test-model:a": 500_000,
            "test-model:b": 0,
        },
        "latency": {
            "test-model:a": 100.0,
            "test-model:b": 2000.0,
        },
        "expected_patched_preference": "test-model:a",
    },
    "equal_load": {
        "description": "Both endpoints have equal load.",
        "endpoints": [
            ("test-model:a", "1.0", "1.0"),
            ("test-model:b", "1.0", "1.0"),
        ],
        "prefill_backlog": {
            "test-model:a": 50_000,
            "test-model:b": 50_000,
        },
        "latency": {
            "test-model:a": 200.0,
            "test-model:b": 300.0,
        },
        "expected_patched_preference": "test-model:a",
    },
    "all_loaded": {
        "description": "All endpoints heavily loaded.",
        "endpoints": [
            ("test-model:a", "1.0", "1.0"),
            ("test-model:b", "1.0", "1.0"),
        ],
        "prefill_backlog": {
            "test-model:a": 1_000_000,
            "test-model:b": 2_000_000,
        },
        "latency": {
            "test-model:a": 200.0,
            "test-model:b": 300.0,
        },
        "expected_patched_preference": "test-model:a",
    },
    "single_huge": {
        "description": "One endpoint has extreme backlog.",
        "endpoints": [
            ("test-model:a", "1.0", "1.0"),
            ("test-model:b", "1.0", "1.0"),
        ],
        "prefill_backlog": {
            "test-model:a": 5_000_000,
            "test-model:b": 0,
        },
        "latency": {
            "test-model:a": 200.0,
            "test-model:b": 300.0,
        },
        "expected_patched_preference": "test-model:b",
    },
}


# ---------------------------------------------------------------------------
# Benchmark runner
# ---------------------------------------------------------------------------


def run_benchmark(
    scenario_name: str,
    n_selections: int = 200,
) -> dict[str, Any]:
    """Run a benchmark scenario and return results."""
    scenario = BENCHMARK_SCENARIOS[scenario_name]

    results = {
        "scenario": scenario_name,
        "description": scenario["description"],
        "n_selections": n_selections,
    }

    # Baseline: feature disabled
    baseline_router = _make_router(
        scenario["endpoints"],
        scenario["prefill_backlog"],
        feature_enabled=False,
    )
    # Set latency profiles if specified
    if "latency" in scenario:
        now = time.time()
        for ep_id, latency_ms in scenario["latency"].items():
            baseline_router._latency_profiles[ep_id].record(now, latency_ms)

    baseline_counts = _run_selections(baseline_router, n_selections)
    results["baseline"] = {
        "counts": baseline_counts,
        "feature_enabled": False,
    }

    # Patched: feature enabled
    patched_router = _make_router(
        scenario["endpoints"],
        scenario["prefill_backlog"],
        feature_enabled=True,
    )
    if "latency" in scenario:
        now = time.time()
        for ep_id, latency_ms in scenario["latency"].items():
            patched_router._latency_profiles[ep_id].record(now, latency_ms)

    patched_counts = _run_selections(patched_router, n_selections)
    results["patched"] = {
        "counts": patched_counts,
        "feature_enabled": True,
    }

    # Determine winner
    expected = scenario.get("expected_patched_preference")
    if expected:
        patched_winner = max(patched_counts, key=patched_counts.get)
        results["patched_winner"] = patched_winner
        results["expected_preference"] = expected
        results["correct"] = patched_winner == expected

    return results


def print_benchmark_results(results: dict[str, Any]) -> None:
    """Pretty-print benchmark results."""
    print(f"\n{'=' * 70}")
    print(f"Scenario: {results['scenario']}")
    print(f"Description: {results['description']}")
    print(f"Selections: {results['n_selections']}")
    print(f"{'=' * 70}")

    for phase in ["baseline", "patched"]:
        data = results[phase]
        print(f"\n  {phase.upper()} (feature_enabled={data['feature_enabled']}):")
        total = sum(data["counts"].values())
        for ep_id, count in sorted(data["counts"].items()):
            pct = count / total * 100 if total > 0 else 0
            print(f"    {ep_id}: {count} ({pct:.1f}%)")

    if "expected_preference" in results:
        print(f"\n  Expected preference: {results['expected_preference']}")
        print(f"  Actual winner: {results['patched_winner']}")
        status = "PASS" if results["correct"] else "FAIL"
        print(f"  Result: {status}")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("scenario_name", list(BENCHMARK_SCENARIOS.keys()))
def test_benchmark_scenario(scenario_name: str) -> None:
    """Run a benchmark scenario and verify expected preference."""
    results = run_benchmark(scenario_name, n_selections=200)
    print_benchmark_results(results)

    # Verify expected preference if specified
    if "expected_preference" in results:
        assert results["correct"], (
            f"Expected {results['expected_preference']} to win, got {results['patched_winner']}"
        )

    # Verify baseline preference if specified
    if "expected_baseline_preference" in results:
        baseline_winner = max(results["baseline"]["counts"], key=results["baseline"]["counts"].get)
        assert baseline_winner == results["expected_baseline_preference"], (
            f"Expected baseline to prefer {results['expected_baseline_preference']}, "
            f"got {baseline_winner}"
        )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    print("Prefill-Load-Aware RouteWise Routing Benchmark")
    print("=" * 70)

    all_pass = True
    for scenario_name in BENCHMARK_SCENARIOS:
        results = run_benchmark(scenario_name, n_selections=200)
        print_benchmark_results(results)
        if "correct" in results and not results["correct"]:
            all_pass = False

    print(f"\n{'=' * 70}")
    if all_pass:
        print("ALL BENCHMARKS PASSED")
    else:
        print("SOME BENCHMARKS FAILED")
    print(f"{'=' * 70}")
