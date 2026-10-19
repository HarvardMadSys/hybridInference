"""Which provider keys the admin quota dashboard lists, and what it shows of them.

``_discover_provider_keys`` merges keys from settings, the provider_api_keys
table and the live key pools. Route-bound DB keys belong to one route and must
not appear as provider-wide keys; when it cannot tell which those are, it lists
no DB key at all rather than risk it. The cards for disabled keys, and every
card, show a key only masked. The response parsers accept the shapes providers
actually send.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from serving.adapters import dynamic_keys
from serving.admin import provider_quotas as quotas

ENV_A = "sk-env-aaaaaaaaaaaaaaaaaaaa"
ENV_B = "sk-env-bbbbbbbbbbbbbbbbbbbb"
DB_C = "sk-db-cccccccccccccccccccccc"
POOL_D = "sk-pool-dddddddddddddddddd"


class Store:
    def __init__(self, *, db_keys=(), disabled=(), fail=()):
        self.db_keys = list(db_keys)
        self.disabled = list(disabled)
        self.fail = set(fail)
        self.excluded = None

    async def list_disabled_provider_env_key_hashes(self, provider):
        if "disabled" in self.fail:
            raise RuntimeError("down")
        return self.disabled

    async def list_provider_keys_full(self, provider, *, exclude_ids):
        self.excluded = exclude_ids
        return list(self.db_keys)


@pytest.fixture
def settings(monkeypatch):
    values: dict[str, str] = {}
    monkeypatch.setattr(quotas, "config_value", lambda name, default="": values.get(name, default))
    monkeypatch.setattr(dynamic_keys, "is_env_key_disabled", lambda provider, key_hash: False)
    monkeypatch.setattr(dynamic_keys, "get_pools_for_provider", lambda *a, **k: [])

    async def no_route_bound(_store):
        return {"route-bound-1"}

    monkeypatch.setattr(dynamic_keys, "_list_route_bound_db_key_ids", no_route_bound)
    return values


async def _discover(store=None):
    return await quotas._discover_provider_keys("zai", "ZAI_API_KEY", "ZAI_API_KEY", store)


async def test_settings_db_and_pool_keys_are_listed_once_each(settings, monkeypatch):
    settings.update({"ZAI_API_KEY": ENV_A, "ZAI_API_KEY2": ENV_B})
    pool = SimpleNamespace(snapshot_keys=lambda: [ENV_A, POOL_D, ""])
    monkeypatch.setattr(dynamic_keys, "get_pools_for_provider", lambda *a, **k: [pool])
    store = Store(db_keys=[DB_C, ENV_B, ""])

    keys = await _discover(store)

    assert keys == [(1, ENV_A), (2, ENV_B), (3, DB_C), (4, POOL_D)]
    assert store.excluded == {"route-bound-1"}  # a route's own key is not provider-wide


async def test_without_the_route_bound_list_no_db_key_is_listed(settings, monkeypatch):
    """Fail closed: a route-bound key must never surface as a provider-wide key."""
    settings["ZAI_API_KEY"] = ENV_A

    async def unavailable(_store):
        raise RuntimeError("database down")

    monkeypatch.setattr(dynamic_keys, "_list_route_bound_db_key_ids", unavailable)
    store = Store(db_keys=[DB_C])

    assert await _discover(store) == [(1, ENV_A)]
    assert store.excluded is None


async def test_a_disabled_setting_key_leaves_a_gap_new_keys_do_not_reuse(settings):
    """Numbers name settings (ZAI_API_KEY2): a DB key must not take the disabled one's."""
    settings.update({"ZAI_API_KEY": ENV_A, "ZAI_API_KEY2": ENV_B})
    store = Store(db_keys=[DB_C], disabled=[dynamic_keys.env_key_hash(ENV_A)])

    assert await _discover(store) == [(2, ENV_B), (3, DB_C)]


