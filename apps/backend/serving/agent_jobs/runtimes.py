"""Agent runtime adapters (issue #1041) — the BYOA half of BYOA x BYOM.

Mirrors the provider-adapter pattern in ``serving/adapters/``: there the
gateway adapts to a model provider's dialect, here the sandbox adapts to an
agent CLI's dialect. Both keep the dialect-specific parsing at the edge so the
rest of the system sees one normalized shape.

An adapter answers three questions:

- ``prepare`` — what command runs this task headlessly, and with what
  environment? The CLI is pinned in the sandbox image; the adapter only builds
  the invocation.
- ``parse_event`` — how does one line of the CLI's output map onto our
  normalized event kinds? **Unrecognized lines are never dropped**: they come
  back as a ``raw`` event so the UI degrades to a log tail instead of going
  silent when a CLI changes its format.
- ``capabilities`` — what can this runtime actually do (resume, report cost)?

Tiering (per the adjudicated design): Tier 1 runtimes get full normalization
and cost attribution; Tier 2 is a generic headless runner whose output flows
through as raw events, so a new agent costs nothing to support badly and can
be promoted when it earns it.

The Claude Code mappings below were derived from observed
``claude -p --output-format stream-json`` output, not from documentation. The
corresponding test data is a sanitized, synthetic contract fixture — see
``tests/fixtures/agent_runtime_streams/``.
"""

from __future__ import annotations

import json
import shlex
from dataclasses import dataclass, field
from typing import Any

# Normalized event kinds. Kept in lockstep with agent_job_events.event_type and
# the frontend's types.ts; anything else must be reported as ``raw``.
THINKING = "thinking"
MESSAGE = "message"
TOOL_USE = "tool_use"
TOOL_RESULT = "tool_result"
DIFF = "diff"
USAGE = "usage"
ERROR = "error"
LIFECYCLE = "lifecycle"
RAW = "raw"

# Where the job token is handed to the agent CLI for its MCP calls. The
# generated MCP config references this name rather than carrying the value,
# so the credential never becomes a process argument.
MCP_TOKEN_ENV = "AGENT_MCP_TOKEN"


@dataclass(frozen=True)
class NormalizedEvent:
    """One event in the shape the control plane and UI understand."""

    event_type: str
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RuntimeCapabilities:
    """What a runtime can do, so the platform does not assume."""

    resume: bool = False
    cost_report: bool = False
    normalized_events: bool = False
    tier: int = 2
    # Whether this adapter can point the CLI at the gateway's MCP proxy *and*
    # shut out every other MCP config source. Both halves or neither: a runtime
    # that could be given servers but not stopped from picking up the
    # repository's own would be worse than one with no MCP at all.
    mcp: bool = False


@dataclass(frozen=True)
class RuntimeMCPConfig:
    """Gateway-owned MCP server ids made available to one agent run.

    This boundary deliberately has no endpoint, header, or credential fields.
    A later broker implementation must resolve these opaque registry ids on
    the trusted gateway and expose only gateway-local endpoints to the
    sandbox; secrets must never become CLI arguments or runtime event data.
    """

    server_ids: tuple[str, ...] = ()


EMPTY_RUNTIME_MCP_CONFIG = RuntimeMCPConfig()


class RuntimeMCPUnavailableError(RuntimeError):
    """Raised when MCP is requested before a runtime has a mediated path."""


