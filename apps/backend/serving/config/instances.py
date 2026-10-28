"""Instance registration and acknowledgements for live configuration groups.

Each backend worker process registers one row in ``config_instances`` under
its own instance id (:data:`serving.servers.restart.INSTANCE_ID`, fresh at
every start; neither a pod name nor a pid identifies a process on its own). The
row carries the build, the groups and schema versions the process supports, its
lifecycle, a heartbeat stamped with the *database's* clock, and the latest
status of every group: the source epoch, the revisions checked and reconciled,
the local publication generation, a state and a stable error code. It never
carries a configuration value, a secret, a raw validation message or anything
derived from a secret.

One task per process writes the row (:class:`InstanceReporter`), and each write
carries a report sequence the database only lets go up, so an older report can
never overwrite a newer acknowledgement. A failed write retries the write alone:
the configuration it reports is already in force.

A heartbeat older than :data:`STALE_AFTER_SECONDS` makes the instance "unknown,
possibly offline". That is not success, and it does not prove the process has
stopped serving either. A process that runs an older build never registers, so
the table cannot prove coverage on its own; :func:`fetch_status` says so.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import random
import socket
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from serving.utils.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

logger = get_logger(__name__)

#: How often a process reports, and the longest an unchanged status waits.
HEARTBEAT_SECONDS = 10.0
#: A heartbeat older than this, by the database clock, is "unknown, possibly offline".
STALE_AFTER_SECONDS = 60.0
#: Registrations of stopped or vanished processes are removed after this long.
RETENTION_SECONDS = 7 * 24 * 3600.0
#: Retry bounds for a failed report write.
_RETRY_MIN_SECONDS = 1.0
_RETRY_MAX_SECONDS = HEARTBEAT_SECONDS
#: The longest the final "stopped" report may hold up shutdown.
_FINAL_REPORT_TIMEOUT_SECONDS = 3.0

#: Version of this registration protocol, reported in ``capabilities``.
PROTOCOL_VERSION = 1

CREATE_INSTANCES_SQL = """
CREATE TABLE IF NOT EXISTS config_instances (
    instance_id   TEXT PRIMARY KEY,
    build         TEXT NOT NULL,
    capabilities  JSONB NOT NULL DEFAULT '{}'::jsonb,
    started_at    TIMESTAMPTZ NOT NULL,
    heartbeat_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    lifecycle     TEXT NOT NULL,
    report_seq    BIGINT NOT NULL DEFAULT 0,
    groups        JSONB NOT NULL DEFAULT '{}'::jsonb
)
"""

DELETE_ALL_INSTANCES_SQL = "DELETE FROM config_instances"

# Monotonic: a report only lands when its sequence is newer than the stored
# one, so a delayed write from this process cannot replace a later one.
UPSERT_REPORT_SQL = """
INSERT INTO config_instances
    (instance_id, build, capabilities, started_at, heartbeat_at, lifecycle, report_seq, groups)
VALUES ($1, $2, $3::jsonb, $4, NOW(), $5, $6, $7::jsonb)
ON CONFLICT (instance_id) DO UPDATE SET
    build = EXCLUDED.build,
    capabilities = EXCLUDED.capabilities,
    heartbeat_at = NOW(),
    lifecycle = EXCLUDED.lifecycle,
    report_seq = EXCLUDED.report_seq,
    groups = EXCLUDED.groups
WHERE config_instances.report_seq < EXCLUDED.report_seq
RETURNING report_seq
"""

PRUNE_SQL = """
DELETE FROM config_instances
WHERE heartbeat_at < NOW() - make_interval(secs => $1)
"""

SELECT_INSTANCES_SQL = """
SELECT instance_id, build, capabilities, started_at, heartbeat_at, lifecycle, report_seq, groups,
       EXTRACT(EPOCH FROM (NOW() - heartbeat_at)) AS heartbeat_age
