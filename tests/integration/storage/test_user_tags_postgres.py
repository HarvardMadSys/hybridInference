"""User tags against PostgreSQL: the rows, the auth lookups' folding, the automatic rule.

What a mock cannot show: the migration's seed and constraints, that every
auth lookup carries an account's tags and holds its quotas to them (the month
spend included when only a tag sets a monthly quota), and the automatic
rule's hour counting, its exclusions and its respect for a dismissal.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import asyncpg
import pytest
import pytest_asyncio

from serving.admin.user_tag_detector import run_rules
from serving.storage.postgres_operational import PostgresOperationalStore
from tests.fixtures import postgres

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

PREFIX = "u-tagtest-"
TAGS = ("tagtest-a", "tagtest-limits", "tagtest-auto", "tagtest-hand")


async def _cleanup(pool) -> None:
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM api_logs WHERE request_id LIKE 'tagtest-%'")
        await conn.execute("DELETE FROM user_daily_cost WHERE user_id LIKE $1", f"{PREFIX}%")
        await conn.execute("DELETE FROM api_keys WHERE user_id LIKE $1", f"{PREFIX}%")
        # Memberships leave with their accounts and their tags.
        await conn.execute("DELETE FROM users WHERE id LIKE $1", f"{PREFIX}%")
        await conn.execute("DELETE FROM user_tags WHERE name = ANY($1::text[])", list(TAGS))


@pytest_asyncio.fixture
async def store():
    pool = await postgres.create_pool(min_size=1, max_size=5)
    op_store = PostgresOperationalStore(pool)
    await op_store.initialize()
    await _cleanup(pool)
    try:
        yield op_store
    finally:
        await _cleanup(pool)
        await pool.close()


async def _user(store, name: str, *, key: bool = False, quota: str = "100") -> str:
    user_id = f"{PREFIX}{name}"
    await store.create_user(user_id=user_id, email=f"{name}@tagtest.example", password_hash="x")
    if key:
        await store.create_key(
            key_hash=f"hash-{user_id}",
            key_prefix=f"sk-tagtest-{name}",
            user_id=user_id,
            account_id=user_id,
            quota_daily_cost_usd=Decimal(quota),
        )
    return user_id


async def _requests(store, user_id: str, hours_ago: list[float], *, batch: bool = False) -> None:
    now = datetime.now(UTC)
    async with store._pool.acquire() as conn:
        for i, hours in enumerate(hours_ago):
            await conn.execute(
                "INSERT INTO api_logs (request_id, model_id, provider, user_id, timestamp, "
                "batch_job_id) VALUES ($1, 'm1', 'test', $2, $3, $4)",
                f"tagtest-{user_id}-{batch}-{i}",
                user_id,
                now - timedelta(hours=hours),
                "batch-1" if batch else None,
            )


async def test_the_built_in_tag_is_seeded_and_cannot_be_deleted(store):
    tag = await store.get_user_tag("unattended")
    assert tag is not None
    assert tag["system"] is True
    assert tag["auto_enabled"] is True
    assert (tag["auto_window_hours"], tag["auto_min_active_hours"]) == (12, 12)
    assert tag["quota_daily_cost"] is None and tag["max_concurrent_requests"] is None
    assert await store.delete_user_tag("unattended") is False
    assert await store.get_user_tag("unattended") is not None


async def test_a_tag_is_created_changed_and_deleted(store):
    created = await store.create_user_tag(
        "tagtest-a",
        {
            "description": "A",
            "quota_daily_cost": Decimal("5"),
            "accounting_currency": "USD",
            "allowed_models": ["m1", "m2"],
        },
    )
    assert created["quota_daily_cost"] == Decimal("5")
    assert created["allowed_models"] == ["m1", "m2"]
    assert created["auto_enabled"] is False
    assert await store.create_user_tag("tagtest-a", {}) is None

    updated = await store.update_user_tag(
        "tagtest-a", {"quota_daily_cost": None, "max_concurrent_requests": 3}
    )
    assert updated["quota_daily_cost"] is None
    assert updated["max_concurrent_requests"] == 3
    assert updated["updated_at"] >= created["updated_at"]
    assert await store.update_user_tag("tagtest-missing", {"description": "x"}) is None

    # The built-in tags come first.
    assert (await store.list_user_tags())[0]["name"] == "unattended"
    assert await store.delete_user_tag("tagtest-a") is True
    assert await store.get_user_tag("tagtest-a") is None


async def test_the_table_refuses_out_of_range_settings(store):
    with pytest.raises(asyncpg.CheckViolationError):
        await store.create_user_tag("Bad Name", {})
    with pytest.raises(asyncpg.CheckViolationError):
        await store.create_user_tag(
            "tagtest-a", {"auto_window_hours": 6, "auto_min_active_hours": 7}
        )
    with pytest.raises(asyncpg.CheckViolationError):
        await store.create_user_tag("tagtest-a", {"max_concurrent_requests": 0})


async def test_memberships_by_hand_and_their_removal(store):
    await store.create_user_tag("tagtest-hand", {})
    alice = await _user(store, "alice")
    bob = await _user(store, "bob")

    changed, unknown = await store.add_user_tag_members(
        "tagtest-hand", [alice, bob, alice, f"{PREFIX}ghost"], assigned_by="admin-1"
    )
    assert changed == [alice, bob]
    assert unknown == [f"{PREFIX}ghost"]
    # Already a manual member: unchanged.
    assert await store.add_user_tag_members("tagtest-hand", [alice], assigned_by=None) == ([], [])
    assert await store.get_tags_for_users([alice, bob, f"{PREFIX}nobody"]) == {
        alice: ["tagtest-hand"],
        bob: ["tagtest-hand"],
    }
    total, members = await store.list_user_tag_members("tagtest-hand")
    assert total == 2
    assert {m["user_id"] for m in members} == {alice, bob}
    assert members[0]["assigned_by"] == "admin-1"

    # A tag without an automatic rule: removing deletes the membership.
    assert (
        await store.remove_user_tag_member("tagtest-hand", alice, removed_by="admin-1") == "manual"
    )
    assert await store.remove_user_tag_member("tagtest-hand", alice, removed_by=None) is None
    assert await store.get_tags_for_users([alice]) == {}

    # Deleting the tag takes its memberships.
    await store.delete_user_tag("tagtest-hand")
    assert await store.get_tags_for_users([bob]) == {}


async def test_removing_a_tag_with_a_rule_dismisses_it(store):
    alice = await _user(store, "alice")
    await store.add_user_tag_members("unattended", [alice], assigned_by=None)
    assert await store.remove_user_tag_member("unattended", alice, removed_by="admin-1") == "manual"
    memberships = await store.get_user_tag_memberships(alice)
    assert [(m["name"], m["source"]) for m in memberships] == [("unattended", "dismissed")]
    # A dismissed membership applies no tag.
    assert await store.get_tags_for_users([alice]) == {}
    user = await store.get_user_by_id(alice)
    assert user["user_tags"] == []
    # Removing the marker forgets the dismissal.
    assert await store.remove_user_tag_member("unattended", alice, removed_by=None) == "dismissed"
    assert await store.get_user_tag_memberships(alice) == []
    # Adding by hand turns a dismissed membership into a manual one.
    await store.add_user_tag_members("unattended", [alice], assigned_by=None)
    await store.remove_user_tag_member("unattended", alice, removed_by=None)
    changed, _ = await store.add_user_tag_members("unattended", [alice], assigned_by="admin-2")
    assert changed == [alice]
    assert (await store.get_user_tag_memberships(alice))[0]["source"] == "manual"


async def test_every_auth_lookup_holds_the_account_to_its_tags(store):
    await store.create_user_tag(
        "tagtest-limits",
        {
            "quota_daily_cost": Decimal("5"),
            "quota_monthly_cost": Decimal("50"),
            "accounting_currency": "USD",
            "max_concurrent_requests": 2,
            "allowed_models": ["m1"],
        },
    )
    alice = await _user(store, "alice", key=True, quota="100")
    async with store._pool.acquire() as conn:
        # Spent earlier this month, so the month's spend shows only if it is read.
        await conn.execute(
            "INSERT INTO user_daily_cost (user_id, day, cost_usd) "
            "VALUES ($1, to_char(NOW() AT TIME ZONE 'UTC', 'YYYY-MM-DD'), 7)",
            alice,
        )

    before = await store.get_auth_context_with_spend(f"hash-{alice}")
    assert before["user_tags"] == []
    assert before["quota_daily_cost_usd"] == Decimal("100")
    assert before["quota_monthly_cost_usd"] is None

    await store.add_user_tag_members("tagtest-limits", [alice], assigned_by=None)

    row = await store.get_auth_context_with_spend(f"hash-{alice}")
    assert row["user_tags"] == ["tagtest-limits"]
    assert row["quota_daily_cost_usd"] == Decimal("5")
    assert row["quota_monthly_cost_usd"] == Decimal("50")
    # The account has no monthly quota of its own; the tag's makes the month's
    # spend part of the one lookup.
    assert row["account_spent_month_usd"] == Decimal("7")
    assert row["tag_max_concurrent_requests"] == 2
    assert row["tag_allowed_models"] == ["m1"]

    identity = await store.get_auth_context_by_key_hash(f"hash-{alice}")
    assert identity["quota_daily_cost_usd"] == Decimal("5")
    assert identity["tag_allowed_models"] == ["m1"]
    light = await store.get_auth_context_lightweight(f"hash-{alice}")
    assert light["tag_allowed_models"] == ["m1"]
    assert light["tag_max_concurrent_requests"] == 2
    (quota_row,) = await store.get_quota_context_for_user(alice)
    assert quota_row["quota_daily_cost_usd"] == Decimal("5")
    assert quota_row["quota_monthly_cost_usd"] == Decimal("50")
    user = await store.get_user_by_id(alice)
    assert user["user_tags"] == ["tagtest-limits"]
    assert user["tag_max_concurrent_requests"] == 2
    assert user["tag_quota_daily_cost"] == Decimal("5")

    # A tag quota above the account's own never loosens it.
    await store.update_user_tag("tagtest-limits", {"quota_daily_cost": Decimal("500")})
    row = await store.get_auth_context_with_spend(f"hash-{alice}")
    assert row["quota_daily_cost_usd"] == Decimal("100")


async def test_the_rule_counts_distinct_recent_hours_and_respects_a_dismissal(store):
    await store.create_user_tag(
        "tagtest-auto",
        {"auto_enabled": True, "auto_window_hours": 12, "auto_min_active_hours": 12},
    )
    every_hour = await _user(store, "every-hour")
    with_a_gap = await _user(store, "with-a-gap")
    batch_only = await _user(store, "batch-only")
    hours = [i + 0.1 for i in range(12)]
    await _requests(store, every_hour, [*hours, 0.2, 0.3])
    await _requests(store, with_a_gap, [h for h in hours if int(h) != 5])
    await _requests(store, batch_only, hours, batch=True)

    preview = await store.preview_user_tag_members(
        "tagtest-auto", window_hours=12, min_active_hours=12, limit=1000
    )
    matched = {row["user_id"]: row for row in preview}
    assert matched[every_hour]["active_hours"] == 12
    assert matched[every_hour]["member_source"] is None
    assert with_a_gap not in matched
    assert batch_only not in matched
    # Eleven of the twelve hours match a looser rule.
    looser = await store.preview_user_tag_members(
        "tagtest-auto", window_hours=12, min_active_hours=11, limit=1000
    )
    assert {every_hour, with_a_gap} <= {row["user_id"] for row in looser}

    added = await store.detect_user_tag_members("tagtest-auto")
    assert every_hour in added and with_a_gap not in added
    assert (await store.get_user_tag_memberships(every_hour))[0]["source"] == "auto"
    # A second run adds nothing new.
    assert every_hour not in await store.detect_user_tag_members("tagtest-auto")

    # Dismissed: the requests before the dismissal no longer count.
    await store.remove_user_tag_member("tagtest-auto", every_hour, removed_by="admin-1")
    assert every_hour not in await store.detect_user_tag_members("tagtest-auto")
    # Twelve hours of requests after the dismissal bring it back.
    async with store._pool.acquire() as conn:
        await conn.execute(
            "UPDATE user_tag_members SET assigned_at = NOW() - INTERVAL '13 hours' "
            "WHERE user_id = $1 AND tag = 'tagtest-auto'",
            every_hour,
        )
    assert every_hour in await store.detect_user_tag_members("tagtest-auto")


async def test_the_rule_never_matches_a_service_account(store):
    """A bot is unattended by design: the rule leaves it out, a hand still tags it."""
    await store.create_user_tag(
        "tagtest-auto",
        {"auto_enabled": True, "auto_window_hours": 12, "auto_min_active_hours": 12},
    )
    person = await _user(store, "person")
    bot = f"{PREFIX}bot"
    await store.create_user(
        user_id=bot,
        email=None,
        password_hash="x",
        login_name="tagtest-bot",
        is_service_account=True,
    )
    hours = [i + 0.1 for i in range(12)]
    await _requests(store, person, hours)
    await _requests(store, bot, hours)

    preview = await store.preview_user_tag_members(
        "tagtest-auto", window_hours=12, min_active_hours=12, limit=1000
    )
    previewed = {row["user_id"] for row in preview}
    assert person in previewed
    assert bot not in previewed

    added = await store.detect_user_tag_members("tagtest-auto")
    assert person in added
    assert bot not in added
    assert await store.get_user_tag_memberships(bot) == []

    # An administrator can still put the tag on it by hand, and the rule keeps it.
    changed, unknown = await store.add_user_tag_members(
        "tagtest-auto", [bot], assigned_by="admin-1"
    )
    assert changed == [bot] and unknown == []
    await store.detect_user_tag_members("tagtest-auto")
    assert (await store.get_user_tag_memberships(bot))[0]["source"] == "manual"


async def test_the_scheduled_run_applies_every_rule_that_is_on(store):
    await store.create_user_tag(
        "tagtest-auto",
        {"auto_enabled": True, "auto_window_hours": 3, "auto_min_active_hours": 3},
    )
    await store.create_user_tag(
        "tagtest-hand", {"auto_window_hours": 3, "auto_min_active_hours": 3}
    )
    busy = await _user(store, "busy")
    await _requests(store, busy, [0.1, 1.1, 2.1])

    added = await run_rules(store._pool)
    assert added is not None
    assert busy in added["tagtest-auto"]
    assert "tagtest-hand" not in added
    assert await store.get_tags_for_users([busy]) == {busy: ["tagtest-auto"]}


async def test_a_rule_turned_off_or_a_deleted_tag_is_left_alone(store):
    await store.create_user_tag(
        "tagtest-hand", {"auto_window_hours": 3, "auto_min_active_hours": 3}
    )
    busy = await _user(store, "busy")
    await _requests(store, busy, [0.1, 1.1, 2.1])
    assert await store.detect_user_tag_members("tagtest-hand") is None
    assert await store.detect_user_tag_members("tagtest-missing") is None
    assert await store.get_tags_for_users([busy]) == {}


async def test_a_run_releases_its_lock(store):
    """The run lock is a session lock on a pooled connection: it must not stay with it."""
    for _ in range(3):
        assert await run_rules(store._pool) is not None


async def test_the_users_list_filters_by_tag(store):
    await store.create_user_tag("tagtest-hand", {})
    alice = await _user(store, "alice")
    await _user(store, "bob")
    await store.add_user_tag_members("tagtest-hand", [alice], assigned_by=None)
    total, rows, _counts = await store.list_users(tag="tagtest-hand", limit=50)
    assert total == 1
    assert [row["id"] for row in rows] == [alice]
