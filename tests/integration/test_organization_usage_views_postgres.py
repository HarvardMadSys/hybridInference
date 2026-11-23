"""What the organization console gained beside Admin: volume, top members, tokens, hours.

Over HTTP, on the real store. Every figure is of the members of one
organization, from the day each became one.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta

import pytest

from tests.integration import test_organizations_postgres as shared
from tests.integration.test_organizations_postgres import (
    _carol,
    _organization_with_an_administrator,
    _seed_usage,
)

pytest_plugins = ["tests.servers.conftest_auth", "tests.fixtures.sso_app"]
pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

# The fixtures of the organization tests, under the names pytest finds them by.
organization_routes = shared.organization_routes
clean_organizations = shared.clean_organizations
members_sign_straight_in = shared.members_sign_straight_in

ORGANIZATION = "/user/organization"


async def _request(
    pool, user_id: str, *, ago: timedelta, status: int = 200, latency: int | None = None, **tokens
) -> None:
    """One logged request of *user_id*, *ago* before now."""
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO api_logs (request_id, timestamp, model_id, provider, user_id, "
            "status_code, prompt_tokens, completion_tokens, total_tokens, cache_read_tokens, "
            "cost_usd, latency_ms) "
            "VALUES ($1, $2, 'model-a', 'test', $3, $4, $5, $6, $7, $8, 0.01, $9)",
            f"org-itest-{secrets.token_hex(6)}",
            datetime.now(UTC) - ago,
            user_id,
            status,
            tokens.get("prompt", 100),
            tokens.get("completion", 50),
            tokens.get("prompt", 100) + tokens.get("completion", 50),
            tokens.get("cached", 0),
            latency,
        )


async def _joined(pool, user_id: str, days_ago: int) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE org_memberships SET joined_at = NOW() - make_interval(days => $2) "
            "WHERE user_id = $1",
            user_id,
            days_ago,
        )


async def _forget_spend(pool, *user_ids: str) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM user_daily_cost WHERE user_id = ANY($1::text[])", list(user_ids)
        )


async def test_the_request_volume_counts_four_windows_that_end_now(
    app, idp, store, clean_organizations, members_sign_straight_in
):
    state = await _organization_with_an_administrator(app, idp, store)
    alice, headers, _ = state["alice"]
    _bob, _bob_headers, bob_id = state["bob"]
    outsider_id = await shared.existing_user(store, "someone@elsewhere.org")
    pool = clean_organizations
    await _joined(pool, bob_id, 25)
    for _ in range(3):
        await _request(pool, bob_id, ago=timedelta(minutes=5), latency=100)
    await _request(pool, bob_id, ago=timedelta(minutes=10), status=500, latency=100)
    await _request(pool, bob_id, ago=timedelta(hours=5), latency=400)
    await _request(pool, bob_id, ago=timedelta(days=3), latency=None)
    await _request(pool, bob_id, ago=timedelta(days=20), status=429)
    # Before the account was a member, and of an account that is none: counted nowhere.
    await _request(pool, bob_id, ago=timedelta(days=27))
    await _request(pool, outsider_id, ago=timedelta(minutes=1), status=500)

    volume = await alice.get(f"{ORGANIZATION}/usage/volume", headers=headers)

    assert volume.status_code == 200, volume.text
    assert volume.json() == {
        "hour": {"requests": 4, "ok": 3, "failed": 1, "avg_latency_ms": 100.0},
        "day": {"requests": 5, "ok": 4, "failed": 1, "avg_latency_ms": 160.0},
        # The request of three days ago recorded no latency: the mean is of the five that did.
        "week": {"requests": 6, "ok": 5, "failed": 1, "avg_latency_ms": 160.0},
        "month": {"requests": 7, "ok": 5, "failed": 2, "avg_latency_ms": 160.0},
    }


async def test_a_quiet_organization_has_a_volume_of_zeros(
    app, idp, store, members_sign_straight_in
):
    state = await _organization_with_an_administrator(app, idp, store)
    alice, headers, _ = state["alice"]
    bob, bob_headers, _ = state["bob"]

    volume = await alice.get(f"{ORGANIZATION}/usage/volume", headers=headers)
    refused = await bob.get(f"{ORGANIZATION}/usage/volume", headers=bob_headers)

    quiet = {"requests": 0, "ok": 0, "failed": 0, "avg_latency_ms": None}
    assert volume.json() == {"hour": quiet, "day": quiet, "week": quiet, "month": quiet}
    assert refused.status_code == 403


async def test_the_overview_names_the_top_members_and_counts_the_tokens_of_each_model(
    app, idp, store, clean_organizations, members_sign_straight_in
):
    state = await _organization_with_an_administrator(app, idp, store)
    alice, headers, alice_id = state["alice"]
    _bob, _bob_headers, bob_id = state["bob"]
    _carol_browser, _carol_headers, carol_id = await _carol(app, idp, store)
    pool = clean_organizations
    await _seed_usage(pool, store, alice_id, requests=2, cost="0.50")
    await _seed_usage(pool, store, bob_id, requests=4, cost="1.50")
    # A request counts from the instant of the membership, so Bob joined before this one.
    await _joined(pool, bob_id, 1)
    await _request(pool, bob_id, ago=timedelta(minutes=1), prompt=1000, completion=10, cached=800)
    try:
        overview = await alice.get(f"{ORGANIZATION}/overview?period=7d", headers=headers)
        models = await alice.get(f"{ORGANIZATION}/usage/models?period=7d", headers=headers)
    finally:
        await _forget_spend(pool, alice_id, bob_id)

    assert overview.status_code == 200, overview.text
    top = overview.json()["top_members"]
    # By spend, the highest first; Carol did nothing and is not ranked.
    assert [member["user_id"] for member in top] == [bob_id, alice_id]
    assert carol_id not in {member["user_id"] for member in top}
    assert (top[0]["cost_usd"], top[0]["requests"]) == (1.5, 5)
    for listed in (overview.json()["models"], models.json()["models"]):
        by_model = {row["model_id"]: row for row in listed}
        # model-a: three seeded requests of 100 + 50 tokens, and the one with a cache read.
        assert by_model["model-a"]["requests"] == 4
        assert by_model["model-a"]["prompt_tokens"] == 1300
        assert by_model["model-a"]["completion_tokens"] == 160
        assert by_model["model-a"]["cached_tokens"] == 800
        assert by_model["model-a"]["total_tokens"] == 1460
        assert by_model["model-b"]["cached_tokens"] == 0


async def test_the_series_has_a_point_for_each_hour_when_it_is_asked_to(
    app, idp, store, clean_organizations, members_sign_straight_in
):
    state = await _organization_with_an_administrator(app, idp, store)
    alice, headers, _ = state["alice"]
    _bob, _bob_headers, bob_id = state["bob"]
    await _joined(clean_organizations, bob_id, 1)
    await _request(clean_organizations, bob_id, ago=timedelta(seconds=30))
    asked_at = datetime.now(UTC)

    def overview(query: str):
        return alice.get(f"{ORGANIZATION}/overview?{query}", headers=headers)

    week_by_hour = await overview("period=7d&granularity=hour")
    week = await overview("period=7d")
    day_by_day = await overview("period=1d&granularity=day")
    month_by_hour = await overview("period=30d&granularity=hour")

    assert week_by_hour.status_code == 200, week_by_hour.text
    body = week_by_hour.json()
    first_midnight = datetime.combine(
        asked_at.date() - timedelta(days=6), datetime.min.time(), tzinfo=UTC
    )
    begun = (asked_at - first_midnight) // timedelta(hours=1) + 1
    assert body["bucket"] == "hour"
    # Every hour of the seven days that has begun, and none that has not.
    assert len(body["series"]) in (begun, begun + 1)
    assert datetime.fromisoformat(body["series"][0]["bucket"]) == first_midnight
    assert sum(point["requests"] for point in body["series"]) == 1
    assert (week.json()["bucket"], len(week.json()["series"])) == ("day", 7)
    assert (day_by_day.json()["bucket"], len(day_by_day.json()["series"])) == ("day", 1)
    assert month_by_hour.status_code == 400


async def test_the_audit_log_is_narrowed_to_one_member_and_has_no_address(
    app, idp, store, members_sign_straight_in
):
    state = await _organization_with_an_administrator(app, idp, store)
    alice, headers, alice_id = state["alice"]
    _bob, _bob_headers, bob_id = state["bob"]
    _carol_browser, _carol_headers, carol_id = await _carol(app, idp, store)
    outsider_id = await shared.existing_user(store, "someone@elsewhere.org")
    for member_id, limit in ((bob_id, "5"), (carol_id, "6")):
        saved = await alice.put(
            f"{ORGANIZATION}/members/{member_id}/monthly-limit",
            json={"monthly_limit_usd": limit},
            headers=headers,
        )
        assert saved.status_code == 200, saved.text

    def log(query: str = ""):
        return alice.get(f"{ORGANIZATION}/audit-log?{query}", headers=headers)

    everything = await log()
    bobs = await log(f"member={bob_id}")
    bobs_limits = await log(f"member={bob_id}&category=limits")
    alices = await log(f"member={alice_id}")
    nobodys = await log(f"member={outsider_id}")

    assert bobs.status_code == 200, bobs.text
    assert {entry["target_user_id"] for entry in bobs.json()["entries"]} == {bob_id}
    assert bobs.json()["total"] == len(bobs.json()["entries"]) < everything.json()["total"]
    # The whole change is in the entry: the old value and the new one.
    (limit_entry,) = bobs_limits.json()["entries"]
    assert limit_entry["action"] == "organization.member_limit.set"
    assert (limit_entry["details"]["old"], limit_entry["details"]["new"]) == (None, "5.0000")
    # Alice set both limits: a member is found as the actor too.
    assert {
        entry["target_user_id"]
        for entry in alices.json()["entries"]
        if entry["action"] == "organization.member_limit.set"
    } == {bob_id, carol_id}
    assert nobodys.json() == {"total": 0, "entries": []}
    for response in (everything, bobs, alices):
        assert "ip" not in {key for entry in response.json()["entries"] for key in entry}
        assert "192.0.2." not in response.text and "127.0.0.1" not in response.text