class AgentRuntime:
    """Base adapter. Subclasses override the three questions."""

    name = "generic"
    binary = ""

    def prepare(
        self,
        *,
        workdir: str,
        task_prompt: str,
        model: str,
        gateway_base_url: str,
        credential: str,
        mcp_config: RuntimeMCPConfig = EMPTY_RUNTIME_MCP_CONFIG,
    ) -> tuple[list[str], dict[str, str]]:
        """Return ``(argv, extra_env)`` to run this task headlessly."""
        raise NotImplementedError

    @staticmethod
    def _mcp_endpoints(
        config: RuntimeMCPConfig, *, gateway_base_url: str
    ) -> dict[str, dict[str, Any]]:
        """Resolve opaque server ids into gateway-local endpoints.

        The agent is told a URL on our own gateway, and nothing else: not the
        upstream address, not the credential that reaches it. Both stay on the
        gateway, which is what lets a sandbox with no egress use these servers
        at all.

        **The token is referenced, not embedded.** ``${AGENT_MCP_TOKEN}`` is
        expanded by the CLI from the environment, so the credential never
        becomes a process argument — where it would be readable from the
        process table and, worse, liable to be copied into an error tail or an
        event payload the owner can read. Verified against the real CLI: with
        the header written this way and the variable exported, the proxy
        authenticates the request and the argv carries only the placeholder.
        """
        base = gateway_base_url.rstrip("/").removesuffix("/v1")
        return {
            server_id: {
                "type": "http",
                "url": f"{base}/v1/agent/mcp/{server_id}",
                # The same credential the model calls use, so tools last
                # exactly as long as inference does: one fence governs both,
                # and a cancelled job loses them together.
                "headers": {"Authorization": f"Bearer ${{{MCP_TOKEN_ENV}}}"},
            }
            for server_id in config.server_ids
        }

    def parse_event(self, line: str) -> NormalizedEvent | None:
        """Map one output line onto a normalized event, or ``None`` to skip."""
        raise NotImplementedError

    def capabilities(self) -> RuntimeCapabilities:
        """Describe what this runtime supports."""
        return RuntimeCapabilities()

    def _require_empty_mcp_config(self, config: RuntimeMCPConfig) -> None:
        """Fail closed until this adapter has a gateway-mediated MCP path."""
        if config.server_ids:
            raise RuntimeMCPUnavailableError(
                f"runtime {self.name!r} cannot use MCP servers until the gateway broker is enabled"
            )

    @staticmethod
    def _raw(line: str, reason: str) -> NormalizedEvent:
        """Wrap an unrecognized line so nothing is silently lost."""
        return NormalizedEvent(RAW, {"text": line[:4000], "reason": reason})


