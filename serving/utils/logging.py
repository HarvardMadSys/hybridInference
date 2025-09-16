"""
Logging utilities with optional JSON formatter.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from . import context as req_ctx


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "time": self.formatTime(record, datefmt="%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "name": record.name,
            "message": record.getMessage(),
        }
        # Merge request context fields if present
        try:
            ctx = req_ctx.get()
            for k in ("request_id", "model", "provider"):
                if k in ctx:
                    payload[k] = ctx[k]
        except Exception:
            pass
        # Merge well-known attributes passed via ``logger.*(extra=...)``
        for key in (
            "method",
            "path",
            "status_code",
            "duration_ms",
            "remote_ip",
            "x_forwarded_for",
            "user_agent",
            "host",
            "request_id",
            "model",
            "provider",
        ):
            if hasattr(record, key):
                payload[key] = getattr(record, key)

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def _env_level() -> int:
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    return getattr(logging, level, logging.INFO)


def _env_is_json() -> bool:
    return os.getenv("LOG_FORMAT", "plain").lower() == "json"


def setup_logging() -> None:
    """Initialize root logger once with configured level and format."""
    root = logging.getLogger()
    level = _env_level()

    if root.handlers:
        root.setLevel(level)
        return

    handler = logging.StreamHandler()
    formatter = (
        JsonFormatter()
        if _env_is_json()
        else logging.Formatter(fmt="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    )
    handler.setFormatter(formatter)

    root.setLevel(level)
    root.addHandler(handler)


def get_logger(name: str | None = None) -> logging.Logger:
    """Get a module logger after ensuring logging is initialized."""
    setup_logging()
    return logging.getLogger(name or __name__)


__all__ = [
    "JsonFormatter",
    "get_logger",
    "setup_logging",
]
