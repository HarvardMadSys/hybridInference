"""Single-sink Slack alerting helper used by all alert paths.

Reuses the post-to-webhook pattern previously embedded in
serving/admin/failed_request_alerter.py.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import enum
import hashlib
import json
import logging
import os
import platform
import socket
import threading
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, cast
from urllib.parse import urlparse
from uuid import uuid4

import httpx

from serving.oncall.models import AlertEvent, sanitize_for_agent

if TYPE_CHECKING:
    from collections.abc import Callable

    from pydantic import JsonValue

from serving.observability.alert_transitions import (
    ThresholdTransitionTracker,
)

log = logging.getLogger(__name__)

_HOST = socket.gethostname()
_DEDUPE_LOCK = asyncio.Lock()
_LAST_FIRED: dict[str, float] = defaultdict(float)
#: Keyed by dedupe key, each event fires when that key's send finishes. A
#: resolution waits on it instead of being dropped; see ``alert_slack``.
_IN_FLIGHT: dict[str, asyncio.Event] = {}

#: How long a resolution waits for an in-flight send of the same key, and how
#: many times. Bounded so a hung sink cannot pin the caller — the alert path
#: runs on the request loop for the health checks.
_RESOLUTION_WAIT_SEC = 10.0
_RESOLUTION_WAIT_ATTEMPTS = 2

# Hostnames that always indicate a non-deployed (local/dev) gateway.
_LOCAL_HOSTS = frozenset(("localhost", "127.0.0.1", "0.0.0.0", "::1", "host.docker.internal"))


def _detect_ip() -> str:
    """Best-effort primary IPv4 of this host. Empty string when undetectable.

    Opens a UDP socket and inspects the local address chosen for an external
    route — this resolves the outbound interface without sending any packet.
    Falls back to a hostname lookup, then to an empty string.
    """
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
        finally:
            sock.close()
    except Exception:
        try:
            return socket.gethostbyname(_HOST)
        except Exception:
            return ""


# Static host identity, resolved once off the event loop. ``socket.getfqdn()``
# may issue a blocking reverse-DNS lookup, so a background daemon thread warms
# this at import time; alerts that fire before it lands use non-blocking
# defaults rather than stalling the loop.
_STATIC_HOST_FACTS: tuple[str, str, str, str] | None = None


def _resolve_static_host_facts() -> None:
    global _STATIC_HOST_FACTS
    try:
        _STATIC_HOST_FACTS = (_HOST, socket.getfqdn(), _detect_ip(), platform.platform())
    except Exception:
        log.debug("static host-fact resolution failed", exc_info=True)


threading.Thread(target=_resolve_static_host_facts, name="alert-host-facts", daemon=True).start()


def _static_host_facts() -> tuple[str, str, str, str]:
    """Return resolved host identity, or non-blocking defaults if not yet ready."""
    facts = _STATIC_HOST_FACTS
    if facts is None:
        # platform.platform() and the cached hostname are cheap and never block.
        return _HOST, _HOST, "", platform.platform()
    return facts


def _base_url() -> tuple[str, bool]:
    """Resolve the gateway's public base URL and whether it was set explicitly.

    Prefers a per-call ``BASE_URL`` environment read so runtime changes are
    picked up, then falls back to settings. The boolean is ``True`` only when
    the value came from an explicit source (env var or ``.env``) rather than the
    built-in field default — which is needed to tell a real production deploy
    apart from a local run that inherits the default URL.
    """
    env_url = os.environ.get("BASE_URL")
    if env_url:
        return env_url, True
    try:
        from serving.config.settings import get_settings

        settings = get_settings()
        explicit = "base_url" in settings.model_fields_set
        if not explicit:
            # The built-in default is only a Settings fallback. Showing it in
            # alerts makes local/test gateways look like production.
            return "", False
        return (settings.base_url or ""), True
    except Exception:
        return "", False


def _detect_environment(base_url: str, *, explicit: bool) -> str:
    """Resolve the deployment environment for an alert.

    Honors an explicit ``DEPLOYMENT_ENV``/``ENVIRONMENT`` override, otherwise
    infers from the base URL host. ``explicit`` indicates whether ``base_url``
    was configured (vs the built-in default); an unconfigured base URL is
    treated as a local run rather than assumed to be production.
    """
    override = (os.environ.get("DEPLOYMENT_ENV") or os.environ.get("ENVIRONMENT") or "").strip()
    if override:
        return override
    try:
        host = (urlparse(base_url).hostname or "").lower()
    except ValueError:
        # Malformed base URL (e.g. an unclosed IPv6 literal). Never let a bad
        # config value abort the alert that is being formatted.
        return "unknown"
    if host in _LOCAL_HOSTS:
        return "local"
    if "staging" in host:
        return "staging"
    if not explicit or not host:
        # No configured base URL — almost certainly a local/dev process, not
        # production. Deployments set BASE_URL or DEPLOYMENT_ENV.
        return "local"
    # An explicitly configured public host is that operator's production. This
    # used to match one domain by name, which labelled every other deployment
    # "unknown" and put a distribution's hostname in upstream alerting logic.
    return "production"


def server_info() -> dict[str, str]:
    """Describe the gateway server emitting an alert.

    Combines static host identity (hostname, FQDN, IP, platform) with the
    current deployment environment and public base URL.
    """
    host, fqdn, ip, plat = _static_host_facts()
    base_url, explicit = _base_url()
    return {
        "hostname": host,
        "fqdn": fqdn,
        "ip": ip,
        "platform": plat,
        "base_url": base_url,
        "environment": _detect_environment(base_url, explicit=explicit),
    }


def _monotonic() -> float:
    return time.monotonic()


def reset_dedupe_state() -> None:
    """Test helper — clears in-memory dedupe table."""
    _LAST_FIRED.clear()
    _IN_FLIGHT.clear()


class AlertSeverity(str, enum.Enum):
    """Severity of an outgoing Slack alert."""

    CRITICAL = "critical"
    ERROR = "error"
    WARN = "warn"
    INFO = "info"


_EMOJI = {
    AlertSeverity.CRITICAL: "\U0001f6a8",  # rotating-light
    AlertSeverity.ERROR: "❌",  # cross-mark
    AlertSeverity.WARN: "⚠️",  # warning-sign
    AlertSeverity.INFO: "\u2139\ufe0f",  # information-source
}


def escape_slack_text(text: str) -> str:
    """Escape Slack mrkdwn control characters in untrusted text.

    Slack interprets ``<...>`` sequences specially (e.g. ``<!channel>`` pings a
    channel, ``<@U…>`` mentions a user). Any caller-controlled value that is
    interpolated into an alert must escape ``&``, ``<`` and ``>`` per Slack's
    guidelines so it renders literally instead of injecting mentions or links.
    ``&`` is escaped first to avoid double-encoding the others.
    """
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _format_message(
    severity: AlertSeverity,
    title: str,
    context: dict[str, Any],
    status: Literal["firing", "resolved"] = "firing",
) -> str:
    ts = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    info = server_info()
    # Titles are written as breach statements ("Provider circuit opened"), so a
    # resolution rendered with the breach's own severity emoji is indis-
    # tinguishable from the outage. The relay carries ``status`` as a field; the
    # plain webhook has only this text, so the recovery has to be said in it.
    heading = f"{_EMOJI[severity]} *{title}*" if status == "firing" else f"✅ *Recovered:* {title}"
    lines = [heading, f"_{ts} · {info['environment']}_"]
    if context:
        lines.append("")
        for k, v in context.items():
            label = k.replace("_", " ").title()
            lines.append(f"• *{label}:* {v}")
    lines.append("")
    lines.append("*Server*")
    host_line = f"{info['hostname']} ({info['ip']})" if info["ip"] else info["hostname"]
    lines.append(f"• *Host:* {host_line}")
    if info["fqdn"] and info["fqdn"] != info["hostname"]:
        lines.append(f"• *FQDN:* {info['fqdn']}")
    if info["platform"]:
        lines.append(f"• *Platform:* {info['platform']}")
    if info["base_url"]:
        lines.append(f"• *Base URL:* {info['base_url']}")
    return "\n".join(lines)


async def _post_to_slack(webhook_url: str, message: str) -> bool:
    """Post ``{"text": message}`` to Slack incoming webhook. Returns True on 2xx."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(webhook_url, json={"text": message})
        return 200 <= resp.status_code < 300
    except Exception:
        log.exception("slack webhook post failed")
        return False