class ClaudeCodeRuntime(AgentRuntime):
    """Claude Code headless (``claude -p --output-format stream-json``).

    Speaks the Anthropic Messages surface to the gateway, so ``credential``
    (the job's capability token) rides in ``ANTHROPIC_API_KEY``.
    """

    name = "claude-code"
    binary = "claude"

    # `type: "system"` covers two unrelated things: a few genuine milestones,
    # and progress telemetry. Only these are milestones. The distinction is not
    # cosmetic — the first real job on staging stored 412 lifecycle events, 408
    # of them `subtype: "thinking_tokens"`, so a counter became 408 rows in the
    # append-only log, 408 SSE frames, and 408 ticked-off steps in the UI.
    # An allowlist rather than a denylist: a CLI upgrade that invents another
    # counter must not be able to flood the stream just because nobody had
    # heard of it yet.
    MILESTONE_SUBTYPES = frozenset({"init", "compact_boundary"})

    def __init__(self) -> None:
        """Track which unclassified subtypes this run has already reported."""
        # One adapter instance per job (see get_runtime), so this is per-run
        # state — the suppression below cannot leak across jobs.
        self._reported_subtypes: set[str] = set()

    def prepare(
        self,
        *,
        workdir: str,
        task_prompt: str,
        model: str,
        gateway_base_url: str,
        credential: str,
        mcp_config: RuntimeMCPConfig = EMPTY_RUNTIME_MCP_CONFIG,
    ) -> tuple[list[str], dict[str, str]]:
        """Build the headless invocation and its environment."""
        argv = [
            self.binary,
            "-p",
            task_prompt,
            "--model",
            model,
            "--output-format",
            "stream-json",
            "--verbose",
            # A repository can commit .mcp.json and Claude otherwise starts
            # its stdio commands before the model's first turn. Reproduced
            # against the real CLI in this exact configuration: without this
            # flag the CLI reports the repository's own server alongside the
            # platform's, with no approval step, because the permission mode
            # below is precisely what removes the gate that would have caught
            # it. Unconditional, including for a job with no MCP servers —
            # which is where an injected one would be least expected.
            "--strict-mcp-config",
            # The sandbox IS the boundary, so an interactive permission prompt
            # inside it has nothing left to protect — it only guarantees the
            # agent cannot do the work. Without this the CLI denies every write
            # with "you haven't granted it yet", the job produces an empty
            # patch, and (worse) a model that ignores the error still reports
            # success. Found by the first real run, not by the fake.
            "--permission-mode",
            "bypassPermissions",
            # Paired with --strict-mcp-config above: that flag makes this the
            # *only* source of MCP servers, and this supplies the platform's.
            # Passed even when empty, so the pairing has no gap.
            "--mcp-config",
            json.dumps(
                {"mcpServers": self._mcp_endpoints(mcp_config, gateway_base_url=gateway_base_url)}
            ),
        ]
        env = {
            "ANTHROPIC_BASE_URL": gateway_base_url.rstrip("/").removesuffix("/v1"),
            "ANTHROPIC_API_KEY": credential,
            # Referenced by the MCP config above rather than written into it,
            # so the job token never appears in a process argument.
            MCP_TOKEN_ENV: credential,
            "ANTHROPIC_MODEL": model,
            "ANTHROPIC_SMALL_FAST_MODEL": model,
            # Telemetry and auto-update are refused explicitly rather than left
            # to the egress allowlist: defence in depth, and it keeps the
            # allowlist free of vendor endpoints.
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "DISABLE_TELEMETRY": "1",
            "DISABLE_AUTOUPDATER": "1",
        }
        return argv, env

    def parse_event(self, line: str) -> NormalizedEvent | None:
        """Normalize one stream-json line."""
        line = line.strip()
        if not line:
            return None
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return self._raw(line, "not json")
        if not isinstance(event, dict):
            return self._raw(line, "not an object")

        kind = event.get("type")
        if kind == "system":
            subtype = str(event.get("subtype") or "system").strip() or "system"
            if subtype not in self.MILESTONE_SUBTYPES:
                # Not discarded outright: the first occurrence is kept as a raw
                # diagnostic so a subtype nobody has classified yet is still
                # discoverable in the event log. Repeats are dropped, which is
                # what turns 408 rows into 1.
                if subtype in self._reported_subtypes:
                    return None
                self._reported_subtypes.add(subtype)
                return self._raw(line, f"unclassified system subtype {subtype!r}")
            return NormalizedEvent(
                LIFECYCLE,
                {
                    "phase": subtype,
                    "model": event.get("model"),
                    "runtime_version": event.get("claude_code_version"),
                },
            )
        if kind == "assistant":
            return self._from_assistant(event)
        if kind == "user":
            return self._from_user(event)
        if kind == "rate_limit_event":
            return NormalizedEvent(
                LIFECYCLE, {"phase": "rate_limit", "info": event.get("rate_limit_info")}
            )
        if kind == "result":
            return self._from_result(event)
        return self._raw(line, f"unknown type {kind!r}")

    def _from_assistant(self, event: dict[str, Any]) -> NormalizedEvent:
        """Map an assistant turn to its most significant block."""
        message = event.get("message") or {}
        blocks = message.get("content") or []
        usage = message.get("usage") or {}
        for block in blocks:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                return NormalizedEvent(
                    TOOL_USE,
                    {
                        "name": block.get("name"),
                        "id": block.get("id"),
                        "input": block.get("input"),
                        "usage": usage or None,
                    },
                )
            if block.get("type") == "thinking":
                return NormalizedEvent(THINKING, {"text": (block.get("thinking") or "")[:4000]})
        text = "".join(
            block.get("text", "")
            for block in blocks
            if isinstance(block, dict) and block.get("type") == "text"
        )
        return NormalizedEvent(MESSAGE, {"text": text[:4000], "usage": usage or None})

    def _from_user(self, event: dict[str, Any]) -> NormalizedEvent:
        """Map a tool result echoed back into the conversation."""
        blocks = (event.get("message") or {}).get("content") or []
        for block in blocks:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                content = block.get("content")
                return NormalizedEvent(
                    TOOL_RESULT,
                    {
                        "tool_use_id": block.get("tool_use_id"),
                        "is_error": bool(block.get("is_error")),
                        "content": _truncate(content),
                    },
                )
        return NormalizedEvent(RAW, {"text": "user turn without a tool result"})

    def _from_result(self, event: dict[str, Any]) -> NormalizedEvent:
        """Map the terminal result line."""
        if event.get("is_error"):
            return NormalizedEvent(
                ERROR,
                {
                    "subtype": event.get("subtype"),
                    "text": str(event.get("result") or "")[:2000],
                },
            )
        return NormalizedEvent(
            LIFECYCLE,
            {
                "phase": "result",
                "text": str(event.get("result") or "")[:4000],
                "num_turns": event.get("num_turns"),
                # Reported for observability only. The billing authority is
                # api_logs.agent_job_id — the platform never trusts a number
                # the agent computed about itself.
                "reported_cost_usd": event.get("total_cost_usd"),
            },
        )

    def capabilities(self) -> RuntimeCapabilities:
        """Claude Code is Tier 1: normalized events, a cost report, and MCP."""
        return RuntimeCapabilities(
            resume=True, cost_report=True, normalized_events=True, tier=1, mcp=True
        )


