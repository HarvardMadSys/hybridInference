"""Edge detection that gives the gateway's fire-only alerts a resolved state."""

from __future__ import annotations

import pytest

from serving.observability.alert_transitions import ThresholdTransitionTracker


def tracker(**kwargs: object) -> ThresholdTransitionTracker:
    defaults: dict[str, object] = {"clear_after_sec": 0.0, "stale_after_sec": None}
    defaults.update(kwargs)
    return ThresholdTransitionTracker(**defaults)  # type: ignore[arg-type]


def test_emits_one_firing_edge_then_stays_quiet_while_breached() -> None:
    t = tracker()
    assert t.observe("k", breached=True, now=0.0) == "firing"
    # A sustained breach must not re-notify: that is what the control plane's
    # occurrence counter on the existing parent message is for.
    for tick in (1.0, 2.0, 3.0):
        assert t.observe("k", breached=True, now=tick) is None
    assert t.is_firing("k")


def test_resolves_once_and_then_stays_quiet() -> None:
    t = tracker()
    t.observe("k", breached=True, now=0.0)
    assert t.observe("k", breached=False, now=1.0) == "resolved"
    assert not t.is_firing("k")
    # Healthy with no incident open is the overwhelmingly common evaluation and
    # must never produce a message.
    assert t.observe("k", breached=False, now=2.0) is None


def test_healthy_from_the_start_never_notifies() -> None:
    t = tracker()
    assert t.observe("k", breached=False, now=0.0) is None
    assert not t.is_firing("k")


def test_keys_are_independent() -> None:
    t = tracker()
    assert t.observe("a", breached=True, now=0.0) == "firing"
    assert t.observe("b", breached=True, now=0.0) == "firing"
    assert t.observe("a", breached=False, now=1.0) == "resolved"
    assert t.is_firing("b")


class TestFlapping:
    """A metric sitting on its threshold must not emit a transition per sample."""

    def test_a_dip_within_the_settling_period_does_not_resolve(self) -> None:
        t = tracker(clear_after_sec=120.0)
        assert t.observe("k", breached=True, now=0.0) == "firing"
        # Dips healthy, then breaches again before the incident may close.
        assert t.observe("k", breached=False, now=30.0) is None
        assert t.observe("k", breached=True, now=60.0) is None
        assert t.is_firing("k")

    def test_the_settling_clock_restarts_after_a_dip(self) -> None:
        t = tracker(clear_after_sec=120.0)
        t.observe("k", breached=True, now=0.0)
        t.observe("k", breached=False, now=30.0)
        t.observe("k", breached=True, now=60.0)
        # The clock runs from the moment the metric goes clear, and the breach
        # at 60 discarded the earlier one — so it starts again here, at 100.
        assert t.observe("k", breached=False, now=100.0) is None
        assert t.observe("k", breached=False, now=219.0) is None
        assert t.observe("k", breached=False, now=220.0) == "resolved"

    def test_resolves_once_the_metric_stays_clear(self) -> None:
        t = tracker(clear_after_sec=120.0)
        t.observe("k", breached=True, now=0.0)
        # Settling is measured from when the metric became healthy (60), not
        # from when it broke — "stay clear for 120s" is the property on-call
        # cares about.
        assert t.observe("k", breached=False, now=60.0) is None
        assert t.observe("k", breached=False, now=179.0) is None
        assert t.observe("k", breached=False, now=180.0) == "resolved"


