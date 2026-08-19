"""Tests for incident dedupe, recovery threading, and dispatch hand-off."""

from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from serving.oncall.models import AlertEvent, OnCallAnalysis
from serving.oncall.service import OnCallOverloadedError, OnCallService, format_analysis
from serving.oncall.store import OnCallStore


class FakeSlack:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str | None]] = []

    async def post(self, text: str, *, thread_ts: str | None = None) -> str:
        self.messages.append((text, thread_ts))
        return thread_ts or f"ts-{len(self.messages)}"


class FakeDispatcher:
    def __init__(
        self,
        fail_times: int = 0,
        confirm_fail_times: int = 0,
        run_appears_after_polls: int | None = 0,
    ) -> None:
        self.dispatched: list[tuple[AlertEvent, str]] = []
        self.confirm_polls = 0
        self._fail_times = fail_times
        self._confirm_fail_times = confirm_fail_times
        # Number of polls that must run before a run "appears"; None means
        # it never appears.
        self._run_appears_after_polls = run_appears_after_polls

    async def dispatch(self, event: AlertEvent, slack_thread_ts: str) -> None:
        if self._fail_times > 0:
            self._fail_times -= 1
            raise RuntimeError("github unavailable")
        self.dispatched.append((event, slack_thread_ts))

    async def latest_run_id(self) -> int | None:
        return 41

    async def confirm_run_started(self, *, before_run_id, dispatched_at):
        self.confirm_polls += 1
        if self._confirm_fail_times > 0:
            self._confirm_fail_times -= 1
            raise RuntimeError("github run list unavailable")
        if (
            self._run_appears_after_polls is not None
            and self.confirm_polls > self._run_appears_after_polls
        ):
            return True, "https://github.com/example/actions/runs/42"
        return False, None


class FakeClock:
    def __init__(self, start: float) -> None:
        self.value = start

    def __call__(self) -> float:
        return self.value

    def advance(self, delta: float) -> None:
        self.value += delta


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


