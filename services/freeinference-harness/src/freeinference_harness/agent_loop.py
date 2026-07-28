"""Agent-loop conformance scenario executors (issue #1041, P-1 layer 1).

These executors drive a deterministic scripted agent loop against a target:
either the fake provider directly, or a gateway model routed to the fake
(``runtime -> gateway -> fake``). The same script definitions in
:mod:`freeinference_harness.agent_scripts` power both the fake's replay and
the assertions here, so protocol failures (splicing, normalization, retry,
truncation) are isolated from model-capability failures by construction.
"""

from __future__ import annotations

import json
import uuid
from typing import TYPE_CHECKING, Any

import httpx

from freeinference_harness.agent_scripts import (
    DS4_MALFORMED_ARGUMENTS,
    AgentScript,
    get_script,
    marker,
    run_marker,
)
from freeinference_harness.clients.anthropic import AnthropicMessagesClient
from freeinference_harness.config import load_tools_fixture

if TYPE_CHECKING:
    from freeinference_harness.clients.openai_compat import OpenAICompatClient
    from freeinference_harness.models import ScenarioConfig, TargetConfig

_DEFAULT_TOOLS_FIXTURE = "tools-claude-code-6.yaml"
_SYSTEM_PROMPT = "You are a deterministic conformance driver for agent-loop testing."


def _resolve_script(scenario: ScenarioConfig) -> AgentScript | None:
    """Resolves the scenario's script, or None when the id is unknown."""
    script_id = scenario.agent_script or scenario.scenario_id
    try:
        return get_script(script_id)
    except KeyError:
        return None


def _unknown_script_result(scenario: ScenarioConfig) -> dict[str, Any]:
    """Builds the failure result for an unknown script id."""
    return {
        "status": "fail",
        "failure_type": "unknown_agent_script",
        "http_status": None,
        "detail": f"Unknown agent script: {scenario.agent_script or scenario.scenario_id}",
        "observed": {},
    }


def _task_prompt(script: AgentScript, run_id: str | None = None) -> str:
    """Returns the marker-bearing user prompt for one script.

    The run marker scopes scripted first-attempt faults to this execution, so
    a long-lived fake provider re-arms them for every run instead of serving
    the fault once per process.
    """
    run = run_id or uuid.uuid4().hex
    return f"{marker(script.script_id)} {run_marker(run)} Execute the scripted agent task."


def _openai_tools(scenario: ScenarioConfig) -> list[dict[str, Any]]:
    """Loads the OpenAI-format tool definitions for a scenario."""
    return load_tools_fixture(scenario.tools_fixture or _DEFAULT_TOOLS_FIXTURE)


def _anthropic_tools(scenario: ScenarioConfig) -> list[dict[str, Any]]:
    """Converts the scenario's OpenAI-format tools into Anthropic format."""
    converted: list[dict[str, Any]] = []
    for tool in _openai_tools(scenario):
        function = tool.get("function") or {}
        converted.append(
            {
                "name": function.get("name", ""),
                "description": function.get("description", ""),
                "input_schema": function.get("parameters") or {"type": "object"},
            }
        )
    return converted


def _result(
    status: str,
    failure_type: str | None,
    detail: str,
    observed: dict[str, Any],
    http_status: int | None = 200,
) -> dict[str, Any]:
    """Builds a runner-compatible result dictionary."""
    return {
        "status": status,
        "failure_type": failure_type,
        "http_status": http_status,
        "detail": detail,
        "observed": observed,
    }


# ── OpenAI surface ─────────────────────────────────────────────────────


