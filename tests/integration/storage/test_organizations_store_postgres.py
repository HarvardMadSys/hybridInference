"""Organizations in the store, against real PostgreSQL.

The tests share one database with the rest of the tier, so every row they
write is tagged (``o-itest-*`` organizations, ``u-org-*`` accounts,
``itest-*`` providers) and removed again.
"""

from __future__ import annotations

import asyncio
import secrets
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio

from serving import organizations as organization_rules
from serving.audit import AuditActor
from serving.storage import organizations as orgs, sso as sso_store
from serving.storage.base import CostIncrement
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
            await conn.execute("DELETE FROM spend_counters WHERE scope_id LIKE 'o:o-itest-%'")
            await conn.execute("DELETE FROM api_logs WHERE user_id LIKE 'u-org-%'")
            await conn.execute("DELETE FROM user_daily_cost WHERE user_id LIKE 'u-org-%'")
            await conn.execute("DELETE FROM api_keys WHERE user_id LIKE 'u-org-%'")
            # No foreign key takes a session away with its account.
            await conn.execute("DELETE FROM auth_sessions WHERE user_id LIKE 'u-org-%'")
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


def _only(*user_ids: str):
    """The rule of a connection that makes exactly *user_ids* members."""
    return lambda _identities: list(user_ids)


