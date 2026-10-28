"""Daily public usage snapshot: accounts, tokens, models, countries, languages and agent clients.

An offline job (``python -m serving.analytics.public_stats``, run once a day by
the deployment's scheduler) reads ``api_logs``, the hourly country rollup,
account statuses and the model catalog, reduces them to aggregates, and stores
the result as one JSON document in ``public_stats_snapshots``.
``GET /public-stats`` serves the newest document when the distribution opts in
with ``features.public_stats``.

Only aggregates leave this module: no account ids, IP addresses, message text
or raw User-Agent strings. Per-item account counts below
``MIN_PUBLIC_ACCOUNTS`` are withheld (``null``). Message text is read only to
detect its language and is never stored. A model is named only while the
catalog offers it to ``PUBLIC_MODEL_ROLE``; every other model's traffic is
counted together as "other", so an internal model's name never leaves.

Each run also checkpoints the language sample and the client totals for every
week still fully present in ``api_logs``. A later prune deletes the rows; the
next snapshot reads the checkpoint, so the language section, the agent
section and the account total do not fall. The checkpoint stores a digest of
the account id and the truncated User-Agent, never the message, and neither
value is published. Weeks removed before a checkpoint was written cannot be
reconstructed.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any

import yaml

from routing.model_selection import (
    AUTO_MODEL_ID,
    AUTO_MODEL_PREFIX,
    AutoConfigError,
    parse_auto_config,
)
from serving.analytics import agent_catalog as catalog
from serving.analytics.language_id import detect_message, user_languages
from serving.config.settings import has_role
from serving.model_access import default_required_role_for_model
from serving.utils.geo_resolver import ALPHA2_TO_ALPHA3
from serving.utils.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

logger = get_logger(__name__)

SCHEMA_VERSION = 1
LOOKBACK_WEEKS = 26
# A client name or product counts once it has this many successful requests
# (in the window for totals, in the week for weekly counts).
MIN_CLIENT_REQUESTS = 10
# The stricter weekly country line.
MIN_COUNTRY_REQUESTS = 100
# Per-item account counts below this are published as null.
MIN_PUBLIC_ACCOUNTS = 3
LANGUAGE_SAMPLE_PER_ACCOUNT = 8
TOP_COUNTRIES = 12
# A model is named when an account with this role may use it. Internal and
# admin models, and models no longer in the catalog, are counted as "other".
PUBLIC_MODEL_ROLE = "pro"
KEEP_SNAPSHOTS = 30
STATEMENT_TIMEOUT_MS = 600_000

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

LATEST_SNAPSHOT_SQL = """
SELECT payload FROM public_stats_snapshots
ORDER BY generated_at DESC, id DESC
LIMIT 1
"""

_NOT_PROBE = "COALESCE(metadata->>'synthetic_probe', 'false') <> 'true'"

FIRST_LOG_SQL = "SELECT MIN(timestamp) FROM api_logs"

# One row per (day, model). The model is the one the caller named, as their own
# request history shows it: an auto model counts as itself, not as the model
# chosen to serve it.
DAILY_SQL = f"""
SELECT
  (date_trunc('day', timestamp AT TIME ZONE 'UTC'))::date AS day,
  COALESCE(metadata->'auto'->>'requested', model_id) AS model,
  COUNT(*)::BIGINT AS requests,
  COALESCE(SUM(prompt_tokens), 0)::BIGINT AS input_tokens,
  COALESCE(SUM(completion_tokens), 0)::BIGINT AS output_tokens,
  COALESCE(SUM(cache_read_tokens), 0)::BIGINT AS cached_tokens
FROM api_logs
WHERE timestamp >= $1 AND timestamp < $2
  AND status_code = 200
  AND {_NOT_PROBE}
GROUP BY 1, 2
"""

# Same day/model grain as DAILY_SQL, for rows the prune has already removed.
# ``$1`` is inclusive and ``$2`` is exclusive, both UTC dates.
RETAINED_DAILY_SQL = """
SELECT day, model, requests, input_tokens, output_tokens, cached_tokens
FROM api_log_retained_daily
WHERE day >= $1 AND day < $2
"""

RETAINED_FIRST_DAY_SQL = "SELECT MIN(day) FROM api_log_retained_daily"

# Week is inclusive at ``$1`` and exclusive at ``$2``.
RETAINED_LANGUAGE_SQL = """
SELECT week, account_key, language
FROM api_log_retained_language
WHERE week >= $1 AND week < $2
"""

RETAINED_LANGUAGE_SAMPLE_SQL = """
SELECT week, messages_sampled
FROM api_log_retained_language_sample
WHERE week >= $1 AND week < $2
"""

RETAINED_CLIENT_SQL = """
SELECT week, account_key, agent, user_agent, requests, tokens
FROM api_log_retained_client
WHERE week >= $1 AND week < $2
"""

DELETE_RETAINED_LANGUAGE_WEEKS_SQL = (
    "DELETE FROM api_log_retained_language WHERE week = ANY($1::date[])"
)
DELETE_RETAINED_LANGUAGE_SAMPLE_WEEKS_SQL = (
    "DELETE FROM api_log_retained_language_sample WHERE week = ANY($1::date[])"
)
INSERT_RETAINED_LANGUAGE_SQL = """
INSERT INTO api_log_retained_language (week, account_key, language)
SELECT * FROM unnest($1::date[], $2::text[], $3::text[])
"""
INSERT_RETAINED_LANGUAGE_SAMPLE_SQL = """
INSERT INTO api_log_retained_language_sample (week, messages_sampled)
SELECT * FROM unnest($1::date[], $2::bigint[])
"""
DELETE_RETAINED_CLIENT_WEEKS_SQL = (
    "DELETE FROM api_log_retained_client WHERE week = ANY($1::date[])"
)
INSERT_RETAINED_CLIENT_SQL = """
INSERT INTO api_log_retained_client
    (week, account_key, agent, user_agent, requests, tokens)
