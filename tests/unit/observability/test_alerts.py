"""Tests for serving.observability.alerts.alert_slack."""

import asyncio
import logging
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from serving.observability.alerts import (
    _EMOJI,
    _LAST_FIRED,
    _PENDING_RESOLUTION_MAX_AGE_SEC,
    _PENDING_RESOLUTION_MAX_ATTEMPTS,
    _PENDING_RESOLUTIONS,
    _REQUEST_ID_MAX_LEN,
    _RESOLUTION_CONTEXT,
    _RESOLUTION_DETAIL,
    _SILENT_RESOLUTIONS,
    _STALE_RETRY_ATTEMPTS,
    _STATE_TRANSITIONS,
    _TRANSITIONS,
    AlertSeverity,
    _base_url,
    _detect_environment,
    _format_message,
    _post_to_slack,
    alert_delivery_failures_total,
    alert_on_transition,
    alert_slack,
    reset_dedupe_state,
    reset_transition_state,
    server_info,
    sweep_stale_breaches,
)
from serving.utils import context as req_ctx
from serving.utils.secret_urls import redact_url, scrub, url_fingerprint


@pytest.fixture(autouse=True)
def reset_state(monkeypatch):
    reset_dedupe_state()
    yield
    reset_dedupe_state()


async def test_alert_slack_no_op_when_webhook_unset(monkeypatch):
    monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "")
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "")
    with patch("serving.observability.alerts._post_to_slack", new=AsyncMock()) as mock_post:
        await alert_slack(AlertSeverity.ERROR, "test", {"k": "v"})
        mock_post.assert_not_called()


async def test_alert_slack_posts_when_webhook_set(monkeypatch):
    monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://hooks.slack.com/x")
    with patch(
        "serving.observability.alerts._post_to_slack",
        new=AsyncMock(return_value=True),
    ) as mock_post:
        await alert_slack(AlertSeverity.ERROR, "test title", {"foo": "bar"})
        mock_post.assert_awaited_once()
        args, _ = mock_post.call_args
        url, message = args
        assert url == "https://hooks.slack.com/x"
        assert "test title" in message
        assert "Foo" in message and "bar" in message


async def test_one_failed_delivery_is_retried_immediately(monkeypatch):
    """A blip must not swallow the page it dropped.

    The cooldown is armed on the attempt now, so the retry that covers a
    transient failure is an explicit exception rather than a side effect of
    never arming — see ``test_a_permanently_failing_sink_stops_after_one_retry``
    for the other half.
    """
    monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://hooks.slack.com/fallback")
    with patch(
        "serving.observability.alerts._post_to_slack",
        new=AsyncMock(side_effect=[False, True]),
    ) as mock_slack:
        first = await alert_slack(AlertSeverity.ERROR, "Provider failed", {}, dedupe_key="K")
        second = await alert_slack(AlertSeverity.ERROR, "Provider failed", {}, dedupe_key="K")

    assert first is False
    assert second is True
    assert mock_slack.await_count == 2


async def test_alert_slack_dedupes_within_cooldown(monkeypatch):
    monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://hooks.slack.com/x")
    with patch(
        "serving.observability.alerts._post_to_slack",
        new=AsyncMock(return_value=True),
    ) as mock_post:
        await alert_slack(AlertSeverity.WARN, "t", {}, dedupe_key="K", cooldown_sec=60)
        await alert_slack(AlertSeverity.WARN, "t", {}, dedupe_key="K", cooldown_sec=60)
    assert mock_post.await_count == 1


async def test_alert_slack_fires_again_after_cooldown(monkeypatch):
    monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://hooks.slack.com/x")
    fake_now = [1000.0]

    def now():
        return fake_now[0]

    with (
        patch(
            "serving.observability.alerts._post_to_slack",
            new=AsyncMock(return_value=True),
        ) as mock_post,
        patch("serving.observability.alerts._monotonic", new=now),
    ):
        await alert_slack(AlertSeverity.WARN, "t", {}, dedupe_key="K", cooldown_sec=60)
        fake_now[0] += 61
        await alert_slack(AlertSeverity.WARN, "t", {}, dedupe_key="K", cooldown_sec=60)
    assert mock_post.await_count == 2


async def test_alert_slack_swallows_post_errors(monkeypatch):
    monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://hooks.slack.com/x")
    with patch(
        "serving.observability.alerts._post_to_slack",
        new=AsyncMock(side_effect=RuntimeError("boom")),
    ):
        # must not raise
        await alert_slack(AlertSeverity.ERROR, "t", {})


def test_server_info_has_expected_keys():
    info = server_info()
    assert set(info) >= {
        "hostname",
        "fqdn",
        "ip",
        "platform",
        "base_url",
        "environment",
    }
    assert info["hostname"]
    assert info["platform"]


def test_unconfigured_default_base_url_is_not_rendered(monkeypatch):
    monkeypatch.delenv("BASE_URL", raising=False)
    monkeypatch.delenv("base_url", raising=False)

    url, explicit = _base_url()
    assert (url, explicit) == ("", False)

    message = _format_message(AlertSeverity.ERROR, "Boom", {})
    assert "• *Base URL:* https://gateway.example.com" not in message


def test_explicit_base_url_is_rendered(monkeypatch):
    monkeypatch.setenv("BASE_URL", "https://staging.example.com")

    url, explicit = _base_url()
    assert (url, explicit) == ("https://staging.example.com", True)

    message = _format_message(AlertSeverity.ERROR, "Boom", {})
    assert "• *Base URL:* https://staging.example.com" in message


@pytest.mark.parametrize(
    "env_overrides, base_url, explicit, expected",
    [
        # Explicit DEPLOYMENT_ENV/ENVIRONMENT overrides always win (and are stripped).
        ({"DEPLOYMENT_ENV": "qa"}, "https://gateway.example.com", True, "qa"),
        ({"ENVIRONMENT": "  canary\n"}, "https://staging.example.com", True, "canary"),
        # Host-based inference (urlparse, so path segments don't misclassify).
        ({}, "https://staging.example.com", True, "staging"),
        ({}, "https://gateway.example.com", True, "production"),
        ({}, "http://localhost:8000", True, "local"),
        ({}, "http://127.0.0.1:8080/staging", True, "local"),
        # Any explicitly configured public host is that operator's production,
        # not just one known domain.
        ({}, "https://example.com", True, "production"),
        # No configured base URL => treat as local, not prod.
        ({}, "https://gateway.example.com", False, "local"),
        ({}, "", True, "local"),
        # Malformed URL (unclosed IPv6 literal) must not raise.
        ({}, "http://[::1", True, "unknown"),
    ],
)
def test_detect_environment(monkeypatch, env_overrides, base_url, explicit, expected):
    monkeypatch.delenv("DEPLOYMENT_ENV", raising=False)
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    for key, value in env_overrides.items():
        monkeypatch.setenv(key, value)
    assert _detect_environment(base_url, explicit=explicit) == expected


def test_format_message_includes_server_block():
    message = _format_message(AlertSeverity.ERROR, "Boom", {"provider": "openai"})
    # Per-alert context (upstream provider) is still rendered.
    assert "• *Provider:* openai" in message
    # Gateway server identity is appended.
    assert "*Server*" in message
    assert "• *Host:*" in message
    info = server_info()
    assert info["hostname"] in message


def test_format_message_marks_a_resolution_as_recovery():
    """The plain webhook has no status field, so the text must carry it.

    Titles are breach statements, so rendering a resolution with the breach's
    severity emoji would read in Slack as a second outage.
    """
    title = "Provider circuit opened"
    firing = _format_message(AlertSeverity.ERROR, title, {}, "firing")
    resolved = _format_message(AlertSeverity.ERROR, title, {}, "resolved")

    assert firing.startswith(f"{_EMOJI[AlertSeverity.ERROR]} *{title}*")
    assert resolved.startswith(f"✅ *Recovered:* {title}")
    assert _EMOJI[AlertSeverity.ERROR] not in resolved
    # Defaulting to "firing" keeps every existing caller rendering as before.
    assert _format_message(AlertSeverity.ERROR, title, {}) == firing


