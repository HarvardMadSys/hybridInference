"""Tests for traffic classification.

Tests cover cold-start, single-signal non-domination, mixed evidence,
determinism, and privacy/safety boundaries.
"""

from __future__ import annotations

import pytest

from serving.utils.traffic_classifier import (
    AUTOMATED_THRESHOLD,
    TrafficClass,
    TrafficClassification,
    TrafficEvidence,
    classification_to_metadata,
    classify_traffic,
    compute_request_shape_hash,
    is_high_confidence_human_hint,
)
from serving.utils.traffic_state import (
    TrafficObservationState,
)

# ---------------------------------------------------------------------------
# Cold start
# ---------------------------------------------------------------------------


def test_cold_start_unknown():
    """Insufficient evidence produces UNKNOWN."""
    evidence = TrafficEvidence()
    classification = classify_traffic(evidence)
    assert classification.class_hint == TrafficClass.UNKNOWN
    assert classification.confidence < 0.4


def test_minimal_evidence_unknown():
    """Minimal evidence (only authentication) produces UNKNOWN."""
    evidence = TrafficEvidence(is_authenticated=True, request_count=1)
    classification = classify_traffic(evidence)
    assert classification.class_hint == TrafficClass.UNKNOWN


def test_missing_evidence_is_not_treated_as_observed_zero():
    """Omitted optional signals do not alter the score or confidence."""
    classification = classify_traffic(TrafficEvidence())

    assert classification.automation_score == 0.0
    assert classification.confidence == 0.0
    assert classification.class_hint == TrafficClass.UNKNOWN


# ---------------------------------------------------------------------------
# Single-signal non-domination
# ---------------------------------------------------------------------------


def test_cadence_only_cannot_classify_automated():
    """Cadence alone cannot produce LIKELY_AUTOMATED."""
    # Very fast cadence (50ms)
    evidence = TrafficEvidence(inter_arrival_ms=50)
    classification = classify_traffic(evidence)
    assert classification.class_hint != TrafficClass.LIKELY_AUTOMATED


def test_concurrency_only_cannot_classify_automated():
    """Concurrency alone cannot produce LIKELY_AUTOMATED."""
    # High concurrency (20 requests)
    evidence = TrafficEvidence(concurrent_requests=20)
    classification = classify_traffic(evidence)
    assert classification.class_hint != TrafficClass.LIKELY_AUTOMATED


def test_shape_repetition_only_cannot_classify_automated():
    """Shape repetition alone cannot produce LIKELY_AUTOMATED."""
    # High shape repetition
    evidence = TrafficEvidence(shape_repeat_count=200)
    classification = classify_traffic(evidence)
    assert classification.class_hint != TrafficClass.LIKELY_AUTOMATED


def test_each_single_signal_stays_below_automated_threshold():
    """The contribution cap is explicit for every individually strong signal."""
    cases = (
        TrafficEvidence(inter_arrival_ms=1),
        TrafficEvidence(concurrent_requests=1000),
        TrafficEvidence(shape_repeat_count=10000),
    )

    for evidence in cases:
        classification = classify_traffic(evidence)
        assert classification.automation_score < AUTOMATED_THRESHOLD
        assert classification.class_hint != TrafficClass.LIKELY_AUTOMATED


def test_identity_only_cannot_classify_automated():
    """Identity alone cannot produce LIKELY_AUTOMATED."""
    evidence = TrafficEvidence(is_authenticated=True, request_count=1)
    classification = classify_traffic(evidence)
    assert classification.class_hint != TrafficClass.LIKELY_AUTOMATED


# ---------------------------------------------------------------------------
# Mixed evidence
# ---------------------------------------------------------------------------


def test_mixed_strong_automation_evidence():
    """Multiple strong automation signals can classify automated."""
    evidence = TrafficEvidence(
        inter_arrival_ms=50,  # Very fast
        concurrent_requests=20,  # High concurrency
        shape_repeat_count=200,  # High repetition
        session_continuity=False,
        is_authenticated=False,
        request_count=5,
    )
    classification = classify_traffic(evidence)
    assert classification.automation_score > 0.5


