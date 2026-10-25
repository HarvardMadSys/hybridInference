"""Batch inference settings, read live from database-backed configuration.

Every value resolves through :func:`serving.config.app_config.config_value`
(database row, then environment, then the default here) on each call, so an
administrator's change in Admin -> Configuration applies to the next request
and the next worker round without a restart. The entries themselves are
registered in ``serving.config.app_config_registry`` under the ``batch``
category.

Per-model concurrency caps are not here: they are rows an administrator edits
per model (:mod:`serving.batch.model_limits`); ``BATCH_MODEL_CONCURRENCY`` is
only the cap of a model without one.
"""

from __future__ import annotations

from dataclasses import dataclass

from serving.config.app_config import config_value
from serving.config.settings import VALID_ROLES, has_role

#: Batch inference is served at all. Off, every /v1/batches route answers 404
#: and the worker claims nothing; stored batches are kept.
ENABLED_KEY = "BATCH_ENABLED"
#: Lowest role that may create batches.
MIN_ROLE_KEY = "BATCH_MIN_ROLE"
#: Concurrent batch requests a model runs when it has no per-model cap.
MODEL_CONCURRENCY_KEY = "BATCH_MODEL_CONCURRENCY"
#: Interactive requests in flight on a model take batch slots from its cap.
YIELD_KEY = "BATCH_YIELD_TO_INTERACTIVE"
#: Requests one batch may hold.
MAX_REQUESTS_KEY = "BATCH_MAX_REQUESTS"
#: Size of a create request's body, in MiB.
MAX_BODY_MB_KEY = "BATCH_MAX_BODY_MB"
#: Batches one user may have queued or running at once.
MAX_ACTIVE_KEY = "BATCH_MAX_ACTIVE_PER_USER"
#: Hours a batch has to finish before its unstarted requests expire.
WINDOW_HOURS_KEY = "BATCH_COMPLETION_WINDOW_HOURS"
#: Days a finished batch, with its results, is kept before it is purged.
RETENTION_DAYS_KEY = "BATCH_RETENTION_DAYS"

DEFAULT_MIN_ROLE = "internal"
DEFAULT_MODEL_CONCURRENCY = 4
DEFAULT_MAX_REQUESTS = 50_000
DEFAULT_MAX_BODY_MB = 32
DEFAULT_MAX_ACTIVE_PER_USER = 10
DEFAULT_WINDOW_HOURS = 24
MAX_WINDOW_HOURS = 168
DEFAULT_RETENTION_DAYS = 7


@dataclass(frozen=True)
class BatchConfig:
    """One consistent reading of every batch setting."""

    enabled: bool = True
    min_role: str = DEFAULT_MIN_ROLE
    default_model_concurrency: int = DEFAULT_MODEL_CONCURRENCY
    yield_to_interactive: bool = True
    max_requests: int = DEFAULT_MAX_REQUESTS
    max_body_bytes: int = DEFAULT_MAX_BODY_MB * 1024 * 1024
    max_active_per_user: int = DEFAULT_MAX_ACTIVE_PER_USER
    completion_window_hours: int = DEFAULT_WINDOW_HOURS
    retention_days: int = DEFAULT_RETENTION_DAYS

    @property
    def completion_window_seconds(self) -> int:
        """The completion window in seconds."""
        return self.completion_window_hours * 3600


def _flag(key: str, default: bool) -> bool:
    raw = (config_value(key, "1" if default else "0") or "").strip()
    return raw != "0" if raw else default


def _int(key: str, default: int, *, minimum: int, maximum: int | None = None) -> int:
    raw = (config_value(key, str(default)) or "").strip()
    try:
        value = int(raw)
    except ValueError:
        return default
    if value < minimum or (maximum is not None and value > maximum):
        return default
    return value


def _role(key: str, default: str) -> str:
    raw = (config_value(key, default) or "").strip().lower()
    return raw if raw in VALID_ROLES else default


def batch_config() -> BatchConfig:
    """Read every batch setting now. Cheap: in-memory lookups only."""
    return BatchConfig(
        enabled=_flag(ENABLED_KEY, True),
        min_role=_role(MIN_ROLE_KEY, DEFAULT_MIN_ROLE),
        default_model_concurrency=_int(MODEL_CONCURRENCY_KEY, DEFAULT_MODEL_CONCURRENCY, minimum=0),
        yield_to_interactive=_flag(YIELD_KEY, True),
        max_requests=_int(MAX_REQUESTS_KEY, DEFAULT_MAX_REQUESTS, minimum=1),
        max_body_bytes=_int(MAX_BODY_MB_KEY, DEFAULT_MAX_BODY_MB, minimum=1) * 1024 * 1024,
        max_active_per_user=_int(MAX_ACTIVE_KEY, DEFAULT_MAX_ACTIVE_PER_USER, minimum=1),
        completion_window_hours=_int(
            WINDOW_HOURS_KEY, DEFAULT_WINDOW_HOURS, minimum=1, maximum=MAX_WINDOW_HOURS
        ),
        retention_days=_int(RETENTION_DAYS_KEY, DEFAULT_RETENTION_DAYS, minimum=1),
    )


def may_use_batch(role: str | None, config: BatchConfig | None = None) -> bool:
    """Whether a caller of *role* may create batches under *config* (default: now)."""
    config = config or batch_config()
    return config.enabled and has_role(role or "free", config.min_role)
