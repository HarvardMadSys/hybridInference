"""Cross-language fixture tests for the dormant alert control-plane contract."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from serving.observability.control_plane_contract import parse_control_plane_alert_event

_FIXTURES = (
    Path(__file__).resolve().parents[3]
    / "services"
    / "alert-control-plane-worker"
    / "test"
    / "fixtures"
)
_NOW = dt.datetime(2026, 7, 20, 7, tzinfo=dt.timezone.utc)


def _fixture(name: str) -> object:
    return json.loads((_FIXTURES / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    "name",
    [
        "valid-provider-circuit-firing.json",
        "valid-provider-circuit-resolved.json",
    ],
)
def test_python_contract_accepts_shared_valid_fixtures(name: str) -> None:
    """Accept every shared fixture declared valid by the TypeScript contract."""
    event = parse_control_plane_alert_event(_fixture(name), now=_NOW)

    assert event.schema_version == 1
    assert event.alert_type == "provider_circuit_open"


class TestGatewayAlertTypes:
    """The two types the backend's own alerts migrate onto.

    Both sides read these fixtures, so a rule that drifts between the
    TypeScript validator and this mirror fails here rather than at ingress,
    where a producer only learns a status code.
    """

    def test_accepts_the_shared_metric_threshold_fixture(self) -> None:
        event = parse_control_plane_alert_event(
            _fixture("valid-metric-threshold-firing.json"), now=_NOW
        )

        assert event.alert_type == "metric_threshold_breach"
        assert event.context.metric == "auth_failure_count"
        assert (event.context.observed, event.context.threshold) == (41.0, 20.0)
        assert event.context.source_addresses == ["203.0.113.7"]

    def test_accepts_the_shared_dependency_fixture(self) -> None:
        event = parse_control_plane_alert_event(
            _fixture("valid-dependency-unavailable-firing.json"), now=_NOW
        )

        assert event.alert_type == "dependency_unavailable"
        assert event.context.dependency == "operational_store"
        assert (event.context.backend, event.context.reason) == (
            "postgres",
            "health_check_failed",
        )

    def test_rejects_a_firing_breach_below_its_own_threshold(self) -> None:
        """Otherwise the card claims a crossing while showing numbers that deny it."""
        payload = _fixture("valid-metric-threshold-firing.json")
        assert isinstance(payload, dict)
        payload["context"] = {**payload["context"], "observed": 3.0, "threshold": 10.0}

        with pytest.raises((ValidationError, ValueError)):
            parse_control_plane_alert_event(payload, now=_NOW)

    def test_a_resolution_may_report_a_value_below_the_threshold(self) -> None:
        """That is precisely what recovery looks like."""
        payload = _fixture("valid-metric-threshold-firing.json")
        assert isinstance(payload, dict)
        payload["status"] = "resolved"
        payload["context"] = {**payload["context"], "observed": 0.0, "threshold": 20.0}

        event = parse_control_plane_alert_event(payload, now=_NOW)

        assert event.status == "resolved"

    def test_addresses_stay_on_the_metric_whose_response_is_to_block_them(self) -> None:
        payload = _fixture("valid-metric-threshold-firing.json")
        assert isinstance(payload, dict)
        payload["context"] = {**payload["context"], "metric": "http_5xx_rate"}

        with pytest.raises((ValidationError, ValueError)):
            parse_control_plane_alert_event(payload, now=_NOW)

    @pytest.mark.parametrize(
        "address",
        ["not-an-ip", "203.0.113.7:443", "example.com", ""],
    )
    def test_source_addresses_must_be_real_addresses(self, address: str) -> None:
        payload = _fixture("valid-metric-threshold-firing.json")
        assert isinstance(payload, dict)
        payload["context"] = {**payload["context"], "source_addresses": [address]}

        with pytest.raises((ValidationError, ValueError)):
            parse_control_plane_alert_event(payload, now=_NOW)

    def test_backend_rejects_a_credential_free_dsn(self) -> None:
        """`postgres://localhost/app` passes every untrusted-text check."""
        payload = _fixture("valid-dependency-unavailable-firing.json")
        assert isinstance(payload, dict)
        payload["context"] = {
            **payload["context"],
            "backend": "postgres://localhost/app",
        }

        with pytest.raises((ValidationError, ValueError)):
            parse_control_plane_alert_event(payload, now=_NOW)

    @pytest.mark.parametrize(
        "name",
        [
            "valid-metric-threshold-firing.json",
            "valid-dependency-unavailable-firing.json",
        ],
    )
    def test_context_shapes_do_not_cross_alert_types(self, name: str) -> None:
        """The union discriminates on alert_type, not on whichever member fits."""
        payload = _fixture(name)
        assert isinstance(payload, dict)
        payload["alert_type"] = "provider_circuit_open"

        with pytest.raises((ValidationError, ValueError)):
            parse_control_plane_alert_event(payload, now=_NOW)


@pytest.mark.parametrize(
    "name",
    [
        "invalid-evidence-path-traversal.json",
        "invalid-context-type.json",
        "invalid-network-ipv6.json",
        "invalid-prompt-injection.json",
        "invalid-secret-material.json",
        "invalid-trusted-field.json",
        "invalid-unknown-context-key.json",
        "invalid-unsupported-alert-type.json",
    ],
)
def test_python_contract_rejects_shared_invalid_fixtures(name: str) -> None:
    """Reject every shared fixture declared invalid by the TypeScript contract."""
    with pytest.raises((ValidationError, ValueError)):
        parse_control_plane_alert_event(_fixture(name), now=_NOW)


@pytest.mark.parametrize(
    "secret",
    [
        "sk-test-NOTAREAL",
        "gsk_NOTAREAL",
        "xai-NOTAREAL",
        "rk_NOTAREAL",
        "AIzaNOTAREAL00",
    ],
)
def test_python_contract_rejects_bare_provider_api_keys(secret: str) -> None:
    """Reject provider credentials even when no key label precedes the value."""
    payload = _fixture("valid-provider-circuit-firing.json")
    assert isinstance(payload, dict)
    payload["summary"] = f"Provider rejected credential {secret}"

    with pytest.raises((ValidationError, ValueError)):
        parse_control_plane_alert_event(payload, now=_NOW)


def test_python_contract_rejects_home_relative_evidence_path() -> None:
    """Keep home-relative evidence paths outside the canonical contract."""
    payload = _fixture("valid-provider-circuit-firing.json")
    assert isinstance(payload, dict)
    payload["evidence_refs"] = ["~/.ssh/id_rsa"]

    with pytest.raises((ValidationError, ValueError)):
        parse_control_plane_alert_event(payload, now=_NOW)


@pytest.mark.parametrize(
    "network_identifier",
    [
        "127.0.0.1",
        "127.0.0.1:8000",
        "10.0.0.5:443",
        "::1",
        "[::1]:8000",
    ],
)
def test_python_contract_rejects_network_identifiers_with_ports(
    network_identifier: str,
) -> None:
    """Reject bare and host:port network identifiers, including IPv4 with a port."""
    payload = _fixture("valid-provider-circuit-firing.json")
    assert isinstance(payload, dict)
    payload["summary"] = f"Provider at {network_identifier} refused connections"

    with pytest.raises((ValidationError, ValueError)):
        parse_control_plane_alert_event(payload, now=_NOW)


def test_python_contract_normalizes_nanosecond_timestamp() -> None:
    """Accept 7-9 digit fractional seconds and normalize to milliseconds.

    datetime.fromisoformat rejects sub-microsecond fractions on Python 3.10, a
    supported runtime, so the validator must truncate before parsing.
    """
    payload = _fixture("valid-provider-circuit-firing.json")
    assert isinstance(payload, dict)
    payload["occurred_at"] = "2026-07-19T06:00:00.123456789Z"

    event = parse_control_plane_alert_event(payload, now=_NOW)

    assert event.occurred_at == "2026-07-19T06:00:00.123Z"
