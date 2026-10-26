"""PostgreSQL storage for alert history: two tables, one write path, bounded reads.

``alert_history_sources`` holds one row per source instance (a backend process
start, a monitor runtime) with the last snapshot it reported.
``alert_history_events`` holds the facts, unique on
``(source, source_namespace, origin_record_id)``.

**Idempotency.** A record is inserted with ``ON CONFLICT DO NOTHING``. A row
that did not insert is *not* assumed to be the same fact: its stored
``payload_digest`` is read back and compared. Equal is a duplicate (a resent
batch whose commit response was lost); different is a conflict, which leaves
the stored fact untouched and records the losing version as a
``history_conflict`` row carrying only its digest and kind.

**Bad records.** Each record is written in its own savepoint, so a value
PostgreSQL refuses fails that record alone; the backend writer drops it and
counts it, an ingest request is refused as a whole.

**Erasure.** A record about an account carries that account's erasure-fence
digest in ``subject_digests``. Before writing it, the transaction takes the
fence's shared advisory lock on each digest and checks ``erasure_fence``,
exactly as an ``api_logs`` write does (``log_schema``): a record whose subject
was erased is not written, however long it waited in a buffer, and a
hard delete that is in progress makes the write wait for it. The hard delete
removes the subject's rows in its own transaction (:func:`purge_subject`).

Nothing here deletes history on a timer.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import asyncpg

from serving.observability.alert_history.records import (
    SCHEMA_VERSION,
    HistoryRecord,
    finalize,
)
from serving.storage.log_schema import (
    ERASURE_FENCE_TABLE,
    bounded_ddl,
    ensure_erasure_fence_table,
    execute_ddl,
    fence_account_advisory_key,
    fence_account_digest,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

EVENTS_TABLE = "alert_history_events"
SOURCES_TABLE = "alert_history_sources"

_SCHEMA_LOCK_SQL = "SELECT pg_advisory_xact_lock(hashtext('hybridinference:alert-history-schema'))"

#: Statement budget inside a write or read transaction. A write waiting on an
#: erasure in progress gives up here and is retried later.
WRITE_STATEMENT_TIMEOUT = "5s"
READ_STATEMENT_TIMEOUT = "5s"

_TABLES: tuple[tuple[str, str], ...] = (
    (
        SOURCES_TABLE,
        f"""
        CREATE TABLE IF NOT EXISTS {SOURCES_TABLE} (
            source TEXT NOT NULL,
            source_namespace TEXT NOT NULL,
            source_instance_id TEXT NOT NULL,
            deployment_id TEXT NOT NULL,
            target_environment TEXT NOT NULL,
            process_id TEXT,
            release_id TEXT,
            started_at TIMESTAMPTZ,
            rules_snapshot JSONB,
            rules_snapshot_hash TEXT,
            capture_config JSONB,
            enabled_at TIMESTAMPTZ,
            last_observed_at TIMESTAMPTZ,
            last_report_at TIMESTAMPTZ,
            last_write_at TIMESTAMPTZ,
            sync_watermark TIMESTAMPTZ,
            pending_count BIGINT,
            pending_bytes BIGINT,
            oldest_pending_at TIMESTAMPTZ,
            pending_reported_at TIMESTAMPTZ,
            dropped_count BIGINT,
            refused_count BIGINT,
            last_error_code TEXT,
            last_error_at TIMESTAMPTZ,
            stopped_at TIMESTAMPTZ,
            stop_reason TEXT,
            lost_at_stop BIGINT,
            credential_id TEXT,
            first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (source, source_namespace, source_instance_id)
        )
        """,
    ),
    (
        EVENTS_TABLE,
        f"""
        CREATE TABLE IF NOT EXISTS {EVENTS_TABLE} (
            record_id UUID PRIMARY KEY,
            source TEXT NOT NULL,
            source_namespace TEXT NOT NULL,
            origin_record_id TEXT NOT NULL,
            payload_digest TEXT NOT NULL,
            schema_version INTEGER NOT NULL,
            source_instance_id TEXT,
            deployment_id TEXT NOT NULL,
            target_environment TEXT NOT NULL,
            observation_scope TEXT,
            process_id TEXT,
            producer_seq BIGINT,
            episode_id TEXT,
            source_incident_id TEXT,
            generation BIGINT,
            source_event_id TEXT,
            action_id TEXT,
            attempt_id TEXT,
            occurred_at TIMESTAMPTZ NOT NULL,
            recorded_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            kind TEXT NOT NULL,
            origin TEXT NOT NULL,
            alert_type TEXT,
            alert_key TEXT,
            severity TEXT,
            rule_id TEXT,
            rule_version TEXT,
            facts JSONB NOT NULL DEFAULT '{{}}'::jsonb,
            model_id TEXT,
            endpoint_id TEXT,
            request_refs JSONB,
            request_ids TEXT[] NOT NULL DEFAULT '{{}}',
            end_basis TEXT,
            decision_reason TEXT,
            count BIGINT,
            bucket_start TIMESTAMPTZ,
            bucket_end TIMESTAMPTZ,
            channel TEXT,
            outcome TEXT,
            error_code TEXT,
            duration_ms BIGINT,
            message JSONB,
            subject_digests TEXT[] NOT NULL DEFAULT '{{}}',
            limitations TEXT[] NOT NULL DEFAULT '{{}}',
            external_ref JSONB,
            audit_ref TEXT,
            association_incomplete BOOLEAN NOT NULL DEFAULT FALSE,
            credential_id TEXT
        )
        """,
    ),
)

_INDEXES: tuple[tuple[str, str], ...] = (
    (
        "uq_alert_history_events_origin",
        f"CREATE UNIQUE INDEX IF NOT EXISTS uq_alert_history_events_origin "
        f"ON {EVENTS_TABLE} (source, source_namespace, origin_record_id)",
    ),
    (
        "idx_alert_history_events_env_occurred",
        f"CREATE INDEX IF NOT EXISTS idx_alert_history_events_env_occurred "
        f"ON {EVENTS_TABLE} (target_environment, occurred_at DESC)",
    ),
    (
        "idx_alert_history_events_recorded",
        f"CREATE INDEX IF NOT EXISTS idx_alert_history_events_recorded "
        f"ON {EVENTS_TABLE} (recorded_at, record_id)",
    ),
    (
        # Per-target coverage: first and last commit of one source's history
        # about one environment.
        "idx_alert_history_events_target_recorded",
        f"CREATE INDEX IF NOT EXISTS idx_alert_history_events_target_recorded "
        f"ON {EVENTS_TABLE} (source, source_namespace, target_environment, recorded_at)",
    ),
    (
        "idx_alert_history_sources_target_report",
        f"CREATE INDEX IF NOT EXISTS idx_alert_history_sources_target_report "
        f"ON {SOURCES_TABLE} (source, source_namespace, target_environment, "
        f"last_report_at DESC NULLS LAST, source_instance_id)",
    ),
    (
        "idx_alert_history_sources_target_write",
        f"CREATE INDEX IF NOT EXISTS idx_alert_history_sources_target_write "
        f"ON {SOURCES_TABLE} (source, source_namespace, target_environment, "
        f"last_write_at DESC NULLS LAST)",
    ),
    (
        "idx_alert_history_events_episode",
        f"CREATE INDEX IF NOT EXISTS idx_alert_history_events_episode "
        f"ON {EVENTS_TABLE} (episode_id, recorded_at, record_id) WHERE episode_id IS NOT NULL",
    ),
    (
        "idx_alert_history_events_incident",
        f"CREATE INDEX IF NOT EXISTS idx_alert_history_events_incident "
        f"ON {EVENTS_TABLE} (source, source_namespace, source_incident_id, recorded_at) "
        f"WHERE source_incident_id IS NOT NULL",
    ),
    (
        "idx_alert_history_events_attempt",
        f"CREATE INDEX IF NOT EXISTS idx_alert_history_events_attempt "
        f"ON {EVENTS_TABLE} (attempt_id) WHERE attempt_id IS NOT NULL",
    ),
    (
        "idx_alert_history_events_subjects",
        f"CREATE INDEX IF NOT EXISTS idx_alert_history_events_subjects "
        f"ON {EVENTS_TABLE} USING GIN (subject_digests) WHERE subject_digests <> '{{}}'",
    ),
    (
        "idx_alert_history_events_requests",
        f"CREATE INDEX IF NOT EXISTS idx_alert_history_events_requests "
        f"ON {EVENTS_TABLE} USING GIN (request_ids) WHERE request_ids <> '{{}}'",
    ),
)

#: Data errors that make one record unwritable. Anything else (connection,
#: timeout, lock) is retryable and fails the whole batch.
DATA_ERRORS: tuple[type[BaseException], ...] = (
    asyncpg.DataError,
    asyncpg.IntegrityConstraintViolationError,
)


async def missing_schema_objects(conn: Any) -> set[str]:
    """Names of the history tables and indexes the database does not have yet."""
    names = [name for name, _ in (*_TABLES, *_INDEXES)]
    rows = await conn.fetch(
        "SELECT name FROM unnest($1::text[]) AS name WHERE to_regclass(name) IS NULL", names
    )
    return {row["name"] for row in rows}


async def ensure_schema(conn: Any) -> None:
    """Create the history tables and indexes (idempotent, catalog-gated).

    A settled database takes no lock. Otherwise the missing objects are created
    in one transaction under an advisory lock and the bounded DDL lock wait;
    :class:`serving.storage.log_schema.SchemaLockUnavailable` means retry later.
    The erasure fence table is ensured too: the write path reads it.
    """
    await ensure_erasure_fence_table(conn)
    if not await missing_schema_objects(conn):
        return
    async with bounded_ddl(conn), conn.transaction():
        await execute_ddl(conn, _SCHEMA_LOCK_SQL)
        missing = await missing_schema_objects(conn)
        for name, ddl in (*_TABLES, *_INDEXES):
            if name in missing:
                await execute_ddl(conn, ddl)


async def tables_present(conn: Any) -> bool:
    """Whether both history tables exist."""
    return bool(
        await conn.fetchval(
            "SELECT to_regclass($1) IS NOT NULL AND to_regclass($2) IS NOT NULL",
            EVENTS_TABLE,
            SOURCES_TABLE,
        )
    )


# -- writing ------------------------------------------------------------------

_EVENT_COLUMNS = (
    "record_id",
    "source",
    "source_namespace",
    "origin_record_id",
    "payload_digest",
    "schema_version",
    "source_instance_id",
    "deployment_id",
    "target_environment",
    "observation_scope",
    "process_id",
    "producer_seq",
    "episode_id",
    "source_incident_id",
    "generation",
    "source_event_id",
    "action_id",
    "attempt_id",
    "occurred_at",
    "kind",
    "origin",
    "alert_type",
    "alert_key",
    "severity",
    "rule_id",
    "rule_version",
    "facts",
    "model_id",
    "endpoint_id",
    "request_refs",
    "request_ids",
    "end_basis",
    "decision_reason",
    "count",
    "bucket_start",
    "bucket_end",
    "channel",
    "outcome",
    "error_code",
    "duration_ms",
    "message",
    "subject_digests",
    "limitations",
    "external_ref",
    "audit_ref",
    "association_incomplete",
    "credential_id",
)
_JSON_COLUMNS = frozenset({"facts", "request_refs", "message", "external_ref"})
_CASTS = dict.fromkeys(_JSON_COLUMNS, "::jsonb")
_CASTS.update({"record_id": "::uuid", "request_ids": "::text[]", "subject_digests": "::text[]"})
_CASTS["limitations"] = "::text[]"

_INSERT_SQL = (
    f"INSERT INTO {EVENTS_TABLE} ({', '.join(_EVENT_COLUMNS)}) VALUES ("
    + ", ".join(f"${i}{_CASTS.get(name, '')}" for i, name in enumerate(_EVENT_COLUMNS, 1))
    + ") ON CONFLICT (source, source_namespace, origin_record_id) DO NOTHING "
    "RETURNING record_id"
)


@dataclass(frozen=True)
class ItemResult:
    """What happened to one record of a batch."""

    origin_record_id: str
    status: str  # inserted | duplicate | conflict | erased | invalid
    record_id: str | None = None


class RetryableWriteError(RuntimeError):
    """The batch hit a race it cannot settle in this transaction; retry it."""


class InvalidRecords(Exception):
    """Records PostgreSQL refused, by index; the transaction was rolled back."""

    def __init__(self, indexes: list[int]) -> None:
        super().__init__(f"{len(indexes)} record(s) could not be stored")
        self.indexes = indexes


def _json(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _params(record: HistoryRecord, record_id: uuid.UUID) -> list[Any]:
    values: dict[str, Any] = {
        "record_id": str(record_id),
        "request_refs": (
            [{"request_id": rid, "owner_digest": owner} for rid, owner in record.request_refs]
            if record.request_refs
            else None
        ),
        "request_ids": [rid for rid, _ in record.request_refs],
        "subject_digests": list(record.subject_digests),
        "limitations": list(record.limitations),
    }
    out: list[Any] = []
    for name in _EVENT_COLUMNS:
        value = values[name] if name in values else getattr(record, name)
        if name in _JSON_COLUMNS:
            value = _json(value if name != "facts" else (value or {}))
        out.append(value)
    return out


async def _fenced_digests(conn: Any, digests: set[str]) -> set[str]:
    """Shared-lock each digest (sorted) and return the ones with a tombstone."""
    if not digests:
        return set()
    ordered = sorted(digests)
    for digest in ordered:
        await conn.execute(
            "SELECT pg_advisory_xact_lock_shared($1)", fence_account_advisory_key(digest)
        )
    rows = await conn.fetch(
        f"SELECT account_digest FROM {ERASURE_FENCE_TABLE} WHERE account_digest = ANY($1::text[])",
        ordered,
    )
    return {row["account_digest"] for row in rows}


def _conflict_record(record: HistoryRecord) -> HistoryRecord:
    """The quarantine row for the losing side of an identity collision."""
    marker = hashlib.sha256(
        f"{record.origin_record_id}\x00{record.payload_digest}".encode()
    ).hexdigest()
    return finalize(
        HistoryRecord(
            source=record.source,
            source_namespace=record.source_namespace,
            origin_record_id=f"conflict:{marker}",
            kind="history_conflict",
            occurred_at=record.occurred_at,
            deployment_id=record.deployment_id,
            target_environment=record.target_environment,
            origin=record.origin,
            source_instance_id=record.source_instance_id,
            facts={
                "conflicting_origin_record_id": record.origin_record_id[:200],
                "conflicting_kind": record.kind,
                "conflicting_payload_digest": record.payload_digest,
            },
            limitations=("payload_not_kept",),
            credential_id=record.credential_id,
        )
    )


async def write_records(
    conn: Any, records: Sequence[HistoryRecord], *, all_or_nothing: bool = False
) -> list[ItemResult]:
    """Write ``records`` inside the caller's transaction; one result per record.

    Raises:
        InvalidRecords: ``all_or_nothing`` and at least one record was refused.
    """
    fenced = await _fenced_digests(conn, {d for r in records for d in r.subject_digests})
    results: list[ItemResult] = []
    invalid: list[int] = []
    for index, record in enumerate(records):
        if fenced and fenced.intersection(record.subject_digests):
            results.append(ItemResult(record.origin_record_id, "erased"))
            continue
        try:
            async with conn.transaction():
                inserted = await conn.fetchval(_INSERT_SQL, *_params(record, uuid.uuid4()))
        except DATA_ERRORS:
            invalid.append(index)
            results.append(ItemResult(record.origin_record_id, "invalid"))
            continue
        if inserted is not None:
            results.append(ItemResult(record.origin_record_id, "inserted", str(inserted)))
            continue
        existing = await conn.fetchrow(
            f"SELECT record_id, payload_digest FROM {EVENTS_TABLE} "
            "WHERE source = $1 AND source_namespace = $2 AND origin_record_id = $3",
            record.source,
            record.source_namespace,
            record.origin_record_id,
        )
        if existing is None:
            # Conflicted on insert, then gone: a concurrent erasure removed it.
            # Retryable — the next attempt sees the fence or inserts afresh.
            raise RetryableWriteError("alert history row vanished during write")
        if existing["payload_digest"] == record.payload_digest:
            results.append(
                ItemResult(record.origin_record_id, "duplicate", str(existing["record_id"]))
            )
            continue
        quarantine = _conflict_record(record)
        async with conn.transaction():
            await conn.fetchval(_INSERT_SQL, *_params(quarantine, uuid.uuid4()))
        results.append(ItemResult(record.origin_record_id, "conflict", str(existing["record_id"])))
    if invalid and all_or_nothing:
        raise InvalidRecords(invalid)
    return results


_SOURCE_SQL = f"""
INSERT INTO {SOURCES_TABLE} AS s (
    source, source_namespace, source_instance_id, deployment_id, target_environment,
    process_id, release_id, started_at, rules_snapshot, rules_snapshot_hash, capture_config,
    enabled_at, last_observed_at, last_report_at, last_write_at, sync_watermark,
    pending_count, pending_bytes, oldest_pending_at, pending_reported_at,
    dropped_count, refused_count, last_error_code, last_error_at,
    stopped_at, stop_reason, lost_at_stop, credential_id
) VALUES (
    $1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10, $11::jsonb, $12, $13,
    clock_timestamp(), CASE WHEN $14 THEN clock_timestamp() END, $15,
    $16, $17, $18, $19, $20, $21, $22, $23, $24, $25, $26, $27
)
ON CONFLICT (source, source_namespace, source_instance_id) DO UPDATE SET
    deployment_id = EXCLUDED.deployment_id,
    target_environment = EXCLUDED.target_environment,
    process_id = COALESCE(EXCLUDED.process_id, s.process_id),
    release_id = COALESCE(EXCLUDED.release_id, s.release_id),
    started_at = COALESCE(s.started_at, EXCLUDED.started_at),
    rules_snapshot = COALESCE(EXCLUDED.rules_snapshot, s.rules_snapshot),
    rules_snapshot_hash = COALESCE(EXCLUDED.rules_snapshot_hash, s.rules_snapshot_hash),
    capture_config = COALESCE(EXCLUDED.capture_config, s.capture_config),
    enabled_at = COALESCE(s.enabled_at, EXCLUDED.enabled_at),
    last_observed_at = GREATEST(s.last_observed_at, EXCLUDED.last_observed_at),
    last_report_at = GREATEST(s.last_report_at, EXCLUDED.last_report_at),
    last_write_at = GREATEST(s.last_write_at, EXCLUDED.last_write_at),
    sync_watermark = GREATEST(s.sync_watermark, EXCLUDED.sync_watermark),
    pending_count = CASE WHEN s.pending_reported_at IS NULL
        OR EXCLUDED.pending_reported_at >= s.pending_reported_at
        THEN EXCLUDED.pending_count ELSE s.pending_count END,
    pending_bytes = CASE WHEN s.pending_reported_at IS NULL
        OR EXCLUDED.pending_reported_at >= s.pending_reported_at
        THEN EXCLUDED.pending_bytes ELSE s.pending_bytes END,
    oldest_pending_at = CASE WHEN s.pending_reported_at IS NULL
        OR EXCLUDED.pending_reported_at >= s.pending_reported_at
        THEN EXCLUDED.oldest_pending_at ELSE s.oldest_pending_at END,
    dropped_count = GREATEST(s.dropped_count, EXCLUDED.dropped_count),
    refused_count = GREATEST(s.refused_count, EXCLUDED.refused_count),
    pending_reported_at = GREATEST(s.pending_reported_at, EXCLUDED.pending_reported_at),
    last_error_code = CASE WHEN EXCLUDED.last_error_at IS NOT NULL
        AND (s.last_error_at IS NULL OR EXCLUDED.last_error_at >= s.last_error_at)
        THEN EXCLUDED.last_error_code ELSE s.last_error_code END,
    last_error_at = GREATEST(s.last_error_at, EXCLUDED.last_error_at),
    stopped_at = COALESCE(EXCLUDED.stopped_at, s.stopped_at),
    stop_reason = COALESCE(EXCLUDED.stop_reason, s.stop_reason),
    lost_at_stop = COALESCE(EXCLUDED.lost_at_stop, s.lost_at_stop),
    credential_id = COALESCE(EXCLUDED.credential_id, s.credential_id)
