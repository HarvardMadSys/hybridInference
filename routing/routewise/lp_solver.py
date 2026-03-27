"""LP-based latency-aware provider selection for Layer 2.

Solves the cost-minimization LP with tail-latency constraints using PuLP:

    minimize:   sum_j pi_j * c_j * (1 + kappa * e_j)
    subject to: sum_j pi_j * F_j(L) >= target_cdf
                sum_j pi_j = 1
                pi_j >= 0

Reference: experiment/strategies/online_latency_router.py (lines 240-370).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pulp

if TYPE_CHECKING:
    from .latency import ProviderProfile


def solve_provider_lp(
    endpoint_ids: list[str],
    cdfs: list[float],
    costs: list[float],
    error_rates: list[float],
    target_cdf: float = 0.99,
    kappa: float = 0.0,
) -> dict[str, float] | None:
    """Solve the cost-minimization LP with tail constraint.

    minimize:   sum_j pi_j * c_j * (1 + kappa * e_j)
    subject to: sum_j pi_j * F_j(L) >= target_cdf
                sum_j pi_j = 1
                pi_j >= 0

    Args:
        endpoint_ids: List of endpoint identifiers.
        cdfs: CDF values F_j(L) for each endpoint at the SLO threshold.
        costs: Cost per request for each endpoint.
        error_rates: Error rate for each endpoint.
        target_cdf: Target CDF value for the mixing constraint (default 0.99).
        kappa: Error penalty coefficient (default 0).

    Returns:
        Dict of endpoint_id -> weight, or None if infeasible.
    """
    n = len(endpoint_ids)
    if n == 0:
        return None

    # Check feasibility: at least one provider must meet the target CDF.
    if max(cdfs) < target_cdf:
        return None

    # Build LP problem.
    prob = pulp.LpProblem("provider_selection", pulp.LpMinimize)

    # Decision variables: pi_j >= 0.
    pi_vars = [pulp.LpVariable(f"pi_{i}", lowBound=0.0, upBound=1.0) for i in range(n)]

    # Objective: minimize sum_j pi_j * c_j * (1 + kappa * e_j).
    obj_coeffs = [costs[i] * (1.0 + kappa * error_rates[i]) for i in range(n)]
    prob += pulp.lpSum(obj_coeffs[i] * pi_vars[i] for i in range(n))

    # Constraint: sum_j pi_j * F_j(L) >= target_cdf.
    prob += pulp.lpSum(cdfs[i] * pi_vars[i] for i in range(n)) >= target_cdf

    # Constraint: sum_j pi_j = 1.
    prob += pulp.lpSum(pi_vars) == 1.0

    # Solve silently.
    prob.solve(pulp.PULP_CBC_CMD(msg=0))

    if prob.status != pulp.constants.LpStatusOptimal:
        return None

    # Extract weights, filter numerical noise.
    weights: dict[str, float] = {}
    for i, eid in enumerate(endpoint_ids):
        val = pi_vars[i].varValue
        if val is not None and val > 1e-6:
            weights[eid] = float(val)

    # Normalize.
    total = sum(weights.values())
    if total > 0:
        weights = {p: w / total for p, w in weights.items()}
    else:
        return None

    return weights


def solve_provider_lp_with_relaxation(
    endpoint_ids: list[str],
    profiles: dict[str, ProviderProfile],
    costs: dict[str, float],
    slo_sec: float,
    current_time: float,
    target_cdf: float = 0.99,
    kappa: float = 0.0,
    relaxation_factors: tuple[float, ...] = (1.2, 1.5, 2.0),
) -> tuple[dict[str, float], str]:
    """LP with progressive SLO relaxation.

    Attempts the LP at the original SLO.  If infeasible, progressively
    relaxes the SLO by the given factors.  If all relaxations fail, falls
    back to the provider with the highest CDF at the original SLO.

    Args:
        endpoint_ids: List of endpoint identifiers.
        profiles: endpoint_id -> ProviderProfile mapping.
        costs: endpoint_id -> cost per request.
        slo_sec: Original SLO latency in seconds.
        current_time: Reference time for CDF computation.
        target_cdf: Target CDF value (default 0.99).
        kappa: Error penalty coefficient.
        relaxation_factors: SLO relaxation multipliers.

    Returns:
        (weights_dict, status) where status is one of:
        - "optimal": original SLO achieved
        - "relaxed_1.2x" / "relaxed_1.5x" / etc: relaxed SLO used
        - "best_effort": all relaxations failed, pick max-CDF provider
    """
    if not endpoint_ids:
        return {}, "no_providers"

    def _try_solve(slo: float) -> dict[str, float] | None:
        cdfs = [profiles[eid].cdf_at(slo, current_time) for eid in endpoint_ids]
        error_rates = [profiles[eid].error_rate(current_time) for eid in endpoint_ids]
        cost_list = [costs.get(eid, 1.0) for eid in endpoint_ids]
        return solve_provider_lp(
            endpoint_ids,
            cdfs,
            cost_list,
            error_rates,
            target_cdf,
            kappa,
        )

    # Try original SLO.
    result = _try_solve(slo_sec)
    if result is not None:
        return result, "optimal"

    # Try relaxed SLOs.
    for factor in relaxation_factors:
        result = _try_solve(slo_sec * factor)
        if result is not None:
            return result, f"relaxed_{factor}x"

    # Best-effort: select provider with max CDF at original SLO.
    best_eid = max(
        endpoint_ids,
        key=lambda eid: profiles[eid].cdf_at(slo_sec, current_time),
    )
    return {best_eid: 1.0}, "best_effort"


def pre_filter_providers(
    profiles: dict[str, ProviderProfile],
    current_time: float,
    min_slo_sec: float = 1.0,
    max_error_rate: float = 0.05,
    min_cdf_at_slo: float = 0.80,
) -> list[str]:
    """Hard-filter providers that cannot meet basic requirements.

    Filtering rules:
    1. error_rate > max_error_rate -> offline (clearly broken).
    2. F(min_slo_sec) < min_cdf_at_slo -> offline (can't meet relaxed SLO).

    Args:
        profiles: endpoint_id -> ProviderProfile mapping.
        current_time: Reference time for CDF and error rate computation.
        min_slo_sec: Minimum SLO for filtering (default 1s).
        max_error_rate: Maximum allowed error rate (default 5%).
        min_cdf_at_slo: Minimum CDF at min_slo_sec (default 80%).

    Returns:
        List of eligible endpoint IDs.
    """
    eligible: list[str] = []

    for eid, profile in profiles.items():
        err = profile.error_rate(current_time)
        if err > max_error_rate:
            continue

        cdf = profile.cdf_at(min_slo_sec, current_time)
        if cdf < min_cdf_at_slo:
            continue

        eligible.append(eid)

    return eligible