class TestResolutionIsNeverSuppressed:
    """A dropped resolution leaves its control-plane incident open forever.

    Both suppressions in ``alert_slack`` exist to stop a breach from repeating.
    Applying them to a resolution instead holds principal quota until it is
    exhausted, at which point real outages start being suppressed — the exact
    failure the recovery work exists to prevent.
    """

    @pytest.mark.asyncio
    async def test_cooldown_does_not_swallow_the_resolution_it_follows(self, monkeypatch):
        monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://hooks.slack.com/x")
        with patch("serving.observability.alerts._post_to_slack", new=AsyncMock(return_value=True)):
            assert await alert_slack(
                AlertSeverity.ERROR, "5xx rate exceeded", {}, dedupe_key="k", cooldown_sec=300
            )
            # A repeat of the breach is correctly suppressed …
            assert not await alert_slack(
                AlertSeverity.ERROR, "5xx rate exceeded", {}, dedupe_key="k", cooldown_sec=300
            )
            # … but the resolution inside the same window must still go out.
            assert await alert_slack(
                AlertSeverity.INFO,
                "5xx rate recovered",
                {},
                dedupe_key="k",
                cooldown_sec=300,
                status="resolved",
            )

    @pytest.mark.asyncio
    async def test_the_next_breach_pages_immediately_after_a_resolution(self, monkeypatch):
        monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://hooks.slack.com/x")
        with patch("serving.observability.alerts._post_to_slack", new=AsyncMock(return_value=True)):
            await alert_slack(
                AlertSeverity.ERROR, "5xx rate exceeded", {}, dedupe_key="k", cooldown_sec=300
            )
            await alert_slack(
                AlertSeverity.INFO,
                "5xx rate recovered",
                {},
                dedupe_key="k",
                cooldown_sec=300,
                status="resolved",
            )
            # The incident is closed, so a fresh breach must not serve out the
            # cooldown the previous one started.
            assert await alert_slack(
                AlertSeverity.ERROR, "5xx rate exceeded", {}, dedupe_key="k", cooldown_sec=300
            )

    @pytest.mark.asyncio
    async def test_snooze_silences_breaches_but_still_closes_incidents(self, monkeypatch):
        monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://hooks.slack.com/x")
        with (
            patch(
                "serving.observability.alert_snooze.is_snoozed",
                new=AsyncMock(return_value=True),
            ),
            patch("serving.observability.alerts._post_to_slack", new=AsyncMock(return_value=True)),
        ):
            assert not await alert_slack(
                AlertSeverity.ERROR, "5xx rate exceeded", {}, dedupe_key="k"
            )
            # Silencing alerts means "stop telling me it is broken", not
            # "leave the incident open once it is fixed".
            assert await alert_slack(
                AlertSeverity.INFO,
                "5xx rate recovered",
                {},
                dedupe_key="k",
                status="resolved",
            )


class TestStateAlertsResolveOnTheirOnlyEdge:
    """A circuit or store reports one healthy edge, so it must resolve on it."""

    async def test_state_kind_resolves_immediately(self, monkeypatch):
        monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://hooks.slack.com/x")
        reset_transition_state()
        with patch(
            "serving.observability.alerts._post_to_slack",
            new=AsyncMock(return_value=True),
        ):
            await alert_on_transition(
                key="circuit_open:zhipu",
                breached=True,
                severity=AlertSeverity.ERROR,
                title="Provider circuit opened",
                context=dict,
                cooldown_sec=0,
                kind="state",
            )
            sent = await alert_on_transition(
                key="circuit_open:zhipu",
                breached=False,
                severity=AlertSeverity.ERROR,
                title="Provider circuit opened",
                context=dict,
                cooldown_sec=0,
                kind="state",
            )

        # Under the metric settling period this would be False, and the only
        # healthy edge the breaker ever reports would be spent.
        assert sent is True

    async def test_a_state_alert_is_never_swept(self, monkeypatch):
        monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://hooks.slack.com/x")
        reset_transition_state()
        with patch(
            "serving.observability.alerts._post_to_slack",
            new=AsyncMock(return_value=True),
        ) as mock_post:
            await alert_on_transition(
                key="circuit_open:zhipu",
                breached=True,
                severity=AlertSeverity.ERROR,
                title="Provider circuit opened",
                context=dict,
                cooldown_sec=0,
                kind="state",
            )
            mock_post.reset_mock()
            await sweep_stale_breaches()

        # Silence is not recovery: sweeping would report the outage as over.
        mock_post.assert_not_awaited()


class TestUndeliveredResolutionIsRetried:
    async def test_a_failed_resolution_stays_open_for_a_retry(self, monkeypatch):
        monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://hooks.slack.com/x")
        reset_transition_state()
        with patch(
            "serving.observability.alerts._post_to_slack",
            new=AsyncMock(side_effect=[True, False, True]),
        ):
            await alert_on_transition(
                key="circuit_open:zhipu",
                breached=True,
                severity=AlertSeverity.ERROR,
                title="Provider circuit opened",
                context=dict,
                cooldown_sec=0,
                kind="state",
            )
            first = await alert_on_transition(
                key="circuit_open:zhipu",
                breached=False,
                severity=AlertSeverity.ERROR,
                title="Provider circuit opened",
                context=dict,
                cooldown_sec=0,
                kind="state",
            )
            retry = await alert_on_transition(
                key="circuit_open:zhipu",
                breached=False,
                severity=AlertSeverity.ERROR,
                title="Provider circuit opened",
                context=dict,
                cooldown_sec=0,
                kind="state",
            )

        assert first is False
        # Without re-arming, the transition is spent and the incident can never
        # be closed by anything.
        assert retry is True


class TestResolutionWaitsForAnInFlightFiring:
    async def test_a_recovery_during_the_firing_send_is_not_dropped(self, monkeypatch):
        """A state alert has no later observation to retry with.

        Dropping the resolution here costs a repeat for a metric — another
        evaluation follows — but strands a circuit-breaker incident forever.
        """
        monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://hooks.slack.com/x")
        reset_transition_state()
        reset_dedupe_state()
        started = asyncio.Event()
        release = asyncio.Event()
        posted: list[str] = []

        async def slow_post(_url, message, **_kwargs):
            if "Recovered" not in message:
                started.set()
                await release.wait()
            posted.append(message)
            return True

        with patch("serving.observability.alerts._post_to_slack", new=slow_post):
            firing = asyncio.ensure_future(
                alert_on_transition(
                    key="circuit_open:zhipu",
                    breached=True,
                    severity=AlertSeverity.ERROR,
                    title="Provider circuit opened",
                    context=dict,
                    cooldown_sec=0,
                    kind="state",
                )
            )
            await started.wait()
            recovery = asyncio.ensure_future(
                alert_on_transition(
                    key="circuit_open:zhipu",
                    breached=False,
                    severity=AlertSeverity.ERROR,
                    title="Provider circuit opened",
                    context=dict,
                    cooldown_sec=0,
                    kind="state",
                )
            )
            await asyncio.sleep(0)
            release.set()
            assert await firing is True
            assert await recovery is True

        # Both landed, and the outage was reported before the recovery.
        assert len(posted) == 2
        assert "Recovered" not in posted[0]
        assert "Recovered" in posted[1]

    async def test_a_recovery_cannot_overtake_a_firing_stalled_in_the_snooze_lookup(
        self, monkeypatch
    ):
        """The in-flight marker must be published before anything on this path awaits.

        The snooze lookup used to run *before* the marker was registered, and it is
        skipped entirely for a resolution. So a firing parked in that lookup had
        published nothing for the recovery to wait on: the recovery went out first
        and the firing landed after it, opening an incident whose only healthy edge
        was already spent. State alerts are never swept, so that page then stayed
        open for a provider that was working, holding principal quota.
        """
        monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://hooks.slack.com/x")
        reset_transition_state()
        reset_dedupe_state()
        reached_lookup = asyncio.Event()
        release_lookup = asyncio.Event()
        posted: list[str] = []

        async def stalled_is_snoozed() -> bool:
            reached_lookup.set()
            await release_lookup.wait()
            return False

        async def record_post(_url, message, **_kwargs):
            posted.append(message)
            return True

        with (
            patch("serving.observability.alert_snooze.is_snoozed", new=stalled_is_snoozed),
            patch("serving.observability.alerts._post_to_slack", new=record_post),
        ):
            firing = asyncio.ensure_future(
                alert_on_transition(
                    key="circuit_open:zhipu",
                    breached=True,
                    severity=AlertSeverity.ERROR,
                    title="Provider circuit opened",
                    context=dict,
                    cooldown_sec=0,
                    kind="state",
                )
            )
            await reached_lookup.wait()
            assert not posted, "the firing must still be mid-delivery for this to be the race"
            recovery = asyncio.ensure_future(
                alert_on_transition(
                    key="circuit_open:zhipu",
                    breached=False,
                    severity=AlertSeverity.ERROR,
                    title="Provider circuit opened",
                    context=dict,
                    cooldown_sec=0,
                    kind="state",
                )
            )
            await asyncio.sleep(0)
            assert not posted, "the recovery was delivered before the page it closes"
            release_lookup.set()
            assert await firing is True
            assert await recovery is True

        assert [("Recovered" in message) for message in posted] == [False, True]
        assert _STATE_TRANSITIONS.is_firing("circuit_open:zhipu") is False


class TestAFailedSweepRetriesPromptly:
    async def test_the_next_sweep_retries_rather_than_the_next_window(self, monkeypatch):
        """A plain re-arm restarts the staleness clock.

        With the staleness window tracking the longest rule window — an hour in
        the shipped config — that pushes the retry hours out while the incident
        stays open.
        """
        monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://hooks.slack.com/x")
        reset_transition_state()
        reset_dedupe_state()
        clock = [1_000.0]
        monkeypatch.setattr("serving.observability.alerts.time.time", lambda: clock[0])

        with patch(
            "serving.observability.alerts._post_to_slack",
            new=AsyncMock(side_effect=[True, False, True]),
        ) as mock_post:
            await alert_on_transition(
                key="failed_request_rate",
                breached=True,
                severity=AlertSeverity.ERROR,
                title="Failed-request rate exceeded",
                context=dict,
                cooldown_sec=0,
                stale_after=3_600.0,
                now=clock[0],
            )
            clock[0] += 3_600.0
            await sweep_stale_breaches()
            assert mock_post.await_count == 2

            # One sweep interval later, not one staleness window later.
            clock[0] += 60.0
            await sweep_stale_breaches()

        assert mock_post.await_count == 3


