"""Unit tests for the MCP registry, the proxy's filters, and runtime wiring.

The properties pinned here are the ones the feature's safety rests on: a
credential never reaching the sandbox, a repository never supplying its own MCP
servers, and a tool outside the allowlist being neither advertised nor callable.
"""

from __future__ import annotations

import json

import pytest

from serving.agent_jobs.mcp_proxy import SseFilter, filter_json_body, plan_request
from serving.agent_jobs.mcp_registry import (
    McpRegistry,
    McpRegistryError,
    McpServer,
    load_registry,
)
from serving.agent_jobs.runtimes import (
    MCP_TOKEN_ENV,
    ClaudeCodeRuntime,
    CodexRuntime,
    RuntimeMCPConfig,
    RuntimeMCPUnavailableError,
    runtimes_supporting_mcp,
)

_GATEWAY = "https://gateway.example.com"
_TOKEN = "ajt.job.attempt.signature"


def _write(tmp_path, body: str):
    """Write a registry YAML file and return its path."""
    path = tmp_path / "mcp.yaml"
    path.write_text(body)
    return path


def _server(**overrides) -> McpServer:
    """Build a server with a read-only allowlist unless told otherwise."""
    return McpServer(
        name=overrides.pop("name", "github"),
        url=overrides.pop("url", "https://mcp.example.com/mcp"),
        tools=overrides.pop("tools", frozenset({"get_issue", "list_issues"})),
        **overrides,
    )


# ── registry ──────────────────────────────────────────────────────────


def test_missing_file_means_no_servers(tmp_path):
    """A deployment that configures nothing keeps working, with no MCP."""
    assert load_registry(tmp_path / "absent.yaml").servers == {}
    assert load_registry(None).servers == {}


def test_servers_load_with_env_interpolated_credentials(tmp_path, monkeypatch):
    """A ${VAR} header resolves from the environment at load time."""
    monkeypatch.setenv("GITHUB_MCP_TOKEN", "ghs_secret")
    registry = load_registry(
        _write(
            tmp_path,
            """
servers:
  - name: github
    url: https://api.githubcopilot.com/mcp/
    default: true
    headers:
      Authorization: "Bearer ${GITHUB_MCP_TOKEN}"
    tools: [get_issue]
""",
        )
    )
    server = registry.get("github")
    assert server is not None
    assert server.headers == {"Authorization": "Bearer ghs_secret"}
    assert registry.defaults == ["github"]


def test_unset_credential_refuses_rather_than_sending_a_blank_header(tmp_path, monkeypatch):
    """An unset ${VAR} is a startup error naming it, not an empty Bearer."""
    monkeypatch.delenv("GITHUB_MCP_TOKEN", raising=False)
    with pytest.raises(McpRegistryError, match="GITHUB_MCP_TOKEN"):
        load_registry(
            _write(
                tmp_path,
                """
servers:
  - name: github
    url: https://api.githubcopilot.com/mcp/
    headers:
      Authorization: "Bearer ${GITHUB_MCP_TOKEN}"
""",
            )
        )


def test_stdio_servers_are_refused(tmp_path):
    """A ``command:`` server has nowhere to run and is rejected by name."""
    with pytest.raises(McpRegistryError, match=r"stdio"):
        load_registry(
            _write(
                tmp_path,
                """
servers:
  - name: local
    command: npx some-server
    url: https://example.com/mcp
""",
            )
        )


def test_plaintext_endpoints_are_refused_off_loopback(tmp_path):
    """The proxy attaches a credential, so the hop may not be plaintext."""
    with pytest.raises(McpRegistryError, match="plaintext"):
        load_registry(
            _write(tmp_path, "servers:\n  - name: bad\n    url: http://mcp.example.com/mcp\n")
        )


def test_loopback_http_is_allowed_for_development(tmp_path):
    """A server on the developer's own machine needs no certificate."""
    registry = load_registry(
        _write(tmp_path, "servers:\n  - name: dev\n    url: http://localhost:9000/mcp\n")
    )
    assert registry.names == ["dev"]


def test_reserved_headers_cannot_be_configured(tmp_path):
    """A registry may not override headers the proxy owns."""
    with pytest.raises(McpRegistryError, match="proxy"):
        load_registry(
            _write(
                tmp_path,
                "servers:\n  - name: x\n    url: https://e.com/mcp\n"
                '    headers:\n      Host: "elsewhere"\n',
            )
        )


@pytest.mark.parametrize("name", ["Has-Caps", "with space", "a/b", "", "x" * 60])
def test_invalid_server_names_are_refused(tmp_path, name):
    """A name reaches a URL path and every tool name; keep it inert."""
    with pytest.raises(McpRegistryError):
        load_registry(
            _write(tmp_path, f'servers:\n  - name: "{name}"\n    url: https://e.com/mcp\n')
        )


