"""Tests for the workflow-side render/post entrypoints."""

import json
from typing import ClassVar

import pytest

from serving.oncall import gha
from serving.oncall.models import OnCallAnalysis


def _payload_file(tmp_path):
    payload = {
        "alert": {
            "alert_id": "alert-1",
            "title": "Provider failed",
            "context": {"api_key": "[REDACTED]", "provider": "openai"},
        },
        "fingerprint": "gateway:production:test",
        "alert_id": "alert-1",
        "slack_channel_id": "C123",
        "slack_thread_ts": "171.1",
        "model": "glm-5.2",
        "base_url": "https://gateway.example.com/v1",
    }
    path = tmp_path / "payload.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class FakeSlackClient:
    sent: ClassVar[list[tuple[str, str, str | None]]] = []

    def __init__(self, token: str, channel: str) -> None:
        self._token = token
        self._channel = channel

    async def post(self, text: str, *, thread_ts: str | None = None) -> str:
        FakeSlackClient.sent.append((self._channel, text, thread_ts))
        return "1"


def test_render_writes_prompt_embedding_schema_and_alert(tmp_path):
    prompt_out = tmp_path / "prompt.txt"
    schema_out = tmp_path / "schema.json"

    rc = gha.main(
        [
            "render",
            "--payload",
            str(_payload_file(tmp_path)),
            "--prompt-out",
            str(prompt_out),
            "--schema-out",
            str(schema_out),
        ]
    )

    assert rc == 0
    prompt = prompt_out.read_text(encoding="utf-8")
    schema = json.loads(schema_out.read_text(encoding="utf-8"))
    assert "read-only incident oncall agent" in prompt
    assert "at least one successful read-only inspection command" in prompt
    assert "<untrusted_alert_json>" in prompt
    assert '"api_key": "[REDACTED]"' in prompt
    assert "classification" in schema["properties"]


def test_parse_thread_id_ignores_non_json_lines():
    output = """progress
{"type":"turn.started"}
{"type":"thread.started","thread_id":"thread-123"}
"""
    assert gha.parse_thread_id(output) == "thread-123"


def test_validate_codex_log_requires_successful_command_and_completed_turn():
    with pytest.raises(ValueError, match="successful command_execution"):
        gha.validate_codex_log(
            '{"type":"item.completed","item":{"type":"command_execution",'
            '"exit_code":1}}\n{"type":"turn.completed"}\n'
        )

    with pytest.raises(ValueError, match="did not complete"):
        gha.validate_codex_log(
            '{"type":"item.completed","item":{"type":"command_execution","exit_code":0}}\n'
        )

    with pytest.raises(ValueError, match="turn failed"):
        gha.validate_codex_log(
            '{"type":"item.completed","item":{"type":"command_execution",'
            '"exit_code":0}}\n{"type":"turn.failed"}\n'
        )


def test_parse_analysis_output_accepts_commentary_and_markdown_fence():
    analysis = OnCallAnalysis(
        summary="Accumulator fails on a null tool name",
        classification="code_bug",
        confidence=0.9,
        impact="Streaming tool calls fail",
        evidence=["The continuation chunk contains a null name"],
        likely_cause="The accumulator concatenates None to a string",
        recommended_actions=["Guard nullable continuation fields"],
        issue_recommendation="create",
        draft_pr_recommendation="create",
    )
    raw_output = f"Inspection complete.\n```json\n{analysis.model_dump_json()}\n```"

    parsed = gha.parse_analysis_output(raw_output)

    assert parsed == analysis


def test_parse_analysis_output_rejects_multiple_valid_objects():
    analysis = OnCallAnalysis(
        summary="Ambiguous result",
        classification="unknown",
        confidence=0.1,
        impact="Unknown",
        evidence=[],
        likely_cause="Unknown",
        recommended_actions=["Inspect the repository"],
        issue_recommendation="none",
        draft_pr_recommendation="none",
    )
    encoded = analysis.model_dump_json()

    with pytest.raises(ValueError, match="multiple valid JSON objects"):
        gha.parse_analysis_output(f"{encoded}\n{encoded}")