def test_interactive_traffic_not_automated():
    """Interactive-like traffic should not be classified as automated."""
    evidence = TrafficEvidence(
        inter_arrival_ms=5000,  # 5 seconds between requests
        concurrent_requests=1,  # Single request
        shape_repeat_count=0,  # No repetition
        session_continuity=True,  # Continuing session
        is_authenticated=True,
        request_count=5,
    )
    classification = classify_traffic(evidence)
    assert classification.class_hint != TrafficClass.LIKELY_AUTOMATED


def test_two_observations_cannot_receive_human_scheduling_hint():
    """A short history may be human-like but is not decision-grade evidence."""
    classification = classify_traffic(
        TrafficEvidence(
            inter_arrival_ms=5000,
            shape_repeat_count=2,
            session_continuity=True,
            is_authenticated=True,
            user_agent="browser/1.0",
            request_count=2,
        )
    )

    assert classification.class_hint in (
        TrafficClass.UNKNOWN,
        TrafficClass.LIKELY_HUMAN,
    )
    assert classification.confidence < 0.70
    assert not is_high_confidence_human_hint(
        classification.class_hint.value, classification.confidence
    )


def test_five_observations_can_receive_human_scheduling_hint():
    """A consistent five-request history can satisfy the confidence gate."""
    classification = classify_traffic(
        TrafficEvidence(
            inter_arrival_ms=5000,
            shape_repeat_count=5,
            session_continuity=True,
            is_authenticated=True,
            user_agent="browser/1.0",
            request_count=5,
        )
    )

    assert classification.class_hint == TrafficClass.LIKELY_HUMAN
    assert classification.confidence >= 0.70
    assert is_high_confidence_human_hint(classification.class_hint.value, classification.confidence)


def test_sequential_interactive_history_can_reach_human_scheduling_hint():
    """Explicit session continuity can satisfy the confidence gate."""
    classification = classify_traffic(
        TrafficEvidence(
            inter_arrival_ms=5000,
            concurrent_requests=1,
            shape_repeat_count=1,
            session_continuity=True,
            is_authenticated=True,
            user_agent="browser/1.0",
            request_count=5,
        )
    )

    assert classification.class_hint == TrafficClass.LIKELY_HUMAN
    assert classification.confidence >= 0.70
    assert is_high_confidence_human_hint(classification.class_hint.value, classification.confidence)


@pytest.mark.parametrize("user_agent", ["OpenAI Python/1.0", "claude-code/1.0"])
def test_sessionless_sequential_history_can_reach_human_scheduling_hint(user_agent):
    """Normal authenticated multi-turn clients need no custom session ID."""
    classification = classify_traffic(
        TrafficEvidence(
            inter_arrival_ms=5000,
            concurrent_requests=1,
            shape_repeat_count=1,
            session_continuity=None,
            is_authenticated=True,
            user_agent=user_agent,
            request_count=5,
        )
    )

    assert classification.class_hint == TrafficClass.LIKELY_HUMAN
    assert is_high_confidence_human_hint(classification.class_hint.value, classification.confidence)


def test_sessionless_changing_shape_history_reaches_preference_gate():
    """The bounded tracker infers continuity from normal changing chat turns."""
    now = [100.0]
    state = TrafficObservationState(clock=lambda: now[0])
    classifications = []

    for turn in range(5):
        observation = state.record_request(
            user_id="authenticated-user",
            shape_hash=f"chat-turn-{turn}",
        )
        classifications.append(
            classify_traffic(
                TrafficEvidence(
                    inter_arrival_ms=observation["inter_arrival_ms"],
                    concurrent_requests=1,
                    shape_repeat_count=observation["shape_repeat_count"],
                    session_continuity=observation["session_continuity"],
                    is_authenticated=True,
                    user_agent="OpenAI Python/1.0",
                    request_count=observation["request_count"],
                )
            )
        )
        now[0] += 5.0

    assert all(
        not is_high_confidence_human_hint(c.class_hint.value, c.confidence)
        for c in classifications[:4]
    )
    assert is_high_confidence_human_hint(
        classifications[-1].class_hint.value,
        classifications[-1].confidence,
    )