"""


def _ts(value: Any) -> dt.datetime | None:
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=dt.UTC)
    return dt.datetime.fromtimestamp(float(value), dt.UTC)


async def upsert_source(conn: Any, snapshot: dict[str, Any], *, wrote_events: bool) -> None:
    """Record a source instance's snapshot; timestamps never move backwards."""
    await conn.execute(
        _SOURCE_SQL,
        snapshot["source"],
        snapshot["source_namespace"],
        snapshot["source_instance_id"],
        snapshot["deployment_id"],
        snapshot["target_environment"],
        snapshot.get("process_id"),
        snapshot.get("release_id"),
        _ts(snapshot.get("started_at")),
        _json(snapshot.get("rules_snapshot")),
        snapshot.get("rules_snapshot_hash"),
        _json(snapshot.get("capture_config")),
        _ts(snapshot.get("enabled_at")),
        _ts(snapshot.get("observed_at")),
        wrote_events,
        _ts(snapshot.get("sync_watermark")),
        snapshot.get("pending_count"),
        snapshot.get("pending_bytes"),
        _ts(snapshot.get("oldest_pending_at")),
        _ts(snapshot.get("pending_reported_at")),
        snapshot.get("dropped_count"),
        snapshot.get("refused_count"),
        snapshot.get("last_error_code"),
        _ts(snapshot.get("last_error_at")),
        _ts(snapshot.get("stopped_at")),
        snapshot.get("stop_reason"),
        snapshot.get("lost_at_stop"),
        snapshot.get("credential_id"),
    )


