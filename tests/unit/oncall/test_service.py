"""Tests for incident dedupe, recovery threading, and dispatch hand-off."""

import sqlite3
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
    def __init__(self, fail_times: int = 0) -> None:
        self.dispatched: list[tuple[AlertEvent, str]] = []
        self._fail_times = fail_times

    async def dispatch(self, event: AlertEvent, slack_thread_ts: str) -> None:
        if self._fail_times > 0:
            self._fail_times -= 1
            raise RuntimeError("github unavailable")
        self.dispatched.append((event, slack_thread_ts))


class FakeAgentDispatcher:
    """Cloud-agent shaped dispatcher: returns the platform job id."""

    def __init__(self, job_id: str = "ajob_0001") -> None:
        self.job_id = job_id
        self.dispatched: list[tuple[AlertEvent, str]] = []

    async def dispatch(self, event: AlertEvent, slack_thread_ts: str) -> str:
        self.dispatched.append((event, slack_thread_ts))
        return self.job_id


class FakeAgentPoller:
    """Scripted control-plane answers for the await_result stage."""

    def __init__(self, states: list[str], events: list[dict] | None = None) -> None:
        self._states = list(states)
        self.events = events if events is not None else []
        self.cancelled: list[str] = []

    async def get_job_state(self, job_id: str) -> str:
        if len(self._states) > 1:
            return self._states.pop(0)
        return self._states[0]

    async def list_events(self, job_id: str) -> list[dict]:
        return self.events

    async def cancel_job(self, job_id: str) -> bool:
        self.cancelled.append(job_id)
        return True


def grounded_events() -> list[dict]:
    """A normalized event log for a grounded, schema-valid analysis."""
    return [
        {"event_type": "tool_result", "payload": {"exit_code": 0, "content": "ok"}},
        {"event_type": "message", "payload": {"text": analysis().model_dump_json()}},
    ]


