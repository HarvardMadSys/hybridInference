"""The account's interface language: stored with the time it was chosen, newest wins."""

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from pydantic import ValidationError

from serving.schemas_auth import LocalePreferenceUpdate
from serving.servers.routers import user_routes


class _PreferencesStore:
    """An operational store holding one user's preferences JSONB in memory."""

    def __init__(self, preferences: dict[str, Any] | None = None) -> None:
        self.preferences: dict[str, Any] = dict(preferences or {})
        self.writes = 0

    async def get_user_preferences(self, user_id: str) -> dict[str, Any]:
        return dict(self.preferences)

    async def get_user_by_id(self, user_id: str) -> dict[str, Any] | None:
        return {"id": user_id}

    async def update_user_preferences(self, user_id: str, preferences: dict[str, Any]) -> None:
        self.preferences = dict(preferences)
        self.writes += 1


USER = {"user_id": "u1"}


async def _choose(store: _PreferencesStore, **body: Any):
    return await user_routes.update_locale_preference(
        body=LocalePreferenceUpdate(**body), current_user=USER, op_store=store
    )


@pytest.mark.asyncio
async def test_a_new_choice_is_stored_at_the_server_time_beside_other_preferences():
    store = _PreferencesStore({"llm_prober_layout": {"e2e_models": ["m"]}})
    before = datetime.now(timezone.utc)

    result = await _choose(store, locale="zh-CN")

    assert result.value == "zh-CN"
    assert before <= result.chosen_at <= datetime.now(timezone.utc)
    assert store.preferences["llm_prober_layout"] == {"e2e_models": ["m"]}
    assert store.preferences["locale"] == {
        "value": "zh-CN",
        "chosen_at": result.chosen_at.isoformat().replace("+00:00", "Z"),
    }


@pytest.mark.asyncio
async def test_a_choice_carried_in_keeps_its_time_but_never_a_future_one():
    store = _PreferencesStore()
    earlier = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)

    assert (await _choose(store, locale="en", chosen_at=earlier)).chosen_at == earlier

    future = datetime.now(timezone.utc) + timedelta(days=30)
    clamped = await _choose(store, locale="zh-CN", chosen_at=future)
    assert clamped.chosen_at <= datetime.now(timezone.utc)


@pytest.mark.asyncio
async def test_an_older_choice_does_not_replace_a_newer_one():
    newer = datetime(2026, 10, 10, tzinfo=timezone.utc)
    store = _PreferencesStore({"locale": {"value": "en", "chosen_at": newer.isoformat()}})

    result = await _choose(store, locale="zh-CN", chosen_at=newer - timedelta(hours=1))

    assert (result.value, result.chosen_at) == ("en", newer)
    assert store.writes == 0


@pytest.mark.asyncio
async def test_a_naive_time_is_read_as_utc():
    store = _PreferencesStore()
    result = await _choose(store, locale="en", chosen_at=datetime(2026, 10, 1, 12, 0))
    assert result.chosen_at == datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


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
        datetime(2026, 10, 1, tzinfo=timezone.utc),
    )