class AlertHistoryStore:
    """The backend writer's storage: one transaction per batch, on its own pool."""

    def __init__(self, acquire: Any) -> None:
        #: ``acquire()`` returns an async context manager yielding a connection.
        self._acquire = acquire

    async def write_batch(self, records: Sequence[HistoryRecord], heartbeat: dict[str, Any]) -> int:
        """Commit ``records`` and the heartbeat; return how many were dropped."""
        async with self._acquire() as conn, conn.transaction():
            await conn.execute(f"SET LOCAL statement_timeout = '{WRITE_STATEMENT_TIMEOUT}'")
            results = await write_records(conn, records)
            await upsert_source(conn, heartbeat, wrote_events=True)
        return sum(1 for r in results if r.status in ("invalid", "erased"))

    async def heartbeat(self, heartbeat: dict[str, Any]) -> None:
        """Commit the heartbeat alone."""
        async with self._acquire() as conn, conn.transaction():
            await conn.execute(f"SET LOCAL statement_timeout = '{WRITE_STATEMENT_TIMEOUT}'")
            await upsert_source(conn, heartbeat, wrote_events=False)


# -- erasure ------------------------------------------------------------------


async def purge_subject(conn: Any, digest: str) -> int:
    """Delete every history row about the subject with fence digest ``digest``.

    Runs inside the hard delete's transaction, after its fence is established.
    """
    status = await conn.execute(
        f"DELETE FROM {EVENTS_TABLE} WHERE subject_digests @> ARRAY[$1]::text[]", digest
    )
    try:
        return int(status.rsplit(" ", 1)[-1])
    except (ValueError, IndexError):
        return 0


