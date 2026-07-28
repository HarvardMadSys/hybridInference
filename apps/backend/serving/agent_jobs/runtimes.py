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

The Claude Code mappings below were derived from a recorded
``claude -p --output-format stream-json`` run against this gateway, not from
documentation — see ``tests/fixtures/agent_runtime_streams/``.
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
    ) -> tuple[list[str], dict[str, str]]:
        """Return ``(argv, extra_env)`` to run this task headlessly."""
        raise NotImplementedError

    def parse_event(self, line: str) -> NormalizedEvent | None:
        """Map one output line onto a normalized event, or ``None`` to skip."""
        raise NotImplementedError

    def capabilities(self) -> RuntimeCapabilities:
        """Describe what this runtime supports."""
        return RuntimeCapabilities()

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

    def prepare(
        self,
        *,
        workdir: str,
        task_prompt: str,
        model: str,
        gateway_base_url: str,
        credential: str,
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
        ]
        env = {
            "ANTHROPIC_BASE_URL": gateway_base_url.rstrip("/").removesuffix("/v1"),
            "ANTHROPIC_API_KEY": credential,
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
            return NormalizedEvent(
                LIFECYCLE,
                {
                    "phase": event.get("subtype") or "system",
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
        """Claude Code is Tier 1: normalized events and a cost report."""
        return RuntimeCapabilities(resume=True, cost_report=True, normalized_events=True, tier=1)


class CodexRuntime(AgentRuntime):
    """OpenAI Codex headless (``codex exec --json``).

    Speaks the OpenAI surface, configured through a scratch ``CODEX_HOME`` so
    nothing touches the operator's own Codex config.
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
    ) -> tuple[list[str], dict[str, str]]:
        """Build the headless invocation and its environment."""
        argv = [self.binary, "exec", "--json", "--skip-git-repo-check", task_prompt]
        env = {
            "CODEX_MODEL": model,
            "CODEX_API_KEY": credential,
            "OPENAI_BASE_URL": gateway_base_url.rstrip("/").removesuffix("/v1") + "/v1",
            "OPENAI_API_KEY": credential,
        }
        return argv, env

    def config_toml(self, *, model: str, gateway_base_url: str) -> str:
        """Return the scratch CODEX_HOME config pointing Codex at the gateway."""
        base = gateway_base_url.rstrip("/").removesuffix("/v1")
        return "\n".join(
            [
                f'model = "{model}"',
                'model_provider = "hybridinference"',
                "",
                "[model_providers.hybridinference]",
                'name = "hybridinference"',
                f'base_url = "{base}/v1"',
                'env_key = "CODEX_API_KEY"',
                'wire_api = "chat"',
                "",
            ]
        )

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
    ) -> tuple[list[str], dict[str, str]]:
        """Expand the template into argv without ever invoking a shell."""
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


_REGISTRY: dict[str, type[AgentRuntime]] = {
    ClaudeCodeRuntime.name: ClaudeCodeRuntime,
    CodexRuntime.name: CodexRuntime,
}


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