def agent_service(
    store: OnCallStore,
    slack: FakeSlack,
    poller: FakeAgentPoller,
    dispatcher: FakeAgentDispatcher | None = None,
    **overrides,
) -> OnCallService:
    kwargs = {
        "agent_poller": poller,
        "agent_poll_seconds": 0.0,
        "agent_timeout_seconds": 600.0,
        "agent_console_url": "https://console.example/agents/jobs/{job_id}",
    }
    kwargs.update(overrides)
    return OnCallService(store, slack, dispatcher or FakeAgentDispatcher(), **kwargs)


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

    # One analysis in flight per fingerprint: the second firing posted its own
    # fresh alert, but its analysis was not enqueued while the first job was
    # still active — the burst is answered once, not once per re-fire (the
    # 2026-08-05 nine-analysis spam). Once the first completes, a later firing
    # gets its own analysis again.
    assert await service.process_one() is True
    assert await service.process_one() is False
    assert [ts for _, ts in dispatcher.dispatched] == [first.slack_thread_ts]

    incident = await store.get_incident(alert().fingerprint)
    assert incident is not None
    with patch(
        "serving.oncall.service.time.time",
        return_value=incident.created_at + 602,
    ):
        third = await service.submit(alert(alert_id="alert-3"))
    assert third.duplicate is False
    assert await service.process_one() is True
    assert [ts for _, ts in dispatcher.dispatched] == [
        first.slack_thread_ts,
        third.slack_thread_ts,
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
    # Backend-neutral wording: the same notice covers both dispatch backends.
    assert "analysis backend failed after 2 attempts" in failure_text
    assert failure_thread == first.slack_thread_ts


def test_format_analysis_is_bounded_and_mention_safe():
    message = format_analysis(analysis(), "thread-1")
    assert message.startswith("*Codex on-call*")
    assert "<!channel>" not in message
    assert "&lt;!channel&gt;" in message
    assert "hyi-" not in message
    assert "[REDACTED]" in message
    assert "thread-1" in message


# ── The cloud-agent backend's await_result stage ───────────────────────


async def test_cloud_agent_flow_posts_grounded_analysis(tmp_path):
    """dispatch → awaiting → poll running → poll succeeded → analysis in thread."""
    store = OnCallStore(tmp_path / "oncall.sqlite3")
    await store.initialize()
    slack = FakeSlack()
    dispatcher = FakeAgentDispatcher()
    poller = FakeAgentPoller(["running", "succeeded"], events=grounded_events())
    service = agent_service(store, slack, poller, dispatcher)

    first = await service.submit(alert())
    assert await service.process_one() is True  # dispatch → await_result
    assert dispatcher.dispatched[0][1] == first.slack_thread_ts
    assert await service.process_one() is True  # poll: running → deferred
    assert (await store.job_counts())["done"] == 0
    assert await service.process_one() is True  # poll: succeeded → posted

    counts = await store.job_counts()
    assert counts["done"] == 1 and counts["failed"] == 0
    result_text, result_thread = slack.messages[-1]
    assert result_thread == first.slack_thread_ts
    assert result_text.startswith("*Codex on-call*")
    # The console link replaces the GHA path's Codex thread id.
    assert "https://console.example/agents/jobs/ajob_0001" in result_text


async def test_cloud_agent_refuses_ungrounded_analysis(tmp_path):
    """A succeeded job with no successful command must not be published."""
    store = OnCallStore(tmp_path / "oncall.sqlite3")
    await store.initialize()
    slack = FakeSlack()
    events = [
        {"event_type": "tool_result", "payload": {"exit_code": 1, "content": "boom"}},
        {"event_type": "message", "payload": {"text": analysis().model_dump_json()}},
    ]
    poller = FakeAgentPoller(["succeeded"], events=events)
    service = agent_service(store, slack, poller)

    first = await service.submit(alert())
    assert await service.process_one() is True
    assert await service.process_one() is True

    assert (await store.job_counts())["failed"] == 1
    failure_text, failure_thread = slack.messages[-1]
    assert failure_thread == first.slack_thread_ts
    assert "Codex on-call unavailable" in failure_text
    assert "unusable" in failure_text
    # No analysis body leaked into the thread despite the valid JSON message.
    assert "*Classification:*" not in failure_text


async def test_cloud_agent_reports_failed_platform_job(tmp_path):
    store = OnCallStore(tmp_path / "oncall.sqlite3")
    await store.initialize()
    slack = FakeSlack()
    poller = FakeAgentPoller(["failed"])
    service = agent_service(store, slack, poller)

    first = await service.submit(alert())
    assert await service.process_one() is True
    assert await service.process_one() is True

    assert (await store.job_counts())["failed"] == 1
    failure_text, failure_thread = slack.messages[-1]
    assert failure_thread == first.slack_thread_ts
    assert "ended 'failed'" in failure_text
    assert "https://console.example/agents/jobs/ajob_0001" in failure_text


async def test_cloud_agent_cancels_at_deadline(tmp_path):
    """A job still running past the deadline is cancelled, loudly."""
    store = OnCallStore(tmp_path / "oncall.sqlite3")
    await store.initialize()
    slack = FakeSlack()
    poller = FakeAgentPoller(["running"])
    service = agent_service(store, slack, poller, agent_timeout_seconds=0.0)

    await service.submit(alert())
    assert await service.process_one() is True  # dispatch; deadline = now
    assert await service.process_one() is True  # poll: running, past deadline

    assert poller.cancelled == ["ajob_0001"]
    assert (await store.job_counts())["failed"] == 1
    failure_text, _ = slack.messages[-1]
    assert "did not finish within" in failure_text


async def test_cloud_agent_defers_on_unreachable_control_plane(tmp_path):
    """A poll blip reschedules; the deadline is the only thing that gives up."""
    store = OnCallStore(tmp_path / "oncall.sqlite3")
    await store.initialize()
    slack = FakeSlack()

    class FlakyPoller(FakeAgentPoller):
        def __init__(self) -> None:
            super().__init__(["succeeded"], events=grounded_events())
            self.calls = 0

        async def get_job_state(self, job_id: str) -> str:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("connect timeout")
            return await super().get_job_state(job_id)

    poller = FlakyPoller()
    service = agent_service(store, slack, poller)

    await service.submit(alert())
    assert await service.process_one() is True  # dispatch
    assert await service.process_one() is True  # poll raises → deferred
    counts = await store.job_counts()
    assert counts["failed"] == 0 and counts["queued"] == 1
    assert await service.process_one() is True  # poll succeeds → posted
    assert (await store.job_counts())["done"] == 1


async def test_awaiting_job_without_poller_fails_closed(tmp_path):
    """A relay restarted onto the github backend cannot poll — say so, once."""
    store = OnCallStore(tmp_path / "oncall.sqlite3")
    await store.initialize()
    slack = FakeSlack()
    dispatcher = FakeAgentDispatcher()
    service = agent_service(store, slack, FakeAgentPoller(["running"]), dispatcher)
    await service.submit(alert())
    assert await service.process_one() is True  # dispatch → await_result

    github_only = OnCallService(store, slack, FakeDispatcher())
    assert await github_only.process_one() is True

    assert (await store.job_counts())["failed"] == 1
    failure_text, _ = slack.messages[-1]
    assert "no longer be polled" in failure_text


async def test_await_result_store_failure_requeues_instead_of_stranding(tmp_path):
    """A store write that throws mid-poll must not strand the job in 'running'.

    ``claim_next_job`` marks the row running and only ever re-claims queued
    rows, so an unguarded write in this stage leaked the job until the next
    restart — invisibly, and still counted against ``max_pending_jobs``.
    """
    store = OnCallStore(tmp_path / "oncall.sqlite3")
    await store.initialize()
    slack = FakeSlack()

    class BrokenDeferStore:
        """The real store, except that rescheduling a poll always fails."""

        def __init__(self, inner: OnCallStore) -> None:
            self._inner = inner

        def __getattr__(self, name: str):
            return getattr(self._inner, name)

        async def defer_poll(self, job_id: int, *, not_before: float) -> None:
            raise sqlite3.OperationalError("database is locked")

    service = agent_service(BrokenDeferStore(store), slack, FakeAgentPoller(["running"]))

    await service.submit(alert())
    assert await service.process_one() is True  # dispatch → await_result
    assert await service.process_one() is True  # poll → defer_poll raises

    counts = await store.job_counts()
    assert counts["running"] == 0
    assert counts["queued"] == 1
    assert counts["failed"] == 0


async def test_await_result_store_failure_finally_pages_the_thread(tmp_path):
    """Once the retries are spent the thread is told, rather than left waiting."""
    store = OnCallStore(tmp_path / "oncall.sqlite3")
    await store.initialize()
    slack = FakeSlack()

    class BrokenFailStore:
        """The real store, except that recording a failed job always fails."""

        def __init__(self, inner: OnCallStore) -> None:
            self._inner = inner

        def __getattr__(self, name: str):
            return getattr(self._inner, name)

        async def fail_job(self, job_id: int, error: str) -> None:
            raise sqlite3.OperationalError("disk I/O error")

    service = agent_service(
        BrokenFailStore(store),
        slack,
        FakeAgentPoller(["failed"]),
        max_attempts=1,
    )

    await service.submit(alert())
    assert await service.process_one() is True  # dispatch → await_result
    assert await service.process_one() is True  # settle → fail_job raises

    assert (await store.job_counts())["failed"] == 1
    failure_text, thread_ts = slack.messages[-1]
    assert "could not settle the analysis job" in failure_text
    assert thread_ts is not None