def run_agent_loop_openai(
    client: OpenAICompatClient,
    target: TargetConfig,
    scenario: ScenarioConfig,
) -> dict[str, Any]:
    """Drives one scripted agent loop over the OpenAI chat-completions surface."""
    script = _resolve_script(scenario)
    if script is None:
        return _unknown_script_result(scenario)
    expected = script.expected
    tools = _openai_tools(scenario)

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": _task_prompt(script)},
    ]
    observed: dict[str, Any] = {
        "script_id": script.script_id,
        "steps": [],
        "retried_after_429": False,
        "saw_429": False,
    }
    first_tool: dict[str, Any] | None = None
    final_stats: dict[str, Any] | None = None

    for _step in range(expected.max_steps + 1):
        try:
            stats = client.collect_stream(
                {
                    "model": target.model,
                    "messages": messages,
                    "tools": tools,
                    "tool_choice": "auto",
                    "max_tokens": scenario.max_tokens or 512,
                    "stream": True,
                },
                tolerate_truncation=expected.expect_truncated_stream,
            )
        except httpx.HTTPStatusError as exc:
            if (
                exc.response.status_code == 429
                and expected.retry_on_429
                and not observed["retried_after_429"]
            ):
                observed["saw_429"] = True
                observed["retried_after_429"] = True
                continue
            raise

        observed["steps"].append(
            {
                "done": stats["done"],
                "events": stats["events"],
                "truncated": stats.get("truncated", False),
                "saw_content": stats["saw_content"],
                "tool_calls": stats["tool_calls"],
                "content_preview": stats["full_content"][:160],
                "finish_reasons": stats["finish_reasons"],
                "stream_errors": stats.get("stream_errors") or [],
            }
        )

        # A gateway that has already flushed bytes cannot answer with an HTTP
        # 429, so it reports the rate limit as an in-stream error frame. Treat
        # that as the same signal, otherwise the retry path is never taken and
        # the scenario fails for the wrong reason.
        rate_limited = any(
            str(error.get("code")) == "429" for error in stats.get("stream_errors") or []
        )
        if rate_limited and expected.retry_on_429 and not observed["retried_after_429"]:
            observed["saw_429"] = True
            observed["saw_429_in_stream"] = True
            observed["retried_after_429"] = True
            continue

        if expected.expect_truncated_stream:
            return _check_truncated_openai(stats, observed)

        if stats["tool_calls"]:
            if first_tool is None:
                first_tool = stats["tool_calls"][0]
            call = stats["tool_calls"][0]
            call_id = call["id"] or "call_harness_0"
            messages.append(
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {"name": call["name"], "arguments": call["arguments"]},
                        }
                    ],
                }
            )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": '{"ok": true, "result": "synthetic harness tool result"}',
                }
            )
            continue

        final_stats = stats
        break

    if final_stats is None:
        return _result(
            "fail",
            "max_steps_exceeded",
            f"Agent loop did not reach a final answer within {expected.max_steps} steps.",
            observed,
        )

    observed["first_tool"] = first_tool
    if (
        not expected.expect_truncated_stream
        and not final_stats["done"]
        and final_stats["events"] == 0
    ):
        return _result(
            "fail",
            "empty_stream_no_done",
            "Stream closed with zero events and no [DONE]; an upstream error "
            "(e.g. 429) may have been swallowed into an empty 200 stream.",
            observed,
        )
    errors = _check_final_openai(script, first_tool, final_stats, observed)
    if errors:
        return _result(
            "fail",
            "agent_loop_conformance",
            "; ".join(errors),
            observed,
        )
    return _result(
        "pass",
        None,
        f"Agent-loop script '{script.script_id}' conformed on the OpenAI surface.",
        observed,
    )


def _check_truncated_openai(stats: dict[str, Any], observed: dict[str, Any]) -> dict[str, Any]:
    """Checks the mid-stream disconnect expectations on the OpenAI surface."""
    if stats["done"]:
        return _result(
            "fail",
            "unexpected_clean_termination",
            "Expected a truncated stream but the stream terminated with [DONE].",
            observed,
        )
    return _result(
        "pass",
        None,
        "Truncated stream surfaced without hanging; partial output and missing "
        "[DONE] were both observable.",
        observed,
    )