FROM config_instances
ORDER BY started_at, instance_id
"""


def build_id() -> str:
    """The build this process runs: ``BUILD_SHA`` when the image sets it."""
    return (os.environ.get("BUILD_SHA") or "").strip()[:64] or "unknown"


@dataclass
class InstanceReporter:
    """Writes this process's registration, heartbeat and group statuses.

    Exactly one per process. :meth:`set_group` replaces a group's latest status
    and wakes the writer; nothing queues, so memory stays bounded whatever the
    database does.
    """

    pool: Any
    instance_id: str
    started_at: Any
    capabilities: Mapping[str, Any]
    build: str = field(default_factory=build_id)
    heartbeat_seconds: float = HEARTBEAT_SECONDS
    #: Called after every report attempt with whether it was stored; the
    #: coordinator uses it to show an acknowledgement as persisted or pending.
    on_report: Callable[[bool], None] | None = None

    _groups: dict[str, dict[str, Any]] = field(default_factory=dict)
    _lifecycle: str = "starting"
    _seq: int = 0
    _persisted_seq: int = 0
    #: Bumped by every local change to what is reported, so an acknowledgement
    #: of an older payload never confirms a newer status.
    _changes: int = 0
    _persisted_changes: int = -1
    _wake: asyncio.Event = field(default_factory=asyncio.Event)
    _task: asyncio.Task[None] | None = None
    _stopping: bool = False
    last_error: str | None = None

    def set_group(self, name: str, status: Mapping[str, Any]) -> None:
        """Record *name*'s latest status and report it soon."""
        self._groups[name] = dict(status)
        self._changes += 1
        self._wake.set()

    def remove_group(self, name: str) -> None:
        """Stop reporting *name* (a contract group that went away)."""
        if self._groups.pop(name, None) is not None:
            self._changes += 1
            self._wake.set()

    def set_lifecycle(self, lifecycle: str) -> None:
        """Record ``starting``, ``running``, ``stopping`` or ``stopped``."""
        self._lifecycle = lifecycle
        self._changes += 1
        self._wake.set()

    @property
    def acknowledged(self) -> bool:
        """Whether the latest local status — not merely some earlier one — is stored."""
        return self._persisted_changes >= self._changes

    def details(self) -> dict[str, Any]:
        """Return this process's own view of its registration (no secrets)."""
        return {
            "instance_id": self.instance_id,
            "build": self.build,
            "lifecycle": self._lifecycle,
            "report_seq": self._seq,
            "persisted_seq": self._persisted_seq,
            "acknowledged": self.acknowledged,
            "last_error": self.last_error,
        }

    async def report_once(self) -> bool:
        """Write the current status; return whether that write was stored.

        :attr:`acknowledged` is about the latest status instead: a status
        recorded while this write was in flight leaves it false.
        """
        self._seq += 1
        seq = self._seq
        changes = self._changes
        groups = json.dumps(self._groups, sort_keys=True, default=str)
        lifecycle = self._lifecycle
        try:
            async with self.pool.acquire() as conn:
                stored = await conn.fetchval(
                    UPSERT_REPORT_SQL,
                    self.instance_id,
                    self.build,
                    json.dumps(dict(self.capabilities), sort_keys=True),
                    self.started_at,
                    lifecycle,
                    seq,
                    groups,
                )
        except Exception as exc:
            # The class name only: a driver message can quote the statement.
            self.last_error = f"report_failed:{type(exc).__name__}"
            logger.warning(
                "Configuration instance report failed (%s); retrying the report only",
                type(exc).__name__,
            )
            if self.on_report is not None:
                self.on_report(False)
            return False
        if stored is not None:
            self._persisted_seq = max(self._persisted_seq, int(stored))
            self._persisted_changes = max(self._persisted_changes, changes)
        self.last_error = None
        if self.on_report is not None:
            self.on_report(self.acknowledged)
        return True

    async def prune(self) -> None:
        """Remove registrations not heard from within :data:`RETENTION_SECONDS`."""
        with contextlib.suppress(Exception):
            async with self.pool.acquire() as conn:
                await conn.execute(PRUNE_SQL, RETENTION_SECONDS)

    async def _run(self) -> None:
        delay = 0.0
        while not self._stopping:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=delay or self.heartbeat_seconds)
            self._wake.clear()
            if self._stopping:
                return
            if await self.report_once():
                delay = 0.0
            else:
                # Bounded backoff with jitter; only the write is retried.
                base = min(_RETRY_MAX_SECONDS, max(_RETRY_MIN_SECONDS, delay * 2 or 1.0))
                delay = base * (0.5 + random.random() / 2)

    def start(self) -> None:
        """Start the single writer task."""
        if self._task is None or self._task.done():
            self._stopping = False
            self._task = asyncio.create_task(self._run(), name="config-instance-reporter")

    async def stop(self) -> None:
        """Stop the writer and report ``stopped``, within a short deadline."""
        self._stopping = True
        self._wake.set()
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._lifecycle = "stopped"
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self.report_once(), timeout=_FINAL_REPORT_TIMEOUT_SECONDS)


