"""Tests for serving.observability.alerts.alert_slack."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from serving.observability.alerts import (
    _EMOJI,
    _PENDING_RESOLUTIONS,
    _STATE_TRANSITIONS,
    _TRANSITIONS,
    AlertSeverity,
    _base_url,
    _detect_environment,
    _format_message,
    alert_on_transition,
    alert_slack,
    reset_dedupe_state,
    reset_transition_state,
    server_info,
    sweep_stale_breaches,
)


@pytest.fixture(autouse=True)
def reset_state(monkeypatch):
    monkeypatch.delenv("CODEX_ONCALL_RELAY_URL", raising=False)
    monkeypatch.delenv("CODEX_ONCALL_RELAY_TOKEN", raising=False)
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


async def test_alert_slack_prefers_oncall_relay(monkeypatch):
    monkeypatch.setenv("CODEX_ONCALL_RELAY_URL", "https://oncall.internal/")
    monkeypatch.setenv("CODEX_ONCALL_RELAY_TOKEN", "relay-secret")
    monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://hooks.slack.com/fallback")
    with (
        patch(
            "serving.observability.alerts._post_to_oncall",
            new=AsyncMock(return_value=True),
        ) as mock_oncall,
        patch("serving.observability.alerts._post_to_slack", new=AsyncMock()) as mock_slack,
    ):
        sent = await alert_slack(
            AlertSeverity.ERROR,
            "Provider failed",
            {"provider": "openai", "api_key": "must-not-leak"},
            dedupe_key="provider:openai",
        )

    assert sent is True
    mock_slack.assert_not_called()
    relay_url, token, event = mock_oncall.call_args.args
    assert relay_url == "https://oncall.internal/"
    assert token == "relay-secret"
    assert event.fingerprint.endswith(":provider:openai")
    assert event.context["provider"] == "openai"
    assert event.context["api_key"] == "[REDACTED]"


async def test_alert_slack_falls_back_when_oncall_relay_fails(monkeypatch):
    monkeypatch.setenv("CODEX_ONCALL_RELAY_URL", "https://oncall.internal")
    monkeypatch.setenv("CODEX_ONCALL_RELAY_TOKEN", "relay-secret")
    monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://hooks.slack.com/fallback")
    with (
        patch(
            "serving.observability.alerts._post_to_oncall",
            new=AsyncMock(return_value=False),
        ),
        patch(
            "serving.observability.alerts._post_to_slack",
            new=AsyncMock(return_value=True),
        ) as mock_slack,
    ):
        sent = await alert_slack(AlertSeverity.ERROR, "Provider failed", {})

    assert sent is True
    mock_slack.assert_awaited_once()
    assert mock_slack.call_args.args[0] == "https://hooks.slack.com/fallback"


async def test_alert_slack_can_deliver_through_relay_without_webhook(monkeypatch):
    monkeypatch.setenv("CODEX_ONCALL_RELAY_URL", "https://oncall.internal")
    monkeypatch.setenv("CODEX_ONCALL_RELAY_TOKEN", "relay-secret")
    monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "")
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "")
    with patch(
        "serving.observability.alerts._post_to_oncall",
        new=AsyncMock(return_value=True),
    ) as mock_oncall:
        sent = await alert_slack(AlertSeverity.WARN, "Latency high", {"p95_ms": 70_000})

    assert sent is True
    mock_oncall.assert_awaited_once()


async def test_failed_delivery_does_not_consume_cooldown(monkeypatch):
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

        async def slow_post(_url, message):
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


class TestTheResolutionWaitSpansBothSinks:
    async def test_a_timeout_does_not_end_the_wait_after_one_attempt(self, monkeypatch):
        """A firing send that tries the relay then the webhook takes both timeouts.

        Giving up on the first would drop exactly the resolution this wait
        exists to save.
        """
        monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://hooks.slack.com/x")
        monkeypatch.setattr("serving.observability.alerts._RESOLUTION_WAIT_SEC", 0.01)
        reset_transition_state()
        reset_dedupe_state()
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow_post(_url, message):
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

        async def post(_url, message):
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

        async def post(_url, message):
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

        async def post(_url, message):
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