def _check_final_openai(
    script: AgentScript,
    first_tool: dict[str, Any] | None,
    final_stats: dict[str, Any],
    observed: dict[str, Any],
) -> list[str]:
    """Returns conformance errors for the completed OpenAI-surface loop."""
    expected = script.expected
    errors: list[str] = []

    if expected.expect_empty_final:
        if not final_stats["done"]:
            errors.append("empty-content stream did not terminate with [DONE]")
        if final_stats["saw_content"] or final_stats["tool_calls"]:
            errors.append("expected an empty final turn but saw visible output")
        return errors

    if expected.retry_on_429 and not observed.get("saw_429"):
        # Without this the scenario passes whenever the upstream simply never
        # rate-limits, so the retry path it exists to cover goes untested.
        errors.append("expected a 429 followed by a successful retry, but no 429 was observed")

    if not final_stats["done"]:
        errors.append("final stream did not terminate with [DONE]")

    if expected.tool_arguments_raw is not None:
        if first_tool is None:
            errors.append("expected a tool call but none was observed")
        else:
            if expected.tool_name and first_tool["name"] != expected.tool_name:
                errors.append(
                    f"tool name mismatch: expected {expected.tool_name!r}, "
                    f"got {first_tool['name']!r}"
                )
            if first_tool["arguments"] != expected.tool_arguments_raw:
                errors.append(
                    "spliced tool arguments mismatch: expected "
                    f"{expected.tool_arguments_raw!r}, got {first_tool['arguments']!r}"
                )

    if expected.final_text_contains and expected.final_text_contains not in (
        final_stats["full_content"] or ""
    ):
        errors.append(
            f"final content missing marker {expected.final_text_contains!r}: "
            f"{(final_stats['full_content'] or '')[:120]!r}"
        )
    return errors


# ── Anthropic surface ──────────────────────────────────────────────────


def _anthropic_client(target: TargetConfig) -> AnthropicMessagesClient:
    """Builds the Anthropic-surface client for a target."""
    return AnthropicMessagesClient(
        base_url=target.base_url,
        api_key=target.api_key,
        timeout_seconds=target.timeout_seconds,
        extra_headers=target.extra_headers or None,
    )


def run_agent_loop_anthropic(
    target: TargetConfig,
    scenario: ScenarioConfig,
) -> dict[str, Any]:
    """Drives one scripted agent loop over the Anthropic Messages surface."""
    script = _resolve_script(scenario)
    if script is None:
        return _unknown_script_result(scenario)
    expected = script.expected
    client = _anthropic_client(target)
    tools = _anthropic_tools(scenario)

    messages: list[dict[str, Any]] = [{"role": "user", "content": _task_prompt(script)}]
    observed: dict[str, Any] = {"script_id": script.script_id, "steps": []}
    first_tool: dict[str, Any] | None = None
    final_stats: dict[str, Any] | None = None

    for _step in range(expected.max_steps + 1):
        stats = client.collect_stream(
            {
                "model": target.model,
                "system": _SYSTEM_PROMPT,
                "messages": messages,
                "tools": tools,
                "max_tokens": scenario.max_tokens or 512,
            },
            tolerate_truncation=expected.expect_truncated_stream,
        )
        observed["steps"].append(
            {
                "done": stats["done"],
                "stop_reason": stats["stop_reason"],
                "content_blocks": stats["content_blocks"],
                "text_preview": stats["full_text"][:160],
            }
        )

        tool_blocks = [
            block for block in stats["content_blocks"] if block.get("type") == "tool_use"
        ]
        if tool_blocks:
            if first_tool is None:
                first_tool = tool_blocks[0]
            block = tool_blocks[0]
            block_id = block.get("id") or "toolu_harness_0"
            messages.append(
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": block_id,
                            "name": block.get("name") or "",
                            "input": block.get("input") if block.get("input") is not None else {},
                        }
                    ],
                }
            )
            messages.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": block_id,
                            "content": '{"ok": true, "result": "synthetic harness tool result"}',
                        }
                    ],
                }
            )
            continue

        final_stats = stats
        break

    if final_stats is None:
        return _result(
            "fail",
            "max_steps_exceeded",
            f"Agent loop did not reach a final answer within {expected.max_steps} steps.",
            observed,
        )

    observed["first_tool"] = first_tool
    errors = _check_final_anthropic(script, first_tool, final_stats)
    if errors:
        return _result("fail", "agent_loop_conformance", "; ".join(errors), observed)
    return _result(
        "pass",
        None,
        f"Agent-loop script '{script.script_id}' conformed on the Anthropic surface.",
        observed,
    )