class CodexRuntime(AgentRuntime):
    """OpenAI Codex headless (``codex exec --json``).

    Speaks the OpenAI surface, configured through command-line provider flags
    so nothing writes to or depends on the operator's own Codex config.
    """

    name = "codex"
    binary = "codex"

    def prepare(
        self,
        *,
        workdir: str,
        task_prompt: str,
        model: str,
        gateway_base_url: str,
        credential: str,
        mcp_config: RuntimeMCPConfig = EMPTY_RUNTIME_MCP_CONFIG,
    ) -> tuple[list[str], dict[str, str]]:
        """Build the headless invocation and its environment.

        A non-empty ``mcp_config`` is refused, and :meth:`capabilities`
        reports ``mcp=False`` so the platform turns such a job away at creation
        rather than running one whose tools silently never appear. Codex
        configures MCP servers through ``mcp_servers.*`` config keys whose
        remote-URL form has moved between releases, and there is no flag
        equivalent to Claude Code's ``--strict-mcp-config`` verified against
        the pinned CLI. Shipping a guess would give a job either no tools or —
        far worse — the repository's own MCP servers. Wiring this up is a
        matter of verifying two flags against ``CODEX_VERSION`` in
        ``Dockerfile.agent-sandbox``, not of design.

        Mirrors the invocation this repository already runs in production
        (.github/workflows/codex-oncall.yml), because that one is known to work
        against this gateway. Three things it gets right that the first pass
        here did not:

        - the model rides ``--model``. The previous version set ``CODEX_MODEL``,
          which is not a variable the CLI reads, so the owner's model choice was
          silently dropped and Codex used whatever its own default was.
        - ``wire_api = "responses"``, not ``"chat"``. Codex removed chat wire
          support upstream (openai/codex#7782); our gateway serves
          ``/v1/responses`` and translates southbound.
        - the provider is configured with ``-c`` flags rather than a config file,
          so there is no scratch ``CODEX_HOME`` to write and nothing to leave
          behind. ``--ignore-user-config`` keeps the operator's own config out.
        """
        self._require_empty_mcp_config(mcp_config)
        base = gateway_base_url.rstrip("/").removesuffix("/v1")
        provider = "hybridinference"
        argv = [
            self.binary,
            "exec",
            "--json",
            "--skip-git-repo-check",
            # The operator's own Codex config must not reach a sandbox run.
            "--ignore-user-config",
            # The container is the boundary, so Codex's own sandbox only needs
            # to permit the work: writing the checked-out worktree. Same
            # reasoning as the Claude runtime's permission mode.
            "--sandbox",
            "workspace-write",
            "--model",
            model,
            "-c",
            f'model_provider="{provider}"',
            "-c",
            f'model_providers.{provider}.name="HybridInference"',
            "-c",
            f'model_providers.{provider}.base_url="{base}/v1"',
            "-c",
            f'model_providers.{provider}.env_key="CODEX_API_KEY"',
            "-c",
            f'model_providers.{provider}.wire_api="responses"',
            task_prompt,
        ]
        env = {
            "CODEX_API_KEY": credential,
            # Some code paths still read the OpenAI names; keep them consistent
            # rather than leaving a second, stale credential source.
            "OPENAI_BASE_URL": f"{base}/v1",
            "OPENAI_API_KEY": credential,
        }
        return argv, env

    def parse_event(self, line: str) -> NormalizedEvent | None:
        """Normalize one ``codex exec --json`` line."""
        line = line.strip()
        if not line:
            return None
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return self._raw(line, "not json")
        if not isinstance(event, dict):
            return self._raw(line, "not an object")

        kind = event.get("type") or ""
        item = event.get("item") or {}
        item_type = item.get("type") or item.get("item_type")

        if kind.startswith("item.") and item_type:
            if item_type == "command_execution" and kind == "item.completed":
                output = (
                    item.get("aggregated_output") or item.get("output") or item.get("stdout") or ""
                )
                exit_code = item.get("exit_code")
                return NormalizedEvent(
                    TOOL_RESULT,
                    {
                        "tool_use_id": item.get("id"),
                        "is_error": isinstance(exit_code, int) and exit_code != 0,
                        "content": _truncate(output),
                        "exit_code": exit_code,
                    },
                )
            if item_type in ("command_execution", "function_call", "tool_call"):
                return NormalizedEvent(
                    TOOL_USE,
                    {
                        "name": item.get("name") or item_type,
                        "id": item.get("id"),
                        "input": item.get("command") or item.get("arguments"),
                    },
                )
            if item_type in ("agent_message", "assistant_message"):
                return NormalizedEvent(MESSAGE, {"text": _truncate(item.get("text"))})
            if item_type in ("reasoning", "thinking"):
                return NormalizedEvent(THINKING, {"text": _truncate(item.get("text"))})
            if item_type in ("patch", "file_change"):
                return NormalizedEvent(DIFF, {"text": _truncate(item.get("changes"))})
        if kind in ("turn.completed", "session.completed", "thread.completed"):
            return NormalizedEvent(LIFECYCLE, {"phase": "result", "usage": event.get("usage")})
        if kind in ("error", "turn.failed"):
            return NormalizedEvent(ERROR, {"text": _truncate(event.get("error") or event)})
        return self._raw(line, f"unknown type {kind!r}")

    def capabilities(self) -> RuntimeCapabilities:
        """Codex is Tier 1 for events; cost comes from the gateway ledger."""
        return RuntimeCapabilities(resume=False, cost_report=False, normalized_events=True, tier=1)


