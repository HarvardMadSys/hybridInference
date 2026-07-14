"""Tests for the hardened Codex command and JSONL parsing."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import SecretStr

from serving.triage.config import TriageSettings
from serving.triage.models import AlertEvent
from serving.triage.runner import CodexRunError, CodexRunner, parse_thread_id


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


def test_build_command_uses_hybrid_inference_without_exposing_secret(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_API_KEY", "inherited-openai-secret")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "upstream-deepseek-secret")
    settings = TriageSettings(
        repository_path=tmp_path,
        state_dir=tmp_path / "state",
        hybrid_inference_base_url="https://staging.freeinference.org/v1/",
        codex_api_key=SecretStr("service-secret"),
    )
    runner = CodexRunner(settings)

    command = runner.build_command(tmp_path / "schema.json", tmp_path / "output.json")
    joined = " ".join(command)

    assert "--sandbox read-only" in joined
    assert "--ignore-user-config" in command
    assert "shell_environment_policy.include_only" in joined
    assert 'model_provider="hybrid_inference"' in command
    assert (
        'model_providers.hybrid_inference.base_url="https://staging.freeinference.org/v1"'
        in command
    )
    assert 'model_providers.hybrid_inference.wire_api="responses"' in command
    assert command[command.index("--model") + 1] == "deepseek-v4-pro"
    assert "service-secret" not in joined

    environment = runner._subprocess_env()
    assert environment["CODEX_API_KEY"] == "service-secret"
    assert "DEEPSEEK_API_KEY" not in environment


def test_settings_require_codex_api_key():
    settings = TriageSettings(
        relay_token=SecretStr("relay-secret"),
        slack_bot_token=SecretStr("slack-secret"),
        slack_channel_id="C0123456789",
        codex_api_key=SecretStr(""),
    )

    assert settings.configured is False
    assert settings.model_copy(update={"codex_api_key": SecretStr("service-secret")}).configured


def test_prompt_excludes_slack_text_and_redacts_context():
    prompt = CodexRunner._prompt(event())

    assert "this must not enter the prompt" not in prompt
    assert "sk-secret" not in prompt
    assert "Bearer [REDACTED]" in prompt
    assert "[REDACTED]" in prompt
    assert "<required_output_json_schema>" in prompt
    assert '"classification"' in prompt


async def test_run_reports_process_start_failure(tmp_path):
    runner = CodexRunner(
        TriageSettings(
            repository_path=tmp_path,
            state_dir=tmp_path / "state",
            codex_binary="missing-codex",
        )
    )

    with (
        patch(
            "serving.triage.runner.asyncio.create_subprocess_exec",
            new=AsyncMock(side_effect=FileNotFoundError("missing-codex")),
        ),
        pytest.raises(CodexRunError, match="failed to start Codex process: missing-codex"),
    ):
        await runner.run(event())


def test_parse_thread_id_ignores_non_json_lines():
    output = """progress
{"type":"turn.started"}
{"type":"thread.started","thread_id":"thread-123"}
"""
    assert parse_thread_id(output) == "thread-123"
