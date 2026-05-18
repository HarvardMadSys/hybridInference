"""Cost-budgeted mean-TTFT LP solver for RouteWise primary selection."""

from __future__ import annotations

from dataclasses import dataclass

_EPS = 1e-12
# Simulator/real-eval parity: RouteWise adds a tiny cost nudge to the latency
# objective so nearly-equal latency choices prefer the cheaper provider.
_COST_TIEBREAK_SEC = 1e-6


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
    """Solve the current RouteWise body LP.

    Objective:
        minimize ``sum_j pi_j * (mean_ttft_j + 1e-6 * normalized_cost_j)``

    Subject to:
        ``sum_j pi_j * cost_j <= c_min + alpha * (c_max - c_min)``
        ``sum_j pi_j = 1``
        ``pi_j >= 0``

    With one cost constraint plus the simplex constraint, an optimum has
    support size at most two.  Enumerating singleton and pair optima avoids
    bringing a general LP solver into the request path.
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

    if len(finite) == 1:
        only = finite[0]
        return LPSolution(
            weights={only.endpoint_id: 1.0},
            budget_usd=only.cost_usd,
            status="single_provider",
        )

    alpha = max(0.0, min(float(alpha), 1.0))
    c_min = min(c.cost_usd for c in finite)
    c_max = max(c.cost_usd for c in finite)
    budget = c_min + alpha * (c_max - c_min)
    objective_by_id = _cost_tiebroken_objective(finite, c_min=c_min, c_max=c_max)

    best_weights: dict[str, float] | None = None
    best_objective = float("inf")

    def consider(weights: dict[str, float]) -> None:
        nonlocal best_weights, best_objective
        total = sum(weights.values())
        if total <= 0:
            return
        normalized = {eid: w / total for eid, w in weights.items() if w > _EPS}
        cost = sum(_by_id[eid].cost_usd * w for eid, w in normalized.items())
        if cost > budget + _EPS:
            return
        objective = sum(objective_by_id[eid] * w for eid, w in normalized.items())
        if objective < best_objective - _EPS:
            best_objective = objective
            best_weights = normalized
        elif abs(objective - best_objective) <= _EPS and best_weights is not None:
            current_cost = sum(_by_id[eid].cost_usd * w for eid, w in best_weights.items())
            if cost < current_cost:
                best_weights = normalized

    _by_id = {c.endpoint_id: c for c in finite}

    # Singleton optima.
    for c in finite:
        consider({c.endpoint_id: 1.0})

    # Pair optima: if the lower-objective provider is too expensive, mix it
    # with the cheaper provider until the budget constraint binds.
    for i, a in enumerate(finite):
        for b in finite[i + 1 :]:
            if objective_by_id[a.endpoint_id] <= objective_by_id[b.endpoint_id]:
                best, other = a, b
            else:
                best, other = b, a

            if best.cost_usd <= budget:
                consider({best.endpoint_id: 1.0})
                continue
            if other.cost_usd > budget:
                continue
            denom = best.cost_usd - other.cost_usd
            if denom <= 0:
                continue
            best_weight = (budget - other.cost_usd) / denom
            best_weight = max(0.0, min(1.0, best_weight))
            consider(
                {
                    best.endpoint_id: best_weight,
                    other.endpoint_id: 1.0 - best_weight,
                }
            )

    if not best_weights:
        cheapest = min(finite, key=lambda c: (c.cost_usd, c.mean_ttft_sec))
        return LPSolution(
            weights={cheapest.endpoint_id: 1.0},
            budget_usd=budget,
            status="cheapest_fallback",
        )

    return LPSolution(weights=best_weights, budget_usd=budget, status="optimal")


def _cost_tiebroken_objective(
    candidates: list[LPCandidate],
    *,
    c_min: float,
    c_max: float,
) -> dict[str, float]:
    """Return simulator-compatible latency objective with cost nudge."""

    cost_span = c_max - c_min
    if cost_span <= _EPS:
        return {c.endpoint_id: c.mean_ttft_sec for c in candidates}
    return {
        c.endpoint_id: c.mean_ttft_sec + _COST_TIEBREAK_SEC * ((c.cost_usd - c_min) / cost_span)
        for c in candidates
    }
