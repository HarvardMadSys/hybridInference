"""Organizations in the store, against real PostgreSQL.

The tests share one database with the rest of the tier, so every row they
write is tagged (``o-itest-*`` organizations, ``u-org-*`` accounts,
``itest-*`` providers) and removed again.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio

from serving.audit import AuditActor
from serving.storage import organizations as orgs, sso as sso_store
from serving.storage.cache import CachedOperationalStore, InMemoryCache
from serving.storage.postgres_operational import PostgresOperationalStore
from tests.fixtures import postgres

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

PLATFORM = AuditActor.admin("u-org-root", "192.0.2.10")
ORG_ADMIN = AuditActor.user("u-org-admin", "192.0.2.20")


@pytest_asyncio.fixture
async def store():
    pool = await postgres.create_pool(min_size=1, max_size=4)
    store = PostgresOperationalStore(pool)
    await store.initialize()

    async def _wipe() -> None:
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM admin_audit_log WHERE organization_id LIKE 'o-itest-%' "
                "OR target_user_id LIKE 'u-org-%' OR actor_id LIKE 'u-org-%'"
            )
            await conn.execute("DELETE FROM sso_organizations WHERE id LIKE 'o-itest-%'")
            await conn.execute("DELETE FROM api_keys WHERE user_id LIKE 'u-org-%'")
            await conn.execute("DELETE FROM users WHERE id LIKE 'u-org-%'")

    await _wipe()
    try:
        yield store, pool
    finally:
        await _wipe()
        await pool.close()


async def _account(store, name: str, *, status: str = "active", role: str = "free") -> str:
    user_id = f"u-org-{name}"
    await store.create_user(
        user_id=user_id,
        email=f"{name}@org-itest.example.edu",
        password_hash=None,
        user_name=name.title(),
        email_verified=True,
        status=status,
    )
    if role != "free":
        await store.update_user_fields(user_id, role=role)
    return user_id


async def _organization(store, name: str, *connections, limit=None) -> str:
    organization_id = f"o-itest-{name}"
    outcome, _added = await store.create_organization(
        organization_id=organization_id,
        name=f"Itest {name}",
        monthly_limit_usd=limit,
        connections=connections,
        actor=PLATFORM,
    )
    assert outcome == orgs.CREATED
    return organization_id


async def _audit(pool, organization_id: str) -> list[dict]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT action, actor_type, actor_id, target_type, target_id, target_user_id, "
            "details::text AS details FROM admin_audit_log "
            "WHERE organization_id = $1 ORDER BY id",
            organization_id,
        )
    return [dict(row) for row in rows]


async def test_an_organization_is_created_with_its_connections_and_members(store):
    op_store, pool = store
    first, second = await _account(op_store, "a1"), await _account(op_store, "a2")
    await _account(op_store, "a3")

    outcome, added = await op_store.create_organization(
        organization_id="o-itest-a",
        name="Itest A",
        monthly_limit_usd=Decimal("1000"),
        connections=[("itest-a-campus", [first, second, "u-org-nobody"])],
        actor=PLATFORM,
    )

    assert (outcome, added) == (orgs.CREATED, 2)
    organization = await op_store.get_organization("o-itest-a")
    assert organization["name"] == "Itest A"
    assert organization["status"] == "active"
    assert organization["member_count"] == 2
    assert organization["provider_ids"] == ["itest-a-campus"]
    assert organization["monthly_limit_usd"] == Decimal("1000")
    assert organization["default_member_monthly_limit_usd"] is None
    assert await op_store.get_provider_organization("itest-a-campus") == "o-itest-a"

    membership = await op_store.get_organization_membership(first)
    assert (membership["organization_id"], membership["role"]) == ("o-itest-a", "member")
    assert membership["organization_name"] == "Itest A"
    assert membership["removed_at"] is None
    assert await op_store.get_organization_membership("u-org-a3") is None

    [row] = await _audit(pool, "o-itest-a")
    assert row["action"] == "organization.create"
    assert (row["actor_type"], row["actor_id"]) == ("admin", "u-org-root")
    assert (row["target_type"], row["target_id"]) == ("organization", "o-itest-a")
    assert row["target_user_id"] is None
    assert '"members_added": 2' in row["details"]


async def test_a_name_and_a_connection_are_taken_once(store):
    op_store, pool = store
    await _organization(op_store, "b1", ("itest-b-campus", []))

    same_name = await op_store.create_organization(
        organization_id="o-itest-b2", name="ITEST B1", actor=PLATFORM
    )
    same_provider = await op_store.create_organization(
        organization_id="o-itest-b3",
        name="Itest b3",
        connections=[("itest-b-campus", [])],
        actor=PLATFORM,
    )

    assert same_name == (orgs.NAME_TAKEN, 0)
    assert same_provider == (orgs.CONNECTION_TAKEN, 0)
    # Nothing of the refused organizations was written, nor an audit row for them.
    assert await op_store.get_organization("o-itest-b2") is None
    assert await op_store.get_organization("o-itest-b3") is None
    assert await _audit(pool, "o-itest-b3") == []
    assert [
        o["id"] for o in await op_store.list_organizations() if o["id"].startswith("o-itest-b")
    ] == ["o-itest-b1"]


async def test_attaching_adds_only_accounts_that_are_in_no_organization(store):
    op_store, pool = store
    first, second = await _account(op_store, "c1"), await _account(op_store, "c2")
    await _organization(op_store, "c1", ("itest-c-one", [first]))
    await _organization(op_store, "c2")

    attached = await op_store.attach_organization_connection(
        "o-itest-c2", "itest-c-two", user_ids=[first, second], actor=PLATFORM
    )
    taken = await op_store.attach_organization_connection(
        "o-itest-c2", "itest-c-one", user_ids=[], actor=PLATFORM
    )
    missing = await op_store.attach_organization_connection(
        "o-itest-nowhere", "itest-c-three", user_ids=[], actor=PLATFORM
    )
    again = await op_store.attach_organization_connection(
        "o-itest-c2", "itest-c-two", user_ids=[second], actor=PLATFORM
    )

    assert attached == (orgs.UPDATED, 1)
    assert taken == (orgs.CONNECTION_TAKEN, 0)
    assert missing == (orgs.NOT_FOUND, 0)
    # Attaching what is attached already only adds the members it lacks: none here.
    assert again == (orgs.UPDATED, 0)
    assert (await op_store.get_organization_membership(first))["organization_id"] == "o-itest-c1"
    assert (await op_store.get_organization_membership(second))["organization_id"] == "o-itest-c2"
    actions = [row["action"] for row in await _audit(pool, "o-itest-c2")]
    assert actions == [
        "organization.create",
        "organization.connection.attach",
        "organization.connection.attach",
    ]


async def test_detaching_a_connection_leaves_its_members(store):
    op_store, pool = store
    member = await _account(op_store, "d1")
    await _organization(op_store, "d", ("itest-d-campus", [member]))

    detached = await op_store.detach_organization_connection(
        "o-itest-d", "itest-d-campus", actor=PLATFORM
    )
    second = await op_store.detach_organization_connection(
        "o-itest-d", "itest-d-campus", actor=PLATFORM
    )

    assert (detached, second) == (True, False)
    assert await op_store.get_provider_organization("itest-d-campus") is None
    assert (await op_store.get_organization("o-itest-d"))["member_count"] == 1
    assert [row["action"] for row in await _audit(pool, "o-itest-d")][-1] == (
        "organization.connection.detach"
    )


async def test_a_first_sign_in_through_a_connection_creates_a_member(store):
    op_store, _pool = store
    await _organization(op_store, "e", ("itest-e-campus", []))

    def _new(name: str, provider_id: str) -> dict:
        return {
            "user_id": f"u-org-{name}",
            "email": f"{name}@org-itest.example.edu",
            "user_name": name,
            "role": "free",
            "status": "active",
            "identity_id": f"i-org-{name}",
            "provider_id": provider_id,
            "issuer": f"https://{provider_id}.example.edu",
            "subject": f"sub-{name}",
        }

    assert await op_store.create_sso_user(**_new("e1", "itest-e-campus")) is True
    assert await op_store.create_sso_user(**_new("e2", "itest-e-unattached")) is True

    membership = await op_store.get_organization_membership("u-org-e1")
    assert (membership["organization_id"], membership["role"]) == ("o-itest-e", "member")
    assert await op_store.get_organization_membership("u-org-e2") is None


async def test_a_link_makes_a_member_and_a_second_organization_is_refused(store):
    op_store, pool = store
    account = await _account(op_store, "f1")
    await _organization(op_store, "f1", ("itest-f-one", []))
    await _organization(op_store, "f2", ("itest-f-two", []))

    def _link(provider_id: str) -> dict:
        return {
            "identity_id": f"i-org-f-{provider_id}",
            "user_id": account,
            "provider_id": provider_id,
            "issuer": f"https://{provider_id}.example.edu",
            "subject": "sub-f1",
            "email": "f1@org-itest.example.edu",
        }

    first = await op_store.link_user_identity(**_link("itest-f-one"))
    second = await op_store.link_user_identity(**_link("itest-f-two"))

    assert first == sso_store.LINKED
    assert second == sso_store.OTHER_ORGANIZATION
    assert (await op_store.get_organization_membership(account))["organization_id"] == "o-itest-f1"
    async with pool.acquire() as conn:
        issuers = await conn.fetch("SELECT issuer FROM user_identities WHERE user_id = $1", account)
    # The refused link wrote no identity.
    assert [row["issuer"] for row in issuers] == ["https://itest-f-one.example.edu"]


async def test_identities_outside_every_organization_are_listed_for_the_member_rule(store):
    op_store, _pool = store
    inside, outside = await _account(op_store, "g1"), await _account(op_store, "g2")
    await _organization(op_store, "g", ("itest-g-campus", []))
    for account, provider_id in ((inside, "itest-g-campus"), (outside, "itest-g-other")):
        await op_store.link_user_identity(
            identity_id=f"i-org-{account}",
            user_id=account,
            provider_id=provider_id,
            issuer=f"https://{provider_id}.example.edu",
            subject=account,
            email=f"{account}@elsewhere.example.org",
        )

    listed = {
        row["user_id"]: row
        for row in await op_store.list_identities_without_organization()
        if row["user_id"].startswith("u-org-g")
    }

    # The linked account became a member with its link, so only the other is left.
    assert set(listed) == {outside}
    assert listed[outside]["provider_id"] == "itest-g-other"
    assert listed[outside]["email"] == f"{outside}@elsewhere.example.org"
    assert listed[outside]["account_email"] == "g2@org-itest.example.edu"


async def test_the_administrator_role_is_given_and_taken_back(store):
    op_store, pool = store
    member = await _account(op_store, "h1")
    await _organization(op_store, "h", ("itest-h-campus", [member]))

    given = await op_store.set_organization_member_role(
        "o-itest-h", member, "admin", actor=PLATFORM
    )
    again = await op_store.set_organization_member_role(
        "o-itest-h", member, "admin", actor=PLATFORM
    )
    stranger = await op_store.set_organization_member_role(
        "o-itest-h", "u-org-nobody", "admin", actor=PLATFORM
    )
    taken = await op_store.set_organization_member_role(
        "o-itest-h", member, "member", actor=PLATFORM
    )

    assert (given, again, stranger, taken) == (
        orgs.UPDATED,
        orgs.UNCHANGED,
        orgs.NOT_FOUND,
        orgs.UPDATED,
    )
    rows = await _audit(pool, "o-itest-h")
    assert [row["action"] for row in rows] == [
        "organization.create",
        "organization.admin.add",
        "organization.admin.remove",
    ]
    assert (rows[1]["target_type"], rows[1]["target_user_id"]) == ("user", member)
    with pytest.raises(ValueError, match="unknown organization role"):
        await op_store.set_organization_member_role("o-itest-h", member, "owner", actor=PLATFORM)


async def test_removing_a_member_suspends_the_account_and_restoring_undoes_only_a_removal(store):
    raw_store, pool = store
    op_store = CachedOperationalStore(raw_store, InMemoryCache())
    member = await _account(op_store, "i1")
    await _organization(op_store, "i", ("itest-i-campus", [member]))
    await op_store.create_key(
        key_hash=secrets.token_hex(16), key_prefix="sk-itest", user_id=member, account_id=member
    )
    await op_store.create_session(
        session_id=f"s-{secrets.token_hex(6)}",
        user_id=member,
        refresh_token_hash=secrets.token_hex(16),
        jti=secrets.token_hex(8),
        sid=secrets.token_hex(8),
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )

    removed = await op_store.remove_organization_member(
        "o-itest-i", member, suspension_message="Removed by Itest i.", actor=ORG_ADMIN
    )

    assert removed == orgs.UPDATED
    async with pool.acquire() as conn:
        account = await conn.fetchrow(
            "SELECT status, suspension_message FROM users WHERE id = $1", member
        )
        keys = await conn.fetch("SELECT status FROM api_keys WHERE user_id = $1", member)
        sessions = await conn.fetch("SELECT revoked FROM auth_sessions WHERE user_id = $1", member)
    assert dict(account) == {"status": "suspended", "suspension_message": "Removed by Itest i."}
    assert [row["status"] for row in keys] == ["revoked"]
    assert [row["revoked"] for row in sessions] == [True]
    membership = await op_store.get_organization_membership(member)
    assert membership["removed_at"] is not None
    assert membership["removed_by"] == "u-org-admin"
    # A removed member is not removed a second time.
    assert (
        await op_store.remove_organization_member("o-itest-i", member, actor=ORG_ADMIN)
        == orgs.NOT_ACTIVE
    )

    restored = await op_store.restore_organization_member("o-itest-i", member, actor=ORG_ADMIN)

    assert restored == orgs.UPDATED
    async with pool.acquire() as conn:
        account = await conn.fetchrow(
            "SELECT status, suspension_message FROM users WHERE id = $1", member
        )
        keys = await conn.fetch("SELECT status FROM api_keys WHERE user_id = $1", member)
    assert dict(account) == {"status": "active", "suspension_message": None}
    # The revoked keys stay revoked: the member makes new ones.
    assert [row["status"] for row in keys] == ["revoked"]
    assert (await op_store.get_organization_membership(member))["removed_at"] is None
    assert (
        await op_store.restore_organization_member("o-itest-i", member, actor=ORG_ADMIN)
        == orgs.NOT_REMOVED
    )
    rows = await _audit(pool, "o-itest-i")
    assert [row["action"] for row in rows] == [
        "organization.create",
        "organization.member.remove",
        "organization.member.restore",
    ]
    assert (rows[1]["actor_type"], rows[1]["actor_id"]) == ("user", "u-org-admin")
    assert rows[1]["target_user_id"] == member


async def test_a_suspension_by_the_platform_is_not_the_organizations_to_undo(store):
    op_store, _pool = store
    member = await _account(op_store, "j1")
    await _organization(op_store, "j", ("itest-j-campus", [member]))

    # Suspended in Admin, never removed by the organization.
    await op_store.suspend_user(member)
    never_removed = await op_store.restore_organization_member("o-itest-j", member, actor=ORG_ADMIN)

    # Removed by the organization, made active in Admin, then suspended there.
    await op_store.update_user_fields(member, status="active")
    await op_store.remove_organization_member("o-itest-j", member, actor=ORG_ADMIN)
    await op_store.update_user_fields(member, status="active")
    await op_store.clear_organization_member_removal(member)
    await op_store.suspend_user(member)
    after_admin = await op_store.restore_organization_member("o-itest-j", member, actor=ORG_ADMIN)

    # Removed by the organization, then suspended again in Admin without a reactivation.
    await op_store.update_user_fields(member, status="active")
    await op_store.remove_organization_member("o-itest-j", member, actor=ORG_ADMIN)
    await op_store.suspend_user(member)
    after_second = await op_store.restore_organization_member("o-itest-j", member, actor=ORG_ADMIN)

    assert (never_removed, after_admin, after_second) == (
        orgs.NOT_REMOVED,
        orgs.NOT_REMOVED,
        orgs.NOT_REMOVED,
    )


async def test_removal_is_refused_for_administrators_and_accounts_that_are_not_active(store):
    op_store, pool = store
    org_admin = await _account(op_store, "k1")
    platform_admin = await _account(op_store, "k2", role="admin")
    pending = await _account(op_store, "k3", status="pending_approval")
    outsider = await _account(op_store, "k4")
    await _organization(op_store, "k", ("itest-k-campus", [org_admin, platform_admin, pending]))
    await op_store.set_organization_member_role("o-itest-k", org_admin, "admin", actor=PLATFORM)

    outcomes = [
        await op_store.remove_organization_member("o-itest-k", account, actor=ORG_ADMIN)
        for account in (org_admin, platform_admin, pending, outsider)
    ]

    assert outcomes == [orgs.PROTECTED, orgs.PROTECTED, orgs.NOT_ACTIVE, orgs.NOT_FOUND]
    async with pool.acquire() as conn:
        statuses = await conn.fetch(
            "SELECT status FROM users WHERE id = ANY($1::text[]) ORDER BY id",
            [org_admin, platform_admin, pending, outsider],
        )
    assert [row["status"] for row in statuses] == [
        "active",
        "active",
        "pending_approval",
        "active",
    ]


async def test_the_limits_are_set_cleared_and_audited(store):
    op_store, pool = store
    await _organization(op_store, "l", limit=Decimal("1000"))

    lowered = await op_store.set_organization_limit(
        "o-itest-l",
        "monthly_limit",
        usd=Decimal("800"),
        action="organization.limit.set",
        actor=PLATFORM,
    )
    same = await op_store.set_organization_limit(
        "o-itest-l",
        "monthly_limit",
        usd=Decimal("800"),
        action="organization.limit.set",
        actor=PLATFORM,
    )
    default = await op_store.set_organization_limit(
        "o-itest-l",
        "default_member_monthly_limit",
        usd=Decimal("20"),
        action="organization.default_member_limit.set",
        actor=ORG_ADMIN,
    )
    cleared = await op_store.set_organization_limit(
        "o-itest-l",
        "default_member_monthly_limit",
        usd=None,
        action="organization.default_member_limit.set",
        actor=ORG_ADMIN,
    )
    missing = await op_store.set_organization_limit(
        "o-itest-nowhere",
        "monthly_limit",
        usd=Decimal("1"),
        action="organization.limit.set",
        actor=PLATFORM,
    )

    assert lowered == (orgs.UPDATED, Decimal("1000"), Decimal("800"))
    assert same == (orgs.UNCHANGED, Decimal("800"), Decimal("800"))
    assert default == (orgs.UPDATED, None, Decimal("20"))
    assert cleared == (orgs.UPDATED, Decimal("20"), None)
    assert missing == (orgs.NOT_FOUND, None, None)
    organization = await op_store.get_organization("o-itest-l")
    assert organization["monthly_limit_usd"] == Decimal("800")
    assert organization["monthly_limit_amount"] == Decimal("800")
    assert organization["default_member_monthly_limit_usd"] is None
    rows = await _audit(pool, "o-itest-l")
    assert [row["action"] for row in rows] == [
        "organization.create",
        "organization.limit.set",
        "organization.default_member_limit.set",
        "organization.default_member_limit.set",
    ]
    assert '"old": "1000' in rows[1]["details"]
    assert '"new": "800' in rows[1]["details"]
    with pytest.raises(ValueError, match="unknown organization limit"):
        await op_store.set_organization_limit(
            "o-itest-l", "daily_limit", usd=1, action="x", actor=PLATFORM
        )


async def test_an_organization_is_renamed_unless_the_name_is_another_ones(store):
    op_store, pool = store
    await _organization(op_store, "m1")
    await _organization(op_store, "m2")

    renamed = await op_store.rename_organization("o-itest-m1", "Itest M One", actor=PLATFORM)
    same = await op_store.rename_organization("o-itest-m1", "Itest M One", actor=PLATFORM)
    taken = await op_store.rename_organization("o-itest-m1", "itest M2", actor=PLATFORM)
    missing = await op_store.rename_organization("o-itest-nowhere", "Anything", actor=PLATFORM)

    assert (renamed, same, taken, missing) == (
        orgs.UPDATED,
        orgs.UNCHANGED,
        orgs.NAME_TAKEN,
        orgs.NOT_FOUND,
    )
    assert (await op_store.get_organization("o-itest-m1"))["name"] == "Itest M One"
    assert [row["action"] for row in await _audit(pool, "o-itest-m1")] == [
        "organization.create",
        "organization.update",
    ]


async def test_the_audit_log_lists_the_rows_of_one_organization(store):
    op_store, _pool = store
    await _organization(op_store, "n1")
    await _organization(op_store, "n2")
    await op_store.rename_organization("o-itest-n1", "Itest N One", actor=PLATFORM)

    total, rows = await op_store.list_audit_log(organization_id="o-itest-n1")
    other_total, _ = await op_store.list_audit_log(organization_id="o-itest-n2")

    assert total == 2
    assert {row["organization_id"] for row in rows} == {"o-itest-n1"}
    assert [row["action"] for row in rows] == ["organization.update", "organization.create"]
    assert other_total == 1
