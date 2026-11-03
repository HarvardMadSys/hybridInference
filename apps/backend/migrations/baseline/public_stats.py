"""The daily snapshots behind ``GET /public-stats`` and their retained inputs, frozen.

Copied from ``serving.analytics.public_stats``.
"""

from __future__ import annotations

from typing import Any

_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS public_stats_snapshots (
        id           BIGSERIAL   PRIMARY KEY,
        generated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        payload      JSONB       NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_public_stats_snapshots_generated
    ON public_stats_snapshots (generated_at DESC)
    """,
    # Additive counters for successful, non-probe requests removed from
    # api_logs. The prune writes these in the same transaction as the DELETE
    # (ops/db/archive-old-logs.sh in the deployment repo). No account id,
    # address, or message text is stored.
    """
    CREATE TABLE IF NOT EXISTS api_log_retained_daily (
        day            DATE   NOT NULL,
        model          TEXT   NOT NULL,
        requests       BIGINT NOT NULL,
        input_tokens   BIGINT NOT NULL,
        output_tokens  BIGINT NOT NULL,
        cached_tokens  BIGINT NOT NULL,
        PRIMARY KEY (day, model)
    )
    """,
    # Language sample and client totals for weeks no longer in api_logs.
    # account_key is a digest, so the public snapshot can count an account
    # once without storing the account id. The job writes these; the prune
    # does not delete them.
    """
    CREATE TABLE IF NOT EXISTS api_log_retained_language (
        week         DATE NOT NULL,
        account_key  TEXT NOT NULL,
        language     TEXT NOT NULL,
        PRIMARY KEY (week, account_key, language)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS api_log_retained_language_sample (
        week              DATE   PRIMARY KEY,
        messages_sampled  BIGINT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS api_log_retained_client (
        week         DATE   NOT NULL,
        account_key  TEXT   NOT NULL,
        agent        TEXT   NOT NULL,
        user_agent   TEXT   NOT NULL,
        requests     BIGINT NOT NULL,
        tokens       BIGINT NOT NULL,
        PRIMARY KEY (week, account_key, agent, user_agent)
    )
    """,
)


async def ensure_public_stats_schema(connection: Any) -> None:
    """Create the snapshot and retained-daily tables if they do not exist."""
    for statement in _SCHEMA_STATEMENTS:
        await connection.execute(statement)
