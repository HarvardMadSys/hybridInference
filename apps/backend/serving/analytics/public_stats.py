"""Daily public usage snapshot: tokens, countries, languages and agent clients.

An offline job (``python -m serving.analytics.public_stats``, run once a day by
the deployment's scheduler) reads ``api_logs`` and the hourly country rollup,
reduces them to aggregates, and stores the result as one JSON document in
``public_stats_snapshots``. ``GET /public-stats`` serves the newest document
when the distribution opts in with ``features.public_stats``.

Only aggregates leave this module: no account ids, IP addresses, message text
or raw User-Agent strings. Per-item account counts below
``MIN_PUBLIC_ACCOUNTS`` are withheld (``null``). Message text is read only to
detect its language and is never stored.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from serving.analytics import agent_catalog as catalog
from serving.analytics.language_id import detect_message, user_languages
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
)

LATEST_SNAPSHOT_SQL = """
SELECT payload FROM public_stats_snapshots
ORDER BY generated_at DESC, id DESC
LIMIT 1
"""

_NOT_PROBE = "COALESCE(metadata->>'synthetic_probe', 'false') <> 'true'"

FIRST_LOG_SQL = "SELECT MIN(timestamp) FROM api_logs"

DAILY_SQL = f"""
SELECT
  (date_trunc('day', timestamp AT TIME ZONE 'UTC'))::date AS day,
  COUNT(*)::BIGINT AS requests,
  COALESCE(SUM(prompt_tokens), 0)::BIGINT AS input_tokens,
  COALESCE(SUM(completion_tokens), 0)::BIGINT AS output_tokens,
  COALESCE(SUM(cache_read_tokens), 0)::BIGINT AS cached_tokens
FROM api_logs
WHERE timestamp >= $1 AND timestamp < $2
  AND status_code = 200
  AND {_NOT_PROBE}
GROUP BY 1
"""

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

INSERT_SNAPSHOT_SQL = "INSERT INTO public_stats_snapshots (payload) VALUES ($1::jsonb)"

PRUNE_SNAPSHOTS_SQL = """
DELETE FROM public_stats_snapshots
WHERE id NOT IN (
  SELECT id FROM public_stats_snapshots ORDER BY generated_at DESC, id DESC LIMIT $1
)
"""


_ALPHA3_TO_ALPHA2 = {alpha3: alpha2 for alpha2, alpha3 in ALPHA2_TO_ALPHA3.items()}


async def ensure_public_stats_schema(connection: Any) -> None:
    """Create the snapshot table if it does not exist."""
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


def build_daily(
    rows: Iterable[Mapping[str, Any]], start: datetime, end: datetime
) -> list[dict[str, Any]]:
    """Return one entry per UTC day in ``[start, end)``, zero-filled."""
    by_day = {row["day"]: row for row in rows}
    days = []
    day, last = start.date(), (end - timedelta(microseconds=1)).date()
    while day <= last:
        row = by_day.get(day)
        days.append(
            {
                "date": day.isoformat(),
                "input_tokens": int(row["input_tokens"]) if row else 0,
                "output_tokens": int(row["output_tokens"]) if row else 0,
                "requests": int(row["requests"]) if row else 0,
            }
        )
        day += timedelta(days=1)
    return days


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
) -> dict[str, Any]:
    """Assemble the public snapshot document."""
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
        "first_week_partial": start > datetime.combine(weeks[0], datetime.min.time(), timezone.utc),
        "last_week_partial": end
        < datetime.combine(weeks[-1] + timedelta(weeks=1), datetime.min.time(), timezone.utc),
        "totals": {
            "tokens": input_tokens + output_tokens,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "requests": sum(d["requests"] for d in daily),
            "accounts": len({r.user_id for r in client_rows if r.user_id}),
            # Providers that never report cache reads count as uncached, so this is a floor.
            "cached_input_share": _share(cached, input_tokens),
        },
        "daily": daily,
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
# Database I/O
# --------------------------------------------------------------------------


async def _sample_languages(
    conn: Any, weeks: list[date], start: datetime, end: datetime
) -> tuple[dict[date, dict[str, set[str]]] | None, int]:
    """Detect each account's languages per week; None when prompts are not stored."""
    version = await conn.fetchval("SHOW server_version_num")
    valid_json = "pg_input_is_valid(l.prompt, 'jsonb')" if int(version) >= 160000 else "TRUE"
    sql = LANGUAGE_SAMPLE_SQL.format(valid_json=valid_json)
    by_week: dict[date, dict[str, set[str]]] = {}
    messages = 0
    for w in weeks:
        lo = max(start, datetime.combine(w, datetime.min.time(), timezone.utc))
        hi = min(end, datetime.combine(w + timedelta(weeks=1), datetime.min.time(), timezone.utc))
        try:
            rows = await conn.fetch(sql, lo, hi, LANGUAGE_SAMPLE_PER_ACCOUNT)
        except Exception:
            logger.warning(
                "public_stats: language sample failed for week %s; skipping it", w, exc_info=True
            )
            continue
        per_account: defaultdict[str, list[Any]] = defaultdict(list)
        for row in rows:
            if row["text"]:
                per_account[row["user_id"]].append(detect_message(row["text"]))
                messages += 1
        by_week[w] = {account: user_languages(found) for account, found in per_account.items()}
    if messages == 0:
        return None, 0
    return by_week, messages


async def compute_snapshot(
    conn: Any, *, now: datetime | None = None, lookback_weeks: int = LOOKBACK_WEEKS
) -> dict[str, Any]:
    """Read the logs and the country rollup and build the snapshot."""
    now = now or datetime.now(timezone.utc)
    end = now.replace(minute=0, second=0, microsecond=0)
    first_log = await conn.fetchval(FIRST_LOG_SQL)
    earliest = datetime.combine(
        week_start(end.date()) - timedelta(weeks=lookback_weeks - 1),
        datetime.min.time(),
        timezone.utc,
    )
    start = max(earliest, first_log) if first_log else earliest

    daily_rows = await conn.fetch(DAILY_SQL, start, end)
    client_rows = [
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
    languages, messages = await _sample_languages(conn, weeks_between(start, end), start, end)
    return build_payload(
        generated_at=now,
        start=start,
        end=end,
        daily_rows=daily_rows,
        client_rows=client_rows,
        country_rows=country_rows,
        languages_by_week=languages,
        messages_sampled=messages,
    )


async def store_snapshot(conn: Any, payload: dict[str, Any]) -> None:
    """Insert the snapshot and keep only the newest ``KEEP_SNAPSHOTS``."""
    await ensure_public_stats_schema(conn)
    async with conn.transaction():
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
        payload = await compute_snapshot(conn, lookback_weeks=args.weeks)
        if args.dry_run:
            json.dump(payload, sys.stdout, indent=2)
            sys.stdout.write("\n")
        else:
            await store_snapshot(conn, payload)
        totals = payload["totals"]
        languages = payload["languages"]
        logger.info(
            "public_stats: %s snapshot for %s days: %s tokens, %s countries, %s languages, %s clients",
            "computed" if args.dry_run else "stored",
            payload["window"]["days"],
            totals["tokens"],
            payload["countries"]["total"],
            languages["total"] if languages else "no",
            payload["agents"]["clients_total"],
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
