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
