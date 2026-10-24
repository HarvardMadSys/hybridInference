"""The account's interface language: stored with the time it was chosen, newest wins.

The store's conditional write is exercised against Postgres in
``tests/servers/test_user_preferences_atomic.py``; here a stand-in with the
same contract checks what the route asks of it and returns.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from serving.exceptions import UserNotFoundError
from serving.schemas_auth import LocalePreferenceUpdate
from serving.servers.routers import user_routes


class _PreferencesStore:
    """One user's preferences, written a key at a time like the real store."""

    def __init__(self, preferences: dict[str, Any] | None = None, exists: bool = True) -> None:
        self.preferences: dict[str, Any] = dict(preferences or {})
        self.exists = exists
        self.writes: list[str] = []

    async def set_user_preference(self, user_id: str, key: str, value: Any) -> bool:
        if not self.exists:
            return False
        self.preferences[key] = value
        self.writes.append(key)
        return True

    async def set_user_preference_if_newer(
        self, user_id: str, key: str, value: dict[str, Any], chosen_at: datetime
    ) -> dict[str, Any] | None:
        if not self.exists:
            return None
        current = self.preferences.get(key)
        try:
            stored_at = datetime.fromisoformat(current["chosen_at"].replace("Z", "+00:00"))
        except (TypeError, KeyError, ValueError, AttributeError):
            stored_at = None
        if stored_at is not None and stored_at >= chosen_at:
            return current
        self.preferences[key] = value
        self.writes.append(key)
        return value


USER = {"user_id": "u1"}


async def _choose(store: _PreferencesStore, **body: Any):
    return await user_routes.update_locale_preference(
        body=LocalePreferenceUpdate(**body), current_user=USER, op_store=store
    )


@pytest.mark.asyncio
async def test_a_new_choice_is_stored_at_the_server_time_and_writes_only_its_key():
    store = _PreferencesStore({"llm_prober_layout": {"e2e_models": ["m"]}})
    before = datetime.now(UTC)

    result = await _choose(store, locale="zh-CN")

    assert result.value == "zh-CN"
    assert before <= result.chosen_at <= datetime.now(UTC)
    assert store.writes == ["locale"]
    assert store.preferences["llm_prober_layout"] == {"e2e_models": ["m"]}


@pytest.mark.asyncio
async def test_the_stored_time_has_a_fixed_width():
    store = _PreferencesStore()
    whole_second = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)

    await _choose(store, locale="en", chosen_at=whole_second)

    # Fixed width, so stored times also order correctly as strings.
    assert store.preferences["locale"]["chosen_at"] == "2026-10-01T12:00:00.000000Z"


@pytest.mark.asyncio
async def test_a_choice_carried_in_keeps_its_time_but_never_a_future_one():
    store = _PreferencesStore()
    earlier = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)

    assert (await _choose(store, locale="en", chosen_at=earlier)).chosen_at == earlier

    future = datetime.now(UTC) + timedelta(days=30)
    clamped = await _choose(store, locale="zh-CN", chosen_at=future)
    assert clamped.chosen_at <= datetime.now(UTC)


@pytest.mark.asyncio
async def test_an_older_choice_returns_the_newer_one_kept():
    newer = datetime(2026, 10, 10, tzinfo=UTC)
    store = _PreferencesStore(
        {"locale": {"value": "en", "chosen_at": "2026-10-10T00:00:00.000000Z"}}
    )

    result = await _choose(store, locale="zh-CN", chosen_at=newer - timedelta(hours=1))

    assert (result.value, result.chosen_at) == ("en", newer)
    assert store.writes == []


@pytest.mark.asyncio
async def test_a_newer_entry_this_build_cannot_read_is_replaced():
    store = _PreferencesStore(
        {"locale": {"value": "fr", "chosen_at": "2099-01-01T00:00:00.000000Z"}}
    )

    result = await _choose(store, locale="zh-CN")

    assert result.value == "zh-CN"
    assert store.preferences["locale"]["value"] == "zh-CN"


@pytest.mark.asyncio
async def test_a_missing_user_is_reported():
    with pytest.raises(UserNotFoundError):
        await _choose(_PreferencesStore(exists=False), locale="en")


@pytest.mark.asyncio
async def test_a_naive_time_is_read_as_utc():
    store = _PreferencesStore()
    result = await _choose(store, locale="en", chosen_at=datetime(2026, 10, 1, 12, 0))
    assert result.chosen_at == datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


def test_a_language_the_console_cannot_render_is_refused():
    with pytest.raises(ValidationError):
        LocalePreferenceUpdate(locale="fr")
    with pytest.raises(ValidationError):
        LocalePreferenceUpdate(locale="zh-TW")


@pytest.mark.parametrize(
    "stored",
    [
        None,
        "zh-CN",
        {"value": "fr", "chosen_at": "2026-10-01T00:00:00Z"},
        {"value": "en"},
        {"value": "en", "chosen_at": "yesterday"},
    ],
)
def test_a_stored_value_this_build_cannot_show_reads_as_never_chosen(stored):
    preferences = {} if stored is None else {"locale": stored}
    assert user_routes._extract_locale_preference(preferences) is None


def test_a_stored_choice_reads_back():
    choice = user_routes._extract_locale_preference(
        {"locale": {"value": "zh-CN", "chosen_at": "2026-10-01T00:00:00Z"}}
    )
    assert choice is not None
    assert (choice.value, choice.chosen_at) == (
        "zh-CN",
        datetime(2026, 10, 1, tzinfo=UTC),
    )
