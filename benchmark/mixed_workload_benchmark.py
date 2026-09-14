"""Deterministic mixed-workload benchmark for prefill-load-aware RouteWise.

Exercises the real RouteWiseRouter under synthetic prefill pressure to answer:
  Does consuming the existing prefill-load signal produce a measurable,
  robust improvement under heavy mixed workloads without harming ordinary traffic?

Usage:
    python benchmark/mixed_workload_benchmark.py
"""

from __future__ import annotations

import random
import statistics
import time
from dataclasses import dataclass, field
from typing import Any

from routing.route_table import EffectiveRoute
from routing.routewise.config import RouteWiseConfig
from routing.routewise.router import RouteWiseRouter

# ---------------------------------------------------------------------------
# Helpers
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
        self,
        endpoint_id: str,
        prompt_price: str = "1.0",
        completion_price: str = "1.0",
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


def _make_router(
    endpoints: list[tuple[str, str, str]],
    prefill_backlog: dict[str, int],
    latency: dict[str, float],
    *,
    feature_enabled: bool,
    seed: int = 42,
    scale_ms_per_1k: float = 1.0,
    max_penalty_ms: float = 5000.0,
) -> RouteWiseRouter:
    adapters = [
        _FakeAdapter(ep_id, prompt_price, completion_price)
        for ep_id, prompt_price, completion_price in endpoints
    ]
    fr = _FakeRouteTable()
    fr.add("test-model", [(adapter, 1.0 / len(adapters)) for adapter in adapters])

    config = RouteWiseConfig(
        prefill_load_routing_enabled=feature_enabled,
        random_seed=seed,
        prefill_load_scale_ms_per_1k=scale_ms_per_1k,
        prefill_load_max_penalty_ms=max_penalty_ms,
    )
    router = RouteWiseRouter(route_table=fr, config=config)

    for ep_id, tokens in prefill_backlog.items():
        router._prefill_load._backlog[ep_id] = tokens

    now = time.time()
    for ep_id, latency_ms in latency.items():
        router._latency_profiles[ep_id].record(now, latency_ms)

    return router


# ---------------------------------------------------------------------------
# Workload definitions
# ---------------------------------------------------------------------------


@dataclass
class RequestSpec:
    request_id: str
    prompt_tokens: int
    output_tokens: int
    category: str


@dataclass
class RequestResult:
    request_id: str
    category: str
    endpoint_id: str
    prompt_tokens: int
    output_tokens: int
    ttft_ms: float
    base_ttft_ms: float
    penalty_ms: float
    prefill_before: int
    wait_ms: float = 0.0
    completed: bool = True


@dataclass
class WorkloadResult:
    scenario: str
    feature_enabled: bool
    requests: list[RequestResult] = field(default_factory=list)
    endpoint_counts: dict[str, int] = field(default_factory=dict)
    endpoint_prefill_tokens: dict[str, int] = field(default_factory=dict)
    total_time_ms: float = 0.0

    @property
    def completed(self) -> int:
        return sum(1 for r in self.requests if r.completed)

    @property
    def ttft_values(self) -> list[float]:
        return [r.ttft_ms for r in self.requests if r.completed]

    @property
    def base_ttft_values(self) -> list[float]:
        return [r.base_ttft_ms for r in self.requests if r.completed]

    @property
    def penalty_values(self) -> list[float]:
        return [r.penalty_ms for r in self.requests if r.completed]

    @property
    def p50_ttft(self) -> float:
        v = self.ttft_values
        return statistics.median(v) if v else 0.0

    @property
    def p95_ttft(self) -> float:
        v = sorted(self.ttft_values)
        if not v:
            return 0.0
        idx = int(len(v) * 0.95)
        return v[min(idx, len(v) - 1)]

    @property
    def p99_ttft(self) -> float:
        v = sorted(self.ttft_values)
        if not v:
            return 0.0
        idx = int(len(v) * 0.99)
        return v[min(idx, len(v) - 1)]

    @property
    def mean_ttft(self) -> float:
        v = self.ttft_values
        return statistics.mean(v) if v else 0.0

    @property
    def mean_penalty(self) -> float:
        v = self.penalty_values
        return statistics.mean(v) if v else 0.0

    @property
    def throughput(self) -> float:
        """Routing throughput: decisions per second.

        This measures how fast the router executes the routing loop, NOT
        end-to-end request completion. End-to-end throughput would be
        total_completed_tokens / simulated_ttft, which is orders of
        magnitude lower (simulated TTFTs are hundreds of seconds).
        """
        if self.total_time_ms <= 0:
            return 0.0
        return self.completed / (self.total_time_ms / 1000.0)


