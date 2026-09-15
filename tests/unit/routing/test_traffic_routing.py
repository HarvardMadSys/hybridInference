"""Typed routing handoff and conservative traffic scheduling policy tests."""

from __future__ import annotations

from routing.prefill_load import (
    PRIORITY_ELEPHANT,
    PRIORITY_INTERACTIVE,
    PRIORITY_LARGE,
)
from routing.protocols import RoutingRequestOptions
from routing.traffic_policy import scheduling_priority_for_traffic
from serving.utils import context as req_ctx
from serving.utils.traffic_classifier import (
    TrafficEvidence,
    classification_to_metadata,
    classify_traffic,
)


def test_routing_options_carry_server_generated_traffic_signal():
    options = RoutingRequestOptions(
        traffic_classification="likely_human",
        traffic_automation_score=0.12,
        traffic_confidence=0.78,
        traffic_reasons=("client_tool", "cadence"),
    )

    assert options.traffic_classification == "likely_human"
    assert options.traffic_automation_score == 0.12
    assert options.traffic_confidence == 0.78
    assert options.traffic_reasons == ("client_tool", "cadence")


def test_only_high_confidence_human_hint_gets_interactive_priority():
    assert (
        scheduling_priority_for_traffic(
            10,
            "likely_human",
            0.70,
            interactive_priority=20,
        )
        == 11
    )
    assert (
        scheduling_priority_for_traffic(
            10,
            "likely_human",
            0.69,
            interactive_priority=20,
        )
        == 10
    )

    # Custom adjacent tiers leave no room for a bonus without crossing into
    # the interactive class.
    assert (
        scheduling_priority_for_traffic(
            19,
            "likely_human",
            0.70,
            interactive_priority=20,
        )
        == 19
    )
    assert (
        scheduling_priority_for_traffic(
            10,
            "unknown",
            1.0,
            interactive_priority=20,
        )
        == 10
    )
    assert (
        scheduling_priority_for_traffic(
            10,
            "likely_automated",
            1.0,
            interactive_priority=20,
        )
        == 10
    )
    assert (
        scheduling_priority_for_traffic(
            10,
            "likely_human",
            "not-a-confidence",  # type: ignore[arg-type]
            interactive_priority=20,
        )
        == 10
    )


def test_human_hint_cannot_bypass_prefill_cost_tier():
    """Traffic preference stays below large/interactive ordering boundaries."""
    assert (
        scheduling_priority_for_traffic(
            0,
            "likely_human",
            0.70,
            interactive_priority=PRIORITY_INTERACTIVE,
        )
        == PRIORITY_ELEPHANT
    )
    assert (
        scheduling_priority_for_traffic(
            PRIORITY_LARGE,
            "likely_human",
            0.70,
            interactive_priority=PRIORITY_INTERACTIVE,
        )
        == PRIORITY_LARGE + 1
    )
    assert (
        scheduling_priority_for_traffic(
            PRIORITY_INTERACTIVE,
            "likely_human",
            0.70,
            interactive_priority=PRIORITY_INTERACTIVE,
        )
        == PRIORITY_INTERACTIVE
    )


def test_classification_crosses_typed_routing_boundary():
    """Evidence reaches the scheduling policy through routing options."""
    classification = classify_traffic(
        TrafficEvidence(
            inter_arrival_ms=5000,
            concurrent_requests=1,
            shape_repeat_count=3,
            session_continuity=True,
            is_authenticated=True,
            request_count=5,
            user_agent="claude-code/1.0",
        )
    )
    metadata = classification_to_metadata(classification)
    options = RoutingRequestOptions(
        traffic_classification=metadata[req_ctx.TRAFFIC_CLASSIFICATION],
        traffic_automation_score=metadata[req_ctx.TRAFFIC_AUTOMATION_SCORE],
        traffic_confidence=metadata[req_ctx.TRAFFIC_CONFIDENCE],
        traffic_reasons=tuple(metadata[req_ctx.TRAFFIC_REASONS]),
    )

    assert options.traffic_classification == "likely_human"
    assert (
        scheduling_priority_for_traffic(
            10,
            options.traffic_classification,
            options.traffic_confidence,
            interactive_priority=20,
        )
        == 11
    )
