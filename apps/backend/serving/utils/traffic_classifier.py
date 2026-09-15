"""Traffic classification for routing policy.

This module provides a conservative, evidence-based classifier that estimates
the likelihood that a request represents automated/batch traffic rather than
interactive/human-driven traffic.

IMPORTANT SEMANTIC DISTINCTION:
This is traffic-pattern classification, NOT proof that a requester is or is
not human. A coding agent is automated but legitimate. A human can be abusive.
A batch job can be legitimate. This infrastructure signal describes traffic
characteristics, not moral/policy judgment.

Architecture:
- TrafficEvidence: immutable snapshot of observed request evidence
- TrafficClassifier: pure scoring function (no side effects)
- TrafficObservationState: optional bounded tracker for temporal signals

The classifier is designed so that:
- No single signal can independently produce LIKELY_AUTOMATED
- Missing evidence cannot inflate remaining signals
- UNKNOWN is preserved when independent evidence is insufficient
- Score and confidence are separate concepts
- Deterministic identical evidence produces identical output
"""

from __future__ import annotations

import enum
import hashlib
import json
import math
from dataclasses import dataclass
from typing import Any, TypeGuard

from serving.analytics.automation_score import ua_automation_value
from serving.utils import context as req_ctx


class TrafficClass(enum.Enum):
    """Coarse classification of traffic characteristics.

    UNKNOWN: insufficient evidence to classify
    LIKELY_HUMAN: traffic patterns consistent with interactive use
    LIKELY_AUTOMATED: traffic patterns consistent with batch/automation
    """

    UNKNOWN = "unknown"
    LIKELY_HUMAN = "likely_human"
    LIKELY_AUTOMATED = "likely_automated"


@dataclass(frozen=True, slots=True)
class TrafficClassification:
    """Result of traffic classification.

    Attributes:
        automation_score: 0.0 (human-like) to 1.0 (automated), bounded [0, 1]
        confidence: 0.0 (no evidence) to 1.0 (strong evidence), bounded [0, 1]
        class_hint: coarse classification derived from score and confidence
        reasons: tuple of stable reason codes explaining contributing signals
    """

    automation_score: float
    confidence: float
    class_hint: TrafficClass
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class TrafficEvidence:
    """Immutable snapshot of observed request evidence.

    All fields are optional. ``None`` means that the signal was not observed;
    explicit zero/False values are retained as observed values where they are
    meaningful.
    """

    # Inter-arrival time in milliseconds (time since last request from same identity)
    inter_arrival_ms: float | None = None

    # Current concurrent in-flight requests for this identity
    concurrent_requests: int | None = None

    # Whether this shape has been seen repeatedly from this identity
    shape_repeat_count: int | None = None

    # Session continuity: whether this request continues an existing session
    session_continuity: bool | None = None

    # Whether the request is authenticated
    is_authenticated: bool | None = None

    # User-Agent used only through the existing shared client-tool taxonomy.
    user_agent: str | None = None

    # Number of requests observed for this authenticated identity, including
    # the current request. This is confidence evidence only when paired with
    # behavioral or session evidence; count alone cannot receive a production
    # scheduling hint.
    request_count: int | None = None


# Thresholds for score → class mapping
# These are intentionally conservative to avoid false automation classification
HUMAN_THRESHOLD = 0.30
AUTOMATED_THRESHOLD = 0.70

# Minimum confidence required to classify as anything other than UNKNOWN
MIN_CONFIDENCE_FOR_CLASSIFICATION = 0.40

# Routing may give a human-like request a bounded within-tier scheduling
# preference only above this separately conservative confidence gate. Unknown
# traffic never receives the preference.
HUMAN_SCHEDULING_CONFIDENCE = 0.70

# Accumulated behavioral history becomes decision-grade after five
# observations, matching the online tracker's intentionally quick adaptation
# while preserving the offline scorer's insufficient-data caution.
HISTORY_OBSERVATION_TARGET = 5.0

# Maximum contribution from any single signal (prevents single-signal domination)
MAX_SINGLE_SIGNAL_SCORE = 0.35