def test_sessionless_sequential_history_stays_cold_until_mature():
    """A few sequential requests do not receive a scheduling preference."""
    classification = classify_traffic(
        TrafficEvidence(
            inter_arrival_ms=5000,
            concurrent_requests=1,
            shape_repeat_count=1,
            is_authenticated=True,
            user_agent="OpenAI Python/1.0",
            request_count=4,
        )
    )

    assert classification.class_hint in (TrafficClass.UNKNOWN, TrafficClass.LIKELY_HUMAN)
    assert not is_high_confidence_human_hint(
        classification.class_hint.value, classification.confidence
    )


def test_sessionless_history_does_not_override_machine_like_cadence():
    """Accumulated count cannot turn high-rate automation into human traffic."""
    classification = classify_traffic(
        TrafficEvidence(
            inter_arrival_ms=50,
            concurrent_requests=1,
            shape_repeat_count=1,
            session_continuity=None,
            is_authenticated=True,
            user_agent="curl/8.0",
            request_count=100,
        )
    )

    assert classification.class_hint != TrafficClass.LIKELY_HUMAN
    assert not is_high_confidence_human_hint(
        classification.class_hint.value, classification.confidence
    )


def test_request_count_alone_cannot_establish_confidence():
    """A large count without behavioral evidence remains cold."""
    classification = classify_traffic(TrafficEvidence(is_authenticated=True, request_count=100))

    assert classification.confidence == 0.0
    assert classification.class_hint == TrafficClass.UNKNOWN


def test_service_time_cannot_flip_human_hint_via_concurrency():
    """Downstream latency cannot turn stable human traffic into automation."""
    common_evidence = {
        "inter_arrival_ms": 5000,
        "shape_repeat_count": 2,
        "session_continuity": True,
        "is_authenticated": True,
        "user_agent": "browser/1.0",
        "request_count": 5,
    }

    classifications = [
        classify_traffic(TrafficEvidence(concurrent_requests=concurrency, **common_evidence))
        for concurrency in (1, 20)
    ]

    assert all(
        classification.class_hint == TrafficClass.LIKELY_HUMAN for classification in classifications
    )
    assert all(
        is_high_confidence_human_hint(classification.class_hint.value, classification.confidence)
        for classification in classifications
    )


def test_conflicting_evidence_unknown():
    """Conflicting signals should result in UNKNOWN."""
    evidence = TrafficEvidence(
        inter_arrival_ms=50,  # Very fast (automated)
        concurrent_requests=1,  # But low concurrency (human)
        shape_repeat_count=0,  # No repetition (human)
        session_continuity=True,  # Session continuity (human)
        is_authenticated=True,
        request_count=5,
    )
    classification = classify_traffic(evidence)
    # Should be uncertain due to conflicting signals
    assert classification.confidence < 0.6 or classification.class_hint == TrafficClass.UNKNOWN


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_determinism():
    """Identical evidence produces identical classification."""
    evidence = TrafficEvidence(
        inter_arrival_ms=100,
        concurrent_requests=5,
        shape_repeat_count=10,
        session_continuity=True,
        is_authenticated=True,
        request_count=5,
    )
    c1 = classify_traffic(evidence)
    c2 = classify_traffic(evidence)
    assert c1.automation_score == c2.automation_score
    assert c1.confidence == c2.confidence
    assert c1.class_hint == c2.class_hint
    assert c1.reasons == c2.reasons


# ---------------------------------------------------------------------------
# Score bounds
# ---------------------------------------------------------------------------


def test_score_bounded():
    """Automation score is always in [0, 1]."""
    evidence = TrafficEvidence(
        inter_arrival_ms=1,
        concurrent_requests=1000,
        shape_repeat_count=10000,
    )
    classification = classify_traffic(evidence)
    assert 0.0 <= classification.automation_score <= 1.0
    assert 0.0 <= classification.confidence <= 1.0


def test_invalid_inter_arrival():
    """Zero or negative inter-arrival is treated as missing."""
    evidence = TrafficEvidence(inter_arrival_ms=0)
    classification = classify_traffic(evidence)
    # Should not crash, should treat as missing
    assert classification.class_hint == TrafficClass.UNKNOWN


def test_negative_inter_arrival():
    """Negative inter-arrival is treated as missing."""
    evidence = TrafficEvidence(inter_arrival_ms=-100)
    classification = classify_traffic(evidence)
    assert classification.class_hint == TrafficClass.UNKNOWN