async def _organization(store, name: str, *connections, limit=None) -> str:
    """An organization; *connections* pairs each provider id with its members."""
    organization_id = f"o-itest-{name}"
    outcome, _added = await store.create_organization(
        organization_id=organization_id,
        name=f"Itest {name}",
        monthly_limit_usd=limit,
        connections=[(provider_id, _only(*user_ids)) for provider_id, user_ids in connections],
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
        connections=[("itest-a-campus", _only(first, second, "u-org-nobody"))],
        actor=PLATFORM,
    )

    assert (outcome, added) == (orgs.CREATED, 2)
    organization = await op_store.get_organization("o-itest-a")
    assert organization["name"] == "Itest A"
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
        connections=[("itest-b-campus", _only())],
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
        "o-itest-c2", "itest-c-two", accounts_of=_only(first, second), actor=PLATFORM
    )
    taken = await op_store.attach_organization_connection(
        "o-itest-c2", "itest-c-one", accounts_of=_only(), actor=PLATFORM
    )
    missing = await op_store.attach_organization_connection(
        "o-itest-nowhere", "itest-c-three", accounts_of=_only(), actor=PLATFORM
    )
    again = await op_store.attach_organization_connection(
        "o-itest-c2", "itest-c-two", accounts_of=_only(second), actor=PLATFORM
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


async def test_the_rule_of_a_connection_is_given_the_identities_outside_every_organization(store):
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
    given: dict[str, dict] = {}

    def rule(identities):
        given.update(
            (row["user_id"], row) for row in identities if row["user_id"].startswith("u-org-g")
        )
        return [outside]

    attached = await op_store.attach_organization_connection(
        "o-itest-g", "itest-g-other", accounts_of=rule, actor=PLATFORM
    )

    # The linked account became a member with its link, so only the other is left.
    assert set(given) == {outside}
    assert given[outside]["provider_id"] == "itest-g-other"
    assert given[outside]["issuer"] == "https://itest-g-other.example.edu"
    assert given[outside]["email"] == f"{outside}@elsewhere.example.org"
    assert given[outside]["account_email"] == "g2@org-itest.example.edu"
    # And the rule's answer is who the connection makes members.
    assert attached == (orgs.UPDATED, 1)
    assert (await op_store.get_organization_membership(outside))["organization_id"] == "o-itest-g"


# -- a sign-in and an attach at the same time ----------------------------------------
#
# Each looks for what the other writes, so one that reads while the other is
# still writing would miss it: the account would have the identity, the
# organization the connection, and nobody the membership. These hold one of the
# two open at the moment it has read and not yet committed, and start the other.


def _of(provider_id: str):
    """The rule of *provider_id* in these tests: the accounts with an identity of it."""
    return lambda identities: [
        row["user_id"] for row in identities if row["provider_id"] == provider_id
    ]


async def _waits_for_the_other(pool, task: asyncio.Task) -> bool:
    """Whether *task* comes to wait for the membership lock, rather than to its end."""
    for _ in range(500):
        if task.done():
            return False
        async with pool.acquire() as conn:
            waiting = await conn.fetchval(
                "SELECT 1 FROM pg_locks l JOIN pg_database d ON d.oid = l.database "
                "WHERE l.locktype = 'advisory' AND NOT l.granted "
                "AND d.datname = current_database()"
            )
        if waiting:
            return True
        await asyncio.sleep(0.01)
    return False


def _sign_in(op_store, kind: str, name: str, provider_id: str):
    """A sign-in that makes account *name* (``first``) or links it (``link``)."""
    identity = {
        "identity_id": f"i-org-{name}",
        "provider_id": provider_id,
        "issuer": f"https://{provider_id}.example.edu",
        "subject": f"sub-{name}",
    }
    if kind == "link":
        return op_store.link_user_identity(
            user_id=f"u-org-{name}", email=f"{name}@org-itest.example.edu", **identity
        )
    return op_store.create_sso_user(
        user_id=f"u-org-{name}",
        email=f"{name}@org-itest.example.edu",
        user_name=name,
        role="free",
        status="active",
        **identity,
    )


@pytest.mark.parametrize("kind", ["first", "link"])
async def test_a_sign_in_during_an_attach_waits_and_becomes_a_member(store, monkeypatch, kind):
    op_store, pool = store
    if kind == "link":
        await _account(op_store, "x1")
    await _organization(op_store, "x")
    attach_connection = orgs._attach
    read, go_on = asyncio.Event(), asyncio.Event()

    async def read_then_wait(conn, *arguments):
        read.set()
        await go_on.wait()
        return await attach_connection(conn, *arguments)

    monkeypatch.setattr(orgs, "_attach", read_then_wait)
    attach = asyncio.create_task(
        op_store.attach_organization_connection(
            "o-itest-x", "itest-x", accounts_of=_of("itest-x"), actor=PLATFORM
        )
    )
    try:
        # The attach has read the identities, without this one, and not written the connection.
        await asyncio.wait_for(read.wait(), 10)
        sign_in = asyncio.create_task(_sign_in(op_store, kind, "x1", "itest-x"))
        waited = await _waits_for_the_other(pool, sign_in)
    finally:
        go_on.set()

    assert await attach == (orgs.UPDATED, 0)
    assert await sign_in in (True, sso_store.LINKED)
    assert waited
    # It then found the connection, and joined by itself.
    assert (await op_store.get_organization_membership("u-org-x1"))["organization_id"] == (
        "o-itest-x"
    )


@pytest.mark.parametrize("kind", ["first", "link"])
@pytest.mark.parametrize("write", ["attach", "create"])
async def test_an_attach_during_a_sign_in_waits_and_finds_the_account(
    store, monkeypatch, kind, write
):
    op_store, pool = store
    if kind == "link":
        await _account(op_store, "y1")
    if write == "attach":
        await _organization(op_store, "y")
    join = sso_store.join_organization
    looked, go_on = asyncio.Event(), asyncio.Event()

    async def look_then_wait(conn, **who):
        made = await join(conn, **who)
        looked.set()
        await go_on.wait()
        return made

    monkeypatch.setattr(sso_store, "join_organization", look_then_wait)
    sign_in = asyncio.create_task(_sign_in(op_store, kind, "y1", "itest-y"))
    try:
        # The sign-in has looked for the connection, found none, and not committed.
        await asyncio.wait_for(looked.wait(), 10)
        if write == "attach":
            attach = asyncio.create_task(
                op_store.attach_organization_connection(
                    "o-itest-y", "itest-y", accounts_of=_of("itest-y"), actor=PLATFORM
                )
            )
        else:
            attach = asyncio.create_task(
                op_store.create_organization(
                    organization_id="o-itest-y",
                    name="Itest y",
                    connections=[("itest-y", _of("itest-y"))],
                    actor=PLATFORM,
                )
            )
        waited = await _waits_for_the_other(pool, attach)
    finally:
        go_on.set()

    assert await sign_in in (True, sso_store.LINKED)
    # It then read the identity the sign-in committed, and made the member.
    assert await attach == (orgs.UPDATED if write == "attach" else orgs.CREATED, 1)
    assert waited
    assert (await op_store.get_organization_membership("u-org-y1"))["organization_id"] == (
        "o-itest-y"
    )


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


async def test_a_member_row_says_which_accounts_the_organization_cannot_manage(store):
    op_store, _pool = store
    org_admin = await _account(op_store, "x1")
    platform_admin = await _account(op_store, "x2", role="admin")
    plain = await _account(op_store, "x3")
    await _organization(op_store, "x", ("itest-x", [org_admin, platform_admin, plain]))
    await op_store.set_organization_member_role("o-itest-x", org_admin, "admin", actor=PLATFORM)

    _total, members = await organization_rules.members_view(op_store, "o-itest-x", "7d")

    # The same accounts that a removal and a limit refuse with "protected".
    assert {member["user_id"]: member["protected"] for member in members} == {
        org_admin: True,
        platform_admin: True,
        plain: False,
    }


async def test_a_status_lists_the_members_that_are_shown_with_its_word(store):
    op_store, _pool = store
    active = await _account(op_store, "y1")
    removed = await _account(op_store, "y2")
    suspended = await _account(op_store, "y3")
    pending = await _account(op_store, "y4", status="pending_approval")
    await _organization(op_store, "y", ("itest-y", [active, removed, suspended, pending]))
    await op_store.remove_organization_member("o-itest-y", removed, actor=ORG_ADMIN)
    # Suspended in Admin, not removed by the organization.
    await op_store.suspend_user(suspended)
    today = datetime.now(UTC).date()

    async def listed(status: str | None) -> tuple[int, set[str]]:
        total, rows = await op_store.list_organization_members(
            "o-itest-y", first_day=today, last_day=today, status=status
        )
        return total, {row["user_id"] for row in rows}

    assert await listed(None) == (4, {active, removed, suspended, pending})
    assert await listed("active") == (1, {active})
    # A removed member is shown as "Removed": the suspended ones do not list it.
    assert await listed("suspended") == (1, {suspended})
    assert await listed("removed") == (1, {removed})
    assert await listed("pending_approval") == (1, {pending})


async def test_the_members_are_ordered_by_the_share_of_the_limit_that_the_month_used(store):
    op_store, pool = store
    nearly, quarter, zero, unlimited = [
        await _account(op_store, name) for name in ("z1", "z2", "z3", "z4")
    ]
    await _organization(
        op_store, "z", ("itest-z", [nearly, quarter, zero, unlimited]), limit=Decimal("5000")
    )
    for member, limit in ((nearly, "10"), (quarter, "200"), (zero, "0")):
        outcome, _old, _new = await op_store.set_organization_member_limit(
            "o-itest-z", member, usd=Decimal(limit), actor=ORG_ADMIN
        )
        assert outcome == orgs.UPDATED
    today = datetime.now(UTC).date()
    day = today.isoformat()

    async def order(*, descending: bool) -> list[str]:
        _total, rows = await op_store.list_organization_members(
            "o-itest-z", first_day=today, last_day=today, sort_by="limit_use", descending=descending
        )
        return [row["user_id"] for row in rows]

    try:
        # 9 of 10, 50 of 200, nothing of a limit of zero, and 500 with no limit.
        await op_store.increment_user_costs(
            [
                CostIncrement(user_id=user, day=day, api_key_id=None, cost_usd=Decimal(cost))
                for user, cost in ((nearly, "9"), (quarter, "50"), (unlimited, "500"))
            ]
        )
        largest_first = await order(descending=True)
        smallest_first = await order(descending=False)
        # The default of the organization is the limit of the member that had none.
        await op_store.set_organization_limit(
            "o-itest-z",
            "default_member_monthly_limit",
            usd=Decimal("1000"),
            action="organization.default_member_limit.set",
            actor=ORG_ADMIN,
        )
        with_a_default = await order(descending=True)
    finally:
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM user_daily_cost WHERE user_id LIKE 'u-org-z%'")

    # A limit of zero is used up. No limit is last, whichever way the order goes:
    # the largest spend of the four is not the largest share of a limit.
    assert largest_first == [zero, nearly, quarter, unlimited]
    assert smallest_first == [quarter, nearly, zero, unlimited]
    # 500 of 1,000 is between 90% and 25%.
    assert with_a_default == [zero, nearly, unlimited, quarter]


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


async def test_two_renames_to_one_name_at_once_give_the_name_to_one(store, monkeypatch):
    """Each finds the name free; the unique index then decides, and the second is told."""
    op_store, pool = store
    await _organization(op_store, "n1")
    await _organization(op_store, "n2")
    write_audit_row = orgs.insert_audit_row
    written, go_on = asyncio.Event(), asyncio.Event()

    async def write_then_wait(conn, **row):
        await write_audit_row(conn, **row)
        if row["details"] == {"name": {"old": "Itest n1", "new": "Itest N"}}:
            written.set()
            await go_on.wait()

    monkeypatch.setattr(orgs, "insert_audit_row", write_then_wait)
    first = asyncio.create_task(
        op_store.rename_organization("o-itest-n1", "Itest N", actor=PLATFORM)
    )
    try:
        # The first has the name, and has not committed: the second cannot see that.
        await asyncio.wait_for(written.wait(), 10)
        second = asyncio.create_task(
            op_store.rename_organization("o-itest-n2", "itest n", actor=PLATFORM)
        )
        await asyncio.sleep(0.2)
    finally:
        go_on.set()

    assert await first == orgs.UPDATED
    assert await second == orgs.NAME_TAKEN
    assert (await op_store.get_organization("o-itest-n2"))["name"] == "Itest n2"
    # The refused rename wrote no audit row.
    assert [row["action"] for row in await _audit(pool, "o-itest-n2")] == ["organization.create"]


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


# -- the monthly limit of a member ---------------------------------------------------


async def _key(store, user_id: str, **quota) -> int:
    token = secrets.token_hex(8)
    row = await store.create_key(
        key_hash=f"hash-{token}",
        key_prefix=f"hyi-{token[:8]}",
        user_id=user_id,
        account_id=user_id,
        quota_daily_cost_usd=Decimal("50"),
        **quota,
    )
    return row["id"]


async def _monthly_quotas(pool, user_id: str) -> tuple[list, list]:
    """The monthly quota on the account's record, and on each of its keys by id."""
    async with pool.acquire() as conn:
        record = await conn.fetch(
            "SELECT quota_daily_cost_usd, quota_monthly_cost_usd FROM account_quotas "
            "WHERE user_id = $1",
            user_id,
        )
        keys = await conn.fetch(
            "SELECT status, quota_daily_cost_usd, quota_monthly_cost_usd FROM api_keys "
            "WHERE user_id = $1 ORDER BY id",
            user_id,
        )
    return [tuple(row) for row in record], [tuple(row) for row in keys]


async def _member_limit(pool, user_id: str) -> tuple:
    """The limit an organization set for a member: both columns of its membership."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT monthly_limit_usd, monthly_limit_amount FROM sso_organization_members "
            "WHERE user_id = $1",
            user_id,
        )
    return tuple(row)


async def _key_hash(pool, key_id: int) -> str:
    async with pool.acquire() as conn:
        return await conn.fetchval("SELECT key_hash FROM api_keys WHERE id = $1", key_id)


async def test_a_members_limit_is_the_organizations_own_and_leaves_the_quota_of_the_account(store):
    op_store, pool = store
    member = await _account(op_store, "q1")
    first, _second = await _key(op_store, member), await _key(op_store, member)
    await _organization(op_store, "q", ("itest-q", [member]), limit=Decimal("100"))
    key_hash = await _key_hash(pool, first)
    account_quota = await _monthly_quotas(pool, member)

    async def enforced():
        return (await op_store.get_auth_context_with_spend(key_hash))["quota_monthly_cost_usd"]

    saved = await op_store.set_organization_member_limit(
        "o-itest-q", member, usd=Decimal("20"), actor=ORG_ADMIN
    )
    same = await op_store.set_organization_member_limit(
        "o-itest-q", member, usd=Decimal("20"), actor=ORG_ADMIN
    )

    assert saved == (orgs.UPDATED, None, Decimal("20"))
    assert same == (orgs.UNCHANGED, Decimal("20"), Decimal("20"))
    # On the member's row, in both columns. The quota of the account, on its
    # record and on its keys, is the platform administrator's: not written.
    assert await _member_limit(pool, member) == (Decimal("20"), Decimal("20"))
    assert await _monthly_quotas(pool, member) == account_quota
    assert account_quota == (
        [(Decimal("50"), None)],
        [("active", Decimal("50"), None), ("active", Decimal("50"), None)],
    )
    # A lookup by one of the keys enforces it, in the same statement as the spend.
    assert await enforced() == Decimal("20")

    cleared = await op_store.set_organization_member_limit(
        "o-itest-q", member, usd=None, actor=ORG_ADMIN
    )

    assert cleared == (orgs.UPDATED, Decimal("20"), None)
    assert await _member_limit(pool, member) == (None, None)
    assert await enforced() is None
    rows = [
        row for row in await _audit(pool, "o-itest-q") if row["action"].endswith("member_limit.set")
    ]
    assert len(rows) == 2  # the write that changed nothing wrote no row
    assert (rows[0]["actor_type"], rows[0]["actor_id"]) == ("user", "u-org-admin")
    assert (rows[0]["target_type"], rows[0]["target_user_id"]) == ("user", member)
    assert '"old": null' in rows[0]["details"]
    assert '"new": "20' in rows[0]["details"]
    assert '"old": "20' in rows[1]["details"]
    assert '"new": null' in rows[1]["details"]


async def test_the_lower_of_the_quota_of_the_account_and_the_organizations_limit_is_enforced(
    store,
):
    """An organization brings the quota of a member down, never above the platform's."""
    op_store, pool = store
    member = await _account(op_store, "p1")
    key_hash = await _key_hash(pool, await _key(op_store, member))
    # What the platform administrator sets in Admin: 10 a month for this account.
    await op_store.update_key(member, quota_monthly_cost_usd=Decimal("10"))
    await _organization(op_store, "p", ("itest-p", [member]), limit=Decimal("1000"))

    async def enforced() -> tuple:
        by_key = (await op_store.get_auth_context_with_spend(key_hash))["quota_monthly_cost_usd"]
        by_account = (await op_store.get_organization_spend_scope(member))["quota_monthly_cost_usd"]
        return by_key, by_account

    async def shown() -> tuple:
        _total, [row] = await organization_rules.members_view(op_store, "o-itest-p", "7d")
        return (
            row["monthly_limit_usd"],
            row["monthly_limit_source"],
            row["own_monthly_limit_usd"],
            row["platform_monthly_limit_usd"],
        )

    async def limit(usd):
        outcome, _old, _new = await op_store.set_organization_member_limit(
            "o-itest-p", member, usd=usd, actor=ORG_ADMIN
        )
        return outcome

    assert await enforced() == (Decimal("10"), Decimal("10"))
    assert await shown() == (Decimal("10"), "platform", None, Decimal("10"))

    # Above the platform's: saved as the organization's wish, and not enforced.
    assert await limit(Decimal("80")) == orgs.UPDATED
    assert await enforced() == (Decimal("10"), Decimal("10"))
    assert await shown() == (Decimal("10"), "platform", Decimal("80"), Decimal("10"))
    # The record of the account still has what the platform administrator set.
    record, keys = await _monthly_quotas(pool, member)
    assert [row[1] for row in record] == [Decimal("10")]
    assert [key[2] for key in keys] == [Decimal("10")]

    # Below it: the organization's limit is the one that applies.
    assert await limit(Decimal("5")) == orgs.UPDATED
    assert await enforced() == (Decimal("5"), Decimal("5"))
    assert await shown() == (Decimal("5"), "member", Decimal("5"), Decimal("10"))
    # Equal: the organization's, the one its administrator just set.
    assert await limit(Decimal("10")) == orgs.UPDATED
    assert await shown() == (Decimal("10"), "member", Decimal("10"), Decimal("10"))

    # Without a limit of its own the member has the default, again not above the platform's.
    assert await limit(None) == orgs.UPDATED
    await op_store.set_organization_limit(
        "o-itest-p",
        "default_member_monthly_limit",
        usd=Decimal("40"),
        action="organization.default_member_limit.set",
        actor=ORG_ADMIN,
    )
    assert await enforced() == (Decimal("10"), Decimal("10"))
    assert await shown() == (Decimal("10"), "platform", None, Decimal("10"))
    # The platform administrator takes the quota away: the default is what is left.
    await op_store.update_key(member, quota_monthly_cost_usd=None)
    assert await enforced() == (Decimal("40"), Decimal("40"))
    assert await shown() == (Decimal("40"), "default", None, None)


async def test_the_default_of_an_organization_does_not_reach_an_administrator(store):
    """An organization administrator cannot stop another administrator with the default."""
    op_store, pool = store
    plain, lead = await _account(op_store, "n1"), await _account(op_store, "n2")
    root = await _account(op_store, "n3", role="admin")
    limited = await _account(op_store, "n4", role="admin")
    hashes = {
        user_id: await _key_hash(pool, await _key(op_store, user_id))
        for user_id in (plain, lead, root, limited)
    }
    # The platform administrator's own quota still holds for an administrator.
    await op_store.update_key(limited, quota_monthly_cost_usd=Decimal("30"))
    await _organization(op_store, "n", ("itest-n", [plain, lead, root, limited]))
    await op_store.set_organization_member_role("o-itest-n", lead, "admin", actor=PLATFORM)

    async def enforced() -> dict:
        quotas = {}
        for user_id, key_hash in hashes.items():
            by_key = await op_store.get_auth_context_with_spend(key_hash)
            by_account = await op_store.get_organization_spend_scope(user_id)
            assert by_key["quota_monthly_cost_usd"] == by_account["quota_monthly_cost_usd"]
            quotas[user_id] = by_key["quota_monthly_cost_usd"]
        return quotas

    async def shown() -> dict:
        _total, rows = await organization_rules.members_view(op_store, "o-itest-n", "7d")
        return {
            row["user_id"]: (row["monthly_limit_usd"], row["monthly_limit_source"]) for row in rows
        }

    for default in (Decimal("5"), Decimal("0")):
        await op_store.set_organization_limit(
            "o-itest-n",
            "default_member_monthly_limit",
            usd=default,
            action="organization.default_member_limit.set",
            actor=ORG_ADMIN,
        )
        assert await enforced() == {plain: default, lead: None, root: None, limited: Decimal("30")}
        assert await shown() == {
            plain: (default, "default"),
            lead: (None, "none"),
            root: (None, "none"),
            limited: (Decimal("30"), "platform"),
        }

    # A limit the organization set before the member got the role does not hold it either.
    await op_store.set_organization_member_role("o-itest-n", lead, "member", actor=PLATFORM)
    await op_store.set_organization_member_limit(
        "o-itest-n", lead, usd=Decimal("1"), actor=ORG_ADMIN
    )
    assert (await enforced())[lead] == Decimal("1")
    await op_store.set_organization_member_role("o-itest-n", lead, "admin", actor=PLATFORM)
    assert (await enforced())[lead] is None
    assert (await shown())[lead] == (None, "none")


async def test_a_members_limit_is_refused_above_the_organizations_and_for_administrators(store):
    op_store, pool = store
    member, lead = await _account(op_store, "r1"), await _account(op_store, "r2")
    root = await _account(op_store, "r3", role="admin")
    outsider, other = await _account(op_store, "r4"), await _account(op_store, "r5")
    await _organization(op_store, "r", ("itest-r", [member, lead, root]), limit=Decimal("100"))
    await _organization(op_store, "r-other", ("itest-r-other", [other]))
    await op_store.set_organization_member_role("o-itest-r", lead, "admin", actor=PLATFORM)

    async def limit(user_id: str, usd: str):
        return await op_store.set_organization_member_limit(
            "o-itest-r", user_id, usd=Decimal(usd), actor=ORG_ADMIN
        )

    assert await limit(member, "100") == (orgs.UPDATED, None, Decimal("100"))
    assert await limit(member, "100.01") == (orgs.ABOVE_LIMIT, Decimal("100"), None)
    # An administrator of the organization, and a platform administrator that is a member.
    assert await limit(lead, "1") == (orgs.PROTECTED, None, None)
    assert await limit(root, "1") == (orgs.PROTECTED, None, None)
    # An account in no organization, and a member of another one.
    assert await limit(outsider, "1") == (orgs.NOT_FOUND, None, None)
    assert await limit(other, "1") == (orgs.NOT_FOUND, None, None)
    for user_id in (lead, root, outsider, other):
        record, _keys = await _monthly_quotas(pool, user_id)
        assert [row[1] for row in record] in ([], [None])

    # No limit on the organization is no ceiling for a member.
    await op_store.set_organization_limit(
        "o-itest-r", "monthly_limit", usd=None, action="organization.limit.set", actor=PLATFORM
    )
    assert await limit(member, "5000") == (orgs.UPDATED, Decimal("100"), Decimal("5000"))


async def test_the_default_of_a_member_cannot_be_above_the_limit_of_the_organization(store):
    op_store, _pool = store
    await _organization(op_store, "s", limit=Decimal("100"))

    async def default(usd):
        return await op_store.set_organization_limit(
            "o-itest-s",
            "default_member_monthly_limit",
            usd=usd,
            action="organization.default_member_limit.set",
            actor=ORG_ADMIN,
        )

    assert await default(Decimal("100.01")) == (orgs.ABOVE_LIMIT, None, None)
    assert await default(Decimal("100")) == (orgs.UPDATED, None, Decimal("100"))
    assert (await op_store.get_organization("o-itest-s"))[
        "default_member_monthly_limit_usd"
    ] == Decimal("100")


async def test_a_quota_that_only_the_keys_carry_is_the_accounts_own_and_stays(store):
    """An account whose record an earlier release never wrote: its keys still answer."""
    op_store, pool = store
    member = await _account(op_store, "t1")
    await _key(op_store, member, quota_monthly_cost_usd=Decimal("30"))
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM account_quotas WHERE user_id = $1", member)
    await _organization(op_store, "t", ("itest-t", [member]), limit=Decimal("100"))
    today = datetime.now(UTC).date()

    async def enforced():
        return (await op_store.get_organization_spend_scope(member))["quota_monthly_cost_usd"]

    _total, [listed] = await op_store.list_organization_members(
        "o-itest-t", first_day=today, last_day=today
    )
    alone = await enforced()
    lowered = await op_store.set_organization_member_limit(
        "o-itest-t", member, usd=Decimal("20"), actor=ORG_ADMIN
    )
    with_a_limit = await enforced()
    cleared = await op_store.set_organization_member_limit(
        "o-itest-t", member, usd=None, actor=ORG_ADMIN
    )
    record, keys = await _monthly_quotas(pool, member)

    assert listed["quota_monthly_cost_usd"] == Decimal("30")
    assert (listed["member_limit_usd"], listed["member_limit_amount"]) == (None, None)
    assert (alone, with_a_limit, await enforced()) == (Decimal("30"), Decimal("20"), Decimal("30"))
    # The organization's limit had no value before, whatever the keys carry.
    assert lowered == (orgs.UPDATED, None, Decimal("20"))
    assert cleared == (orgs.UPDATED, Decimal("20"), None)
    # Clearing it takes nothing from the keys, and writes no record for the account.
    assert record == []
    assert [key[2] for key in keys] == [Decimal("30")]


async def test_a_member_without_a_key_has_its_own_limit_or_the_default_on_the_read_by_account(
    store,
):
    op_store, _pool = store
    member, outsider = await _account(op_store, "v1"), await _account(op_store, "v2")
    await _organization(op_store, "v", ("itest-v", [member]), limit=Decimal("100"))

    async def monthly():
        scope = await op_store.get_organization_spend_scope(member)
        return scope["quota_monthly_cost_usd"]

    none = await monthly()
    await op_store.set_organization_limit(
        "o-itest-v",
        "default_member_monthly_limit",
        usd=Decimal("5"),
        action="organization.default_member_limit.set",
        actor=ORG_ADMIN,
    )
    default = await monthly()
    await op_store.set_organization_member_limit(
        "o-itest-v", member, usd=Decimal("20"), actor=ORG_ADMIN
    )
    own = await monthly()
    await op_store.set_organization_member_limit("o-itest-v", member, usd=None, actor=ORG_ADMIN)
    back = await monthly()
    scope = await op_store.get_organization_spend_scope(member)

    assert (none, default, own, back) == (None, Decimal("5"), Decimal("20"), Decimal("5"))
    assert (scope["organization_id"], scope["organization_limit_usd"]) == (
        "o-itest-v",
        Decimal("100"),
    )
    assert scope["organization_spent_month_usd"] == 0
    # An account in no organization has no scope at all.
    assert await op_store.get_organization_spend_scope(outsider) is None


async def test_a_charge_is_added_to_the_counter_of_the_members_organization_only(store):
    op_store, pool = store
    member, outsider = await _account(op_store, "w1"), await _account(op_store, "w2")
    late = await _account(op_store, "w3")
    await _organization(op_store, "w", ("itest-w", [member]), limit=Decimal("100"))
    day = datetime.now(UTC).strftime("%Y-%m-%d")

    def charge(user_id: str, cost: str) -> CostIncrement:
        return CostIncrement(user_id=user_id, day=day, api_key_id=None, cost_usd=Decimal(cost))

    async def counter() -> list[tuple]:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT period, cost_usd, cost_amount, requests FROM spend_counters "
                "WHERE scope_id = 'o:o-itest-w' ORDER BY period"
            )
        return [
            (row["period"], row["cost_usd"], row["cost_amount"], row["requests"]) for row in rows
        ]

    try:
        # One batch, as the writer coalesces them: two members' worth and an outsider's.
        await op_store.increment_user_costs(
            [
                charge(member, "0.25"),
                charge(outsider, "9"),
                charge(member, "0.50"),
                charge(late, "3"),
            ]
        )
        first = await counter()
        # An account that becomes a member afterwards adds from then on only.
        await op_store.attach_organization_connection(
            "o-itest-w", "itest-w", accounts_of=_only(late), actor=PLATFORM
        )
        await op_store.increment_user_costs([charge(late, "1")])
        second = await counter()
        month = await op_store.get_organization_month_spend("o-itest-w")
    finally:
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM user_daily_cost WHERE user_id LIKE 'u-org-w%'")

    # One row, for the month: an organization has no limit for a day.
    assert first == [(f"m:{day[:7]}", Decimal("0.750000"), Decimal("0.75"), 2)]
    assert second == [(f"m:{day[:7]}", Decimal("1.750000"), Decimal("1.75"), 3)]
    assert month["cost_usd"] == Decimal("1.750000")


