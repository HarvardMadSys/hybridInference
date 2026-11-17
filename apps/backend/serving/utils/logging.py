"""Logging utilities with optional JSON formatter."""

from __future__ import annotations

import json
import logging
import os
import re
from typing import TYPE_CHECKING, Any

from . import context as req_ctx

if TYPE_CHECKING:
    from collections.abc import Callable

_STRUCTURED_LOG_KEYS = (
    "event",
    "method",
    "path",
    "status_code",
    "duration_ms",
    # Time the request spent in Kubernetes proxy discovery, lock waits
    # included (servers/middleware/proxy_trust.py). Kept apart from
    # duration_ms, which measures only what runs inside the request log.
    "proxy_discovery_ms",
    "remote_ip",
    "peer_ip",
    "ip_source",
    "ip_resolved",
    "result",
    "source",
    "x_forwarded_for",
    "x_real_ip",
    "cf_connecting_ip",
    "cf_connecting_ipv6",
    "trusted_forwarded_headers",
    "trusted_cloudflare_headers",
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
    # Why a *resolved* credential was refused (revoked / expired /
    # user_suspended), set alongside "user_id" on an auth_failure record for a
    # key this deployment did issue. Without it the alert can name the account
    # but not what is wrong with its key, which is the actionable half.
    "credential_state",
    "reason",
    "age_sec",
    "idle_sec",
    "pending_count",
    "capacity",
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
    # Request-log queue health: preserve counts and bounds without adding
    # request payloads to the structured field allowlist.
    "dropped_total",
    "queue_maxsize",
    "queued_rows_discarded",
    "inflight_writes_uncertain",
    "pending_rows",
    "from_provider",
    "to_provider",
    "ttft_ms",
    "endpoint_id",
    "key_index",
    "status",
    "stage",
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
    # Circuit-breaker state-change events (routing/endpoint_health.py, which
    # keeps the "routing.routers" logger name it was extracted from).
    "consecutive_failures",
    "availability",
    "trip_cause",
    "upstream_error",
    # Runtime route-weight divergence (routing/routers.py, events
    # ``route_weight_zeroed`` / ``route_weight_overridden``). The pair is the
    # whole record: "this route is at 0.0" is only actionable next to the
    # weight the configuration asked for, which is what says whether an
    # operator zeroed it at runtime or the overlay always read that way.
    "configured_weight",
    "effective_weight",
    # Who the failure streak hit. Formerly "offending_users", which named the
    # victims of an upstream fault as its culprits — see the module comment on
    # routing/endpoint_health._MAX_TRACKED_CALLERS.
    "affected_callers",
    # Whose request a breaker-exempt failure was (``client_error_skip_breaker``,
    # ``caller_repeat_skip_breaker``): ``endpoint_health._caller_key``, a short
    # hash of the affinity key with no way back to the credential. A skipped
    # repeat is only actionable when it says which caller kept failing.
    "caller",
    # Upstream rejection of the gateway's own credential
    # (routing/endpoint_health.py ``upstream_auth_misconfig``).
    "consecutive_auth_rejections",
    # Auth-failure blocklist: ``auth_ip_blocked`` /
    # ``auth_ip_block_cleared`` (utils/auth_failure_blocklist.py) and
    # ``auth_block_clear_audit_failed`` (servers/routers/admin/auth_blocks.py).
    # ``ip_bucket`` is the whole point of those lines -- an operator reading
    # "a source was blocked" needs to know *which*, and it is what the clear
    # endpoint takes back. Without these the records serialize to an event
    # name and a traceback, which is also what made "the log records are still
    # emitted, so an investigation loses no evidence" (#1370) weaker than it
    # sounds.
    "ip_bucket",
    "cleared",
    "threshold",
    "window_sec",
    "block_seconds",
    # The evidence the error redaction relies on existing.
    #
    # Error responses no longer carry internal detail (see
    # servers/middleware/exception_handler.py and the validation handler in
    # routers/anthropic_messages.py); the whole premise of that change is
    # "redacted from the response, intact in the log". These are the keys that
    # text was moved *into* -- ``detail`` carries the exception/validation text
    # itself, ``error_code`` is the only machine-readable field on the
    # ``domain_error`` line, and ``endpoint`` names the identity endpoint whose
    # configuration fault stopped being published. Both formatters emit only
    # keys listed here, so omitting them would drop the relocated text at format
    # time and turn the redaction into a net loss of evidence -- the same
    # failure mode the ``ip_bucket`` note above describes, but applied to the
    # text a support request quoting an X-Request-ID is trying to recover.
    "detail",
    "endpoint",
    "error_code",
    # Which client tool call the OpenAI-compatible adapter had to repair
    # (``tool_call_arguments_repaired`` in adapters/openai_compat.py). The
    # repair hides the producer's bug from the user, so this line is the only
    # remaining trace of it -- and without the id and function name it says
    # only "something somewhere sent bad JSON". The argument text is user data
    # and is deliberately not among these keys.
    #
    # The same ``tool_call_id`` also locates a tool call the gateway streamed
    # OUT with arguments that are not a JSON object
    # (``tool_call_arguments_unparseable``, servers/routers/
    # completions_stream.py) -- the producer-side half of the same incident,
    # which was a single poisoned call replayed on every subsequent turn.
    # That event adds ``function_name`` (the adapter's repair path calls the
    # same field ``tool_name``, since ``name`` collides with
    # ``LogRecord.name``), the upstream ``finish_reason``, and
    # ``arguments_len`` -- which stands in for the arguments themselves, user
    # data that is never logged.
    "tool_call_id",
    "tool_name",
    "function_name",
    "finish_reason",
    "arguments_len",
    # Adaptive outbound concurrency (adapters/upstream_limiter.py). A limit
    # moving is only readable as a pair -- ``old_limit`` next to ``new_limit``
    # next to the ``reason`` that moved it -- and ``key_fingerprint`` is what
    # says *which* of a provider's keys, since the key itself is never logged.
    # ``in_flight`` / ``waiting`` / ``limit`` / ``timeout_sec`` are the state a
    # saturation line exists to report: without them it says only "something
    # somewhere queued too long".
    "key_fingerprint",
    "old_limit",
    "new_limit",
    "in_flight",
    "waiting",
    "limit",
    "timeout_sec",
    # A stored admin capability override that names no served route
    # (``route_capability_override_orphaned``, servers/routers/admin/
    # provider_routes.py). Next to ``model_id`` it says which row to look at.
    "route_id",
    # The shared spend accounting's evidence (serving/shared_spend/ledger.py,
    # and the shutdown report of its ledgers in serving/spend_ledger.py). Its
    # alerts say "the log lists each entry to reconcile", and for a cost that
    # was set aside, dropped as a duplicate or left unaccounted the line is the
    # only record of how much: ``increments`` and ``reservations`` are those
    # lists (a settlement batch's, so at most a few hundred short items),
    # ``amount_usd`` the sum, ``batch_id`` / ``reservation_id`` / ``sqlstate``
    # what to look them up by, ``entry_count`` and ``unavailable`` how a write
    # ended. ``held`` is what a process still held when it shut down;
    # ``failing_for_seconds``, ``oldest_age_seconds`` and ``instances`` are
    # what a stalled settlement and stranded reservations are reported with.
    # None of these names is used by any other log line: the per-process
    # ledger's own fields (``entries``, ``cost_usd``, ...) are not among them
    # and are written as before.
    "increments",
    "reservations",
    "amount_usd",
    "batch_id",
    "reservation_id",
    "sqlstate",
    "entry_count",
    "unavailable",
    "held",
    "failing_for_seconds",
    "oldest_age_seconds",
    "instances",
    # Where a release handoff stood (serving/servers/handoff.py): its state,
    # the requests in flight and those refused after the seal. A release
    # record is built from these lines; no other line uses the names.
    "handoff_state",
    "handoff_in_flight",
    "handoff_refused_after_seal",
)


