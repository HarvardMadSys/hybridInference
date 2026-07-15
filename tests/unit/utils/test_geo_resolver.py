"""Tests for bounded, failure-safe GeoIP resolution."""

from __future__ import annotations

from typing import Any

from serving.utils import geo_resolver
from serving.utils.geo_resolver import GeoResolver


class FakeReader:
    def __init__(self, records: dict[str, dict[str, Any]]) -> None:
        self.records = records
        self.calls: list[str] = []
        self.closed = False

    def get(self, ip: str) -> dict[str, Any] | None:
        self.calls.append(ip)
        return self.records.get(ip)

    def close(self) -> None:
        self.closed = True


def test_resolves_country_and_continent() -> None:
    country = FakeReader(
        {
            "8.8.8.8": {"country": {"iso_code": "US"}, "continent": {"code": "NA"}},
            "1.1.1.1": {"country": {"iso_code": "AU"}, "continent": {"code": "OC"}},
        }
    )
    resolver = GeoResolver(country_reader=country)

    assert resolver.resolve("8.8.8.8") == ("USA", "US", "NA")
    assert resolver.resolve("1.1.1.1") == ("AUS", "AU", "OC")
    assert resolver.country_enabled is True
    assert resolver.degraded is False


def test_internal_invalid_and_missing_ips_do_not_hit_readers() -> None:
    country = FakeReader({})
    resolver = GeoResolver(country_reader=country)

    assert resolver.resolve("127.0.0.1") == ("?", "?", "?")
    assert resolver.resolve("10.0.0.4") == ("?", "?", "?")
    assert resolver.resolve("169.254.1.2") == ("?", "?", "?")
    assert resolver.resolve("not-an-ip") == ("?", "?", "?")
    assert resolver.resolve(None) == ("?", "?", "?")
    assert country.calls == []


def test_cache_hit_and_bounded_eviction() -> None:
    records = {
        "8.8.8.8": {"country": {"iso_code": "US"}, "continent": {"code": "NA"}},
        "1.1.1.1": {"country": {"iso_code": "AU"}, "continent": {"code": "OC"}},
    }
    country = FakeReader(records)
    resolver = GeoResolver(country_reader=country, cache_size=1)

    resolver.resolve("8.8.8.8")
    resolver.resolve("8.8.8.8")
    assert country.calls == ["8.8.8.8"]

    resolver.resolve("1.1.1.1")
    resolver.resolve("8.8.8.8")
    assert country.calls == ["8.8.8.8", "1.1.1.1", "8.8.8.8"]


def test_unmapped_alpha2_is_visible_without_losing_bucket() -> None:
    country = FakeReader({"8.8.8.8": {"country": {"iso_code": "ZZ"}, "continent": {"code": "NA"}}})
    resolver = GeoResolver(country_reader=country)

    assert resolver.resolve("8.8.8.8") == ("?ZZ", "ZZ", "NA")
    assert resolver.unmapped_a2 == {"ZZ"}


def test_missing_database_files_degrade_safely(tmp_path) -> None:
    resolver = GeoResolver(country_db=str(tmp_path / "missing-country.mmdb"))

    assert resolver.resolve("8.8.8.8") == ("?", "?", "?")
    assert resolver.country_enabled is False
    assert resolver.degraded is True
    assert resolver.degraded_reasons == ("country_database_missing",)


def test_missing_module_and_reader_open_failure_degrade_safely(tmp_path, monkeypatch) -> None:
    database = tmp_path / "country.mmdb"
    database.write_bytes(b"not-a-real-database")

    monkeypatch.setattr(geo_resolver, "maxminddb", None)
    missing_module = GeoResolver(country_db=str(database))
    assert missing_module.country_enabled is False
    assert missing_module.degraded_reasons == ("mmdb_reader_unavailable",)

    class BrokenMMDBModule:
        @staticmethod
        def open_database(path):
            raise RuntimeError("corrupt database")

    monkeypatch.setattr(geo_resolver, "maxminddb", BrokenMMDBModule())
    broken_database = GeoResolver(country_db=str(database))
    assert broken_database.country_enabled is False
    assert broken_database.degraded_reasons == ("country_database_open_failed",)


def test_close_closes_injected_reader() -> None:
    reader = FakeReader({})
    resolver = GeoResolver(country_reader=reader)

    resolver.close()

    assert reader.closed is True


def test_provider_attribution_is_explicit_and_only_for_enabled_dbip(monkeypatch) -> None:
    monkeypatch.setenv("GEOIP_COUNTRY_PROVIDER", "dbip-lite")
    unattributed = GeoResolver(country_reader=FakeReader({}))
    assert unattributed.country_provider is None
    assert unattributed.country_attribution is None

    dbip = GeoResolver(country_reader=FakeReader({}), country_provider="dbip-lite")
    assert dbip.country_provider == "dbip-lite"
    assert dbip.country_attribution == {
        "label": "IP Geolocation by DB-IP",
        "url": "https://db-ip.com",
    }

    unavailable = GeoResolver(
        country_db="/does/not/exist.mmdb",
        country_provider="dbip-lite",
    )
    assert unavailable.country_provider is None
    assert unavailable.country_attribution is None