async def _post_to_oncall(relay_url: str, token: str, event: AlertEvent) -> bool:
    """Post a structured alert to the oncall relay. Returns True on 2xx."""
    endpoint = f"{relay_url.rstrip('/')}/v1/alerts"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.post(
                endpoint,
                headers={"Authorization": f"Bearer {token}"},
                json=event.model_dump(mode="json"),
            )
        if 200 <= response.status_code < 300:
            return True
        log.error("codex oncall relay returned HTTP %s", response.status_code)
    except Exception:
        log.exception("codex oncall relay post failed")
    return False


def _oncall_fingerprint(environment: str, dedupe_key: str) -> str:
    """Build a readable bounded fingerprint for relay-level incident dedupe."""
    value = f"gateway:{environment}:{dedupe_key}"
    if len(value) <= 512:
        return value
    digest = hashlib.sha256(value.encode()).hexdigest()
    return f"gateway:{environment}:sha256:{digest}"


def _oncall_context(context: dict[str, Any]) -> dict[str, JsonValue]:
    """Convert arbitrary alert values to bounded, redacted JSON."""
    serializable = json.loads(json.dumps(context, default=str))
    sanitized = sanitize_for_agent(cast("JsonValue", serializable))
    return cast("dict[str, JsonValue]", sanitized)


