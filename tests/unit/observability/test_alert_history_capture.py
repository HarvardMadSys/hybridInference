"""Alert history capture through the real alert paths (no database, no network).

Every webhook post is replaced by a synthetic sender; identities, keys and the
fence secret are synthetic.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import itertools
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from serving.config.settings import get_settings
from serving.observability import alerts
from serving.observability.alert_history import hooks
from serving.observability.alert_history.capture import (
    BackendCapture,
    BackendIdentity,
    DeliveryOutcome,
)
from serving.observability.alerts import (
    AlertSeverity,
    SlackSink,
    WeComSink,
    alert_on_transition,
    alert_slack,
    reset_dedupe_state,
    reset_transition_state,
    sweep_stale_breaches,
)
from serving.storage.log_schema import fence_account_digest
from serving.utils import context as req_ctx

FENCE = "synthetic-fence-secret-for-tests"
SLACK = "https://hooks.slack.com/services/TSYN/BSYN/synthetic-webhook-secret"


def _identity() -> BackendIdentity:
    return BackendIdentity(
        source_namespace="test-deployment",
        deployment_id="test-deployment",
        target_environment="test",
        source_instance_id="be_test_instance",
        process_id="test-host:1",
        started_at=dt.datetime(2026, 10, 26, tzinfo=dt.UTC),
    )


class Posts:
    """A synthetic Slack sender: records posts, answers from a script."""

    def __init__(self) -> None:
        self.messages: list[str] = []
        self.answers: list[bool | BaseException] = []
        self.gate: asyncio.Event | None = None

    async def __call__(self, url: str, message: str, *, dedupe_key: str = "") -> bool:
        self.messages.append(message)
        if self.gate is not None:
            gate, self.gate = self.gate, None
            await gate.wait()
        answer = self.answers.pop(0) if self.answers else True
        if isinstance(answer, BaseException):
            raise answer
        return answer


@pytest.fixture
def history(monkeypatch):
    for name in ("SLACK_ALERTS_WEBHOOK_URL", "SLACK_WEBHOOK_URL", "WECOM_ALERTS_WEBHOOK_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", SLACK)
    get_settings.cache_clear()
    reset_dedupe_state()
    reset_transition_state()
    records: list = []
    capture = BackendCapture(_identity(), lambda r: records.append(r) or True, fence_secret=FENCE)
    posts = Posts()
    monkeypatch.setattr(alerts, "_post_to_slack", posts)
    hooks.install(capture)
    try:
        yield capture, records, posts
    finally:
        hooks.install(None)
        reset_dedupe_state()
        reset_transition_state()
        get_settings.cache_clear()


def kinds(records, **match):
    return [r for r in records if all(getattr(r, name) == value for name, value in match.items())]


async def _breach(key="fivexx_rate", *, now, kind="metric", context=None, cooldown=300):
    return await alert_on_transition(
        key=key,
        breached=True,
        severity=AlertSeverity.ERROR,
        title="5xx rate exceeded",
        context=lambda: dict(context or {"rate": "9.0% (9 of 100 requests, last 300s)"}),
        cooldown_sec=cooldown,
        kind=kind,
        now=now,
    )


async def _healthy(key="fivexx_rate", *, now, kind="metric"):
    return await alert_on_transition(
        key=key,
        breached=False,
        severity=AlertSeverity.ERROR,
        title="5xx rate exceeded",
        context=dict,
        cooldown_sec=300,
        kind=kind,
        now=now,
    )


# -- gating and bool compatibility --------------------------------------------


async def test_capture_off_records_nothing_and_returns_the_same_bools(monkeypatch):
    hooks.install(None)
    monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", SLACK)
    get_settings.cache_clear()
    reset_dedupe_state()
    posts = Posts()
    posts.answers = [True, False]
    monkeypatch.setattr(alerts, "_post_to_slack", posts)
    assert await alert_slack(AlertSeverity.ERROR, "t", {}, dedupe_key="a") is True
    assert await alert_slack(AlertSeverity.ERROR, "t", {}, dedupe_key="b") is False
    assert hooks.active() is None
    reset_dedupe_state()
    get_settings.cache_clear()


@pytest.mark.parametrize("answer", [True, False])
async def test_capture_on_keeps_the_bool_and_records_attempt_and_result(history, answer):
    _capture, records, posts = history
    posts.answers = [answer]
    sent = await alert_slack(AlertSeverity.ERROR, "Title", {"count": 3}, dedupe_key="k1")
    assert sent is answer
    (attempt,) = kinds(records, kind="delivery_attempt")
    (result,) = kinds(records, kind="delivery_result")
    assert attempt.attempt_id == result.attempt_id
    assert result.outcome == ("accepted" if answer else "unknown")
    assert result.channel == "slack"
    assert attempt.message["format"] == "slack_mrkdwn"
    assert SLACK not in str(attempt.content())


async def test_sender_exception_is_still_false_and_recorded_as_unknown(history):
    _capture, records, posts = history
    posts.answers = [RuntimeError("synthetic transport failure")]
    assert await alert_slack(AlertSeverity.ERROR, "T", {}, dedupe_key="k2") is False
    (result,) = kinds(records, kind="delivery_result")
    assert (result.outcome, result.error_code) == ("unknown", "exception")


async def test_post_detailed_explains_the_real_sender(monkeypatch):
    def handler(request):
        if "slack" in str(request.url):
            return httpx.Response(404)
        return httpx.Response(200, json={"errcode": 45009, "errmsg": "synthetic"})

    real_client = httpx.AsyncClient

    def client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(alerts.httpx, "AsyncClient", client)
    slack = await SlackSink(SLACK).post_detailed("m", dedupe_key="d")
    assert slack == DeliveryOutcome(False, "rejected", "http_404", 404)
    wecom = await WeComSink(
        "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=synthetic"
    ).post_detailed("m", dedupe_key="d")
    assert wecom == DeliveryOutcome(False, "rejected", "wecom_errcode_45009", 200)
    # The bool contract is unchanged.
    assert await SlackSink(SLACK).post("m", dedupe_key="d") is False


async def test_connect_failure_is_not_sent_and_read_timeout_is_unknown(monkeypatch):
    errors = [httpx.ConnectError("synthetic"), httpx.ReadTimeout("synthetic")]

    def handler(request):
        raise errors.pop(0)

    real_client = httpx.AsyncClient

    def client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(alerts.httpx, "AsyncClient", client)
    first = await SlackSink(SLACK).post_detailed("m", dedupe_key="d")
    second = await SlackSink(SLACK).post_detailed("m", dedupe_key="d")
    assert (first.accepted, first.outcome) == (False, "not_sent")
    assert (second.accepted, second.outcome) == (False, "unknown")


# -- lifecycle ------------------------------------------------------------------


async def test_firing_then_observed_recovery(history):
    _capture, records, _posts = history
    t0 = 1_800_000_000.0
    assert await _breach(now=t0) is True
    (trigger,) = kinds(records, kind="condition_triggered")
    episode = trigger.episode_id
    assert trigger.facts["rate"].startswith("9.0%")
    assert trigger.facts["title"] == "5xx rate exceeded"
    assert trigger.alert_type == "fivexx_rate"
    await _healthy(now=t0 + 10)
    await _healthy(now=t0 + 200)  # past the settling period
    (ended,) = kinds(records, kind="condition_ended")
    assert (ended.episode_id, ended.end_basis) == (episode, "observed_healthy")
    resolution = [
        r
        for r in kinds(records, kind="delivery_attempt")
        if r.facts["notification"] == "resolution"
    ]
    assert [r.episode_id for r in resolution] == [episode]
    assert all(r.episode_id == episode for r in records if r.episode_id)


async def test_cooldown_repeats_are_recorded_once_then_counted_exactly(history):
    capture, records, posts = history
    t0 = 1_800_000_010.0
    await _breach(now=t0)
    for _ in range(7):
        assert await _breach(now=t0) is False
    (decision,) = kinds(records, kind="notification_decision")
    assert decision.decision_reason == "cooldown"
    capture.flush_buckets(force=True)
    summaries = kinds(records, kind="suppression_summary")
    assert sum(s.count for s in summaries) == 6
    assert {s.decision_reason for s in summaries} == {"cooldown"}
    assert all(s.bucket_start <= s.bucket_end for s in summaries)
    # Seven suppressed evaluations: one recorded alone, six counted.
    assert len(posts.messages) == 1


async def test_buckets_do_not_overlap(history):
    capture, records, _posts = history
    capture.on_breach("k", severity="error", title="t", kind="metric", context={})
    episode = capture.open_episode("k")
    for moment in (1000.0, 1001.0, 1029.0, 1031.0, 1059.0, 1061.0):
        capture.decision("k", episode_id=episode, reason="cooldown", now=moment)
    capture.flush_buckets(force=True)
    summaries = kinds(records, kind="suppression_summary")
    windows = sorted((s.bucket_start, s.bucket_end, s.count) for s in summaries)
    # 1000.0 is the first decision, recorded alone; the rest fall in the
    # epoch-aligned windows [990, 1020), [1020, 1050) and [1050, 1080).
    assert [w[2] for w in windows] == [1, 2, 2]
    assert len(kinds(records, kind="notification_decision")) == 1
    for (_, end, _), (start, _, _) in itertools.pairwise(windows):
        assert end <= start


async def test_mute_snooze_and_no_channel_are_decisions(history, monkeypatch):
    _capture, records, posts = history
    with patch.object(alerts, "_type_muted", AsyncMock(return_value=True)):
        assert await _breach("p95_latency:prov-a", now=1.0) is False
    with patch("serving.observability.alert_snooze.is_snoozed", AsyncMock(return_value=True)):
        assert await _breach("p95_latency:prov-b", now=1.0) is False
    monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "")
    get_settings.cache_clear()
    assert await _breach("p95_latency:prov-c", now=1.0) is False
    reasons = {r.alert_key: r.decision_reason for r in kinds(records, kind="notification_decision")}
    assert reasons == {
        "p95_latency:prov-a": "type_muted",
        "p95_latency:prov-b": "snoozed",
        "p95_latency:prov-c": "no_channel",
    }
    # Suppressed, but each breach is still a recorded fact.
    assert len(kinds(records, kind="condition_triggered")) == 3
    assert posts.messages == []


async def test_failed_recovery_rearm_then_rebreach_opens_a_new_episode(history):
    capture, records, posts = history
    key = "db_disconnect:pg"
    await _breach(key, now=10.0, kind="state")
    first = capture.open_episode(key)
    posts.answers = [False]  # the recovery card fails
    assert await _healthy(key, now=20.0, kind="state") is False
    assert alerts._STATE_TRANSITIONS.is_firing(key)  # tracker re-armed
    assert capture.open_episode(key) is None  # the episode stays ended
    assert alerts._PENDING_RESOLUTIONS[key].history_episode == first
    await _breach(key, now=30.0, kind="state")
    second = capture.open_episode(key)
    assert second and second != first
    (terminated,) = kinds(records, kind="resolution_retry_terminated")
    assert (terminated.episode_id, terminated.decision_reason) == (
        first,
        "superseded_by_new_episode",
    )
    ended = kinds(records, kind="condition_ended")
    assert [(e.episode_id, e.end_basis) for e in ended] == [(first, "observed_healthy")]


async def test_old_recovery_finishing_late_cannot_touch_the_new_episode(history):
    capture, records, posts = history
    key = "circuit_open:prov-x"
    await _breach(key, now=1.0, kind="state")
    first = capture.open_episode(key)
    gate = asyncio.Event()
    posts.gate = gate  # the first recovery card hangs in flight
    old = asyncio.create_task(_healthy(key, now=2.0, kind="state"))
    await asyncio.sleep(0.01)
    # A new breach while the old card is in flight: in_flight decision, new episode.
    await _breach(key, now=3.0, kind="state")
    second = capture.open_episode(key)
    assert second != first
    posts.answers = [True, False]  # old card succeeds, the new one fails
    new = asyncio.create_task(_healthy(key, now=4.0, kind="state"))
    await asyncio.sleep(0.01)
    gate.set()
    await asyncio.gather(old, new)
    results = kinds(records, kind="delivery_result")
    by_episode = {
        r.episode_id: r.outcome for r in results if r.facts["notification"] == "resolution"
    }
    assert by_episode[first] == "accepted"
    assert by_episode[second] == "unknown"
    # The new episode's retry association survives the old confirmation.
    assert capture.closed_episode(key) == second
    assert alerts._PENDING_RESOLUTIONS[key].history_episode == second
    posts.answers = [True]
    await sweep_stale_breaches()
    retry = [
        r
        for r in kinds(records, kind="delivery_attempt")
        if r.facts["notification"] == "resolution"
    ][-1]
    assert retry.episode_id == second


async def test_stale_sweep_records_no_samples_not_healthy(history, monkeypatch):
    capture, records, _posts = history
    key = "stream_failure_rate:model-a"
    t0 = 1_000.0
    await alert_on_transition(
        key=key,
        breached=True,
        severity=AlertSeverity.ERROR,
        title="Streaming failures",
        context=lambda: {"model": "model-a", "count": 9},
        cooldown_sec=300,
        stale_after=60.0,
        now=t0,
    )
    episode = capture.open_episode(key)
    monkeypatch.setattr(alerts.time, "time", lambda: t0 + 120)
    await sweep_stale_breaches()
    (ended,) = kinds(records, kind="condition_ended")
    assert (ended.episode_id, ended.end_basis) == (episode, "no_samples")
    (trigger,) = kinds(records, kind="condition_triggered")
    assert trigger.model_id == "model-a"


async def test_sweep_of_a_rearmed_key_retries_without_a_second_end(history, monkeypatch):
    capture, records, posts = history
    key = "fivexx_rate"
    t0 = 5_000.0
    await _breach(now=t0)
    episode = capture.open_episode(key)
    posts.answers = [False]
    await _healthy(now=t0 + 1)
    await _healthy(now=t0 + 200)  # resolved edge; the card fails; tracker re-armed
    alerts._PENDING_RESOLUTIONS.clear()  # retry queue gave up
    monkeypatch.setattr(alerts.time, "time", lambda: t0 + 10_000)
    await sweep_stale_breaches()
    ended = kinds(records, kind="condition_ended")
    assert [(e.episode_id, e.end_basis) for e in ended] == [(episode, "observed_healthy")]
    retry = [
        r
        for r in kinds(records, kind="delivery_attempt")
        if r.facts["notification"] == "resolution"
    ]
    assert {r.episode_id for r in retry} == {episode}


async def test_silent_close_is_a_decision(history):
    _capture, records, _posts = history
    await alert_on_transition(
        key="auth_ip_blocked",
        breached=True,
        severity=AlertSeverity.WARN,
        title="blocked",
        context=lambda: {"blocks_in_window": 1},
        cooldown_sec=0,
        now=1.0,
        announce_resolution=False,
    )
    for moment in (500.0, 700.0):  # healthy, then past the settling period
        await alert_on_transition(
            key="auth_ip_blocked",
            breached=False,
            severity=AlertSeverity.WARN,
            title="blocked",
            context=dict,
            cooldown_sec=0,
            now=moment,
            announce_resolution=False,
        )
    decisions = kinds(records, kind="notification_decision")
    assert [d.decision_reason for d in decisions] == ["resolution_silenced"]


# -- what is kept --------------------------------------------------------------


async def test_subject_in_key_is_digest_only(history):
    _capture, records, _posts = history
    user = "user-synthetic-1234"
    await alert_on_transition(
        key=f"cost_overrun:{user}:2026-10-26",
        breached=True,
        severity=AlertSeverity.WARN,
        title=f"Daily cost quota consumed by {user}",
        context=lambda: {"user_id": user, "role": "pro", "spend": "$20.00", "quota": "$20.00"},
        cooldown_sec=0,
        now=1.0,
    )
    digest = fence_account_digest(user, FENCE)
    for record in records:
        assert user not in str(record.content())
        assert digest in record.subject_digests
    (trigger,) = kinds(records, kind="condition_triggered")
    assert trigger.alert_key == "cost_overrun:[subject]:2026-10-26"
    assert "omitted:user_id" in trigger.limitations
    assert trigger.facts["spend"] == "$20.00"


async def test_without_a_fence_secret_identity_is_not_kept(history):
    capture, records, _posts = history
    capture._fence_secret = None
    capture.on_breach(
        "cost_overrun:user-synthetic-9:2026-10-26",
        severity="warn",
        title="t",
        kind="metric",
        context={},
    )
    (trigger,) = records
    assert trigger.subject_digests == ()
    assert "subject_unfenced" in trigger.limitations
    assert "user-synthetic-9" not in str(trigger.content())


async def test_request_log_saturation_counts_are_history_facts(history):
    """The new rule's counts are safe facts, not omitted free text."""
    _capture, records, _posts = history
    context = {
        "events_in_window": 2,
        "dropped_total": 300,
        "queue_maxsize": 1024,
        "window_sec": 600,
        "note": "api_logs report rows were discarded; quota increments are separate",
    }
    await _breach("request_log_queue_saturation", now=1.0, context=context)
    (trigger,) = kinds(records, kind="condition_triggered")
    assert trigger.facts["events_in_window"] == 2
    assert trigger.facts["dropped_total"] == 300
    assert trigger.facts["queue_maxsize"] == 1024
    assert trigger.facts["window_sec"] == 600
    assert not [note for note in trigger.limitations if note.startswith("omitted:")]


