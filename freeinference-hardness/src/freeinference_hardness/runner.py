"""Scenario runner for the standalone hardness harness."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

import httpx

from freeinference_hardness.clients.openai_compat import OpenAICompatClient
from freeinference_hardness.models import (
    AttemptResult,
    RunRecord,
    ScenarioConfig,
    ScenarioSummary,
    SuiteConfig,
    TargetConfig,
)


def build_run_id() -> str:
    """Builds a sortable run identifier."""
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


class HardnessRunner:
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
            elif scenario.scenario_type == "embedding_basic":
                result = self._run_embedding_basic(client, target)
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
        tool_calls = message.get("tool_calls")
        observed = {
            "finish_reason": choice.get("finish_reason"),
            "usage": response.get("usage"),
            "content_preview": content[:160] if isinstance(content, str) else content,
            "has_tool_calls": bool(tool_calls),
        }
        if content or tool_calls:
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
        if stats["saw_content"] or stats["saw_tool_calls"]:
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

    def _run_forced_tool_call(
        self,
        client: OpenAICompatClient,
        target: TargetConfig,
        scenario: ScenarioConfig,
    ) -> dict[str, Any]:
        """Runs a forced tool-call contract."""
        stats = client.collect_stream(
            {
                "model": target.model,
                "messages": [
                    {"role": "system", "content": "You are a tool-using assistant."},
                    {
                        "role": "user",
                        "content": "Use the tool immediately and do not answer in plain text.",
                    },
                ],
                "tools": [
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
                ],
                "tool_choice": {"type": "function", "function": {"name": "record_findings"}},
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
        if stats["saw_tool_calls"]:
            return {
                "status": "pass",
                "failure_type": None,
                "http_status": 200,
                "detail": "Forced tool-call stream emitted tool_calls.",
                "observed": stats,
            }
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
