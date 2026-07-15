"""Export hourly geo-temporal demand aggregates from ``api_logs`` for geo_globe.html.

Reads the live database (``.env`` / ``DB_*`` env vars, same conventions as
``ops/db/export_logs.py``), resolves each request's network origin from
``metadata->>'ip'`` with offline MaxMind GeoLite2 databases, and writes a
privacy-safe ``data.json``: hourly buckets of ``country x traffic-class`` and
hourly ``country x class x provider`` flows. No raw IPs, user ids, or prompts
ever leave the database — only counts, token sums, and latency percentiles.

Traffic classes (network origin, not identity — a VPN or office NAT can fool it):
  - ``nondc``    public IP not in a datacenter ASN (likely human / edu / office)
  - ``dc``       public IP in a hosting/cloud ASN (likely agent, CI, or server)
  - ``internal`` private / loopback address (probes, port-forwarded dev traffic)
  - ``unknown``  no IP recorded or GeoIP lookup unavailable

GeoIP databases are optional but strongly recommended: without them every
public IP degrades to country ``??`` / class ``unknown``. Get the free
GeoLite2-Country and GeoLite2-ASN ``.mmdb`` files from MaxMind and point
``--geoip-country`` / ``--geoip-asn`` (or ``GEOIP_COUNTRY_DB`` / ``GEOIP_ASN_DB``)
at them. Requires the ``maxminddb`` package (``uv pip install maxminddb``).

Typical runs:
  # on the server, last 30 days, next to the viewer
  uv run python ops/db/analysis/geo_hourly_export.py --days 30 \
      --geoip-country /srv/geoip/GeoLite2-Country.mmdb \
      --geoip-asn /srv/geoip/GeoLite2-ASN.mmdb \
      --out data.json --csv geo_hourly.csv

  # anywhere, no database needed: synthetic demo data for the viewer
  uv run python ops/db/analysis/geo_hourly_export.py --demo --out data.json

View the result with ``ops/db/analysis/geo_globe.html`` served from the same
directory as ``data.json`` (e.g. ``python3 -m http.server``).
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import ipaddress
import json
import math
import os
import random
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import asyncpg
import dotenv

try:  # Optional: only needed for real GeoIP resolution, not for --demo.
    import maxminddb
except ImportError:  # pragma: no cover - exercised on hosts without the package
    maxminddb = None

# --------------------------------------------------------------------------
# Static tables
# --------------------------------------------------------------------------

# ISO 3166-1 alpha-2 -> alpha-3. The viewer matches countries against a
# Natural Earth atlas keyed by alpha-3; MaxMind returns alpha-2. Codes missing
# here are passed through as "?XX" (aggregated correctly, just not plotted).
ALPHA2_TO_ALPHA3 = {
    "AD": "AND",
    "AE": "ARE",
    "AF": "AFG",
    "AG": "ATG",
    "AI": "AIA",
    "AL": "ALB",
    "AM": "ARM",
    "AO": "AGO",
    "AQ": "ATA",
    "AR": "ARG",
    "AS": "ASM",
    "AT": "AUT",
    "AU": "AUS",
    "AW": "ABW",
    "AX": "ALA",
    "AZ": "AZE",
    "BA": "BIH",
    "BB": "BRB",
    "BD": "BGD",
    "BE": "BEL",
    "BF": "BFA",
    "BG": "BGR",
    "BH": "BHR",
    "BI": "BDI",
    "BJ": "BEN",
    "BL": "BLM",
    "BM": "BMU",
    "BN": "BRN",
    "BO": "BOL",
    "BQ": "BES",
    "BR": "BRA",
    "BS": "BHS",
    "BT": "BTN",
    "BV": "BVT",
    "BW": "BWA",
    "BY": "BLR",
    "BZ": "BLZ",
    "CA": "CAN",
    "CC": "CCK",
    "CD": "COD",
    "CF": "CAF",
    "CG": "COG",
    "CH": "CHE",
    "CI": "CIV",
    "CK": "COK",
    "CL": "CHL",
    "CM": "CMR",
    "CN": "CHN",
    "CO": "COL",
    "CR": "CRI",
    "CU": "CUB",
    "CV": "CPV",
    "CW": "CUW",
    "CX": "CXR",
    "CY": "CYP",
    "CZ": "CZE",
    "DE": "DEU",
    "DJ": "DJI",
    "DK": "DNK",
    "DM": "DMA",
    "DO": "DOM",
    "DZ": "DZA",
    "EC": "ECU",
    "EE": "EST",
    "EG": "EGY",
    "EH": "ESH",
    "ER": "ERI",
    "ES": "ESP",
    "ET": "ETH",
    "FI": "FIN",
    "FJ": "FJI",
    "FK": "FLK",
    "FM": "FSM",
    "FO": "FRO",
    "FR": "FRA",
    "GA": "GAB",
    "GB": "GBR",
    "GD": "GRD",
    "GE": "GEO",
    "GF": "GUF",
    "GG": "GGY",
    "GH": "GHA",
    "GI": "GIB",
    "GL": "GRL",
    "GM": "GMB",
    "GN": "GIN",
    "GP": "GLP",
    "GQ": "GNQ",
    "GR": "GRC",
    "GS": "SGS",
    "GT": "GTM",
    "GU": "GUM",
    "GW": "GNB",
    "GY": "GUY",
    "HK": "HKG",
    "HM": "HMD",
    "HN": "HND",
    "HR": "HRV",
    "HT": "HTI",
    "HU": "HUN",
    "ID": "IDN",
    "IE": "IRL",
    "IL": "ISR",
    "IM": "IMN",
    "IN": "IND",
    "IO": "IOT",
    "IQ": "IRQ",
    "IR": "IRN",
    "IS": "ISL",
    "IT": "ITA",
    "JE": "JEY",
    "JM": "JAM",
    "JO": "JOR",
    "JP": "JPN",
    "KE": "KEN",
    "KG": "KGZ",
    "KH": "KHM",
    "KI": "KIR",
    "KM": "COM",
    "KN": "KNA",
    "KP": "PRK",
    "KR": "KOR",
    "KW": "KWT",
    "KY": "CYM",
    "KZ": "KAZ",
    "LA": "LAO",
    "LB": "LBN",
    "LC": "LCA",
    "LI": "LIE",
    "LK": "LKA",
    "LR": "LBR",
    "LS": "LSO",
    "LT": "LTU",
    "LU": "LUX",
    "LV": "LVA",
    "LY": "LBY",
    "MA": "MAR",
    "MC": "MCO",
    "MD": "MDA",
    "ME": "MNE",
    "MF": "MAF",
    "MG": "MDG",
    "MH": "MHL",
    "MK": "MKD",
    "ML": "MLI",
    "MM": "MMR",
    "MN": "MNG",
    "MO": "MAC",
    "MP": "MNP",
    "MQ": "MTQ",
    "MR": "MRT",
    "MS": "MSR",
    "MT": "MLT",
    "MU": "MUS",
    "MV": "MDV",
    "MW": "MWI",
    "MX": "MEX",
    "MY": "MYS",
    "MZ": "MOZ",
    "NA": "NAM",
    "NC": "NCL",
    "NE": "NER",
    "NF": "NFK",
    "NG": "NGA",
    "NI": "NIC",
    "NL": "NLD",
    "NO": "NOR",
    "NP": "NPL",
    "NR": "NRU",
    "NU": "NIU",
    "NZ": "NZL",
    "OM": "OMN",
    "PA": "PAN",
    "PE": "PER",
    "PF": "PYF",
    "PG": "PNG",
    "PH": "PHL",
    "PK": "PAK",
    "PL": "POL",
    "PM": "SPM",
    "PN": "PCN",
    "PR": "PRI",
    "PS": "PSE",
    "PT": "PRT",
    "PW": "PLW",
    "PY": "PRY",
    "QA": "QAT",
    "RE": "REU",
    "RO": "ROU",
    "RS": "SRB",
    "RU": "RUS",
    "RW": "RWA",
    "SA": "SAU",
    "SB": "SLB",
    "SC": "SYC",
    "SD": "SDN",
    "SE": "SWE",
    "SG": "SGP",
    "SH": "SHN",
    "SI": "SVN",
    "SJ": "SJM",
    "SK": "SVK",
    "SL": "SLE",
    "SM": "SMR",
    "SN": "SEN",
    "SO": "SOM",
    "SR": "SUR",
    "SS": "SSD",
    "ST": "STP",
    "SV": "SLV",
    "SX": "SXM",
    "SY": "SYR",
    "SZ": "SWZ",
    "TC": "TCA",
    "TD": "TCD",
    "TF": "ATF",
    "TG": "TGO",
    "TH": "THA",
    "TJ": "TJK",
    "TK": "TKL",
    "TL": "TLS",
    "TM": "TKM",
    "TN": "TUN",
    "TO": "TON",
    "TR": "TUR",
    "TT": "TTO",
    "TV": "TUV",
    "TW": "TWN",
    "TZ": "TZA",
    "UA": "UKR",
    "UG": "UGA",
    "UM": "UMI",
    "US": "USA",
    "UY": "URY",
    "UZ": "UZB",
    "VA": "VAT",
    "VC": "VCT",
    "VE": "VEN",
    "VG": "VGB",
    "VI": "VIR",
    "VN": "VNM",
    "VU": "VUT",
    "WF": "WLF",
    "WS": "WSM",
    "XK": "XKX",
    "YE": "YEM",
    "YT": "MYT",
    "ZA": "ZAF",
    "ZM": "ZMB",
    "ZW": "ZWE",
}

# Case-insensitive substrings of ASN organization names that indicate
# hosting / cloud / CDN origin. Deliberately editable — extend as new
# agent-hosting ASNs show up in the logs.
DC_ASN_KEYWORDS = [
    "amazon",
    "aws",
    "google",
    "gcp",
    "microsoft",
    "azure",
    "oracle",
    "alibaba",
    "aliyun",
    "tencent",
    "huawei",
    "baidu",
    "bytedance",
    "volcengine",
    "ucloud",
    "kingsoft",
    "qiniu",
    "digitalocean",
    "hetzner",
    "ovh",
    "linode",
    "akamai",
    "vultr",
    "choopa",
    "constant company",
    "contabo",
    "leaseweb",
    "scaleway",
    "online s.a.s",
    "upcloud",
    "netcup",
    "ionos",
    "gcore",
    "g-core",
    "cloudflare",
    "fastly",
    "m247",
    "datacamp",
    "packethub",
    "hostinger",
    "namecheap",
    "godaddy",
    "dreamhost",
    "rackspace",
    "softlayer",
    "ibm",
    "salesforce",
    "zenlayer",
    "cdn77",
    "stackpath",
    "kamatera",
    "hostwinds",
    "colocrossing",
    "quadranet",
    "psychz",
    "hivelocity",
    "equinix",
    "latitude.sh",
    "fly.io",
    "render",
    "railway",
    "heroku",
    "vercel",
    "netlify",
    "hosting",
    "datacenter",
    "data center",
    "dedicated server",
    "vps",
    "colocation",
    "cloud",
]

# Where each provider's compute actually sits. ``local`` providers get a node
# + inbound arcs on the globe; ``remote_api`` providers are listed in a side
# rail without a location (we do NOT know where an API vendor's GPUs are, so
# we refuse to draw them on the map). coord is [lon, lat].
# EDIT ME: adjust local cluster coords/regions to the real deployments.
PROVIDER_SITES = {
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

CLASSES = ["nondc", "dc", "internal", "unknown"]

BUCKET_COLS = ["c", "cc", "cont", "cls", "n", "err", "users", "tin", "tout", "gs", "p50", "p90"]
FLOW_COLS = ["c", "cls", "p", "n"]

ROWS_QUERY = """
SELECT
  date_trunc('hour', timestamp) AS hour,
  metadata->>'ip'               AS ip,
  provider,
  prompt_tokens,
  completion_tokens,
  latency_ms,
  ttft_ms,
  (error IS NOT NULL OR COALESCE(status_code, 200) >= 400) AS is_err,
  user_id