def _deployment_sha() -> str | None:
    for name in ("DEPLOYMENT_SHA", "GIT_COMMIT", "COMMIT_SHA"):
        value = os.environ.get(name, "").strip()
        if value:
            return value[:128]
    return None


def _build_oncall_event(
    severity: AlertSeverity,
    title: str,
    context: dict[str, Any],
    message: str,
    key: str,
    status: Literal["firing", "resolved"],
    cooldown_sec: int,
) -> AlertEvent:
    """Create a bounded relay event from the existing Slack alert."""
    info = server_info()
    return AlertEvent(
        alert_id=str(uuid4()),
        fingerprint=_oncall_fingerprint(info["environment"], key),
        source="hybrid-inference-gateway",
        status=status,
        severity=severity.value,
        title=title[:500],
        environment=info["environment"],
        occurred_at=dt.datetime.now(dt.timezone.utc),
        summary=title[:4_000],
        context=_oncall_context(context),
        slack_text=message[:40_000],
        deployment_sha=_deployment_sha(),
        dedupe_window_seconds=min(max(cooldown_sec, 0), 604_800),
    )


async def alert_slack(
    severity: AlertSeverity,
    title: str,
    context: dict[str, Any],
    *,
    dedupe_key: str | None = None,
    cooldown_sec: int = 300,
    status: Literal["firing", "resolved"] = "firing",
) -> bool:
    """Send an alert through the oncall relay, falling back to Slack directly.

    Returns True if a message was actually sent, False otherwise.
    """
    webhook_url = os.environ.get("SLACK_ALERTS_WEBHOOK_URL", "") or os.environ.get(
        "SLACK_WEBHOOK_URL", ""
    )
    relay_url = os.environ.get("CODEX_ONCALL_RELAY_URL", "").strip()
    relay_token = os.environ.get("CODEX_ONCALL_RELAY_TOKEN", "").strip()
    relay_configured = bool(relay_url and relay_token)
    if not webhook_url and not relay_configured:
        return False

    # Both suppressions below exist to stop a *breach* from repeating, and
    # neither may swallow a resolution. Under the alert control plane a dropped
    # resolution leaves its incident open forever, holding principal quota until
    # it is exhausted and real outages start being suppressed; and closing an
    # incident is not the noise an operator silences alerts to avoid.
    resolution = status == "resolved"

    key = dedupe_key or f"{severity.value}:{title}"
    # A resolution waits for a send already running for this key rather than
    # being dropped by the guard. Dropping it costs a repeat for a breach —
    # another evaluation follows — but for a state alert the recovery edge is
    # the only one there is, so a discarded resolution strands the incident.
    #
    # Waiting is also what keeps one key's firing and resolved in order, and that
    # only works because nothing above suspends: from this function's first line
    # to the ``_IN_FLIGHT`` registration below there is no await that yields
    # (``_DEDUPE_LOCK`` is never held across one), so a firing always publishes
    # its marker before the resolution that closes it can look for it. Any await
    # added before that point reintroduces the inversion — the resolution finds
    # nothing to wait on, posts first, and the firing then lands after the
    # recovery it precedes, opening an incident whose only healthy edge is
    # already spent. That is why the snooze lookup runs *after* the
    # registration rather than here.
    for _ in range(_RESOLUTION_WAIT_ATTEMPTS):
        async with _DEDUPE_LOCK:
            done = _IN_FLIGHT.get(key)
        if done is None or not resolution:
            break
        try:
            await asyncio.wait_for(done.wait(), timeout=_RESOLUTION_WAIT_SEC)
        except (TimeoutError, asyncio.TimeoutError):
            # Keep waiting up to the attempt bound. A firing send that tries the
            # relay and then falls back to the webhook takes both timeouts, so
            # giving up on the first would drop exactly the resolution this
            # wait exists to save.
            continue

    now = _monotonic()
    async with _DEDUPE_LOCK:
        last = _LAST_FIRED.get(key, 0.0)
        # Two concurrent sends of the same key would duplicate rather than
        # repeat, so one of them still has to yield.
        if key in _IN_FLIGHT:
            return False
        if not resolution and last > 0.0 and now - last < cooldown_sec:
            return False
        _IN_FLIGHT[key] = asyncio.Event()

    sent = False
    try:
        # Admin-controlled global snooze: pause all alerts until a deadline.
        # Deliberately inside the try, after the in-flight registration: the
        # lookup awaits, and a firing that suspends here before publishing its
        # marker is exactly the inversion described above. The early return is
        # safe from here — the ``finally`` releases the marker, and ``sent`` stays
        # False so a snoozed breach does not consume its cooldown either.
        if not resolution:
            try:
                from serving.observability.alert_snooze import is_snoozed

                if await is_snoozed():
                    return False
            except Exception:
                log.debug("alert snooze check failed; sending alert", exc_info=True)
        message = _format_message(severity, title, context, status)
        if relay_configured:
            try:
                event = _build_oncall_event(
                    severity,
                    title,
                    context,
                    message,
                    key,
                    status,
                    cooldown_sec,
                )
            except Exception:
                log.exception("codex oncall event construction failed")
            else:
                if await _post_to_oncall(relay_url, relay_token, event):
                    sent = True
        if not sent and webhook_url:
            sent = await _post_to_slack(webhook_url, message)
        return sent
    except Exception:
        log.exception("alert_slack post raised; suppressing")
        return False
    finally:
        async with _DEDUPE_LOCK:
            done = _IN_FLIGHT.pop(key, None)
            if done is not None:
                done.set()
            if sent and resolution:
                # The breach is over, so the next one must page immediately
                # rather than serve out the cooldown this incident started.
                _LAST_FIRED.pop(key, None)
            elif sent:
                _LAST_FIRED[key] = _monotonic()