def subject_digest(user_id: str, fence_secret: str) -> str:
    """The digest a record about ``user_id`` carries."""
    return fence_account_digest(user_id, fence_secret)


# -- reading ------------------------------------------------------------------

MAX_PAGE = 200
DEFAULT_PAGE = 50

_EVENT_OUTPUT_COLUMNS = (
    "record_id",
    "source",
    "source_namespace",
    "origin_record_id",
    "schema_version",
    "source_instance_id",
    "deployment_id",
    "target_environment",
    "observation_scope",
    "process_id",
    "producer_seq",
    "episode_id",
    "source_incident_id",
    "generation",
    "source_event_id",
    "action_id",
    "attempt_id",
    "occurred_at",
    "recorded_at",
    "kind",
    "origin",
    "alert_type",
    "alert_key",
    "severity",
    "rule_id",
    "rule_version",
    "facts",
    "model_id",
    "endpoint_id",
    "request_ids",
    "end_basis",
    "decision_reason",
    "count",
    "bucket_start",
    "bucket_end",
    "channel",
    "outcome",
    "error_code",
    "duration_ms",
    "message",
    "cardinality(subject_digests) AS subject_count",
    "limitations",
    "external_ref",
    "audit_ref",
    "association_incomplete",
)
_SELECT_EVENTS = f"SELECT {', '.join(_EVENT_OUTPUT_COLUMNS)} FROM {EVENTS_TABLE}"


