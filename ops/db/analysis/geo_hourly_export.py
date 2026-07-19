"""Export hourly geo-temporal demand aggregates for ``geo_globe.html``.

The real-data path streams ``api_logs`` and resolves network-origin IPs with
an offline DB-IP Country Lite database. The output contains aggregates only: no
IPs, user ids, or prompts leave the database.

Typical runs::

  uv run python ops/db/analysis/geo_hourly_export.py --days 30 \
      --geoip-country var/data/geoip/dbip-country-lite.mmdb \
      --geoip-provider dbip-lite \
      --out data.json
  uv run python ops/db/analysis/geo_hourly_export.py --demo --out data.json
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import math
import os
import random
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import asyncpg
import dotenv

from serving.utils.geo_resolver import ALPHA2_TO_ALPHA3, GeoResolver

BUCKET_COLS = ["c", "cc", "cont", "n", "err", "users", "tin", "tout", "gs", "p50", "p90"]
FLOW_COLS = ["c", "p", "e", "n"]

ROWS_QUERY = """
SELECT
  date_trunc('hour', timestamp AT TIME ZONE 'UTC') AT TIME ZONE 'UTC' AS hour,
  metadata->>'ip'               AS ip,
  provider,
  served_endpoint_id,
  prompt_tokens,
  completion_tokens,
  latency_ms,
  ttft_ms,
  (error IS NOT NULL OR COALESCE(status_code, 200) >= 400) AS is_err,
  user_id
FROM api_logs
WHERE timestamp >= $1 AND timestamp < $2
  AND COALESCE(metadata->>'synthetic_probe', 'false') <> 'true'
