"""Conservative routing policies driven by online traffic evidence."""

from __future__ import annotations

from routing.prefill_load import PRIORITY_ELEPHANT
from serving.utils.traffic_classifier import is_high_confidence_human_hint


def scheduling_priority_for_traffic(
    base_priority: int,
    traffic_classification: str | None,
    traffic_confidence: float | None,
    *,
    interactive_priority: int,
) -> int:
    """Apply the online traffic hint to an existing scheduling priority.

    Only a high-confidence human-like result can receive a one-point preference
    within the existing cost tier. Unknown, automated, malformed, or
    low-confidence hints retain the caller-independent base priority.

    Prefill cost remains the hard ordering constraint. In particular, an
    elephant request can never be promoted to the interactive priority merely
    because its traffic pattern looks human-like. ``interactive_priority`` is
    the exclusive boundary for lower-tier bonuses; already-interactive work
    remains at ``interactive_priority`` itself.
    """
    if is_high_confidence_human_hint(traffic_classification, traffic_confidence):
        if base_priority == PRIORITY_ELEPHANT:
            return base_priority
        if base_priority < interactive_priority:
            # A one-point bonus must remain below the interactive tier even
            # when operators configure adjacent priority values.
            return min(base_priority + 1, interactive_priority - 1)
    return base_priority
