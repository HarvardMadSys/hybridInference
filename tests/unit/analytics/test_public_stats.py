"""Public usage snapshot: aggregation, privacy thresholds and storage."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from serving.analytics import public_stats as ps
from serving.analytics.public_stats import ClientRow

START = datetime(2026, 7, 31, 7, 33, tzinfo=timezone.utc)  # a Friday
END = datetime(2026, 8, 12, 14, 0, tzinfo=timezone.utc)  # a Wednesday
W1, W2, W3 = date(2026, 7, 27), date(2026, 8, 3), date(2026, 8, 10)


def _client(week, user, agent=None, ua=None, requests=20, tokens=1000):
    return ClientRow(
        week=week, user_id=user, agent=agent, user_agent=ua, requests=requests, tokens=tokens
    )


def test_weeks_cover_partial_first_and_last_weeks():
    assert ps.weeks_between(START, END) == [W1, W2, W3]
    assert ps.week_start(date(2026, 8, 9)) == W2  # Sunday belongs to the Monday before


def test_daily_rows_are_zero_filled_across_the_window():
    rows = [{"day": date(2026, 8, 2), "input_tokens": 100, "output_tokens": 5, "requests": 3}]
    daily = ps.build_daily(rows, START, END)
    assert daily[0]["date"] == "2026-07-31"
    assert daily[-1]["date"] == "2026-08-12"
    assert len(daily) == 13
    assert daily[2] == {
        "date": "2026-08-02",
        "input_tokens": 100,
        "output_tokens": 5,
        "requests": 3,
    }
    assert daily[0]["requests"] == 0


def test_countries_count_weekly_activity_and_hide_exact_volumes():
    rows = [
        {"week": W2, "country_code": "USA", "continent_code": "NA", "requests": 900},
        {"week": W2, "country_code": "DEU", "continent_code": "EU", "requests": 100},
        {"week": W3, "country_code": "DEU", "continent_code": "EU", "requests": 99},
        {"week": W3, "country_code": "KEN", "continent_code": "AF", "requests": 1},
        {"week": W3, "country_code": "NZL", "continent_code": "OC", "requests": 0},
    ]
    out = ps.build_countries(rows, [W1, W2, W3])
    assert out["total"] == 3
    assert out["total_min_requests"] == 2  # USA 900, DEU 199
    assert out["continents"] == 3
    assert out["weekly"] == [
        {"any": 0, "min_requests": 0},
        {"any": 2, "min_requests": 2},
        {"any": 2, "min_requests": 0},
    ]
    assert out["top"][0] == {"code": "USA", "alpha2": "US", "share": 0.818182}
    by_code = {c["code"]: c for c in out["all"]}
    assert by_code["USA"]["level"] == 5
    assert by_code["KEN"]["level"] == 1
    assert by_code["KEN"]["alpha2"] == "KE"
    assert all(set(c) == {"code", "alpha2", "continent", "level"} for c in out["all"])


def test_agents_count_clients_and_products_with_thresholds():
    rows = [
        # Claude Code from three accounts, across two User-Agent versions.
        _client(W2, "a", "Claude", "claude-cli/2.1.289 (external, cli)"),
        _client(W2, "b", None, "claude-cli/2.1.292 (external, cli)"),
        _client(W3, "c", None, "claude-cli/2.1.292 (external, cli)", tokens=5000),
        # pi from one account: the product counts, its account count is withheld.
        _client(W3, "a", "pi", "OpenAI/JS 6.35.0", requests=12),
        # A custom client under the request threshold, and a generic SDK.
        _client(W3, "d", None, "my-bot/1.0", requests=9),
        _client(W3, "e", None, "python-requests/2.32.5", requests=500, tokens=3000),
        # A chat app is a client but not an agent product.
        _client(W3, "f", None, "doc_assistant", requests=10, tokens=10),
    ]
    out = ps.build_agents(rows, [W1, W2, W3])
    assert out["clients_total"] == 2  # claude-cli, doc_assistant; my-bot is under 10
    assert out["clients_multi_account"] == 1
    assert out["products_total"] == 2  # Claude Code, pi
    assert out["weekly"] == [
        {"clients": 0, "products": 0},
        {"clients": 1, "products": 1},
        {"clients": 2, "products": 2},
    ]
    products = {p["name"]: p for p in out["products"]}
    assert products["Claude Code"] == {
        "name": "Claude Code",
        "kind": "coding",
        "tokens": 7000,
        "accounts": 3,
    }
    assert products["pi"]["accounts"] is None
    assert products["Docs assistant"]["kind"] == "chat"
    shares = {k["kind"]: k["token_share"] for k in out["kinds"]}
    assert [k["kind"] for k in out["kinds"]] == list(ps.catalog.KINDS)
    assert shares["direct"] == pytest.approx(3000 / 12010, abs=1e-6)
    assert out["kind_weekly"][0] == dict.fromkeys(ps.catalog.KINDS, 0.0)


def test_languages_withhold_small_counts():
    by_week = {
        W2: {"a": {"en"}, "b": {"en", "zh"}, "c": {"en", "zh"}, "d": set()},
        W3: {"a": {"en"}, "e": {"zh"}, "f": {"lt"}},
    }
    out = ps.build_languages(by_week, [W1, W2, W3], messages=40)
    assert out["total"] == 3
    assert out["weekly"] == [0, 2, 3]
    assert out["items"] == [
        {"code": "en", "accounts": 3},
        {"code": "zh", "accounts": 3},
        {"code": "lt", "accounts": None},
    ]
    assert out["accounts_classified"] == 5
    assert out["accounts_non_english"] == 4
    assert out["messages_sampled"] == 40


def test_payload_is_aggregate_only():
    payload = ps.build_payload(
        generated_at=END,
        start=START,
        end=END,
        daily_rows=[
            {
                "day": date(2026, 8, 3),
                "input_tokens": 1000,
                "output_tokens": 10,
                "requests": 4,
                "cached_tokens": 900,
            }
        ],
        client_rows=[_client(W2, "secret-user-id", "Claude", "claude-cli/2.1.289 (secret-host)")],
        country_rows=[{"week": W2, "country_code": "USA", "continent_code": "NA", "requests": 4}],
        languages_by_week=None,
        messages_sampled=0,
        registrations_row={"approved": 12, "waiting": 7},
    )
    assert payload["schema_version"] == 1
    assert payload["weeks"] == ["2026-07-27", "2026-08-03", "2026-08-10"]
    assert payload["first_week_partial"] is True
    assert payload["last_week_partial"] is True
    assert payload["totals"] == {
        "tokens": 1010,
        "input_tokens": 1000,
        "output_tokens": 10,
        "requests": 4,
        "accounts": 1,
        "cached_input_share": 0.9,
    }
    assert payload["registrations"] == {"approved": 12, "waiting": 7}
    assert payload["languages"] is None
    text = json.dumps(payload)
    assert "secret-user-id" not in text
    assert "secret-host" not in text


def test_registrations_count_confirmed_non_team_accounts_only():
    sql = " ".join(ps.REGISTRATIONS_SQL.split())
    assert "FROM users" in sql
    assert "status = 'active'" in sql
    assert "status = 'pending_approval'" in sql
    assert "WHERE email_verified" in sql
    assert "role NOT IN ('admin', 'internal')" in sql


def test_payload_without_registrations_publishes_null():
    payload = ps.build_payload(
        generated_at=END,
        start=START,
        end=END,
        daily_rows=[],
        client_rows=[],
        country_rows=[],
        languages_by_week=None,
        messages_sampled=0,
    )
    assert payload["registrations"] is None


@pytest.mark.asyncio
async def test_schema_is_idempotent_ddl_without_identifying_columns():
    conn = MagicMock()
    conn.execute = AsyncMock()
    await ps.ensure_public_stats_schema(conn)
    await ps.ensure_public_stats_schema(conn)
    statements = [call.args[0] for call in conn.execute.await_args_list]
    assert len(statements) == 4
    assert all("IF NOT EXISTS" in s for s in statements)
    joined = " ".join(statements).lower()
    for identifier in ("user_id", " ip ", "metadata", "prompt"):
        assert identifier not in joined


def _fake_conn(*, server_version="160004", sample_texts=None):
    sample_texts = sample_texts or []

    async def fetch(sql, *args):
        if sql is ps.DAILY_SQL:
            return [
                {
                    "day": date(2026, 8, 3),
                    "input_tokens": 50,
                    "output_tokens": 5,
                    "requests": 2,
                    "cached_tokens": 0,
                }
            ]
        if sql is ps.CLIENTS_SQL:
            return [
                {
                    "week": W2,
                    "user_id": "u1",
                    "agent": "Claude",
                    "user_agent": "claude-cli/2.1",
                    "requests": 20,
                    "tokens": 55,
                }
            ]
        if sql is ps.COUNTRIES_SQL:
            return [{"week": W2, "country_code": "FRA", "continent_code": "EU", "requests": 2}]
        assert "jsonb_array_elements" in sql
        return [{"user_id": "u1", "text": t} for t in sample_texts]

    async def fetchval(sql, *args):
        if sql == "SHOW server_version_num":
            return server_version
        assert sql is ps.FIRST_LOG_SQL
        return START

    async def fetchrow(sql, *args):
        assert sql is ps.REGISTRATIONS_SQL
        return {"approved": 40, "waiting": 9}

    conn = MagicMock()
    conn.fetch = AsyncMock(side_effect=fetch)
    conn.fetchval = AsyncMock(side_effect=fetchval)
    conn.fetchrow = AsyncMock(side_effect=fetchrow)
    return conn


@pytest.mark.asyncio
async def test_compute_snapshot_reads_from_the_first_log_to_the_hour():
    texts = [
        "Bonjour, pouvez-vous m'expliquer pourquoi le serveur renvoie une erreur après le déploiement ?",
        "Merci, et comment puis-je configurer la limite de requêtes pour chaque utilisateur ?",
    ]
    conn = _fake_conn(sample_texts=texts)
    now = datetime(2026, 8, 12, 14, 27, tzinfo=timezone.utc)
    payload = await ps.compute_snapshot(conn, now=now)
    assert payload["window"]["start"] == START.isoformat()
    assert payload["window"]["end"] == "2026-08-12T14:00:00+00:00"
    assert payload["countries"]["total"] == 1
    assert payload["registrations"] == {"approved": 40, "waiting": 9}
    assert payload["agents"]["products"][0]["name"] == "Claude Code"
    assert payload["languages"]["items"] == [{"code": "fr", "accounts": None}]
    sample_calls = [c for c in conn.fetch.await_args_list if "jsonb_array_elements" in c.args[0]]
    assert len(sample_calls) == 3  # one per week in the window
    assert "pg_input_is_valid" in sample_calls[0].args[0]
    assert sample_calls[0].args[3] == ps.LANGUAGE_SAMPLE_PER_ACCOUNT


@pytest.mark.asyncio
async def test_languages_are_omitted_when_prompts_are_not_stored():
    conn = _fake_conn(server_version="150008", sample_texts=[])
    payload = await ps.compute_snapshot(
        conn, now=datetime(2026, 8, 12, 14, 27, tzinfo=timezone.utc)
    )
    assert payload["languages"] is None
    sample_sql = next(
        c.args[0] for c in conn.fetch.await_args_list if "jsonb_array_elements" in c.args[0]
    )
    assert "pg_input_is_valid" not in sample_sql


@pytest.mark.asyncio
async def test_store_snapshot_inserts_and_prunes_in_one_transaction():
    conn = MagicMock()
    conn.execute = AsyncMock()
    tx = MagicMock()
    tx.__aenter__ = AsyncMock(return_value=None)
    tx.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=tx)
    await ps.store_snapshot(conn, {"schema_version": 1})
    calls = [c.args for c in conn.execute.await_args_list]
    assert calls[-2] == (ps.INSERT_SNAPSHOT_SQL, '{"schema_version":1}')
    assert calls[-1] == (ps.PRUNE_SNAPSHOTS_SQL, ps.KEEP_SNAPSHOTS)