# ---------------------------------------------------------------------------
# Metadata conversion
# ---------------------------------------------------------------------------


def test_classification_to_metadata():
    """Classification converts to metadata dict."""
    classification = TrafficClassification(
        automation_score=0.75,
        confidence=0.8,
        class_hint=TrafficClass.LIKELY_AUTOMATED,
        reasons=("cadence", "concurrency"),
    )
    metadata = classification_to_metadata(classification)
    assert metadata["traffic_classification"] == "likely_automated"
    assert metadata["traffic_automation_score"] == 0.75
    assert metadata["traffic_confidence"] == 0.8
    assert "cadence" in metadata["traffic_reasons"]


# ---------------------------------------------------------------------------
# Request shape hash
# ---------------------------------------------------------------------------


def test_shape_hash_deterministic():
    """Same inputs produce same hash."""
    h1 = compute_request_shape_hash("gpt-4", 5, 100, 0.7)
    h2 = compute_request_shape_hash("gpt-4", 5, 100, 0.7)
    assert h1 == h2


def test_shape_hash_different_inputs():
    """Different inputs produce different hashes."""
    h1 = compute_request_shape_hash("gpt-4", 5, 100, 0.7)
    h2 = compute_request_shape_hash("gpt-4", 5, 200, 0.7)
    assert h1 != h2


def test_shape_hash_no_content():
    """Shape hash does not inspect prompt content."""
    # Same structure, different content → same hash
    h1 = compute_request_shape_hash("gpt-4", 3, 100, None)
    h2 = compute_request_shape_hash("gpt-4", 3, 100, None)
    assert h1 == h2


def test_shape_hash_handles_lone_surrogate():
    """Malformed-but-decodable model identifiers remain deterministic and distinct."""
    first = compute_request_shape_hash("\ud800", 1, 1, None)
    second = compute_request_shape_hash("\ud800", 1, 1, None)
    other = compute_request_shape_hash("\ud801", 1, 1, None)

    assert first == second
    assert first != other


# ---------------------------------------------------------------------------
# Traffic state
# ---------------------------------------------------------------------------


def test_traffic_state_bounded():
    """Traffic state respects max identities."""
    state = TrafficObservationState(max_identities=10)
    for i in range(20):
        state.record_request(
            user_id=f"user_{i}",
        )
    assert state.get_identity_count() <= 10


def test_traffic_state_ttl_eviction():
    """Old entries are evicted after TTL."""
    now = [100.0]
    state = TrafficObservationState(ttl_seconds=10.0, clock=lambda: now[0])
    state.record_request(user_id="user_1")
    assert state.get_identity_count() == 1
    now[0] += 10.0
    state.expire_old_entries()
    assert state.get_identity_count() == 0


def test_traffic_state_inter_arrival():
    """Inter-arrival time is calculated correctly."""
    now = [100.0]
    state = TrafficObservationState(clock=lambda: now[0])
    result1 = state.record_request(user_id="user_1")
    assert result1["inter_arrival_ms"] is None  # First request
    now[0] += 0.05
    result2 = state.record_request(user_id="user_1")
    assert result2["inter_arrival_ms"] is not None
    assert result2["inter_arrival_ms"] == pytest.approx(50.0)


def test_traffic_state_shape_repetition():
    """Shape repetition is tracked."""
    state = TrafficObservationState()
    state.record_request(
        user_id="user_1",
        shape_hash="abc123",
    )
    result = state.record_request(
        user_id="user_1",
        shape_hash="abc123",
    )
    assert result["shape_repeat_count"] == 2


def test_traffic_state_preview_does_not_commit_history():
    """Pre-dispatch classification evidence is not durable until committed."""
    now = [100.0]
    state = TrafficObservationState(clock=lambda: now[0])
    state.record_request(user_id="user_1", shape_hash="abc123", session_id="session")
    now[0] += 0.05

    preview = state.preview_request(
        user_id="user_1",
        shape_hash="abc123",
        session_id="session",
    )
    assert preview["request_count"] == 2
    assert preview["shape_repeat_count"] == 2
    assert preview["session_continuity"] is True

    committed = state.record_request(
        user_id="user_1",
        shape_hash="abc123",
        session_id="session",
    )
    assert committed == preview


