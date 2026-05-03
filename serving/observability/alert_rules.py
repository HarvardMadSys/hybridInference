"""AlertEngine: drains AlertingLogHandler queue, runs rule-based alerts,
and schedules periodic SQL alerts via APScheduler.

Rules implemented in subsequent tasks add themselves via the _build_rules
hook; periodic SQL jobs are registered in _schedule_periodic_jobs.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Protocol

from serving.observability.alert_config import AlertConfig
from serving.observability.log_handler import AlertingLogHandler

if TYPE_CHECKING:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    from serving.storage.base import LogStore, OperationalStore

log = logging.getLogger(__name__)


class _Rule(Protocol):
    name: str

    async def on_record(self, record: logging.LogRecord) -> None: ...


class AlertEngine:
    """Drains structured log records, fans out to rules, runs periodic SQL jobs."""

    def __init__(
        self,
        *,
        handler: AlertingLogHandler,
        config: AlertConfig,
        scheduler: "AsyncIOScheduler | None",
        op_store: "OperationalStore | None",
        log_store: "LogStore | None",
    ) -> None:
        self._handler = handler
        self._config = config
        self._scheduler = scheduler
        self._op_store = op_store
        self._log_store = log_store
        self._task: asyncio.Task[None] | None = None
        self._rules: list[_Rule] = []
        self._scheduled_jobs: list[Any] = []

    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        self._build_rules()
        self._schedule_periodic_jobs()
        self._task = asyncio.create_task(self._drain(), name="AlertEngine.drain")
        log.info(
            "AlertEngine started with %d rules and %d periodic jobs",
            len(self._rules),
            len(self._scheduled_jobs),
        )

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        for job in self._scheduled_jobs:
            try:
                job.remove()
            except Exception:
                log.exception("failed to remove alert job")
        self._scheduled_jobs.clear()
        self._task = None

    def _build_rules(self) -> None:
        # Rules are added in Tasks 7-11; left empty here.
        return

    def _schedule_periodic_jobs(self) -> None:
        # Scheduled jobs are added in Tasks 14-15; left empty here.
        return

    async def _drain(self) -> None:
        try:
            while True:
                record = await self._handler.queue.get()
                for rule in self._rules:
                    try:
                        await rule.on_record(record)
                    except Exception:
                        log.exception("rule %s raised", rule.name)
        except asyncio.CancelledError:
            raise