def analysis() -> OnCallAnalysis:
    return OnCallAnalysis(
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


async def test_service_dedupes_and_dispatches_to_github_once(tmp_path):
    store = OnCallStore(tmp_path / "oncall.sqlite3")
    await store.initialize()
    slack = FakeSlack()
    dispatcher = FakeDispatcher()
    service = OnCallService(store, slack, dispatcher)

    first = await service.submit(alert())
    duplicate = await service.submit(alert())
    assert first.duplicate is False
    assert duplicate.duplicate is True
    assert len(slack.messages) == 1

    # One iteration dispatches and parks the job; the next confirms that a
    # workflow run appeared; then the queue is idle.
    assert await service.process_one() is True
    assert await service.process_one() is True
    assert await service.process_one() is False
    assert len(dispatcher.dispatched) == 1
    dispatched_event, thread_ts = dispatcher.dispatched[0]
    assert dispatched_event.fingerprint == alert().fingerprint
    assert thread_ts == first.slack_thread_ts
    assert await store.job_counts() == {"queued": 0, "running": 0, "done": 1, "failed": 0}


async def test_service_posts_recovery_to_existing_incident_thread(tmp_path):
    store = OnCallStore(tmp_path / "oncall.sqlite3")
    await store.initialize()
    slack = FakeSlack()
    service = OnCallService(store, slack, FakeDispatcher())

    firing = await service.submit(alert())
    resolved = await service.submit(alert("resolved"))

    assert resolved.slack_thread_ts == firing.slack_thread_ts
    assert slack.messages[-1] == ("Slack resolved", firing.slack_thread_ts)
    incident = await store.get_incident(alert().fingerprint)
    assert incident is not None and incident.status == "resolved"
    assert incident.alert_id == "alert-resolved"


async def test_service_dedupes_recovery_retries(tmp_path):
    store = OnCallStore(tmp_path / "oncall.sqlite3")
    await store.initialize()
    slack = FakeSlack()
    service = OnCallService(store, slack, FakeDispatcher())

    firing = await service.submit(alert())
    recovery = alert("resolved", alert_id="recovery-1")
    first = await service.submit(recovery)
    window_retry = await service.submit(alert("resolved", alert_id="recovery-2"))
    incident = await store.get_incident(alert().fingerprint)
    assert incident is not None
    with patch(
        "serving.oncall.service.time.time",
        return_value=incident.updated_at + 301,
    ):
        id_retry = await service.submit(recovery)

    assert first.duplicate is False
    assert window_retry.duplicate is True
    assert id_retry.duplicate is True
    assert first.slack_thread_ts == firing.slack_thread_ts
    assert len(slack.messages) == 2


async def test_service_dedupes_unmatched_recovery_retries(tmp_path):
    store = OnCallStore(tmp_path / "oncall.sqlite3")
    await store.initialize()
    slack = FakeSlack()
    service = OnCallService(store, slack, FakeDispatcher())

    recovery = alert("resolved", alert_id="recovery-1")
    first = await service.submit(recovery)
    duplicate = await service.submit(recovery)

    assert first.duplicate is False
    assert duplicate.duplicate is True
    assert duplicate.slack_thread_ts == first.slack_thread_ts
    assert slack.messages == [("Slack resolved", None)]
    incident = await store.get_incident(recovery.fingerprint)
    assert incident is not None and incident.status == "resolved"
    assert incident.alert_id == recovery.alert_id


async def test_service_allows_new_incident_after_dedupe_window(tmp_path):
    store = OnCallStore(tmp_path / "oncall.sqlite3")
    await store.initialize()
    slack = FakeSlack()
    dispatcher = FakeDispatcher()
    service = OnCallService(store, slack, dispatcher)

    first = await service.submit(alert(alert_id="alert-1"))
    incident = await store.get_incident(alert().fingerprint)
    assert incident is not None
    with patch(
        "serving.oncall.service.time.time",
        return_value=incident.created_at + 301,
    ):
        second = await service.submit(alert(alert_id="alert-2"))

    assert second.duplicate is False
    assert second.slack_thread_ts != first.slack_thread_ts

    # Each queued hand-off still targets its own original Slack thread even
    # though the current incident row now points at the newer occurrence.
    assert await service.process_one() is True
    assert await service.process_one() is True
    assert [ts for _, ts in dispatcher.dispatched] == [
        first.slack_thread_ts,
        second.slack_thread_ts,
    ]


async def test_service_rejects_new_analysis_when_queue_is_full(tmp_path):
    store = OnCallStore(tmp_path / "oncall.sqlite3")
    await store.initialize()
    slack = FakeSlack()
    service = OnCallService(store, slack, FakeDispatcher(), max_pending_jobs=1)

    await service.submit(alert(alert_id="alert-1"))
    second = alert(alert_id="alert-2").model_copy(
        update={"fingerprint": "gateway:production:different"}
    )
    with pytest.raises(OnCallOverloadedError):
        await service.submit(second)

    assert len(slack.messages) == 1


async def test_service_posts_failure_notice_when_dispatch_finally_fails(tmp_path):
    store = OnCallStore(tmp_path / "oncall.sqlite3")
    await store.initialize()
    slack = FakeSlack()
    dispatcher = FakeDispatcher(fail_times=2)
    service = OnCallService(store, slack, dispatcher, max_attempts=2)

    first = await service.submit(alert())
    assert await service.process_one() is True
    assert await service.process_one() is True

    assert dispatcher.dispatched == []
    assert (await store.job_counts())["failed"] == 1
    failure_text, failure_thread = slack.messages[-1]
    assert "Codex on-call unavailable" in failure_text
    assert "GitHub Actions" in failure_text
    assert failure_thread == first.slack_thread_ts


async def test_service_posts_failure_notice_when_no_run_ever_appears(tmp_path):
    store = OnCallStore(tmp_path / "oncall.sqlite3")
    await store.initialize()
    slack = FakeSlack()
    dispatcher = FakeDispatcher(run_appears_after_polls=None)
    service = OnCallService(store, slack, dispatcher, max_attempts=2, confirm_timeout_seconds=120.0)

    clock = FakeClock(1_000.0)
    with patch("serving.oncall.service.time.time", clock):
        first = await service.submit(alert())
        assert await service.process_one() is True  # dispatch + park (deadline 1120)
        assert await service.process_one() is True  # poll: no run yet, defer to 1010
        clock.advance(10.0)
        assert await service.process_one() is True  # poll: no run yet, defer to 1020
        clock.advance(110.0)  # now 1120, the deadline
        assert await service.process_one() is True  # deadline hit: requeue for a retry
        assert await service.process_one() is True  # second dispatch + park (deadline 1240)
        clock.advance(121.0)
        assert await service.process_one() is True  # deadline hit again: final

    assert dispatcher.confirm_polls >= 3
    assert (await store.job_counts())["failed"] == 1
    failure_text, failure_thread = slack.messages[-1]
    assert "Codex on-call unavailable" in failure_text
    assert "no `Codex On-Call` workflow run appeared" in failure_text
    assert failure_thread == first.slack_thread_ts


async def test_service_defers_confirm_poll_while_github_is_unreadable(tmp_path):
    store = OnCallStore(tmp_path / "oncall.sqlite3")
    await store.initialize()
    slack = FakeSlack()
    dispatcher = FakeDispatcher(confirm_fail_times=999, run_appears_after_polls=None)
    service = OnCallService(store, slack, dispatcher, max_attempts=2, confirm_timeout_seconds=120.0)

    clock = FakeClock(2_000.0)
    with patch("serving.oncall.service.time.time", clock):
        first = await service.submit(alert())
        assert await service.process_one() is True  # dispatch + park (deadline 2120)
        assert await service.process_one() is True  # poll fails: defer to 2010
        assert len(dispatcher.dispatched) == 1  # no redispatch while the window is open
        assert len(slack.messages) == 1  # no failure notice while the window is open
        clock.advance(10.0)
        assert await service.process_one() is True  # poll fails: defer to 2020
        clock.advance(110.0)  # now 2120, the deadline
        assert await service.process_one() is True  # deadline hit: requeue for a retry
        assert await service.process_one() is True  # second dispatch + park
        clock.advance(121.0)
        assert await service.process_one() is True  # deadline hit again: final

    assert (await store.job_counts())["failed"] == 1
    failure_text, failure_thread = slack.messages[-1]
    assert "last confirmation check failed" in failure_text
    assert failure_thread == first.slack_thread_ts


async def test_service_completes_job_once_the_run_appears(tmp_path):
    store = OnCallStore(tmp_path / "oncall.sqlite3")
    await store.initialize()
    slack = FakeSlack()
    dispatcher = FakeDispatcher(run_appears_after_polls=1)
    service = OnCallService(store, slack, dispatcher, max_attempts=2, confirm_timeout_seconds=120.0)

    clock = FakeClock(3_000.0)
    with patch("serving.oncall.service.time.time", clock):
        await service.submit(alert())
        assert await service.process_one() is True  # dispatch + park
        assert await service.process_one() is True  # poll 1: not found, defer to 3010
        clock.advance(10.0)
        assert await service.process_one() is True  # poll 2: run appeared, complete
        assert await service.process_one() is False

    assert (await store.job_counts())["done"] == 1
    # The original alert was the only top-level message; no failure notice.
    assert slack.messages == [("Slack firing", None)]


def test_format_analysis_is_bounded_and_mention_safe():
    message = format_analysis(analysis(), "thread-1")
    assert message.startswith("*Codex on-call*")
    assert "<!channel>" not in message
    assert "&lt;!channel&gt;" in message
    assert "hyi-" not in message
    assert "[REDACTED]" in message
    assert "thread-1" in message
