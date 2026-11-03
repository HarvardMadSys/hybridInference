"""The alert-history tables, frozen.

Copied from ``serving.storage.alert_history_store``.
"""

from __future__ import annotations

from typing import Any

from migrations.baseline.api_logs import ensure_erasure_fence_table
from migrations.baseline.ddl import bounded_ddl, execute_ddl

EVENTS_TABLE = "alert_history_events"
SOURCES_TABLE = "alert_history_sources"


_SCHEMA_LOCK_SQL = "SELECT pg_advisory_xact_lock(hashtext('hybridinference:alert-history-schema'))"


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
