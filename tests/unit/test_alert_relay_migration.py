"""Contract tests for the alert relay D1 schema using stdlib SQLite."""

import sqlite3
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
MIGRATION = ROOT / "services/alert-relay-worker/migrations/0001_init.sql"


def database() -> sqlite3.Connection:
    """Create an in-memory database with the Worker migration applied."""
    connection = sqlite3.connect(":memory:")
    connection.executescript(MIGRATION.read_text(encoding="utf-8"))
    return connection


def insert_incident(
    connection: sqlite3.Connection,
    incident_id: str,
    *,
    environment: str = "staging",
    active_key: str | None = '["staging","gateway:test"]',
    status: str = "firing",
    occurrence_count: int = 1,
) -> None:
    """Insert the minimum valid incident row for constraint checks."""
    connection.execute(
        """
        INSERT INTO alert_incidents (
            id, environment, fingerprint, active_key, status, alert_json,
            occurrence_count, first_seen, last_seen, slack_channel_id,
            created_at, updated_at
        ) VALUES (?, ?, 'gateway:test', ?, ?, '{}', ?, ?, ?, 'C123', ?, ?)
        """,
        (
            incident_id,
            environment,
            active_key,
            status,
            occurrence_count,
            "2026-07-19T12:00:00.000Z",
            "2026-07-19T12:00:00.000Z",
            "2026-07-19T12:00:00.000Z",
            "2026-07-19T12:00:00.000Z",
        ),
    )


def test_migration_creates_core_tables():
    with database() as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }

    assert {"alert_incidents", "alert_jobs", "alert_receipts"} <= tables


def test_only_one_active_environment_fingerprint_is_allowed():
    with database() as connection:
        insert_incident(connection, "incident-1")
        with pytest.raises(sqlite3.IntegrityError):
            insert_incident(
                connection,
                "incident-2",
                active_key='["staging","gateway:test"]',
            )

        connection.execute(
            """
            UPDATE alert_incidents
            SET active_key = NULL, status = 'resolved'
            WHERE id = 'incident-1'
            """
        )
        insert_incident(
            connection,
            "incident-2",
            active_key='["staging","gateway:test"]',
        )


@pytest.mark.parametrize(
    ("environment", "status", "occurrence_count"),
    [
        ("unknown", "firing", 1),
        ("staging", "invalid", 1),
        ("staging", "firing", 0),
    ],
)
def test_incident_check_constraints(environment, status, occurrence_count):
    with database() as connection, pytest.raises(sqlite3.IntegrityError):
        insert_incident(
            connection,
            "incident-invalid",
            environment=environment,
            active_key="invalid-active-key",
            status=status,
            occurrence_count=occurrence_count,
        )
