"""The public-stats job against PostgreSQL, across runs.

The country rollup keeps 90 days, less than the 26-week window, so a week the
rollup has dropped takes its weekly country counts from the previous snapshot
instead of falling to zero, and the totals say from which day they count.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio

from serving.analytics import public_stats as ps
from tests.fixtures import postgres

if TYPE_CHECKING:
    import asyncpg

pytestmark = [pytest.mark.integration, pytest.mark.dbtest]

# Mondays, oldest first; NOW is a Wednesday afternoon in the third week.
W1, W2, W3 = (datetime(2026, 7, 27, tzinfo=UTC) + timedelta(weeks=i) for i in range(3))
NOW = datetime(2026, 8, 12, 14, 27, tzinfo=UTC)

_TABLES = (
    "geo_hourly_demand",
    "geo_hourly_coverage",
    "public_stats_snapshots",
    "api_log_retained_daily",
    "api_log_retained_language",
    "api_log_retained_language_sample",
    "api_log_retained_client",
    "api_logs",
)


@pytest_asyncio.fixture
async def conn():
    # A plain connection, as the job opens one: jsonb comes back as text.
    connection = await postgres.connect()
    await connection.execute(f"TRUNCATE TABLE {', '.join(_TABLES)}")
    try:
        yield connection
    finally:
        await connection.execute(f"TRUNCATE TABLE {', '.join(_TABLES)}")
        await connection.close()


async def _country(conn, hour: datetime, country: str, continent: str, requests: int) -> None:
    await conn.execute(
        "INSERT INTO geo_hourly_coverage (hour_bucket, rows_total, rows_with_ip) "
        "VALUES ($1, $2, $2) ON CONFLICT (hour_bucket) DO NOTHING",
        hour,
        requests,
    )
    await conn.execute(
        "INSERT INTO geo_hourly_demand "
        "(hour_bucket, country_code, continent_code, request_count, completion_tokens) "
        "VALUES ($1, $2, $3, $4, 0)",
        hour,
        country,
        continent,
        requests,
    )


async def _request(conn, at: datetime) -> None:
    await conn.execute(
        "INSERT INTO api_logs (request_id, model_id, provider, timestamp, status_code) "
        "VALUES ($1, 'm', 'p', $2, 200)",
        f"req-{at.isoformat()}",
        at,
    )


async def _run(conn) -> dict:
    retention: list[ps.RetentionWrite] = []
    payload = await ps.compute_snapshot(conn, now=NOW, retention_out=retention)
    await ps.store_snapshot(conn, payload, retention[0])
    return payload


async def test_a_week_the_country_rollup_dropped_keeps_its_weekly_counts(conn):
    await _request(conn, W1 + timedelta(hours=1))
    for country, continent in (("FRA", "EU"), ("USA", "NA"), ("KEN", "AF")):
        await _country(conn, W1 + timedelta(days=1, hours=3), country, continent, 150)
    await _country(conn, W2 + timedelta(days=2, hours=5), "FRA", "EU", 20)

    first = await _run(conn)
    assert first["countries"]["weekly"][:2] == [
        {"any": 3, "min_requests": 3},
        {"any": 1, "min_requests": 0},
    ]
    assert first["countries"]["total"] == 3

    # The rollup's purge reaches W1 (its rows go with their coverage).
    await conn.execute("DELETE FROM geo_hourly_coverage WHERE hour_bucket < $1", W2)

    second = await _run(conn)
    countries = second["countries"]
    assert countries["weekly"] == [
        {"any": 3, "min_requests": 3},  # from the first snapshot
        {"any": 1, "min_requests": 0},
        {"any": 0, "min_requests": 0},
    ]
    # The totals count what the rollup still holds, and say from when.
    assert countries["total"] == 1
    assert countries["since"] == (W2 + timedelta(days=2, hours=5)).date().isoformat()


async def test_the_first_run_carries_nothing(conn):
    await _request(conn, W2)
    await _country(conn, W2 + timedelta(hours=7), "DEU", "EU", 5)

    payload = await _run(conn)

    # The window starts at the first log, in W2.
    assert payload["weeks"] == [W2.date().isoformat(), W3.date().isoformat()]
    assert payload["countries"]["weekly"] == [
        {"any": 1, "min_requests": 0},
        {"any": 0, "min_requests": 0},
    ]
    assert payload["countries"]["since"] == W2.date().isoformat()


# --- closed weeks are read from their checkpoints ---------------------------------

_FRENCH = (
    "Bonjour, pouvez-vous m'expliquer pourquoi le serveur renvoie une erreur après le déploiement ?",
    "Merci, et comment puis-je configurer la limite de requêtes pour chaque utilisateur ?",
)
_GERMAN = (
    "Guten Tag, können Sie mir bitte erklären, warum der Server nach dem Update abstürzt?",
    "Danke, und wie kann ich das Limit für jeden einzelnen Benutzer richtig einstellen?",
)


class _Recording:
    """The connection, with the statements the job ran and their arguments kept."""

    def __init__(self, connection: asyncpg.Connection) -> None:
        self._connection = connection
        self.calls: list[tuple[str, tuple]] = []

    async def fetch(self, sql: str, *args):
        self.calls.append((sql, args))
        return await self._connection.fetch(sql, *args)

    async def fetchval(self, sql: str, *args):
        return await self._connection.fetchval(sql, *args)

    async def fetchrow(self, sql: str, *args):
        return await self._connection.fetchrow(sql, *args)

    async def execute(self, sql: str, *args):
        return await self._connection.execute(sql, *args)

    def transaction(self):
        return self._connection.transaction()


async def _message(conn, at: datetime, user: str, text: str, n: int) -> None:
    import json

    prompt = json.dumps([{"role": "system", "content": "x"}, {"role": "user", "content": text}])
    await conn.execute(
        """
        INSERT INTO api_logs (
            request_id, model_id, provider, timestamp, status_code, user_id, prompt,
            last_user_msg_hash, last_user_msg_chars, prompt_tokens, completion_tokens, metadata
        ) VALUES ($1, 'm', 'p', $2, 200, $3, $4, $5, $6, 10, 5, $7::jsonb)
        """,
        f"msg-{user}-{at.isoformat()}-{n}",
        at,
        user,
        prompt,
        hash((user, text)) & 0x7FFFFFFFFFFFFFFF,
        len(text),
        json.dumps({"agent": "Claude", "user_agent": "claude-cli/2.1"}),
    )


async def _seed_three_weeks(conn) -> None:
    for week, (user, texts) in zip(
        (W1, W2, W3), (("ana", _FRENCH), ("ben", _GERMAN), ("cai", _FRENCH)), strict=True
    ):
        for n, text in enumerate(texts):
            await _message(conn, week + timedelta(days=1, hours=n), user, text, n)


async def test_a_second_run_reads_closed_weeks_from_their_checkpoints(conn):
    await _seed_three_weeks(conn)

    first = await _run(conn)
    recording = _Recording(conn)
    second = await _run_recorded(recording)

    # The same answer as the run that read every week from the logs.
    for section in ("languages", "agents", "totals"):
        assert second[section] == first[section]
    assert [item["code"] for item in second["languages"]["items"]] == ["fr", "de"]
    assert second["languages"]["weekly"] == [1, 1, 1]
    # But W1 and W2, closed before the first run ended, were not read again.
    sampled = [args[0] for sql, args in recording.calls if "jsonb_array_elements" in sql]
    assert [ps.week_start(at.date()) for at in sampled] == [W3.date()]
    [clients] = [args for sql, args in recording.calls if sql is ps.CLIENTS_SQL]
    assert clients[0] == W3


async def _run_recorded(recording: _Recording) -> dict:
    retention: list[ps.RetentionWrite] = []
    payload = await ps.compute_snapshot(recording, now=NOW, retention_out=retention)
    # Nothing settled is rewritten.
    assert retention[0].language_weeks == (W3.date(),)
    assert retention[0].client_weeks == (W3.date(),)
    await ps.store_snapshot(recording, payload, retention[0])
    return payload


async def test_a_plain_text_prompt_that_starts_like_an_array_does_not_fail_its_week(conn):
    """The validity guard, on the server that has one (PostgreSQL 16+)."""
    if int(await conn.fetchval("SHOW server_version_num")) < 160000:
        pytest.skip("pg_input_is_valid needs PostgreSQL 16; older servers cast unguarded")
    for n, text in enumerate(_FRENCH):
        await _message(conn, W3 + timedelta(hours=n), "cai", text, n)
    await conn.execute(
        "INSERT INTO api_logs (request_id, model_id, provider, timestamp, status_code, "
        "user_id, prompt, last_user_msg_hash, last_user_msg_chars) "
        "VALUES ('plain', 'm', 'p', $1, 200, 'dee', '[INST] bonjour tout le monde [/INST]', 7, 30)",
        W3 + timedelta(hours=3),
    )

    payload = await ps.compute_snapshot(conn, now=NOW)

    assert [item["code"] for item in payload["languages"]["items"]] == ["fr"]
    assert payload["languages"]["messages_sampled"] == 2


async def test_the_job_names_the_models_the_stored_catalog_offers(monkeypatch, capsys):
    # The job runs in a process of its own: the catalog it names models from is
    # the one stored in app_config, which it has to load itself.
    import argparse
    import json

    from serving.config.settings import get_settings
    from tests.fixtures import migrations as mig

    catalog = {"models": [{"id": "open-model"}, {"id": "staff-model", "required_role": "admin"}]}
    async with mig.scratch_database() as database:
        await mig.upgrade(database)
        async with mig.connected(database) as scratch:
            await scratch.execute(
                "INSERT INTO app_config(key,value,secret,source) "
                "VALUES ('MODEL_CATALOG',$1,false,'admin')",
                json.dumps(catalog),
            )
            for model in ("open-model", "staff-model"):
                await scratch.execute(
                    "INSERT INTO api_logs (request_id, model_id, provider, timestamp, "
                    "status_code, prompt_tokens, completion_tokens) "
                    "VALUES ($1, $1, 'p', NOW() - INTERVAL '1 hour', 200, 10, 5)",
                    model,
                )
        monkeypatch.delenv("MODEL_CATALOG", raising=False)
        for key, value in {**postgres.target().app_env(), "DB_NAME": database}.items():
            monkeypatch.setenv(key, value)
        get_settings.cache_clear()

        assert await ps._main(argparse.Namespace(dry_run=True, weeks=2)) == 0

    models = json.loads(capsys.readouterr().out)["models"]
    assert [item["name"] for item in models["items"]] == ["open-model"]
    assert models["other"]["requests"] == 1