SELECT * FROM unnest(
    $1::date[], $2::text[], $3::text[], $4::text[], $5::bigint[], $6::bigint[]
)
"""
DELETE_RETAINED_LANGUAGE_ACCOUNT_SQL = (
    "DELETE FROM api_log_retained_language WHERE account_key = $1"
)
DELETE_RETAINED_CLIENT_ACCOUNT_SQL = "DELETE FROM api_log_retained_client WHERE account_key = $1"

CLIENTS_SQL = f"""
SELECT
  (date_trunc('week', timestamp AT TIME ZONE 'UTC'))::date AS week,
  user_id,
  metadata->>'agent' AS agent,
  left(metadata->>'user_agent', 200) AS user_agent,
  COUNT(*)::BIGINT AS requests,
  COALESCE(SUM(COALESCE(prompt_tokens, 0) + COALESCE(completion_tokens, 0)), 0)::BIGINT AS tokens
FROM api_logs
WHERE timestamp >= $1 AND timestamp < $2
  AND status_code = 200
  AND {_NOT_PROBE}
GROUP BY 1, 2, 3, 4
"""

COUNTRIES_SQL = """
SELECT
  (date_trunc('week', hour_bucket AT TIME ZONE 'UTC'))::date AS week,
  country_code,
  continent_code,
  SUM(request_count)::BIGINT AS requests
FROM geo_hourly_demand
WHERE hour_bucket >= $1 AND hour_bucket < $2
  AND country_code NOT LIKE '?%'
GROUP BY 1, 2, 3
"""

# Up to $3 distinct newest-user-message hashes per account in [$1, $2), each
# read from the first request that carried it (the shortest history), and the
# text of that message. ``{valid_json}`` guards the jsonb cast where the server
# can check it (PostgreSQL 16+).
LANGUAGE_SAMPLE_SQL = f"""
WITH candidates AS (
  SELECT user_id, last_user_msg_hash AS h, MIN(id) AS id
  FROM api_logs
  WHERE timestamp >= $1 AND timestamp < $2
    AND status_code = 200
    AND user_id IS NOT NULL
    AND last_user_msg_hash IS NOT NULL
    AND last_user_msg_chars BETWEEN 8 AND 8000
    AND {_NOT_PROBE}
  GROUP BY 1, 2
), picked AS (
  SELECT user_id, id
  FROM (SELECT *, row_number() OVER (PARTITION BY user_id ORDER BY h) AS rn FROM candidates) r
  WHERE rn <= $3
)
SELECT p.user_id, (
  SELECT left(
    CASE jsonb_typeof(m->'content')
      WHEN 'string' THEN m->>'content'
      ELSE (
        SELECT string_agg(b->>'text', E'\\n')
        FROM jsonb_array_elements(m->'content') b
        WHERE b->>'type' IN ('text', 'input_text')
      )
    END, 6000)
  FROM jsonb_array_elements(l.prompt::jsonb) WITH ORDINALITY e(m, i)
  WHERE m->>'role' = 'user'
  ORDER BY i DESC
  LIMIT 1
) AS text
FROM picked p
JOIN api_logs l ON l.id = p.id
WHERE l.prompt IS NOT NULL
  AND left(l.prompt, 1) = '['
  AND jsonb_typeof(CASE WHEN {{valid_json}} THEN l.prompt::jsonb END) = 'array'
"""

# Accounts as of the run: approved (active) and waiting for review, whatever
# their role or email verification.
REGISTRATIONS_SQL = """
SELECT
  COUNT(*) FILTER (WHERE status = 'active')::BIGINT AS approved,
  COUNT(*) FILTER (WHERE status = 'pending_approval')::BIGINT AS waiting
FROM users
"""

# Runtime role overrides set from the admin console. The table is created by
# the gateway's operational store, so a database it has not run against lacks it.
VISIBILITY_OVERRIDES_TABLE_SQL = "SELECT to_regclass('model_visibility_overrides') IS NOT NULL"
VISIBILITY_OVERRIDES_SQL = "SELECT model_id, required_role FROM model_visibility_overrides"

INSERT_SNAPSHOT_SQL = "INSERT INTO public_stats_snapshots (payload) VALUES ($1::jsonb)"

PRUNE_SNAPSHOTS_SQL = """
DELETE FROM public_stats_snapshots
WHERE id NOT IN (
  SELECT id FROM public_stats_snapshots ORDER BY generated_at DESC, id DESC LIMIT $1
)
"""


_ALPHA3_TO_ALPHA2 = {alpha3: alpha2 for alpha2, alpha3 in ALPHA2_TO_ALPHA3.items()}


async def ensure_public_stats_schema(connection: Any) -> None:
    """Create the snapshot and retained-daily tables if they do not exist."""
    for statement in _SCHEMA_STATEMENTS:
        await connection.execute(statement)


# --------------------------------------------------------------------------
# Pure aggregation. Each section takes plain rows so it can be tested alone.
# --------------------------------------------------------------------------


def week_start(day: date) -> date:
    """Return the Monday that starts ``day``'s week."""
    return day - timedelta(days=day.weekday())


def weeks_between(start: datetime, end: datetime) -> list[date]:
    """Return the Monday of every week that overlaps ``[start, end)``."""
    weeks = []
    w, last = week_start(start.date()), week_start((end - timedelta(microseconds=1)).date())
    while w <= last:
        weeks.append(w)
        w += timedelta(weeks=1)
    return weeks


def _public_count(n: int) -> int | None:
    return n if n >= MIN_PUBLIC_ACCOUNTS else None