async def test_unsafe_fields_are_omitted_and_typed_facts_kept(history):
    _capture, records, _posts = history
    context = {
        "provider": "prov-a",
        "consecutive_failures": 5,
        "availability": 0.42,
        "affected_callers": "user-a x3, user-b x1",
        "upstream_error": "400: the request said Authorization: Bearer synthetic",
        "request_id": "req-from-client",
        "reason": "upstream_500 from https://upstream.example/v1?key=synthetic",
    }
    await _breach("circuit_open:prov-a", now=1.0, kind="state", context=context)
    (trigger,) = kinds(records, kind="condition_triggered")
    assert trigger.facts["consecutive_failures"] == 5
    assert trigger.facts["availability"] == 0.42
    assert trigger.endpoint_id == "prov-a"
    for name in ("affected_callers", "upstream_error", "request_id"):
        assert name not in trigger.facts
        assert f"omitted:{name}" in trigger.limitations
    assert "synthetic" not in trigger.facts["reason"]
    assert "redacted:reason" in trigger.limitations
    (attempt,) = kinds(records, kind="delivery_attempt")
    assert set(attempt.message["omitted_fields"]) == {
        "affected_callers",
        "upstream_error",
        "request_id",
    }
    assert "user-a" not in attempt.message["body"]
    assert "Bearer synthetic" not in attempt.message["body"]


