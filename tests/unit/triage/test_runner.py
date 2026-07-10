"""Tests for the hardened Codex command and JSONL parsing."""

from datetime import datetime, timezone

from pydantic import SecretStr

from serving.triage.config import TriageSettings
from serving.triage.models import AlertEvent
from serving.triage.runner import CodexRunner, parse_thread_id


def event() -> AlertEvent:
    return AlertEvent(
        alert_id="alert-1",
        fingerprint="gateway:production:test",
        source="test",
        status="firing",
        severity="error",
        title="Provider failed",
        environment="production",
        occurred_at=datetime.now(timezone.utc),
        summary="Bearer abcdefghijklmnop",
        context={"api_key": "sk-secret", "provider": "openai"},
        slack_text="this must not enter the prompt",
    )


def test_build_command_is_read_only_and_filters_shell_environment(tmp_path):
    settings = TriageSettings(
        repository_path=tmp_path,
        state_dir=tmp_path / "state",
        codex_api_key=SecretStr("top-secret"),
    )
    runner = CodexRunner(settings)

    command = runner.build_command(tmp_path / "schema.json", tmp_path / "output.json")
    joined = " ".join(command)

    assert "--sandbox read-only" in joined
    assert "--ignore-user-config" in command
    assert "shell_environment_policy.include_only" in joined
    assert "top-secret" not in joined


def test_prompt_excludes_slack_text_and_redacts_context():
    prompt = CodexRunner._prompt(event())

    assert "this must not enter the prompt" not in prompt
    assert "sk-secret" not in prompt
    assert "Bearer [REDACTED]" in prompt
    assert "[REDACTED]" in prompt


def test_parse_thread_id_ignores_non_json_lines():
    output = """progress
{"type":"turn.started"}
{"type":"thread.started","thread_id":"thread-123"}
"""
    assert parse_thread_id(output) == "thread-123"