def generate_workload(
    n_requests: int,
    mix: dict[str, float],
    sizes: dict[str, tuple[int, int]],
    seed: int = 42,
) -> list[RequestSpec]:
    """Generate a deterministic workload."""
    rng = random.Random(seed)
    categories = list(mix.keys())
    weights = [mix[c] for c in categories]

    requests: list[RequestSpec] = []
    for i in range(n_requests):
        cat = rng.choices(categories, weights=weights, k=1)[0]
        inp, out = sizes[cat]
        requests.append(
            RequestSpec(
                request_id=f"req-{i:04d}",
                prompt_tokens=inp,
                output_tokens=out,
                category=cat,
            )
        )
    return requests


def run_workload(
    router: RouteWiseRouter,
    requests: list[RequestSpec],
    *,
    release_every: int = 10,
    prefill_rate_tps: float = 5000.0,  # tokens per second
) -> WorkloadResult:
    """Run a workload through the router and collect results.

    Simulates prefill pressure using the real execution path: acquire a
    lease on the selected endpoint, release periodically to simulate
    completion. This exercises RouteWise's actual prefill tracking,
    not a manual mutation of internal state.

    Actual TTFT is modeled as: base_latency + (backlog_at_arrival / prefill_rate)
    This approximates the queuing delay a request experiences.
    """
    result = WorkloadResult(
        scenario="mixed",
        feature_enabled=router.config.prefill_load_routing_enabled,
    )

    start_time = time.perf_counter()
    active_leases: list[tuple[str, Any]] = []

    for i, req in enumerate(requests):
        # Periodically release oldest lease to simulate completion
        if i > 0 and i % release_every == 0 and active_leases:
            _, lease = active_leases.pop(0)
            router._prefill_load.release(lease, prefill_confirmed=True)

        decision = router._select_decision(
            "test-model",
            {"prompt_tokens": req.prompt_tokens, "request_id": req.request_id},
        )

        if decision is None:
            result.requests.append(
                RequestResult(
                    request_id=req.request_id,
                    category=req.category,
                    endpoint_id="NONE",
                    prompt_tokens=req.prompt_tokens,
                    output_tokens=req.output_tokens,
                    ttft_ms=0.0,
                    base_ttft_ms=0.0,
                    penalty_ms=0.0,
                    prefill_before=0,
                    completed=False,
                )
            )
            continue

        endpoint_id: str = decision.adapter.config.endpoint_id

        # Extract base TTFT from decision metadata
        base_ttft_sec = decision.metadata.get("selected_mean_ttft_sec", 0.5)
        base_ttft_ms = base_ttft_sec * 1000.0

        # Record backlog BEFORE we add this request's pressure
        prefill_before = router._prefill_load.backlog(endpoint_id)

        # Compute actual TTFT: base latency + queuing delay from backlog
        # Queuing delay = backlog / prefill_rate
        queuing_ms = (prefill_before / prefill_rate_tps) * 1000.0
        actual_ttft_ms = base_ttft_ms + queuing_ms

        # Also extract the LP's penalty for comparison
        adjusted_ttft_map = decision.metadata.get("candidate_prefill_load_adjusted_ttft_sec", {})
        adjusted_ttft_sec = adjusted_ttft_map.get(endpoint_id, base_ttft_sec)
        penalty_ms = max(0.0, (adjusted_ttft_sec * 1000.0) - base_ttft_ms)

        # Track endpoint selection
        result.endpoint_counts[endpoint_id] = result.endpoint_counts.get(endpoint_id, 0) + 1
        result.endpoint_prefill_tokens[endpoint_id] = (
            result.endpoint_prefill_tokens.get(endpoint_id, 0) + req.prompt_tokens
        )

        result.requests.append(
            RequestResult(
                request_id=req.request_id,
                category=req.category,
                endpoint_id=endpoint_id,
                prompt_tokens=req.prompt_tokens,
                output_tokens=req.output_tokens,
                ttft_ms=actual_ttft_ms,
                base_ttft_ms=base_ttft_ms,
                penalty_ms=penalty_ms,
                prefill_before=prefill_before,
                completed=True,
            )
        )

        # Use the real execution path: acquire a lease on the selected endpoint
        # so subsequent requests see this request's prefill pressure.
        lease = router._prefill_load.acquire(endpoint_id, req.prompt_tokens)
        active_leases.append((endpoint_id, lease))

    # Release remaining leases
    for _, lease in active_leases:
        router._prefill_load.release(lease, prefill_confirmed=True)

    end_time = time.perf_counter()
    result.total_time_ms = (end_time - start_time) * 1000.0

    return result


