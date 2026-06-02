"""Cost-budgeted mean-TTFT LP solver for RouteWise primary selection."""

from __future__ import annotations

from dataclasses import dataclass

from routewise.core import (
    BudgetLPCandidate,
    cost_tiebroken_objective,
    solve_budget_lp,
)


@dataclass(frozen=True)
class LPCandidate:
    """One feasible provider in the RouteWise body LP."""

    endpoint_id: str
    cost_usd: float
    mean_ttft_sec: float


@dataclass(frozen=True)
class LPSolution:
    """Sparse provider mixture returned by the body LP."""

    weights: dict[str, float]
    budget_usd: float
    status: str


def solve_cost_budgeted_mean_ttft(
    candidates: list[LPCandidate],
    *,
    alpha: float,
) -> LPSolution:
    """Solve the current RouteWise body LP through ``routewise.core``.

    Objective:
        minimize ``sum_j pi_j * (mean_ttft_j + 1e-6 * normalized_cost_j)``

    Subject to:
        ``sum_j pi_j * cost_j <= c_min + alpha * (c_max - c_min)``
        ``sum_j pi_j = 1``
        ``pi_j >= 0``

    With one cost constraint plus the simplex constraint, an optimum has
    support size at most two.  The public RouteWise core owns that exact
    enumeration; this module only maps hybridInference endpoint snapshots into
    the core API and preserves the existing ``LPSolution`` wrapper contract.
    """
    finite = [
        c
        for c in candidates
        if c.cost_usd >= 0
        and c.cost_usd < float("inf")
        and c.mean_ttft_sec >= 0
        and c.mean_ttft_sec < float("inf")
    ]
    if not finite:
        return LPSolution(weights={}, budget_usd=0.0, status="no_feasible_candidates")

    alpha = max(0.0, min(float(alpha), 1.0))
    c_min = min(c.cost_usd for c in finite)
    c_max = max(c.cost_usd for c in finite)
    budget = c_min + alpha * (c_max - c_min)

    if len(finite) == 1:
        only = finite[0]
        return LPSolution(
            weights={only.endpoint_id: 1.0},
            budget_usd=budget,
            status="single_provider",
        )

    latency_objective_ms = [c.mean_ttft_sec * 1000.0 for c in finite]
    effective_costs = [c.cost_usd for c in finite]
    objective_ms = cost_tiebroken_objective(latency_objective_ms, effective_costs)

    result = solve_budget_lp(
        [
            BudgetLPCandidate(
                name=c.endpoint_id,
                objective=objective_ms[index],
                effective_cost=c.cost_usd,
            )
            for index, c in enumerate(finite)
        ],
        budget=budget,
    )
    if not result.feasible or not result.weights:
        cheapest = min(finite, key=lambda c: (c.cost_usd, c.mean_ttft_sec))
        return LPSolution(
            weights={cheapest.endpoint_id: 1.0},
            budget_usd=budget,
            status="cheapest_fallback",
        )

    return LPSolution(weights=dict(result.weights), budget_usd=budget, status="optimal")