class TestTheResolutionWaitOutlivesASlowSend:
    async def test_a_timeout_does_not_end_the_wait_after_one_attempt(self, monkeypatch):
        """A firing send can occupy the webhook for longer than one wait window.

        Giving up after the first would drop exactly the resolution this wait
        exists to save.
        """
        monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://hooks.slack.com/x")
        monkeypatch.setattr("serving.observability.alerts._RESOLUTION_WAIT_SEC", 0.01)
        reset_transition_state()
        reset_dedupe_state()
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow_post(_url, message, **_kwargs):
            if "Recovered" not in message:
                started.set()
                # Outlives one wait window but not the attempt bound.
                await asyncio.wait_for(release.wait(), timeout=1.0)
            return True

        with patch("serving.observability.alerts._post_to_slack", new=slow_post):
            firing = asyncio.ensure_future(
                alert_on_transition(
                    key="circuit_open:zhipu",
                    breached=True,
                    severity=AlertSeverity.ERROR,
                    title="Provider circuit opened",
                    context=dict,
                    cooldown_sec=0,
                    kind="state",
                )
            )
            await started.wait()
            recovery = asyncio.ensure_future(
                alert_on_transition(
                    key="circuit_open:zhipu",
                    breached=False,
                    severity=AlertSeverity.ERROR,
                    title="Provider circuit opened",
                    context=dict,
                    cooldown_sec=0,
                    kind="state",
                )
            )
            await asyncio.sleep(0.015)
            release.set()
            assert await firing is True
            assert await recovery is True


class TestPendingResolutionRetriesOffTheSweepTimer:
    """A failed resolution send is retried by the sweep timer, not by observations.

    The circuit breaker calls ``alert_on_transition`` only on a state *change*,
    so after its single healthy edge there is no later observation to carry a
    retry: re-arming alone would leave the incident announced-open forever.
    """

    async def test_a_failed_state_resolution_is_delivered_by_the_sweep(self, monkeypatch):
        monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://hooks.slack.com/x")
        reset_transition_state()
        with patch(
            "serving.observability.alerts._post_to_slack",
            new=AsyncMock(side_effect=[True, False, True]),
        ) as mock_post:
            await alert_on_transition(
                key="circuit_open:zhipu",
                breached=True,
                severity=AlertSeverity.ERROR,
                title="Provider circuit opened",
                context=dict,
                cooldown_sec=0,
                kind="state",
            )
            closed = await alert_on_transition(
                key="circuit_open:zhipu",
                breached=False,
                severity=AlertSeverity.ERROR,
                title="Provider circuit opened",
                context=dict,
                cooldown_sec=0,
                kind="state",
            )
            assert closed is False
            # The breaker reports no further edges; only the timer runs.
            await sweep_stale_breaches()

            assert mock_post.await_count == 3
            message = mock_post.await_args.args[1]
            assert "Recovered: Provider circuit opened" in message
            # Delivered exactly once: a later sweep must not repeat it.
            await sweep_stale_breaches()
            assert mock_post.await_count == 3

        assert not _PENDING_RESOLUTIONS
        # A confirmed close leaves no tracker residue behind.
        assert not _STATE_TRANSITIONS._firing
        assert not _STATE_TRANSITIONS._bounds

    async def test_a_re_breach_cancels_the_pending_resolution(self, monkeypatch):
        """The queued recovery is stale the moment the condition is real again.

        Sending it later would announce a live outage as recovered, which is
        strictly worse than the fire-only behaviour this module replaced.
        """
        monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://hooks.slack.com/x")
        reset_transition_state()
        with patch(
            "serving.observability.alerts._post_to_slack",
            new=AsyncMock(side_effect=[True, False, True]),
        ) as mock_post:
            for breached in (True, False, True):
                await alert_on_transition(
                    key="circuit_open:zhipu",
                    breached=breached,
                    severity=AlertSeverity.ERROR,
                    title="Provider circuit opened",
                    context=dict,
                    cooldown_sec=0,
                    kind="state",
                )
            await sweep_stale_breaches()

        # Fire, failed close, re-fire — and no recovery for the live breach.
        assert mock_post.await_count == 3
        assert not _PENDING_RESOLUTIONS
        assert _STATE_TRANSITIONS.is_firing("circuit_open:zhipu")

    async def test_a_confirmed_metric_close_clears_the_staleness_bound(self, monkeypatch):
        """Dynamic keys (per-user, per-period) must not leak a bound per incident."""
        monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://hooks.slack.com/x")
        reset_transition_state()
        key = "provider_budget:zhipu:2026-07-30T10"
        t0 = 1_000.0
        with patch(
            "serving.observability.alerts._post_to_slack",
            new=AsyncMock(side_effect=[True, True]),
        ):
            await alert_on_transition(
                key=key,
                breached=True,
                severity=AlertSeverity.WARN,
                title="Provider hourly spend exceeded budget",
                context=dict,
                cooldown_sec=0,
                stale_after=3_600.0,
                now=t0,
            )
            await alert_on_transition(
                key=key,
                breached=False,
                severity=AlertSeverity.WARN,
                title="Provider hourly spend exceeded budget",
                context=dict,
                cooldown_sec=0,
                stale_after=3_600.0,
                now=t0 + 1.0,
            )
            resolved = await alert_on_transition(
                key=key,
                breached=False,
                severity=AlertSeverity.WARN,
                title="Provider hourly spend exceeded budget",
                context=dict,
                cooldown_sec=0,
                stale_after=3_600.0,
                now=t0 + 200.0,
            )

        assert resolved is True
        assert not _TRANSITIONS._firing
        assert not _TRANSITIONS._bounds

    async def test_the_stale_sweep_names_its_weaker_evidence(self, monkeypatch):
        """A sweep close means "no recent samples", not an observed-clear metric.

        The wording must say so: a rule can go quiet because traffic stopped or
        the process is draining, and reading that as a measured recovery would
        mislead whoever is watching the channel during an outage.
        """
        monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://hooks.slack.com/x")
        reset_transition_state()
        reset_dedupe_state()
        clock = [1_000.0]
        monkeypatch.setattr("serving.observability.alerts.time.time", lambda: clock[0])
        with patch(
            "serving.observability.alerts._post_to_slack",
            new=AsyncMock(side_effect=[True, True]),
        ) as mock_post:
            await alert_on_transition(
                key="failed_request_rate",
                breached=True,
                severity=AlertSeverity.ERROR,
                title="Failed-request rate exceeded",
                context=dict,
                cooldown_sec=0,
                stale_after=3_600.0,
                now=clock[0],
            )
            clock[0] += 3_600.0
            await sweep_stale_breaches()

        message = mock_post.await_args.args[1]
        assert "Recovered (no recent samples): failed_request_rate" in message
        # A successful sweep close is a confirmed close: no bound left behind.
        assert not _TRANSITIONS._firing
        assert not _TRANSITIONS._bounds