def _share(part: float, whole: float) -> float:
    return round(part / whole, 6) if whole else 0.0


def retained_day_bounds(start: datetime, end: datetime) -> tuple[date, date]:
    """Return ``[start_day, end_day)`` covering every UTC day that overlaps the window.

    A midnight ``end`` is already the next day, so that date stays exclusive.
    An ``end`` later in the day still owns that calendar day.
    """
    start_utc = start.astimezone(UTC)
    end_utc = end.astimezone(UTC)
    until = end_utc.date()
    if end_utc.time() != datetime.min.time():
        until += timedelta(days=1)
    return start_utc.date(), until


def days_between(start: datetime, end: datetime) -> list[date]:
    """Return every UTC day that overlaps ``[start, end)``."""
    days = []
    day, last = start.date(), (end - timedelta(microseconds=1)).date()
    while day <= last:
        days.append(day)
        day += timedelta(days=1)
    return days


def build_daily(
    rows: Iterable[Mapping[str, Any]], start: datetime, end: datetime
) -> list[dict[str, Any]]:
    """Return one entry per UTC day in ``[start, end)``, zero-filled, summed over models."""
    by_day: defaultdict[date, Counter[str]] = defaultdict(Counter)
    for row in rows:
        for key in ("input_tokens", "output_tokens", "requests"):
            by_day[row["day"]][key] += int(row[key])
    return [
        {
            "date": day.isoformat(),
            "input_tokens": by_day[day]["input_tokens"],
            "output_tokens": by_day[day]["output_tokens"],
            "requests": by_day[day]["requests"],
        }
        for day in days_between(start, end)
    ]


def public_model_names(
    models_config: Mapping[str, Any], overrides: Mapping[str, str]
) -> dict[str, str]:
    """Map each model id and alias the page may name to the id it is shown under.

    A model is public when ``PUBLIC_MODEL_ROLE`` meets its required role, after
    the runtime override. A role the entry names explicitly is read as written
    (unknown names never satisfy the public role); an omitted role reads as
    the same cost-derived default the registry's gate enforces (a paid or
    unknown-cost model publishes as pro; a provably free one stays free), and
    ``admin_only`` always hides. An auto model is public when its tier has a
    public candidate.

    Args:
        models_config: The parsed ``models.yaml``.
        overrides: Runtime required role by canonical model id.
    """
    names: dict[str, str] = {}
    for model in models_config.get("models") or []:
        if not isinstance(model, dict) or model.get("id") is None:
            continue
        model_id = str(model["id"])
        # Same default the registry's gate derives (issue #1576): a paid or
        # unknown-cost model without an explicit required_role publishes as
        # pro, so the public page's visibility agrees with what the gate
        # enforces.
        role = overrides.get(model_id) or default_required_role_for_model(model)
        if model.get("admin_only") or not has_role(PUBLIC_MODEL_ROLE, role):
            continue
        names[model_id] = model_id
        for alias in model.get("aliases") or []:
            names.setdefault(str(alias), model_id)

    if models_config.get("auto") is None:
        return names
    try:
        auto = parse_auto_config(models_config["auto"])
    except AutoConfigError:
        return names
    public_tiers = {
        name
        for name, profile in auto.profiles.items()
        if any(candidate.model_id in names for candidate in profile.candidates)
    }
    for name in public_tiers:
        names[f"{AUTO_MODEL_PREFIX}{name}"] = f"{AUTO_MODEL_PREFIX}{name}"
    if auto.default_profile in public_tiers:
        names[AUTO_MODEL_ID] = AUTO_MODEL_ID
    return names


def build_models(
    rows: Iterable[Mapping[str, Any]], start: datetime, end: datetime, names: Mapping[str, str]
) -> dict[str, Any]:
    """Return requests and tokens per day for each public model, and for the rest together.

    Args:
        rows: Successful requests per (day, model).
        start: Start of the window.
        end: End of the window.
        names: From :func:`public_model_names`; a model not in it counts as other.
    """
    days = days_between(start, end)
    index = {day: i for i, day in enumerate(days)}
    requests: defaultdict[str | None, list[int]] = defaultdict(lambda: [0] * len(days))
    tokens: defaultdict[str | None, list[int]] = defaultdict(lambda: [0] * len(days))
    for row in rows:
        name = names.get(row["model"]) if row["model"] is not None else None
        i = index[row["day"]]
        requests[name][i] += int(row["requests"])
        tokens[name][i] += int(row["input_tokens"]) + int(row["output_tokens"])

    def series(name: str | None) -> dict[str, Any]:
        return {
            "requests": sum(requests[name]),
            "tokens": sum(tokens[name]),
            "daily_requests": requests[name],
            "daily_tokens": tokens[name],
        }

    named = sorted((n for n in requests if n is not None), key=lambda n: (-sum(tokens[n]), n))
    return {
        "items": [{"name": name, **series(name)} for name in named],
        "other": series(None) if None in requests else None,
    }


def build_countries(rows: Iterable[Mapping[str, Any]], weeks: list[date]) -> dict[str, Any]:
    """Summarise the weekly country rollup: counts, top shares, and every country seen."""
    totals: Counter[str] = Counter()
    continent: dict[str, str] = {}
    weekly_any: Counter[date] = Counter()
    weekly_min: Counter[date] = Counter()
    for row in rows:
        requests = int(row["requests"])
        if requests <= 0:
            continue
        code = row["country_code"]
        totals[code] += requests
        continent[code] = row["continent_code"]
        weekly_any[row["week"]] += 1
        if requests >= MIN_COUNTRY_REQUESTS:
            weekly_min[row["week"]] += 1
    grand = sum(totals.values())
    top = max(totals.values(), default=0)
    seen = [
        {
            "code": code,
            "alpha2": _ALPHA3_TO_ALPHA2.get(code),
            "continent": continent[code],
            # 1-5 on a log scale of request volume, for shading only.
            "level": max(1, math.ceil(5 * math.log10(n + 1) / math.log10(top + 1))) if top else 1,
        }
        for code, n in totals.most_common()
    ]
    return {
        "total": len(totals),
        "total_min_requests": sum(1 for n in totals.values() if n >= MIN_COUNTRY_REQUESTS),
        "continents": len(set(continent.values())),
        "weekly": [{"any": weekly_any[w], "min_requests": weekly_min[w]} for w in weeks],
        "top": [
            {"code": code, "alpha2": _ALPHA3_TO_ALPHA2.get(code), "share": _share(n, grand)}
            for code, n in totals.most_common(TOP_COUNTRIES)
        ],
        "all": seen,
    }