ORDER BY timestamp
"""

# Research-only serving metadata. External providers deliberately have no
# coordinates because their serving locations are not known.
PROVIDER_SITES: dict[str, dict[str, Any]] = {
    "sglang": {
        "kind": "local",
        "label": "Local cluster (sglang)",
        "region": "us-east",
        "cont": "NA",
        "coord": [-71.09, 42.36],
    },
    "vllm": {
        "kind": "local",
        "label": "Local cluster (vLLM)",
        "region": "us-east",
        "cont": "NA",
        "coord": [-71.09, 42.36],
    },
    "ollama": {
        "kind": "local",
        "label": "Local cluster (Ollama)",
        "region": "us-east",
        "cont": "NA",
        "coord": [-71.09, 42.36],
    },
    "deepseek": {"kind": "remote_api", "label": "DeepSeek API"},
    "kimi": {"kind": "remote_api", "label": "Moonshot Kimi API"},
    "minimax": {"kind": "remote_api", "label": "MiniMax API"},
    "zai": {"kind": "remote_api", "label": "Zhipu (Z.ai) API"},
    "chutes": {"kind": "remote_api", "label": "Chutes API"},
    "openrouter": {"kind": "remote_api", "label": "OpenRouter API"},
    "anthropic": {"kind": "remote_api", "label": "Anthropic API"},
    "openai": {"kind": "remote_api", "label": "OpenAI API"},
    "gemini": {"kind": "remote_api", "label": "Gemini API"},
}


class GeoBucket:
    """Mutable research accumulator for one hour and country bucket."""

    __slots__ = ("err", "gs", "n", "tin", "tout", "ttfts", "users")

    def __init__(self) -> None:
        self.n = 0
        self.err = 0
        self.users: set[Any] = set()
        self.tin = 0
        self.tout = 0
        self.gs = 0.0
        self.ttfts: list[int] = []


def _percentile(sorted_values: list[int], q: float) -> int | None:
    if not sorted_values:
        return None
    index = max(0, math.ceil(q * len(sorted_values)) - 1)
    return sorted_values[index]


def _floor_hour(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must include a timezone")
    return value.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)


def _normalize_window(since: datetime, until: datetime) -> tuple[datetime, datetime]:
    start = _floor_hour(since)
    end = _floor_hour(until)
    if start >= end:
        raise ValueError("since must be before until after hourly alignment")
    return start, end


def _build_hours_index(since: datetime, until: datetime) -> list[datetime]:
    hours: list[datetime] = []
    current = since
    while current < until:
        hours.append(current)
        current += timedelta(hours=1)
    return hours


def _finalize_geo_demand(
    hours_index: list[datetime],
    buckets: dict[tuple[datetime, str, str, str], GeoBucket],
    flows: dict[tuple[datetime, str, str, str], int],
    providers_seen: set[str],
    meta: dict[str, Any],
) -> dict[str, Any]:
    """Assemble the rich research payload consumed by ``geo_globe.html``."""
    hour_positions = {hour: index for index, hour in enumerate(hours_index)}
    hours_out: list[dict[str, list[list[Any]]]] = [{"b": [], "f": []} for _ in hours_index]

    for (hour, alpha3, alpha2, continent), bucket in sorted(
        buckets.items(), key=lambda item: (item[0][0], -item[1].n)
    ):
        if hour not in hour_positions:
            continue
        bucket.ttfts.sort()
        hours_out[hour_positions[hour]]["b"].append(
            [
                alpha3,
                alpha2,
                continent,
                bucket.n,
                bucket.err,
                len(bucket.users),
                bucket.tin,
                bucket.tout,
                round(bucket.gs, 1),
                _percentile(bucket.ttfts, 0.5),
                _percentile(bucket.ttfts, 0.9),
            ]
        )

    for (hour, alpha3, provider, endpoint_id), count in sorted(
        flows.items(), key=lambda item: (item[0][0], -item[1])
    ):
        if hour in hour_positions:
            hours_out[hour_positions[hour]]["f"].append([alpha3, provider, endpoint_id, count])

    providers_out = []
    for provider in sorted(providers_seen):
        site = PROVIDER_SITES.get(provider, {"kind": "remote_api", "label": f"{provider} API"})
        providers_out.append(
            {
                "id": provider,
                "label": site.get("label", provider),
                "kind": site.get("kind", "remote_api"),
                "region": site.get("region"),
                "cont": site.get("cont"),
                "coord": site.get("coord"),
            }
        )

    return {
        "meta": meta,
        "bucket_cols": BUCKET_COLS,
        "flow_cols": FLOW_COLS,
        "providers": providers_out,
        "hours_index": [hour.isoformat() for hour in hours_index],
        "hours": hours_out,
    }


def _add_geo_row(
    row: Any,
    resolver: GeoResolver,
    buckets: dict[tuple[datetime, str, str, str], GeoBucket],
    flows: dict[tuple[datetime, str, str, str], int],
    providers_seen: set[str],
) -> bool:
    ip = row["ip"]
    alpha3, alpha2, continent = resolver.resolve(ip)
    provider = row["provider"] or "unknown"
    endpoint_id = row["served_endpoint_id"] or provider
    providers_seen.add(provider)

    bucket = buckets[(row["hour"], alpha3, alpha2, continent)]
    bucket.n += 1
    if row["is_err"]:
        bucket.err += 1
    if row["user_id"] is not None:
        bucket.users.add(row["user_id"])
    bucket.tin += row["prompt_tokens"] or 0
    bucket.tout += row["completion_tokens"] or 0
    bucket.gs += (row["latency_ms"] or 0) / 1000.0
    if row["ttft_ms"] is not None:
        bucket.ttfts.append(int(row["ttft_ms"]))

    flows[(row["hour"], alpha3, provider, endpoint_id)] += 1
    return bool(ip)


async def _aggregate_geo_demand(
    connection: Any,
    since: datetime,
    until: datetime,
    resolver: GeoResolver,
    *,
    prefetch: int = 5_000,
) -> dict[str, Any]:
    since, until = _normalize_window(since, until)
    buckets: dict[tuple[datetime, str, str, str], GeoBucket] = defaultdict(GeoBucket)
    flows: dict[tuple[datetime, str, str, str], int] = defaultdict(int)
    providers_seen: set[str] = set()
    rows_total = 0
    rows_with_ip = 0

    async with connection.transaction():
        cursor = connection.cursor(ROWS_QUERY, since, until, prefetch=prefetch)
        async for row in cursor:
            rows_total += 1
            rows_with_ip += _add_geo_row(row, resolver, buckets, flows, providers_seen)
            if rows_total % 10_000 == 0:
                await asyncio.sleep(0)

    hours_index = _build_hours_index(since, until)
    meta = {
        "source": "api_logs",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "start": hours_index[0].isoformat() if hours_index else None,
        "hours": len(hours_index),
        "rows_total": rows_total,
        "rows_with_ip": rows_with_ip,
        "geoip": {
            "country": resolver.country_enabled,
            "provider": resolver.country_provider,
            "attribution": resolver.country_attribution,
        },
        "degraded": resolver.degraded,
        "degraded_reasons": list(resolver.degraded_reasons),
        "unmapped_alpha2": sorted(resolver.unmapped_a2),
        "notes": [
            "origin = network origin (IP-based), not user residence",
            "gs = sum of total request latency seconds",
        ],
    }
    return _finalize_geo_demand(hours_index, buckets, flows, providers_seen, meta)


def _load_env(env_path: str | None = None) -> None:
    """Load the first available .env file, matching export_logs.py conventions."""
    candidates = [
        env_path,
        os.environ.get("ENV_FILE"),
        "/srv/hybridInference/.env",
        str(Path(__file__).resolve().parents[3] / ".env"),
    ]
    for path in candidates:
        if path and Path(path).is_file():
            dotenv.load_dotenv(path, override=False)
            return


def _dsn() -> str:
    """Build the Postgres DSN from DB_* environment variables."""
    db_user = os.environ.get("DB_USER")
    if not db_user:
        raise SystemExit(
            "ERROR: DB_USER is not set. Load the .env file or set the environment variable."
        )
    return (
        f"postgresql://{db_user}"
        f":{os.environ.get('DB_PASSWORD', '')}"
        f"@{os.environ.get('DB_HOST', 'localhost')}"
        f":{os.environ.get('DB_PORT', '5432')}"
        f"/{os.environ.get('DB_NAME', 'freeinference_db')}"
    )


async def export_real(since: datetime, until: datetime, resolver: GeoResolver) -> dict:
    """Stream live rows through the research export aggregation."""
    pool = await asyncpg.create_pool(_dsn(), min_size=1, max_size=2)
    try:
        async with pool.acquire() as connection:
            payload = await _aggregate_geo_demand(connection, since, until, resolver)
    finally:
        await pool.close()
    meta = payload["meta"]
    print(
        f"aggregated {meta['rows_total']:,} rows ({meta['rows_with_ip']:,} with IP) "
        f"into {meta['hours']} hours"
    )
    return payload


# (alpha2, continent, utc_offset_hours, hourly_base)
_DEMO_COUNTRIES = [
    ("CN", "AS", 8, 345),
    ("US", "NA", -5, 480),
    ("SG", "AS", 8, 135),
    ("DE", "EU", 1, 150),
    ("GB", "EU", 0, 100),
    ("JP", "AS", 9, 100),
    ("IN", "AS", 5.5, 75),
    ("KR", "AS", 9, 55),
    ("HK", "AS", 8, 55),
    ("FR", "EU", 1, 55),
    ("NL", "EU", 1, 70),
    ("RU", "EU", 3, 35),
    ("CA", "NA", -5, 52),
    ("BR", "SA", -3, 43),
    ("AU", "OC", 10, 38),
    ("AE", "AS", 4, 21),
    ("TW", "AS", 8, 31),
    ("VN", "AS", 7, 22),
    ("NG", "AF", 1, 14),
    ("ZA", "AF", 2, 13),
]

_DEMO_PROVIDER_WEIGHTS = {
    "AS": [("sglang", 0.35), ("deepseek", 0.30), ("kimi", 0.15), ("zai", 0.10), ("minimax", 0.10)],
    "EU": [("vllm", 0.30), ("openrouter", 0.30), ("deepseek", 0.20), ("chutes", 0.20)],
    "NA": [("vllm", 0.40), ("openrouter", 0.25), ("deepseek", 0.20), ("sglang", 0.15)],
    "SA": [("openrouter", 0.40), ("deepseek", 0.30), ("vllm", 0.30)],
    "AF": [("openrouter", 0.40), ("deepseek", 0.30), ("vllm", 0.30)],
    "OC": [("openrouter", 0.40), ("sglang", 0.30), ("deepseek", 0.30)],
}


def _demo_hour_rate(
    rng: random.Random,
    hour_utc: datetime,
    offset: float,
    base: int,
) -> int:
    """Return a synthetic request count with a local-time demand curve."""
    local = (hour_utc.hour + offset) % 24
    evening = math.exp(-((min(abs(local - 20.5), 24 - abs(local - 20.5))) ** 2) / 9)
    midday = math.exp(-((min(abs(local - 11.0), 24 - abs(local - 11.0))) ** 2) / 18)
    weekend = 0.72 if hour_utc.weekday() >= 5 else 1.0
    rate = base * (0.18 + 0.85 * evening + 0.40 * midday) * weekend
    if rng.random() < 0.02:
        rate *= rng.uniform(2.5, 4.5)
    return max(0, round(rate * rng.lognormvariate(0, 0.3)))


def generate_demo(days: int) -> dict:
    """Generate clearly labeled, seeded synthetic globe data."""
    rng = random.Random(42)
    until = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    start = until - timedelta(days=days)
    hours_index = [start + timedelta(hours=index) for index in range(days * 24)]

    buckets: dict[tuple[datetime, str, str, str], GeoBucket] = defaultdict(GeoBucket)
    flows: dict[tuple[datetime, str, str, str], int] = defaultdict(int)
    providers_seen: set[str] = set()

    for hour in hours_index:
        for alpha2, continent, offset, base in _DEMO_COUNTRIES:
            alpha3 = ALPHA2_TO_ALPHA3[alpha2]
            count = _demo_hour_rate(rng, hour, offset, base)
            if count <= 0:
                continue
            bucket = buckets[(hour, alpha3, alpha2, continent)]
            bucket.n += count
            bucket.err += max(0, round(count * 0.012 * rng.lognormvariate(0, 0.5)))
            bucket.users.update(f"u{rng.randrange(3000)}" for _ in range(max(1, count // 22)))
            bucket.tin += int(count * rng.lognormvariate(7.4, 0.3))
            bucket.tout += int(count * rng.lognormvariate(5.8, 0.3))
            bucket.gs += count * rng.uniform(2.5, 9.0)
            bucket.ttfts.extend(
                int(500 * rng.lognormvariate(0, 0.45)) for _ in range(min(count, 40))
            )
            for provider, weight in _DEMO_PROVIDER_WEIGHTS[continent]:
                provider_count = round(count * weight * rng.uniform(0.8, 1.2))
                if provider_count > 0:
                    flows[(hour, alpha3, provider, provider)] += provider_count
                    providers_seen.add(provider)

    total = sum(bucket.n for bucket in buckets.values())
    meta = {
        "source": "synthetic-demo",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "start": hours_index[0].isoformat(),
        "hours": len(hours_index),
        "rows_total": total,
        "rows_with_ip": total,
        "geoip": {"country": False, "provider": None, "attribution": None},
        "degraded": False,
        "degraded_reasons": [],
        "unmapped_alpha2": [],
        "notes": ["SYNTHETIC DEMO DATA - diurnal patterns are hard-coded, not observed"],
    }
    return _finalize_geo_demand(hours_index, buckets, flows, providers_seen, meta)


def write_outputs(payload: dict, out_path: str, csv_path: str | None) -> None:
    """Write compact JSON and an optional flat hourly-bucket CSV."""
    with open(out_path, "w", encoding="utf-8") as output:
        json.dump(payload, output, separators=(",", ":"), ensure_ascii=False)
    size_mb = Path(out_path).stat().st_size / 1e6
    print(f"wrote {out_path} ({size_mb:.1f} MB, {payload['meta']['hours']} hours)")

    if csv_path:
        with open(csv_path, "w", newline="", encoding="utf-8") as output:
            writer = csv.writer(output)
            writer.writerow(["hour_utc", *BUCKET_COLS])
            for hour_iso, hour_data in zip(payload["hours_index"], payload["hours"], strict=True):
                for row in hour_data["b"]:
                    writer.writerow([hour_iso, *row])
        print(f"wrote {csv_path}")


def cli() -> None:
    """Parse arguments and run a live export or demo generation."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--days", type=int, default=14, help="Days of history (default: 14)")
    parser.add_argument("--since", default=None, help="ISO start (overrides --days)")
    parser.add_argument("--until", default=None, help="ISO end (default: now)")
    parser.add_argument("--out", default="data.json", help="Output JSON path")
    parser.add_argument("--csv", default=None, help="Optional flat CSV of hourly buckets")
    parser.add_argument(
        "--geoip-country",
        default=os.environ.get("GEOIP_COUNTRY_DB"),
        help="Path to a Country MMDB file (env: GEOIP_COUNTRY_DB)",
    )
    parser.add_argument(
        "--geoip-provider",
        default=os.environ.get("GEOIP_COUNTRY_PROVIDER"),
        help="Country data provider id, e.g. dbip-lite (env: GEOIP_COUNTRY_PROVIDER)",
    )
    parser.add_argument("--env-file", default=None, help="Path to .env with DB_* variables")
    parser.add_argument("--demo", action="store_true", help="Generate synthetic demo data")
    parser.add_argument("--demo-days", type=int, default=14, help="Demo range length")
    args = parser.parse_args()

    if args.demo:
        write_outputs(generate_demo(args.demo_days), args.out, args.csv)
        return

    _load_env(args.env_file)
    if not args.geoip_country:
        print("WARNING: no Country MMDB configured - all origins will be country '?'.")

    until = (
        datetime.fromisoformat(args.until).astimezone(timezone.utc)
        if args.until
        else datetime.now(timezone.utc)
    )
    since = (
        datetime.fromisoformat(args.since).astimezone(timezone.utc)
        if args.since
        else until - timedelta(days=args.days)
    )
    resolver = GeoResolver(args.geoip_country, country_provider=args.geoip_provider)
    try:
        payload = asyncio.run(export_real(since, until, resolver))
    finally:
        resolver.close()
    if payload["meta"]["unmapped_alpha2"]:
        print(f"NOTE: unmapped alpha-2 codes (not plotted): {payload['meta']['unmapped_alpha2']}")
    write_outputs(payload, args.out, args.csv)


if __name__ == "__main__":
    cli()