class TestReBreachDuringAnInFlightRecoverySend:
    """The narrowest window: the condition re-breaks while a recovery send is
    on the wire.

    The breach path pops the pending entry and (for a state alert) re-creates
    firing state, but the send that is already in flight completes afterwards.
    Cleanup after that send must re-validate what it is cleaning: an
    unconditional ``forget`` would erase the live incident's state and, for a
    state alert, silently spend the only healthy edge its close will ever get.
    """

    @staticmethod
    async def _circuit(breached: bool) -> bool:
        return await alert_on_transition(
            key="circuit_open:zhipu",
            breached=breached,
            severity=AlertSeverity.ERROR,
            title="Provider circuit opened",
            context=dict,
            cooldown_sec=0,
            kind="state",
        )

    async def test_the_edge_close_does_not_forget_a_mid_send_re_breach(self, monkeypatch):
        monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://hooks.slack.com/x")
        reset_transition_state()
        recoveries = []

        async def post(_url, message, **_kwargs):
            if "Recovered" in message:
                recoveries.append(message)
                if len(recoveries) == 1:
                    # Re-opens while the recovery is on the wire. Its firing
                    # send is dropped by the in-flight guard; only the tracker
                    # state records that the breach is live again.
                    await self._circuit(True)
            return True

        with patch("serving.observability.alerts._post_to_slack", new=post):
            await self._circuit(True)
            await self._circuit(False)
            # The re-breach must still be tracked after the stale close...
            assert _STATE_TRANSITIONS.is_firing("circuit_open:zhipu")
            # ...so the eventual real close still announces.
            closed = await self._circuit(False)

        assert closed is True
        assert len(recoveries) == 2
        assert not _STATE_TRANSITIONS._firing
        assert not _STATE_TRANSITIONS._bounds

    async def test_the_retry_does_not_forget_a_mid_send_re_breach(self, monkeypatch):
        monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://hooks.slack.com/x")
        reset_transition_state()
        recoveries = []

        async def post(_url, message, **_kwargs):
            if "Recovered" not in message:
                return True
            recoveries.append(message)
            if len(recoveries) == 1:
                # The edge close fails, which is what queues the retry.
                return False
            if len(recoveries) == 2:
                # Re-opens while the sweep's retry send is on the wire.
                await self._circuit(True)
            return True

        with patch("serving.observability.alerts._post_to_slack", new=post):
            await self._circuit(True)
            failed_close = await self._circuit(False)
            assert failed_close is False
            assert "circuit_open:zhipu" in _PENDING_RESOLUTIONS
            await sweep_stale_breaches()

            # The re-breach cancelled the entry mid-send; the delivered text is
            # stale, but the incident must stay tracked for its real close.
            assert _STATE_TRANSITIONS.is_firing("circuit_open:zhipu")
            assert "circuit_open:zhipu" not in _PENDING_RESOLUTIONS
            closed = await self._circuit(False)

        assert closed is True
        assert not _STATE_TRANSITIONS._firing
        assert not _STATE_TRANSITIONS._bounds

    async def test_the_stale_sweep_does_not_forget_a_mid_send_re_breach(self, monkeypatch):
        monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://hooks.slack.com/x")
        reset_transition_state()
        reset_dedupe_state()
        clock = [1_000.0]
        monkeypatch.setattr("serving.observability.alerts.time.time", lambda: clock[0])

        async def refire():
            await alert_on_transition(
                key="failed_request_rate",
                breached=True,
                severity=AlertSeverity.ERROR,
                title="Failed-request rate exceeded",
                context=dict,
                cooldown_sec=0,
                stale_after=3_600.0,
                now=clock[0],
            )

        async def post(_url, message, **_kwargs):
            if "no recent samples" in message:
                # Traffic returns and the metric re-breaches while the sweep's
                # close is on the wire.
                await refire()
            return True

        with patch("serving.observability.alerts._post_to_slack", new=post):
            await refire()
            clock[0] += 3_600.0
            await sweep_stale_breaches()

        # The fresh incident survives the sweep's cleanup, with its own bound.
        assert _TRANSITIONS.is_firing("failed_request_rate")
        assert "failed_request_rate" in _TRANSITIONS._bounds


def test_format_message_escapes_caller_controlled_context():
    """A value chosen by whoever triggered the alert renders literally.

    Alert context now routinely carries text the offending caller picked — a
    client IP read off a spoofable ``X-Forwarded-For`` hop, a request path, the
    leading characters of a presented key. Escaping is done here, once, rather
    than in each rule, so that a rule added later cannot hand a scanner a way to
    ping the alert channel.
    """
    message = _format_message(
        AlertSeverity.WARN,
        "Auth failure spike",
        {"top_ips": "<!channel> (40)", "top_paths": "/v1/chat & /v1/models"},
    )

    assert "&lt;!channel&gt; (40)" in message
    assert "<!channel>" not in message
    assert "/v1/chat &amp; /v1/models" in message


class TestAlertsNameTheTriggeringRequest:
    """A page should be joinable to a row in ``api_logs``.

    Without the request id an operator reading "Provider circuit opened" has
    only a timestamp to go on, and has to guess which of the requests in that
    minute is the one the alert is about.
    """

    @pytest.fixture(autouse=True)
    def _clean_request_context(self):
        """Start each test with no ambient request.

        The contextvar is process-wide, and a sync test elsewhere in the suite
        that writes one outside a task (``tests/unit/test_auth_key_hash.py``
        seeds a scope) leaves it visible to whatever runs next in the same
        worker. The "no request context" cases here would then depend on test
        ordering.
        """
        saved = req_ctx.get()
        req_ctx.set({})
        yield
        req_ctx.set(saved)

    async def test_a_firing_page_carries_the_request_id(self, monkeypatch):
        monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://hooks.slack.com/x")
        with (
            patch(
                "serving.observability.alerts._post_to_slack",
                new=AsyncMock(return_value=True),
            ) as mock_post,
            patch(
                "serving.observability.alerts._format_message",
                wraps=_format_message,
            ) as mock_format,
            req_ctx.push(request_id="abc123def456"),
        ):
            await alert_slack(AlertSeverity.ERROR, "Provider circuit opened", {"provider": "zhipu"})

        # The label is the key title-cased; ``_format_message`` has no label table.
        assert "• *Request Id:* abc123def456" in mock_post.await_args.args[1]
        assert mock_format.call_args.args[2]["request_id"] == "abc123def456"

    async def test_no_request_context_leaves_the_field_off(self, monkeypatch):
        """Background alerters (rules engine, stale sweep) have no request."""
        monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://hooks.slack.com/x")
        with patch(
            "serving.observability.alerts._post_to_slack",
            new=AsyncMock(return_value=True),
        ) as mock_post:
            sent = await alert_slack(AlertSeverity.WARN, "Failure rate high", {"rate": "0.4"})

        assert sent is True
        assert "Request Id" not in mock_post.await_args.args[1]

    async def test_a_caller_supplied_id_wins(self, monkeypatch):
        """A caller alerting *about* one request knows better than the ambient one.

        The failed-request alerter reads its rows out of the database, so the
        request it is paging about is not the request it is running under.
        """
        monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://hooks.slack.com/x")
        with (
            patch(
                "serving.observability.alerts._post_to_slack",
                new=AsyncMock(return_value=True),
            ) as mock_post,
            req_ctx.push(request_id="ambient"),
        ):
            await alert_slack(AlertSeverity.ERROR, "Request failed", {"request_id": "explicit"})

        assert "• *Request Id:* explicit" in mock_post.await_args.args[1]
        assert "ambient" not in mock_post.await_args.args[1]

    async def test_a_resolution_does_not_gain_the_field(self, monkeypatch):
        """The request that observed the recovery is a *healthy* request.

        Naming it on the recovery card would point an investigation at a
        request that has nothing to do with the incident.
        """
        monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://hooks.slack.com/x")
        with (
            patch(
                "serving.observability.alerts._post_to_slack",
                new=AsyncMock(return_value=True),
            ) as mock_post,
            req_ctx.push(request_id="recovering-request"),
        ):
            await alert_slack(
                AlertSeverity.INFO,
                "Recovered: Provider circuit opened",
                {"alert": "circuit_open:zhipu"},
                status="resolved",
            )

        assert "Request Id" not in mock_post.await_args.args[1]
        assert "recovering-request" not in mock_post.await_args.args[1]

    async def test_a_hostile_request_id_is_escaped(self, monkeypatch):
        """``X-Request-ID`` is client-supplied and propagated verbatim.

        Without the escape, naming your own request ``<!channel>`` is enough to
        make the gateway ping the alert channel for you.
        """
        monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://hooks.slack.com/x")
        with (
            patch(
                "serving.observability.alerts._post_to_slack",
                new=AsyncMock(return_value=True),
            ) as mock_post,
            req_ctx.push(request_id="<!channel>\nfake line"),
        ):
            await alert_slack(AlertSeverity.ERROR, "Provider circuit opened", {})

        message = mock_post.await_args.args[1]
        # Escaped once, by ``_format_message``: the newline is gone so the id
        # cannot forge a bullet, and the mention renders as literal text.
        assert "• *Request Id:* &lt;!channel&gt; fake line" in message
        assert "<!channel>" not in message

    async def test_an_overlong_request_id_is_bounded(self, monkeypatch):
        """An unbounded id could push the server block past Slack's limit."""
        monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://hooks.slack.com/x")
        with (
            patch(
                "serving.observability.alerts._post_to_slack",
                new=AsyncMock(return_value=True),
            ) as mock_post,
            req_ctx.push(request_id="z" * 5000),
        ):
            await alert_slack(AlertSeverity.ERROR, "Provider circuit opened", {})

        message = mock_post.await_args.args[1]
        assert f"• *Request Id:* {'z' * _REQUEST_ID_MAX_LEN}\n" in message
        assert "z" * (_REQUEST_ID_MAX_LEN + 1) not in message
        # The fields an operator reads first still made it into the card.
        assert "*Server*" in message

    async def test_the_callers_context_dict_is_not_mutated(self, monkeypatch):
        """``alert_on_transition`` hands the same dict to every repeat."""
        monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://hooks.slack.com/x")
        context = {"provider": "zhipu"}
        with (
            patch(
                "serving.observability.alerts._post_to_slack",
                new=AsyncMock(return_value=True),
            ),
            req_ctx.push(request_id="abc123def456"),
        ):
            await alert_slack(AlertSeverity.ERROR, "Provider circuit opened", context)

        assert context == {"provider": "zhipu"}