class GenericRuntime(AgentRuntime):
    """Tier 2: run any headless CLI and stream its output as raw events.

    The point of Tier 2 is that a new agent costs nothing to support: it gets
    a working log tail immediately, and only earns a normalizing adapter once
    it proves worth the maintenance.
    """

    name = "generic"

    def __init__(self, command_template: str, *, binary: str = "") -> None:
        """Build from a shell-style template using ``{prompt}`` / ``{model}``."""
        self.command_template = command_template
        self.binary = binary or shlex.split(command_template)[0]

    def prepare(
        self,
        *,
        workdir: str,
        task_prompt: str,
        model: str,
        gateway_base_url: str,
        credential: str,
        mcp_config: RuntimeMCPConfig = EMPTY_RUNTIME_MCP_CONFIG,
    ) -> tuple[list[str], dict[str, str]]:
        """Expand the template into argv without ever invoking a shell.

        A Tier 2 runtime is any headless CLI, so there is no flag this adapter
        could know to pass and no way to shut out a repository's own config.
        A non-empty set is refused rather than dropped.
        """
        self._require_empty_mcp_config(mcp_config)
        argv = [
            part.replace("{prompt}", task_prompt).replace("{model}", model)
            for part in shlex.split(self.command_template)
        ]
        base = gateway_base_url.rstrip("/").removesuffix("/v1")
        env = {
            "OPENAI_BASE_URL": f"{base}/v1",
            "OPENAI_API_KEY": credential,
            "ANTHROPIC_BASE_URL": base,
            "ANTHROPIC_API_KEY": credential,
        }
        return argv, env

    def parse_event(self, line: str) -> NormalizedEvent | None:
        """Emit every non-empty line as a raw event."""
        line = line.rstrip()
        if not line:
            return None
        return NormalizedEvent(RAW, {"text": line[:4000]})


class PiRuntime(GenericRuntime):
    """Tier 2: the pi coding agent, streamed as raw JSON lines.

    pi ignores ``OPENAI_BASE_URL`` — its built-in ``openai`` provider goes
    straight to api.openai.com (verified against a local fake: zero hits, a
    real OpenAI 401). The supported route is a custom provider in
    ``~/.pi/agent/models.json``, so the sandbox image ships a reviewed
    ``pi-freeinference`` wrapper that writes that file from this environment
    and then ``exec``s the real CLI. The prompt stays in argv end to end;
    nothing user-controlled passes through a shell.

    ``--mode json`` output is structured (turn/message/usage events) but is
    deliberately passed through as ``raw``: promotion to a normalizing Tier 1
    adapter happens once real jobs prove the format worth pinning with
    recorded fixtures, per the tier design.
    """

    name = "pi"

    def __init__(self) -> None:
        """Fix the wrapper invocation; Tier 2 mechanics come from Generic."""
        super().__init__(
            "pi-freeinference --provider freeinference --model {model} "
            # pi has no native MCP client at the pinned version; MCP arrives
            # through executable extensions, including project-local ones.
            # Disable their discovery until the gateway supplies a reviewed
            # extension explicitly.
            "--mode json --no-session --no-extensions -p {prompt}",
            binary="pi-freeinference",
        )

    def prepare(
        self,
        *,
        workdir: str,
        task_prompt: str,
        model: str,
        gateway_base_url: str,
        credential: str,
        mcp_config: RuntimeMCPConfig = EMPTY_RUNTIME_MCP_CONFIG,
    ) -> tuple[list[str], dict[str, str]]:
        """Add the model id the wrapper writes into pi's provider config."""
        argv, env = super().prepare(
            workdir=workdir,
            task_prompt=task_prompt,
            model=model,
            gateway_base_url=gateway_base_url,
            credential=credential,
            mcp_config=mcp_config,
        )
        # models.json wants the model listed under the provider; the wrapper
        # cannot parse it back out of pi's argv without reimplementing pi's
        # option handling, so hand it over explicitly.
        env["PI_GATEWAY_MODEL"] = model
        return argv, env


