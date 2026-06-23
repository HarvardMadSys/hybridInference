"""Logging utilities with optional JSON formatter."""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from . import context as req_ctx

_STRUCTURED_LOG_KEYS = (
    "event",
    "method",
    "path",
    "status_code",
    "duration_ms",
    "remote_ip",
    "peer_ip",
    "ip_source",
    "x_forwarded_for",
    "x_real_ip",
    "user_agent",
    "host",
    "origin",
    "referer",
    "request_id",
    "model",
    "provider",
    "session_id",
    "user_id",
    "key_prefix",
    "reason",
    "rewritten_path",
    "body_bytes",
    "upstream_status",
    "latency_ms",
    "error",
    "error_type",
    "strategy",
    "param_keys",
    # Structured event extras emitted by observability + routing codepaths.
    # When ``event`` is set, callers may attach any of these; serialize them
    # so JSON-mode logs preserve signal that alert rules + downstream tools
    # depend on.
    "task_name",
    "success",
    "from_provider",
    "to_provider",
    "ttft_ms",
    "endpoint_id",
    "key_index",
    "outcome",
    "remaining",
    "cooldown_sec",
    "elapsed_ms",
    "role",
    # RouteWise decision metadata (emitted by routers on every route choice).
    "model_id",
    "selected_provider_type",
    "selected_provider",
    "selected_endpoint_id",
    "hedging_triggered",
    "hedge_backup_provider",
    "hedge_backup_endpoint_id",
    "v_t",
    "gain_c",
    "gain_q",
    "gain_a",
    "theta_q",
    "lp_status",
    # Circuit-breaker state-change events (routing/routers.py).
    "consecutive_failures",
    "availability",
    "upstream_error",
    "offending_users",
)


def _format_extra_value(value: Any) -> str:
    """Format a structured extra value for plain logs."""
    if isinstance(value, str):
        return json.dumps(value)
    return json.dumps(value, default=str)


class PlainFormatter(logging.Formatter):
    """Plain formatter that still prints selected ``extra=`` fields."""

    def format(self, record: logging.LogRecord) -> str:
        """Append structured fields to the normal log line."""
        base = super().format(record)
        extras = [
            f"{key}={_format_extra_value(record.__dict__[key])}"
            for key in _STRUCTURED_LOG_KEYS
            if key in record.__dict__ and record.__dict__[key] is not None
        ]
        if not extras:
            return base
        return f"{base} {' '.join(extras)}"


class JsonFormatter(logging.Formatter):
    """Format log records as JSON including request context metadata."""

    def format(self, record: logging.LogRecord) -> str:
        """Return a JSON-formatted representation of the log record.

        Args:
            record: The log record emitted by the logger.

        Returns:
            JSON encoded string for the log entry.
        """
        record.message = record.getMessage()
        payload: dict[str, Any] = {
            "time": self.formatTime(record, datefmt="%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "name": record.name,
            "message": record.message,
        }
        # Merge request context fields if present
        try:
            ctx = req_ctx.get()
            for k in ("request_id", "model", "provider"):
                if k in ctx:
                    payload[k] = ctx[k]
        except Exception:
            pass
        # Merge well-known attributes passed via ``logger.*(extra=...)``.
        # Note: logging attaches items from ``extra`` into ``record.__dict__``.
        # Keys with hyphens (e.g., "x-session-id") are not valid attributes,
        # so ``hasattr`` will not work. We therefore read from ``__dict__``.
        for key in (*_STRUCTURED_LOG_KEYS, "headers", "age_sec"):
            if key in record.__dict__:
                payload[key] = record.__dict__[key]

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


_QUIET_PATHS = frozenset({"/health", "/health/deep", "/health/ready", "/metrics"})


class _QuietPathFilter(logging.Filter):
    """Suppress uvicorn access log lines for polling paths unless DEBUG is enabled."""

    def filter(self, record: logging.LogRecord) -> bool:
        """Return False (suppress) for quiet paths when the effective level is above DEBUG."""
        if logging.root.level <= logging.DEBUG:
            return True
        # Uvicorn access log args: (client_addr, method, path, http_version, status_code)
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3:
            return args[2] not in _QUIET_PATHS
        # Fallback for unexpected record formats
        msg = record.getMessage()
        return not any(f'"{path} ' in msg or f'"{path}"' in msg for path in _QUIET_PATHS)


def attach_quiet_access_filter() -> None:
    """Attach the quiet-path filter to uvicorn's access logger.

    Must be called after uvicorn's own logging setup (i.e., from the app
    lifespan), otherwise uvicorn's dictConfig will wipe the filter.
    Set LOG_LEVEL=DEBUG to disable suppression and see all access logs.
    """
    uvicorn_access = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, _QuietPathFilter) for f in uvicorn_access.filters):
        uvicorn_access.addFilter(_QuietPathFilter())


def _env_level() -> int:
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    return getattr(logging, level, logging.INFO)


def _env_is_json() -> bool:
    return os.getenv("LOG_FORMAT", "plain").lower() == "json"


def setup_logging() -> None:
    """Initialize or update root logger with env-controlled level and format."""
    root = logging.getLogger()
    level = _env_level()

    formatter: logging.Formatter = (
        JsonFormatter()
        if _env_is_json()
        else PlainFormatter(fmt="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    )

    # If handlers already exist (e.g., logging initialized before dotenv), update them.
    if root.handlers:
        root.setLevel(level)
        for h in root.handlers:
            h.setFormatter(formatter)
        return

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
    "PlainFormatter",
    "attach_quiet_access_filter",
    "get_logger",
    "setup_logging",
]
