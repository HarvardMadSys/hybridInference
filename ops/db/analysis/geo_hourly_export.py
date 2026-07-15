"""Export hourly geo-temporal demand aggregates for ``geo_globe.html``.

The real-data path streams ``api_logs`` and resolves network-origin IPs with
offline GeoLite2 databases. The output contains aggregates only: no IPs, user
ids, or prompts leave the database. ``dc`` and ``nondc`` are ASN network-origin
heuristics; they do not identify a human or an agent.

Typical runs::

  uv run python ops/db/analysis/geo_hourly_export.py --days 30 \
      --geoip-country /srv/geoip/GeoLite2-Country.mmdb \
      --geoip-asn /srv/geoip/GeoLite2-ASN.mmdb --out data.json
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

import asyncpg
import dotenv

from serving.analytics.geo_demand import (
    BUCKET_COLS,
    GeoBucket,
    aggregate_geo_demand,
    finalize_geo_demand,
)
from serving.utils.geo_resolver import ALPHA2_TO_ALPHA3, GeoResolver


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
    """Stream live rows through the shared serving aggregation implementation."""
    pool = await asyncpg.create_pool(_dsn(), min_size=1, max_size=2)
    try:
        async with pool.acquire() as connection:
            payload = await aggregate_geo_demand(connection, since, until, resolver)
    finally:
        await pool.close()
    meta = payload["meta"]
    print(
        f"aggregated {meta['rows_total']:,} rows ({meta['rows_with_ip']:,} with IP) "
        f"into {meta['hours']} hours"
    )
    return payload


# (alpha2, continent, utc_offset_hours, nondc_base, dc_base)
_DEMO_COUNTRIES = [
    ("CN", "AS", 8, 300, 45),
    ("US", "NA", -5, 220, 260),
    ("SG", "AS", 8, 40, 95),
    ("DE", "EU", 1, 95, 55),
    ("GB", "EU", 0, 70, 30),
    ("JP", "AS", 9, 80, 20),
    ("IN", "AS", 5.5, 60, 15),
    ("KR", "AS", 9, 45, 10),
    ("HK", "AS", 8, 30, 25),
    ("FR", "EU", 1, 40, 15),
    ("NL", "EU", 1, 25, 45),
    ("RU", "EU", 3, 25, 10),
    ("CA", "NA", -5, 40, 12),
    ("BR", "SA", -3, 35, 8),
    ("AU", "OC", 10, 30, 8),
    ("AE", "AS", 4, 15, 6),
    ("TW", "AS", 8, 25, 6),
    ("VN", "AS", 7, 18, 4),
    ("NG", "AF", 1, 12, 2),
    ("ZA", "AF", 2, 10, 3),
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
    nondc: int,
    dc: int,
) -> tuple[int, int]:
    """Return synthetic non-datacenter and datacenter request counts."""
    local = (hour_utc.hour + offset) % 24
    evening = math.exp(-((min(abs(local - 20.5), 24 - abs(local - 20.5))) ** 2) / 9)
    midday = math.exp(-((min(abs(local - 11.0), 24 - abs(local - 11.0))) ** 2) / 18)
    weekend = 0.72 if hour_utc.weekday() >= 5 else 1.0
    nondc_rate = nondc * (0.12 + 0.85 * evening + 0.40 * midday) * weekend
    dc_rate = dc * (0.80 + 0.20 * math.sin((hour_utc.hour + offset) * math.pi / 12))
    if rng.random() < 0.02:
        dc_rate *= rng.uniform(2.5, 4.5)
    noise = rng.lognormvariate(0, 0.25)
    return (
        max(0, round(nondc_rate * noise)),
        max(0, round(dc_rate * rng.lognormvariate(0, 0.35))),
    )


def generate_demo(days: int) -> dict:
    """Generate clearly labeled, seeded synthetic globe data."""
    rng = random.Random(42)
    until = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    start = until - timedelta(days=days)
    hours_index = [start + timedelta(hours=index) for index in range(days * 24)]

    buckets: dict[tuple[datetime, str, str, str, str], GeoBucket] = defaultdict(GeoBucket)
    flows: dict[tuple[datetime, str, str, str, str], int] = defaultdict(int)
    providers_seen: set[str] = set()

    for hour in hours_index:
        for alpha2, continent, offset, nondc_base, dc_base in _DEMO_COUNTRIES:
            alpha3 = ALPHA2_TO_ALPHA3[alpha2]
            for network_class, count in zip(
                ("nondc", "dc"),
                _demo_hour_rate(rng, hour, offset, nondc_base, dc_base),
                strict=True,
            ):
                if count <= 0:
                    continue
                bucket = buckets[(hour, alpha3, alpha2, continent, network_class)]
                bucket.n += count
                bucket.err += max(0, round(count * 0.012 * rng.lognormvariate(0, 0.5)))
                bucket.users.update(f"u{rng.randrange(3000)}" for _ in range(max(1, count // 22)))
                bucket.tin += int(count * rng.lognormvariate(7.4, 0.3))
                bucket.tout += int(count * rng.lognormvariate(5.8, 0.3))
                bucket.gs += count * rng.uniform(2.5, 9.0)
                base_ttft = 550 if network_class == "nondc" else 420
                bucket.ttfts.extend(
                    int(base_ttft * rng.lognormvariate(0, 0.45)) for _ in range(min(count, 40))
                )
                for provider, weight in _DEMO_PROVIDER_WEIGHTS[continent]:
                    provider_count = round(count * weight * rng.uniform(0.8, 1.2))
                    if provider_count > 0:
                        flows[(hour, alpha3, network_class, provider, provider)] += provider_count
                        providers_seen.add(provider)

    total = sum(bucket.n for bucket in buckets.values())
    meta = {
        "source": "synthetic-demo",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "start": hours_index[0].isoformat(),
        "hours": len(hours_index),
        "rows_total": total,
        "rows_with_ip": total,
        "geoip": {"country": False, "asn": False},
        "degraded": False,
        "degraded_reasons": [],
        "unmapped_alpha2": [],
        "notes": ["SYNTHETIC DEMO DATA - diurnal patterns are hard-coded, not observed"],
    }
    return finalize_geo_demand(hours_index, buckets, flows, providers_seen, meta)


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
        help="Path to GeoLite2-Country.mmdb (env: GEOIP_COUNTRY_DB)",
    )
    parser.add_argument(
        "--geoip-asn",
        default=os.environ.get("GEOIP_ASN_DB"),
        help="Path to GeoLite2-ASN.mmdb (env: GEOIP_ASN_DB)",
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
        print("WARNING: no GeoLite2-Country.mmdb - all origins will be country '?'.")
    if not args.geoip_asn:
        print("WARNING: no GeoLite2-ASN.mmdb - traffic classes will be 'unknown'.")

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
    resolver = GeoResolver(args.geoip_country, args.geoip_asn)
    try:
        payload = asyncio.run(export_real(since, until, resolver))
    finally:
        resolver.close()
    if payload["meta"]["unmapped_alpha2"]:
        print(f"NOTE: unmapped alpha-2 codes (not plotted): {payload['meta']['unmapped_alpha2']}")
    write_outputs(payload, args.out, args.csv)


if __name__ == "__main__":
    cli()
