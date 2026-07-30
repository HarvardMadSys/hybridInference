"""Unit tests for agent runtime adapters.

The Claude Code cases replay a sanitized, synthetic contract fixture derived
from observed ``claude -p --output-format stream-json`` output. This pins the
parser to the CLI's emitted shape without checking in a real machine's runtime
metadata.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from serving.agent_jobs.runtimes import (
    ClaudeCodeRuntime,
    CodexRuntime,
    GenericRuntime,
    OpencodeRuntime,
    PiRuntime,
    RuntimeMCPConfig,
    RuntimeMCPUnavailableError,
    get_runtime,
)

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "agent_runtime_streams"


def _contract_lines() -> list[str]:
    """Return the synthetic Claude Code contract stream, line by line."""
    return (_FIXTURES / "claude_code_stream_contract.jsonl").read_text().splitlines()


def test_synthetic_contract_stream_maps_to_the_normalized_kinds():
    """The sanitized contract stream preserves the six observed event kinds."""
    runtime = ClaudeCodeRuntime()
    kinds = [
        event.event_type
        for line in _contract_lines()
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
    assert "raw" not in kinds, "synthetic contract stream should be fully recognized"


def test_tool_use_carries_name_and_input():
    """The contract's synthetic tool call is reported with its arguments."""
    runtime = ClaudeCodeRuntime()
    events = [runtime.parse_event(line) for line in _contract_lines()]
    tool_use = next(e for e in events if e and e.event_type == "tool_use")
    assert tool_use.payload["name"] == "bash"
    assert tool_use.payload["input"] == {"cmd": "pwd"}


def test_final_result_reports_cost_as_untrusted():
    """The runtime's self-reported cost is exposed but labelled as such."""
    runtime = ClaudeCodeRuntime()
    events = [runtime.parse_event(line) for line in _contract_lines()]
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
    # Ignore both user-level and committed .mcp.json servers. Platform MCP is
    # supplied explicitly later; P0's effective set must stay empty.
    assert "--strict-mcp-config" in argv
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
        json.dumps(
            {"type": "item.completed", "item": {"type": "command_execution", "command": "ls"}}
        )
    )
    assert tool.event_type == "tool_use"
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
    assert isinstance(get_runtime("pi"), PiRuntime)
    assert isinstance(get_runtime("opencode"), OpencodeRuntime)
    assert isinstance(
        get_runtime("some-new-cli", generic_command="some-new-cli run"), GenericRuntime
    )
    with pytest.raises(KeyError):
        get_runtime("does-not-exist")


def test_pi_runtime_targets_the_gateway_through_its_wrapper():
    """pi is invoked via the wrapper, with everything the wrapper needs.

    pi ignores OPENAI_BASE_URL (verified against a local fake: zero hits, a
    real OpenAI 401), so the invocation must go through `pi-freeinference`,
    which writes the provider config from the environment. A regression here
    silently sends jobs to api.openai.com, where the egress policy turns them
    into timeouts that read like a broken model.
    """
    runtime = PiRuntime()
    argv, env = runtime.prepare(
        workdir="/tmp/x",
        task_prompt="do the thing; carefully",
        model="glm-5.1",
        gateway_base_url="http://backend:8080",
        credential="ajt.a.b",
    )

    assert argv[0] == "pi-freeinference"
    assert runtime.binary == "pi-freeinference"
    # The prompt stays one argv element — no shell anywhere on the path.
    assert "do the thing; carefully" in argv
    assert argv[argv.index("--model") + 1] == "glm-5.1"
    assert "--mode" in argv and argv[argv.index("--mode") + 1] == "json"
    # The pinned pi release implements MCP through executable extensions,
    # including extensions committed under .pi/.
    assert "--no-extensions" in argv
    # Everything the wrapper reads to build ~/.pi/agent/models.json.
    assert env["OPENAI_BASE_URL"] == "http://backend:8080/v1"
    assert env["OPENAI_API_KEY"] == "ajt.a.b"
    assert env["PI_GATEWAY_MODEL"] == "glm-5.1"
    # Tier 2: structured or not, pi's output is passed through as raw.
    assert runtime.parse_event('{"type":"turn_start"}').event_type == "raw"
    assert runtime.capabilities().tier == 2


@pytest.mark.parametrize(
    "runtime",
    [
        ClaudeCodeRuntime(),
        CodexRuntime(),
        PiRuntime(),
        OpencodeRuntime(),
        GenericRuntime("some-agent {prompt}"),
    ],
    ids=["claude", "codex", "pi", "opencode", "generic"],
)
def test_mcp_config_fails_closed_until_the_gateway_broker_exists(runtime):
    """No adapter may silently turn a requested server into direct MCP."""
    config = RuntimeMCPConfig(server_ids=("repository-supplied-server",))

    with pytest.raises(RuntimeMCPUnavailableError) as raised:
        runtime.prepare(
            workdir="/tmp/x",
            task_prompt="do it",
            model="glm-5.1",
            gateway_base_url="http://backend:8080",
            credential="mcp-secret-must-not-leak",
            mcp_config=config,
        )

    # Server ids and credentials do not enter an error that may reach job logs.
    assert "repository-supplied-server" not in str(raised.value)
    assert "mcp-secret-must-not-leak" not in str(raised.value)