#: Longest text a structured field of a log line keeps. A request's path and
#: headers are the client's to choose, up to the server's 16 KiB header limit,
#: and reach lines written for every such request (an auth failure, a refused
#: body), where JSON escaping makes six characters of each byte over 0x7f. The
#: request log cuts its own values shorter; this bounds every other line.
MAX_LOGGED_FIELD_CHARS = 2048


def _bounded_field(value: Any) -> Any:
    """*value*, or the start of it when it is text longer than :data:`MAX_LOGGED_FIELD_CHARS`."""
    if isinstance(value, str) and len(value) > MAX_LOGGED_FIELD_CHARS:
        return value[:MAX_LOGGED_FIELD_CHARS] + "…"
    return value


def _format_extra_value(value: Any) -> str:
    """Format a structured extra value for plain logs."""
    value = _bounded_field(value)
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
        for key in (*_STRUCTURED_LOG_KEYS, "headers"):
            if key in record.__dict__:
                payload[key] = _bounded_field(record.__dict__[key])

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


#: Paths whose ``token`` query parameter is a credential: an admin export's
#: download link (``serving.servers.routers.admin.export``), which downloads the
#: export with no other authentication for as long as it lasts.
_CREDENTIAL_QUERY_PATHS = ("/admin/export/requests/download",)
_TOKEN_PARAM = re.compile(r"([?&]token=)[^&]*")


