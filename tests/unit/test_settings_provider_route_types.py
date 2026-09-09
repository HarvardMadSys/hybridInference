"""``PROVIDER_ROUTE_TYPES``: the deployment's per-provider route-type policy."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from serving.config.settings import Settings, parse_provider_route_types


def test_parse_maps_each_provider_to_its_route_types():
    assert parse_provider_route_types(
        "chutes=quota, featherless=concurrency ,openrouter=concurrency|on_demand"
    ) == {
        "chutes": frozenset({"quota"}),
        "featherless": frozenset({"concurrency"}),
        "openrouter": frozenset({"concurrency", "on_demand"}),
    }


def test_parse_empty_policy_restricts_nothing():
    assert parse_provider_route_types("") == {}
    assert parse_provider_route_types(" , ") == {}


@pytest.mark.parametrize("raw", ["chutes", "chutes=", "=quota", "chutes=|"])
def test_parse_rejects_a_malformed_entry(raw):
    with pytest.raises(ValueError, match="must look like provider=type"):
        parse_provider_route_types(raw)


def test_parse_rejects_an_unknown_route_type():
    with pytest.raises(ValueError, match="unknown route type daily"):
        parse_provider_route_types("chutes=quota|daily")


def test_settings_reject_a_policy_the_console_could_not_enforce(monkeypatch):
    monkeypatch.setenv("PROVIDER_ROUTE_TYPES", "chutes=daily")
    with pytest.raises(ValidationError, match="unknown route type daily"):
        Settings()


def test_settings_accept_a_valid_policy(monkeypatch):
    monkeypatch.setenv("PROVIDER_ROUTE_TYPES", "chutes=quota")
    assert Settings().provider_route_types == "chutes=quota"
