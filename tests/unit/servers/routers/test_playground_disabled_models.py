"""The playground reads the models that are off for the account of its session."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from serving.servers.routers import playground

pytestmark = pytest.mark.asyncio


def _store(membership_role: str, account_role: str = "internal") -> SimpleNamespace:
    return SimpleNamespace(
        get_user_preferences=AsyncMock(return_value={"disabled_models": ["off-by-the-platform"]}),
        get_organization_membership=AsyncMock(
            return_value={
                "organization_id": "o1",
                "role": membership_role,
                "account_role": account_role,
                "disabled_models": ["off-by-the-organization"],
            }
        ),
    )


async def test_the_callers_context_has_the_lists_of_the_platform_and_of_the_organization():
    """A session names no model: without this the playground serves what every other surface refuses."""
    store = _store("member")

    caller = await playground._require_internal({"user_id": "u1", "role": "internal"}, store)

    assert caller["disabled_models"] == ["off-by-the-organization", "off-by-the-platform"]
    assert caller["role"] == "internal"
    store.get_user_preferences.assert_awaited_once_with("u1")


@pytest.mark.parametrize(
    ("membership_role", "account_role"), [("admin", "internal"), ("member", "admin")]
)
async def test_the_list_of_the_organization_does_not_hold_an_administrator(
    membership_role, account_role
):
    store = _store(membership_role, account_role)

    caller = await playground._require_internal({"user_id": "u1", "role": account_role}, store)

    assert caller["disabled_models"] == ["off-by-the-platform"]


async def test_a_gateway_without_a_store_keeps_the_session_context():
    session = {"user_id": "u1", "role": "internal"}

    assert await playground._require_internal(session, None) is session