class TestResolutionCarriesIncidentDetail:
    """A recovery card may describe the incident, not just name the rule.

    The default stays as it was: breach numbers describe a healthy system by the
    time it recovers. But some incidents are about *who* rather than how much,
    and for those the identity is worth as much on the close as on the breach.
    """

    async def test_a_rule_summary_reaches_the_recovery_card(self, monkeypatch):
        monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://hooks.slack.com/x")
        reset_transition_state()
        with patch(
            "serving.observability.alerts._post_to_slack",
            new=AsyncMock(return_value=True),
        ) as mock_post:
            for breached in (True, False):
                await alert_on_transition(
                    key="auth_failure_spike",
                    breached=breached,
                    severity=AlertSeverity.WARN,
                    title="Auth failure spike",
                    context=dict,
                    cooldown_sec=0,
                    kind="state",
                    resolution_context=lambda: {"top_ips": "203.0.113.9 (412)"},
                )

        recovery = mock_post.await_args.args[1]
        assert "Recovered:" in recovery
        assert "203.0.113.9 (412)" in recovery

    async def test_a_rule_that_opts_out_still_gets_the_bare_card(self, monkeypatch):
        """Unchanged for every rule that does not pass one."""
        monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://hooks.slack.com/x")
        reset_transition_state()
        with patch(
            "serving.observability.alerts._post_to_slack",
            new=AsyncMock(return_value=True),
        ) as mock_post:
            for breached in (True, False):
                await alert_on_transition(
                    key="circuit_open:zhipu",
                    breached=breached,
                    severity=AlertSeverity.ERROR,
                    title="Provider circuit opened",
                    context=dict,
                    cooldown_sec=0,
                    kind="state",
                )

        assert "Alert:* circuit_open:zhipu" in mock_post.await_args.args[1]

    async def test_a_retry_resends_the_summary_built_on_the_transition(self, monkeypatch):
        """The builder answers once, so every path that may send must agree.

        A rule that tallies an incident resets that tally when it hands the
        summary over — otherwise the next incident inherits this one's
        addresses. Rebuilding at retry time would therefore post a recovery
        thinner than the one that failed to send.
        """
        monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://hooks.slack.com/x")
        reset_transition_state()
        summaries = iter([{"top_ips": "203.0.113.9 (412)"}, {}])

        with patch(
            "serving.observability.alerts._post_to_slack",
            # firing, resolution (dropped), sweep retry
            new=AsyncMock(side_effect=[True, False, True]),
        ) as mock_post:
            for breached in (True, False):
                await alert_on_transition(
                    key="auth_failure_spike",
                    breached=breached,
                    severity=AlertSeverity.WARN,
                    title="Auth failure spike",
                    context=dict,
                    cooldown_sec=0,
                    kind="state",
                    resolution_context=lambda: next(summaries),
                )
            assert "auth_failure_spike" in _PENDING_RESOLUTIONS
            await sweep_stale_breaches()

        assert "203.0.113.9 (412)" in mock_post.await_args.args[1]
        assert "auth_failure_spike" not in _PENDING_RESOLUTIONS

    async def test_the_stale_sweep_carries_the_summary_too(self, monkeypatch):
        """The likeliest way a spike ends is its traffic simply stopping.

        That incident is closed by the sweep, which is handed a bare key — so
        the summary has to be reachable from the key rather than passed along
        the call that closes it.
        """
        monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://hooks.slack.com/x")
        reset_transition_state()
        with patch(
            "serving.observability.alerts._post_to_slack",
            new=AsyncMock(return_value=True),
        ) as mock_post:
            await alert_on_transition(
                key="auth_failure_spike",
                breached=True,
                severity=AlertSeverity.WARN,
                title="Auth failure spike",
                context=dict,
                cooldown_sec=0,
                stale_after=0.0,
                resolution_context=lambda: {"known_accounts": "01USER (revoked) (88)"},
            )
            await sweep_stale_breaches()

        swept = mock_post.await_args.args[1]
        assert "01USER (revoked) (88)" in swept
        # The weaker claim this path makes must still win the "reason" key.
        assert "no samples within the rule window" in swept

    async def test_a_builder_that_raises_cannot_take_the_recovery_down(self, monkeypatch):
        """A resolution is the last word on an incident; nothing may drop it."""
        monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://hooks.slack.com/x")
        reset_transition_state()

        def _boom() -> dict:
            raise RuntimeError("tally is gone")

        with patch(
            "serving.observability.alerts._post_to_slack",
            new=AsyncMock(return_value=True),
        ) as mock_post:
            for breached in (True, False):
                sent = await alert_on_transition(
                    key="auth_failure_spike",
                    breached=breached,
                    severity=AlertSeverity.WARN,
                    title="Auth failure spike",
                    context=dict,
                    cooldown_sec=0,
                    kind="state",
                    resolution_context=_boom,
                )

        assert sent is True
        assert "Recovered:" in mock_post.await_args.args[1]

    async def test_a_closed_incident_leaves_no_builder_behind(self, monkeypatch):
        """One entry per open incident, dropped on a confirmed close."""
        monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://hooks.slack.com/x")
        reset_transition_state()
        with patch(
            "serving.observability.alerts._post_to_slack",
            new=AsyncMock(return_value=True),
        ):
            for breached in (True, False):
                await alert_on_transition(
                    key="auth_failure_spike",
                    breached=breached,
                    severity=AlertSeverity.WARN,
                    title="Auth failure spike",
                    context=dict,
                    cooldown_sec=0,
                    kind="state",
                    resolution_context=lambda: {"top_ips": "203.0.113.9 (412)"},
                )

        assert "auth_failure_spike" not in _RESOLUTION_CONTEXT
        assert "auth_failure_spike" not in _RESOLUTION_DETAIL

    async def test_a_swept_retry_does_not_rebuild_an_emptied_summary(self, monkeypatch):
        """The stale sweep re-arms rather than queueing, so it rebuilds.

        A spike whose traffic stops closes here, and a dropped send puts the key
        back for the next tick — which would call a builder that has already
        handed its tally over.
        """
        monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://hooks.slack.com/x")
        reset_transition_state()
        summaries = iter([{"top_ips": "203.0.113.9 (412)"}, {}])

        with patch(
            "serving.observability.alerts._post_to_slack",
            # firing, first sweep (dropped), second sweep
            new=AsyncMock(side_effect=[True, False, True]),
        ) as mock_post:
            await alert_on_transition(
                key="auth_failure_spike",
                breached=True,
                severity=AlertSeverity.WARN,
                title="Auth failure spike",
                context=dict,
                cooldown_sec=0,
                stale_after=0.0,
                resolution_context=lambda: next(summaries),
            )
            await sweep_stale_breaches()
            await sweep_stale_breaches()

        assert "203.0.113.9 (412)" in mock_post.await_args.args[1]
        assert "auth_failure_spike" not in _RESOLUTION_DETAIL

    async def test_a_reopened_incident_builds_its_own_summary(self, monkeypatch):
        """A breach after a dropped close is a live incident, not the old one."""
        monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://hooks.slack.com/x")
        reset_transition_state()
        summaries = iter([{"top_ips": "203.0.113.9 (412)"}, {"top_ips": "198.51.100.4 (7)"}])

        with patch(
            "serving.observability.alerts._post_to_slack",
            # firing, close (dropped), re-breach, close
            new=AsyncMock(side_effect=[True, False, True, True]),
        ) as mock_post:
            for breached in (True, False, True, False):
                await alert_on_transition(
                    key="auth_failure_spike",
                    breached=breached,
                    severity=AlertSeverity.WARN,
                    title="Auth failure spike",
                    context=dict,
                    cooldown_sec=0,
                    kind="state",
                    resolution_context=lambda: next(summaries),
                )

        recovery = mock_post.await_args.args[1]
        assert "198.51.100.4 (7)" in recovery
        assert "203.0.113.9 (412)" not in recovery