#: Metric alerts: a quantity recomputed from a rolling window of request
#: records. Shared across every rule so one sweep closes incidents for all of
#: them, and so a rule reloaded with new config does not lose which breaches are
#: open. ``stale_after_sec`` is raised at engine start to exceed the longest
#: configured rule window — see ``alert_rules``.
_TRANSITIONS = ThresholdTransitionTracker()

#: State alerts: a condition the process already tracks (an open circuit, a
#: disconnected store). These report exactly one healthy edge ever, so a
#: settling period would mean the incident never closes; and silence is not
#: recovery, so sweeping one would announce the outage as over while it is
#: still happening.
_STATE_TRANSITIONS = ThresholdTransitionTracker(
    clear_after_sec=0.0,
    stale_after_sec=None,
)

#: How often to look for breaches nothing is evaluating any more. Well under
#: the tracker's own staleness threshold so a stale incident closes promptly
#: once it qualifies, rather than at the next multiple of a long interval.
_STALE_SWEEP_INTERVAL_SEC = 60


@dataclass
class _PendingResolution:
    """A resolution whose delivery failed, kept for the sweep timer to retry.

    Re-arming the tracker alone is not a retry path for every alert shape: a
    state alert (circuit breaker, store health) reports exactly one healthy
    edge and is never swept, so after a failed send nothing would ever call
    ``observe`` again and its only resolution would be lost. A metric alert
    fares little better — the re-armed key must sit through a fresh settling
    period, and only if traffic continues. Recording the failed send here lets
    the periodic sweep retry the delivery itself, independent of observations.
    """

    kind: Literal["metric", "state"]
    title: str


