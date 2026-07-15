"""Tests for the workflow-side render/post entrypoints."""

import json
from typing import ClassVar

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
        "model": "deepseek-v4-flash",
        "responses_base_url": "https://freeinference.org/v1",
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
    assert "<untrusted_alert_json>" in prompt
    assert '"api_key": "[REDACTED]"' in prompt
    assert "classification" in schema["properties"]


def test_parse_thread_id_ignores_non_json_lines():
    output = """progress
{"type":"turn.started"}
{"type":"thread.started","thread_id":"thread-123"}
"""
    assert gha.parse_thread_id(output) == "thread-123"


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
    codex_log.write_text('{"type":"thread.started","thread_id":"th-9"}\n', encoding="utf-8")

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