def _check_final_anthropic(
    script: AgentScript,
    first_tool: dict[str, Any] | None,
    final_stats: dict[str, Any],
) -> list[str]:
    """Returns conformance errors for the completed Anthropic-surface loop."""
    expected = script.expected
    errors: list[str] = []

    if expected.expect_empty_final:
        if not final_stats["done"]:
            errors.append("empty-content stream did not reach message_stop")
        return errors

    if not final_stats["done"]:
        errors.append("final stream did not reach message_stop")

    if (
        expected.tool_input_object is not None or expected.anthropic_input_raw is not None
    ) and expected.tool_name:
        if first_tool is None:
            errors.append("expected a tool_use block but none was observed")
        else:
            if first_tool.get("name") != expected.tool_name:
                errors.append(
                    f"tool name mismatch: expected {expected.tool_name!r}, "
                    f"got {first_tool.get('name')!r}"
                )
            if expected.anthropic_input_raw is not None:
                if first_tool.get("input_raw") != expected.anthropic_input_raw:
                    errors.append(
                        "raw partial_json mismatch: expected "
                        f"{expected.anthropic_input_raw!r}, "
                        f"got {first_tool.get('input_raw')!r}"
                    )
            elif first_tool.get("input") != expected.tool_input_object:
                errors.append(
                    "normalized tool input mismatch: expected "
                    f"{expected.tool_input_object!r}, got {first_tool.get('input')!r} "
                    f"(raw={first_tool.get('input_raw')!r})"
                )

    if expected.final_text_contains and expected.final_text_contains not in (
        final_stats["full_text"] or ""
    ):
        errors.append(
            f"final text missing marker {expected.final_text_contains!r}: "
            f"{(final_stats['full_text'] or '')[:120]!r}"
        )
    return errors


def run_agent_loop_cancel(
    target: TargetConfig,
    scenario: ScenarioConfig,
) -> dict[str, Any]:
    """Aborts a stream client-side, then proves the target is not wedged.

    Black-box check for the client-abort path: reading a few deltas and
    closing the connection must not poison later requests on the same
    conversation (compare the gateway stream-abort incident history).
    """
    script = _resolve_script(scenario)
    if script is None:
        return _unknown_script_result(scenario)
    root = target.base_url.rstrip("/").removesuffix("/v1")
    headers = {
        "Authorization": f"Bearer {target.api_key}",
        "Content-Type": "application/json",
        **(target.extra_headers or {}),
    }
    task_prompt = _task_prompt(script)
    observed: dict[str, Any] = {"script_id": script.script_id, "aborted_after_events": 0}

    payload = {
        "model": target.model,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": task_prompt},
        ],
        "max_tokens": scenario.max_tokens or 512,
        "stream": True,
    }
    timeout = httpx.Timeout(connect=20.0, read=target.timeout_seconds, write=20.0, pool=20.0)
    with (
        httpx.Client(timeout=timeout) as client,
        client.stream(
            "POST", f"{root}/v1/chat/completions", headers=headers, json=payload
        ) as response,
    ):
        response.raise_for_status()
        for line in response.iter_lines():
            if line.startswith("data: ") and line[6:].strip() != "[DONE]":
                observed["aborted_after_events"] += 1
                if observed["aborted_after_events"] >= 2:
                    break
        # Exiting the context closes the connection mid-stream: the abort.

    if observed["aborted_after_events"] < 2:
        return _result(
            "fail",
            "cancel_setup_failed",
            "Stream ended before the driver could abort it mid-stream.",
            observed,
        )

    # Follow-up turn on the same conversation must still work.
    from freeinference_harness.clients.openai_compat import OpenAICompatClient

    follow_client = OpenAICompatClient(
        base_url=target.base_url,
        api_key=target.api_key,
        timeout_seconds=target.timeout_seconds,
        extra_headers=target.extra_headers or None,
    )
    follow = follow_client.collect_stream(
        {
            "model": target.model,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": task_prompt},
                {"role": "assistant", "content": "[aborted client-side]"},
                {"role": "user", "content": "continue"},
            ],
            "max_tokens": scenario.max_tokens or 512,
            "stream": True,
        }
    )
    observed["follow_up"] = {
        "done": follow["done"],
        "content_preview": follow["full_content"][:160],
    }
    expected_text = script.expected.final_text_contains
    if not follow["done"] or expected_text not in follow["full_content"]:
        return _result(
            "fail",
            "post_cancel_wedged",
            "Follow-up request after a client abort did not complete cleanly.",
            observed,
        )
    return _result(
        "pass",
        None,
        "Client abort mid-stream did not wedge the target; follow-up turn completed.",
        observed,
    )