async def test_request_reference_needs_a_known_owner(history):
    _capture, records, _posts = history
    req_ctx.set({"request_id": "req-synthetic-1", "user_id": "user-synthetic-7"})
    try:
        await _breach("circuit_open:prov-r", now=1.0, kind="state", context={"provider": "prov-r"})
    finally:
        req_ctx.set({})
    (trigger,) = kinds(records, kind="condition_triggered")
    digest = fence_account_digest("user-synthetic-7", FENCE)
    assert trigger.request_refs == (("req-synthetic-1", digest),)
    assert digest in trigger.subject_digests
    records.clear()
    req_ctx.set({"request_id": "req-synthetic-2"})
    try:
        await _breach("circuit_open:prov-s", now=1.0, kind="state", context={"provider": "prov-s"})
    finally:
        req_ctx.set({})
    (trigger,) = kinds(records, kind="condition_triggered")
    assert trigger.request_refs == ()
    assert "request_ref_omitted:owner_unknown" in trigger.limitations


async def test_rule_version_stamps_records(history):
    capture, records, _posts = history
    capture.rule_version = "0123456789abcdef"
    await _breach(now=1.0)
    assert {r.rule_version for r in records} == {"0123456789abcdef"}
    assert {r.rule_id for r in records} == {"fivexx_rate"}


