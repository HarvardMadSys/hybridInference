"""Synthetic evaluation harness for traffic classification.

This is NOT a claim of real-world accuracy. It provides regression evidence
and behavioral sanity checking for the classifier.
"""

from __future__ import annotations

import statistics

from serving.utils.traffic_classifier import (
    TrafficEvidence,
    classify_traffic,
)


def evaluate_scenario(
    name: str,
    *,
    concurrent_requests: int | None = None,
    inter_arrival_ms: float | None = None,
    shape_repeat_count: int | None = None,
    session_continuity: bool | None = None,
    is_authenticated: bool | None = None,
    user_agent: str | None = None,
    request_count: int | None = None,
) -> dict[str, str | float | list[str]]:
    """Evaluate a traffic scenario and return results."""
    classification = classify_traffic(
        TrafficEvidence(
            concurrent_requests=concurrent_requests,
            inter_arrival_ms=inter_arrival_ms,
            shape_repeat_count=shape_repeat_count,
            session_continuity=session_continuity,
            is_authenticated=is_authenticated,
            user_agent=user_agent,
            request_count=request_count,
        )
    )

    return {
        "scenario": name,
        "class": classification.class_hint.value,
        "automation_score": classification.automation_score,
        "confidence": classification.confidence,
        "reasons": list(classification.reasons),
    }


def run_synthetic_evaluation() -> list[dict[str, str | float | list[str]]]:
    """Run synthetic evaluation across representative scenarios."""
    results = []

    # Interactive: variable think time, low concurrency, irregular cadence
    results.append(
        evaluate_scenario(
            "interactive",
            concurrent_requests=1,
            inter_arrival_ms=5000,
            shape_repeat_count=3,
            session_continuity=True,
            is_authenticated=True,
            user_agent="browser/1.0",
            request_count=5,
        )
    )

    # Agentic/IDE: human-triggered but automated multi-call
    results.append(
        evaluate_scenario(
            "agentic_ide",
            concurrent_requests=3,
            inter_arrival_ms=500,
            shape_repeat_count=20,
            session_continuity=True,
            is_authenticated=True,
            user_agent="claude-code/1.0",
            request_count=5,
        )
    )

    # Batch: stable/high-volume machine traffic
    results.append(
        evaluate_scenario(
            "batch",
            concurrent_requests=20,
            inter_arrival_ms=50,
            shape_repeat_count=200,
            is_authenticated=True,
            user_agent="curl/8.0",
            request_count=5,
        )
    )

    # Burst: short intense traffic then inactivity
    results.append(
        evaluate_scenario(
            "burst",
            concurrent_requests=10,
            inter_arrival_ms=100,
            shape_repeat_count=100,
            is_authenticated=True,
            user_agent="python-requests/2.0",
            request_count=5,
        )
    )

    # Shared-network human: multiple identities, one origin
    results.append(
        evaluate_scenario(
            "shared_network_human",
            concurrent_requests=1,
            inter_arrival_ms=8000,
            shape_repeat_count=3,
            session_continuity=True,
            is_authenticated=True,
            user_agent="browser/1.0",
            request_count=5,
        )
    )

    # Distributed automation: one identity, multiple origins
    results.append(
        evaluate_scenario(
            "distributed_automation",
            concurrent_requests=15,
            inter_arrival_ms=200,
            shape_repeat_count=500,
            is_authenticated=True,
            user_agent="httpx/0.27",
            request_count=5,
        )
    )

    # Cold start: no history
    results.append(
        evaluate_scenario(
            "cold_start",
            inter_arrival_ms=None,
        )
    )

    return results


def test_synthetic_evaluation_uses_classifier_contract() -> None:
    """The evaluation harness stays executable as the classifier evolves."""
    results = run_synthetic_evaluation()

    assert len(results) == 7
    assert {result["scenario"] for result in results} == {
        "interactive",
        "agentic_ide",
        "batch",
        "burst",
        "shared_network_human",
        "distributed_automation",
        "cold_start",
    }
    cold_start = next(result for result in results if result["scenario"] == "cold_start")
    assert cold_start["class"] == "unknown"
    expected_classes = {
        "interactive": "likely_human",
        "agentic_ide": "unknown",
        "batch": "likely_automated",
        "burst": "likely_automated",
        "shared_network_human": "likely_human",
        "distributed_automation": "unknown",
        "cold_start": "unknown",
    }
    assert {result["scenario"]: result["class"] for result in results} == expected_classes

    # Keep broad score bands as regression guards without coupling CI to
    # floating-point implementation details.
    for result in results:
        assert 0.0 <= result["automation_score"] <= 1.0
        assert 0.0 <= result["confidence"] <= 1.0

    agentic = next(result for result in results if result["scenario"] == "agentic_ide")
    assert 0.25 <= agentic["automation_score"] <= 0.45
    # Agentic traffic deliberately gets strong classification confidence, but
    # its mixed cadence/concurrency/repetition evidence keeps the class UNKNOWN.
    assert 0.7 <= agentic["confidence"] <= 1.0


def print_evaluation_report(results: list[dict[str, str | float | list[str]]]) -> None:
    """Print evaluation report."""
    print("Traffic Classification Synthetic Evaluation")
    print("=" * 60)
    print("NOTE: This is behavioral sanity checking, NOT real-world accuracy.")
    print()

    for r in results:
        print(f"Scenario: {r['scenario']}")
        print(f"  Class: {r['class']}")
        print(f"  Automation Score: {r['automation_score']:.3f}")
        print(f"  Confidence: {r['confidence']:.3f}")
        print(f"  Reasons: {r['reasons']}")
        print()

    # Summary statistics
    scores = [r["automation_score"] for r in results]
    print(f"Score range: [{min(scores):.3f}, {max(scores):.3f}]")
    if len(scores) > 1:
        print(f"Mean score: {statistics.mean(scores):.3f}")
        print(f"Score stdev: {statistics.stdev(scores):.3f}")


if __name__ == "__main__":
    results = run_synthetic_evaluation()
    print_evaluation_report(results)
