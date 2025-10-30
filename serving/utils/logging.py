"""Logging utilities with optional JSON formatter."""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from . import context as req_ctx


class JsonFormatter(logging.Formatter):
    """Format log records as JSON including request context metadata."""

    def format(self, record: logging.LogRecord) -> str:
        """Return a JSON-formatted representation of the log record.

        Args:
            record: The log record emitted by the logger.

        Returns:
            JSON encoded string for the log entry.
        """
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

    formatter = (
        JsonFormatter()
        if _env_is_json()
        else logging.Formatter(fmt="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    )

    # Console handler (stdout)
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    root.addHandler(console_handler)

    # File handler if LOG_FILE is set
    log_file = os.getenv("LOG_FILE")
    if log_file:
        file_handler = logging.FileHandler(log_file, mode="a", encoding="utf-8")
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)

    root.setLevel(level)


def get_logger(name: str | None = None) -> logging.Logger:
    """Get a module logger after ensuring logging is initialized."""
    setup_logging()
    return logging.getLogger(name or __name__)


__all__ = [
    "JsonFormatter",
    "get_logger",
    "setup_logging",
]