def print_result(result: WorkloadResult) -> None:
    """Print a workload result."""
    print(f"\n{'=' * 70}")
    print(f"Scenario: {result.scenario}")
    print(f"Feature enabled: {result.feature_enabled}")
    print(f"Completed: {result.completed}/{len(result.requests)}")
    print(f"Total time: {result.total_time_ms:.1f} ms")
    print(f"Throughput (routing decisions/s): {result.throughput:.2f}")
    print(f"TTFT p50: {result.p50_ttft:.1f} ms")
    print(f"TTFT p95: {result.p95_ttft:.1f} ms")
    print(f"TTFT p99: {result.p99_ttft:.1f} ms")
    print(f"TTFT mean: {result.mean_ttft:.1f} ms")
    print(f"Mean penalty: {result.mean_penalty:.1f} ms")
    print("\nEndpoint distribution:")
    total = sum(result.endpoint_counts.values())
    for ep_id, count in sorted(result.endpoint_counts.items()):
        pct = count / total * 100 if total > 0 else 0
        prefill = result.endpoint_prefill_tokens.get(ep_id, 0)
        print(f"  {ep_id}: {count} ({pct:.1f}%) - {prefill:,} input tokens")


def compare_results(baseline: WorkloadResult, patched: WorkloadResult) -> None:
    """Compare baseline vs patched results."""
    print(f"\n{'=' * 70}")
    print("COMPARISON: Baseline vs Patched")
    print(f"{'=' * 70}")

    metrics = [
        ("Throughput (routing decisions/s)", baseline.throughput, patched.throughput),
        ("TTFT p50 (ms)", baseline.p50_ttft, patched.p50_ttft),
        ("TTFT p95 (ms)", baseline.p95_ttft, patched.p95_ttft),
        ("TTFT p99 (ms)", baseline.p99_ttft, patched.p99_ttft),
        ("TTFT mean (ms)", baseline.mean_ttft, patched.mean_ttft),
        ("Mean penalty (ms)", baseline.mean_penalty, patched.mean_penalty),
    ]

    print(f"\n{'Metric':<25} {'Baseline':>12} {'Patched':>12} {'Delta':>12} {'Change':>10}")
    print("-" * 75)
    for name, b, p in metrics:
        delta = p - b
        if b != 0:
            pct = (delta / b) * 100
            sign = "+" if pct >= 0 else ""
            change = f"{sign}{pct:.1f}%"
        else:
            change = "N/A"
        sign = "+" if delta >= 0 else ""
        print(f"{name:<25} {b:>12.2f} {p:>12.2f} {sign}{delta:>11.2f} {change:>10}")

    # Endpoint distribution comparison
    print("\nEndpoint distribution:")
    all_eps = sorted(set(baseline.endpoint_counts) | set(patched.endpoint_counts))
    for ep in all_eps:
        b_cnt = baseline.endpoint_counts.get(ep, 0)
        p_cnt = patched.endpoint_counts.get(ep, 0)
        b_pct = b_cnt / max(baseline.completed, 1) * 100
        p_pct = p_cnt / max(patched.completed, 1) * 100
        print(f"  {ep}: baseline={b_cnt} ({b_pct:.1f}%) -> patched={p_cnt} ({p_pct:.1f}%)")

    # Category breakdown
    print("\nTTFT by category (p50):")
    categories = sorted({r.category for r in baseline.requests})
    for cat in categories:
        b_vals = [r.ttft_ms for r in baseline.requests if r.category == cat and r.completed]
        p_vals = [r.ttft_ms for r in patched.requests if r.category == cat and r.completed]
        if b_vals and p_vals:
            b_med = statistics.median(b_vals)
            p_med = statistics.median(p_vals)
            delta = p_med - b_med
            sign = "+" if delta >= 0 else ""
            print(
                f"  {cat:<12}: baseline={b_med:>8.1f}ms -> patched={p_med:>8.1f}ms ({sign}{delta:.1f}ms)"
            )


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------


