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


def test_resolves_country_continent_and_asn_network_class() -> None:
    country = FakeReader(
        {
            "8.8.8.8": {"country": {"iso_code": "US"}, "continent": {"code": "NA"}},
            "1.1.1.1": {"country": {"iso_code": "AU"}, "continent": {"code": "OC"}},
        }
    )
    asn = FakeReader(
        {
            "8.8.8.8": {"autonomous_system_organization": "Google LLC"},
            "1.1.1.1": {"autonomous_system_organization": "Example University"},
        }
    )
    resolver = GeoResolver(country_reader=country, asn_reader=asn)

    assert resolver.resolve("8.8.8.8") == ("USA", "US", "NA", "dc")
    assert resolver.resolve("1.1.1.1") == ("AUS", "AU", "OC", "nondc")
    assert resolver.country_enabled is True
    assert resolver.asn_enabled is True
    assert resolver.degraded is False


def test_internal_invalid_and_missing_ips_do_not_hit_readers() -> None:
    country = FakeReader({})
    asn = FakeReader({})
    resolver = GeoResolver(country_reader=country, asn_reader=asn)

    assert resolver.resolve("127.0.0.1") == ("?", "?", "?", "internal")
    assert resolver.resolve("10.0.0.4") == ("?", "?", "?", "internal")
    assert resolver.resolve("169.254.1.2") == ("?", "?", "?", "internal")
    assert resolver.resolve("not-an-ip") == ("?", "?", "?", "unknown")
    assert resolver.resolve(None) == ("?", "?", "?", "unknown")
    assert country.calls == []
    assert asn.calls == []


def test_cache_hit_and_bounded_eviction() -> None:
    records = {
        "8.8.8.8": {"country": {"iso_code": "US"}, "continent": {"code": "NA"}},
        "1.1.1.1": {"country": {"iso_code": "AU"}, "continent": {"code": "OC"}},
    }
    country = FakeReader(records)
    asn = FakeReader({ip: {"autonomous_system_organization": "ISP"} for ip in records})
    resolver = GeoResolver(country_reader=country, asn_reader=asn, cache_size=1)

    resolver.resolve("8.8.8.8")
    resolver.resolve("8.8.8.8")
    assert country.calls == ["8.8.8.8"]

    resolver.resolve("1.1.1.1")
    resolver.resolve("8.8.8.8")
    assert country.calls == ["8.8.8.8", "1.1.1.1", "8.8.8.8"]


def test_unmapped_alpha2_is_visible_without_losing_bucket() -> None:
    country = FakeReader({"8.8.8.8": {"country": {"iso_code": "ZZ"}, "continent": {"code": "NA"}}})
    asn = FakeReader({"8.8.8.8": {"autonomous_system_organization": "Example ISP"}})
    resolver = GeoResolver(country_reader=country, asn_reader=asn)

    assert resolver.resolve("8.8.8.8") == ("?ZZ", "ZZ", "NA", "nondc")
    assert resolver.unmapped_a2 == {"ZZ"}


def test_missing_database_files_degrade_safely(tmp_path) -> None:
    resolver = GeoResolver(
        country_db=str(tmp_path / "missing-country.mmdb"),
        asn_db=str(tmp_path / "missing-asn.mmdb"),
    )

    assert resolver.resolve("8.8.8.8") == ("?", "?", "?", "unknown")
    assert resolver.country_enabled is False
    assert resolver.asn_enabled is False
    assert resolver.degraded is True
    assert resolver.degraded_reasons == ("asn_database_missing", "country_database_missing")


def test_missing_module_and_reader_open_failure_degrade_safely(tmp_path, monkeypatch) -> None:
    database = tmp_path / "country.mmdb"
    database.write_bytes(b"not-a-real-database")

    monkeypatch.setattr(geo_resolver, "maxminddb", None)
    missing_module = GeoResolver(country_db=str(database), asn_reader=FakeReader({}))
    assert missing_module.country_enabled is False
    assert missing_module.degraded_reasons == ("maxminddb_unavailable",)

    class BrokenMaxMind:
        @staticmethod
        def open_database(path):
            raise RuntimeError("corrupt database")

    monkeypatch.setattr(geo_resolver, "maxminddb", BrokenMaxMind())
    broken_database = GeoResolver(country_db=str(database), asn_reader=FakeReader({}))
    assert broken_database.country_enabled is False
    assert broken_database.degraded_reasons == ("country_database_open_failed",)


def test_close_closes_each_injected_reader_once() -> None:
    reader = FakeReader({})
    resolver = GeoResolver(country_reader=reader, asn_reader=reader)

    resolver.close()

    assert reader.closed is True
