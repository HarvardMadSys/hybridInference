"""Shared vocabulary used by alert producers and the ingestion contract."""

from enum import StrEnum


class DecisionReason(StrEnum):
    """Reasons a notification decision can be recorded in history."""

    COOLDOWN = "cooldown"
    TYPE_MUTED = "type_muted"
    SNOOZED = "snoozed"
    IN_FLIGHT = "in_flight"
    NO_CHANNEL = "no_channel"
    CHANNEL_CONFIG_INVALID = "channel_config_invalid"
    POLICY_DISABLED = "policy_disabled"
    RATE_LIMITED = "rate_limited"
    RECORD_ONLY = "record_only"
    ALREADY_NOTIFIED = "already_notified"
    BELOW_MIN_DURATION = "below_min_duration"
    RESOLUTION_SILENCED = "resolution_silenced"
    SOURCE_SUPPRESSED = "source_suppressed"
    OTHER = "other"


def decision_reason(value: str) -> DecisionReason:
    """Validate a producer decision reason against the wire vocabulary."""
    try:
        return DecisionReason(value)
    except ValueError as exc:
        raise ValueError(f"unknown alert decision reason: {value!r}") from exc