#: Failed resolution sends by key, retried each sweep tick. In-memory like the
#: trackers: a restart loses it, which degrades to the pre-#1076 behaviour
#: (the incident's close is never announced), never to a wrong announcement.
_PENDING_RESOLUTIONS: dict[str, _PendingResolution] = {}


def reset_transition_state() -> None:
    """Drop all open-breach state. For tests and for a clean engine restart."""
    for tracker_ in (_TRANSITIONS, _STATE_TRANSITIONS):
        tracker_._firing.clear()
        tracker_._bounds.clear()
    _PENDING_RESOLUTIONS.clear()


async def alert_on_transition(
    *,
    key: str,
    breached: bool,
    severity: AlertSeverity,
    title: str,
    context: Callable[[], dict[str, Any]],
    cooldown_sec: int,
    kind: Literal["metric", "state"] = "metric",
    stale_after: float | None = None,
    now: float | None = None,
) -> bool:
    """Send only when the breach state changes, so incidents open and close once.

    Rules previously returned silently while healthy, which is why every alert
    was fire-only: the moment a breach ended was observable and thrown away.
    Routing that same decision through the tracker turns it into the resolution
    the control plane needs to close the incident, while a sustained breach
    still notifies exactly once.

    The tracker's only job here is the *resolution* edge. Breach reporting is
    left exactly as it was — every breached evaluation reaches the sink and its
    cooldown decides what becomes a message — because those repeats are what
    advance the incident's occurrence count and "last seen" on the control
    plane. What was missing was never the repeat, only the close.

    ``context`` is a callable so the breach detail — counters, top-N summaries —
    is only built when a message is actually attempted. A resolution
    carries just the metric identity, since breach numbers describe a healthy
    system by then and would only mislead on the recovery card.
    """
    tracker = _TRANSITIONS if kind == "metric" else _STATE_TRANSITIONS
    moment = now or time.time()
    transition = tracker.observe(
        key,
        breached=breached,
        now=moment,
        stale_after=stale_after,
    )
    if breached:
        # The incident is (still or again) real, so any resolution queued for a
        # retry is stale — sending it later would announce a live breach as
        # recovered.
        _PENDING_RESOLUTIONS.pop(key, None)
        # Every breached evaluation still goes to the sink, exactly as before.
        # The cooldown there decides whether it becomes a message, and under the
        # control plane each repeat is what advances the incident's occurrence
        # count and "last seen" — suppressing them here would freeze the card at
        # one occurrence and make a long outage look like a stale alert.
        return await alert_slack(
            severity,
            title,
            context(),
            dedupe_key=key,
            cooldown_sec=cooldown_sec,
        )
    if transition != "resolved":
        return False
    sent = await alert_slack(
        AlertSeverity.INFO,
        f"Recovered: {title}",
        {"alert": key},
        dedupe_key=key,
        cooldown_sec=cooldown_sec,
        status="resolved",
    )
    if sent:
        # Confirmed close — but only when no re-breach slipped in while the
        # send was in flight. ``observe`` deleted the firing state on the
        # resolved edge, so any state present *now* is a new incident opened by
        # a concurrent ``breached=True`` call; forgetting it would untrack a
        # live breach and, for a state alert, silently spend the only healthy
        # edge its close will ever get. On a clean close ``forget`` just drops
        # the staleness bound, which dynamic keys (per-user cost, per-period
        # budgets) would otherwise leak one entry each.
        if not tracker.is_firing(key):
            tracker.forget(key)
        _PENDING_RESOLUTIONS.pop(key, None)
    else:
        # ``observe`` already cleared the key, so without this the only
        # resolution it will ever produce is gone and the incident stays open
        # with nothing able to close it. Re-arming alone only helps a rule that
        # keeps observing; the pending entry lets the sweep timer retry the
        # send itself, which is the sole retry path for a state alert whose
        # single healthy edge is already spent.
        tracker.rearm(key, moment)
        _PENDING_RESOLUTIONS[key] = _PendingResolution(kind=kind, title=title)
    return sent


