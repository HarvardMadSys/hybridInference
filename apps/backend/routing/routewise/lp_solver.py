"""LP-based latency-aware provider selection for Layer 2.

Solves the min-latency provider mix under a normalized cost budget using PuLP:

    minimize:   sum_j pi_j * mean_latency_j
    subject to: sum_j pi_j * c_j <= budget
                sum_j pi_j = 1
                pi_j >= 0

where ``budget = (1-alpha) * c_min + alpha * c_max`` interpolates between the
cheapest and most expensive finite-cost endpoints.
"""

from __future__ import annotations

import pulp


def solve_cost_budgeted_latency_lp(
    endpoint_ids: list[str],
    mean_latencies_sec: dict[str, float],
    costs: dict[str, float],
    alpha: float,
) -> tuple[dict[str, float], str]:
    """Solve min-latency provider mix under normalized cost budget.

    The budget is ``(1-alpha) * c_min + alpha * c_max`` over finite-cost
    endpoints.  Returns normalized endpoint weights and a status string.
    """
    finite = [
        eid
        for eid in endpoint_ids
        if eid in costs
        and eid in mean_latencies_sec
        and costs[eid] < float("inf")
        and mean_latencies_sec[eid] < float("inf")
    ]
    if not finite:
        return {}, "no_providers"

    alpha = min(max(alpha, 0.0), 1.0)
    c_min = min(costs[eid] for eid in finite)
    c_max = max(costs[eid] for eid in finite)
    budget = (1.0 - alpha) * c_min + alpha * c_max

    prob = pulp.LpProblem("cost_budgeted_latency_selection", pulp.LpMinimize)
    pi_vars = {
        eid: pulp.LpVariable(f"pi_{i}", lowBound=0.0, upBound=1.0) for i, eid in enumerate(finite)
    }

    prob += pulp.lpSum(mean_latencies_sec[eid] * pi_vars[eid] for eid in finite)
    prob += pulp.lpSum(costs[eid] * pi_vars[eid] for eid in finite) <= budget
    prob += pulp.lpSum(pi_vars[eid] for eid in finite) == 1.0
    prob.solve(pulp.PULP_CBC_CMD(msg=0))

    if prob.status != pulp.constants.LpStatusOptimal:
        return {}, "infeasible"

    weights = {
        eid: float(value)
        for eid, var in pi_vars.items()
        if (value := var.varValue) is not None and value > 1e-6
    }
    total = sum(weights.values())
    if total <= 0:
        return {}, "infeasible"
    return {eid: weight / total for eid, weight in weights.items()}, "optimal"