def test_public_view_never_carries_the_url_or_the_credential():
    """What the composer sees has nowhere to put the two secrets."""
    server = _server(headers={"Authorization": "Bearer ghs_secret"})
    rendered = json.dumps(McpRegistry(servers={"github": server}).public())
    assert "ghs_secret" not in rendered
    assert "mcp.example.com" not in rendered
    assert "get_issue" in rendered


def test_resolve_distinguishes_unset_from_empty():
    """Omitted takes the defaults; an explicit [] means no servers at all."""
    registry = McpRegistry(servers={"a": _server(name="a", default=True), "b": _server(name="b")})
    assert registry.resolve(None) == ["a"]
    assert registry.resolve([]) == []
    assert registry.resolve(["b"]) == ["b"]


def test_resolve_refuses_an_unknown_server():
    """A job asking for a tool surface it will not get fails at creation."""
    registry = McpRegistry(servers={"a": _server(name="a")})
    with pytest.raises(McpRegistryError, match="nope"):
        registry.resolve(["nope"])


# ── request guard ─────────────────────────────────────────────────────


def _call(tool: str, *, request_id: int = 1) -> bytes:
    """Encode a ``tools/call`` request."""
    return json.dumps(
        {"jsonrpc": "2.0", "id": request_id, "method": "tools/call", "params": {"name": tool}}
    ).encode()


def test_allowed_tool_is_forwarded():
    """A tool on the allowlist produces no rejection."""
    assert not plan_request(_call("get_issue"), _server()).rejected


def test_disallowed_tool_is_refused_before_leaving_the_gateway():
    """The allowlist is enforced here, not by asking the model nicely."""
    plan = plan_request(_call("create_issue"), _server())
    assert plan.rejected
    assert plan.blocked_tools == ("create_issue",)
    assert plan.rejection["id"] == 1
    assert "create_issue" in plan.rejection["error"]["message"]


def test_a_batch_with_one_blocked_tool_is_refused_whole():
    """Partial forwarding would need replies re-associated across requests."""
    body = json.dumps(
        [
            json.loads(_call("get_issue", request_id=1)),
            json.loads(_call("create_issue", request_id=2)),
        ]
    ).encode()
    plan = plan_request(body, _server())
    assert plan.rejected
    assert [entry["id"] for entry in plan.rejection] == [2]


def test_an_unfiltered_server_allows_every_tool():
    """Omitting ``tools:`` means the server's own surface, unmodified."""
    assert not plan_request(_call("delete_everything"), _server(tools=frozenset())).rejected


def test_a_malformed_body_is_passed_through_rather_than_guessed_at():
    """The upstream owns the protocol and answers a bad request better."""
    assert not plan_request(b"{not json", _server()).rejected


# ── response filtering ────────────────────────────────────────────────


def _catalogue(*names: str) -> bytes:
    """Encode a ``tools/list`` result advertising ``names``."""
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 7,
            "result": {"tools": [{"name": name, "description": name} for name in names]},
        }
    ).encode()


def _listed(body: bytes) -> list[str]:
    """Return the tool names a filtered catalogue still advertises."""
    return [tool["name"] for tool in json.loads(body)["result"]["tools"]]


def test_disallowed_tools_are_never_advertised():
    """A model cannot be talked into a tool it was never offered."""
    filtered = filter_json_body(_catalogue("get_issue", "create_issue", "list_issues"), _server())
    assert _listed(filtered) == ["get_issue", "list_issues"]


def test_an_unfiltered_server_advertises_everything():
    """No allowlist means the catalogue passes through untouched."""
    body = _catalogue("get_issue", "create_issue")
    assert filter_json_body(body, _server(tools=frozenset())) == body


def test_a_non_catalogue_reply_is_left_alone():
    """Only ``result.tools`` is ours; every other reply passes through."""
    body = json.dumps({"jsonrpc": "2.0", "id": 3, "result": {"content": "hello"}}).encode()
    assert json.loads(filter_json_body(body, _server()))["result"]["content"] == "hello"


def test_sse_catalogues_are_filtered_frame_by_frame():
    """Streamable HTTP may deliver the catalogue as an event, not a body."""
    stream = b"event: message\ndata: " + _catalogue("get_issue", "create_issue") + b"\n\n"
    sse = SseFilter(_server())
    out = sse.feed(stream) + sse.flush()
    assert b"create_issue" not in out
    assert b"get_issue" in out
    # The transport is the upstream's business and is left exactly as written.
    assert out.startswith(b"event: message\n")


