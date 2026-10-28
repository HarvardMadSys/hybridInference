"""Public usage snapshot: aggregation, privacy thresholds and storage."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from serving.analytics import public_stats as ps
from serving.analytics.public_stats import ClientRow

START = datetime(2026, 7, 31, 7, 33, tzinfo=UTC)  # a Friday
END = datetime(2026, 8, 12, 14, 0, tzinfo=UTC)  # a Wednesday
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


def test_daily_sums_every_model_of_a_day():
    day = date(2026, 8, 2)
    rows = [
        {"day": day, "model": "a", "input_tokens": 100, "output_tokens": 5, "requests": 3},
        {"day": day, "model": "b", "input_tokens": 20, "output_tokens": 1, "requests": 2},
    ]
    daily = ps.build_daily(rows, START, END)
    assert daily[2] == {
        "date": "2026-08-02",
        "input_tokens": 120,
        "output_tokens": 6,
        "requests": 5,
    }


CATALOG = {
    "models": [
        {"id": "open-model", "aliases": ["open-alias"]},
        {"id": "pro-model", "required_role": "pro"},
        {"id": "internal-model", "required_role": "internal", "aliases": ["internal-alias"]},
        {"id": "admin-model", "required_role": "admin"},
        {"id": "flagged-model", "admin_only": True},
        {"id": "typo-model", "required_role": "everyone"},
        {"id": "hidden-at-runtime"},
        {"id": "opened-at-runtime", "required_role": "internal"},
    ],
    "auto": {
        "default_profile": "coding",
        "profiles": {
            "coding": {"candidates": [{"model": "internal-model"}, {"model": "open-model"}]},
            "lab": {"candidates": [{"model": "internal-model"}]},
        },
    },
}


def test_public_model_names_follow_the_role_a_pro_account_has():
    names = ps.public_model_names(
        CATALOG, {"hidden-at-runtime": "internal", "opened-at-runtime": "free"}
    )
    assert names == {
        "open-model": "open-model",
        "open-alias": "open-model",
        "pro-model": "pro-model",
        "opened-at-runtime": "opened-at-runtime",
        # A tier is named when any of its candidates is; bare auto is its default tier.
        "auto-coding": "auto-coding",
        "auto": "auto",
    }


def test_public_model_names_ignore_an_unusable_auto_section():
    names = ps.public_model_names({**CATALOG, "auto": {"profiles": "nope"}}, {})
    assert not any(name.startswith("auto") for name in names)
    assert names["open-model"] == "open-model"


def test_models_name_public_models_and_pool_the_rest():
    d1, d2 = date(2026, 7, 31), date(2026, 8, 12)
    rows = [
        {"day": d1, "model": "open-alias", "input_tokens": 90, "output_tokens": 10, "requests": 2},
        {"day": d2, "model": "open-model", "input_tokens": 9, "output_tokens": 1, "requests": 1},
        {"day": d1, "model": "pro-model", "input_tokens": 500, "output_tokens": 0, "requests": 1},
        {
            "day": d1,
            "model": "internal-model",
            "input_tokens": 7,
            "output_tokens": 0,
            "requests": 1,
        },
        {"day": d2, "model": "retired-model", "input_tokens": 3, "output_tokens": 0, "requests": 4},
        {"day": d2, "model": None, "input_tokens": 1, "output_tokens": 0, "requests": 1},
    ]
    out = ps.build_models(rows, START, END, ps.public_model_names(CATALOG, {}))
    assert [m["name"] for m in out["items"]] == ["pro-model", "open-model"]
    open_model = out["items"][1]
    assert open_model["requests"] == 3
    assert open_model["tokens"] == 110
    assert len(open_model["daily_tokens"]) == 13
    assert open_model["daily_tokens"][0] == 100
    assert open_model["daily_tokens"][-1] == 10
    assert open_model["daily_requests"][1:-1] == [0] * 11
    assert out["other"]["requests"] == 6
    assert out["other"]["tokens"] == 11
    assert out["other"]["daily_requests"][0] == 1
    assert out["other"]["daily_requests"][-1] == 5
    assert "internal-model" not in json.dumps(out)
    assert "retired-model" not in json.dumps(out)


def test_models_without_other_traffic_publish_null_other():
    rows = [
        {
            "day": date(2026, 8, 1),
            "model": "open-model",
            "input_tokens": 1,
            "output_tokens": 1,
            "requests": 1,
        }
    ]
    out = ps.build_models(rows, START, END, {"open-model": "open-model"})
    assert out["other"] is None
    assert ps.build_models([], START, END, {}) == {"items": [], "other": None}


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
    assert payload["models"] is None
    text = json.dumps(payload)
    assert "secret-user-id" not in text
    assert "secret-host" not in text


def test_registrations_count_every_active_and_pending_account():
    sql = " ".join(ps.REGISTRATIONS_SQL.split())
    assert "status = 'active'" in sql
    assert "status = 'pending_approval'" in sql
    # No role or email-verification filter: every account counts.
    assert sql.endswith("FROM users")


def test_retained_day_bounds_keep_a_partial_last_day():
    assert ps.retained_day_bounds(START, END) == (date(2026, 7, 31), date(2026, 8, 13))
    midnight = datetime(2026, 8, 12, tzinfo=UTC)
    assert ps.retained_day_bounds(START, midnight) == (date(2026, 7, 31), date(2026, 8, 12))


@pytest.mark.asyncio
async def test_pruned_rows_still_count_in_request_and_token_totals():
    conn = _fake_conn(
        first_retained=date(2026, 7, 20),
        retained_rows=[
            {
                "day": date(2026, 7, 20),
                "model": "glm-4.6",
                "input_tokens": 80,
                "output_tokens": 20,
                "requests": 10,
                "cached_tokens": 8,
            }
        ],
    )
    payload = await ps.compute_snapshot(conn, now=datetime(2026, 8, 12, 14, 27, tzinfo=UTC))
    assert payload["window"]["start"] == "2026-07-20T00:00:00+00:00"
    assert payload["totals"]["requests"] == 12
    assert payload["totals"]["tokens"] == 155
    assert payload["totals"]["cached_input_share"] == round(8 / 130, 6)
    retained_call = next(
        c for c in conn.fetch.await_args_list if c.args[0] is ps.RETAINED_DAILY_SQL
    )
    assert retained_call.args[1:] == (date(2026, 7, 20), date(2026, 8, 13))


@pytest.mark.asyncio
async def test_retained_blank_model_and_datetime_day_still_count():
    conn = _fake_conn(
        first_retained=datetime(2026, 7, 20, 15, tzinfo=UTC),
        retained_rows=[
            {
                "day": datetime(2026, 7, 20, tzinfo=UTC),
                "model": "",
                "input_tokens": 4,
                "output_tokens": 1,
                "requests": 1,
                "cached_tokens": 0,
            }
        ],
    )
    payload = await ps.compute_snapshot(conn, now=datetime(2026, 8, 12, 14, 27, tzinfo=UTC))
    assert payload["window"]["start"] == "2026-07-20T00:00:00+00:00"
    # Live fixture is 2 requests / 55 tokens; the blank model still adds.
    assert payload["totals"]["requests"] == 3
    assert payload["totals"]["tokens"] == 60
    assert payload["daily"][0] == {
        "date": "2026-07-20",
        "input_tokens": 4,
        "output_tokens": 1,
        "requests": 1,
    }


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
    assert len(statements) == 2 * len(ps._SCHEMA_STATEMENTS)
    assert all("IF NOT EXISTS" in s for s in statements)
    joined = " ".join(statements).lower()
    for identifier in ("user_id", " ip ", "metadata", "prompt"):
        assert identifier not in joined


def _fake_conn(
    *,
    server_version="160004",
    sample_texts=None,
    sample_by_week=None,
    overrides_table=True,
    retained_rows=None,
    first_retained=None,
    first_log=START,
    live_clients=None,
    retained_languages=None,
    retained_language_samples=None,
    retained_clients=None,
):
    sample_texts = sample_texts or []

    async def fetch(sql, *args):
        if sql is ps.DAILY_SQL:
            return [
                {
                    "day": date(2026, 8, 3),
                    "model": "internal-model",
                    "input_tokens": 50,
                    "output_tokens": 5,
                    "requests": 2,
                    "cached_tokens": 0,
                }
            ]
        if sql is ps.RETAINED_DAILY_SQL:
            return retained_rows or []
        if sql is ps.RETAINED_LANGUAGE_SQL:
            return retained_languages or []
        if sql is ps.RETAINED_LANGUAGE_SAMPLE_SQL:
            return retained_language_samples or []
        if sql is ps.RETAINED_CLIENT_SQL:
            return retained_clients or []
        if sql is ps.CLIENTS_SQL:
            if live_clients is not None:
                return live_clients
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
        if sql is ps.VISIBILITY_OVERRIDES_SQL:
            return [{"model_id": "internal-model", "required_role": "free"}]
        assert "jsonb_array_elements" in sql
        texts = sample_texts
        if sample_by_week is not None:
            texts = sample_by_week.get(ps.week_start(args[0].date()), [])
        return [{"user_id": "u1", "text": t} for t in texts]

    async def fetchval(sql, *args):
        if sql == "SHOW server_version_num":
            return server_version
        if sql is ps.VISIBILITY_OVERRIDES_TABLE_SQL:
            return overrides_table
        if sql is ps.RETAINED_FIRST_DAY_SQL:
            return first_retained
        assert sql is ps.FIRST_LOG_SQL
        return first_log

    async def fetchrow(sql, *args):
        assert sql is ps.REGISTRATIONS_SQL
        return {"approved": 40, "waiting": 9}

    conn = MagicMock()
    conn.execute = AsyncMock()
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
    now = datetime(2026, 8, 12, 14, 27, tzinfo=UTC)
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
    # Without the catalog there is no per-model section.
    assert payload["models"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(("overrides_table", "named"), [(True, True), (False, False)])
async def test_compute_snapshot_names_models_after_runtime_overrides(overrides_table, named):
    conn = _fake_conn(overrides_table=overrides_table)
    payload = await ps.compute_snapshot(
        conn, now=datetime(2026, 8, 12, 14, 27, tzinfo=UTC), models_config=CATALOG
    )
    models = payload["models"]
    if named:
        # The override opens internal-model, so it is named.
        assert [m["name"] for m in models["items"]] == ["internal-model"]
        assert models["other"] is None
    else:
        assert models["items"] == []
        assert models["other"]["tokens"] == 55
    queried = [c.args[0] for c in conn.fetch.await_args_list]
    assert (ps.VISIBILITY_OVERRIDES_SQL in queried) is overrides_table


@pytest.mark.asyncio
async def test_languages_are_omitted_when_prompts_are_not_stored():
    conn = _fake_conn(server_version="150008", sample_texts=[])
    payload = await ps.compute_snapshot(conn, now=datetime(2026, 8, 12, 14, 27, tzinfo=UTC))
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


_FRENCH = [
    "Bonjour, pouvez-vous m'expliquer pourquoi le serveur renvoie une erreur après le déploiement ?",
    "Merci, et comment puis-je configurer la limite de requêtes pour chaque utilisateur ?",
]


@pytest.mark.asyncio
async def test_missing_prompt_text_does_not_wipe_a_language_checkpoint():
    conn = _fake_conn(
        sample_texts=[],
        retained_languages=[{"week": W2, "account_key": ps.account_key("old"), "language": "de"}],
        retained_language_samples=[{"week": W2, "messages_sampled": 6}],
    )
    held: list[ps.RetentionWrite] = []
    payload = await ps.compute_snapshot(
        conn, now=datetime(2026, 8, 12, 14, 27, tzinfo=UTC), retention_out=held
    )
    assert [item["code"] for item in payload["languages"]["items"]] == ["de"]
    assert payload["languages"]["messages_sampled"] == 6
    assert W2 not in held[0].language_weeks


def test_account_key_is_stable_and_not_the_account_id():
    assert ps.account_key("same-account") == ps.account_key("same-account")
    assert ps.account_key("same-account") != "same-account"
    assert ps.account_key(None) == ""
    assert ps.account_key("") == ""


@pytest.mark.asyncio
async def test_pruned_weeks_keep_languages_agents_and_accounts():
    # Regression: a prune used to drop every public stat that is not a token
    # or request total. Languages, agents and the account total come back
    # from the checkpoint, and the same account is not counted twice.
    now = datetime(2026, 8, 12, 14, 27, tzinfo=UTC)
    conn = _fake_conn(
        first_log=datetime(2026, 8, 3, tzinfo=UTC),
        first_retained=date(2026, 7, 20),
        retained_languages=[
            {
                "week": datetime(2026, 7, 27, tzinfo=UTC),
                "account_key": ps.account_key("same-account"),
                "language": "de",
            },
            {
                "week": W1,
                "account_key": ps.account_key("only-old"),
                "language": "ja",
            },
        ],
        retained_language_samples=[{"week": W1, "messages_sampled": 8}],
        retained_clients=[
            {
                "week": W1,
                "account_key": ps.account_key("same-account"),
                "agent": "Claude",
                "user_agent": "claude-cli/1.0",
                "requests": 30,
                "tokens": 400,
            }
        ],
        live_clients=[
            {
                "week": W2,
                "user_id": "same-account",
                "agent": "Claude",
                "user_agent": "claude-cli/2.1",
                "requests": 20,
                "tokens": 55,
            }
        ],
        sample_by_week={W1: _FRENCH, W2: _FRENCH},
    )
    held: list[ps.RetentionWrite] = []
    payload = await ps.compute_snapshot(conn, now=now, retention_out=held)
    languages = payload["languages"]
    assert [item["code"] for item in languages["items"]] == ["de", "fr", "ja"]
    assert languages["weekly"] == [0, 2, 1, 0]
    assert languages["messages_sampled"] == 10
    # de and ja are checkpointed accounts; fr is the account still in the logs.
    assert languages["accounts_classified"] == 3
    assert payload["totals"]["accounts"] == 1
    claude = payload["agents"]["products"][0]
    assert claude["name"] == "Claude Code"
    assert claude["tokens"] == 455
    assert payload["agents"]["clients_total"] == 1
    assert [week["products"] for week in payload["agents"]["weekly"]] == [0, 1, 1, 0]
    text = json.dumps(payload)
    assert "same-account" not in text
    assert "only-old" not in text
    assert "claude-cli/1.0" not in text
    assert ps.account_key("same-account") not in text

    write = held[0]
    assert W1 not in write.language_weeks
    assert W1 not in write.client_weeks
    assert W2 in write.language_weeks
    assert (W2, ps.account_key("u1"), "fr") in write.language_rows
    assert (
        W2,
        ps.account_key("same-account"),
        "Claude",
        "claude-cli/2.1",
        20,
        55,
    ) in write.client_rows


@pytest.mark.asyncio
async def test_partial_week_does_not_add_the_remaining_tail_twice():
    # The checkpoint for a week the cutoff splits was taken while the week
    # was complete. Adding the tail still in the logs would count it twice.
    conn = _fake_conn(
        first_log=START,
        first_retained=date(2026, 7, 20),
        retained_languages=[{"week": W1, "account_key": ps.account_key("old"), "language": "de"}],
        retained_language_samples=[{"week": W1, "messages_sampled": 4}],
        retained_clients=[
            {
                "week": W1,
                "account_key": ps.account_key("old"),
                "agent": "Claude",
                "user_agent": "claude-cli/1.0",
                "requests": 100,
                "tokens": 1000,
            }
        ],
        live_clients=[
            {
                "week": W1,
                "user_id": "other",
                "agent": "codex",
                "user_agent": "codex-tui/1.0",
                "requests": 40,
                "tokens": 400,
            },
            {
                "week": W2,
                "user_id": "u1",
                "agent": "Claude",
                "user_agent": "claude-cli/2.1",
                "requests": 20,
                "tokens": 55,
            },
        ],
        sample_by_week={W1: _FRENCH},
    )
    held: list[ps.RetentionWrite] = []
    payload = await ps.compute_snapshot(
        conn, now=datetime(2026, 8, 12, 14, 27, tzinfo=UTC), retention_out=held
    )
    assert [item["code"] for item in payload["languages"]["items"]] == ["de"]
    assert payload["languages"]["messages_sampled"] == 4
    names = [product["name"] for product in payload["agents"]["products"]]
    assert names == ["Claude Code"]
    assert payload["agents"]["products"][0]["tokens"] == 1055
    assert [week["products"] for week in payload["agents"]["weekly"]] == [0, 1, 1, 0]
    assert W1 not in held[0].language_weeks
    assert W1 not in held[0].client_weeks


@pytest.mark.asyncio
async def test_store_snapshot_refreshes_language_and_client_checkpoints():
    conn = MagicMock()
    conn.execute = AsyncMock()
    tx = MagicMock()
    tx.__aenter__ = AsyncMock(return_value=None)
    tx.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=tx)
    retention = ps.RetentionWrite(
        language_weeks=(W2,),
        language_rows=((W2, "digest", "fr"),),
        language_messages=((W2, 2),),
        client_weeks=(W2, W3),
        client_rows=((W2, "digest", "Claude", "claude-cli/2.1", 20, 55),),
    )
    await ps.store_snapshot(conn, {"schema_version": 1}, retention)
    statements = [call.args[0] for call in conn.execute.await_args_list]
    assert ps.DELETE_RETAINED_LANGUAGE_WEEKS_SQL in statements
    assert ps.INSERT_RETAINED_LANGUAGE_SQL in statements
    assert ps.INSERT_RETAINED_CLIENT_SQL in statements
    delete_clients = next(
        call
        for call in conn.execute.await_args_list
        if call.args[0] is ps.DELETE_RETAINED_CLIENT_WEEKS_SQL
    )
    assert delete_clients.args[1] == [W2, W3]
    assert statements[-2] == ps.INSERT_SNAPSHOT_SQL


@pytest.mark.asyncio
async def test_purge_retained_account_deletes_language_and_client_rows():
    conn = MagicMock()
    conn.execute = AsyncMock(side_effect=["DELETE 2", "DELETE 1"])
    removed = await ps.purge_retained_account(conn, "same-account")
    assert removed == 3
    statements = [call.args for call in conn.execute.await_args_list]
    assert statements[0][0] is ps.DELETE_RETAINED_LANGUAGE_ACCOUNT_SQL
    assert statements[1][0] is ps.DELETE_RETAINED_CLIENT_ACCOUNT_SQL
    assert statements[0][1] == ps.account_key("same-account")
    assert "user_id" not in statements[0][0]
