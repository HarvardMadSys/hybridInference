"""Unit tests for agent runtime adapters.

The Claude Code cases replay a stream recorded from a real
``claude -p --output-format stream-json`` run against this gateway
(``tests/fixtures/agent_runtime_streams/``), so the parser is pinned to what
the CLI actually emits rather than to what its docs describe.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from serving.agent_jobs.runtimes import (
    ClaudeCodeRuntime,
    CodexRuntime,
    GenericRuntime,
    get_runtime,
)

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "agent_runtime_streams"


def _recorded_lines() -> list[str]:
    """Return the recorded Claude Code stream, line by line."""
    return (_FIXTURES / "claude_code_tool_roundtrip.jsonl").read_text().splitlines()


def test_recorded_stream_maps_to_the_normalized_kinds():
    """A real tool round trip normalizes into the documented event kinds."""
    runtime = ClaudeCodeRuntime()
    kinds = [
        event.event_type
        for line in _recorded_lines()
        if (event := runtime.parse_event(line)) is not None
    ]
    # init -> tool_use -> rate-limit notice -> tool_result -> answer -> result
    assert kinds == [
        "lifecycle",
        "tool_use",
        "lifecycle",
        "tool_result",
        "message",
        "lifecycle",
    ]
    assert "raw" not in kinds, "recorded stream should be fully recognized"


def test_tool_use_carries_name_and_input():
    """The tool call the agent actually made is reported with its arguments."""
    runtime = ClaudeCodeRuntime()
    events = [runtime.parse_event(line) for line in _recorded_lines()]
    tool_use = next(e for e in events if e and e.event_type == "tool_use")
    assert tool_use.payload["name"] == "bash"
    assert tool_use.payload["input"] == {"cmd": "pwd"}


def test_final_result_reports_cost_as_untrusted():
    """The runtime's self-reported cost is recorded but labelled as such."""
    runtime = ClaudeCodeRuntime()
    events = [runtime.parse_event(line) for line in _recorded_lines()]
    result = [e for e in events if e and e.event_type == "lifecycle"][-1]
    assert result.payload["phase"] == "result"
    assert "PWD_OK" in result.payload["text"]
    # Present for observability; api_logs remains the billing authority.
    assert result.payload["reported_cost_usd"] is not None


def test_unrecognized_lines_become_raw_not_dropped():
    """A format change degrades to a log tail instead of silence."""
    runtime = ClaudeCodeRuntime()
    for line in ('{"type":"brand_new_thing","x":1}', "not json at all", "[1,2,3]"):
        event = runtime.parse_event(line)
        assert event is not None
        assert event.event_type == "raw"
        assert event.payload["text"]


def test_blank_lines_are_skipped():
    """Empty output produces no event at all."""
    assert ClaudeCodeRuntime().parse_event("   ") is None


def test_error_result_maps_to_error():
    """A failed run surfaces as an error event, not a successful lifecycle."""
    line = json.dumps({"type": "result", "subtype": "error_max_turns", "is_error": True})
    event = ClaudeCodeRuntime().parse_event(line)
    assert event.event_type == "error"
    assert event.payload["subtype"] == "error_max_turns"


def test_claude_prepare_points_at_the_gateway_and_disables_telemetry():
    """The agent talks to our gateway with the job's token and phones nobody."""
    argv, env = ClaudeCodeRuntime().prepare(
        workdir="/tmp/x",
        task_prompt="do the thing",
        model="glm-5.1",
        gateway_base_url="https://gateway.example.com/v1",
        credential="ajt.a.b",
    )
    assert argv[:3] == ["claude", "-p", "do the thing"]
    assert "--output-format" in argv and "stream-json" in argv
    assert env["ANTHROPIC_BASE_URL"] == "https://gateway.example.com"
    assert env["ANTHROPIC_API_KEY"] == "ajt.a.b"
    assert env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"
    assert env["DISABLE_AUTOUPDATER"] == "1"