async def test_external_suppression_opens_and_healthy_observation_closes(history):
    capture, records, posts = history
    key = "circuit_open:prov-q"
    hooks.external_decision(
        key,
        reason="policy_disabled",
        severity=AlertSeverity.ERROR,
        title="Provider circuit opened",
        kind="state",
        context={"provider": "prov-q", "gate": "circuit_open_disabled"},
    )
    episode = capture.open_episode(key)
    assert episode
    await _healthy(key, now=5.0, kind="state")  # the breaker's on_success path
    ended = kinds(records, kind="condition_ended")
    assert [(e.episode_id, e.end_basis) for e in ended] == [(episode, "observed_healthy")]
    (decision,) = kinds(records, kind="notification_decision")
    assert decision.decision_reason == "policy_disabled"
    assert posts.messages == []


async def test_breaker_policy_off_still_records_the_trip(history, monkeypatch):
    from routing.endpoint_health import EndpointHealthRegistry
    from serving.observability import state_alert_policy

    _capture, records, posts = history
    monkeypatch.setattr(
        state_alert_policy,
        "_CIRCUIT_OPEN",
        state_alert_policy.circuit_open_policy().model_copy(update={"enabled": False}),
    )
    registry = EndpointHealthRegistry()
    for _ in range(3):
        registry.record_failure("prov-z", reason="upstream_500")
    await asyncio.sleep(0)
    (decision,) = kinds(records, kind="notification_decision")
    assert (decision.alert_key, decision.decision_reason) == (
        "circuit_open:prov-z",
        "policy_disabled",
    )
    assert decision.endpoint_id == "prov-z"
    assert posts.messages == []