async def sweep_stale_breaches() -> None:
    """Resolve breaches that nothing is evaluating any more.

    Two classes never re-evaluate themselves: a rule whose traffic stopped
    entirely, and periodic budget checks whose incident key embeds the day or
    hour, so the previous period is never observed again. Without this both
    would hold their incident — and its principal quota — forever.

    Only metric alerts are swept. For a state alert silence means nothing was
    observed, not that the condition cleared, so sweeping one would report an
    ongoing outage as recovered.
    """
    now = time.time()

    # First, retry resolutions whose delivery failed on their transition edge.
    # This runs off observations entirely: for a state alert it is the only
    # retry there is, and for a metric alert it beats waiting out a fresh
    # settling period that may never come if traffic stopped.
    for key, pending in list(_PENDING_RESOLUTIONS.items()):
        if _PENDING_RESOLUTIONS.get(key) is not pending:
            # A re-breach cancelled this entry after the snapshot: the breach
            # is live again and this recovery would announce it as over.
            continue
        tracker = _TRANSITIONS if pending.kind == "metric" else _STATE_TRANSITIONS
        sent = False
        try:
            sent = await alert_slack(
                AlertSeverity.INFO,
                f"Recovered: {pending.title}",
                {"alert": key},
                dedupe_key=key,
                cooldown_sec=0,
                status="resolved",
            )
        except Exception:
            # One stuck resolution must not strand every other pending one.
            log.exception("pending resolution retry failed for %s", key)
        # Re-validate identity after the await too: a re-breach while the send
        # was in flight popped this entry, and the firing state it holds now
        # belongs to a live incident — ``forget`` would untrack it and spend
        # its future healthy edge. The stale "Recovered" text may already have
        # reached the channel (nothing can unsend it); what matters is that
        # the tracker stays correct so the next real transition re-announces
        # and eventually closes properly. The check is race-free because no
        # await sits between the send returning and this line.
        if sent and _PENDING_RESOLUTIONS.get(key) is pending:
            _PENDING_RESOLUTIONS.pop(key, None)
            # Confirmed close: clears the re-armed firing state and the key's
            # staleness bound in one step.
            tracker.forget(key)

    for key in _TRANSITIONS.sweep(now):
        sent = False
        try:
            sent = await alert_slack(
                AlertSeverity.INFO,
                # "No recent samples" is a materially weaker claim than an
                # observed-clear recovery: the rule stopped seeing data (idle
                # traffic, a drained process), so the breach can no longer be
                # evaluated. Say so, instead of wording that reads as if the
                # metric was measured healthy.
                f"Recovered (no recent samples): {key}",
                {"alert": key, "reason": "no samples within the rule window"},
                dedupe_key=key,
                cooldown_sec=0,
                status="resolved",
            )
        except Exception:
            # One stuck resolution must not strand every other open incident.
            log.exception("stale breach resolution failed for %s", key)
        if _TRANSITIONS.is_firing(key):
            # The metric re-breached while the send was in flight and opened a
            # fresh incident: leave its state (and the bound the new ``observe``
            # just set) alone. Forgetting would untrack the live breach;
            # re-arming would overwrite its liveness clock with a backdated one
            # and set up a premature no-samples close.
            continue
        if sent:
            # Confirmed close: without this, dynamic keys (per-user cost,
            # per-period budgets) each leave a ``_bounds`` entry behind forever.
            _TRANSITIONS.forget(key)
        else:
            # The sweep already dropped the key, so leaving it dropped would
            # lose the resolution outright. Re-arm stale enough that the *next*
            # sweep retries: plain re-arming would restart the staleness clock
            # and, with a long rule window, push the retry hours out while the
            # incident stays open.
            _TRANSITIONS.rearm(key, now, retry_in=_STALE_SWEEP_INTERVAL_SEC)
