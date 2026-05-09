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
import socket
import time
from collections import defaultdict
from typing import Any

import httpx

log = logging.getLogger(__name__)

_HOST = socket.gethostname()
_DEDUPE_LOCK = asyncio.Lock()
_LAST_FIRED: dict[str, float] = defaultdict(float)


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
    lines = [
        f"{_EMOJI[severity]} *{title}*",
        f"_{ts} · {_HOST}_",
    ]
    if context:
        lines.append("")
        for k, v in context.items():
            label = k.replace("_", " ").title()
            lines.append(f"• *{label}:* {v}")
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