@dataclass(frozen=True)
class Scope:
    """What a reader may see. ``None`` fields are unrestricted (admin)."""

    deployment_ids: tuple[str, ...] | None = None
    target_environments: tuple[str, ...] | None = None
    sources: tuple[str, ...] | None = None

    @property
    def unrestricted(self) -> bool:
        """Whether this is the admin scope."""
        return (
            self.deployment_ids is None
            and self.target_environments is None
            and self.sources is None
        )

    def allows(self, *, deployment_id: str, target_environment: str, source: str) -> bool:
        """Whether one row is inside the scope."""
        return (
            (self.deployment_ids is None or deployment_id in self.deployment_ids)
            and (self.target_environments is None or target_environment in self.target_environments)
            and (self.sources is None or source in self.sources)
        )


class _Where:
    def __init__(self) -> None:
        self.clauses: list[str] = []
        self.params: list[Any] = []

    def add(self, clause: str, *values: Any) -> None:
        for value in values:
            self.params.append(value)
            clause = clause.replace("?", f"${len(self.params)}", 1)
        self.clauses.append(clause)

    def scope(self, scope: Scope, alias: str = "") -> None:
        if scope.deployment_ids is not None:
            self.add(f"{alias}deployment_id = ANY(?::text[])", list(scope.deployment_ids))
        if scope.target_environments is not None:
            self.add(f"{alias}target_environment = ANY(?::text[])", list(scope.target_environments))
        if scope.sources is not None:
            self.add(f"{alias}source = ANY(?::text[])", list(scope.sources))

    def sql(self) -> str:
        return (" WHERE " + " AND ".join(self.clauses)) if self.clauses else ""


def encode_cursor(
    position: tuple[dt.datetime, str],
    fingerprint: str,
    window: tuple[dt.datetime, dt.datetime] | None = None,
) -> str:
    """Opaque keyset cursor bound to the query it came from.

    ``window`` is the resolved time window of the first page, carried so a
    defaulted window ("the last 24 hours") does not move between pages.
    """
    data: dict[str, Any] = {"t": position[0].isoformat(), "i": position[1], "q": fingerprint}
    if window is not None:
        data["w"] = [window[0].isoformat(), window[1].isoformat()]
    raw = json.dumps(data)
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(
    cursor: str, fingerprint: str
) -> tuple[tuple[dt.datetime, str], tuple[dt.datetime, dt.datetime] | None]:
    """Decode a cursor into its position and window.

    Raises:
        ValueError: malformed, or from a different query.
    """
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode()).decode())
        position = (dt.datetime.fromisoformat(data["t"]), str(uuid.UUID(data["i"])))
        query = data["q"]
        window = None
        if "w" in data:
            window = (
                dt.datetime.fromisoformat(data["w"][0]),
                dt.datetime.fromisoformat(data["w"][1]),
            )
    except (ValueError, KeyError, TypeError, IndexError, json.JSONDecodeError) as exc:
        raise ValueError("malformed cursor") from exc
    if query != fingerprint or position[0].tzinfo is None:
        raise ValueError("cursor belongs to a different query")
    if window is not None and (window[0].tzinfo is None or window[1].tzinfo is None):
        raise ValueError("malformed cursor")
    return position, window


