"""Tests for incident dedupe, recovery threading, and analysis delivery."""

from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from serving.triage.models import AlertEvent, TriageAnalysis
from serving.triage.runner import CodexRun
from serving.triage.service import TriageOverloadedError, TriageService, format_analysis
from serving.triage.store import TriageStore


class FakeSlack:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str | None]] = []

    async def post(self, text: str, *, thread_ts: str | None = None) -> str:
        self.messages.append((text, thread_ts))
        return thread_ts or f"ts-{len(self.messages)}"


class FakeRunner:
    def __init__(self, result: TriageAnalysis) -> None:
        self.result = result
        self.events: list[AlertEvent] = []

    async def run(self, event: AlertEvent) -> CodexRun:
        self.events.append(event)
        return CodexRun(thread_id="codex-thread-1", analysis=self.result)


def alert(status: str = "firing", alert_id: str | None = None) -> AlertEvent:
    return AlertEvent(
        alert_id=alert_id or f"alert-{status}",
        fingerprint="gateway:production:provider-openai",
        source="test",
        status=status,
        severity="error" if status == "firing" else "info",
        title="Provider failed" if status == "firing" else "Provider recovered",
        environment="production",
        occurred_at=datetime.now(timezone.utc),
        summary="Provider state changed",
        context={"provider": "openai"},
        slack_text=f"Slack {status}",
    )


def analysis() -> TriageAnalysis:
    return TriageAnalysis(
        summary="Inspect upstream status",
        classification="upstream_provider",
        confidence=0.7,
        impact="Some requests fail",
        evidence=[
            "<!channel> provider errors",
            "hyi-abcdefghijklmnopqrstuvwxyz0123456789",
        ],
        likely_cause="Upstream availability",
        recommended_actions=["Check provider dashboard"],
        issue_recommendation="none",
        draft_pr_recommendation="none",
    )


async def test_service_dedupes_and_posts_analysis_in_original_thread(tmp_path):
    store = TriageStore(tmp_path / "triage.sqlite3")
    await store.initialize()
    slack = FakeSlack()
    runner = FakeRunner(analysis())
    service = TriageService(store, slack, runner)

    first = await service.submit(alert())
    duplicate = await service.submit(alert())
    assert first.duplicate is False
    assert duplicate.duplicate is True
    assert len(slack.messages) == 1

    assert await service.process_one() is True
    assert await service.process_one() is True
    assert len(runner.events) == 1
    assert runner.events[0].fingerprint == alert().fingerprint
    assert slack.messages[1][1] == first.slack_thread_ts
    assert "Codex triage (DeepSeek)" in slack.messages[1][0]
    assert "&lt;!channel&gt;" in slack.messages[1][0]


async def test_service_posts_recovery_to_existing_incident_thread(tmp_path):
    store = TriageStore(tmp_path / "triage.sqlite3")
    await store.initialize()
    slack = FakeSlack()
    service = TriageService(store, slack, FakeRunner(analysis()))

    firing = await service.submit(alert())
    resolved = await service.submit(alert("resolved"))

    assert resolved.slack_thread_ts == firing.slack_thread_ts
    assert slack.messages[-1] == ("Slack resolved", firing.slack_thread_ts)
    incident = await store.get_incident(alert().fingerprint)
    assert incident is not None and incident.status == "resolved"


async def test_service_allows_new_incident_after_dedupe_window(tmp_path):
    store = TriageStore(tmp_path / "triage.sqlite3")
    await store.initialize()
    slack = FakeSlack()
    service = TriageService(store, slack, FakeRunner(analysis()))

    first = await service.submit(alert(alert_id="alert-1"))
    incident = await store.get_incident(alert().fingerprint)
    assert incident is not None
    with patch(
        "serving.triage.service.time.time",
        return_value=incident.created_at + 301,
    ):
        second = await service.submit(alert(alert_id="alert-2"))

    assert second.duplicate is False
    assert second.slack_thread_ts != first.slack_thread_ts

    # The first queued analysis still replies to its original Slack thread even
    # though the current incident row now points at the newer occurrence.
    assert await service.process_one() is True
    assert await service.process_one() is True
    assert slack.messages[2][1] == first.slack_thread_ts


async def test_service_rejects_new_analysis_when_queue_is_full(tmp_path):
    store = TriageStore(tmp_path / "triage.sqlite3")
    await store.initialize()
    slack = FakeSlack()
    service = TriageService(store, slack, FakeRunner(analysis()), max_pending_jobs=1)

    await service.submit(alert(alert_id="alert-1"))
    second = alert(alert_id="alert-2").model_copy(
        update={"fingerprint": "gateway:production:different"}
    )
    with pytest.raises(TriageOverloadedError):
        await service.submit(second)

    assert len(slack.messages) == 1


def test_format_analysis_is_bounded_and_mention_safe():
    message = format_analysis(analysis(), "thread-1")
    assert "<!channel>" not in message
    assert "&lt;!channel&gt;" in message
    assert "hyi-" not in message
    assert "[REDACTED]" in message
    assert "thread-1" in message