@dataclass(frozen=True)
class ClientRow:
    """Successful requests for one (week, account, declared agent, User-Agent)."""

    week: date
    user_id: str | None
    agent: str | None
    user_agent: str | None
    requests: int
    tokens: int


def build_agents(rows: Iterable[ClientRow], weeks: list[date]) -> dict[str, Any]:
    """Count distinct clients (by User-Agent name) and known agent products."""
    client_requests: Counter[str] = Counter()
    client_accounts: defaultdict[str, set[str | None]] = defaultdict(set)
    client_weekly: defaultdict[date, Counter[str]] = defaultdict(Counter)
    product_requests: Counter[str] = Counter()
    product_tokens: Counter[str] = Counter()
    product_accounts: defaultdict[str, set[str | None]] = defaultdict(set)
    product_kind: dict[str, str] = {}
    product_weekly: defaultdict[date, Counter[str]] = defaultdict(Counter)
    kind_tokens: Counter[str] = Counter()
    kind_weekly: defaultdict[date, Counter[str]] = defaultdict(Counter)

    for row in rows:
        product, kind = catalog.classify(row.agent, row.user_agent)
        kind_tokens[kind] += row.tokens
        kind_weekly[row.week][kind] += row.tokens
        if product is not None:
            product_kind[product] = kind
            product_requests[product] += row.requests
            product_tokens[product] += row.tokens
            product_accounts[product].add(row.user_id)
            product_weekly[row.week][product] += row.requests
        if not catalog.is_generic_client(row.user_agent):
            name = catalog.client_name(row.user_agent)
            if name:
                client_requests[name] += row.requests
                client_accounts[name].add(row.user_id)
                client_weekly[row.week][name] += row.requests

    clients = [name for name, n in client_requests.items() if n >= MIN_CLIENT_REQUESTS]
    products = [p for p, n in product_requests.items() if n >= MIN_CLIENT_REQUESTS]
    agent_products = [p for p in products if product_kind[p] in catalog.AGENT_KINDS]
    total_tokens = sum(kind_tokens.values())

    def active(counter: Counter[str], kinds: frozenset[str] | None = None) -> int:
        return sum(
            1
            for name, n in counter.items()
            if n >= MIN_CLIENT_REQUESTS and (kinds is None or product_kind.get(name) in kinds)
        )

    return {
        "clients_total": len(clients),
        "clients_multi_account": sum(1 for name in clients if len(client_accounts[name]) >= 2),
        "products_total": len(agent_products),
        "weekly": [
            {
                "clients": active(client_weekly[w]),
                "products": active(product_weekly[w], catalog.AGENT_KINDS),
            }
            for w in weeks
        ],
        "products": [
            {
                "name": p,
                "kind": product_kind[p],
                "tokens": product_tokens[p],
                "accounts": _public_count(len(product_accounts[p])),
            }
            for p in sorted(products, key=lambda p: (-product_tokens[p], p))
        ],
        "kinds": [
            {"kind": kind, "token_share": _share(kind_tokens[kind], total_tokens)}
            for kind in catalog.KINDS
        ],
        "kind_weekly": [
            {
                kind: _share(kind_weekly[w][kind], sum(kind_weekly[w].values()))
                for kind in catalog.KINDS
            }
            for w in weeks
        ],
    }


