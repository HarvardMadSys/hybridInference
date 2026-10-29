"""The outcome gate must hold on the path production actually takes.

Every other test in this area injects ``outcome=CompletionOutcome.X`` directly.
That proves the consumer honors the value it is handed; it does not prove the
production path ever produces one. These tests instead drive the real entry
point -- ``CompletionsLogger.record_routing_observation`` -- with a provider
response body and no outcome argument at all, because that is how
``completions.py`` and ``completions_stream.py`` call it.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from routing.completion_outcome import (
    CompletionOutcome,
    classify_stream_prefix,
    could_still_be_transient_prefix,
)
from routing.response_text import chunk_text, response_text
from routing.routers import RoutingObservation
from serving.servers.routers.completions_logging import CompletionsLogger

WARMUP_NOTICE = "The model is starting up - this takes about 120 seconds. Please wait..."
REAL_ANSWER = "Applied the erasure-fence fix and verified it against postgres."
# The maintainer's scenario: a short answer whose own first words are the warmup
# phrase must still classify TRANSIENT, so short-prefix matching stays intact.
SHORT_WARMUP_ANSWER = "The model is starting up; give it another second."
# A genuine answer that merely mentions warmup in a longer reply must be PROGRESS.
LONG_ANSWER_MENTIONING_WARMUP = (
    "Applied the fix and verified it. The vLLM server can take a minute after a "
    "restart; while the model is warming up, the probe returns the startup notice, "
    "which is why the first request failed. The retry then succeeded with the "
    "expected answer, so the route itself is healthy again."
)


def _body(content: str) -> dict:
    """An OpenAI-shaped non-streaming provider response body."""
    return {"choices": [{"message": {"role": "assistant", "content": content}}]}


class _Recorder:
    """Captures observations the way a real router would receive them."""

    def __init__(self) -> None:
        self.observations: list[RoutingObservation] = []

    def record_observation(self, obs: RoutingObservation) -> None:
        self.observations.append(obs)

    @property
    def only(self) -> RoutingObservation:
        assert len(self.observations) == 1, self.observations
        return self.observations[0]


def _observe(*, response_body, success=True, completion_tokens=0):
    """Drive the production observation path; never pass an outcome."""
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
        completion_tokens=completion_tokens,
        success=success,
        response_body=response_body,
    )
    return router.only


class TestProductionPathSuppliesOutcome:
    """The production path must classify; an unclassified observation must not admit."""

    def test_warmup_notice_is_not_progress(self):
        """A 200 whose only content is a warmup notice is not useful work."""
        obs = _observe(response_body=_body(WARMUP_NOTICE), completion_tokens=20)
        assert obs.success is True, "transport did succeed"
        assert obs.outcome is CompletionOutcome.TRANSIENT_NO_PROGRESS
        assert obs.admits_real_work is False

    def test_non_streaming_handler_passes_response_body_to_the_classifier(self):
        """The non-streaming handler must forward a warmup body with positive tokens."""
        captured: dict[str, Any] = {}

        def _record(active_router, model_id, routing, **kwargs):
            captured.update(kwargs)

        logger = CompletionsLogger(log_store=None)
        logger.record_routing_observation = _record  # type: ignore[method-assign]

        response = _body(WARMUP_NOTICE)
        logger.record_routing_observation(
            object(),
            "m1",
            None,
            request_id="req-nonstream-body",
            ttft_ms=None,
            total_latency_ms=10.0,
            prompt_tokens=10,
            completion_tokens=20,
            success=True,
            cached_tokens=None,
            response_body=response,
        )

        assert captured["response_body"] is response
        outcome = CompletionsLogger.classify_outcome(
            success=True,
            response_body=captured["response_body"],
            usage={"completion_tokens": 20},
        )
        assert outcome is CompletionOutcome.TRANSIENT_NO_PROGRESS

    def test_real_answer_admits_work(self):
        obs = _observe(response_body=_body(REAL_ANSWER))
        assert obs.outcome is CompletionOutcome.PROGRESS
        assert obs.admits_real_work is True

    def test_empty_body_is_not_progress(self):
        obs = _observe(response_body=_body(""))
        assert obs.outcome is CompletionOutcome.EMPTY
        assert obs.admits_real_work is False

    def test_tool_call_only_response_is_progress(self):
        response = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "tool_calls": [{"id": "call-1", "type": "function"}],
                    }
                }
            ]
        }
        obs = _observe(response_body=response)
        assert obs.outcome is CompletionOutcome.PROGRESS
        assert obs.admits_real_work is True

    def test_failure_path_reports_provider_error(self):
        obs = _observe(response_body=None, success=False)
        assert obs.outcome is CompletionOutcome.PROVIDER_ERROR
        assert obs.admits_real_work is False


class TestUnclassifiedObservationIsInert:
    """A caller that forgets to classify must not be read as evidence of work.

    This is the failure the incident fix exists to prevent. If the default ever
    flips back to inferring PROGRESS from transport success, a warmup notice
    silently regains its power to poison prefix-locality evidence.
    """

    def test_default_outcome_does_not_admit(self):
        obs = RoutingObservation(
            model_id="m",
            endpoint_id="e",
            ttft_ms=1.0,
            total_latency_ms=2.0,
            token_count=10,
            success=True,
        )
        assert obs.outcome is CompletionOutcome.UNKNOWN
        assert obs.admits_real_work is False

    def test_transport_success_alone_never_admits(self):
        """Explicitly: a 200 is not a completion."""
        obs = RoutingObservation(
            model_id="m",
            endpoint_id="e",
            ttft_ms=1.0,
            total_latency_ms=2.0,
            token_count=10,
            success=True,
            request_id="r",
            terminal=True,
        )
        assert obs.success is True
        assert obs.admits_real_work is False


class TestWarmupMarkerAnchoring:
    """The warmup regex must not flag genuine answers that mention warmup."""

    def test_short_answer_starting_with_warmup_is_still_transient(self):
        from routing.completion_outcome import CompletionOutcome, classify_completion_outcome

        outcome = classify_completion_outcome(
            content=SHORT_WARMUP_ANSWER, http_status=200, terminal=True
        )
        assert outcome is CompletionOutcome.TRANSIENT_NO_PROGRESS

    def test_long_answer_mentioning_warmup_is_progress(self):
        from routing.completion_outcome import CompletionOutcome, classify_completion_outcome

        assert len(LONG_ANSWER_MENTIONING_WARMUP) > 200
        outcome = classify_completion_outcome(
            content=LONG_ANSWER_MENTIONING_WARMUP, http_status=200, terminal=True
        )
        assert outcome is CompletionOutcome.PROGRESS

    @pytest.mark.parametrize(
        "text",
        [
            "Applied the fix. Note that the model is initializing on cold start.",
            'UI copy: "Model is loading, please wait..." with a progress spinner.',
            "That vLLM error means the model is not ready yet; retry after it loads.",
            "I wrote a function that retries while the model is warming up.",
        ],
    )
    def test_long_or_mid_text_containing_warmup_is_progress(self, text):
        from routing.completion_outcome import CompletionOutcome, classify_completion_outcome

        outcome = classify_completion_outcome(content=text, http_status=200, terminal=True)
        assert outcome is CompletionOutcome.PROGRESS, text

    @pytest.mark.parametrize(
        "text",
        [
            "The model is starting up - this takes about 120 seconds. Please wait...",
            "Model is warming up.",
            "model is initializing...",
        ],
    )
    def test_short_leading_warmup_is_transient(self, text):
        from routing.completion_outcome import CompletionOutcome, classify_completion_outcome

        outcome = classify_completion_outcome(content=text, http_status=200, terminal=True)
        assert outcome is CompletionOutcome.TRANSIENT_NO_PROGRESS, text

    def test_malformed_notice_suffix_is_bounded(self):
        """Repeated caller-influenced punctuation cannot backtrack exponentially."""
        from routing.completion_outcome import CompletionOutcome, classify_completion_outcome

        # At the defective regex's roughly 4x-per-two-characters growth rate,
        # this caller-influenced output would take minutes to classify.
        adversarial = "the model is starting up" + (". " * 30) + "x"
        start = time.perf_counter()
        outcome = classify_completion_outcome(content=adversarial, http_status=200, terminal=True)
        elapsed = time.perf_counter() - start

        assert outcome is CompletionOutcome.PROGRESS
        assert elapsed < 0.1, f"classification took {elapsed:.3f}s"


class TestStreamingOutcomePrefixes:
    def test_short_warmup_prefix_is_undecided_until_stream_end(self):
        assert classify_stream_prefix("The") is None
        assert classify_stream_prefix("The model is starting up...") is None
        assert (
            classify_stream_prefix("The model is starting up...", terminal=True)
            is CompletionOutcome.TRANSIENT_NO_PROGRESS
        )

    def test_substantive_continuation_resolves_a_warmup_prefix(self):
        assert (
            classify_stream_prefix("The model is starting up to answer the request")
            is CompletionOutcome.PROGRESS
        )

    @pytest.mark.parametrize(
        "text",
        [
            "Please wait while the model sta",
            "Model is loading, please",
            "The model is starting up - this takes about 12",
        ],
    )
    def test_regex_alternatives_and_suffixes_remain_possible_prefixes(self, text):
        assert could_still_be_transient_prefix(text)

    def test_tool_call_and_thinking_deltas_are_semantic_output(self):
        tool_call = (
            'data: {"choices":[{"delta":{"tool_calls":'
            '[{"id":"call-1","function":{"name":"lookup"}}]}}]}\n\n'
        )
        thinking = 'data: {"choices":[{"delta":{"thinking":"reasoning here"}}]}\n\n'

        assert chunk_text(tool_call) == "[tool_call]"
        assert chunk_text(thinking) == "reasoning here"

    def test_legacy_function_call_is_semantic_output_in_stream_and_response(self):
        function_call = {"name": "lookup", "arguments": "{}"}
        chunk = (
            'data: {"choices":[{"delta":{"function_call":'
            '{"name":"lookup","arguments":"{}"}}}] }\n\n'
        )

        assert chunk_text(chunk) == "[tool_call]"
        assert (
            response_text({"choices": [{"message": {"function_call": function_call}}]})
            == "[tool_call]"
        )