class TestSilence:
    """Rules run on request records, so a breach then no traffic never re-evaluates."""

    def test_sweep_closes_an_incident_whose_rule_went_quiet(self) -> None:
        t = tracker(stale_after_sec=900.0)
        t.observe("k", breached=True, now=0.0)
        # Without this the incident stays open forever, holding principal quota
        # until it is exhausted and real outages start being suppressed.
        assert t.sweep(now=899.0) == []
        assert t.sweep(now=900.0) == ["k"]
        assert not t.is_firing("k")

    def test_sweep_leaves_a_recently_observed_incident_alone(self) -> None:
        t = tracker(stale_after_sec=900.0)
        t.observe("k", breached=True, now=0.0)
        t.observe("k", breached=True, now=800.0)
        assert t.sweep(now=1000.0) == []
        assert t.is_firing("k")

    def test_a_healthy_observation_also_refreshes_liveness(self) -> None:
        # Otherwise a metric that is clear but still inside its settling period
        # would be closed by the sweep rather than by the settling logic, and
        # the distinction matters for which resolution reason is reported.
        t = tracker(clear_after_sec=1200.0, stale_after_sec=900.0)
        t.observe("k", breached=True, now=0.0)
        t.observe("k", breached=False, now=800.0)
        assert t.sweep(now=1000.0) == []

    def test_sweep_returns_every_stale_key_sorted(self) -> None:
        t = tracker(stale_after_sec=900.0)
        for key in ("b", "a", "c"):
            t.observe(key, breached=True, now=0.0)
        assert t.sweep(now=900.0) == ["a", "b", "c"]

    def test_staleness_closing_can_be_disabled(self) -> None:
        t = tracker(stale_after_sec=None)
        t.observe("k", breached=True, now=0.0)
        assert t.sweep(now=10_000.0) == []
        assert t.is_firing("k")


def test_forget_drops_state_without_emitting() -> None:
    t = tracker()
    t.observe("k", breached=True, now=0.0)
    t.forget("k")
    assert not t.is_firing("k")
    # The next breach is a fresh incident, not a continuation.
    assert t.observe("k", breached=True, now=1.0) == "firing"


@pytest.mark.parametrize(
    "kwargs",
    [{"clear_after_sec": -1.0}, {"stale_after_sec": 0.0}, {"stale_after_sec": -1.0}],
)
def test_rejects_incoherent_configuration(kwargs: dict[str, float]) -> None:
    with pytest.raises(ValueError):
        tracker(**kwargs)


class TestStateAlerts:
    """An open circuit or a disconnected store is not a metric.

    It reports a condition the process already tracks, so there is exactly one
    healthy edge ever — a settling period would mean the incident never closes
    — and silence means nothing was observed, not that the condition cleared.
    """

    def state(self) -> ThresholdTransitionTracker:
        return ThresholdTransitionTracker(clear_after_sec=0.0, stale_after_sec=None)

    def test_the_single_healthy_edge_resolves(self) -> None:
        t = self.state()
        t.observe("circuit_open:zhipu", breached=True, now=0.0)
        # The breaker closes once and never reports again. Waiting for a second
        # healthy sample would leave the incident open forever.
        assert t.observe("circuit_open:zhipu", breached=False, now=1.0) == "resolved"

    def test_silence_never_resolves_an_open_circuit(self) -> None:
        t = self.state()
        t.observe("circuit_open:zhipu", breached=True, now=0.0)
        # Sweeping this would announce the outage as over while it is ongoing.
        assert t.sweep(now=100_000.0) == []
        assert t.is_firing("circuit_open:zhipu")


class TestUndeliveredResolutions:
    """observe/sweep clear the key before the caller knows if it was sent."""

    def test_rearm_restores_a_resolution_that_was_not_delivered(self) -> None:
        t = tracker()
        t.observe("k", breached=True, now=0.0)
        assert t.observe("k", breached=False, now=1.0) == "resolved"

        t.rearm("k", now=1.0)

        assert t.is_firing("k")
        # The next healthy observation retries rather than losing it outright.
        assert t.observe("k", breached=False, now=2.0) == "resolved"

    def test_rearm_after_a_sweep_lets_the_next_sweep_retry(self) -> None:
        t = tracker(stale_after_sec=900.0)
        t.observe("k", breached=True, now=0.0)
        assert t.sweep(now=900.0) == ["k"]

        t.rearm("k", now=900.0)

        assert t.sweep(now=1_000.0) == []
        assert t.sweep(now=1_800.0) == ["k"]

    def test_rearm_does_not_re_emit_a_firing_edge(self) -> None:
        # The incident was never closed, so re-announcing it would post a
        # duplicate outage message for an outage already reported.
        t = tracker()
        t.observe("k", breached=True, now=0.0)
        t.observe("k", breached=False, now=1.0)
        t.rearm("k", now=1.0)

        assert t.observe("k", breached=True, now=2.0) is None