class TestASilencedCloseSaysNothingButStillCloses:
    """``announce_resolution=False``: no card, and no incident left open either.

    For a rule that only ever observes a transition, going quiet is its normal
    state rather than a measurement, so every close it can produce would report
    a recovery nobody observed. Suppressing the card must not reintroduce the
    problem the tracker exists to solve — a key firing forever, and a ``_bounds``
    entry per dynamic key — so the close still has to happen, silently.
    """

    @staticmethod
    async def _blocked(breached: bool, *, announce: bool = False, **kwargs) -> bool:
        return await alert_on_transition(
            key="auth_ip_blocked",
            breached=breached,
            severity=AlertSeverity.WARN,
            title="Auth-failure blocklist refusing a source",
            context=dict,
            cooldown_sec=0,
            announce_resolution=announce,
            **kwargs,
        )

    async def test_the_breach_card_is_unaffected(self, monkeypatch):
        """Only the close is silenced; the page an operator needs still goes."""
        monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://hooks.slack.com/x")
        reset_transition_state()
        with patch(
            "serving.observability.alerts._post_to_slack",
            new=AsyncMock(return_value=True),
        ) as mock_post:
            fired = await self._blocked(True, kind="state")

        assert fired is True
        assert "Auth-failure blocklist refusing a source" in mock_post.await_args.args[1]

    async def test_the_resolved_edge_sends_nothing_and_clears_the_tracker(self, monkeypatch):
        monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://hooks.slack.com/x")
        reset_transition_state()
        with patch(
            "serving.observability.alerts._post_to_slack",
            new=AsyncMock(return_value=True),
        ) as mock_post:
            await self._blocked(True, kind="state")
            mock_post.reset_mock()
            closed = await self._blocked(False, kind="state")

        # Nothing was sent, which is what the return says.
        mock_post.assert_not_awaited()
        assert closed is False
        # And the incident is genuinely over rather than stuck firing.
        assert not _STATE_TRANSITIONS.is_firing("auth_ip_blocked")
        assert not _STATE_TRANSITIONS._bounds
        assert "auth_ip_blocked" not in _PENDING_RESOLUTIONS
        assert "auth_ip_blocked" not in _SILENT_RESOLUTIONS
        assert "auth_ip_blocked" not in _RESOLUTION_CONTEXT
        assert "auth_ip_blocked" not in _RESOLUTION_DETAIL

    async def test_the_stale_sweep_posts_no_no_samples_card(self, monkeypatch):
        """The sweep is the close this rule would actually reach in production.

        Its traffic stops by construction — one record per blocking transition,
        and the block outlasts the rule window many times over — so without the
        opt-out every block would be followed by "Recovered (no recent
        samples)" while the source was still being refused.
        """
        monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://hooks.slack.com/x")
        reset_transition_state()
        reset_dedupe_state()
        clock = [1_000.0]
        monkeypatch.setattr("serving.observability.alerts.time.time", lambda: clock[0])
        with patch(
            "serving.observability.alerts._post_to_slack",
            new=AsyncMock(return_value=True),
        ) as mock_post:
            await self._blocked(True, stale_after=600.0, now=clock[0])
            mock_post.reset_mock()
            clock[0] += 601.0
            await sweep_stale_breaches()

        mock_post.assert_not_awaited()
        # Swept means closed: no firing key, and none of the per-key registries
        # holding an entry a dynamic key would leak.
        assert not _TRANSITIONS.is_firing("auth_ip_blocked")
        assert not _TRANSITIONS._bounds
        assert "auth_ip_blocked" not in _STALE_RETRY_ATTEMPTS
        assert "auth_ip_blocked" not in _SILENT_RESOLUTIONS
        assert "auth_ip_blocked" not in _RESOLUTION_CONTEXT
        assert "auth_ip_blocked" not in _RESOLUTION_DETAIL

    async def test_a_swept_silent_key_does_not_come_back_next_tick(self, monkeypatch):
        """Nothing to retry, so nothing may be re-armed for the next sweep."""
        monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://hooks.slack.com/x")
        reset_transition_state()
        reset_dedupe_state()
        clock = [1_000.0]
        monkeypatch.setattr("serving.observability.alerts.time.time", lambda: clock[0])
        with patch(
            "serving.observability.alerts._post_to_slack",
            new=AsyncMock(return_value=True),
        ) as mock_post:
            await self._blocked(True, stale_after=600.0, now=clock[0])
            mock_post.reset_mock()
            clock[0] += 601.0
            await sweep_stale_breaches()
            clock[0] += 601.0
            await sweep_stale_breaches()

        mock_post.assert_not_awaited()
        assert not _TRANSITIONS._firing

    async def test_a_rule_that_does_not_opt_out_still_announces_both_closes(self, monkeypatch):
        """The generic default is untouched: both recovery cards still send."""
        monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://hooks.slack.com/x")
        reset_transition_state()
        reset_dedupe_state()
        clock = [1_000.0]
        monkeypatch.setattr("serving.observability.alerts.time.time", lambda: clock[0])
        with patch(
            "serving.observability.alerts._post_to_slack",
            new=AsyncMock(return_value=True),
        ) as mock_post:
            # The settling edge.
            await alert_on_transition(
                key="circuit_open:zhipu",
                breached=True,
                severity=AlertSeverity.ERROR,
                title="Provider circuit opened",
                context=dict,
                cooldown_sec=0,
                kind="state",
            )
            closed = await alert_on_transition(
                key="circuit_open:zhipu",
                breached=False,
                severity=AlertSeverity.ERROR,
                title="Provider circuit opened",
                context=dict,
                cooldown_sec=0,
                kind="state",
            )
            assert closed is True
            assert "Recovered: Provider circuit opened" in mock_post.await_args.args[1]

            # And the no-samples close.
            await alert_on_transition(
                key="failed_request_rate",
                breached=True,
                severity=AlertSeverity.ERROR,
                title="Failed-request rate exceeded",
                context=dict,
                cooldown_sec=0,
                stale_after=600.0,
                now=clock[0],
            )
            clock[0] += 601.0
            await sweep_stale_breaches()

        assert "Recovered (no recent samples): failed_request_rate" in mock_post.await_args.args[1]

    async def test_a_rule_that_stops_opting_out_is_not_held_silent(self, monkeypatch):
        """The opt-out is rewritten on every breach, in both directions.

        Registering on the breach edge is what makes the sweep reachable, but it
        also means a stale registration could outlive the rule that asked for
        it — a key that flips back to announcing must announce.
        """
        monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://hooks.slack.com/x")
        reset_transition_state()
        with patch(
            "serving.observability.alerts._post_to_slack",
            new=AsyncMock(return_value=True),
        ) as mock_post:
            await self._blocked(True, kind="state")
            await self._blocked(True, announce=True, kind="state")
            mock_post.reset_mock()
            closed = await self._blocked(False, announce=True, kind="state")

        assert closed is True
        assert "Recovered:" in mock_post.await_args.args[1]

    async def test_reset_transition_state_clears_the_silent_registry(self, monkeypatch):
        monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://hooks.slack.com/x")
        reset_transition_state()
        with patch(
            "serving.observability.alerts._post_to_slack",
            new=AsyncMock(return_value=True),
        ):
            await self._blocked(True, kind="state")

        assert "auth_ip_blocked" in _SILENT_RESOLUTIONS
        reset_transition_state()
        assert not _SILENT_RESOLUTIONS


class _StubResponse:
    """Just the attribute ``_post_to_slack`` reads off an httpx response."""

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class _StubClient:
    """Async-context httpx client that answers every post with one status."""

    def __init__(self, status_code: int) -> None:
        self._status_code = status_code

    async def __aenter__(self) -> "_StubClient":
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False

    async def post(self, _url: str, json: dict | None = None) -> _StubResponse:
        return _StubResponse(self._status_code)


class _RaisingClient(_StubClient):
    """Transport error rather than an answer — the ``except`` branch's case."""

    def __init__(self) -> None:
        super().__init__(0)

    async def post(self, _url: str, json: dict | None = None) -> _StubResponse:
        raise RuntimeError("connection refused")


def _webhook_refusing_to_connect():
    """Patch target for ``httpx.AsyncClient`` whose posts raise."""
    return lambda *_args, **_kwargs: _RaisingClient()


def _webhook_answering(status_code: int):
    """Patch target for ``httpx.AsyncClient`` that always returns ``status_code``."""
    return lambda *_args, **_kwargs: _StubClient(status_code)


