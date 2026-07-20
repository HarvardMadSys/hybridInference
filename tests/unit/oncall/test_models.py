"""Tests for oncall event validation and redaction."""

import pytest
from pydantic import ValidationError

from serving.oncall.models import AlertEvent, AlertEventV2, sanitize_for_agent


def test_sanitize_for_agent_redacts_nested_secrets_and_bearer_tokens():
    value = {
        "provider": "openai",
        "api_key": "sk-secret",
        "user_id": "01ABC",
        "nested": {
            "message": "Authorization: Bearer abcdefghijklmnop",
            "password_hint": "do not include",
            "detail": "response contained hyi-abcdefghijklmnopqrstuvwxyz0123456789",
            "remote_ip": "192.0.2.1",
        },
    }

    assert sanitize_for_agent(value) == {
        "provider": "openai",
        "api_key": "[REDACTED]",
        "user_id": "[REDACTED]",
        "nested": {
            "message": "Authorization: Bearer [REDACTED]",
            "password_hint": "[REDACTED]",
            "detail": "response contained [REDACTED]",
            "remote_ip": "[REDACTED]",
        },
    }


def test_sanitize_for_agent_bounds_large_collections():
    sanitized = sanitize_for_agent({"items": list(range(100))})
    assert isinstance(sanitized, dict)
    assert len(sanitized["items"]) == 50


def test_v1_and_v2_alert_models_coexist_without_changing_v1_contract():
    common = {
        "alert_id": "alert-1",
        "fingerprint": "gateway:test",
        "source": "gateway",
        "status": "firing",
        "severity": "error",
        "title": "Provider failed",
        "occurred_at": "2026-07-19T12:00:00Z",
        "summary": "Requests fail",
        "context": {},
    }
    v1 = AlertEvent(
        **common,
        environment="staging",
        slack_text="legacy rendered message",
    )
    v2 = AlertEventV2(**common, deployment_sha="a" * 40)

    assert v1.version == "1"
    assert v1.slack_text == "legacy rendered message"
    assert v2.version == "2"
    assert "environment" not in v2.model_dump()
    assert "slack_text" not in v2.model_dump()


@pytest.mark.parametrize("producer_owned", [{"environment": "production"}, {"slack_text": "x"}])
def test_v2_model_rejects_producer_owned_rendering_and_identity(producer_owned):
    with pytest.raises(ValidationError):
        AlertEventV2(
            alert_id="alert-1",
            fingerprint="gateway:test",
            source="gateway",
            status="firing",
            severity="error",
            title="Provider failed",
            occurred_at="2026-07-19T12:00:00Z",
            summary="Requests fail",
            context={},
            **producer_owned,
        )


def test_v2_model_requires_full_immutable_deployment_sha():
    with pytest.raises(ValidationError):
        AlertEventV2(
            alert_id="alert-1",
            fingerprint="gateway:test",
            source="gateway",
            status="firing",
            severity="error",
            title="Provider failed",
            occurred_at="2026-07-19T12:00:00Z",
            summary="Requests fail",
            context={},
            deployment_sha="abc123",
        )
