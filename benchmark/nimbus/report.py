"""Latency and accounting summaries with explicit failure and measurement coverage."""

from __future__ import annotations

import math
from collections import Counter
from decimal import Decimal
from typing import Any


def percentiles(values: list[float]) -> dict[str, float | int | None]:
    """Return linearly interpolated empirical quantiles, including empty coverage."""
    ordered = sorted(v for v in values if math.isfinite(v))
    result: dict[str, float | int | None] = {"count": len(ordered)}
    for quantile in (50, 90, 95, 99):
        if not ordered:
            result[f"p{quantile}"] = None
            continue
        index = (len(ordered) - 1) * quantile / 100
        lower, upper = math.floor(index), math.ceil(index)
        result[f"p{quantile}"] = ordered[lower] + (ordered[upper] - ordered[lower]) * (
            index - lower
        )
    result["max"] = ordered[-1] if ordered else None
    return result


def summarize(records: list[dict[str, Any]], slo: dict[str, float]) -> dict[str, Any]:
    """Keep errors in request denominators and undefined TPOT visible."""
    count = len(records)
    success = [r for r in records if r["status"] == "success"]
    satisfied = [r for r in records if r.get("joint_slo_satisfied") is True]
    known_cost = sum((Decimal(r.get("cost_cny_estimated") or "0") for r in records), Decimal(0))
    unresolved = sum(
        (Decimal(r.get("unresolved_reserved_cny") or "0") for r in records), Decimal(0)
    )
    result: dict[str, Any] = {
        "requests": count,
        "successful_requests": len(success),
        "failed_requests": count - len(success),
        "statuses": dict(Counter(r["status"] for r in records)),
        "routes": dict(Counter(r["route"] for r in records)),
        "truncated_requests": sum(bool(r.get("truncated")) for r in records),
        "authoritative_usage_requests": sum(bool(r.get("usage_authoritative")) for r in records),
        "joint_slo_satisfied_requests": len(satisfied),
        "joint_slo_satisfied_fraction_all_requests": len(satisfied) / count if count else None,
        "joint_slo_unsatisfied_or_unknown_requests": count - len(satisfied),
        "joint_slo_unknown_requests": sum(r.get("joint_slo_satisfied") is None for r in records),
        "slo": slo,
        "cost_cny_estimated": str(known_cost),
        "cost_complete": not any(
            r.get("budget_status") in {"reserved", "dispatched", "unknown"} for r in records
        ),
        "unresolved_reserved_cny": str(unresolved),
        "conservative_cost_upper_bound_cny": str(known_cost + unresolved),
        "cost_basis": "configured_rate_estimate_not_invoice",
        "cloud_unknown_usage_requests": sum(
            r["route"] == "cloud" and r.get("budget_status") == "unknown" for r in records
        ),
        "latency_population": "all attempts with measured values, including failed attempts",
        "tpot_definition": "(end_s-first_usable_s)/(authoritative_completion_tokens-1)",
        "output_span_tpot_definition": "(last_usable_s-first_usable_s)/(authoritative_completion_tokens-1)",
        "measurement_note": (
            "TPOT is a request average, not per-token ITL; primary TPOT includes trailing "
            "finish/usage-frame overhead. Chunk coalescing is observable but token timing is not."
        ),
        "tpot_undefined_reasons": dict(
            Counter(r.get("tpot_undefined_reason") for r in records if r.get("tpot_s") is None)
        ),
        "single_usable_chunk_requests": sum(r.get("usable_chunks") == 1 for r in records),
    }
    for metric in ("ttft_s", "tpot_s", "output_span_tpot_s", "e2e_s", "queue_s", "service_s"):
        result[metric] = percentiles([r[metric] for r in records if r.get(metric) is not None])
    for metric in ("prompt_tokens", "completion_tokens", "cache_tokens_priced"):
        result[metric] = percentiles([r[metric] for r in records if r.get(metric) is not None])
    for metric in ("ttft_s", "tpot_s"):
        result[f"{metric}_violations"] = sum(
            r.get(metric) is not None and r[metric] > slo[metric] for r in records
        )
        result[f"{metric}_unknown"] = sum(r.get(metric) is None for r in records)
    result["successful_latency"] = {
        metric: percentiles([r[metric] for r in success if r.get(metric) is not None])
        for metric in ("ttft_s", "tpot_s", "e2e_s")
    }
    result["by_route"] = {
        route: {
            "requests": sum(r["route"] == route for r in records),
            **{
                metric: percentiles(
                    [
                        r[metric]
                        for r in records
                        if r["route"] == route and r.get(metric) is not None
                    ]
                )
                for metric in ("ttft_s", "tpot_s", "e2e_s")
            },
        }
        for route in sorted({r["route"] for r in records})
    }
    return result
