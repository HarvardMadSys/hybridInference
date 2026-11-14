"""The outcome gate must hold through the callers, not just the classifier.

The earlier suite supplied response_body by hand, which proved the classifier
works and nothing about whether production supplies a body. These tests assert
the wiring: each production call site must forward the response it already has.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from routing.completion_outcome import CompletionOutcome
from serving.servers.routers.completions_logging import CompletionsLogger

WARMUP_NOTICE = "The model is starting up - this takes about 120 seconds. Please wait..."
REAL_ANSWER = "Applied the erasure-fence fix and verified it against postgres."

_BACKEND = (
    Path(__file__).resolve().parents[3] / "apps" / "backend" / "serving" / "servers" / "routers"
)


def _body(content: str) -> dict:
    return {"choices": [{"message": {"role": "assistant", "content": content}}]}


class _Recorder:
    def __init__(self) -> None:
        self.observations: list = []

    def record_observation(self, obs) -> None:
        self.observations.append(obs)


def _observe(**kw):
    router = _Recorder()
    logger = CompletionsLogger.__new__(CompletionsLogger)
    logger.record_routing_observation(
        router,
        "model-1",
        None,
        request_id="req-1",
        ttft_ms=10.0,
        total_latency_ms=20.0,
        prompt_tokens=185_000,
        completion_tokens=kw.pop("completion_tokens", 0),
        **kw,
    )
    return router.observations[0]


class TestProductionCallSitesForwardTheBody:
    """A classifier that never receives a body cannot gate anything."""

    @pytest.mark.parametrize("module", ["completions.py", "completions_stream.py"])
    def test_call_site_passes_response_body(self, module: str) -> None:
        tree = ast.parse((_BACKEND / module).read_text())
        calls = [
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "record_routing_observation"
        ]
        assert calls, module + " has no record_routing_observation call"
        for call in calls:
            names = {kw.arg for kw in call.keywords}
            if "success" in names:
                success = next((kw.value for kw in call.keywords if kw.arg == "success"), None)
                # A literal success=False is the exception path: there is no
                # completed body to classify, and classify_outcome ignores it.
                if isinstance(success, ast.Constant) and success.value is False:
                    continue
            assert "response_body" in names, (
                module + ":" + str(call.lineno) + " omits response_body on a success path"
            )

    def test_a_warmup_body_with_output_tokens_does_not_admit(self) -> None:
        """The incident shape: a 200 whose only content is a startup notice."""
        obs = _observe(success=True, response_body=_body(WARMUP_NOTICE), completion_tokens=5)
        assert obs.success is True
        assert obs.outcome is CompletionOutcome.TRANSIENT_NO_PROGRESS
        assert obs.admits_real_work is False

    def test_a_real_answer_admits(self) -> None:
        obs = _observe(success=True, response_body=_body(REAL_ANSWER))
        assert obs.outcome is CompletionOutcome.PROGRESS
        assert obs.admits_real_work is True

    def test_a_tool_call_with_no_prose_and_no_usage_admits(self) -> None:
        """A structured tool call is real work even with no text and no usage."""
        body = {"choices": [{"message": {"tool_calls": [{"id": "1", "function": {"name": "x"}}]}}]}
        obs = _observe(success=True, response_body=body)
        assert obs.outcome is CompletionOutcome.PROGRESS
        assert obs.admits_real_work is True