# In-flight concurrency is affected by downstream service time. Keep it as a
# weak automation signal so scheduler-induced latency cannot by itself flip an
# otherwise stable human-like arrival pattern out of the scheduling cohort.
MAX_CONCURRENCY_SIGNAL_SCORE = 0.70

# Signal weights (must sum to 1.0 for proper normalization)
WEIGHT_CADENCE = 0.30
WEIGHT_CONCURRENCY = 0.25
WEIGHT_SHAPE_REPETITION = 0.25
WEIGHT_SESSION_CONTINUITY = 0.10
WEIGHT_CLIENT_TOOL = 0.10


def _clamp(value: float, min_val: float = 0.0, max_val: float = 1.0) -> float:
    """Clamp value to [min_val, max_val]."""
    return max(min_val, min(max_val, value))


def _positive_finite(value: object) -> TypeGuard[float | int]:
    """Return whether *value* is a usable positive duration."""
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value > 0
    )


def _greater_than_one(value: object) -> TypeGuard[int]:
    """Return whether *value* is a usable count greater than one."""
    return isinstance(value, int) and not isinstance(value, bool) and value > 1


def _positive_count(value: object) -> TypeGuard[int]:
    """Return whether *value* is a usable positive observation count."""
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def is_high_confidence_human_hint(
    classification: str | None,
    confidence: float | None,
) -> bool:
    """Return whether routing may apply the bounded human-like preference."""
    return (
        classification == TrafficClass.LIKELY_HUMAN.value
        and isinstance(confidence, (int, float))
        and not isinstance(confidence, bool)
        and math.isfinite(confidence)
        and confidence >= HUMAN_SCHEDULING_CONFIDENCE
    )


def _score_cadence(inter_arrival_ms: float) -> float:
    """Score request cadence. Lower inter-arrival → higher automation score.

    Human think time is typically > 2 seconds, irregular.
    Machine traffic is typically < 500ms, regular.
    """
    if inter_arrival_ms < 50:
        return 0.90
    elif inter_arrival_ms < 200:
        return 0.70
    elif inter_arrival_ms < 500:
        return 0.50
    elif inter_arrival_ms < 2000:
        return 0.30
    else:
        return 0.10


def _score_concurrency(concurrent_requests: int) -> float:
    """Score concurrency. Higher concurrency → higher automation score.

    Interactive users typically have 1-2 concurrent requests.
    Automated systems often have 5+ concurrent requests. The score is capped
    because in-flight concurrency also reflects downstream service time.
    """
    if concurrent_requests >= 20:
        score = 0.90
    elif concurrent_requests >= 10:
        score = 0.70
    elif concurrent_requests >= 5:
        score = 0.50
    elif concurrent_requests >= 3:
        score = 0.30
    else:
        score = 0.10
    return min(score, MAX_CONCURRENCY_SIGNAL_SCORE)


def _score_shape_repetition(shape_repeat_count: int) -> float:
    """Score request shape repetition. More repetition → higher automation score.

    Repeated identical request structures suggest automation.
    """
    if shape_repeat_count >= 100:
        return 0.80
    elif shape_repeat_count >= 20:
        return 0.60
    elif shape_repeat_count >= 5:
        return 0.40
    elif shape_repeat_count >= 2:
        return 0.20
    else:
        return 0.05


def _score_session_continuity(session_continuity: bool) -> float:
    """Score session continuity. Continuity suggests interactive use.

    Session continuity is a weak signal — it only reduces automation score slightly.
    """
    return 0.10 if session_continuity else 0.30