def test_sse_filtering_survives_a_frame_split_across_chunks():
    """A catalogue arriving in pieces is still filtered, not passed through."""
    stream = b"data: " + _catalogue("get_issue", "create_issue") + b"\n\n"
    sse = SseFilter(_server())
    out = b"".join(sse.feed(stream[i : i + 7]) for i in range(0, len(stream), 7)) + sse.flush()
    assert b"create_issue" not in out
    assert b"get_issue" in out


def test_sse_passes_through_frames_that_are_not_catalogues():
    """Progress notifications and comments must survive untouched."""
    frame = b': keep-alive\n\ndata: {"jsonrpc":"2.0","method":"notifications/progress"}\n\n'
    sse = SseFilter(_server())
    assert sse.feed(frame) + sse.flush() == frame


# ── runtime wiring ────────────────────────────────────────────────────


def _prepare(*server_ids: str) -> tuple[list[str], dict[str, str]]:
    """Build the Claude Code invocation for a job."""
    return ClaudeCodeRuntime().prepare(
        workdir="/w",
        task_prompt="do it",
        model="glm-5.1",
        gateway_base_url=_GATEWAY,
        credential=_TOKEN,
        mcp_config=RuntimeMCPConfig(server_ids=server_ids),
    )


def _argv(*server_ids: str) -> list[str]:
    """Build only the argv."""
    return _prepare(*server_ids)[0]


def _mcp_config(argv: list[str]) -> dict:
    """Return the MCP config the invocation carries."""
    return json.loads(argv[argv.index("--mcp-config") + 1])


def test_repository_supplied_mcp_config_is_shut_out_even_with_no_servers():
    """The repository is untrusted input and may not add its own servers.

    Reproduced against the real CLI in a job's exact configuration (headless,
    ``bypassPermissions``, cwd = a checkout carrying a ``.mcp.json``): without
    this flag the CLI reports the repository's server alongside the platform's,
    with no approval step — the permission mode the sandbox needs for its own
    work is what removes the gate. Hence unconditional, rather than paired with
    having servers: a job with no MCP at all is where an injected one would be
    least expected.
    """
    argv = _argv()
    assert "--strict-mcp-config" in argv
    assert _mcp_config(argv) == {"mcpServers": {}}


def test_granted_servers_point_at_the_gateway_and_never_upstream():
    """The sandbox is given our proxy, and learns nothing about who answers."""
    config = _mcp_config(_argv("github"))["mcpServers"]
    assert config["github"]["url"] == f"{_GATEWAY}/v1/agent/mcp/github"
    assert config["github"]["type"] == "http"


def test_the_job_token_is_referenced_by_name_never_written_into_argv():
    """A secret in argv is readable from the process table and leaks into tails.

    The CLI expands ``${AGENT_MCP_TOKEN}`` from the environment, verified
    against the real binary: the proxy authenticates a request configured this
    way while the command line carries only the placeholder.
    """
    argv, env = _prepare("github")
    config = _mcp_config(argv)["mcpServers"]
    assert config["github"]["headers"] == {"Authorization": f"Bearer ${{{MCP_TOKEN_ENV}}}"}
    assert _TOKEN not in " ".join(argv)
    assert env[MCP_TOKEN_ENV] == _TOKEN


def test_the_gateway_base_url_is_normalized_once():
    """A base url already ending in /v1 must not produce /v1/v1."""
    argv, _ = ClaudeCodeRuntime().prepare(
        workdir="/w",
        task_prompt="do it",
        model="glm-5.1",
        gateway_base_url=f"{_GATEWAY}/v1",
        credential=_TOKEN,
        mcp_config=RuntimeMCPConfig(server_ids=("github",)),
    )
    assert _mcp_config(argv)["mcpServers"]["github"]["url"] == f"{_GATEWAY}/v1/agent/mcp/github"


def test_only_runtimes_that_can_also_shut_out_repo_config_report_mcp():
    """Codex is not wired up, and says so rather than dropping tools."""
    assert runtimes_supporting_mcp() == ["claude-code"]
    assert ClaudeCodeRuntime().capabilities().mcp is True
    assert CodexRuntime().capabilities().mcp is False


def test_a_runtime_without_a_mediated_path_refuses_rather_than_drops():
    """Codex fails closed, so nothing can hand it servers it would ignore."""
    with pytest.raises(RuntimeMCPUnavailableError):
        CodexRuntime().prepare(
            workdir="/w",
            task_prompt="do it",
            model="glm-5.1",
            gateway_base_url=_GATEWAY,
            credential=_TOKEN,
            mcp_config=RuntimeMCPConfig(server_ids=("github",)),
        )