FROM api_logs
WHERE timestamp >= $1 AND timestamp < $2
"""


# --------------------------------------------------------------------------
# Environment / connection (mirrors ops/db/export_logs.py)
# --------------------------------------------------------------------------


def _load_env(env_path: str | None = None) -> None:
    """Load the first available .env file, matching export_logs.py conventions."""
    candidates = [
        env_path,
        os.environ.get("ENV_FILE"),
        "/srv/hybridInference/.env",
        str(Path(__file__).resolve().parents[3] / ".env"),
    ]
    for p in candidates:
        if p and Path(p).is_file():
            dotenv.load_dotenv(p, override=False)
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


# --------------------------------------------------------------------------
# Geo resolution
# --------------------------------------------------------------------------


class GeoResolver:
    """Resolve an IP string to (alpha3, alpha2, continent, traffic class), cached."""

    def __init__(self, country_db: str | None, asn_db: str | None) -> None:
        """Open the optional GeoLite2 country/ASN readers and an empty cache."""
        self._cache: dict[str, tuple[str, str, str, str]] = {}
        self.unmapped_a2: set[str] = set()
        self._country = None
        self._asn = None
        if country_db and maxminddb is not None:
            self._country = maxminddb.open_database(country_db)
        if asn_db and maxminddb is not None:
            self._asn = maxminddb.open_database(asn_db)

    @property
    def country_enabled(self) -> bool:
        """Whether a country database is open."""
        return self._country is not None

    @property
    def asn_enabled(self) -> bool:
        """Whether an ASN database is open."""
        return self._asn is not None

    def resolve(self, ip: str | None) -> tuple[str, str, str, str]:
        """Return ``(alpha3, alpha2, continent_code, cls)`` for one IP string."""
        if not ip:
            return ("?", "?", "?", "unknown")
        hit = self._cache.get(ip)
        if hit is not None:
            return hit
        result = self._resolve_uncached(ip)
        self._cache[ip] = result
        return result

    def _resolve_uncached(self, ip: str) -> tuple[str, str, str, str]:
        try:
            parsed = ipaddress.ip_address(ip)
        except ValueError:
            return ("?", "?", "?", "unknown")
        if parsed.is_private or parsed.is_loopback or parsed.is_link_local:
            return ("?", "?", "?", "internal")

        a2, cont = "?", "?"
        if self._country is not None:
            try:
                rec = self._country.get(ip) or {}
            except ValueError:
                rec = {}
            a2 = (rec.get("country") or {}).get("iso_code") or "?"
            cont = (rec.get("continent") or {}).get("code") or "?"

        cls = "unknown"
        if self._asn is not None:
            try:
                asn_rec = self._asn.get(ip) or {}
            except ValueError:
                asn_rec = {}
            org = (asn_rec.get("autonomous_system_organization") or "").lower()
            if org:
                cls = "dc" if any(k in org for k in DC_ASN_KEYWORDS) else "nondc"

        a3 = ALPHA2_TO_ALPHA3.get(a2)
        if a3 is None:
            if a2 != "?":
                self.unmapped_a2.add(a2)
            a3 = f"?{a2}" if a2 != "?" else "?"
        return (a3, a2, cont, cls)


# --------------------------------------------------------------------------
# Aggregation
# --------------------------------------------------------------------------


class _Bucket:
    """Mutable accumulator for one (hour, country, class) cell."""

    __slots__ = ("err", "gs", "n", "tin", "tout", "ttfts", "users")

    def __init__(self) -> None:
        self.n = 0
        self.err = 0
        self.users: set[str] = set()
        self.tin = 0
        self.tout = 0
        self.gs = 0.0
        self.ttfts: list[int] = []


def _percentile(sorted_values: list[int], q: float) -> int | None:
    """Nearest-rank percentile of a pre-sorted list (None when empty)."""
    if not sorted_values:
        return None
    idx = max(0, math.ceil(q * len(sorted_values)) - 1)
    return sorted_values[idx]


def _finalize(
    hours_index: list[datetime],
    buckets: dict[tuple[datetime, str, str, str, str], _Bucket],
    flows: dict[tuple[datetime, str, str, str], int],
    providers_seen: set[str],
    meta: dict,
) -> dict:
    """Assemble the columnar data.json payload from the accumulators."""
    hour_pos = {h: i for i, h in enumerate(hours_index)}
    hours_out: list[dict] = [{"b": [], "f": []} for _ in hours_index]

    for (hour, a3, a2, cont, cls), b in sorted(
        buckets.items(), key=lambda kv: (kv[0][0], -kv[1].n)
    ):
        b.ttfts.sort()
        hours_out[hour_pos[hour]]["b"].append(
            [
                a3,
                a2,
                cont,
                cls,
                b.n,
                b.err,
                len(b.users),
                b.tin,
                b.tout,
                round(b.gs, 1),
                _percentile(b.ttfts, 0.5),
                _percentile(b.ttfts, 0.9),
            ]
        )

    for (hour, a3, cls, provider), n in sorted(flows.items(), key=lambda kv: (kv[0][0], -kv[1])):
        hours_out[hour_pos[hour]]["f"].append([a3, cls, provider, n])

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
        "classes": CLASSES,
        "bucket_cols": BUCKET_COLS,
        "flow_cols": FLOW_COLS,
        "providers": providers_out,
        "hours_index": [h.isoformat() for h in hours_index],
        "hours": hours_out,
    }


async def export_real(
    since: datetime,
    until: datetime,
    resolver: GeoResolver,
) -> dict:
    """Stream api_logs rows and aggregate them into the data.json payload."""
    buckets: dict[tuple[datetime, str, str, str, str], _Bucket] = defaultdict(_Bucket)
    flows: dict[tuple[datetime, str, str, str], int] = defaultdict(int)
    providers_seen: set[str] = set()
    total = 0
    with_ip = 0

    pool = await asyncpg.create_pool(_dsn(), min_size=1, max_size=2)
    try:
        async with pool.acquire() as conn, conn.transaction():
            async for row in conn.cursor(ROWS_QUERY, since, until, prefetch=5000):
                total += 1
                ip = row["ip"]
                if ip:
                    with_ip += 1
                a3, a2, cont, cls = resolver.resolve(ip)
                provider = row["provider"] or "unknown"
                providers_seen.add(provider)

                b = buckets[(row["hour"], a3, a2, cont, cls)]
                b.n += 1
                if row["is_err"]:
                    b.err += 1
                if row["user_id"]:
                    b.users.add(row["user_id"])
                b.tin += row["prompt_tokens"] or 0
                b.tout += row["completion_tokens"] or 0
                b.gs += (row["latency_ms"] or 0) / 1000.0
                if row["ttft_ms"] is not None:
                    b.ttfts.append(row["ttft_ms"])

                flows[(row["hour"], a3, cls, provider)] += 1
                if total % 200_000 == 0:
                    print(f"  ... {total:,} rows", flush=True)
    finally:
        await pool.close()

    start = since.replace(minute=0, second=0, microsecond=0)
    hours_index = []
    h = start
    while h < until:
        hours_index.append(h)
        h += timedelta(hours=1)

    meta = {
        "source": "api_logs",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "start": hours_index[0].isoformat() if hours_index else None,
        "hours": len(hours_index),
        "rows_total": total,
        "rows_with_ip": with_ip,
        "geoip": {"country": resolver.country_enabled, "asn": resolver.asn_enabled},
        "unmapped_alpha2": sorted(resolver.unmapped_a2),
        "notes": [
            "origin = network origin (IP-based), not user residence",
            "gs = sum of server-side latency seconds (compute-time estimate)",
            "classes: nondc / dc are ASN-based heuristics",
        ],
    }
    print(f"aggregated {total:,} rows ({with_ip:,} with IP) into {len(hours_index)} hours")
    return _finalize(hours_index, buckets, flows, providers_seen, meta)


# --------------------------------------------------------------------------
# Demo data (no database, no GeoIP needed) — for developing the viewer
# --------------------------------------------------------------------------

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


def _demo_hour_rate(rng: random.Random, hour_utc: datetime, offset: float, nondc: int, dc: int):
    """Return (nondc_n, dc_n) synthetic request counts for one country-hour."""
    local = (hour_utc.hour + offset) % 24
    evening = math.exp(-((min(abs(local - 20.5), 24 - abs(local - 20.5))) ** 2) / 9)
    midday = math.exp(-((min(abs(local - 11.0), 24 - abs(local - 11.0))) ** 2) / 18)
    weekend = 0.72 if hour_utc.weekday() >= 5 else 1.0
    nondc_rate = nondc * (0.12 + 0.85 * evening + 0.40 * midday) * weekend
    dc_rate = dc * (0.80 + 0.20 * math.sin((hour_utc.hour + offset) * math.pi / 12))
    if rng.random() < 0.02:  # occasional agent burst
        dc_rate *= rng.uniform(2.5, 4.5)
    noise = rng.lognormvariate(0, 0.25)
    return max(0, round(nondc_rate * noise)), max(0, round(dc_rate * rng.lognormvariate(0, 0.35)))


def generate_demo(days: int) -> dict:
    """Generate a clearly-labeled synthetic data.json payload (seeded, reproducible)."""
    rng = random.Random(42)
    until = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    start = until - timedelta(days=days)
    hours_index = [start + timedelta(hours=i) for i in range(days * 24)]

    buckets: dict[tuple[datetime, str, str, str, str], _Bucket] = defaultdict(_Bucket)
    flows: dict[tuple[datetime, str, str, str], int] = defaultdict(int)
    providers_seen: set[str] = set()

    for h in hours_index:
        for a2, cont, offset, nondc_base, dc_base in _DEMO_COUNTRIES:
            a3 = ALPHA2_TO_ALPHA3[a2]
            for cls, n in zip(
                ("nondc", "dc"), _demo_hour_rate(rng, h, offset, nondc_base, dc_base), strict=True
            ):
                if n <= 0:
                    continue
                b = buckets[(h, a3, a2, cont, cls)]
                b.n += n
                b.err += max(0, round(n * 0.012 * rng.lognormvariate(0, 0.5)))
                b.users.update(f"u{rng.randrange(3000)}" for _ in range(max(1, n // 22)))
                b.tin += int(n * rng.lognormvariate(7.4, 0.3))
                b.tout += int(n * rng.lognormvariate(5.8, 0.3))
                b.gs += n * rng.uniform(2.5, 9.0)
                base_ttft = 550 if cls == "nondc" else 420
                b.ttfts.extend(
                    int(base_ttft * rng.lognormvariate(0, 0.45)) for _ in range(min(n, 40))
                )
                for provider, w in _DEMO_PROVIDER_WEIGHTS[cont]:
                    pn = round(n * w * rng.uniform(0.8, 1.2))
                    if pn > 0:
                        flows[(h, a3, cls, provider)] += pn
                        providers_seen.add(provider)

    meta = {
        "source": "synthetic-demo",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "start": hours_index[0].isoformat(),
        "hours": len(hours_index),
        "rows_total": sum(b.n for b in buckets.values()),
        "rows_with_ip": sum(b.n for b in buckets.values()),
        "geoip": {"country": False, "asn": False},
        "unmapped_alpha2": [],
        "notes": ["SYNTHETIC DEMO DATA - diurnal patterns are hard-coded, not observed"],
    }
    return _finalize(hours_index, buckets, flows, providers_seen, meta)


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------


def write_outputs(payload: dict, out_path: str, csv_path: str | None) -> None:
    """Write data.json (compact) and the optional flat CSV of hourly buckets."""
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, separators=(",", ":"), ensure_ascii=False)
    size_mb = Path(out_path).stat().st_size / 1e6
    print(f"wrote {out_path} ({size_mb:.1f} MB, {payload['meta']['hours']} hours)")

    if csv_path:
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["hour_utc", *BUCKET_COLS])
            for hour_iso, hour_data in zip(payload["hours_index"], payload["hours"], strict=True):
                for row in hour_data["b"]:
                    writer.writerow([hour_iso, *row])
        print(f"wrote {csv_path}")


def cli() -> None:
    """Parse arguments and run the export (or demo generation)."""
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
    if (args.geoip_country or args.geoip_asn) and maxminddb is None:
        raise SystemExit("ERROR: GeoIP paths given but `maxminddb` is not installed.")
    if not args.geoip_country:
        print("WARNING: no GeoLite2-Country.mmdb — all origins will be country '?'.")
    if not args.geoip_asn:
        print("WARNING: no GeoLite2-ASN.mmdb — traffic classes will be 'unknown'.")

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
    payload = asyncio.run(export_real(since, until, resolver))
    if payload["meta"]["unmapped_alpha2"]:
        print(f"NOTE: unmapped alpha-2 codes (not plotted): {payload['meta']['unmapped_alpha2']}")
    write_outputs(payload, args.out, args.csv)


if __name__ == "__main__":
    cli()
