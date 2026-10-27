"""The ``alerts.runtime`` configuration group: alert channels and the failed-request detector.

Eight settings travel together as one immutable :class:`AlertRuntimeConfig` —
the three webhooks (``SLACK_ALERTS_WEBHOOK_URL``, ``SLACK_WEBHOOK_URL``,
``WECOM_ALERTS_WEBHOOK_URL``) and the five failed-request detector parameters —
because the channel choice, the Slack fallback and the detector's switch are
judged together. Every alert path reads them here (:func:`acquire`), never
from ``Settings``: one reference per use, so a send, a recovery or a detector
evaluation sees one consistent configuration from start to end.

Until an operator activates the group's contract the configuration is the one
the process booted with, exactly as before (each key is restart-required). Once
active, a saved change is validated as a whole, prepared and published by
:mod:`serving.config.live` without a restart. Preparing builds the channel
object and nothing else: no test message is sent, and no connection is opened.

``ALERTS_ENABLED``, ``alerts.yaml`` and the alert engine are not part of the
group: they keep their restart-required behavior.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from typing import TYPE_CHECKING, Any, Literal

from serving.config import live
from serving.config.app_config_registry import ALERTS_RUNTIME_GROUP, MANAGED_GROUPS

if TYPE_CHECKING:
    from collections.abc import Mapping

    from serving.config.revisions import SourceSnapshot
    from serving.config.settings import Settings
    from serving.observability.alerts import AlertSink

#: The group's name and the keys it owns, from the registry.
GROUP_NAME = ALERTS_RUNTIME_GROUP
CONTRACT = MANAGED_GROUPS[GROUP_NAME]
KEYS = CONTRACT.keys

#: ``Settings`` field of each key.
_FIELDS: dict[str, str] = {
    "SLACK_ALERTS_WEBHOOK_URL": "slack_alerts_webhook_url",
    "SLACK_WEBHOOK_URL": "slack_webhook_url",
    "WECOM_ALERTS_WEBHOOK_URL": "wecom_alerts_webhook_url",
    "FAILED_REQUEST_ALERT_THRESHOLD": "failed_request_alert_threshold",
    "FAILED_REQUEST_ALERT_WINDOW_MINUTES": "failed_request_alert_window_minutes",
    "FAILED_REQUEST_ALERT_COOLDOWN_MINUTES": "failed_request_alert_cooldown_minutes",
    "FAILED_REQUEST_ALERT_RATE": "failed_request_alert_rate",
    "FAILED_REQUEST_ALERT_RATE_MIN_COUNT": "failed_request_alert_rate_min_count",
}

Channel = Literal["slack", "wecom", "none", "conflict"]


@dataclass(frozen=True)
class AlertRuntimeConfig:
    """The alert channel and the failed-request detector parameters, as one value.

    The webhooks are credentials: they are left out of ``repr`` so that logging
    the configuration can never print one.
    """

    #: The Slack webhook alerts post to, after the ``SLACK_WEBHOOK_URL``
    #: fallback ``Settings`` applies.
    slack_alerts_webhook_url: str = field(default="", repr=False)
    slack_webhook_url: str = field(default="", repr=False)
    wecom_alerts_webhook_url: str = field(default="", repr=False)
    failed_request_alert_threshold: int = 200
    failed_request_alert_window_minutes: int = 5
    failed_request_alert_cooldown_minutes: int = 5
    failed_request_alert_rate: float = 0.10
    failed_request_alert_rate_min_count: int = 10

    def __post_init__(self) -> None:
        # The fallback Settings applies, so a value built directly selects the
        # same channel as one taken from Settings.
        if not self.slack_alerts_webhook_url and self.slack_webhook_url:
            object.__setattr__(self, "slack_alerts_webhook_url", self.slack_webhook_url)

    @classmethod
    def from_settings(cls, settings: Settings) -> AlertRuntimeConfig:
        """Take the group's fields from validated settings."""
        return cls(**{name: getattr(settings, name) for name in _FIELDS.values()})

    @property
    def channel(self) -> Channel:
        """The one channel the webhooks select. Blank (whitespace) is no channel."""
        slack = self.slack_alerts_webhook_url.strip()
        wecom = self.wecom_alerts_webhook_url.strip()
        if slack and wecom:
            return "conflict"
        if slack:
            return "slack"
        if wecom:
            return "wecom"
        return "none"

    @property
    def detector_enabled(self) -> bool:
        """Whether the failed-request detector evaluates.

        The predicate it has always had: ``SLACK_WEBHOOK_URL`` or
        ``WECOM_ALERTS_WEBHOOK_URL`` set. ``SLACK_ALERTS_WEBHOOK_URL`` alone
        does not enable it, and ``ALERTS_ENABLED`` plays no part.
        """
        return bool(self.slack_webhook_url.strip() or self.wecom_alerts_webhook_url.strip())

    def sink(self) -> AlertSink | None:
        """Return the channel to deliver through, or ``None``.

        Raises:
            ValueError: Both a Slack and a WeCom webhook are configured; the
                group refuses to publish that, so only a baseline can hold it.
        """
        from serving.observability.alerts import SlackSink, WeComSink

        channel = self.channel
        if channel == "conflict":
            raise ValueError(
                "SLACK_ALERTS_WEBHOOK_URL / SLACK_WEBHOOK_URL and "
                "WECOM_ALERTS_WEBHOOK_URL are mutually exclusive; "
                "configure one alert channel"
            )
        if channel == "slack":
            return SlackSink(self.slack_alerts_webhook_url.strip())
        if channel == "wecom":
            return WeComSink(self.wecom_alerts_webhook_url.strip())
        return None


