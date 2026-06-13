"""Single-sink Slack alerting helper used by all alert paths.

Reuses the post-to-webhook pattern previously embedded in
serving/admin/failed_request_alerter.py.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import enum
import logging
import os
import platform
import socket
import threading
import time
from collections import defaultdict
from typing import Any
from urllib.parse import urlparse

import httpx

log = logging.getLogger(__name__)

_HOST = socket.gethostname()
_DEDUPE_LOCK = asyncio.Lock()
_LAST_FIRED: dict[str, float] = defaultdict(float)

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
        return (settings.base_url or ""), explicit
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
    host = (urlparse(base_url).hostname or "").lower()
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


async def alert_slack(
    severity: AlertSeverity,
    title: str,
    context: dict[str, Any],
    *,
    dedupe_key: str | None = None,
    cooldown_sec: int = 300,
) -> bool:
    """Send a Slack alert. No-op if webhook unset or within cooldown.

    Returns True if a message was actually sent, False otherwise.
    """
    webhook_url = os.environ.get("SLACK_ALERTS_WEBHOOK_URL", "") or os.environ.get(
        "SLACK_WEBHOOK_URL", ""
    )
    if not webhook_url:
        return False

    key = dedupe_key or f"{severity.value}:{title}"
    now = _monotonic()
    async with _DEDUPE_LOCK:
        last = _LAST_FIRED.get(key, 0.0)
        if last > 0.0 and now - last < cooldown_sec:
            return False
        _LAST_FIRED[key] = now

    message = _format_message(severity, title, context)
    try:
        return await _post_to_slack(webhook_url, message)
    except Exception:
        log.exception("alert_slack post raised; suppressing")
        return False