def test_parse_analysis_output_rejects_nested_analysis_object():
    analysis = OnCallAnalysis(
        summary="Nested result",
        classification="unknown",
        confidence=0.1,
        impact="Unknown",
        evidence=[],
        likely_cause="Unknown",
        recommended_actions=["Inspect the repository"],
        issue_recommendation="none",
        draft_pr_recommendation="none",
    )
    wrapped = json.dumps({"analysis": analysis.model_dump(mode="json")})

    with pytest.raises(ValueError, match="no valid JSON object"):
        gha.parse_analysis_output(wrapped)


def test_post_delivers_formatted_analysis_in_thread(tmp_path, monkeypatch):
    FakeSlackClient.sent = []
    monkeypatch.setattr(gha, "SlackClient", FakeSlackClient)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    analysis = OnCallAnalysis(
        summary="Upstream 429s",
        classification="upstream_provider",
        confidence=0.6,
        impact="Some requests fail",
        evidence=["provider dashboard shows throttling"],
        likely_cause="Provider rate limits",
        recommended_actions=["Wait for provider recovery"],
        issue_recommendation="none",
        draft_pr_recommendation="none",
    )
    analysis_path = tmp_path / "analysis.json"
    analysis_path.write_text(analysis.model_dump_json(), encoding="utf-8")
    codex_log = tmp_path / "codex.jsonl"
    codex_log.write_text(
        '{"type":"thread.started","thread_id":"th-9"}\n'
        '{"type":"item.completed","item":{"type":"command_execution",'
        '"exit_code":0}}\n'
        '{"type":"turn.completed"}\n',
        encoding="utf-8",
    )

    rc = gha.main(
        [
            "post",
            "--payload",
            str(_payload_file(tmp_path)),
            "--analysis",
            str(analysis_path),
            "--codex-log",
            str(codex_log),
        ]
    )

    assert rc == 0
    channel, text, thread_ts = FakeSlackClient.sent[0]
    assert channel == "C123"
    assert thread_ts == "171.1"
    assert text.startswith("*Codex on-call*")
    assert "th-9" in text


def test_post_rejects_analysis_without_successful_command(tmp_path, monkeypatch):
    FakeSlackClient.sent = []
    monkeypatch.setattr(gha, "SlackClient", FakeSlackClient)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    analysis = OnCallAnalysis(
        summary="Guessed answer",
        classification="unknown",
        confidence=0.1,
        impact="Unknown",
        evidence=[],
        likely_cause="Unknown",
        recommended_actions=["Inspect the repository"],
        issue_recommendation="none",
        draft_pr_recommendation="none",
    )
    analysis_path = tmp_path / "analysis.json"
    analysis_path.write_text(analysis.model_dump_json(), encoding="utf-8")
    codex_log = tmp_path / "codex.jsonl"
    codex_log.write_text(
        '{"type":"item.completed","item":{"type":"agent_message"}}\n{"type":"turn.completed"}\n',
        encoding="utf-8",
    )

    rc = gha.main(
        [
            "post",
            "--payload",
            str(_payload_file(tmp_path)),
            "--analysis",
            str(analysis_path),
            "--codex-log",
            str(codex_log),
        ]
    )

    assert rc == 2
    assert FakeSlackClient.sent == []


def test_post_failure_notice_includes_run_url(tmp_path, monkeypatch):
    FakeSlackClient.sent = []
    monkeypatch.setattr(gha, "SlackClient", FakeSlackClient)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")

    rc = gha.main(
        [
            "post",
            "--payload",
            str(_payload_file(tmp_path)),
            "--failed",
            "--run-url",
            "https://github.com/org/repo/actions/runs/1",
        ]
    )

    assert rc == 0
    channel, text, thread_ts = FakeSlackClient.sent[0]
    assert channel == "C123"
    assert thread_ts == "171.1"
    assert "Codex on-call unavailable" in text
    assert "https://github.com/org/repo/actions/runs/1" in text