class TestARefusedDeliveryIsVisible:
    """A revoked webhook answers every post with 404 and raises nothing.

    Logging only from the ``except`` branch made that a silent black hole: 199
    consecutive posts were refused over a fortnight, every one of them answered
    rather than failed, and not a single line was written. The only trace was
    httpx's INFO line for the request, which nobody greps, so the alert channel
    stayed dead from the moment the webhook was rotated until someone noticed
    the silence.
    """

    #: Shaped like a real webhook so the leak assertion has something to find.
    WEBHOOK = "https://hooks.slack.com/services/T00000000/B00000000/sUpErSeCrEtToKeN"

    async def test_a_non_2xx_logs_once_with_the_status_and_the_alert(self, caplog):
        with (
            patch("serving.observability.alerts.httpx.AsyncClient", _webhook_answering(404)),
            caplog.at_level(logging.WARNING, logger="serving.observability.alerts"),
        ):
            sent = await _post_to_slack(self.WEBHOOK, "body", dedupe_key="circuit_open:zhipu")

        assert sent is False
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert len(warnings) == 1
        # The status says *why* (404 revoked, 429 throttled) and the key says
        # which alert was lost — enough to act on without reading the payload.
        assert "404" in warnings[0].getMessage()
        assert "circuit_open:zhipu" in warnings[0].getMessage()

    async def test_the_webhook_url_is_never_logged(self, caplog):
        """The URL is a bearer credential: holding it is permission to post.

        This deployment already leaks it through httpx's own INFO logging, which
        is being dealt with separately. Adding a second copy — in a line whose
        whole purpose is to be read by whoever is on call — would make that
        worse rather than better.
        """
        with (
            patch("serving.observability.alerts.httpx.AsyncClient", _webhook_answering(404)),
            caplog.at_level(logging.DEBUG, logger="serving.observability.alerts"),
        ):
            await _post_to_slack(self.WEBHOOK, "body", dedupe_key="circuit_open:zhipu")

        # Assert the line exists before asserting what it omits: a version of
        # this module that logs nothing at all would otherwise pass here, and
        # silence is the defect the class above exists to catch.
        assert [r for r in caplog.records if r.levelno >= logging.WARNING]
        # ``caplog.text`` is the *formatted* output, so this covers the
        # traceback of the ``except`` branch as well as the message itself.
        assert self.WEBHOOK not in caplog.text
        assert "sUpErSeCrEtToKeN" not in caplog.text

    async def test_a_delivered_alert_says_nothing(self, caplog):
        """One line per refusal, none per success: this must stay greppable."""
        with (
            patch("serving.observability.alerts.httpx.AsyncClient", _webhook_answering(200)),
            caplog.at_level(logging.WARNING, logger="serving.observability.alerts"),
        ):
            sent = await _post_to_slack(self.WEBHOOK, "body", dedupe_key="circuit_open:zhipu")

        assert sent is True
        assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []

    async def test_refusals_are_counted_by_status(self):
        """A counter is the only shape delivery health can honestly take here.

        An alert about the alert transport would have to travel over the
        transport that is failing. The count is left for the independent
        control-plane path to read instead.
        """
        with patch("serving.observability.alerts.httpx.AsyncClient", _webhook_answering(404)):
            await _post_to_slack(self.WEBHOOK, "body", dedupe_key="k")
            await _post_to_slack(self.WEBHOOK, "body", dedupe_key="k")

        assert alert_delivery_failures_total() == {"404": 2}

    async def test_a_transport_error_still_names_the_alert_it_lost(self, caplog):
        """The ``except`` branch logged already; what it never said was *which*.

        A traceback with no key tells an operator the sink is unhappy but not
        which page went missing, and the counter needs the transport failures
        too or a webhook that stops resolving looks like a healthy one.
        """
        with (
            patch(
                "serving.observability.alerts.httpx.AsyncClient",
                _webhook_refusing_to_connect(),
            ),
            caplog.at_level(logging.DEBUG, logger="serving.observability.alerts"),
        ):
            sent = await _post_to_slack(self.WEBHOOK, "body", dedupe_key="circuit_open:zhipu")

        assert sent is False
        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert len(errors) == 1
        assert "circuit_open:zhipu" in errors[0].getMessage()
        assert alert_delivery_failures_total() == {"exception": 1}
        # ``caplog.text`` carries the formatted traceback, so this covers the
        # frames as well as the message.
        assert self.WEBHOOK not in caplog.text
        assert "sUpErSeCrEtToKeN" not in caplog.text


class TestTheCooldownIsArmedOnTheAttempt:
    """A sink that never succeeds must not become an unbounded retry loop.

    Arming the cooldown on delivery meant the dedupe table stayed empty for as
    long as the webhook refused, so a live breach was re-posted at every single
    evaluation — the failure that produced the least visible alerting was also
    the one that generated the most traffic.
    """

    async def test_a_permanently_failing_sink_stops_after_one_retry(self, monkeypatch):
        monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://hooks.slack.com/x")
        with patch(
            "serving.observability.alerts._post_to_slack",
            new=AsyncMock(return_value=False),
        ) as mock_post:
            for _ in range(5):
                sent = await alert_slack(
                    AlertSeverity.ERROR,
                    "Provider failed",
                    {},
                    dedupe_key="K",
                    cooldown_sec=300,
                )
                assert sent is False

        # The original attempt plus the one free retry the transient case buys.
        assert mock_post.await_count == 2

    async def test_a_recovered_sink_serves_the_whole_cooldown_again(self, monkeypatch):
        """A delivered alert resets the failure count, so the exception closes."""
        monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://hooks.slack.com/x")
        with patch(
            "serving.observability.alerts._post_to_slack",
            new=AsyncMock(side_effect=[False, True, True]),
        ) as mock_post:
            for _ in range(4):
                await alert_slack(
                    AlertSeverity.ERROR,
                    "Provider failed",
                    {},
                    dedupe_key="K",
                    cooldown_sec=300,
                )

        # Failed attempt, free retry that lands — and then silence, because the
        # delivered alert is under cooldown like any other.
        assert mock_post.await_count == 2

    async def test_a_failed_resolution_does_not_arm_the_firing_cooldown(self, monkeypatch):
        """A close nobody read must not delay the page that follows it.

        The guard skips resolutions outright — a recovery may never be
        suppressed — so arming the cooldown on a failed close buys no
        suppression at all. It would only push the window for the *next* breach
        out by a full cooldown, measured from the moment the close failed, and
        the sweep's own caps are what bound the resolution retries instead.
        """
        monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://hooks.slack.com/x")
        reset_dedupe_state()
        with patch(
            "serving.observability.alerts._post_to_slack",
            new=AsyncMock(return_value=False),
        ) as mock_post:
            # The resolution edge, then the retry the sweep would make.
            for _ in range(2):
                await alert_slack(
                    AlertSeverity.INFO,
                    "Recovered: Provider failed",
                    {},
                    dedupe_key="K",
                    cooldown_sec=300,
                    status="resolved",
                )
            assert mock_post.await_count == 2
            assert "K" not in _LAST_FIRED

            sent = await alert_slack(
                AlertSeverity.ERROR,
                "Provider failed",
                {},
                dedupe_key="K",
                cooldown_sec=300,
            )

        assert sent is False  # the sink is still refusing
        assert mock_post.await_count == 3  # but the breach was attempted, not swallowed


class TestPendingResolutionsStopRetrying:
    """The retry queue must drain while the sink is down, not accumulate.

    A queued resolution can only be confirmed by the sink it cannot reach, so
    every entry was retried on every tick and none ever left: roughly 87% of the
    undelivered posts in the production window were this one loop, all of them
    landing in the same second of each minute.
    """

    @staticmethod
    async def _circuit(breached: bool) -> bool:
        return await alert_on_transition(
            key="circuit_open:zhipu",
            breached=breached,
            severity=AlertSeverity.ERROR,
            title="Provider circuit opened",
            context=dict,
            cooldown_sec=0,
            kind="state",
        )

    async def test_the_queue_gives_up_after_the_attempt_cap(self, monkeypatch):
        monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://hooks.slack.com/x")
        reset_transition_state()
        reset_dedupe_state()
        clock = [1_000.0]
        monkeypatch.setattr("serving.observability.alerts.time.time", lambda: clock[0])

        with patch(
            "serving.observability.alerts._post_to_slack",
            new=AsyncMock(return_value=False),
        ) as mock_post:
            await self._circuit(True)
            await self._circuit(False)
            assert "circuit_open:zhipu" in _PENDING_RESOLUTIONS
            # Well past the cap: a tick a minute, as the scheduler runs it.
            for _ in range(_PENDING_RESOLUTION_MAX_ATTEMPTS + 5):
                clock[0] += 60.0
                await sweep_stale_breaches()

        assert not _PENDING_RESOLUTIONS
        # Breach, failed close, then a bounded number of retries — not one per
        # tick for the life of the process.
        assert mock_post.await_count == 2 + _PENDING_RESOLUTION_MAX_ATTEMPTS

    async def test_an_entry_older_than_the_age_cap_is_dropped(self, monkeypatch):
        """The attempt cap assumes a tick a minute; a quiet process ticks rarely.

        A recovery announced this long after the fact tells an operator nothing
        they cannot see from the absence of breaches, so age bounds it too.
        """
        monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://hooks.slack.com/x")
        reset_transition_state()
        reset_dedupe_state()
        clock = [1_000.0]
        monkeypatch.setattr("serving.observability.alerts.time.time", lambda: clock[0])

        with patch(
            "serving.observability.alerts._post_to_slack",
            new=AsyncMock(return_value=False),
        ) as mock_post:
            await self._circuit(True)
            await self._circuit(False)
            clock[0] += _PENDING_RESOLUTION_MAX_AGE_SEC + 1.0
            await sweep_stale_breaches()
            assert not _PENDING_RESOLUTIONS
            await sweep_stale_breaches()

        # Breach, failed close, one last try — the second sweep has nothing left.
        assert mock_post.await_count == 3

    async def test_a_re_breach_still_cancels_before_the_cap(self, monkeypatch):
        """Giving up must not be reached by announcing a live outage as over."""
        monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://hooks.slack.com/x")
        reset_transition_state()
        reset_dedupe_state()
        with patch(
            "serving.observability.alerts._post_to_slack",
            new=AsyncMock(return_value=False),
        ):
            await self._circuit(True)
            await self._circuit(False)
            await self._circuit(True)
            await sweep_stale_breaches()

        assert not _PENDING_RESOLUTIONS
        assert _STATE_TRANSITIONS.is_firing("circuit_open:zhipu")