async def test_db_disconnect_policy_off_is_recorded_and_closed(history, monkeypatch):
    from unittest.mock import MagicMock

    from serving.observability import state_alert_policy
    from serving.servers.routers.health import _test_store_health

    _capture, records, posts = history
    monkeypatch.setattr(
        state_alert_policy,
        "_DB_DISCONNECT",
        state_alert_policy.db_disconnect_policy().model_copy(update={"enabled": False}),
    )
    op_store = MagicMock()
    op_store.health_check = AsyncMock(side_effect=RuntimeError("synthetic: db unreachable"))
    await _test_store_health(op_store, None)
    (decision,) = kinds(records, kind="notification_decision")
    assert decision.decision_reason == "policy_disabled"
    episode = decision.episode_id
    op_store.health_check = AsyncMock(return_value=True)
    await _test_store_health(op_store, None)
    ended = kinds(records, kind="condition_ended")
    assert [(e.episode_id, e.end_basis) for e in ended] == [(episode, "observed_healthy")]
    assert posts.messages == []


async def test_render_failure_is_not_sent_and_still_false(history, monkeypatch):
    _capture, records, posts = history

    def broken(self, *args, **kwargs):
        raise RuntimeError("synthetic render failure")

    monkeypatch.setattr(SlackSink, "render", broken)
    assert await alert_slack(AlertSeverity.ERROR, "T", {}, dedupe_key="k-render") is False
    (result,) = kinds(records, kind="delivery_result")
    assert (result.outcome, result.error_code) == ("not_sent", "render_failed")
    assert kinds(records, kind="delivery_attempt") == []
    assert posts.messages == []
