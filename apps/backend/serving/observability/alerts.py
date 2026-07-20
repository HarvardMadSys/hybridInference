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
import re
import socket
import threading
import time
from collections import defaultdict
from typing import TYPE_CHECKING, Any, Literal, cast
from urllib.parse import urlparse
from uuid import uuid4

import httpx

from serving.oncall.models import AlertEvent, AlertEventV2, sanitize_for_agent

if TYPE_CHECKING:
    from pydantic import JsonValue

log = logging.getLogger(__name__)

_HOST = socket.gethostname()
_DEDUPE_LOCK = asyncio.Lock()
_LAST_FIRED: dict[tuple[str, str], float] = defaultdict(float)
_IN_FLIGHT: set[str] = set()

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
    if not explicit:
        # Built-in default URL with no override — almost certainly a local/dev
        # process, not production. Deployments set BASE_URL or DEPLOYMENT_ENV.
        return "local"
    if host.endswith("freeinference.org"):
        return "production"
    return "unknown"


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


def _format_message(severity: AlertSeverity, title: str, context: dict[str, Any]) -> str:
    ts = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    info = server_info()
    lines = [
        f"{_EMOJI[severity]} *{title}*",
        f"_{ts} · {info['environment']}_",
    ]
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


async def _post_to_alert_relay_v2(
    relay_url: str,
    token: str,
    event: AlertEventV2,
) -> bool:
    """Post a producer-neutral alert to the opt-in V2 relay."""
    if not _credential_free_https_url(relay_url):
        return False
    endpoint = f"{relay_url.rstrip('/')}/v2/alerts"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.post(
                endpoint,
                headers={"Authorization": f"Bearer {token}"},
                json=event.model_dump(mode="json", exclude_none=True),
            )
        if 200 <= response.status_code < 300:
            return True
        log.error("V2 alert relay returned HTTP %s", response.status_code)
    except Exception:
        log.exception("V2 alert relay post failed")
    return False


def _oncall_fingerprint(environment: str, dedupe_key: str) -> str:
    """Build a readable bounded fingerprint for relay-level incident dedupe."""
    value = f"gateway:{environment}:{dedupe_key}"
    if len(value) <= 512:
        return value
    digest = hashlib.sha256(value.encode()).hexdigest()
    return f"gateway:{environment}:sha256:{digest}"


def _alert_v2_fingerprint(dedupe_key: str) -> str:
    """Build a bounded fingerprint without producer-claimed environment identity."""
    value = f"gateway:{dedupe_key}"
    if len(value) <= 512:
        return value
    digest = hashlib.sha256(value.encode()).hexdigest()
    return f"gateway:sha256:{digest}"


def _credential_free_https_url(value: str) -> bool:
    """Return whether a credential-bearing request can safely use this URL."""
    try:
        parsed = urlparse(value)
        return bool(
            parsed.scheme == "https"
            and parsed.hostname
            and parsed.username is None
            and parsed.password is None
            and not parsed.query
            and not parsed.fragment
        )
    except ValueError:
        return False


def _oncall_context(context: dict[str, Any]) -> dict[str, JsonValue]:
    """Convert arbitrary alert values to bounded, redacted JSON."""
    serializable = json.loads(json.dumps(context, default=str))
    sanitized = sanitize_for_agent(cast("JsonValue", serializable))
    return cast("dict[str, JsonValue]", sanitized)


def _compact_json(value: JsonValue, string_limit: int, collection_limit: int) -> JsonValue:
    if isinstance(value, str):
        return value[:string_limit]
    if isinstance(value, list):
        return [
            _compact_json(item, string_limit, collection_limit) for item in value[:collection_limit]
        ]
    if isinstance(value, dict):
        return {
            key: _compact_json(child, string_limit, collection_limit)
            for key, child in list(value.items())[:collection_limit]
        }
    return value


def _alert_v2_context(context: dict[str, Any]) -> dict[str, JsonValue]:
    """Fit sanitized context within the relay's strict total-size contract."""
    sanitized = cast("JsonValue", _oncall_context(context))
    for string_limit, collection_limit in ((2_000, 50), (512, 25), (256, 15), (128, 10)):
        compacted = _compact_json(sanitized, string_limit, collection_limit)
        if len(json.dumps(compacted, separators=(",", ":")).encode()) <= 24_000:
            return cast("dict[str, JsonValue]", compacted)
    return {"_truncated": True}


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


def _build_alert_v2_event(
    severity: AlertSeverity,
    title: str,
    context: dict[str, Any],
    key: str,
    status: Literal["firing", "resolved"],
) -> AlertEventV2:
    """Create a bounded V2 event with no Slack text or environment field."""
    deployment_sha = _deployment_sha()
    return AlertEventV2(
        alert_id=str(uuid4()),
        fingerprint=_alert_v2_fingerprint(key),
        source="hybrid-inference-gateway",
        status=status,
        severity=severity.value,
        title=title[:500],
        occurred_at=dt.datetime.now(dt.timezone.utc),
        summary=title[:4_000],
        context=_alert_v2_context(context),
        deployment_sha=(
            deployment_sha
            if deployment_sha and re.fullmatch(r"[0-9a-fA-F]{40}", deployment_sha)
            else None
        ),
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
    relay_v2_url = os.environ.get("ALERT_RELAY_V2_URL", "").strip()
    relay_v2_token = os.environ.get("ALERT_RELAY_V2_TOKEN", "").strip()
    relay_v2_configured = bool(relay_v2_token and _credential_free_https_url(relay_v2_url))
    if not webhook_url and not relay_configured and not relay_v2_configured:
        return False

    # Admin-controlled global snooze: pause all alerts until a deadline.
    try:
        from serving.observability.alert_snooze import is_snoozed

        if await is_snoozed():
            return False
    except Exception:
        log.debug("alert snooze check failed; sending alert", exc_info=True)

    key = dedupe_key or f"{severity.value}:{title}"
    status_key = (key, status)
    now = _monotonic()
    async with _DEDUPE_LOCK:
        last = _LAST_FIRED.get(status_key, 0.0)
        if key in _IN_FLIGHT or (last > 0.0 and now - last < cooldown_sec):
            return False
        _IN_FLIGHT.add(key)

    sent = False
    try:
        message = _format_message(severity, title, context)
        if relay_v2_configured:
            try:
                event_v2 = _build_alert_v2_event(
                    severity,
                    title,
                    context,
                    key,
                    status,
                )
            except Exception:
                log.exception("V2 alert event construction failed")
            else:
                if await _post_to_alert_relay_v2(relay_v2_url, relay_v2_token, event_v2):
                    sent = True

        fallback_message = f"[Relay fallback]\n{message}" if relay_v2_configured else message
        if not sent and relay_configured:
            try:
                event = _build_oncall_event(
                    severity,
                    title,
                    context,
                    fallback_message,
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
            sent = await _post_to_slack(webhook_url, fallback_message)
        return sent
    except Exception:
        log.exception("alert_slack post raised; suppressing")
        return False
    finally:
        async with _DEDUPE_LOCK:
            _IN_FLIGHT.discard(key)
            if sent:
                _LAST_FIRED[status_key] = _monotonic()
                opposite = "resolved" if status == "firing" else "firing"
                _LAST_FIRED.pop((key, opposite), None)
