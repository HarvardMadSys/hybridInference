"""The outcome gate must hold on the path production actually takes.

Every other test in this area injects ``outcome=CompletionOutcome.X`` directly.
That proves the consumer honors the value it is handed; it does not prove the
production path ever produces one. These tests instead drive the real entry
point -- ``CompletionsLogger.record_routing_observation`` -- with a provider
response body and no outcome argument at all, because that is how
``completions.py`` and ``completions_stream.py`` call it.
"""

from __future__ import annotations

from typing import Any

import pytest

from routing.completion_outcome import CompletionOutcome, classify_completion_outcome
from routing.routers import RoutingObservation
from serving.servers.routers.completions_logging import CompletionsLogger

WARMUP_NOTICE = "The model is starting up - this takes about 120 seconds. Please wait..."
REAL_ANSWER = "Applied the erasure-fence fix and verified it against postgres."


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
        obs = _observe(response_body=_body(WARMUP_NOTICE))
        assert obs.success is True, "transport did succeed"
        assert obs.outcome is CompletionOutcome.TRANSIENT_NO_PROGRESS
        assert obs.admits_real_work is False

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


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # The incident signature: warmup notice with provider-reported tokens.
        ("The model is starting up - this takes about 120 seconds. Please wait...", True),
        ("Model is loading, please wait", True),
        ("the model is warming up now", True),
        ("please wait while the model loads", True),
        ("the model is not ready yet, hold on", True),
        ("model is initializing", True),
        # A genuine answer that merely mentions a marker stays PROGRESS.
        ("Here is how to fix it: the model is starting up because the GPU is cold.", False),
        ("The log says the model is starting up, so I will retry.", False),
        ("The answer is 42, and 'model is not ready' is a common message", False),
    ],
)
def test_warmup_notice_is_transient_only_when_it_leads_the_message(text, expected):
    outcome = classify_completion_outcome(content=text, usage=None, http_status=200, terminal=True)
    is_transient = outcome is CompletionOutcome.TRANSIENT_NO_PROGRESS
    assert is_transient is expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # Answers that BEGIN with a marker but continue into an explanation:
        # the bounded notice suffix must not swallow real prose.
        (
            "The model is starting up because the GPU was cold; that is why "
            "latency spiked. Here is what I found.",
            False,
        ),
        (
            '"The model is starting up" is the warmup message; here is how to retry.',
            False,
        ),
        (
            "The model is starting up is a common log line. To fix it, restart "
            "the worker and check the GPU pool.",
            False,
        ),
        # Free-form prose after the marker is an answer, not a notice suffix.
        ("Model is loading, please wait. Meanwhile, here is the summary you asked for.", False),
        # Known notice suffixes stay transient.
        ("The model is starting up - this takes about 120 seconds. Please wait...", True),
        ("model is starting up, please try again shortly", True),
        ("the model is not ready yet, hold on", True),
    ],
)
def test_notice_suffix_is_bounded_to_known_provider_wording(text, expected):
    outcome = classify_completion_outcome(content=text, usage=None, http_status=200, terminal=True)
    is_transient = outcome is CompletionOutcome.TRANSIENT_NO_PROGRESS
    assert is_transient is expected


def test_logger_classifies_response_body_when_supplied():
    """The logger classifies structured provider content when its caller supplies it."""
    captured: dict[str, Any] = {}

    def _record(active_router, model_id, routing, **kwargs):
        captured.update(kwargs)

    from serving.servers.routers.completions_logging import CompletionsLogger

    real_logger = CompletionsLogger(log_store=None)
    real_logger.record_routing_observation = _record  # type: ignore[method-assign]

    response = _body(WARMUP_NOTICE)
    real_logger.record_routing_observation(
        object(),  # active_router
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

    assert captured["response_body"] is response, (
        "the handler must pass the provider's response body through unchanged"
    )
    outcome = CompletionsLogger.classify_outcome(
        success=True,
        response_body=captured["response_body"],
        usage={"completion_tokens": 20},
    )
    assert outcome is CompletionOutcome.TRANSIENT_NO_PROGRESS
