"""Privacy-safe IP geolocation helpers for aggregate analytics."""

from __future__ import annotations

import ipaddress
import os
from collections import OrderedDict
from contextlib import suppress
from pathlib import Path
from typing import Any, Protocol

try:
    import maxminddb
except ImportError:  # pragma: no cover - dependency is present in production installs
    maxminddb = None


class GeoReader(Protocol):
    """Subset of the MMDB reader API used by :class:`GeoResolver`."""

    def get(self, ip: str) -> dict[str, Any] | None:
        """Return the database record for an IP."""

    def close(self) -> None:
        """Close the underlying database."""


# Country MMDB records use alpha-2 codes while the Natural Earth atlas uses
# alpha-3 identifiers. Unknown-but-valid codes deliberately become ``?XX``.
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

# External providers intentionally have no coordinates: their serving locations
# are not known. Only hand-maintained local sites may be drawn on the globe.
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


class GeoResolver:
    """Resolve network-origin country and continent with bounded caching."""

    def __init__(
        self,
        country_db: str | None = None,
        *,
        country_reader: GeoReader | None = None,
        country_provider: str | None = None,
        cache_size: int = 100_000,
    ) -> None:
        """Configure readers; database files are opened only on first use."""
        if cache_size < 1:
            raise ValueError("cache_size must be at least 1")
        self._country_path = country_db or os.environ.get("GEOIP_COUNTRY_DB")
        self._country = country_reader
        self._country_injected = country_reader is not None
        self._country_provider = (
            country_provider
            if self._country_injected
            else country_provider or os.environ.get("GEOIP_COUNTRY_PROVIDER")
        )
        self._readers_initialized = country_reader is not None
        self._cache_size = cache_size
        self._cache: OrderedDict[str, tuple[str, str, str]] = OrderedDict()
        self.unmapped_a2: set[str] = set()
        self._degraded_reasons: set[str] = set()

    def _open_reader(self, path: str | None) -> GeoReader | None:
        if not path:
            self._degraded_reasons.add("country_database_not_configured")
            return None
        if maxminddb is None:
            self._degraded_reasons.add("mmdb_reader_unavailable")
            return None
        if not Path(path).is_file():
            self._degraded_reasons.add("country_database_missing")
            return None
        try:
            return maxminddb.open_database(path)
        except Exception:  # MMDB readers raise backend-specific errors for invalid files.
            self._degraded_reasons.add("country_database_open_failed")
            return None

    def _ensure_readers(self) -> None:
        if self._readers_initialized:
            return
        if not self._country_injected:
            self._country = self._open_reader(self._country_path)
        self._readers_initialized = True

    @property
    def country_enabled(self) -> bool:
        """Whether the country reader is available."""
        self._ensure_readers()
        return self._country is not None

    @property
    def country_provider(self) -> str | None:
        """Configured data provider, only when country resolution is enabled."""
        return self._country_provider if self.country_enabled else None

    @property
    def country_attribution(self) -> dict[str, str] | None:
        """Required public attribution for the configured country database."""
        if self.country_provider == "dbip-lite":
            return {
                "label": "IP Geolocation by DB-IP",
                "url": "https://db-ip.com",
            }
        return None

    @property
    def degraded(self) -> bool:
        """Whether country resolution is unavailable or encountered an error."""
        self._ensure_readers()
        return bool(self._degraded_reasons)

    @property
    def degraded_reasons(self) -> tuple[str, ...]:
        """Stable reason codes explaining degraded resolution."""
        self._ensure_readers()
        return tuple(sorted(self._degraded_reasons))

    def resolve(self, ip: str | None) -> tuple[str, str, str]:
        """Return ``(alpha3, alpha2, continent)`` for an IP."""
        if not ip:
            return ("?", "?", "?")
        cached = self._cache.get(ip)
        if cached is not None:
            self._cache.move_to_end(ip)
            return cached

        result = self._resolve_uncached(ip)
        self._cache[ip] = result
        self._cache.move_to_end(ip)
        if len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)
        return result

    def _resolve_uncached(self, ip: str) -> tuple[str, str, str]:
        try:
            parsed = ipaddress.ip_address(ip)
        except ValueError:
            return ("?", "?", "?")
        if parsed.is_private or parsed.is_loopback or parsed.is_link_local:
            return ("?", "?", "?")

        self._ensure_readers()
        a2, continent = "?", "?"
        if self._country is not None:
            try:
                record = self._country.get(ip) or {}
            except Exception:  # A corrupt lookup must not break the admin page.
                self._degraded_reasons.add("country_lookup_failed")
                record = {}
            a2 = (record.get("country") or {}).get("iso_code") or "?"
            continent = (record.get("continent") or {}).get("code") or "?"

        alpha3 = ALPHA2_TO_ALPHA3.get(a2)
        if alpha3 is None:
            if a2 != "?":
                self.unmapped_a2.add(a2)
            alpha3 = f"?{a2}" if a2 != "?" else "?"
        return (alpha3, a2, continent)

    def close(self) -> None:
        """Close any readers opened or injected into this resolver."""
        if self._country is not None:
            with suppress(Exception):
                self._country.close()
        self._country = None