def build_languages(
    by_week: Mapping[date, Mapping[str, set[str]]], weeks: list[date], messages: int
) -> dict[str, Any]:
    """Summarise per-week, per-account language sets."""
    accounts_by_language: defaultdict[str, set[str]] = defaultdict(set)
    classified: set[str] = set()
    weekly = []
    for w in weeks:
        languages: set[str] = set()
        for account, langs in by_week.get(w, {}).items():
            if langs:
                classified.add(account)
            for language in langs:
                accounts_by_language[language].add(account)
            languages |= langs
        weekly.append(len(languages))
    non_english = {
        a
        for language, accounts in accounts_by_language.items()
        if language != "en"
        for a in accounts
    }
    items = sorted(accounts_by_language.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    return {
        "total": len(accounts_by_language),
        "weekly": weekly,
        "items": [
            {"code": code, "accounts": _public_count(len(accounts))} for code, accounts in items
        ],
        "accounts_classified": len(classified),
        "accounts_non_english": len(non_english),
        "messages_sampled": messages,
    }


def build_payload(
    *,
    generated_at: datetime,
    start: datetime,
    end: datetime,
    daily_rows: Iterable[Mapping[str, Any]],
    client_rows: list[ClientRow],
    country_rows: Iterable[Mapping[str, Any]],
    languages_by_week: Mapping[date, Mapping[str, set[str]]] | None,
    messages_sampled: int,
    registrations_row: Mapping[str, Any] | None = None,
    model_names: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Assemble the public snapshot document.

    ``models`` is ``null`` when ``model_names`` is None (the catalog could not
    be read), rather than every model counted as other.
    """
    daily_rows = list(daily_rows)
    weeks = weeks_between(start, end)
    daily = build_daily(daily_rows, start, end)
    input_tokens = sum(d["input_tokens"] for d in daily)
    output_tokens = sum(d["output_tokens"] for d in daily)
    cached = sum(int(r["cached_tokens"]) for r in daily_rows)
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at.isoformat(),
        "window": {"start": start.isoformat(), "end": end.isoformat(), "days": len(daily)},
        "weeks": [w.isoformat() for w in weeks],
        "first_week_partial": start > datetime.combine(weeks[0], datetime.min.time(), UTC),
        "last_week_partial": end
        < datetime.combine(weeks[-1] + timedelta(weeks=1), datetime.min.time(), UTC),
        "totals": {
            "tokens": input_tokens + output_tokens,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "requests": sum(d["requests"] for d in daily),
            "accounts": len({r.user_id for r in client_rows if r.user_id}),
            # Providers that never report cache reads count as uncached, so this is a floor.
            "cached_input_share": _share(cached, input_tokens),
        },
        "registrations": (
            {
                "approved": int(registrations_row["approved"]),
                "waiting": int(registrations_row["waiting"]),
            }
            if registrations_row is not None
            else None
        ),
        "daily": daily,
        "models": (
            build_models(daily_rows, start, end, model_names) if model_names is not None else None
        ),
        "countries": build_countries(country_rows, weeks),
        "languages": (
            build_languages(languages_by_week, weeks, messages_sampled)
            if languages_by_week is not None
            else None
        ),
        "agents": build_agents(client_rows, weeks),
        "thresholds": {
            "min_client_requests": MIN_CLIENT_REQUESTS,
            "min_country_requests": MIN_COUNTRY_REQUESTS,
            "min_public_accounts": MIN_PUBLIC_ACCOUNTS,
        },
    }


# --------------------------------------------------------------------------
# Checkpoints for weeks the prune has removed from api_logs.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class LanguageSample:
    """Languages still detectable from ``api_logs``, by week."""

    by_week: dict[date, dict[str, set[str]]]
    messages_by_week: dict[date, int]
    sampled_weeks: frozenset[date]


@dataclass(frozen=True)
class RetentionWrite:
    """Checkpoint rows to refresh for weeks that are still complete in ``api_logs``."""

    language_weeks: tuple[date, ...]
    language_rows: tuple[tuple[date, str, str], ...]
    language_messages: tuple[tuple[date, int], ...]
    client_weeks: tuple[date, ...]
    client_rows: tuple[tuple[date, str, str, str, int, int], ...]


def account_key(user_id: str | None) -> str:
    """Return a digest of an account id, or ``""`` when there is none.

    Retained rows store the digest so one account is counted once across a
    pruned week and a week still in the logs. The published snapshot never
    includes it.
    """
    if not user_id:
        return ""
    return hashlib.sha256(user_id.encode()).hexdigest()


def _as_date(value: date | datetime) -> date:
    """Return a UTC calendar date. ``datetime`` is a ``date``, so test it first."""
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(UTC)
        return value.date()
    return value


def pruned_before_day(
    first_log: datetime | None, first_retained: date | datetime | None
) -> date | None:
    """Return the first day still in ``api_logs`` when older days were pruned.

    ``None`` means nothing in front of the remaining logs was removed, so
    every week in the window is still complete. ``date.max`` means the logs
    are gone and only the checkpoint remains.
    """
    retained = _as_date(first_retained) if first_retained is not None else None
    if first_log is None:
        return date.max if retained is not None else None
    start = _as_date(first_log)
    if retained is not None and retained < start:
        return start
    return None


def _week_coverage(week: date, pruned_before: date | None) -> str:
    """Return ``full``, ``partial`` or ``pruned`` for one Monday-start week."""
    if pruned_before is None:
        return "full"
    if week + timedelta(weeks=1) <= pruned_before:
        return "pruned"
    if week < pruned_before:
        return "partial"
    return "full"


def _hashed_languages(by_account: Mapping[str, set[str]]) -> dict[str, set[str]]:
    hashed: dict[str, set[str]] = {}
    for account, languages in by_account.items():
        if not languages:
            continue
        hashed.setdefault(account_key(account), set()).update(languages)
    return hashed


def _prefer_live_languages(
    coverage: str, sampled: bool, live_messages: int, has_retained: bool, prompts_stored: bool
) -> bool:
    """Return whether the live sample should replace this week's checkpoint."""
    if coverage != "full" or not sampled:
        return False
    if live_messages > 0:
        return True
    # No text in this week's logs. When no week had text, a stored checkpoint
    # is the last measurement; wiping it would drop the section.
    return prompts_stored or not has_retained


def merge_language_weeks(
    *,
    weeks: list[date],
    pruned_before: date | None,
    sample: LanguageSample,
    retained_by_week: Mapping[date, Mapping[str, set[str]]],
    retained_messages: Mapping[date, int],
) -> tuple[dict[date, dict[str, set[str]]] | None, int, RetentionWrite, int]:
    """Combine the live language sample with the checkpoint.

    A week still fully in the logs is re-sampled. A week the prune has
    already removed keeps its checkpoint, so a partial tail still in the
    logs is not added on top. Returns the sets ``build_languages`` consumes,
    the messages sampled, the checkpoint to write, and how many weeks came
    from the checkpoint.
    """
    prompts_stored = any(sample.messages_by_week.get(week, 0) > 0 for week in sample.sampled_weeks)
    merged: dict[date, dict[str, set[str]]] = {}
    total = 0
    replace: list[date] = []
    rows: list[tuple[date, str, str]] = []
    message_rows: list[tuple[date, int]] = []
    from_checkpoint = 0

    def _take_live(week: date, *, checkpoint: bool) -> None:
        nonlocal total
        languages = _hashed_languages(sample.by_week.get(week, {}))
        count = sample.messages_by_week.get(week, 0)
        if languages:
            merged[week] = languages
        total += count
        if not checkpoint:
            return
        replace.append(week)
        message_rows.append((week, count))
        for key, langs in languages.items():
            rows.extend((week, key, language) for language in sorted(langs))

    for week in weeks:
        coverage = _week_coverage(week, pruned_before)
        retained = retained_by_week.get(week, {})
        sampled = week in sample.sampled_weeks
        if _prefer_live_languages(
            coverage, sampled, sample.messages_by_week.get(week, 0), bool(retained), prompts_stored
        ):
            _take_live(week, checkpoint=True)
            continue
        if retained:
            merged[week] = {key: set(languages) for key, languages in retained.items()}
            total += retained_messages.get(week, 0)
            from_checkpoint += 1
            continue
        if coverage == "partial" and sampled:
            _take_live(week, checkpoint=True)

    has_language = any(languages for accounts in merged.values() for languages in accounts.values())
    if not has_language and total == 0:
        languages_by_week = None
        total = 0
    else:
        languages_by_week = merged
    write = RetentionWrite(
        language_weeks=tuple(replace),
        language_rows=tuple(rows),
        language_messages=tuple(message_rows),
        client_weeks=(),
        client_rows=(),
    )
    return languages_by_week, total, write, from_checkpoint


def _hashed_client(row: ClientRow) -> ClientRow:
    key = account_key(row.user_id)
    return ClientRow(
        week=_as_date(row.week),
        user_id=key or None,
        agent=(row.agent or "")[:200] or None,
        user_agent=(row.user_agent or "")[:200] or None,
        requests=row.requests,
        tokens=row.tokens,
    )


def _collapse_clients(rows: Iterable[ClientRow]) -> list[ClientRow]:
    """Sum rows that share a week, account, declared agent and User-Agent."""
    collapsed: dict[tuple[date, str | None, str | None, str | None], ClientRow] = {}
    for row in rows:
        identity = (row.week, row.user_id, row.agent, row.user_agent)
        previous = collapsed.get(identity)
        if previous is None:
            collapsed[identity] = row
            continue
        collapsed[identity] = ClientRow(
            week=row.week,
            user_id=row.user_id,
            agent=row.agent,
            user_agent=row.user_agent,
            requests=previous.requests + row.requests,
            tokens=previous.tokens + row.tokens,
        )
    return list(collapsed.values())


def merge_client_rows(
    *,
    weeks: list[date],
    pruned_before: date | None,
    live_rows: Iterable[ClientRow],
    retained_rows: Iterable[ClientRow],
) -> tuple[list[ClientRow], tuple[date, ...], int]:
    """Combine live client rows with the checkpoint.

    Returns the rows ``build_agents`` consumes (account ids already digested),
    the full or newly checkpointed weeks to rewrite, and how many weeks came
    from the checkpoint.
    """
    live_by_week: dict[date, list[ClientRow]] = defaultdict(list)
    for row in live_rows:
        live_by_week[_as_date(row.week)].append(_hashed_client(row))
    retained_by_week: dict[date, list[ClientRow]] = defaultdict(list)
    for row in retained_rows:
        retained_by_week[_as_date(row.week)].append(row)

    merged: list[ClientRow] = []
    replace: list[date] = []
    from_checkpoint = 0
    for week in weeks:
        coverage = _week_coverage(week, pruned_before)
        retained = retained_by_week.get(week, [])
        if coverage == "full":
            merged.extend(live_by_week.get(week, []))
            replace.append(week)
        elif retained:
            merged.extend(retained)
            from_checkpoint += 1
        elif coverage == "partial":
            merged.extend(live_by_week.get(week, []))
            replace.append(week)
    return _collapse_clients(merged), tuple(replace), from_checkpoint


def _client_write_row(row: ClientRow) -> tuple[date, str, str, str, int, int]:
    return (
        row.week,
        row.user_id or "",
        row.agent or "",
        row.user_agent or "",
        row.requests,
        row.tokens,
    )


def _retained_languages(
    rows: Iterable[Mapping[str, Any]], sample_rows: Iterable[Mapping[str, Any]]
) -> tuple[dict[date, dict[str, set[str]]], dict[date, int]]:
    """Group checkpoint language rows by week and account digest."""
    by_week: dict[date, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    for row in rows:
        by_week[_as_date(row["week"])][row["account_key"]].add(row["language"])
    counts = {_as_date(row["week"]): int(row["messages_sampled"]) for row in sample_rows}
    return {week: dict(accounts) for week, accounts in by_week.items()}, counts


def _retained_client(row: Mapping[str, Any]) -> ClientRow:
    """Build a client row from a checkpoint. The digest stays in ``user_id``."""
    return ClientRow(
        week=_as_date(row["week"]),
        user_id=row["account_key"] or None,
        agent=row["agent"] or None,
        user_agent=row["user_agent"] or None,
        requests=int(row["requests"]),
        tokens=int(row["tokens"]),
    )


def _command_count(status: str) -> int:
    try:
        return int(str(status).rsplit(" ", 1)[-1])
    except (ValueError, IndexError):
        return 0


async def write_retained_facts(conn: Any, retention: RetentionWrite) -> None:
    """Replace checkpoint rows for the weeks ``retention`` refreshes."""
    if retention.language_weeks:
        weeks = list(retention.language_weeks)
        await conn.execute(DELETE_RETAINED_LANGUAGE_WEEKS_SQL, weeks)
        await conn.execute(DELETE_RETAINED_LANGUAGE_SAMPLE_WEEKS_SQL, weeks)
        if retention.language_rows:
            week_col, key_col, language_col = zip(*retention.language_rows, strict=True)
            await conn.execute(
                INSERT_RETAINED_LANGUAGE_SQL, list(week_col), list(key_col), list(language_col)
            )
        if retention.language_messages:
            message_weeks, message_counts = zip(*retention.language_messages, strict=True)
            await conn.execute(
                INSERT_RETAINED_LANGUAGE_SAMPLE_SQL, list(message_weeks), list(message_counts)
            )
    if retention.client_weeks:
        await conn.execute(DELETE_RETAINED_CLIENT_WEEKS_SQL, list(retention.client_weeks))
        if retention.client_rows:
            columns = [list(column) for column in zip(*retention.client_rows, strict=True)]
            await conn.execute(INSERT_RETAINED_CLIENT_SQL, *columns)


async def purge_retained_account(conn: Any, user_id: str) -> int:
    """Delete retained language and client rows for one account.

    Raises ``asyncpg.UndefinedTableError`` when the stats job has not created
    the tables yet. Callers that purge an account should ignore that.
    """
    key = account_key(user_id)
    if not key:
        return 0
    removed = 0
    for statement in (DELETE_RETAINED_LANGUAGE_ACCOUNT_SQL, DELETE_RETAINED_CLIENT_ACCOUNT_SQL):
        removed += _command_count(await conn.execute(statement, key))
    return removed


# --------------------------------------------------------------------------
# Database I/O
# --------------------------------------------------------------------------


async def _sample_languages(
    conn: Any, weeks: list[date], start: datetime, end: datetime
) -> LanguageSample:
    """Detect each account's languages per week.

    A week whose query fails is left out of ``sampled_weeks`` so its
    checkpoint is kept. A week that succeeds with no text is included, which
    is what an empty week and a deployment that stores no message text look
    like.
    """
    version = await conn.fetchval("SHOW server_version_num")
    valid_json = "pg_input_is_valid(l.prompt, 'jsonb')" if int(version) >= 160000 else "TRUE"
    sql = LANGUAGE_SAMPLE_SQL.format(valid_json=valid_json)
    by_week: dict[date, dict[str, set[str]]] = {}
    messages_by_week: dict[date, int] = {}
    sampled: set[date] = set()
    for w in weeks:
        lo = max(start, datetime.combine(w, datetime.min.time(), UTC))
        hi = min(end, datetime.combine(w + timedelta(weeks=1), datetime.min.time(), UTC))
        try:
            rows = await conn.fetch(sql, lo, hi, LANGUAGE_SAMPLE_PER_ACCOUNT)
        except Exception:
            logger.warning(
                "public_stats: language sample failed for week %s; skipping it", w, exc_info=True
            )
            continue
        sampled.add(w)
        per_account: defaultdict[str, list[Any]] = defaultdict(list)
        count = 0
        for row in rows:
            if row["text"]:
                per_account[row["user_id"]].append(detect_message(row["text"]))
                count += 1
        messages_by_week[w] = count
        by_week[w] = {account: user_languages(found) for account, found in per_account.items()}
    return LanguageSample(by_week, messages_by_week, frozenset(sampled))


async def _visibility_overrides(conn: Any) -> dict[str, str]:
    """Return the runtime required role by canonical model id."""
    if not await conn.fetchval(VISIBILITY_OVERRIDES_TABLE_SQL):
        return {}
    rows = await conn.fetch(VISIBILITY_OVERRIDES_SQL)
    return {row["model_id"]: row["required_role"] for row in rows}


async def compute_snapshot(
    conn: Any,
    *,
    now: datetime | None = None,
    lookback_weeks: int = LOOKBACK_WEEKS,
    models_config: Mapping[str, Any] | None = None,
    retention_out: list[RetentionWrite] | None = None,
) -> dict[str, Any]:
    """Read the logs, the country rollup and account statuses and build the snapshot.

    ``models_config`` is the parsed ``models.yaml``; without it the snapshot
    has no per-model section. Request and token totals include
    ``api_log_retained_daily``. Language, agent and account totals include the
    checkpoint of weeks no longer in ``api_logs``. When ``retention_out`` is
    given, the checkpoint to refresh is appended to it.
    """
    now = now or datetime.now(UTC)
    end = now.replace(minute=0, second=0, microsecond=0)
    await ensure_public_stats_schema(conn)
    first_log = await conn.fetchval(FIRST_LOG_SQL)
    first_retained = await conn.fetchval(RETAINED_FIRST_DAY_SQL)
    earliest = datetime.combine(
        week_start(end.date()) - timedelta(weeks=lookback_weeks - 1),
        datetime.min.time(),
        UTC,
    )
    candidates: list[datetime] = []
    if first_log is not None:
        candidates.append(first_log)
    if first_retained is not None:
        if isinstance(first_retained, datetime):
            first_retained = first_retained.date()
        candidates.append(datetime.combine(first_retained, datetime.min.time(), UTC))
    start = max(earliest, min(candidates)) if candidates else earliest

    retained_from, retained_until = retained_day_bounds(start, end)
    retained_rows = []
    for row in await conn.fetch(RETAINED_DAILY_SQL, retained_from, retained_until):
        item = dict(row)
        # DATE columns can come back as a midnight datetime. build_daily keys
        # days by datetime.date, so a datetime would miss the chart and the totals.
        day = item["day"]
        if isinstance(day, datetime):
            item["day"] = day.date()
        # The prune stores a missing model as '' because the primary key
        # cannot hold NULL. That traffic is not a named model.
        if item["model"] == "":
            item["model"] = None
        retained_rows.append(item)
    daily_rows = [*await conn.fetch(DAILY_SQL, start, end), *retained_rows]
    live_clients = [
        ClientRow(
            week=row["week"],
            user_id=row["user_id"],
            agent=row["agent"],
            user_agent=row["user_agent"],
            requests=int(row["requests"]),
            tokens=int(row["tokens"]),
        )
        for row in await conn.fetch(CLIENTS_SQL, start, end)
    ]
    country_rows = await conn.fetch(COUNTRIES_SQL, start, end)
    registrations_row = await conn.fetchrow(REGISTRATIONS_SQL)
    model_names = (
        public_model_names(models_config, await _visibility_overrides(conn))
        if models_config is not None
        else None
    )
    weeks = weeks_between(start, end)
    pruned_before = pruned_before_day(first_log, first_retained)
    week_from, week_until = weeks[0], weeks[-1] + timedelta(weeks=1)
    sample = await _sample_languages(conn, weeks, start, end)
    retained_languages, retained_language_counts = _retained_languages(
        await conn.fetch(RETAINED_LANGUAGE_SQL, week_from, week_until),
        await conn.fetch(RETAINED_LANGUAGE_SAMPLE_SQL, week_from, week_until),
    )
    languages, messages, language_write, language_weeks = merge_language_weeks(
        weeks=weeks,
        pruned_before=pruned_before,
        sample=sample,
        retained_by_week=retained_languages,
        retained_messages=retained_language_counts,
    )
    retained_clients = [
        _retained_client(row)
        for row in await conn.fetch(RETAINED_CLIENT_SQL, week_from, week_until)
    ]
    client_rows, client_weeks, client_checkpoint_weeks = merge_client_rows(
        weeks=weeks,
        pruned_before=pruned_before,
        live_rows=live_clients,
        retained_rows=retained_clients,
    )
    if language_weeks or client_checkpoint_weeks:
        logger.info(
            "public_stats: kept %s language week(s) and %s client week(s) from before the prune",
            language_weeks,
            client_checkpoint_weeks,
        )
    if retention_out is not None:
        client_write = [
            _client_write_row(row) for row in client_rows if row.week in set(client_weeks)
        ]
        retention_out.append(
            RetentionWrite(
                language_weeks=language_write.language_weeks,
                language_rows=language_write.language_rows,
                language_messages=language_write.language_messages,
                client_weeks=client_weeks,
                client_rows=tuple(client_write),
            )
        )
    return build_payload(
        generated_at=now,
        start=start,
        end=end,
        daily_rows=daily_rows,
        client_rows=client_rows,
        country_rows=country_rows,
        languages_by_week=languages,
        messages_sampled=messages,
        registrations_row=registrations_row,
        model_names=model_names,
    )


def load_models_config() -> dict[str, Any] | None:
    """Read the gateway's ``models.yaml``, or return None when it cannot be read."""
    from serving.config.distribution import resolve_config_path

    try:
        path = resolve_config_path("models").path
        data = yaml.safe_load(path.read_text())
    except Exception:
        logger.warning(
            "public_stats: cannot read the model catalog; leaving out models", exc_info=True
        )
        return None
    return data if isinstance(data, dict) else None


async def store_snapshot(
    conn: Any, payload: dict[str, Any], retention: RetentionWrite | None = None
) -> None:
    """Insert the snapshot and keep only the newest ``KEEP_SNAPSHOTS``.

    ``retention`` refreshes language and client checkpoints in the same
    transaction, so a failed snapshot does not leave a half-written week.
    """
    await ensure_public_stats_schema(conn)
    async with conn.transaction():
        if retention is not None:
            await write_retained_facts(conn, retention)
        await conn.execute(INSERT_SNAPSHOT_SQL, json.dumps(payload, separators=(",", ":")))
        await conn.execute(PRUNE_SNAPSHOTS_SQL, KEEP_SNAPSHOTS)


async def _main(args: argparse.Namespace) -> int:
    import asyncpg

    from serving.config.settings import get_settings

    settings = get_settings()
    conn = await asyncpg.connect(
        host=settings.db_host,
        port=settings.db_port,
        user=settings.db_user,
        password=settings.db_password,
        database=settings.db_name,
        server_settings={
            "application_name": "public_stats",
            "statement_timeout": str(STATEMENT_TIMEOUT_MS),
        },
    )
    try:
        retained: list[RetentionWrite] = []
        payload = await compute_snapshot(
            conn,
            lookback_weeks=args.weeks,
            models_config=load_models_config(),
            retention_out=None if args.dry_run else retained,
        )
        if args.dry_run:
            json.dump(payload, sys.stdout, indent=2)
            sys.stdout.write("\n")
        else:
            await store_snapshot(conn, payload, retained[0] if retained else None)
        totals = payload["totals"]
        models = payload["models"]
        languages = payload["languages"]
        registrations = payload["registrations"]
        logger.info(
            "public_stats: %s snapshot for %s days: %s tokens, %s named models, %s countries, "
            "%s languages, %s clients; %s approved accounts, %s waiting",
            "computed" if args.dry_run else "stored",
            payload["window"]["days"],
            totals["tokens"],
            len(models["items"]) if models else "no",
            payload["countries"]["total"],
            languages["total"] if languages else "no",
            payload["agents"]["clients_total"],
            registrations["approved"],
            registrations["waiting"],
        )
    finally:
        await conn.close()
    return 0


def cli(argv: list[str] | None = None) -> int:
    """Compute today's public stats snapshot and store it (or print it with --dry-run)."""
    parser = argparse.ArgumentParser(description=cli.__doc__)
    parser.add_argument(
        "--dry-run", action="store_true", help="print the snapshot instead of storing it"
    )
    parser.add_argument(
        "--weeks", type=int, default=LOOKBACK_WEEKS, help="weeks of history to include"
    )
    args = parser.parse_args(argv)
    if args.weeks < 1:
        parser.error("--weeks must be at least 1")
    return asyncio.run(_main(args))


if __name__ == "__main__":
    raise SystemExit(cli())
