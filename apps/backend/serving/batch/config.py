"""Batch inference settings, read live from database-backed configuration.

Every value resolves through :func:`serving.config.app_config.config_value`
(database row, then environment, then the default here) on each call, so an
administrator's change in Admin -> Settings applies to the next request
and the next worker round without a restart. The entries themselves are
registered in ``serving.config.app_config_registry`` under the ``batch``
category.

Per-model concurrency caps are not here: they are rows an administrator edits
per model (:mod:`serving.batch.model_limits`); ``BATCH_MODEL_CONCURRENCY`` is
only the cap of a model without one.

A user's batch work is bounded by its own per-role quota
(``BATCH_ROLE_CONCURRENCY_<ROLE>``), not by the role's interactive concurrency
limit: a batch neither waits on its owner's interactive requests nor takes the
slots they need.

``BATCH_ENABLED`` is the one switch: off, every batch route answers 404
(results and management included) and nothing is claimed. On, every batch kind
this version runs is available -- inline and file batches, the Chat,
Embeddings, Messages and Responses targets, and webhooks -- to the
roles ``BATCH_MIN_ROLE`` admits. ``BATCH_EXPANSION_ENABLED``, which once held
the newer kinds back, is retired (``RETIRED_SETTINGS`` in the registry): a
value left in the environment or the database controls nothing here. See
``docs/agents/specs/2026-10-27-batch-default-availability-amendment.zh.md``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from serving.config.app_config import config_value
from serving.config.settings import VALID_ROLES, has_role

#: Batch inference is served at all. Off, every batch route answers 404 and
#: the worker claims nothing; stored batches are kept.
ENABLED_KEY = "BATCH_ENABLED"
#: Lowest role that may create batches.
MIN_ROLE_KEY = "BATCH_MIN_ROLE"
#: Concurrent batch requests a model runs when it has no per-model cap.
MODEL_CONCURRENCY_KEY = "BATCH_MODEL_CONCURRENCY"
#: Interactive requests in flight on a model take batch slots from its cap.
YIELD_KEY = "BATCH_YIELD_TO_INTERACTIVE"
#: ... counted across every gateway process, not only this one.
SHARED_LOAD_KEY = "BATCH_SHARED_INTERACTIVE_LOAD"
#: Requests one batch may hold.
MAX_REQUESTS_KEY = "BATCH_MAX_REQUESTS"
#: Size of a create request's body (and of an uploaded input file), in MiB.
MAX_BODY_MB_KEY = "BATCH_MAX_BODY_MB"
#: Batches one user may have queued or running at once.
MAX_ACTIVE_KEY = "BATCH_MAX_ACTIVE_PER_USER"
#: Unfinished requests one user may have, across every batch.
MAX_QUEUED_KEY = "BATCH_MAX_QUEUED_REQUESTS_PER_USER"
#: Hours an inline batch has to finish before its unstarted requests expire.
WINDOW_HOURS_KEY = "BATCH_COMPLETION_WINDOW_HOURS"
#: Days a finished batch, with its results, is kept before it is purged.
RETENTION_DAYS_KEY = "BATCH_RETENTION_DAYS"
#: Largest line of an input file, in KiB.
MAX_LINE_KB_KEY = "BATCH_MAX_LINE_KB"
#: Largest stored result of one request, in KiB.
MAX_RESULT_KB_KEY = "BATCH_MAX_RESULT_KB"
#: Bytes of uploaded files one user may store, in MiB.
FILES_MAX_TOTAL_MB_KEY = "BATCH_FILES_MAX_TOTAL_MB_PER_USER"
#: Files one user may upload per hour.
FILE_UPLOADS_PER_HOUR_KEY = "BATCH_FILE_UPLOADS_PER_HOUR"
#: Days an uploaded file is kept.
FILE_RETENTION_DAYS_KEY = "BATCH_FILE_RETENTION_DAYS"
#: Batch requests one user of each role may have running at once in a gateway
#: process -- the batch counterpart of the role's interactive concurrency limit.
ROLE_CONCURRENCY_KEYS: dict[str, str] = {
    role: f"BATCH_ROLE_CONCURRENCY_{role.upper()}" for role in ("free", "pro", "internal", "admin")
}
#: Slots of every model's batch cap kept free for interactive traffic.
INTERACTIVE_RESERVE_KEY = "BATCH_INTERACTIVE_RESERVE"
#: Anthropic API versions a new Messages batch may name (its ``anthropic-version`` header).
ANTHROPIC_VERSIONS_KEY = "BATCH_ANTHROPIC_VERSIONS"
#: Webhooks may target private, loopback or link-local addresses (testing only).
WEBHOOK_ALLOW_PRIVATE_KEY = "BATCH_WEBHOOK_ALLOW_PRIVATE_TARGETS"

DEFAULT_MIN_ROLE = "internal"
DEFAULT_MODEL_CONCURRENCY = 64
DEFAULT_MAX_REQUESTS = 50_000
DEFAULT_MAX_BODY_MB = 32
DEFAULT_MAX_ACTIVE_PER_USER = 10
DEFAULT_MAX_QUEUED_PER_USER = 100_000
DEFAULT_WINDOW_HOURS = 24
MAX_WINDOW_HOURS = 168
DEFAULT_RETENTION_DAYS = 7
DEFAULT_MAX_LINE_KB = 4096
DEFAULT_MAX_RESULT_KB = 4096
DEFAULT_FILES_MAX_TOTAL_MB = 512
DEFAULT_FILE_UPLOADS_PER_HOUR = 60
DEFAULT_FILE_RETENTION_DAYS = 30
#: 0 means no cap for that role.
DEFAULT_ROLE_CONCURRENCY: dict[str, int] = {"free": 8, "pro": 16, "internal": 64, "admin": 64}
DEFAULT_INTERACTIVE_RESERVE = 0
#: Every Anthropic version a batch can be validated and run under (see
#: :mod:`serving.batch.profiles`); the setting chooses among these.
KNOWN_ANTHROPIC_VERSIONS: tuple[str, ...] = ("2023-06-01", "2026-10-21")
DEFAULT_ANTHROPIC_VERSIONS = ",".join(KNOWN_ANTHROPIC_VERSIONS)

#: The window of file batches: the protocol fixes it at 24 hours.
PROTOCOL_WINDOW_SECONDS = 24 * 3600


@dataclass(frozen=True)
class BatchConfig:
    """One consistent reading of every batch setting."""

    enabled: bool = True
    min_role: str = DEFAULT_MIN_ROLE
    default_model_concurrency: int = DEFAULT_MODEL_CONCURRENCY
    yield_to_interactive: bool = True
    shared_interactive_load: bool = True
    max_requests: int = DEFAULT_MAX_REQUESTS
    max_body_bytes: int = DEFAULT_MAX_BODY_MB * 1024 * 1024
    max_active_per_user: int = DEFAULT_MAX_ACTIVE_PER_USER
    max_queued_per_user: int = DEFAULT_MAX_QUEUED_PER_USER
    completion_window_hours: int = DEFAULT_WINDOW_HOURS
    retention_days: int = DEFAULT_RETENTION_DAYS
    max_line_bytes: int = DEFAULT_MAX_LINE_KB * 1024
    max_result_bytes: int = DEFAULT_MAX_RESULT_KB * 1024
    files_max_total_bytes: int = DEFAULT_FILES_MAX_TOTAL_MB * 1024 * 1024
    file_uploads_per_hour: int = DEFAULT_FILE_UPLOADS_PER_HOUR
    file_retention_days: int = DEFAULT_FILE_RETENTION_DAYS
    role_concurrency: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_ROLE_CONCURRENCY))
    interactive_reserve: int = DEFAULT_INTERACTIVE_RESERVE
    anthropic_versions: tuple[str, ...] = KNOWN_ANTHROPIC_VERSIONS
    webhook_allow_private: bool = False

    @property
    def completion_window_seconds(self) -> int:
        """The completion window of an inline batch, in seconds."""
        return self.completion_window_hours * 3600

    @property
    def retention_seconds(self) -> int:
        """How long a finished batch is kept, in seconds (frozen into each new batch)."""
        return self.retention_days * 86400


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


def _versions(key: str) -> tuple[str, ...]:
    raw = config_value(key, DEFAULT_ANTHROPIC_VERSIONS) or ""
    chosen = [part.strip() for part in raw.split(",") if part.strip()]
    known = tuple(version for version in KNOWN_ANTHROPIC_VERSIONS if version in chosen)
    return known or KNOWN_ANTHROPIC_VERSIONS[:1]


def batch_config() -> BatchConfig:
    """Read every batch setting now. Cheap: in-memory lookups only."""
    return BatchConfig(
        enabled=_flag(ENABLED_KEY, True),
        min_role=_role(MIN_ROLE_KEY, DEFAULT_MIN_ROLE),
        default_model_concurrency=_int(MODEL_CONCURRENCY_KEY, DEFAULT_MODEL_CONCURRENCY, minimum=0),
        yield_to_interactive=_flag(YIELD_KEY, True),
        shared_interactive_load=_flag(SHARED_LOAD_KEY, True),
        max_requests=_int(MAX_REQUESTS_KEY, DEFAULT_MAX_REQUESTS, minimum=1),
        max_body_bytes=_int(MAX_BODY_MB_KEY, DEFAULT_MAX_BODY_MB, minimum=1) * 1024 * 1024,
        max_active_per_user=_int(MAX_ACTIVE_KEY, DEFAULT_MAX_ACTIVE_PER_USER, minimum=1),
        max_queued_per_user=_int(MAX_QUEUED_KEY, DEFAULT_MAX_QUEUED_PER_USER, minimum=1),
        completion_window_hours=_int(
            WINDOW_HOURS_KEY, DEFAULT_WINDOW_HOURS, minimum=1, maximum=MAX_WINDOW_HOURS
        ),
        retention_days=_int(RETENTION_DAYS_KEY, DEFAULT_RETENTION_DAYS, minimum=1),
        max_line_bytes=_int(MAX_LINE_KB_KEY, DEFAULT_MAX_LINE_KB, minimum=1) * 1024,
        max_result_bytes=_int(MAX_RESULT_KB_KEY, DEFAULT_MAX_RESULT_KB, minimum=1) * 1024,
        files_max_total_bytes=_int(FILES_MAX_TOTAL_MB_KEY, DEFAULT_FILES_MAX_TOTAL_MB, minimum=1)
        * 1024
        * 1024,
        file_uploads_per_hour=_int(
            FILE_UPLOADS_PER_HOUR_KEY, DEFAULT_FILE_UPLOADS_PER_HOUR, minimum=1
        ),
        file_retention_days=_int(FILE_RETENTION_DAYS_KEY, DEFAULT_FILE_RETENTION_DAYS, minimum=1),
        role_concurrency={
            role: _int(key, DEFAULT_ROLE_CONCURRENCY[role], minimum=0)
            for role, key in ROLE_CONCURRENCY_KEYS.items()
        },
        interactive_reserve=_int(INTERACTIVE_RESERVE_KEY, DEFAULT_INTERACTIVE_RESERVE, minimum=0),
        anthropic_versions=_versions(ANTHROPIC_VERSIONS_KEY),
        webhook_allow_private=_flag(WEBHOOK_ALLOW_PRIVATE_KEY, False),
    )


def may_use_batch(role: str | None, config: BatchConfig | None = None) -> bool:
    """Whether a caller of *role* may create batches under *config* (default: now)."""
    config = config or batch_config()
    return config.enabled and has_role(role or "free", config.min_role)