async def test_an_unreadable_disabled_list_still_lists_the_settings(settings):
    settings["ZAI_API_KEY"] = ENV_A

    assert await _discover(Store(fail={"disabled"})) == [(1, ENV_A)]


async def test_disabled_keys_get_cards_that_never_show_the_key(settings):
    class DisabledStore:
        async def list_disabled_provider_env_keys(self, provider):
            return [(dynamic_keys.env_key_hash(ENV_A), "sk-env-a…aaaa")]

        async def list_provider_keys(self, provider):
            return [
                SimpleNamespace(id="row-1", status="disabled", key_prefix="sk-db-c…cccc"),
                SimpleNamespace(id="row-2", status="active", key_prefix="sk-live…"),
                SimpleNamespace(id="row-3", status="disabled", key_prefix="sk-gone…"),
            ]

        async def get_provider_key_full(self, key_id):
            return {"row-1": ("zai", DB_C)}.get(key_id)

    cards = await quotas._disabled_keys_for_provider(DisabledStore(), "zai", "Z.ai")

    assert [card.key_masked for card in cards] == ["sk-env-a…aaaa", "sk-db-c…cccc"]
    assert ENV_A not in repr(cards) and DB_C not in repr(cards)


async def test_disabled_cards_survive_an_unreadable_store(settings):
    class Broken:
        async def list_disabled_provider_env_keys(self, provider):
            raise RuntimeError("down")

        async def list_provider_keys(self, provider):
            raise RuntimeError("down")

    assert await quotas._disabled_keys_for_provider(Broken(), "zai", "Z.ai") == []


# --- what a card shows of a key, and the response parsers ---------------------


@pytest.mark.parametrize(
    ("key", "shown"),
    [
        ("sk-0123456789abcdef", "sk-01234...cdef"),
        ("short-secret", "***configured***"),  # never a prefix of a short secret
    ],
)
def test_a_key_is_only_ever_shown_masked(key, shown):
    assert quotas._mask_key(key) == shown
    card = quotas._err("zai", "Z.ai", key, "boom")
    assert card.key_masked == shown and key not in repr(card)


@pytest.mark.parametrize(
    ("value", "parsed"),
    [
        (12, 12.0),
        (1.5, 1.5),
        ("1200", 1200.0),  # Kimi sends figures as strings
        (" 3.5 ", 3.5),
        ("", None),
        ("n/a", None),
        (True, None),
        (math.inf, None),
        ("nan", None),
        (None, None),
        ([1], None),
    ],
)
def test_as_float(value, parsed):
    assert quotas._as_float(value) == parsed


def test_first_float_takes_the_first_numeric_alias():
    assert quotas._first_float({"a": "x", "b": "2", "c": 3}, "a", "b", "c") == 2.0
    assert quotas._first_float({"a": None}, "a", "missing") is None


@pytest.mark.parametrize(
    ("value", "parsed"),
    [
        (1_700_000_000_000, datetime(2023, 11, 14, 22, 13, 20, tzinfo=UTC)),
        (1_700_000_000_000.0, datetime(2023, 11, 14, 22, 13, 20, tzinfo=UTC)),
        (True, None),
        ("1700000000000", None),
        (10**20, None),  # out of range
    ],
)
def test_parse_epoch_ms(value, parsed):
    assert quotas._parse_epoch_ms(value) == parsed


@pytest.mark.parametrize(
    ("cookie", "value"),
    [
        ("a=1; token=abc; b=2", "abc"),
        ("token=abc", "abc"),
        ("a=1", ""),
        ('broken="unterminated', ""),
    ],
)
def test_extract_cookie_value(cookie, value):
    assert quotas._extract_cookie_value(cookie, "token") == value


def test_credit_fields_are_detected_case_insensitively():
    assert quotas._entry_has_credit_field({"remainingCredits": 1})
    assert not quotas._entry_has_credit_field({"remaining": 1})