def classify_traffic(evidence: TrafficEvidence) -> TrafficClassification:
    """Classify traffic based on available evidence.

    This is a pure, side-effect-free function. It does not read global state
    or perform I/O. All evidence must be passed in.

    Scoring model:
    - Each signal contributes a score in [0, 1]
    - Each signal is weighted and summed
    - The final score is the weighted average of PRESENT signals only
    - No single signal can contribute more than MAX_SINGLE_SIGNAL_SCORE
    - Confidence is based on how many independent signals are available
    - UNKNOWN is returned when confidence is too low or score is in the middle band

    Args:
        evidence: immutable snapshot of observed request evidence

    Returns:
        TrafficClassification with score, confidence, class, and reasons
    """
    # Collect present signals with their scores and weights
    signals: list[tuple[str, float, float]] = []  # (name, score, weight)

    if _positive_finite(evidence.inter_arrival_ms):
        score = _score_cadence(evidence.inter_arrival_ms)
        signals.append(("cadence", score, WEIGHT_CADENCE))

    if _greater_than_one(evidence.concurrent_requests):
        score = _score_concurrency(evidence.concurrent_requests)
        signals.append(("concurrency", score, WEIGHT_CONCURRENCY))

    if _greater_than_one(evidence.shape_repeat_count):
        score = _score_shape_repetition(evidence.shape_repeat_count)
        signals.append(("shape_repetition", score, WEIGHT_SHAPE_REPETITION))

    if evidence.session_continuity is not None:
        signals.append(
            (
                "session_continuity",
                _score_session_evidence(evidence.session_continuity),
                WEIGHT_SESSION_CONTINUITY,
            )
        )

    if evidence.user_agent:
        signals.append(
            (
                "client_tool",
                ua_automation_value(evidence.user_agent),
                WEIGHT_CLIENT_TOOL,
            )
        )

    if not signals:
        return TrafficClassification(
            automation_score=0.0,
            confidence=0.0,
            class_hint=TrafficClass.UNKNOWN,
            reasons=("insufficient_evidence",),
        )

    # Calculate weighted score
    # Key invariant: normalize by the sum of weights of PRESENT signals
    # This prevents missing evidence from inflating the score
    total_weight = sum(weight for _, _, weight in signals)
    weighted_score = sum(score * weight for _, score, weight in signals)
    score = _clamp(weighted_score / total_weight)

    # Apply single-signal cap: no single signal can dominate
    # This ensures no single signal can independently produce LIKELY_AUTOMATED
    max_signal_contribution = max(
        signal_score * weight / total_weight for _, signal_score, weight in signals
    )
    if max_signal_contribution > MAX_SINGLE_SIGNAL_SCORE:
        # Scale down to ensure no single signal dominates
        score = _clamp(score * (MAX_SINGLE_SIGNAL_SCORE / max_signal_contribution))

    # Confidence is based on independent categories, including accumulated
    # behavioral history and longitudinal continuity. History is deliberately
    # not a multiplier: sequential interactive traffic needs a path to the
    # routing gate even when each request is single-threaded and changes shape.
    num_signals = len(signals)
    has_temporal = _positive_finite(evidence.inter_arrival_ms)
    has_volume = _greater_than_one(evidence.concurrent_requests) or _greater_than_one(
        evidence.shape_repeat_count
    )
    has_identity = evidence.is_authenticated is True
    has_history = (
        _positive_count(evidence.request_count)
        and evidence.request_count >= HISTORY_OBSERVATION_TARGET
        and (has_temporal or has_volume)
    )
    has_longitudinal_continuity = (
        _positive_count(evidence.request_count)
        and evidence.request_count >= HISTORY_OBSERVATION_TARGET
        and (
            # An explicit session is one source of longitudinal continuity.
            evidence.session_continuity is True
            # Sessionless sequential traffic has the same evidence when it
            # has accumulated history without volume-like repetition.
            or (evidence.session_continuity is None and has_temporal and not has_volume)
        )
    )

    # No category contributes more than 0.20. In particular, request count
    # alone cannot establish confidence: history requires behavioral evidence,
    # and longitudinal continuity requires either explicit session evidence or
    # mature, sequential temporal history.
    base_confidence = _clamp(
        (0.20 * has_temporal)
        + (0.20 * has_volume)
        + (0.15 * has_identity)
        + (0.20 * has_history)
        + (0.15 * has_longitudinal_continuity)
        + (0.10 * min(num_signals, 5) / 5)
    )
    # A first request may expose a user-agent and authentication context, but
    # neither is behavioral history. Keep cold-start routing metadata absent
    # until at least one temporal or volume observation exists.
    if not has_temporal and not has_volume:
        base_confidence = 0.0
    confidence = base_confidence

    # Derive class from score and confidence
    class_hint = _derive_class(score, confidence)

    # Generate stable reason codes
    reasons = _generate_reasons(evidence, signals, class_hint)

    return TrafficClassification(
        automation_score=score,
        confidence=confidence,
        class_hint=class_hint,
        reasons=tuple(reasons),
    )


