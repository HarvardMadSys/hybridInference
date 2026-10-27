"""Process wiring for alert history: config, its own small pool, start and stop.

History gets its own connection pool (``min_size=0``, at most four
connections) so a retrying writer or a slow history query can never take a
connection the request path needs, and so a database that is down at boot
costs nothing until the first write is tried. The pool is created whenever the
gateway has a database, so the read API can report what was stored even with
capture turned off; the schema is created only when capture is on.

Start: :func:`start_runtime` installs the recorder and starts the writer
*before* the producers start. Stop: :meth:`AlertHistoryRuntime.stop` runs after
every producer has stopped; it waits (bounded) for notifications still in
flight, writes the open suppression buckets, closes the queue and drains it to
PostgreSQL within its budget. Whatever is left then is lost and says so.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import hashlib
import json
import logging
import os
import re
import socket
import sys
import time
import uuid
from typing import TYPE_CHECKING, Any

from serving.observability.alert_history import hooks
from serving.observability.alert_history.capture import BackendCapture, BackendIdentity
from serving.observability.alert_history.queue import (
    BATCH_MAX_BYTES,
    BATCH_MAX_RECORDS,
    DEFAULT_MAX_BYTES,
    DEFAULT_MAX_RECORDS,
    DRAIN_BUDGET_SEC,
    HistoryQueue,
    HistoryWriter,
    diag,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

logger = logging.getLogger(__name__)

ENABLED_SETTING = "ALERT_HISTORY_ENABLED"
#: Capture with a database unless ``ALERT_HISTORY_ENABLED`` says otherwise.
DEFAULT_ENABLED = True
DEPLOYMENT_SETTING = "ALERT_HISTORY_DEPLOYMENT_ID"
POOL_MAX_SIZE = 4
POOL_ACQUIRE_TIMEOUT_SEC = 3.0
POOL_CONNECT_TIMEOUT_SEC = 5.0
IN_FLIGHT_WAIT_SEC = 5.0
_LABEL = re.compile(r"[^a-z0-9_.-]+")


class HistoryUnavailable(RuntimeError):
    """The history database (or its schema) cannot be used right now."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def normalize_label(value: str, *, default: str) -> str:
    """Lower-case ``value`` into ``[a-z0-9_.-]{1,64}``."""
    cleaned = _LABEL.sub("-", (value or "").strip().lower()).strip("-")[:64]
    return cleaned or default


def history_enabled() -> bool:
    """Whether capture is switched on (``ALERT_HISTORY_ENABLED``).

    On unless the setting says otherwise. The built-in default is applied here:
    :func:`config_value` supplies a registry default only for extension
    settings. It applies only when :func:`config_value` resolves nothing — no
    stored row in force, no environment variable. (A stored row the registry
    refuses is passed over there, with its own warning, as for any setting.) A
    value that resolves is read as written: blank or not a boolean keeps
    capture off, as before.
    """
    from serving.config.app_config import config_value
    from serving.config.app_config_registry import parse_bool

    raw = config_value(ENABLED_SETTING)
    if raw is None:
        return DEFAULT_ENABLED
    if not raw.strip():
        logger.warning("%s is set but empty; alert history stays off", ENABLED_SETTING)
        return False
    try:
        return parse_bool(raw)
    except ValueError:
        logger.warning("%s is not a boolean; alert history stays off", ENABLED_SETTING)
        return False


def deployment_id() -> str:
    """This deployment's id for history rows (``ALERT_HISTORY_DEPLOYMENT_ID``)."""
    from serving.config.app_config import config_value

    return normalize_label(config_value(DEPLOYMENT_SETTING) or "", default="default")