@pytest.mark.parametrize(
    "runtime",
    [ClaudeCodeRuntime(), CodexRuntime(), PiRuntime(), OpencodeRuntime()],
    ids=["claude", "codex", "pi", "opencode"],
)
def test_runtime_credentials_stay_out_of_process_arguments(runtime):
    """The model-scoped token is environment-only, never argv/log material."""
    credential = "ajt.secret.value"
    argv, _env = runtime.prepare(
        workdir="/tmp/x",
        task_prompt="do it",
        model="glm-5.1",
        gateway_base_url="http://backend:8080",
        credential=credential,
        mcp_config=RuntimeMCPConfig(),
    )

    assert all(credential not in argument for argument in argv)


def test_opencode_runtime_targets_the_gateway_through_its_wrapper():
    """OpenCode is invoked via the wrapper, with everything it needs.

    OpenCode ignores OPENAI_BASE_URL and its built-in openai provider speaks
    the Responses API; the wrapper declares a chat-completions provider over
    the SDK package bundled in the binary and disables the models.dev fetch
    that otherwise hard-fails every offline run.
    """
    runtime = OpencodeRuntime()
    argv, env = runtime.prepare(
        workdir="/tmp/x",
        task_prompt="fix the bug; then run tests",
        model="glm-5.1",
        gateway_base_url="http://backend:8080",
        credential="ajt.a.b",
    )

    assert argv[0] == "opencode-freeinference"
    assert runtime.binary == "opencode-freeinference"
    assert "fix the bug; then run tests" in argv
    # The model rides inside the provider-qualified -m argument.
    assert argv[argv.index("-m") + 1] == "freeinference/glm-5.1"
    # The sandbox is the boundary; an interactive permission gate inside it
    # only guarantees the agent cannot do the work.
    assert "--auto" in argv
    assert env["OPENAI_BASE_URL"] == "http://backend:8080/v1"
    assert env["OPENAI_API_KEY"] == "ajt.a.b"
    assert env["OPENCODE_GATEWAY_MODEL"] == "glm-5.1"
    assert runtime.parse_event('{"type":"text"}').event_type == "raw"
    assert runtime.capabilities().tier == 2


def test_capabilities_declare_tiers():
    """Tiering is explicit so the platform never assumes support."""
    assert ClaudeCodeRuntime().capabilities().tier == 1
    assert ClaudeCodeRuntime().capabilities().resume is True
    assert GenericRuntime("x").capabilities().tier == 2
    assert GenericRuntime("x").capabilities().normalized_events is False


def test_system_telemetry_does_not_become_408_milestones():
    """A progress counter must not enter the event log once per emission.

    Shape taken from the first real staging job, not from documentation: it
    stored 412 lifecycle events, 408 of them ``subtype: "thinking_tokens"``,
    which the UI then drew as 408 ticked-off steps. The first occurrence is
    kept as a raw diagnostic so a new subtype stays discoverable; the rest are
    dropped.
    """
    runtime = ClaudeCodeRuntime()
    line = json.dumps({"type": "system", "subtype": "thinking_tokens", "session_id": "s1"})

    first = runtime.parse_event(line)
    assert first is not None
    assert first.event_type == "raw", "an unclassified subtype must not pass as a milestone"
    assert "thinking_tokens" in first.payload["reason"]

    for _ in range(407):
        assert runtime.parse_event(line) is None, "repeats must be suppressed, not stored"


def test_real_milestones_still_arrive_as_lifecycle():
    """The allowlisted subtypes keep their phase, model and runtime version."""
    runtime = ClaudeCodeRuntime()
    event = runtime.parse_event(
        json.dumps(
            {
                "type": "system",
                "subtype": "init",
                "model": "glm-5.1",
                "claude_code_version": "2.1.220",
            }
        )
    )
    assert event is not None
    assert event.event_type == "lifecycle"
    assert event.payload["phase"] == "init"
    assert event.payload["runtime_version"] == "2.1.220"

    compacted = runtime.parse_event(json.dumps({"type": "system", "subtype": "compact_boundary"}))
    assert compacted is not None and compacted.event_type == "lifecycle"


def test_suppression_does_not_leak_between_jobs():
    """Each job gets its own adapter, so its first occurrence is still reported."""
    line = json.dumps({"type": "system", "subtype": "thinking_tokens"})
    assert ClaudeCodeRuntime().parse_event(line) is not None
    assert ClaudeCodeRuntime().parse_event(line) is not None