def _score_session_evidence(session_continuity: bool) -> float:
    """Score session continuity evidence."""
    return 0.10 if session_continuity else 0.30


def _derive_class(score: float, confidence: float) -> TrafficClass:
    """Derive classification from score and confidence.

    Low confidence always preserves UNKNOWN regardless of score.
    """
    if confidence < MIN_CONFIDENCE_FOR_CLASSIFICATION:
        return TrafficClass.UNKNOWN
    if score <= HUMAN_THRESHOLD:
        return TrafficClass.LIKELY_HUMAN
    if score >= AUTOMATED_THRESHOLD:
        return TrafficClass.LIKELY_AUTOMATED
    return TrafficClass.UNKNOWN


def _generate_reasons(
    evidence: TrafficEvidence,
    signals: list[tuple[str, float, float]],
    class_hint: TrafficClass,
) -> list[str]:
    """Generate stable reason codes."""
    reasons: list[str] = []

    if class_hint == TrafficClass.UNKNOWN:
        if (
            not _positive_finite(evidence.inter_arrival_ms)
            and not _greater_than_one(evidence.concurrent_requests)
            and not _greater_than_one(evidence.shape_repeat_count)
        ):
            reasons.append("insufficient_evidence")
        else:
            reasons.append("mixed_evidence")

    # Add stable directional reason codes rather than raw internal signal
    # names. These remain useful in request metadata without overstating what
    # the classifier knows about the caller.
    for name, score, _ in signals:
        reason = _signal_reason(name, score, evidence)
        if reason is not None:
            reasons.append(reason)

    if not reasons:
        if class_hint == TrafficClass.LIKELY_HUMAN:
            reasons.append("human_like_pattern")
        elif class_hint == TrafficClass.LIKELY_AUTOMATED:
            reasons.append("automated_pattern")
        else:
            reasons.append("no_significant_signals")

    return reasons


def _signal_reason(
    name: str,
    score: float,
    evidence: TrafficEvidence,
) -> str | None:
    """Return a stable directional reason code for one present signal."""
    if name == "cadence":
        if score <= 0.10:
            return "slow_cadence"
        if score >= 0.50:
            return "rapid_cadence"
    elif name == "concurrency" and score >= 0.50:
        return "high_concurrency"
    elif name == "shape_repetition" and score >= 0.40:
        return "repeated_request_shape"
    elif name == "session_continuity" and evidence.session_continuity:
        return "session_continuity"
    elif name == "client_tool":
        if score <= 0.30:
            return "interactive_client"
        if score >= 0.50:
            return "automation_client"
    return None


def compute_request_shape_hash(
    model: str,
    messages_count: int,
    max_tokens: int | None,
    temperature: float | None,
) -> str:
    """Compute a non-sensitive structural fingerprint of a request.

    This hash captures request shape without inspecting prompt content.
    It is used to detect repeated identical request structures.

    Args:
        model: model identifier
        messages_count: number of messages in the request
        max_tokens: max_tokens parameter (or None)
        temperature: temperature parameter (or None)

    Returns:
        A stable hash string for the request shape
    """
    # Structural properties only — no prompt content. JSON avoids delimiter
    # ambiguity (for example, a model name containing a colon).
    shape = json.dumps(
        [model, messages_count, max_tokens, temperature],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(shape.encode("utf-8")).hexdigest()[:16]


def classification_to_metadata(classification: TrafficClassification) -> dict[str, Any]:
    """Convert classification to routing metadata dict."""
    return {
        req_ctx.TRAFFIC_CLASSIFICATION: classification.class_hint.value,
        req_ctx.TRAFFIC_AUTOMATION_SCORE: round(classification.automation_score, 3),
        req_ctx.TRAFFIC_CONFIDENCE: round(classification.confidence, 3),
        req_ctx.TRAFFIC_REASONS: list(classification.reasons),
    }