@dataclass(frozen=True)
class AlertRuntimeTarget:
    """A target for the group: the typed configuration and each key's raw value."""

    config: AlertRuntimeConfig
    #: Raw resolved values by key, for the compatibility readers. Compared by
    #: the typed configuration only, so a respelling rebuilds nothing.
    raw: Mapping[str, str] = field(default_factory=dict, compare=False, repr=False)


class DetectorState:
    """Whether the failed-request detector job exists in this process."""

    #: ``unavailable`` (no scheduler or database), or ``registered``.
    state: str = "unavailable"


DETECTOR = DetectorState()


class AlertRuntimeComponent:
    """The group's component: projection, preparation and description."""

    def __init__(self) -> None:
        self._cache: tuple[tuple[tuple[str, str], ...], AlertRuntimeTarget] | None = None

    def baseline(self) -> tuple[AlertRuntimeTarget, AlertRuntimeConfig]:
        """The configuration this process booted with: the validated settings."""
        from serving.config import app_config
        from serving.config.settings import get_settings

        config = AlertRuntimeConfig.from_settings(get_settings())
        raw = {key: app_config.boot_raw(key) for key in KEYS}
        return AlertRuntimeTarget(config, raw), config

    def project(self, snapshot: SourceSnapshot) -> AlertRuntimeTarget:
        """Judge the saved values of all eight keys over the environment, as one.

        Raises:
            live.InvalidConfig: A stored value is not valid, or the channels
                conflict; the codes name the keys, never their values.
        """
        from serving.config import app_config

        stored = tuple(
            (key, row.value)
            for key in KEYS
            if (row := snapshot.rows.get(key)) is not None and not app_config.is_reset_row(row)
        )
        cached = self._cache
        if cached is not None and cached[0] == stored:
            return cached[1]
        settings, raw = app_config.project_settings(dict(stored), KEYS)
        target = AlertRuntimeTarget(AlertRuntimeConfig.from_settings(settings), raw)
        if target.config.channel == "conflict":
            raise live.InvalidConfig(
                "channels_conflict",
                ("SLACK_ALERTS_WEBHOOK_URL", "SLACK_WEBHOOK_URL", "WECOM_ALERTS_WEBHOOK_URL"),
            )
        self._cache = (stored, target)
        return target

    def same(self, desired: AlertRuntimeTarget, active: AlertRuntimeTarget) -> bool:
        """Equal typed configurations are the same target."""
        return desired.config == active.config

    async def prepare(
        self, desired: AlertRuntimeTarget, active: AlertRuntimeConfig
    ) -> AlertRuntimeConfig:
        """Check the channel can be built; send nothing, connect to nothing."""
        desired.config.sink()
        return desired.config

    async def discard(self, prepared: AlertRuntimeConfig) -> None:
        """Nothing to release: a configuration holds no connection."""

    async def retire(self, previous: AlertRuntimeConfig) -> None:
        """Nothing to release: a send already started keeps its own sink to the end."""

    def mirror(self, desired: AlertRuntimeTarget) -> Mapping[str, str]:
        """Each key's raw value."""
        return desired.raw

    def describe(self, view: AlertRuntimeConfig) -> dict[str, Any]:
        """The channel kind and the detector's state; never a webhook."""
        if DETECTOR.state != "registered":
            detector = "unavailable"
        else:
            detector = "running" if view.detector_enabled else "paused"
        return {"channel": view.channel, "detector": detector}

    def paused(self, view: AlertRuntimeConfig) -> bool:
        """Paused when no channel is configured: alerts are not delivered on purpose."""
        return view.channel == "none"

    def settings_fields(self, view: AlertRuntimeConfig) -> dict[str, Any]:
        """The ``Settings`` attributes mirroring *view*, for older readers."""
        return settings_fields(view)

    def reset(self) -> None:
        """Forget the cached projection (tests change the environment)."""
        self._cache = None


GROUP: live.LiveGroup[AlertRuntimeTarget, AlertRuntimeConfig] = live.LiveGroup(
    GROUP_NAME,
    schema_versions=(CONTRACT.schema_version,),
    keys=KEYS,
    component=AlertRuntimeComponent(),
)
live.COORDINATOR.register(GROUP)


def acquire() -> live.Published[AlertRuntimeTarget, AlertRuntimeConfig]:
    """Return the configuration in force, with its generation, for one use."""
    return GROUP.acquire()


def current() -> AlertRuntimeConfig:
    """Return the configuration in force."""
    return GROUP.acquire().view


def is_current(published: live.Published[Any, Any]) -> bool:
    """Whether *published* is still in force: the fence a long evaluation checks."""
    return GROUP.is_current(published)


def settings_fields(config: AlertRuntimeConfig) -> dict[str, Any]:
    """The ``Settings`` attributes mirroring *config*, for compatibility readers."""
    return {item.name: getattr(config, item.name) for item in fields(config)}


def set_detector_state(state: str) -> None:
    """Record whether the detector job exists, and report it."""
    DETECTOR.state = state
    GROUP.refresh_detail()