def test_traffic_state_commit_preserves_preview_timestamp():
    """Post-dispatch commits retain arrival time instead of completion time."""
    now = [100.0]
    state = TrafficObservationState(clock=lambda: now[0])
    state.record_request(user_id="user_1")

    # The tracker clock has advanced during gateway preflight, but the router
    # captured this request's arrival at 110.0 before those awaits.
    now[0] = 210.0
    preview = state.preview_request(user_id="user_1", observed_at=110.0)

    # Simulate a slow upstream response before dispatch admission is confirmed.
    committed = state.record_request(
        user_id="user_1",
        observed_at=float(preview["observed_at"]),
    )

    assert committed["observed_at"] == 110.0
    assert committed["inter_arrival_ms"] == pytest.approx(10_000.0)


def test_traffic_state_backdated_observation_preserves_latest_session():
    """A late commit cannot replace the session from a newer arrival."""
    now = [100.0]
    state = TrafficObservationState(clock=lambda: now[0])
    state.record_request(user_id="user_1", session_id="new-session", observed_at=100.0)

    backdated = state.record_request(
        user_id="user_1",
        session_id="old-session",
        observed_at=90.0,
    )
    assert backdated["session_continuity"] is False

    latest = state.record_request(
        user_id="user_1",
        session_id="new-session",
        observed_at=110.0,
    )
    assert latest["session_continuity"] is True


def test_traffic_state_sessionless_request_breaks_explicit_continuity():
    """A sessionless turn clears the prior explicit session marker."""
    state = TrafficObservationState(clock=lambda: 100.0)
    state.record_request(user_id="user_1", session_id="session-a")
    sessionless = state.record_request(user_id="user_1")
    assert sessionless["session_continuity"] is None

    resumed = state.record_request(user_id="user_1", session_id="session-a")
    assert resumed["session_continuity"] is None


def test_traffic_state_bystander_isolation():
    """Two authenticated users are tracked separately."""
    state = TrafficObservationState()
    state.record_request(user_id="user_1")
    state.record_request(user_id="user_2")
    assert state.get_identity_count() == 2


def test_traffic_state_does_not_bucket_auth_disabled_callers():
    """Auth-disabled callers without a user identity are never grouped."""
    state = TrafficObservationState()

    first = state.record_request(
        user_id=None,
        shape_hash="same-shape",
    )
    second = state.record_request(
        user_id=None,
        shape_hash="same-shape",
    )

    assert first["tracked"] is False
    assert second["tracked"] is False
    assert first["inter_arrival_ms"] is None
    assert second["shape_repeat_count"] is None
    assert state.get_identity_count() == 0


def test_traffic_state_session_continuity():
    """Session continuity is detected."""
    state = TrafficObservationState()
    state.record_request(
        user_id="user_1",
        session_id="session_abc",
    )
    result = state.record_request(
        user_id="user_1",
        session_id="session_abc",
    )
    assert result["session_continuity"] is True


def test_traffic_state_hashes_lone_surrogate_session_deterministically():
    """Malformed-but-decodable JSON session text must not become a 500."""
    state = TrafficObservationState()
    session_id = "session-\ud800"

    state.record_request(user_id="user_1", session_id=session_id)
    result = state.record_request(user_id="user_1", session_id=session_id)

    assert result["session_continuity"] is True


def test_traffic_state_reset():
    """Reset clears all state."""
    state = TrafficObservationState()
    state.record_request(user_id="user_1")
    state.reset()
    assert state.get_identity_count() == 0


def test_traffic_state_lru_refreshes_on_access():
    """A recently used identity is retained when capacity forces eviction."""
    now = [100.0]
    state = TrafficObservationState(max_identities=2, clock=lambda: now[0])
    state.record_request(user_id="user_a")
    state.record_request(user_id="user_b")
    state.record_request(user_id="user_a")
    state.record_request(user_id="user_c")

    result = state.record_request(user_id="user_a")
    assert result["inter_arrival_ms"] == 0.0

    result = state.record_request(user_id="user_b")
    assert result["inter_arrival_ms"] is None