class _CredentialQueryFilter(logging.Filter):
    """Write a download link's ``token`` as ``<redacted>`` in uvicorn's access log.

    The link is a URL, so uvicorn logs it whole; the log would otherwise hold
    a usable credential for the rest of its minute.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        """Redact the token in place; never suppress the line."""
        # Uvicorn access log args: (client_addr, method, path, http_version, status_code)
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str):
            path = args[2]
            if path.startswith(_CREDENTIAL_QUERY_PATHS) and "token=" in path:
                redacted = _TOKEN_PARAM.sub(r"\1<redacted>", path)
                record.args = (*args[:2], redacted, *args[3:])
        return True


_SSO_CALLBACK_WITH_QUERY = re.compile(r"(/auth/sso/[^/?]+/callback)\?")


class _CallbackQueryFilter(logging.Filter):
    """Keep a single sign-on callback's query out of uvicorn's access log.

    The identity provider sends the browser back with its one-time
    authorization code and the flow's state in the query. The line keeps the
    path, which names the provider and, with the status, says what happened.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        """Drop the query from a callback's request line; never suppress the line."""
        # Uvicorn access log args: (client_addr, method, path, http_version, status_code)
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str):
            callback = _SSO_CALLBACK_WITH_QUERY.match(args[2])
            if callback:
                record.args = (*args[:2], callback.group(1), *args[3:])
        return True


def attach_quiet_access_filter() -> None:
    """Attach the gateway's filters to uvicorn's access logger.

    The quiet-path filter drops polling paths; set LOG_LEVEL=DEBUG to disable
    that and see all access logs. The credential filter redacts a download
    link's token, and the callback filter leaves a single sign-on callback's
    query out, at every level.

    Must be called after uvicorn's own logging setup (i.e., from the app
    lifespan), otherwise uvicorn's dictConfig will wipe the filters.
    """
    uvicorn_access = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, _QuietPathFilter) for f in uvicorn_access.filters):
        uvicorn_access.addFilter(_QuietPathFilter())
    if not any(isinstance(f, _CredentialQueryFilter) for f in uvicorn_access.filters):
        uvicorn_access.addFilter(_CredentialQueryFilter())
    if not any(isinstance(f, _CallbackQueryFilter) for f in uvicorn_access.filters):
        uvicorn_access.addFilter(_CallbackQueryFilter())


# Resolves LOG_LEVEL and LOG_FORMAT: the environment until the database-backed
# configuration loads and installs its own resolver (serving.config.app_config),
# which this module cannot import because everything imports it. LOG_FILE is
# read from the environment only: it decides where logging goes before any
# configuration can load.
_setting_resolver: Callable[[str, str], str | None] = os.getenv


def use_setting_resolver(resolver: Callable[[str, str], str | None]) -> None:
    """Resolve LOG_LEVEL and LOG_FORMAT through *resolver* from now on."""
    global _setting_resolver
    _setting_resolver = resolver


def _env_level() -> int:
    level = (_setting_resolver("LOG_LEVEL", "INFO") or "").upper()
    return getattr(logging, level, logging.INFO)


def _env_is_json() -> bool:
    return (_setting_resolver("LOG_FORMAT", "plain") or "").lower() == "json"


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
    "use_setting_resolver",
]