# -- what an organization is shown of a member -----------------------------------------


async def _request(pool, user_id: str, at: datetime, *, model: str, cost: str = "0.10") -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO api_logs (request_id, timestamp, model_id, provider, user_id, "
            "status_code, prompt_tokens, completion_tokens, total_tokens, cost_usd) "
            "VALUES ($1, $2, $3, 'test', $4, 200, 100, 50, 150, $5)",
            f"org-store-{secrets.token_hex(6)}",
            at,
            model,
            user_id,
            Decimal(cost),
        )


async def _day_cost(pool, user_id: str, at: datetime, cost: str) -> None:
    """The counter row of the UTC day of *at*, with *at* as its last request."""
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO user_daily_cost (user_id, day, cost_usd, requests, last_request_at) "
            "VALUES ($1, $2, $3, 1, $4)",
            user_id,
            at.date().isoformat(),
            Decimal(cost),
            at,
        )


async def test_the_figures_of_a_member_start_when_its_account_became_a_member(store):
    """An attach enrolls accounts that did nothing for it: their earlier use stays theirs."""
    op_store, pool = store
    member, early = await _account(op_store, "j1"), await _account(op_store, "j2")
    await _organization(op_store, "j", ("itest-j", [member, early]))
    now = datetime.now(UTC)
    joined = now - timedelta(days=2)
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE sso_organization_members SET joined_at = $1 WHERE user_id LIKE 'u-org-j%'",
            joined,
        )
    before, at_joining, after = joined - timedelta(days=3), joined, now - timedelta(minutes=5)
    for at, model in (
        (before, "model-before"),
        (joined - timedelta(seconds=1), "model-before"),
        (joined + timedelta(seconds=1), "model-after"),
        (after, "model-after"),
    ):
        await _request(pool, member, at, model=model)
    for at, cost in ((before, "5"), (at_joining, "2"), (after, "1")):
        await _day_cost(pool, member, at, cost)
    # An account that has done nothing since it became a member.
    await _request(pool, early, before, model="model-before")
    await _day_cost(pool, early, before, "7")
    first_day, last_day = (now - timedelta(days=29)).date(), now.date()
    month_start = now.date().replace(day=1)
    whole_month = sum(
        Decimal(cost)
        for at, cost in ((before, "5"), (at_joining, "2"), (after, "1"))
        if at.date() >= month_start
    )

    _total, rows = await op_store.list_organization_members(
        "o-itest-j", first_day=first_day, last_day=last_day
    )
    usage = await op_store.get_organization_usage(
        "o-itest-j", first_day=first_day, last_day=last_day
    )
    requests = await op_store.list_organization_requests(
        "o-itest-j", first_day=first_day, last_day=last_day
    )
    detail = await organization_rules.member_detail_view(op_store, "o-itest-j", member, "30d")

    listed = {row["user_id"]: row for row in rows}
    # The request log has the instant: one second before the joining is before it.
    assert (listed[member]["requests"], listed[member]["total_tokens"]) == (2, 300)
    assert listed[member]["last_request_at"] == after
    # A day counter is one row for a day: the day of the joining counts whole,
    # the days before it do not count.
    assert listed[member]["cost_usd"] == Decimal("3")
    # What the monthly limit is compared with is the whole month, as the request path counts it.
    assert listed[member]["spent_month_usd"] == whole_month
    assert (listed[early]["requests"], listed[early]["cost_usd"]) == (0, 0)
    assert listed[early]["last_request_at"] is None

    assert (usage["requests"], usage["total_tokens"], usage["members_with_requests"]) == (2, 300, 1)
    assert {row["model_id"]: row["requests"] for row in usage["models"]} == {"model-after": 2}
    assert [row["day"] for row in usage["spend_days"]] == sorted(
        {at_joining.date().isoformat(), after.date().isoformat()}
    )
    assert sum(row["cost_usd"] for row in usage["spend_days"]) == Decimal("3")
    assert [row["model_id"] for row in requests] == ["model-after", "model-after"]

    days = {point["bucket"].date(): point["cost_usd"] for point in detail["days"]}
    assert (days[before.date()], days[at_joining.date()], days[after.date()]) == (0, 2, 1)
    assert {row["model_id"] for row in detail["models"]} == {"model-after"}
    assert [row["model_id"] for row in detail["requests"]] == ["model-after", "model-after"]


