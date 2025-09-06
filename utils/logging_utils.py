"""Lightweight logging utilities with optional JSON formatting.

Environment variables:
- LOG_LEVEL: DEBUG|INFO|WARNING|ERROR (default: INFO)
- LOG_FORMAT: json|plain (default: plain)
"""

from __future__ import annotations

import json
import logging
import os


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "time": self.formatTime(record, datefmt="%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "name": record.name,
            "message": record.getMessage(),
        }
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