def run_anthropic_poisoned_history(
    target: TargetConfig,
    scenario: ScenarioConfig,
) -> dict[str, Any]:
    """Replays the ds4 poison echo: a history whose tool_use input is a raw string.

    The gateway must normalize the poisoned ``input`` instead of rejecting the
    whole session with a 400 (the original incident wedged every later turn).
    """
    script = _resolve_script(scenario)
    if script is None:
        return _unknown_script_result(scenario)
    client = _anthropic_client(target)

    payload = {
        "model": target.model,
        "system": _SYSTEM_PROMPT,
        "max_tokens": scenario.max_tokens or 256,
        "tools": _anthropic_tools(scenario),
        "messages": [
            {"role": "user", "content": _task_prompt(script)},
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_poison_0",
                        "name": "bash",
                        # The poison: a client echoing the ds4 malformed
                        # arguments back as a raw string instead of an object.
                        "input": DS4_MALFORMED_ARGUMENTS,
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_poison_0",
                        "content": "command failed",
                    }
                ],
            },
        ],
    }
    response = client.create_message(payload)
    observed: dict[str, Any] = {
        "script_id": script.script_id,
        "http_status": response.status_code,
        "body_preview": response.text[:400],
    }
    if response.status_code != 200:
        return _result(
            "fail",
            "poisoned_history_rejected",
            f"Poisoned tool_use history returned HTTP {response.status_code}; "
            "the session-poison regression is back.",
            observed,
            http_status=response.status_code,
        )

    body = response.json()
    text = "".join(
        block.get("text", "")
        for block in body.get("content") or []
        if isinstance(block, dict) and block.get("type") == "text"
    )
    observed["final_text"] = text[:200]
    expected_text = script.expected.final_text_contains
    if expected_text and expected_text not in text:
        return _result(
            "fail",
            "agent_loop_conformance",
            f"Poisoned-history turn succeeded but final text missed {expected_text!r}.",
            observed,
        )
    return _result(
        "pass",
        None,
        "Poisoned tool_use history was normalized and the session continued.",
        observed,
    )


def run_anthropic_count_tokens(
    target: TargetConfig,
    scenario: ScenarioConfig,
) -> dict[str, Any]:
    """Checks the count_tokens endpoint used by agent runtimes for compaction."""
    client = _anthropic_client(target)
    payload = {
        "model": target.model,
        "system": _SYSTEM_PROMPT,
        "tools": _anthropic_tools(scenario),
        "messages": [{"role": "user", "content": "Count the tokens for this request."}],
    }
    response = client.count_tokens(payload)
    observed: dict[str, Any] = {
        "http_status": response.status_code,
        "body_preview": response.text[:200],
    }
    if response.status_code != 200:
        return _result(
            "fail",
            "count_tokens_error",
            f"count_tokens returned HTTP {response.status_code}.",
            observed,
            http_status=response.status_code,
        )
    try:
        body = response.json()
    except json.JSONDecodeError:
        return _result(
            "fail",
            "count_tokens_error",
            "count_tokens returned a non-JSON body.",
            observed,
        )
    input_tokens = body.get("input_tokens")
    observed["input_tokens"] = input_tokens
    if not isinstance(input_tokens, int) or input_tokens <= 0:
        return _result(
            "fail",
            "count_tokens_error",
            f"count_tokens returned an invalid input_tokens value: {input_tokens!r}.",
            observed,
        )
    return _result(
        "pass",
        None,
        f"count_tokens returned a positive estimate ({input_tokens}).",
        observed,
    )
