"""The member list of an organization: its spend columns, its filters and its two cards.

The columns are those of Admin's user list (today, the last 7 days, this
month, all time), counted from the day an account became a member. A card
above the list is the list under one filter, so the two never disagree.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tests.integration import test_organizations_postgres as shared
from tests.integration.test_organizations_postgres import (
    _carol,
    _organization_with_an_administrator,
)

pytest_plugins = ["tests.servers.conftest_auth", "tests.fixtures.sso_app"]
pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

# The fixtures of the organization tests, under the names pytest finds them by.
organization_routes = shared.organization_routes
clean_organizations = shared.clean_organizations
members_sign_straight_in = shared.members_sign_straight_in

MEMBERS = "/user/organization/members"


def _day(days_ago: int) -> str:
    return (datetime.now(UTC).date() - timedelta(days=days_ago)).isoformat()


@pytest.fixture
async def spend(clean_organizations):
    """Write what an account spent on a day; every row is removed afterwards."""
    accounts: set[str] = set()

    async def write(user_id: str, days_ago: int, cost: str) -> None:
        accounts.add(user_id)
        async with clean_organizations.acquire() as conn:
            await conn.execute(
                "INSERT INTO user_daily_cost (user_id, day, cost_usd, requests, last_request_at) "
                "VALUES ($1, $2, $3::numeric, 1, NOW())",
                user_id,
                _day(days_ago),
                cost,
            )

    yield write
    async with clean_organizations.acquire() as conn:
        await conn.execute(
            "DELETE FROM user_daily_cost WHERE user_id = ANY($1::text[])", list(accounts)
        )


async def _joined(pool, user_id: str, days_ago: int) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE org_memberships SET joined_at = NOW() - make_interval(days => $2) "
            "WHERE user_id = $1",
            user_id,
            days_ago,
        )


def _row(listed, user_id: str) -> dict:
    return next(member for member in listed.json()["members"] if member["user_id"] == user_id)


async def test_a_member_has_the_spend_of_today_of_seven_days_and_of_all_time_since_it_joined(
    app, idp, store, clean_organizations, members_sign_straight_in, spend
):
    state = await _organization_with_an_administrator(app, idp, store)
    alice, headers, _ = state["alice"]
    _bob, _bob_headers, bob_id = state["bob"]
    await _joined(clean_organizations, bob_id, 10)
    await spend(bob_id, 0, "2.00")
    await spend(bob_id, 3, "3.00")
    await spend(bob_id, 8, "5.00")
    # Before the account was a member: in no column of the organization.
    await spend(bob_id, 20, "7.00")

    listed = await alice.get(f"{MEMBERS}?sort_by=today&order=desc", headers=headers)
    detail = await alice.get(f"{MEMBERS}/{bob_id}", headers=headers)

    assert listed.status_code == 200, listed.text
    assert listed.json()["members"][0]["user_id"] == bob_id
    for bob in (_row(listed, bob_id), detail.json()["member"]):
        assert (bob["spent_today_usd"], bob["spent_7d_usd"], bob["spent_alltime_usd"]) == (
            2.0,
            5.0,
            10.0,
        )
        days = bob["spend_days"]
        assert [point["day"] for point in days] == [_day(ago) for ago in range(6, -1, -1)]
        assert [point["cost_usd"] for point in days] == [0, 0, 0, 3.0, 0, 0, 2.0]
    by_week = await alice.get(f"{MEMBERS}?sort_by=week&order=desc", headers=headers)
    by_all = await alice.get(f"{MEMBERS}?sort_by=alltime&order=desc", headers=headers)
    assert by_week.json()["members"][0]["user_id"] == bob_id
    assert by_all.json()["members"][0]["user_id"] == bob_id


async def test_the_list_is_narrowed_by_the_limit_and_by_spend_and_the_cards_count_the_same(
    app, idp, store, clean_organizations, members_sign_straight_in, spend
):
    state = await _organization_with_an_administrator(app, idp, store)
    alice, headers, alice_id = state["alice"]
    _bob, _bob_headers, bob_id = state["bob"]
    _carol_browser, _carol_headers, carol_id = await _carol(app, idp, store)
    for member_id in (bob_id, carol_id):
        limited = await alice.put(
            f"{MEMBERS}/{member_id}/monthly-limit",
            json={"monthly_limit_usd": "10"},
            headers=headers,
        )
        assert limited.status_code == 200, limited.text
    await spend(bob_id, 0, "9.00")  # 90% of the limit
    await spend(carol_id, 0, "12.00")  # over it

    async def ids(query: str) -> tuple[int, list[str]]:
        listed = await alice.get(f"{MEMBERS}?{query}", headers=headers)
        assert listed.status_code == 200, listed.text
        return listed.json()["total"], [member["user_id"] for member in listed.json()["members"]]

    assert await ids("limit_state=near&sort_by=limit_use&order=desc") == (2, [carol_id, bob_id])
    assert await ids("limit_state=over") == (1, [carol_id])
    assert await ids("min_cost_today=10") == (1, [carol_id])
    assert await ids("min_cost_month=9&currency=USD&sort_by=month&order=desc") == (
        2,
        [carol_id, bob_id],
    )
    assert await ids("min_cost_today=10&limit_state=near&status=active") == (1, [carol_id])
    # A page of one still says how many match.
    assert await ids("limit_state=near&sort_by=limit_use&order=desc&limit=1") == (2, [carol_id])
    stale = await alice.get(f"{MEMBERS}?min_cost_today=10&currency=CNY", headers=headers)
    assert stale.status_code == 409

    cards = await alice.get(f"{MEMBERS}/summary", headers=headers)

    assert cards.status_code == 200, cards.text
    near, spenders = cards.json()["near_limit"], cards.json()["top_spenders_today"]
    assert (near["count"], [member["user_id"] for member in near["top"]]) == (
        2,
        [carol_id, bob_id],
    )
    assert (spenders["count"], [member["user_id"] for member in spenders["top"]]) == (
        2,
        [carol_id, bob_id],
    )
    assert spenders["top"][0]["spent_today_usd"] == 12.0
    # An administrator has no limit of the organization and spent nothing: in neither card.
    assert alice_id not in {member["user_id"] for member in near["top"] + spenders["top"]}


async def test_the_cards_are_the_organization_administrators_alone(
    app, idp, store, members_sign_straight_in
):
    state = await _organization_with_an_administrator(app, idp, store)
    bob, bob_headers, _ = state["bob"]

    refused = await bob.get(f"{MEMBERS}/summary", headers=bob_headers)
    signed_out = await shared.new_browser(app).get(f"{MEMBERS}/summary")

    assert refused.status_code == 403
    assert signed_out.status_code == 401