async def _read_tx(conn: Any) -> None:
    await conn.execute(f"SET LOCAL statement_timeout = '{READ_STATEMENT_TIMEOUT}'")


async def list_events(
    conn: Any,
    *,
    scope: Scope,
    since: dt.datetime,
    until: dt.datetime,
    filters: dict[str, Any],
    after: tuple[dt.datetime, str] | None,
    limit: int,
) -> list[dict[str, Any]]:
    """Events that occurred in ``[since, until)``, paged by ``(recorded_at, record_id)``.

    Same keyset as :func:`timeline`, with the same limitation: a row whose
    transaction committed after a cursor passed its ``recorded_at`` is not on
    any later page. Only a fresh scan finds it.
    """
    where = _Where()
    where.add("occurred_at >= ?", since)
    where.add("occurred_at < ?", until)
    where.scope(scope)
    for column, value in filters.items():
        if value is not None:
            where.add(f"{column} = ?", value)
    if after is not None:
        where.add("(recorded_at, record_id) > (?, ?::uuid)", after[0], after[1])
    where.params.append(limit + 1)
    sql = (
        f"{_SELECT_EVENTS}{where.sql()} ORDER BY recorded_at, record_id LIMIT ${len(where.params)}"
    )
    async with conn.transaction(readonly=True):
        await _read_tx(conn)
        rows = await conn.fetch(sql, *where.params)
    return [_event_row(row) for row in rows]


async def timeline(
    conn: Any,
    *,
    scope: Scope,
    match: dict[str, Any],
    after: tuple[dt.datetime, str] | None,
    limit: int,
) -> list[dict[str, Any]]:
    """Events matching ``match``, paged by ``(recorded_at, record_id)``.

    ``recorded_at`` is the database clock when the row was *inserted*, inside
    its transaction — not when that transaction committed, and not a
    changefeed position. Rows usually arrive in that order, but a transaction
    that inserts early and commits late becomes visible behind a cursor that
    has already passed it. Pages are therefore not a snapshot and not complete
    for late arrivals; a consumer that needs everything rescans the
    ``occurred_at`` window from the start and deduplicates by ``record_id``.
    Sort by ``occurred_at`` to display.
    """
    where = _Where()
    for column, value in match.items():
        where.add(f"{column} = ?", value)
    where.scope(scope)
    if after is not None:
        where.add("(recorded_at, record_id) > (?, ?::uuid)", after[0], after[1])
    where.params.append(limit + 1)
    sql = (
        f"{_SELECT_EVENTS}{where.sql()} ORDER BY recorded_at, record_id LIMIT ${len(where.params)}"
    )
    async with conn.transaction(readonly=True):
        await _read_tx(conn)
        rows = await conn.fetch(sql, *where.params)
    return [_event_row(row) for row in rows]


def _event_row(row: Any) -> dict[str, Any]:
    out = dict(row)
    for name in ("facts", "message", "external_ref"):
        if isinstance(out.get(name), str):
            out[name] = json.loads(out[name])
    out["record_id"] = str(out["record_id"])
    out["request_ids"] = list(out.get("request_ids") or [])
    out["limitations"] = list(out.get("limitations") or [])
    return out


async def known_gaps(
    conn: Any, *, scope: Scope, since: dt.datetime, until: dt.datetime, limit: int = 50
) -> list[dict[str, Any]]:
    """``history_gap`` and ``history_conflict`` rows recorded in the window."""
    where = _Where()
    where.add("kind = ANY(?::text[])", ["history_gap", "history_conflict"])
    where.add("occurred_at >= ?", since)
    where.add("occurred_at < ?", until)
    where.scope(scope)
    where.params.append(limit)
    sql = (
        f"{_SELECT_EVENTS}{where.sql()} ORDER BY occurred_at DESC, record_id "
        f"LIMIT ${len(where.params)}"
    )
    async with conn.transaction(readonly=True):
        await _read_tx(conn)
        rows = await conn.fetch(sql, *where.params)
    return [_event_row(row) for row in rows]


_SOURCE_COLUMNS = (
    "source, source_namespace, source_instance_id, deployment_id, target_environment, "
    "process_id, release_id, started_at, rules_snapshot_hash, enabled_at, last_observed_at, "
    "last_report_at, last_write_at, sync_watermark, pending_count, pending_bytes, "
    "oldest_pending_at, pending_reported_at, dropped_count, refused_count, last_error_code, "
    "last_error_at, stopped_at, stop_reason, lost_at_stop, first_seen_at"
)

#: Instances counted per target before the count is reported as a floor.
INSTANCE_COUNT_CAP = 1000


async def observed_targets(
    conn: Any, *, scope: Scope, limit: int = 200
) -> tuple[list[tuple[str, str, str, str]], bool]:
    """``(source, namespace, environment, deployment)`` groups that have reported.

    For listing sources no credential names any more; configured targets are
    looked up directly, never inferred from this (possibly truncated) list.
    """
    where = _Where()
    where.scope(scope)
    where.params.append(limit + 1)
    sql = (
        f"SELECT DISTINCT source, source_namespace, target_environment, deployment_id "
        f"FROM {SOURCES_TABLE}{where.sql()} ORDER BY 1, 2, 3, 4 LIMIT ${len(where.params)}"
    )
    async with conn.transaction(readonly=True):
        await _read_tx(conn)
        rows = await conn.fetch(sql, *where.params)
    groups = [tuple(row.values()) for row in rows]
    return groups[:limit], len(groups) > limit