def scenario_mixed_workload() -> tuple[WorkloadResult, WorkloadResult]:
    """Scenario 1: Mixed workload."""
    print("\n" + "=" * 70)
    print("SCENARIO 1: Mixed Workload (100 requests)")
    print("=" * 70)

    endpoints = [
        ("test-model:a", "1.0", "1.0"),
        ("test-model:b", "1.0", "1.0"),
        ("test-model:c", "1.0", "1.0"),
    ]
    latency = {
        "test-model:a": 150.0,
        "test-model:b": 200.0,
        "test-model:c": 250.0,
    }
    prefill_backlog = {
        "test-model:a": 300_000,
        "test-model:b": 50_000,
        "test-model:c": 20_000,
    }

    mix = {"short": 0.40, "medium": 0.30, "heavy": 0.20, "extreme": 0.10}
    sizes = {
        "short": (2_000, 500),
        "medium": (16_000, 1_000),
        "heavy": (64_000, 2_000),
        "extreme": (128_000, 4_000),
    }

    requests = generate_workload(100, mix, sizes, seed=42)

    baseline_router = _make_router(endpoints, prefill_backlog, latency, feature_enabled=False)
    baseline = run_workload(baseline_router, requests)
    baseline.scenario = "mixed_baseline"
    print_result(baseline)

    patched_router = _make_router(endpoints, prefill_backlog, latency, feature_enabled=True)
    patched = run_workload(patched_router, requests)
    patched.scenario = "mixed_patched"
    print_result(patched)

    compare_results(baseline, patched)
    return baseline, patched