def default_details() -> dict[str, Any]:
    """Non-secret facts about where this process runs, for the console."""
    return {"host": socket.gethostname()[:128], "pid": os.getpid()}


def _json(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return {}
    return value or {}


async def fetch_instances(conn: Any) -> list[dict[str, Any]]:
    """Return every registration, with its heartbeat age by the database clock."""
    records = await conn.fetch(SELECT_INSTANCES_SQL)
    instances = []
    for record in records:
        age = float(record["heartbeat_age"]) if record["heartbeat_age"] is not None else None
        instances.append(
            {
                "instance_id": record["instance_id"],
                "build": record["build"],
                "capabilities": _json(record["capabilities"]),
                "started_at": record["started_at"],
                "heartbeat_at": record["heartbeat_at"],
                "heartbeat_age_seconds": age,
                "stale": age is None or age > STALE_AFTER_SECONDS,
                "lifecycle": record["lifecycle"],
                "report_seq": int(record["report_seq"]),
                "groups": _json(record["groups"]),
            }
        )
    return instances


def supports(capabilities: Mapping[str, Any], group: str, schema_version: int) -> bool:
    """Whether a registration's capabilities include *group* at *schema_version*."""
    versions = (capabilities.get("groups") or {}).get(group)
    return isinstance(versions, list) and schema_version in versions


def coverage(
    instances: list[dict[str, Any]],
    *,
    group: str,
    schema_version: int,
    epoch: str | None,
    revision: int | None,
) -> dict[str, Any]:
    """Count how the registered instances stand on *group* at the source version.

    An instance counts as converged only when it is live (a heartbeat within
    :data:`STALE_AFTER_SECONDS`), reports the current epoch, and has reconciled
    the group to the current revision. A stale heartbeat is ``unknown``, never
    success. A process on an older build never registers, so the counts cover
    registered processes only: ``verified`` stays false until a deployment
    inventory has been compared (see the runbook).
    """
    counts = {
        "registered": 0,
        "live": 0,
        "converged": 0,
        "applying": 0,
        "failed": 0,
        "unsupported": 0,
        "dormant": 0,
        "unknown": 0,
        "stopped": 0,
    }
    for instance in instances:
        if instance["lifecycle"] == "stopped":
            counts["stopped"] += 1
            continue
        counts["registered"] += 1
        if instance["stale"]:
            counts["unknown"] += 1
            continue
        counts["live"] += 1
        status = (instance["groups"] or {}).get(group)
        if not supports(instance["capabilities"], group, schema_version):
            counts["unsupported"] += 1
            continue
        if not isinstance(status, dict) or status.get("epoch") != epoch:
            counts["unknown"] += 1
            continue
        state = status.get("state")
        if state == "unsupported":
            counts["unsupported"] += 1
        elif state in ("dormant", "dormant-frozen"):
            counts["dormant"] += 1
        elif state in ("invalid", "failed", "cleanup_blocked"):
            counts["failed"] += 1
        elif (
            state in ("applied", "paused")
            and revision is not None
            and status.get("reconciled_revision") == revision
        ):
            counts["converged"] += 1
        else:
            counts["applying"] += 1
    return {**counts, "verified": False}