async def test_a_search_takes_a_percent_sign_and_an_underscore_as_they_are(store):
    op_store, pool = store
    exact, other = await _account(op_store, "under_score"), await _account(op_store, "underxscore")
    await _organization(op_store, "k", ("itest-k", [exact, other]))
    now = datetime.now(UTC)
    async with pool.acquire() as conn:
        # Members for an hour: a request is shown from the joining on.
        await conn.execute(
            "UPDATE sso_organization_members SET joined_at = $1 WHERE organization_id = $2",
            now - timedelta(hours=1),
            "o-itest-k",
        )
    for user_id in (exact, other):
        await _request(pool, user_id, now - timedelta(minutes=1), model="model-a")

    async def members(search: str) -> tuple[int, set[str]]:
        total, rows = await op_store.list_organization_members(
            "o-itest-k", first_day=now.date(), last_day=now.date(), search=search
        )
        return total, {row["user_id"] for row in rows}

    async def requests(search: str) -> set[str]:
        rows = await op_store.list_organization_requests(
            "o-itest-k", first_day=now.date(), last_day=now.date(), search=search
        )
        return {row["user_id"] for row in rows}

    async def audit(search: str) -> int:
        total, _rows = await op_store.list_organization_audit("o-itest-k", search=search)
        return total

    assert await members("UNDER") == (2, {exact, other})
    # An underscore is that character, not "any one character".
    assert await members("r_s") == (1, {exact})
    assert await members("_") == (1, {exact})
    assert await requests("r_s") == {exact}
    # A percent sign is that character, not "anything": nothing here has one.
    assert await members("%") == (0, set())
    assert await requests("%") == set()
    assert await audit("%") == 0
    assert await audit("organization.create") == 1
    assert orgs.contains_pattern("50%_a\\b") == "%50\\%\\_a\\\\b%"