def scenario_heavy_then_interactive() -> tuple[WorkloadResult, WorkloadResult]:
    """Scenario 2: Heavy jobs first, then interactive burst."""
    print("\n" + "=" * 70)
    print("SCENARIO 2: Heavy + Interactive")
    print("=" * 70)

    endpoints = [
        ("test-model:a", "1.0", "1.0"),
        ("test-model:b", "1.0", "1.0"),
    ]
    latency = {
        "test-model:a": 150.0,
        "test-model:b": 200.0,
    }
    prefill_backlog = {
        "test-model:a": 500_000,
        "test-model:b": 0,
    }

    heavy_requests = [RequestSpec(f"heavy-{i}", 128_000, 4_000, "heavy") for i in range(10)]
    interactive_requests = [
        RequestSpec(f"interactive-{i}", 2_000, 500, "interactive") for i in range(30)
    ]
    all_requests = heavy_requests + interactive_requests

    baseline_router = _make_router(endpoints, prefill_backlog, latency, feature_enabled=False)
    baseline = run_workload(baseline_router, all_requests, release_every=5)
    baseline.scenario = "heavy_interactive_baseline"
    print_result(baseline)

    patched_router = _make_router(endpoints, prefill_backlog, latency, feature_enabled=True)
    patched = run_workload(patched_router, all_requests, release_every=5)
    patched.scenario = "heavy_interactive_patched"
    print_result(patched)

    compare_results(baseline, patched)

    print("\n--- Interactive-only TTFT comparison ---")
    for label, result in [("Baseline", baseline), ("Patched", patched)]:
        interactive_ttfts = [r.ttft_ms for r in result.requests if r.category == "interactive"]
        if interactive_ttfts:
            print(
                f"  {label}: p50={statistics.median(interactive_ttfts):.1f}ms, "
                f"p95={sorted(interactive_ttfts)[int(len(interactive_ttfts) * 0.95)]:.1f}ms, "
                f"mean={statistics.mean(interactive_ttfts):.1f}ms"
            )

    return baseline, patched


def scenario_small_only() -> tuple[WorkloadResult, WorkloadResult]:
    """Scenario 3: Small-only workload (control)."""
    print("\n" + "=" * 70)
    print("SCENARIO 3: Small-only Control (100 requests)")
    print("=" * 70)

    endpoints = [
        ("test-model:a", "1.0", "1.0"),
        ("test-model:b", "1.0", "1.0"),
    ]
    latency = {
        "test-model:a": 150.0,
        "test-model:b": 200.0,
    }
    prefill_backlog = {
        "test-model:a": 5_000,
        "test-model:b": 5_000,
    }

    requests = [RequestSpec(f"small-{i}", 2_000, 500, "short") for i in range(100)]

    baseline_router = _make_router(endpoints, prefill_backlog, latency, feature_enabled=False)
    baseline = run_workload(baseline_router, requests)
    baseline.scenario = "small_baseline"
    print_result(baseline)

    patched_router = _make_router(endpoints, prefill_backlog, latency, feature_enabled=True)
    patched = run_workload(patched_router, requests)
    patched.scenario = "small_patched"
    print_result(patched)

    compare_results(baseline, patched)
    return baseline, patched


def scenario_heavy_only() -> tuple[WorkloadResult, WorkloadResult]:
    """Scenario 4: Heavy-only workload."""
    print("\n" + "=" * 70)
    print("SCENARIO 4: Heavy-only (50 requests)")
    print("=" * 70)

    endpoints = [
        ("test-model:a", "1.0", "1.0"),
        ("test-model:b", "1.0", "1.0"),
        ("test-model:c", "1.0", "1.0"),
    ]
    latency = {
        "test-model:a": 150.0,
        "test-model:b": 200.0,
        "test-model:c": 250.0,
    }
    prefill_backlog = {
        "test-model:a": 100_000,
        "test-model:b": 50_000,
        "test-model:c": 20_000,
    }

    rng = random.Random(42)
    requests = [
        RequestSpec(
            f"heavy-{i}",
            rng.choice([64_000, 128_000]),
            rng.choice([2_000, 4_000]),
            "heavy",
        )
        for i in range(50)
    ]

    baseline_router = _make_router(endpoints, prefill_backlog, latency, feature_enabled=False)
    baseline = run_workload(baseline_router, requests, release_every=5)
    baseline.scenario = "heavy_baseline"
    print_result(baseline)

    patched_router = _make_router(endpoints, prefill_backlog, latency, feature_enabled=True)
    patched = run_workload(patched_router, requests, release_every=5)
    patched.scenario = "heavy_patched"
    print_result(patched)

    compare_results(baseline, patched)
    return baseline, patched


