"""Producer-side construction of canonical gateway alert events."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from serving.observability.control_plane_producer import (
    ControlPlaneEventError,
    build_dependency_unavailable_event,
    build_metric_threshold_event,
    metric_threshold_fingerprint,
)

MOMENT = dt.datetime(2026, 7, 28, 12, 0, 0, tzinfo=dt.timezone.utc)


def test_builds_a_firing_breach_with_units_left_to_the_renderer() -> None:
    event = build_metric_threshold_event(
        metric="failed_request_rate",
        status="firing",
        observed=0.123,
        threshold=0.05,
        window_sec=300,
        scope="gateway",
        sample_count=366,
        occurred_at=MOMENT,
        event_id="gateway-1",
    )
    assert event["alert_type"] == "metric_threshold_breach"
    assert event["occurred_at"] == "2026-07-28T12:00:00Z"
    # The wire carries a bare ratio; percent formatting is the renderer's job,
    # which keeps the value comparable across alerts.
    assert event["context"]["observed"] == 0.123
    assert event["severity"] == "error"


def test_resolution_downgrades_severity_and_keeps_the_incident_key() -> None:
    firing = build_metric_threshold_event(
        metric="http_5xx_rate", status="firing", observed=0.2, threshold=0.05
    )
    resolved = build_metric_threshold_event(
        metric="http_5xx_rate", status="resolved", observed=0.01, threshold=0.05
    )
    # Same fingerprint or the control plane would open a second incident
    # instead of closing the first.
    assert firing["fingerprint"] == resolved["fingerprint"]
    assert resolved["severity"] == "info"


def test_a_firing_breach_below_its_own_threshold_is_refused() -> None:
    # Such a card would argue against itself in Slack; caught here so the stack
    # trace points at the rule rather than at an ingress status code.
    with pytest.raises(ControlPlaneEventError):
        build_metric_threshold_event(
            metric="http_5xx_rate", status="firing", observed=0.01, threshold=0.05
        )
    # The same numbers are exactly what a resolution reports.
    assert build_metric_threshold_event(
        metric="http_5xx_rate", status="resolved", observed=0.01, threshold=0.05
    )


class TestScopedSubjects:
    """Per-subject alerts must resolve independently of each other."""

    def test_each_subject_gets_its_own_incident_key(self) -> None:
        one = metric_threshold_fingerprint("latency_p95_ms", "zhipu")
        other = metric_threshold_fingerprint("latency_p95_ms", "minimax")
        assert one != other
        assert metric_threshold_fingerprint("http_5xx_rate") == "gateway:http_5xx_rate"

    def test_the_subject_reaches_the_title_so_the_card_names_it(self) -> None:
        event = build_metric_threshold_event(
            metric="latency_p95_ms",
            status="firing",
            observed=2200,
            threshold=1500,
            scope="provider",
            subject="zhipu",
        )
        assert "zhipu" in event["title"]
        assert event["context"]["subject"] == "zhipu"

    @pytest.mark.parametrize(
        "value",
        ["user 4711 (over budget)", "12.3% of requests", "a" * 129, ""],
    )
    def test_free_text_cannot_enter_through_the_subject(self, value: str) -> None:
        with pytest.raises(ControlPlaneEventError):
            build_metric_threshold_event(
                metric="user_daily_cost",
                status="firing",
                observed=42.5,
                threshold=25,
                subject=value,
            )


class TestSourceAddresses:
    """The values on-call blocks survive, but only as addresses."""

    def test_addresses_are_normalized_and_kept(self) -> None:
        event = build_metric_threshold_event(
            metric="auth_failure_count",
            status="firing",
            observed=41,
            threshold=20,
            source_addresses=["203.0.113.7", "2001:0db8::0001"],
        )
        # Normalized, so the same source cannot appear twice in different forms.
        assert event["context"]["source_addresses"] == ["203.0.113.7", "2001:db8::1"]

    @pytest.mark.parametrize(
        "values",
        [
            ["1.2.3.4 (12), 5.6.7.8 (3)"],  # the old free-text shape
            ["not-an-address"],
            ["203.0.113.7", "203.0.113.7"],
            [],
            ["1.1.1.1", "2.2.2.2", "3.3.3.3", "4.4.4.4", "5.5.5.5", "6.6.6.6"],
        ],
    )
    def test_anything_that_is_not_a_short_list_of_addresses_is_refused(
        self, values: list[str]
    ) -> None:
        with pytest.raises(ControlPlaneEventError):
            build_metric_threshold_event(
                metric="auth_failure_count",
                status="firing",
                observed=41,
                threshold=20,
                source_addresses=values,
            )

    def test_the_exception_stays_scoped_to_metrics_that_block(self) -> None:
        with pytest.raises(ControlPlaneEventError):
            build_metric_threshold_event(
                metric="user_daily_cost",
                status="firing",
                observed=42.5,
                threshold=25,
                source_addresses=["203.0.113.7"],
            )


class TestDependencyEvents:
    def test_firing_carries_the_cause_and_resolution_drops_it(self) -> None:
        firing = build_dependency_unavailable_event(
            dependency="operational_store",
            status="firing",
            backend="postgres",
            reason="health_check_failed",
        )
        resolved = build_dependency_unavailable_event(
            dependency="operational_store", status="resolved", backend="postgres"
        )
        assert firing["context"]["reason"] == "health_check_failed"
        assert "reason" not in resolved["context"]
        assert firing["fingerprint"] == resolved["fingerprint"]
        assert firing["severity"] == "critical"

    def test_each_store_resolves_independently(self) -> None:
        one = build_dependency_unavailable_event(dependency="operational_store", status="firing")
        other = build_dependency_unavailable_event(dependency="log_store", status="firing")
        assert one["fingerprint"] != other["fingerprint"]


def test_naive_timestamps_are_refused() -> None:
    # A naive datetime would silently be read as UTC and skew occurred_at,
    # which orders the incident lifecycle.
    with pytest.raises(ControlPlaneEventError):
        build_metric_threshold_event(
            metric="http_5xx_rate",
            status="firing",
            observed=0.2,
            threshold=0.05,
            occurred_at=dt.datetime(2026, 7, 28, 12, 0, 0),
        )


def test_events_match_the_fixtures_the_typescript_validator_parses() -> None:
    """Cross-language contract: what Python builds is what the control plane accepts.

    The same fixture files are parsed by the alert-control-plane validator test,
    so a drift on either side fails on both.
    """
    fixtures = (
        Path(__file__).resolve().parents[3]
        / "services"
        / "alert-control-plane-worker"
        / "test"
        / "fixtures"
    )
    breach = json.loads((fixtures / "valid-metric-threshold-firing.json").read_text())
    outage = json.loads((fixtures / "valid-dependency-unavailable-firing.json").read_text())

    assert (
        build_metric_threshold_event(
            metric="auth_failure_count",
            status="firing",
            observed=41,
            threshold=20,
            window_sec=300,
            scope="gateway",
            source_addresses=["203.0.113.7"],
            distinct_sources=3,
            top_source_share=0.8,
            occurred_at=MOMENT,
            event_id="gateway-auth-failure-1",
        )
        == breach
    )
    assert (
        build_dependency_unavailable_event(
            dependency="operational_store",
            status="firing",
            backend="postgres",
            reason="health_check_failed",
            occurred_at=MOMENT,
            event_id="gateway-dependency-1",
        )
        == outage
    )