class AlertHistoryRuntime:
    """Everything alert history keeps for the life of the process."""

    def __init__(
        self,
        *,
        enabled: bool,
        identity: BackendIdentity,
        db_config: dict[str, Any] | None,
        fence_secret: str | None,
        pool: Any | None = None,
        max_records: int = DEFAULT_MAX_RECORDS,
        max_bytes: int = DEFAULT_MAX_BYTES,
    ) -> None:
        self.enabled = enabled
        self.identity = identity
        self.fence_secret = fence_secret or None
        self._db_config = db_config
        self._pool = pool
        self._owns_pool = pool is None
        self._pool_lock = asyncio.Lock()
        self._schema_ready = False
        self.read_slots = asyncio.Semaphore(2)
        self.ingest_slots = asyncio.Semaphore(2)
        self.queue = HistoryQueue(max_records=max_records, max_bytes=max_bytes)
        self.capture: BackendCapture | None = None
        self.writer: HistoryWriter | None = None
        self.started_at = time.time()
        self.stopped = False
        self.rules_snapshot: dict[str, Any] | None = None
        self.rules_hash: str | None = None
        if enabled:
            from serving.storage.alert_history_store import AlertHistoryStore

            self.capture = BackendCapture(identity, self.queue.offer, fence_secret=fence_secret)
            self.writer = HistoryWriter(
                self.queue,
                AlertHistoryStore(self.acquire),
                heartbeat=self.heartbeat,
                before_batch=self.capture.flush_buckets,
                gap_record=self.capture.gap_record,
            )

    # -- database -----------------------------------------------------------

    @property
    def has_database(self) -> bool:
        """Whether a database is configured for history at all."""
        return self._pool is not None or self._db_config is not None

    async def _get_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        if self._db_config is None:
            raise HistoryUnavailable("database_not_configured")
        async with self._pool_lock:
            if self._pool is None:
                import asyncpg

                self._pool = await asyncpg.create_pool(
                    **self._db_config,
                    min_size=0,
                    max_size=POOL_MAX_SIZE,
                    timeout=POOL_CONNECT_TIMEOUT_SEC,
                    command_timeout=15,
                )
        return self._pool

    @contextlib.asynccontextmanager
    async def acquire(self) -> AsyncIterator[Any]:
        """A connection from the history pool, with the schema checked once."""
        pool = await self._get_pool()
        try:
            conn = await pool.acquire(timeout=POOL_ACQUIRE_TIMEOUT_SEC)
        except Exception as exc:
            raise HistoryUnavailable("database_unavailable") from exc
        try:
            if not self._schema_ready:
                from serving.storage import alert_history_store as store

                if self.enabled:
                    await store.ensure_schema(conn)
                elif not await store.tables_present(conn):
                    raise HistoryUnavailable("schema_missing")
                self._schema_ready = True
            yield conn
        finally:
            await pool.release(conn)

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        """Install the recorder and start the writer (capture on only)."""
        if self.capture is None or self.writer is None:
            return
        hooks.install(self.capture)
        self.writer.start()
        logger.info(
            "alert history capture enabled: deployment=%s environment=%s instance=%s",
            self.identity.deployment_id,
            self.identity.target_environment,
            self.identity.source_instance_id,
        )

    async def stop(
        self,
        *,
        producer_budget_sec: float = IN_FLIGHT_WAIT_SEC,
        drain_budget_sec: float = DRAIN_BUDGET_SEC,
    ) -> None:
        """Wait for producers, seal, drain, record the stop, close the pool.

        Two bounded phases. First, up to ``producer_budget_sec`` for every task
        still inside the alert path or scheduled to enter it — a request that
        just tripped a breaker, a health-route report, a scheduler job, a send
        whose result is not in yet — so their facts and results are queued
        before the queue closes. Then everything else, the drain, the stop
        record and closing the pool, inside ``drain_budget_sec``.
        """
        if self.stopped:
            return
        self.stopped = True
        if self.capture is not None and self.writer is not None:
            settled = await wait_for_producers(producer_budget_sec)
            deadline = time.monotonic() + drain_budget_sec
            with contextlib.suppress(Exception):
                self.capture.seal()
            self.queue.close()
            # Leave a sliver of the budget for the stop record.
            left = await self.writer.stop(
                drain_budget_sec=max(0.0, (deadline - time.monotonic()) * 0.8)
            )
            hooks.install(None)
            snapshot = self.heartbeat()
            snapshot.update(
                stopped_at=time.time(),
                stop_reason=(
                    "shutdown"
                    if left == 0 and settled
                    else "shutdown_producers_unsettled"
                    if not settled
                    else "shutdown_drain_timeout"
                ),
                lost_at_stop=left + self.queue.pending_refusals(),
            )
            try:
                from serving.observability.alert_history.queue import bounded
                from serving.storage.alert_history_store import AlertHistoryStore

                await bounded(
                    AlertHistoryStore(self.acquire).heartbeat(snapshot),
                    max(0.05, deadline - time.monotonic()),
                )
            except Exception:
                diag.warning("alert history could not record its own stop", exc_info=False)
            await self._close_pool(max(0.05, deadline - time.monotonic()))
        else:
            await self._close_pool(2.0)

    async def _close_pool(self, budget: float) -> None:
        pool, self._pool = self._pool, None
        if pool is None or not self._owns_pool:
            return
        try:
            await asyncio.wait_for(pool.close(), budget)
        except Exception:
            pool.terminate()

    def set_rules(self, snapshot: dict[str, Any] | None) -> None:
        """Record the alert rule configuration in force, and stamp records with its hash."""
        if snapshot is None:
            return
        text = json.dumps(snapshot, sort_keys=True, separators=(",", ":"), default=str)
        if len(text) > 16 * 1024:
            self.rules_snapshot = {"omitted": "snapshot_too_large"}
        else:
            self.rules_snapshot = snapshot
        self.rules_hash = "sha256:" + hashlib.sha256(text.encode()).hexdigest()
        if self.capture is not None:
            self.capture.rule_version = self.rules_hash[7:23]

    # -- status -------------------------------------------------------------

    def heartbeat(self) -> dict[str, Any]:
        """This process's source snapshot, as of now."""
        ident = self.identity
        now = time.time()
        writer = self.writer
        pending_count, pending_bytes, oldest_pending = self.queue.after_in_flight()
        return {
            "source": "backend",
            "source_namespace": ident.source_namespace,
            "source_instance_id": ident.source_instance_id,
            "deployment_id": ident.deployment_id,
            "target_environment": ident.target_environment,
            "process_id": ident.process_id,
            "release_id": ident.release_id,
            "started_at": ident.started_at,
            "enabled_at": ident.started_at,
            "rules_snapshot": self.rules_snapshot,
            "rules_snapshot_hash": self.rules_hash,
            "capture_config": {
                "max_records": self.queue.max_records,
                "max_bytes": self.queue.max_bytes,
                "batch_max_records": BATCH_MAX_RECORDS,
                "batch_max_bytes": BATCH_MAX_BYTES,
            },
            "observed_at": now,
            # What stays queued if the batch being written with this snapshot
            # commits: count, bytes and oldest all describe those same records.
            "pending_count": pending_count,
            "pending_bytes": pending_bytes,
            "oldest_pending_at": oldest_pending,
            "pending_reported_at": now,
            "dropped_count": self.queue.dropped_total,
            "refused_count": self.queue.refused_total,
            "last_error_code": writer.last_error_code if writer else None,
            "last_error_at": writer.last_error_at if writer else None,
        }

    def local_status(self) -> dict[str, Any]:
        """The answering process's own buffer. Not visible across workers."""
        writer = self.writer
        return {
            "scope": "answering_process_only",
            "source_instance_id": self.identity.source_instance_id,
            "capture_enabled": self.capture is not None and not self.stopped,
            "queued_records": len(self.queue),
            "queued_bytes": self.queue.queued_bytes,
            "in_flight_records": self.queue.in_flight,
            "max_records": self.queue.max_records,
            "max_bytes": self.queue.max_bytes,
            "refused_total": self.queue.refused_total,
            "unreported_refusals": self.queue.pending_refusals(),
            "dropped_total": self.queue.dropped_total,
            "committed_total": self.queue.committed_total,
            "last_success_at": _iso(writer.last_success_at) if writer else None,
            "last_error_code": writer.last_error_code if writer else None,
            "last_error_at": _iso(writer.last_error_at) if writer else None,
            "consecutive_failures": writer.consecutive_failures if writer else 0,
        }