async def target_status(
    conn: Any,
    *,
    scope: Scope,
    targets: list[tuple[str, str, str]],
    instance_limit: int = 20,
) -> dict[tuple[str, str, str], dict[str, Any]]:
    """Exact per-target state for each ``(source, namespace, environment)``.

    Each target is looked up on its own, so a busy source cannot push a quiet
    one out of a shared limit and make it look as if it never reported. Per
    target: latest report and latest commit, how many instances (a floor past
    :data:`INSTANCE_COUNT_CAP`), first and last committed record about it, and
    the ``instance_limit`` most recently reporting instances (one more is
    fetched so the caller can say the list is truncated).
    """
    if not targets:
        return {}
    # The same scope, once per table alias; both number their parameters
    # identically, so one parameter list serves both.
    where = _Where()
    where.scope(scope, alias="s.")
    events_where = _Where()
    events_where.scope(scope, alias="e.")
    extra = "".join(f" AND {c}" for c in where.clauses)
    ev_extra = "".join(f" AND {c}" for c in events_where.clauses)
    base = len(where.params)
    match_s = "s.source = t.source AND s.source_namespace = t.ns AND s.target_environment = t.env"
    match_e = "e.source = t.source AND e.source_namespace = t.ns AND e.target_environment = t.env"
    unnest = f"unnest(${base + 1}::text[], ${base + 2}::text[], ${base + 3}::text[]) AS t(source, ns, env)"
    summary_sql = f"""
        SELECT t.source, t.ns, t.env,
          (SELECT s.last_report_at FROM {SOURCES_TABLE} s WHERE {match_s}{extra}
            ORDER BY s.last_report_at DESC NULLS LAST LIMIT 1) AS last_report_at,
          (SELECT s.last_write_at FROM {SOURCES_TABLE} s WHERE {match_s}{extra}
            ORDER BY s.last_write_at DESC NULLS LAST LIMIT 1) AS last_write_at,
          (SELECT count(*) FROM (SELECT 1 FROM {SOURCES_TABLE} s WHERE {match_s}{extra}
            LIMIT {INSTANCE_COUNT_CAP + 1}) c) AS instance_count,
          (SELECT min(e.recorded_at) FROM {EVENTS_TABLE} e WHERE {match_e}{ev_extra})
            AS first_recorded_at,
          (SELECT max(e.recorded_at) FROM {EVENTS_TABLE} e WHERE {match_e}{ev_extra})
            AS last_recorded_at
        FROM {unnest}
    """
    instances_sql = f"""
        SELECT t.source AS t_source, t.ns AS t_ns, t.env AS t_env, i.*
        FROM {unnest}
        CROSS JOIN LATERAL (
            SELECT {", ".join("s." + c.strip() for c in _SOURCE_COLUMNS.split(","))}
            FROM {SOURCES_TABLE} s WHERE {match_s}{extra}
            ORDER BY s.last_report_at DESC NULLS LAST, s.source_instance_id
            LIMIT {int(instance_limit) + 1}
        ) i
    """
    columns = (
        [t[0] for t in targets],
        [t[1] for t in targets],
        [t[2] for t in targets],
    )
    async with conn.transaction(readonly=True):
        await _read_tx(conn)
        summaries = await conn.fetch(summary_sql, *where.params, *columns)
        instance_rows = await conn.fetch(instances_sql, *where.params, *columns)
    out: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in summaries:
        out[(row["source"], row["ns"], row["env"])] = {
            "last_report_at": row["last_report_at"],
            "last_write_at": row["last_write_at"],
            "instance_count": row["instance_count"],
            "instance_count_is_floor": row["instance_count"] > INSTANCE_COUNT_CAP,
            "first_recorded_at": row["first_recorded_at"],
            "last_recorded_at": row["last_recorded_at"],
            "instances": [],
        }
    for row in instance_rows:
        key = (row["t_source"], row["t_ns"], row["t_env"])
        item = {k: v for k, v in dict(row).items() if not k.startswith("t_")}
        out[key]["instances"].append(item)
    return out


async def timeline_summary(
    conn: Any, *, scope: Scope, match: dict[str, Any], limit: int = 100
) -> dict[str, Any]:
    """Whole-timeline facts in scope, not one page's.

    Two parts, read in one transaction. The **totals** — records, distinct
    generations, whether any trigger or end record exists — are one exact
    aggregate over every matching row (bounded by the read statement timeout,
    never by a row limit), so ``end_recorded`` is never inferred from a
    subset. The **per-generation list** is at most ``limit`` groups, in
    generation order, with ``generations_truncated`` when more exist.
    """
    where = _Where()
    for column, value in match.items():
        where.add(f"{column} = ?", value)
    where.scope(scope)
    totals_sql = (
        f"SELECT count(*) AS records, count(DISTINCT generation) AS generations, "
        f"bool_or(generation IS NULL) AS has_unnumbered, "
        f"coalesce(bool_or(kind = 'condition_triggered'), false) AS trigger_recorded, "
        f"coalesce(bool_or(kind = 'condition_ended'), false) AS end_recorded "
        f"FROM {EVENTS_TABLE}{where.sql()}"
    )
    params = [*where.params, limit + 1]
    groups_sql = (
        f"SELECT generation, count(*) AS records, "
        f"bool_or(kind = 'condition_triggered') AS trigger_recorded, "
        f"bool_or(kind = 'condition_ended') AS end_recorded, "
        f"min(occurred_at) AS first_occurred_at, max(occurred_at) AS last_occurred_at "
        f"FROM {EVENTS_TABLE}{where.sql()} GROUP BY generation "
        f"ORDER BY generation NULLS FIRST LIMIT ${len(params)}"
    )
    async with conn.transaction(readonly=True, isolation="repeatable_read"):
        await _read_tx(conn)
        totals = dict(await conn.fetchrow(totals_sql, *where.params))
        rows = await conn.fetch(groups_sql, *params)
    groups = [dict(row) for row in rows]
    # count(DISTINCT) skips NULL; an unnumbered group is a group too.
    group_count = totals.pop("generations") + (1 if totals.pop("has_unnumbered") else 0)
    return {
        **totals,
        "generation_count": group_count,
        "generations": groups[:limit],
        "generations_truncated": len(groups) > limit,
    }