def test_traffic_state_shape_lru_refreshes_on_access():
    """Shape eviction retains recently repeated request structures."""
    state = TrafficObservationState(max_shapes_per_identity=2)
    kwargs = {"user_id": "user_1"}
    state.record_request(shape_hash="shape_a", **kwargs)
    state.record_request(shape_hash="shape_b", **kwargs)
    state.record_request(shape_hash="shape_a", **kwargs)
    state.record_request(shape_hash="shape_c", **kwargs)

    result = state.record_request(shape_hash="shape_a", **kwargs)
    assert result["shape_repeat_count"] == 3
    result = state.record_request(shape_hash="shape_b", **kwargs)
    assert result["shape_repeat_count"] == 1


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"max_identities": 0}, "max_identities"),
        ({"max_shapes_per_identity": 0}, "max_shapes_per_identity"),
        ({"ttl_seconds": 0}, "ttl_seconds"),
        ({"ttl_seconds": float("inf")}, "ttl_seconds"),
    ],
)
def test_traffic_state_rejects_invalid_limits(kwargs, message):
    """Invalid bounds fail at construction instead of degrading silently."""
    with pytest.raises(ValueError, match=message):
        TrafficObservationState(**kwargs)


# ---------------------------------------------------------------------------
# Trusted vs untrusted proxy identity
# ---------------------------------------------------------------------------


def test_authenticated_identity_is_not_an_automation_signal():
    """Authentication is identity context, not an automation signal."""
    evidence = TrafficEvidence(
        inter_arrival_ms=5000,
        is_authenticated=True,
        request_count=5,
    )
    classification = classify_traffic(evidence)
    # Trusted provenance should not increase automation score
    assert classification.automation_score < 0.5


def test_anonymous_identity_is_not_an_automation_signal():
    """Anonymous identity context is omitted rather than scored."""
    evidence = TrafficEvidence(
        inter_arrival_ms=5000,
        is_authenticated=None,
    )
    classification = classify_traffic(evidence)
    assert "provenance" not in classification.reasons
    assert classification.class_hint == TrafficClass.UNKNOWN


def test_existing_client_taxonomy_treats_coding_agents_as_human_like():
    """Online scoring agrees with the existing analytics client taxonomy."""
    evidence = TrafficEvidence(
        inter_arrival_ms=500,
        concurrent_requests=5,
        shape_repeat_count=20,
        session_continuity=True,
        is_authenticated=True,
        user_agent="claude-code/1.0",
        request_count=5,
    )
    classification = classify_traffic(evidence)

    assert classification.automation_score < 0.5


def test_script_client_taxonomy_contributes_automation_signal():
    """Raw HTTP clients use the shared analytics script prior."""
    coding_agent = classify_traffic(
        TrafficEvidence(
            inter_arrival_ms=50,
            concurrent_requests=20,
            shape_repeat_count=200,
            user_agent="curl/8.0",
            request_count=5,
        )
    )
    without_user_agent = classify_traffic(
        TrafficEvidence(
            inter_arrival_ms=50,
            concurrent_requests=20,
            shape_repeat_count=200,
            request_count=5,
        )
    )

    assert coding_agent.automation_score > without_user_agent.automation_score


# ---------------------------------------------------------------------------
# Reason codes
# ---------------------------------------------------------------------------


def test_reason_codes_present():
    """Reason codes are present for classified traffic."""
    evidence = TrafficEvidence(
        inter_arrival_ms=50,
        concurrent_requests=20,
        shape_repeat_count=200,
    )
    classification = classify_traffic(evidence)
    assert len(classification.reasons) > 0


def test_human_reason_codes_are_directional():
    """Human-like results explain the human-direction evidence."""
    classification = classify_traffic(
        TrafficEvidence(
            inter_arrival_ms=5000,
            session_continuity=True,
            is_authenticated=True,
            user_agent="browser/1.0",
            request_count=5,
        )
    )

    assert classification.class_hint == TrafficClass.LIKELY_HUMAN
    assert {"slow_cadence", "session_continuity", "interactive_client"} <= set(
        classification.reasons
    )
    assert "no_significant_signals" not in classification.reasons


def test_reason_codes_unknown():
    """Reason codes indicate insufficient evidence for UNKNOWN."""
    evidence = TrafficEvidence()
    classification = classify_traffic(evidence)
    assert (
        "insufficient_evidence" in classification.reasons
        or "mixed_evidence" in classification.reasons
    )
