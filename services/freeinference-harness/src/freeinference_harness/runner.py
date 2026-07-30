"""Scenario runner for the standalone FreeInference harness."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

import httpx

from freeinference_harness.agent_loop import (
    run_agent_loop_anthropic,
    run_agent_loop_cancel,
    run_agent_loop_openai,
    run_anthropic_count_tokens,
    run_anthropic_poisoned_history,
)
from freeinference_harness.clients.openai_compat import OpenAICompatClient
from freeinference_harness.config import load_tools_fixture
from freeinference_harness.models import (
    AttemptResult,
    RunRecord,
    ScenarioConfig,
    ScenarioSummary,
    SuiteConfig,
    TargetConfig,
)
from freeinference_harness.runtime_drivers import (
    run_runtime_claude_smoke,
    run_runtime_codex_smoke,
    run_runtime_pi_smoke,
)
from freeinference_harness.tool_validation import validate_tool_calls

# Default fallback tools when no fixture is specified (backwards compat).
_DEFAULT_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "record_findings",
            "description": "Record a short finding for regression testing.",
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {"type": "string"},
                },
                "required": ["summary"],
            },
        },
    }
]
_DEFAULT_FORCED_NAME = "record_findings"


def build_run_id() -> str:
    """Builds a sortable run identifier."""
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _load_scenario_tools(scenario: ScenarioConfig) -> list[dict[str, Any]]:
    """Loads tool definitions for a scenario (fixture or default)."""
    if scenario.tools_fixture:
        return load_tools_fixture(scenario.tools_fixture)
    return list(_DEFAULT_TOOLS)


def _user_prompt(scenario: ScenarioConfig) -> str:
    """Returns the user prompt for a tool-call scenario."""
    return scenario.user_prompt or "Use the tool immediately and do not answer in plain text."


class HarnessRunner:
    """Executes suites against black-box FreeInference targets."""

    def run(
        self,
        *,
        targets: list[TargetConfig],
        suite: SuiteConfig,
    ) -> RunRecord:
        """Runs a suite across the selected targets."""
        summaries: list[ScenarioSummary] = []
        for target in targets:
            client = OpenAICompatClient(
                base_url=target.base_url,
                api_key=target.api_key,
                timeout_seconds=target.timeout_seconds,
                extra_headers=target.extra_headers or None,
            )
            for scenario in suite.scenarios:
                summary = ScenarioSummary(
                    target_name=target.name,
                    target_model=target.model,
                    suite_name=suite.suite_name,
                    scenario_id=scenario.scenario_id,
                    scenario_type=scenario.scenario_type,
                )
                if not self._is_supported(target, scenario):
                    summary.attempts.append(
                        AttemptResult(
                            target_name=target.name,
                            target_model=target.model,
                            suite_name=suite.suite_name,
                            scenario_id=scenario.scenario_id,
                            scenario_type=scenario.scenario_type,
                            repetition=1,
                            status="skip",
                            failure_type="capability_skip",
                            latency_ms=None,
                            http_status=None,
                            detail=(
                                "Scenario skipped because the target does not declare all "
                                "required capabilities."
                            ),
                            observed={
                                "required_capabilities": list(scenario.required_capabilities),
                            },
                        )
                    )
                    summaries.append(summary)
                    continue

                repetitions = scenario.repetitions or target.sampling_count
                for repetition in range(1, repetitions + 1):
                    summary.attempts.append(
                        self._run_attempt(
                            client=client,
                            target=target,
                            suite=suite,
                            scenario=scenario,
                            repetition=repetition,
                        )
                    )
                summaries.append(summary)

        return RunRecord(
            run_id=build_run_id(),
            timestamp=datetime.now(UTC).isoformat(),
            suite_name=suite.suite_name,
            targets=targets,
            scenario_summaries=summaries,
        )

    def _is_supported(self, target: TargetConfig, scenario: ScenarioConfig) -> bool:
        """Returns whether a target supports the scenario's capabilities."""
        return all(target.capabilities.supports(name) for name in scenario.required_capabilities)

    def _run_attempt(
        self,
        *,
        client: OpenAICompatClient,
        target: TargetConfig,
        suite: SuiteConfig,
        scenario: ScenarioConfig,
        repetition: int,
    ) -> AttemptResult:
        """Runs one scenario attempt and returns a classified result."""
        started = time.perf_counter()
        try:
            if scenario.scenario_type == "non_stream_basic":
                result = self._run_non_stream_basic(client, target, scenario)
            elif scenario.scenario_type == "stream_basic":
                result = self._run_stream_basic(client, target, scenario)
            elif scenario.scenario_type == "forced_tool_call":
                result = self._run_forced_tool_call(client, target, scenario)
            elif scenario.scenario_type == "forced_tool_call_nonstream":
                result = self._run_forced_tool_call_nonstream(client, target, scenario)
            elif scenario.scenario_type == "auto_tool_call":
                result = self._run_auto_tool_call(client, target, scenario)
            elif scenario.scenario_type == "multi_turn_tool":
                result = self._run_multi_turn_tool(client, target, scenario)
            elif scenario.scenario_type == "embedding_basic":
                result = self._run_embedding_basic(client, target)
            elif scenario.scenario_type == "agent_loop_openai":
                result = run_agent_loop_openai(client, target, scenario)
            elif scenario.scenario_type == "agent_loop_anthropic":
                result = run_agent_loop_anthropic(target, scenario)
            elif scenario.scenario_type == "anthropic_poisoned_history":
                result = run_anthropic_poisoned_history(target, scenario)
            elif scenario.scenario_type == "anthropic_count_tokens":
                result = run_anthropic_count_tokens(target, scenario)
            elif scenario.scenario_type == "agent_loop_cancel":
                result = run_agent_loop_cancel(target, scenario)
            elif scenario.scenario_type == "runtime_claude_smoke":
                result = run_runtime_claude_smoke(target, scenario)
            elif scenario.scenario_type == "runtime_codex_smoke":
                result = run_runtime_codex_smoke(target, scenario)
            elif scenario.scenario_type == "runtime_pi_smoke":
                result = run_runtime_pi_smoke(target, scenario)
            else:
                return self._attempt(
                    target=target,
                    suite=suite,
                    scenario=scenario,
                    repetition=repetition,
                    status="skip",
                    failure_type="unknown_scenario",
                    latency_ms=0,
                    http_status=None,
                    detail=f"Scenario type '{scenario.scenario_type}' is not implemented yet.",
                    observed={},
                )
        except Exception as exc:
            status, failure_type, http_status, detail = self._classify_exception(exc)
            latency_ms = int((time.perf_counter() - started) * 1000)
            return self._attempt(
                target=target,
                suite=suite,
                scenario=scenario,
                repetition=repetition,
                status=status,
                failure_type=failure_type,
                latency_ms=latency_ms,
                http_status=http_status,
                detail=detail,
                observed={},
            )

        latency_ms = int((time.perf_counter() - started) * 1000)
        return self._attempt(
            target=target,
            suite=suite,
            scenario=scenario,
            repetition=repetition,
            latency_ms=latency_ms,
            **result,
        )

    def _attempt(
        self,
        *,
        target: TargetConfig,
        suite: SuiteConfig,
        scenario: ScenarioConfig,
        repetition: int,
        status: str,
        failure_type: str | None,
        latency_ms: int | None,
        http_status: int | None,
        detail: str,
        observed: dict[str, Any],
    ) -> AttemptResult:
        """Builds a single attempt result."""
        return AttemptResult(
            target_name=target.name,
            target_model=target.model,
            suite_name=suite.suite_name,
            scenario_id=scenario.scenario_id,
            scenario_type=scenario.scenario_type,
            repetition=repetition,
            status=status,
            failure_type=failure_type,
            latency_ms=latency_ms,
            http_status=http_status,
            detail=detail,
            observed=observed,
        )

    # ── Non-streaming basic ────────────────────────────────────────────

    def _run_non_stream_basic(
        self,
        client: OpenAICompatClient,
        target: TargetConfig,
        scenario: ScenarioConfig,
    ) -> dict[str, Any]:
        """Runs the non-streaming assistant contract."""
        response = client.create_chat_completion(
            {
                "model": target.model,
                "messages": [
                    {"role": "system", "content": "You are a concise assistant."},
                    {
                        "role": "user",
                        "content": "Reply with one short sentence about API regression tests.",
                    },
                ],
                "max_tokens": scenario.max_tokens or 96,
            }
        )
        choice = (response.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        content = message.get("content")
        reasoning_content = message.get("reasoning_content")
        tool_calls = message.get("tool_calls")
        observed = {
            "finish_reason": choice.get("finish_reason"),
            "usage": response.get("usage"),
            "content_preview": content[:160] if isinstance(content, str) else content,
            "has_reasoning_content": bool(reasoning_content),
            "has_tool_calls": bool(tool_calls),
        }
        if content or tool_calls or reasoning_content:
            return {
                "status": "pass",
                "failure_type": None,
                "http_status": 200,
                "detail": "Non-streaming response contained visible assistant output.",
                "observed": observed,
            }
        return {
            "status": "fail",
            "failure_type": "empty_assistant",
            "http_status": 200,
            "detail": "Non-streaming response returned no visible assistant content or tool calls.",
            "observed": observed,
        }

    # ── Streaming basic ────────────────────────────────────────────────

    def _run_stream_basic(
        self,
        client: OpenAICompatClient,
        target: TargetConfig,
        scenario: ScenarioConfig,
    ) -> dict[str, Any]:
        """Runs the basic streaming contract."""
        stats = client.collect_stream(
            {
                "model": target.model,
                "messages": [
                    {"role": "system", "content": "You are a careful assistant."},
                    {
                        "role": "user",
                        "content": (
                            "Think carefully if needed, then answer with exactly three short bullet "
                            "points about why adapter regression tests matter for reliability."
                        ),
                    },
                ],
                "max_tokens": scenario.max_tokens or 512,
                "stream": True,
            }
        )
        if not stats["done"]:
            return {
                "status": "fail",
                "failure_type": "missing_done",
                "http_status": 200,
                "detail": "Streaming response did not terminate with [DONE].",
                "observed": stats,
            }
        if stats["events"] <= 0:
            return {
                "status": "fail",
                "failure_type": "no_events",
                "http_status": 200,
                "detail": "Streaming response produced no data events.",
                "observed": stats,
            }
        if stats["saw_content"] or stats["saw_tool_calls"] or stats["saw_reasoning"]:
            return {
                "status": "pass",
                "failure_type": None,
                "http_status": 200,
                "detail": "Streaming response produced visible output and terminated correctly.",
                "observed": stats,
            }
        return {
            "status": "fail",
            "failure_type": "true_empty_terminal",
            "http_status": 200,
            "detail": "Streaming response reached [DONE] without visible content or tool calls.",
            "observed": stats,
        }

    # ── Forced tool call (streaming) ───────────────────────────────────

    def _run_forced_tool_call(
        self,
        client: OpenAICompatClient,
        target: TargetConfig,
        scenario: ScenarioConfig,
    ) -> dict[str, Any]:
        """Runs a forced tool-call contract (streaming) with fixture-based validation."""
        tools = _load_scenario_tools(scenario)
        forced_name = scenario.forced_tool_name or _DEFAULT_FORCED_NAME

        stats = client.collect_stream(
            {
                "model": target.model,
                "messages": [
                    {"role": "system", "content": "You are a tool-using assistant."},
                    {"role": "user", "content": _user_prompt(scenario)},
                ],
                "tools": tools,
                "tool_choice": {"type": "function", "function": {"name": forced_name}},
                "max_tokens": scenario.max_tokens or 256,
                "stream": True,
            }
        )

        if not stats["done"]:
            return {
                "status": "fail",
                "failure_type": "missing_done",
                "http_status": 200,
                "detail": "Forced tool-call stream did not terminate with [DONE].",
                "observed": stats,
            }
        if not stats["saw_tool_calls"]:
            if stats["saw_content"]:
                return {
                    "status": "fail",
                    "failure_type": "text_instead_of_tool_calls",
                    "http_status": 200,
                    "detail": "Forced tool-call request degraded into plain text output.",
                    "observed": stats,
                }
            return {
                "status": "fail",
                "failure_type": "true_empty_terminal",
                "http_status": 200,
                "detail": "Forced tool-call request ended without visible tool calls or text.",
                "observed": stats,
            }

        # Validate accumulated tool calls against fixture schemas.
        validation_errors = validate_tool_calls(stats["tool_calls"], tools, forced_name=forced_name)
        stats["validation_errors"] = validation_errors
        if validation_errors:
            return {
                "status": "fail",
                "failure_type": "tool_call_validation",
                "http_status": 200,
                "detail": f"Tool call validation failed: {'; '.join(validation_errors)}",
                "observed": stats,
            }
        return {
            "status": "pass",
            "failure_type": None,
            "http_status": 200,
            "detail": "Forced tool-call stream emitted validated tool_calls.",
            "observed": stats,
        }

    # ── Forced tool call (non-streaming) ───────────────────────────────

    def _run_forced_tool_call_nonstream(
        self,
        client: OpenAICompatClient,
        target: TargetConfig,
        scenario: ScenarioConfig,
    ) -> dict[str, Any]:
        """Runs a forced tool-call contract (non-streaming) with validation."""
        tools = _load_scenario_tools(scenario)
        forced_name = scenario.forced_tool_name or _DEFAULT_FORCED_NAME

        response = client.create_chat_completion(
            {
                "model": target.model,
                "messages": [
                    {"role": "system", "content": "You are a tool-using assistant."},
                    {"role": "user", "content": _user_prompt(scenario)},
                ],
                "tools": tools,
                "tool_choice": {"type": "function", "function": {"name": forced_name}},
                "max_tokens": scenario.max_tokens or 256,
            }
        )

        choice = (response.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        raw_tool_calls = message.get("tool_calls") or []

        # Normalize to the same shape as streaming accumulator output.
        tool_calls = [
            {
                "id": tc.get("id", ""),
                "name": (tc.get("function") or {}).get("name", ""),
                "arguments": (tc.get("function") or {}).get("arguments", ""),
            }
            for tc in raw_tool_calls
        ]

        observed: dict[str, Any] = {
            "finish_reason": choice.get("finish_reason"),
            "usage": response.get("usage"),
            "tool_calls": tool_calls,
            "has_tool_calls": bool(tool_calls),
            "has_content": bool(message.get("content")),
        }

        if not tool_calls:
            if message.get("content"):
                return {
                    "status": "fail",
                    "failure_type": "text_instead_of_tool_calls",
                    "http_status": 200,
                    "detail": "Forced tool-call (non-stream) degraded into plain text.",
                    "observed": observed,
                }
            return {
                "status": "fail",
                "failure_type": "empty_assistant",
                "http_status": 200,
                "detail": "Forced tool-call (non-stream) returned no tool calls or text.",
                "observed": observed,
            }

        validation_errors = validate_tool_calls(tool_calls, tools, forced_name=forced_name)
        observed["validation_errors"] = validation_errors
        if validation_errors:
            return {
                "status": "fail",
                "failure_type": "tool_call_validation",
                "http_status": 200,
                "detail": f"Tool call validation failed: {'; '.join(validation_errors)}",
                "observed": observed,
            }
        return {
            "status": "pass",
            "failure_type": None,
            "http_status": 200,
            "detail": "Forced tool-call (non-stream) returned validated tool_calls.",
            "observed": observed,
        }

    # ── Auto tool call ─────────────────────────────────────────────────

    def _run_auto_tool_call(
        self,
        client: OpenAICompatClient,
        target: TargetConfig,
        scenario: ScenarioConfig,
    ) -> dict[str, Any]:
        """Runs tool_choice='auto': model decides whether to call a tool."""
        tools = _load_scenario_tools(scenario)

        stats = client.collect_stream(
            {
                "model": target.model,
                "messages": [
                    {"role": "system", "content": "You are a tool-using assistant."},
                    {"role": "user", "content": _user_prompt(scenario)},
                ],
                "tools": tools,
                "tool_choice": "auto",
                "max_tokens": scenario.max_tokens or 512,
                "stream": True,
            }
        )

        if not stats["done"]:
            return {
                "status": "fail",
                "failure_type": "missing_done",
                "http_status": 200,
                "detail": "Auto tool-call stream did not terminate with [DONE].",
                "observed": stats,
            }

        # Auto mode: the model might return text or tool calls -- both are valid.
        # But if it called tools, validate them.
        if stats["saw_tool_calls"] and stats["tool_calls"]:
            validation_errors = validate_tool_calls(stats["tool_calls"], tools)
            stats["validation_errors"] = validation_errors
            if validation_errors:
                return {
                    "status": "fail",
                    "failure_type": "tool_call_validation",
                    "http_status": 200,
                    "detail": f"Auto tool-call validation failed: {'; '.join(validation_errors)}",
                    "observed": stats,
                }
            return {
                "status": "pass",
                "failure_type": None,
                "http_status": 200,
                "detail": "Auto tool-call stream emitted validated tool_calls.",
                "observed": stats,
            }

        if stats["saw_content"]:
            return {
                "status": "pass",
                "failure_type": None,
                "http_status": 200,
                "detail": "Auto tool-call stream produced text content (model chose not to call tools).",
                "observed": stats,
            }

        return {
            "status": "fail",
            "failure_type": "true_empty_terminal",
            "http_status": 200,
            "detail": "Auto tool-call stream ended without tool calls or text.",
            "observed": stats,
        }

    # ── Multi-turn tool call ───────────────────────────────────────────

    def _run_multi_turn_tool(
        self,
        client: OpenAICompatClient,
        target: TargetConfig,
        scenario: ScenarioConfig,
    ) -> dict[str, Any]:
        """Two-turn tool call: get tool call, send synthetic result, get final response."""
        tools = _load_scenario_tools(scenario)
        forced_name = scenario.forced_tool_name or _DEFAULT_FORCED_NAME

        # Turn 1: force a tool call.
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": "You are a tool-using assistant."},
            {"role": "user", "content": _user_prompt(scenario)},
        ]
        turn1 = client.collect_stream(
            {
                "model": target.model,
                "messages": messages,
                "tools": tools,
                "tool_choice": {"type": "function", "function": {"name": forced_name}},
                "max_tokens": scenario.max_tokens or 256,
                "stream": True,
            }
        )

        if not turn1["done"] or not turn1["saw_tool_calls"] or not turn1["tool_calls"]:
            return {
                "status": "fail",
                "failure_type": "turn1_no_tool_call",
                "http_status": 200,
                "detail": "Multi-turn: first turn did not produce a tool call.",
                "observed": {"turn1": turn1},
            }

        # Validate turn 1 tool calls.
        validation_errors = validate_tool_calls(turn1["tool_calls"], tools, forced_name=forced_name)
        if validation_errors:
            return {
                "status": "fail",
                "failure_type": "tool_call_validation",
                "http_status": 200,
                "detail": f"Multi-turn turn1 validation failed: {'; '.join(validation_errors)}",
                "observed": {"turn1": turn1, "validation_errors": validation_errors},
            }

        # Build turn 2 messages: original + assistant tool call + tool result.
        tc = turn1["tool_calls"][0]
        messages.append(
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": tc["id"] or "call_harness_0",
                        "type": "function",
                        "function": {"name": tc["name"], "arguments": tc["arguments"]},
                    }
                ],
            }
        )
        messages.append(
            {
                "role": "tool",
                "tool_call_id": tc["id"] or "call_harness_0",
                "content": '{"status": "ok", "result": "Synthetic harness result for regression testing."}',
            }
        )

        # Turn 2: model should produce a final text response (or another tool call).
        turn2 = client.collect_stream(
            {
                "model": target.model,
                "messages": messages,
                "tools": tools,
                "tool_choice": "auto",
                "max_tokens": scenario.max_tokens or 512,
                "stream": True,
            }
        )

        observed: dict[str, Any] = {"turn1": turn1, "turn2": turn2}

        if not turn2["done"]:
            return {
                "status": "fail",
                "failure_type": "turn2_missing_done",
                "http_status": 200,
                "detail": "Multi-turn: second turn did not terminate with [DONE].",
                "observed": observed,
            }

        if turn2["saw_content"] or turn2["saw_tool_calls"]:
            # If turn 2 has tool calls, validate them too.
            if turn2["saw_tool_calls"] and turn2["tool_calls"]:
                t2_errors = validate_tool_calls(turn2["tool_calls"], tools)
                observed["turn2_validation_errors"] = t2_errors
                if t2_errors:
                    return {
                        "status": "fail",
                        "failure_type": "tool_call_validation",
                        "http_status": 200,
                        "detail": f"Multi-turn turn2 validation failed: {'; '.join(t2_errors)}",
                        "observed": observed,
                    }
            return {
                "status": "pass",
                "failure_type": None,
                "http_status": 200,
                "detail": "Multi-turn tool call round-trip completed successfully.",
                "observed": observed,
            }

        return {
            "status": "fail",
            "failure_type": "turn2_empty",
            "http_status": 200,
            "detail": "Multi-turn: second turn produced no content or tool calls.",
            "observed": observed,
        }

    # ── Embeddings ─────────────────────────────────────────────────────

    def _run_embedding_basic(
        self,
        client: OpenAICompatClient,
        target: TargetConfig,
    ) -> dict[str, Any]:
        """Runs the basic embeddings contract."""
        response = client.create_embeddings(
            {
                "model": target.model,
                "input": "Adapter regression tests help catch public API regressions.",
            }
        )
        data = response.get("data") or []
        embedding = data[0].get("embedding") if data else None
        observed = {
            "usage": response.get("usage"),
            "embedding_length": len(embedding) if isinstance(embedding, list) else None,
        }
        if (
            isinstance(embedding, list)
            and embedding
            and all(isinstance(value, int | float) for value in embedding)
        ):
            return {
                "status": "pass",
                "failure_type": None,
                "http_status": 200,
                "detail": "Embeddings response returned a non-empty float vector.",
                "observed": observed,
            }
        return {
            "status": "fail",
            "failure_type": "invalid_embedding_shape",
            "http_status": 200,
            "detail": "Embeddings response did not contain a non-empty float vector.",
            "observed": observed,
        }

    # ── Exception classification ───────────────────────────────────────

    def _classify_exception(self, exc: Exception) -> tuple[str, str, int | None, str]:
        """Maps exceptions into scored or non-scored harness outcomes."""
        name = exc.__class__.__name__
        response = getattr(exc, "response", None)
        status_code = getattr(exc, "status_code", None)
        if status_code is None and response is not None:
            status_code = getattr(response, "status_code", None)

        if isinstance(exc, httpx.TimeoutException) or "Timeout" in name:
            return ("fail", "timeout", status_code, f"Request timed out: {exc}")

        if isinstance(exc, httpx.HTTPStatusError) or status_code is not None:
            assert isinstance(status_code, int)
            if status_code == 429:
                return ("skip", "rate_limited", status_code, f"Request was rate limited: {exc}")
            if 400 <= status_code < 500:
                return ("skip", "client_error", status_code, f"Client-visible 4xx response: {exc}")
            return ("fail", "server_error", status_code, f"Server-visible 5xx response: {exc}")

        if isinstance(exc, httpx.RequestError):
            return ("fail", "network_error", status_code, f"Network error: {exc}")

        return ("fail", "unexpected_error", status_code, f"Unexpected exception: {exc}")