def scenario_equal_load() -> tuple[WorkloadResult, WorkloadResult]:
    """Scenario 5: Equal load on all endpoints."""
    print("\n" + "=" * 70)
    print("SCENARIO 5: Equal Load (100 requests)")
    print("=" * 70)

    endpoints = [
        ("test-model:a", "1.0", "1.0"),
        ("test-model:b", "1.0", "1.0"),
    ]
    latency = {
        "test-model:a": 150.0,
        "test-model:b": 200.0,
    }
    prefill_backlog = {
        "test-model:a": 100_000,
        "test-model:b": 100_000,
    }

    mix = {"short": 0.5, "medium": 0.3, "heavy": 0.2}
    sizes = {
        "short": (2_000, 500),
        "medium": (16_000, 1_000),
        "heavy": (64_000, 2_000),
    }
    requests = generate_workload(100, mix, sizes, seed=42)

    baseline_router = _make_router(endpoints, prefill_backlog, latency, feature_enabled=False)
    baseline = run_workload(baseline_router, requests)
    baseline.scenario = "equal_baseline"
    print_result(baseline)

    patched_router = _make_router(endpoints, prefill_backlog, latency, feature_enabled=True)
    patched = run_workload(patched_router, requests)
    patched.scenario = "equal_patched"
    print_result(patched)

    compare_results(baseline, patched)
    return baseline, patched


def scenario_sensitivity() -> None:
    """Scenario 6: Sensitivity analysis."""
    print("\n" + "=" * 70)
    print("SCENARIO 6: Sensitivity Analysis")
    print("=" * 70)

    endpoints = [
        ("test-model:a", "1.0", "1.0"),
        ("test-model:b", "1.0", "1.0"),
    ]
    latency = {
        "test-model:a": 150.0,
        "test-model:b": 200.0,
    }
    prefill_backlog = {
        "test-model:a": 300_000,
        "test-model:b": 30_000,
    }

    mix = {"short": 0.4, "medium": 0.3, "heavy": 0.2, "extreme": 0.1}
    sizes = {
        "short": (2_000, 500),
        "medium": (16_000, 1_000),
        "heavy": (64_000, 2_000),
        "extreme": (128_000, 4_000),
    }
    requests = generate_workload(100, mix, sizes, seed=42)

    scales = [0.25, 0.5, 1.0, 2.0]
    caps = [1000.0, 2500.0, 5000.0]

    print(f"\n{'Scale':>6} {'Cap':>8} {'p50 TTFT':>10} {'p95 TTFT':>10} {'A%':>6} {'B%':>6}")
    print("-" * 60)

    for scale in scales:
        for cap in caps:
            router = _make_router(
                endpoints,
                prefill_backlog,
                latency,
                feature_enabled=True,
                scale_ms_per_1k=scale,
                max_penalty_ms=cap,
            )
            result = run_workload(router, requests)
            a_pct = result.endpoint_counts.get("test-model:a", 0) / max(result.completed, 1) * 100
            b_pct = result.endpoint_counts.get("test-model:b", 0) / max(result.completed, 1) * 100
            print(
                f"{scale:>6.2f} {cap:>8.0f} {result.p50_ttft:>10.1f} "
                f"{result.p95_ttft:>10.1f} {a_pct:>5.1f}% {b_pct:>5.1f}%"
            )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    print("Prefill-Load-Aware RouteWise — Mixed Workload Benchmark")
    print("=" * 70)

    scenario_mixed_workload()
    scenario_heavy_then_interactive()
    scenario_small_only()
    scenario_heavy_only()
    scenario_equal_load()
    scenario_sensitivity()

    print("\n" + "=" * 70)
    print("BENCHMARK COMPLETE")
    print("=" * 70)


if __name__ == "__main__":
    main()