def test_codex_prepare_and_config_target_the_gateway():
    """Codex is pointed at the gateway through a scratch provider config."""
    runtime = CodexRuntime()
    argv, env = runtime.prepare(
        workdir="/tmp/x",
        task_prompt="do it",
        model="glm-5.1",
        gateway_base_url="http://localhost:8000",
        credential="ajt.a.b",
    )
    assert argv[:2] == ["codex", "exec"]
    assert env["CODEX_API_KEY"] == "ajt.a.b"

    # The owner's model must actually reach the CLI. This used to ride
    # `CODEX_MODEL`, which Codex does not read, so the choice was dropped
    # silently and the run used whatever the CLI defaulted to.
    assert "--model" in argv
    assert argv[argv.index("--model") + 1] == "glm-5.1"

    joined = " ".join(argv)
    assert 'model_provider="hybridinference"' in joined
    assert 'model_providers.hybridinference.name="HybridInference"' in joined
    assert 'base_url="http://localhost:8000/v1"' in joined
    # Codex removed chat wire support upstream (openai/codex#7782); the gateway
    # serves /v1/responses. `chat` here would fail every turn.
    assert 'wire_api="responses"' in joined
    # The operator's own Codex config must not reach a sandbox run.
    assert "--ignore-user-config" in argv
    # The prompt is the final operand, after every flag.
    assert argv[-1] == "do it"


def test_codex_events_normalize():
    """Codex item events map onto the same normalized kinds."""
    runtime = CodexRuntime()
    tool = runtime.parse_event(
        json.dumps({"type": "item.started", "item": {"type": "command_execution", "command": "ls"}})
    )
    assert tool.event_type == "tool_use"
    result = runtime.parse_event(
        json.dumps(
            {
                "type": "item.completed",
                "item": {
                    "id": "item-1",
                    "type": "command_execution",
                    "command": "ls",
                    "aggregated_output": "README.md\n",
                    "exit_code": 0,
                },
            }
        )
    )
    assert result.event_type == "tool_result"
    assert result.payload == {
        "tool_use_id": "item-1",
        "is_error": False,
        "content": "README.md\n",
        "exit_code": 0,
    }
    message = runtime.parse_event(
        json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "hi"}})
    )
    assert message.event_type == "message"
    assert runtime.parse_event(json.dumps({"type": "turn.completed"})).event_type == "lifecycle"


def test_generic_runtime_streams_raw_and_never_uses_a_shell():
    """Tier 2 gets a log tail for free, with no shell interpretation."""
    runtime = GenericRuntime("mytool --prompt {prompt} --model {model}")
    argv, env = runtime.prepare(
        workdir="/tmp/x",
        task_prompt="a b; rm -rf /",
        model="m",
        gateway_base_url="http://gw",
        credential="ajt.a.b",
    )
    # The dangerous string stays a single argv element — no shell, no split.
    assert "a b; rm -rf /" in argv
    assert env["OPENAI_API_KEY"] == "ajt.a.b"
    assert runtime.parse_event("some log line").event_type == "raw"


def test_registry_resolution_and_refusal():
    """Known runtimes resolve; unknown ones refuse unless Tier 2 is requested."""
    assert isinstance(get_runtime("claude-code"), ClaudeCodeRuntime)
    assert isinstance(get_runtime("codex"), CodexRuntime)
    assert isinstance(get_runtime("pi", generic_command="pi run"), GenericRuntime)
    with pytest.raises(KeyError):
        get_runtime("does-not-exist")


def test_capabilities_declare_tiers():
    """Tiering is explicit so the platform never assumes support."""
    assert ClaudeCodeRuntime().capabilities().tier == 1
    assert ClaudeCodeRuntime().capabilities().resume is True
    assert GenericRuntime("x").capabilities().tier == 2
    assert GenericRuntime("x").capabilities().normalized_events is False
