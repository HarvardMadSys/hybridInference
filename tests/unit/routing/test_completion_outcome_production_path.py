"""The outcome gate must hold on the path production actually takes.

Every other test in this area injects ``outcome=CompletionOutcome.X`` directly.
That proves the consumer honors the value it is handed; it does not prove the
production path ever produces one. These tests instead drive the real entry
point -- ``CompletionsLogger.record_routing_observation`` -- with a provider
response body and no outcome argument at all, because that is how
``completions.py`` and ``completions_stream.py`` call it.
"""

from __future__ import annotations

from routing.completion_outcome import CompletionOutcome
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