class OpencodeRuntime(GenericRuntime):
    """Tier 2: OpenCode headless, streamed as raw JSON lines.

    Two verified facts shape the invocation. OpenCode ignores
    ``OPENAI_BASE_URL``, and its built-in ``openai`` provider speaks the
    Responses API; the ``opencode-freeinference`` wrapper instead declares a
    provider over the **bundled** ``@ai-sdk/openai-compatible`` package
    (chat-completions dialect, nothing downloaded at run time). And its
    startup fetch of the models.dev catalog hard-fails offline, so the
    wrapper disables it and declares the model in the config — without which
    every sandboxed run dies before the first request.

    ``--auto`` is the same lesson as Claude Code's bypassPermissions: the
    sandbox is the boundary, and an interactive permission gate inside it
    only guarantees the agent cannot do the work.
    """

    name = "opencode"

    def __init__(self) -> None:
        """Fix the wrapper invocation; Tier 2 mechanics come from Generic."""
        super().__init__(
            "opencode-freeinference run --format json --auto -m freeinference/{model} {prompt}",
            binary="opencode-freeinference",
        )

    def prepare(
        self,
        *,
        workdir: str,
        task_prompt: str,
        model: str,
        gateway_base_url: str,
        credential: str,
        mcp_config: RuntimeMCPConfig = EMPTY_RUNTIME_MCP_CONFIG,
    ) -> tuple[list[str], dict[str, str]]:
        """Add the model id the wrapper declares in OpenCode's config."""
        argv, env = super().prepare(
            workdir=workdir,
            task_prompt=task_prompt,
            model=model,
            gateway_base_url=gateway_base_url,
            credential=credential,
            mcp_config=mcp_config,
        )
        env["OPENCODE_GATEWAY_MODEL"] = model
        return argv, env


_REGISTRY: dict[str, type[AgentRuntime]] = {
    ClaudeCodeRuntime.name: ClaudeCodeRuntime,
    CodexRuntime.name: CodexRuntime,
    PiRuntime.name: PiRuntime,
    OpencodeRuntime.name: OpencodeRuntime,
}


def registered_runtimes() -> list[str]:
    """Runtime ids this deployment can actually run.

    The composer offers these rather than a hardcoded list, so the picker
    cannot advertise a runtime the backend would refuse at job creation.
    """
    return sorted(_REGISTRY)


def runtimes_supporting_mcp() -> list[str]:
    """Runtime ids that can be given MCP servers safely.

    Used by the composer and by job creation, so a job never reaches a sandbox
    having been promised tools its harness will not load.
    """
    return sorted(
        name for name, runtime_cls in _REGISTRY.items() if runtime_cls().capabilities().mcp
    )


def get_runtime(name: str, *, generic_command: str | None = None) -> AgentRuntime:
    """Return the adapter for a runtime id.

    Unknown ids fall back to :class:`GenericRuntime` when a command template is
    supplied, so a Tier 2 runtime needs no code change — and otherwise raise,
    because silently running the wrong agent is worse than refusing.
    """
    runtime_cls = _REGISTRY.get(name)
    if runtime_cls is not None:
        return runtime_cls()
    if generic_command:
        return GenericRuntime(generic_command)
    known = ", ".join(sorted(_REGISTRY))
    raise KeyError(f"Unknown agent runtime {name!r}. Known: {known}")


def _truncate(value: Any, limit: int = 4000) -> str:
    """Render any payload fragment as bounded text."""
    if value is None:
        return ""
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False, default=str)
    return value[:limit]