def _iso(epoch: float | None) -> str | None:
    if epoch is None:
        return None
    return dt.datetime.fromtimestamp(epoch, dt.UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _producer_tasks() -> set[asyncio.Task[Any]]:
    """Every task that is in, or is about to enter, the alert path."""
    from serving.observability import alerts

    tasks: set[asyncio.Task[Any]] = set(alerts.active_alert_tasks())
    # Background alert deliveries the producers detach so a request or a
    # health probe never waits on a webhook. Imported lazily: either module
    # may be absent from a partial process (a CLI, a unit test).
    for module_name in ("routing.endpoint_health", "serving.servers.routers.health"):
        module = sys.modules.get(module_name)
        tasks |= set(getattr(module, "_ALERT_TASKS", ()) or ())
    current = asyncio.current_task()
    return {task for task in tasks if task is not current and not task.done()}


async def wait_for_producers(budget_sec: float) -> bool:
    """Wait, bounded, until no alert producer is running. ``True`` when none is.

    Not cancelled when the budget runs out: a send still going is the
    alerting's business, not history's. Whatever it records later is refused
    by the closed queue and counted as lost at shutdown.
    """
    from serving.observability import alerts

    deadline = time.monotonic() + max(0.0, budget_sec)
    while True:
        tasks = _producer_tasks()
        if not tasks and not alerts._IN_FLIGHT:
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            diag.warning(
                "alert history stopped with %d alert producer task(s) still running",
                len(tasks),
            )
            return False
        if tasks:
            await asyncio.wait(tasks, timeout=min(remaining, 0.25))
        else:
            await asyncio.sleep(min(remaining, 0.05))


_RUNTIME: AlertHistoryRuntime | None = None


def get_runtime() -> AlertHistoryRuntime | None:
    """The process's history runtime, or ``None`` when there is no database."""
    return _RUNTIME


def set_runtime(runtime: AlertHistoryRuntime | None) -> None:
    """Install ``runtime`` (tests and :func:`start_runtime`)."""
    global _RUNTIME
    _RUNTIME = runtime


def build_identity(*, target_environment: str) -> BackendIdentity:
    """This process's identity: a fresh instance id at every start."""
    dep = deployment_id()
    return BackendIdentity(
        source_namespace=dep,
        deployment_id=dep,
        target_environment=normalize_label(target_environment, default="unknown"),
        source_instance_id=f"be_{uuid.uuid4().hex}",
        process_id=f"{socket.gethostname()[:64]}:{os.getpid()}",
        started_at=dt.datetime.now(dt.UTC),
        release_id=(os.environ.get("BUILD_SHA") or "").strip()[:64] or None,
    )


def start_runtime(
    *, db_config: dict[str, Any] | None, fence_secret: str | None
) -> AlertHistoryRuntime | None:
    """Create, install and start the runtime. Never raises.

    With no database there is nothing to keep history in, so nothing is
    created and the read API answers "not configured". A failure here only
    costs history: the original alerting is untouched.
    """
    try:
        from serving.observability.alerts import _base_url, _detect_environment

        base_url, explicit = _base_url()
        runtime = AlertHistoryRuntime(
            enabled=history_enabled() and db_config is not None,
            identity=build_identity(
                target_environment=_detect_environment(base_url, explicit=explicit)
            ),
            db_config=db_config,
            fence_secret=fence_secret,
        )
        if db_config is None:
            set_runtime(None)
            return None
        set_runtime(runtime)
        runtime.start()
        if not runtime.enabled:
            logger.info("alert history capture turned off by %s", ENABLED_SETTING)
        return runtime
    except Exception:
        logger.exception("alert history could not start; alerting continues without it")
        hooks.install(None)
        set_runtime(None)
        return None


async def stop_runtime() -> None:
    """Stop the installed runtime, bounded. Never raises."""
    runtime = get_runtime()
    if runtime is None:
        return
    try:
        await runtime.stop()
    except Exception:
        logger.exception("alert history shutdown failed")
    finally:
        hooks.install(None)
        set_runtime(None)
