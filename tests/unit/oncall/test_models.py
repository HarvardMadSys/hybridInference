"""Tests for oncall event validation and redaction."""

from serving.oncall.models import sanitize_for_agent


def test_sanitize_for_agent_redacts_nested_secrets_and_bearer_tokens():
    value = {
        "provider": "openai",
        "api_key": "sk-secret",
        "nested": {
            "message": "Authorization: Bearer abcdefghijklmnop",
            "password_hint": "do not include",
            "detail": "response contained hyi-abcdefghijklmnopqrstuvwxyz0123456789",
        },
    }

    assert sanitize_for_agent(value) == {
        "provider": "openai",
        "api_key": "[REDACTED]",
        "nested": {
            "message": "Authorization: Bearer [REDACTED]",
            "password_hint": "[REDACTED]",
            "detail": "response contained [REDACTED]",
        },
    }


def test_sanitize_for_agent_bounds_large_collections():
    sanitized = sanitize_for_agent({"items": list(range(100))})
    assert isinstance(sanitized, dict)
    assert len(sanitized["items"]) == 50