class TestTheNoSamplesSweepStopsRetrying:
    """The sweep's own re-arm path is the same loop wearing a different hat.

    A failed no-samples close re-arms the key so the next tick retries it, which
    while the sink is down means every open incident is re-posted once a minute
    for as long as the process lives.
    """

    async def test_a_down_sink_does_not_re_arm_for_ever(self, monkeypatch):
        monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://hooks.slack.com/x")
        reset_transition_state()
        reset_dedupe_state()
        clock = [1_000.0]
        monkeypatch.setattr("serving.observability.alerts.time.time", lambda: clock[0])

        with patch(
            "serving.observability.alerts._post_to_slack",
            new=AsyncMock(return_value=False),
        ) as mock_post:
            await alert_on_transition(
                key="failed_request_rate",
                breached=True,
                severity=AlertSeverity.ERROR,
                title="Failed-request rate exceeded",
                context=dict,
                cooldown_sec=0,
                stale_after=3_600.0,
                now=clock[0],
            )
            clock[0] += 3_600.0
            for _ in range(_PENDING_RESOLUTION_MAX_ATTEMPTS + 5):
                await sweep_stale_breaches()
                clock[0] += 60.0

        assert mock_post.await_count == 1 + _PENDING_RESOLUTION_MAX_ATTEMPTS
        # Given up on, not left armed — a key the sweep keeps returning is a key
        # it keeps re-posting.
        assert not _TRANSITIONS._firing
        assert not _TRANSITIONS._bounds


#: Captured before any patching: ``patch`` on ``alerts.httpx.AsyncClient``
#: rebinds the attribute on the httpx module itself, so a factory that looked
#: the class up by name would call itself.
_REAL_ASYNC_CLIENT = httpx.AsyncClient


def _real_client_over(handler):
    """Patch target giving the *real* ``httpx.AsyncClient`` a mock transport.

    The stub client the classes above patch in never reaches httpx's own
    logging, which is exactly where the credential leak lives — so these tests
    drive the real client and let ``httpx`` write its request line.
    """

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return _REAL_ASYNC_CLIENT(*args, **kwargs)

    return factory


class TestTheRequestLineCannotCarryTheWebhook:
    """``httpx`` logs the full request URL at INFO for every call it makes.

    For a Slack incoming webhook that URL *is* the credential: the
    ``T…/B…/…`` path is the whole secret, with no separate token behind it. So
    the request line wrote the credential into the application log once per
    alert post — 221 lines in 24h of container logs on the deployment where
    this was measured — and anyone who could read logs could post to the
    channel. The fix redacts that URL for this sender only; these tests hold
    both halves of that: the secret never appears, and the line still does.
    """

    #: Obviously fake, and shaped like the real thing so the assertions below
    #: have something to find. ``.test`` is a reserved TLD; nothing resolves.
    WEBHOOK = "https://hooks.slack.test/services/T00000000/B00000000/nOtArEaLtOkEn"
    SECRET_PATH = "/services/T00000000/B00000000/nOtArEaLtOkEn"

    def _assert_no_secret(self, caplog):
        """Nothing in the formatted output may carry the URL, path or token."""
        assert self.WEBHOOK not in caplog.text
        assert self.SECRET_PATH not in caplog.text
        assert "nOtArEaLtOkEn" not in caplog.text

    async def test_a_delivered_post_writes_no_copy_of_the_url(self, caplog):
        """The steady-state case: 221 of those 221 lines were successful posts."""
        with (
            patch(
                "serving.observability.alerts.httpx.AsyncClient",
                _real_client_over(lambda _req: httpx.Response(200)),
            ),
            caplog.at_level(logging.DEBUG),
        ):
            sent = await _post_to_slack(self.WEBHOOK, "body", dedupe_key="circuit_open:zhipu")

        assert sent is True
        self._assert_no_secret(caplog)
        # Redacted, not silenced: httpx still logged the call, and the host plus
        # the fingerprint still answer "did we post, and to which sink".
        assert "HTTP Request" in caplog.text
        assert "hooks.slack.test" in caplog.text
        assert url_fingerprint(self.WEBHOOK) in caplog.text

    async def test_a_refusal_still_names_its_status_and_alert(self, caplog):
        """The WARNING is the primary delivery signal now that the URL is gone."""
        with (
            patch(
                "serving.observability.alerts.httpx.AsyncClient",
                _real_client_over(lambda _req: httpx.Response(404)),
            ),
            caplog.at_level(logging.DEBUG),
        ):
            sent = await _post_to_slack(self.WEBHOOK, "body", dedupe_key="circuit_open:zhipu")

        assert sent is False
        warnings = [
            r
            for r in caplog.records
            if r.levelno >= logging.WARNING and r.name == "serving.observability.alerts"
        ]
        assert len(warnings) == 1
        assert "404" in warnings[0].getMessage()
        assert "circuit_open:zhipu" in warnings[0].getMessage()
        assert url_fingerprint(self.WEBHOOK) in warnings[0].getMessage()
        self._assert_no_secret(caplog)

    async def test_a_transport_error_is_logged_without_the_url(self, caplog):
        """A real ``httpx`` transport failure, formatted traceback and all.

        Alone this one proves nothing: it passes against the unfixed sender too,
        because httpx's own error wording omits the URL and the request line is
        never reached when the post never completes. It is a regression guard —
        the case below is the one that fails without the fix.
        """

        def _refuse(request):
            raise httpx.ConnectError("connection refused", request=request)

        with (
            patch(
                "serving.observability.alerts.httpx.AsyncClient",
                _real_client_over(_refuse),
            ),
            caplog.at_level(logging.DEBUG),
        ):
            sent = await _post_to_slack(self.WEBHOOK, "body", dedupe_key="circuit_open:zhipu")

        assert sent is False
        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert len(errors) == 1
        assert "circuit_open:zhipu" in errors[0].getMessage()
        assert alert_delivery_failures_total() == {"exception": 1}
        self._assert_no_secret(caplog)

    async def test_an_error_that_quotes_the_url_is_scrubbed_out_of_the_traceback(self, caplog):
        """The traceback is the leak path that survives a careful log call.

        An exception worded in terms of the request carries the URL into the
        formatted output even when every ``%s`` in the log call is safe, and
        nothing about httpx guarantees its wording stays URL-free across
        versions or proxies. So the rendered traceback is scrubbed, not trusted.
        """

        def _fail_loudly(_request):
            raise RuntimeError(f"could not reach {self.WEBHOOK}")

        with (
            patch(
                "serving.observability.alerts.httpx.AsyncClient",
                _real_client_over(_fail_loudly),
            ),
            caplog.at_level(logging.DEBUG),
        ):
            sent = await _post_to_slack(self.WEBHOOK, "body", dedupe_key="circuit_open:zhipu")

        assert sent is False
        self._assert_no_secret(caplog)
        # The failure is still reported, and still identifies the sink.
        assert "could not reach" in caplog.text
        assert url_fingerprint(self.WEBHOOK) in caplog.text

    async def test_httpx_is_not_muted_for_every_other_call_site(self, caplog):
        """Scoped to the alert path: the blunt fix would have cost the rest.

        ``logging.getLogger("httpx").setLevel(WARNING)`` at import time would
        have fixed this file and silently removed the request line from every
        adapter, probe and health check in the gateway.
        """
        with patch(
            "serving.observability.alerts.httpx.AsyncClient",
            _real_client_over(lambda _req: httpx.Response(200)),
        ):
            await _post_to_slack(self.WEBHOOK, "body", dedupe_key="k")

        # Same process, same logger, a call that is not an alert post.
        other = "https://example.test/v1/models?verbose=1"
        with caplog.at_level(logging.DEBUG):
            async with _REAL_ASYNC_CLIENT(
                transport=httpx.MockTransport(lambda _req: httpx.Response(200))
            ) as client:
                await client.get(other)

        assert other in caplog.text
        assert logging.getLogger("httpx").level == logging.NOTSET
        assert logging.getLogger("httpx").disabled is False


class TestTheRedactedFormNamesTheSinkOnly:
    """What replaces the URL has to be usable and has to not be the URL."""

    WEBHOOK = "https://hooks.slack.test/services/T00000000/B00000000/nOtArEaLtOkEn"

    def test_it_keeps_the_host_and_drops_everything_after_it(self):
        redacted = redact_url(self.WEBHOOK)
        assert redacted.startswith("https://hooks.slack.test/")
        assert "services" not in redacted
        assert "nOtArEaLtOkEn" not in redacted

    def test_two_webhooks_on_one_host_stay_distinguishable(self):
        """ "Which webhook did we post to" must survive the redaction."""
        other = self.WEBHOOK.replace("nOtArEaLtOkEn", "aLsOnOtReAl")
        assert redact_url(self.WEBHOOK) != redact_url(other)
        assert redact_url(self.WEBHOOK) == redact_url(self.WEBHOOK)

    def test_a_path_only_mention_is_scrubbed_too(self):
        """Some errors name the request target without the host in front."""
        scrubbed = scrub("POST /services/T00000000/B00000000/nOtArEaLtOkEn failed", self.WEBHOOK)
        assert "nOtArEaLtOkEn" not in scrubbed
        assert url_fingerprint(self.WEBHOOK) in scrubbed