async def coverage(
    conn: Any, *, scope: Scope, pairs: list[tuple[str, str]]
) -> dict[tuple[str, str], dict[str, Any]]:
    """First and last committed ``recorded_at`` per ``(source, namespace)``."""
    if not pairs:
        return {}
    where = _Where()
    where.scope(scope)
    extra = (" AND " + " AND ".join(where.clauses)) if where.clauses else ""
    base = len(where.params)
    sql = f"""
        SELECT p.source, p.ns,
               (SELECT min(recorded_at) FROM {EVENTS_TABLE}
                 WHERE source = p.source AND source_namespace = p.ns{extra}) AS first_recorded_at,
               (SELECT max(recorded_at) FROM {EVENTS_TABLE}
                 WHERE source = p.source AND source_namespace = p.ns{extra}) AS last_recorded_at
        FROM unnest(${base + 1}::text[], ${base + 2}::text[]) AS p(source, ns)
    """
    async with conn.transaction(readonly=True):
        await _read_tx(conn)
        rows = await conn.fetch(sql, *where.params, [p[0] for p in pairs], [p[1] for p in pairs])
    return {
        (row["source"], row["ns"]): {
            "first_recorded_at": row["first_recorded_at"],
            "last_recorded_at": row["last_recorded_at"],
        }
        for row in rows
    }


async def events_referencing_request(
    conn: Any, *, scope: Scope, request_id: str, limit: int = 5
) -> list[dict[str, Any]]:
    """Backend events in scope that reference ``request_id``, with owner digests."""
    where = _Where()
    where.add("request_ids @> ARRAY[?]::text[]", request_id)
    where.add("source = ?", "backend")
    where.scope(scope)
    where.params.append(limit)
    sql = (
        f"SELECT record_id, episode_id, kind, occurred_at, request_refs FROM {EVENTS_TABLE}"
        f"{where.sql()} ORDER BY occurred_at, record_id LIMIT ${len(where.params)}"
    )
    async with conn.transaction(readonly=True):
        await _read_tx(conn)
        rows = await conn.fetch(sql, *where.params)
    out = []
    for row in rows:
        refs = row["request_refs"]
        refs = json.loads(refs) if isinstance(refs, str) else (refs or [])
        owners = [r.get("owner_digest") for r in refs if r.get("request_id") == request_id]
        out.append(
            {
                "record_id": str(row["record_id"]),
                "episode_id": row["episode_id"],
                "kind": row["kind"],
                "occurred_at": row["occurred_at"],
                "owner_digests": [o for o in owners if o],
            }
        )
    return out


async def request_evidence(
    conn: Any, *, request_id: str, owner_digests: set[str], fence_secret: str
) -> tuple[str, dict[str, Any] | None]:
    """Limited ``api_logs`` metadata for a referenced request, fence-checked.

    Returns ``(status, evidence)``: ``available``, ``erased`` (the owner's
    tombstone exists), ``ownership_mismatch`` or ``not_found`` (never logged,
    purged by retention, or deleted). Reads under the owner's shared fence
    lock, so a hard delete in progress is waited for, and once it commits the
    evidence is gone.
    """
    async with conn.transaction(readonly=True):
        await conn.execute(f"SET LOCAL statement_timeout = '{READ_STATEMENT_TIMEOUT}'")
        if await _fenced_digests(conn, owner_digests):
            return "erased", None
        row = await conn.fetchrow(
            "SELECT timestamp, model_id, served_endpoint_id, status_code, latency_ms, "
            "user_id, metadata->>'credential_owner_id' AS credential_owner_id "
            "FROM api_logs WHERE request_id = $1",
            request_id,
        )
    if row is None:
        return "not_found", None
    owners = {
        fence_account_digest(owner, fence_secret)
        for owner in (row["user_id"], row["credential_owner_id"])
        if owner
    }
    if not owners & owner_digests:
        return "ownership_mismatch", None
    status = row["status_code"]
    return "available", {
        "request_id": request_id,
        "timestamp": row["timestamp"],
        "model_id": row["model_id"],
        "endpoint_id": row["served_endpoint_id"],
        "status_code": status,
        "latency_ms": row["latency_ms"],
        "error_class": (
            "unknown"
            if status is None
            else "ok"
            if status < 400
            else "client_error"
            if status < 500
            else "server_error"
        ),
    }


__all__ = [
    "SCHEMA_VERSION",
    "AlertHistoryStore",
    "InvalidRecords",
    "ItemResult",
    "Scope",
    "ensure_schema",
    "purge_subject",
    "write_records",
]
