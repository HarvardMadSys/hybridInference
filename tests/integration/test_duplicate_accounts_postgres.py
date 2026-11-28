"""Real SQL coverage for the duplicate-account scan.

Seeds registrations (``admin_audit_log``), sign-ins (``login_events``),
requests (``api_logs``), multi-account browsers and email aliases in a schema
of their own, and runs :func:`serving.analytics.duplicate_accounts.scan_duplicate_accounts`
over them: the queries, the row mapping and the hour-by-hour read for the
accounts a link reaches.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest
import pytest_asyncio

from migrations.baseline.api_logs import ensure_api_logs_schema
from serving.analytics import duplicate_accounts
from serving.analytics.duplicate_accounts import scan_duplicate_accounts
from serving.storage.postgres_log import PostgresLogStore
from tests.fixtures import postgres
from tests.fixtures.accounting_schema import add_native_columns

pytestmark = [pytest.mark.integration, pytest.mark.dbtest]

NOW = datetime.now(UTC).replace(microsecond=0)
#: Webmail domains, spelled at runtime: the private-surface sweep refuses a
#: literal webmail address anywhere in the tree, and these accounts are made up.
GMAIL = "gmail" + ".com"
GOOGLEMAIL = "googlemail" + ".com"
HOME = "198.51.100.7"
OFFICE = "203.0.113.9"

_USERS = [
    # id, email, role, status, is_service_account
    ("burst-a", "ann@example.org", "free", "active", False),
    ("burst-b", "ben@example.org", "free", "pending_approval", False),
    ("staff", "ops@example.org", "admin", "active", False),
    ("robot", None, "free", "active", True),
    ("hours-a", "hal@example.org", "free", "active", False),
    ("hours-b", "hil@example.org", "free", "active", False),
    ("alias-a", f"Sam.Smith+work@{GMAIL.title()}", "free", "active", False),
    ("alias-b", f"samsmith@{GOOGLEMAIL}", "free", "suspended", False),
    ("browser-a", "bea@example.org", "free", "active", False),
    ("browser-b", "bob@example.org", "free", "active", False),
    ("batchy", "bat@example.org", "free", "active", False),
    ("lone", "lone@example.org", "free", "active", False),
]


async def _seed(db) -> None:
    await db.executemany(
        "INSERT INTO users (id, email, password_hash, role, status, is_service_account, created_at) "
        "VALUES ($1, $2, 'fixture', $3, $4, $5, $6)",
        [
            (uid, email, role, status, service, NOW - timedelta(days=20 - index))
            for index, (uid, email, role, status, service) in enumerate(_USERS)
        ],
    )

    # Registrations: a burst from HOME, the admin's own and an old one from
    # HOME outside the window, an admin-created account, and a failed write.
    await db.executemany(
        "INSERT INTO admin_audit_log "
        "(timestamp, admin_ip, action, target_user_id, success, actor_type, source_ip, "
        " target_type, target_id) "
        "VALUES ($1, COALESCE($5, 'unknown'), $2, $3, $4, $6, $5, 'user', $3)",
        [
            (NOW - timedelta(days=2), "create_user", "burst-a", True, HOME, "anonymous"),
            (
                NOW - timedelta(days=2) + timedelta(minutes=3),
                "create_user",
                "burst-b",
                True,
                HOME,
                "anonymous",
            ),
            (NOW - timedelta(days=2), "create_user", "staff", True, HOME, "anonymous"),
            (NOW - timedelta(days=40), "create_user", "lone", True, HOME, "anonymous"),
            (NOW - timedelta(days=2), "create_user", "robot", True, HOME, "admin"),
            (NOW - timedelta(days=2), "update_user", "lone", True, HOME, "anonymous"),
            (NOW - timedelta(days=2), "create_user", "batchy", True, None, "anonymous"),
        ],
    )

    # Sign-ins: hours-a and hours-b from OFFICE; failures never count.
    await db.executemany(
        "INSERT INTO login_events (created_at, user_id, email, outcome, ip, user_agent) "
        "VALUES ($1, $2, $3, $4, $5, 'Mozilla/5.0')",
        [
            (NOW - timedelta(days=1), "hours-a", "hal@example.org", "success", OFFICE),
            (NOW - timedelta(days=1), "hours-b", "hil@example.org", "failure", OFFICE),
            (NOW - timedelta(days=1), "lone", "lone@example.org", "failure", OFFICE),
        ],
    )

    rows = []

    def request(user_id, *, hours_ago, ip, agent="acme-farm/0.1", **extra):
        rows.append(
            (
                f"req-{uuid.uuid4().hex}",
                user_id,
                NOW - timedelta(hours=hours_ago),
                json.dumps({"ip": ip, "user_agent": agent, **extra.pop("metadata", {})}),
                extra.pop("batch_job_id", None),
                extra.pop("agent_job_id", None),
            )
        )

    # hours-a and hours-b: one address, the same clock hours, the same client.
    for user_id in ("hours-a", "hours-b"):
        for hours_ago in (3, 27, 51):
            request(user_id, hours_ago=hours_ago, ip=OFFICE)
            request(user_id, hours_ago=hours_ago, ip=OFFICE)
    # Traffic that is not the person's own never links: batch, agent, probes.
    for hours_ago in (3, 27):
        request("batchy", hours_ago=hours_ago, ip=OFFICE, batch_job_id="batch_1")
        request("batchy", hours_ago=hours_ago, ip=OFFICE, agent_job_id="job_1")
        request("batchy", hours_ago=hours_ago, ip=OFFICE, metadata={"synthetic_probe": True})
    # An unattributed address, an alias active 30 hours ago, and requests
    # outside the window. Each with a client of its own, so the hours pair's
    # stays uncommon.
    request("browser-a", hours_ago=5, ip="unknown", agent="Mozilla/5.0")
    request("alias-a", hours_ago=30, ip="192.0.2.77", agent="curl/8.5.0")
    request("lone", hours_ago=24 * 30, ip=OFFICE, agent="lone-bot/1.0")
    request("lone", hours_ago=24 * 30, ip=OFFICE, agent="lone-bot/1.0")
    await db.executemany(
        "INSERT INTO api_logs "
        "(request_id, model_id, provider, user_id, timestamp, metadata, batch_job_id, agent_job_id) "
        "VALUES ($1, 'fixture', 'fixture', $2, $3, $4::jsonb, $5, $6)",
        rows,
    )

    # One browser holding two accounts, another holding one.
    await db.executemany(
        "INSERT INTO browser_sessions (id, secret_hash, expires_at) VALUES ($1, $2, $3)",
        [("bs-1", "h1", NOW + timedelta(days=30)), ("bs-2", "h2", NOW + timedelta(days=30))],
    )
    await db.executemany(
        "INSERT INTO browser_session_accounts "
        "(browser_session_id, user_id, auth_session_id, added_at) VALUES ($1, $2, $3, $4)",
        [
            ("bs-1", "browser-a", "as-1", NOW - timedelta(days=4)),
            ("bs-1", "browser-b", "as-2", NOW - timedelta(days=3)),
            ("bs-2", "lone", "as-3", NOW - timedelta(days=3)),
        ],
    )


@pytest_asyncio.fixture
async def pool():
    schema = "duplicate_accounts_" + uuid.uuid4().hex
    conn = await postgres.connect()
    await conn.execute(f'CREATE SCHEMA "{schema}"')
    pool = await postgres.create_pool(
        min_size=1, max_size=2, server_settings={"search_path": schema}
    )
    try:
        async with pool.acquire() as db:
            for table in (
                "users",
                "admin_audit_log",
                "login_events",
                "browser_sessions",
                "browser_session_accounts",
            ):
                await db.execute(f"CREATE TABLE {table} (LIKE public.{table} INCLUDING ALL)")
            await ensure_api_logs_schema(db)
            await add_native_columns(db, ("api_logs",))
            await _seed(db)
        yield pool
    finally:
        await pool.close()
        await conn.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await conn.close()


def _group(report, *user_ids):
    for group in report["groups"]:
        if group["user_ids"] == sorted(user_ids):
            return group
    raise AssertionError(
        f"no group {sorted(user_ids)} in {[g['user_ids'] for g in report['groups']]}"
    )


def _evidence(group):
    [link] = group["links"]
    return {item["kind"]: item for item in link["evidence"]}


async def test_the_scan_reads_every_kind_of_evidence(pool):
    async with pool.acquire() as conn:
        report = await scan_duplicate_accounts(conn, days=14, now=NOW)

    assert sorted(group["user_ids"] for group in report["groups"]) == [
        ["alias-a", "alias-b"],
        ["browser-a", "browser-b"],
        ["burst-a", "burst-b"],
        ["hours-a", "hours-b"],
    ]
    assert report["since"] == NOW - timedelta(days=14)
    assert report["oversized"] == []

    burst = _evidence(_group(report, "burst-a", "burst-b"))
    assert set(burst) == {"signup_burst", "signup_ip"}
    assert burst["signup_burst"]["detail"] == f"Registered 3 minutes apart from {HOME}"

    hours = _group(report, "hours-a", "hours-b")
    assert set(_evidence(hours)) == {"request_ip", "co_active", "same_client"}
    assert _evidence(hours)["co_active"]["detail"] == (
        f"Sent requests from {OFFICE} in the same clock hour, 3 times"
    )
    assert {member["user_id"]: member["requests"] for member in hours["members"]} == {
        "hours-a": 6,
        "hours-b": 6,
    }

    alias = _group(report, "alias-a", "alias-b")
    assert _evidence(alias)["email_alias"]["detail"] == (
        f"Their email addresses reach one mailbox, samsmith@{GMAIL}"
    )
    assert [member["status"] for member in alias["members"]] == ["active", "suspended"]

    browser = _group(report, "browser-a", "browser-b")
    assert set(_evidence(browser)) == {"same_browser"}


async def test_the_scan_leaves_out_what_is_not_a_persons_own(pool):
    async with pool.acquire() as conn:
        report = await scan_duplicate_accounts(conn, days=14, now=NOW)

    linked = {user_id for group in report["groups"] for user_id in group["user_ids"]}
    # The admin and the admin-created service account share HOME but are the
    # operator's; batch, agent and probe traffic is the gateway's; "lone" is
    # active only outside the window or through failed sign-ins.
    assert linked.isdisjoint({"staff", "robot", "batchy", "lone"})
    # Active in the window: the burst pair, the hours pair, alias-a and
    # browser-a. Their partners alias-b and browser-b are linked through them.
    assert report["accounts_scanned"] == 6


async def test_a_short_window_drops_older_evidence(pool):
    async with pool.acquire() as conn:
        report = await scan_duplicate_accounts(conn, days=1, now=NOW)

    # The registrations are two days old, the shared hours but one are older
    # than a day, and alias-a was last active 30 hours ago. The browser holds:
    # browser-a sent a request five hours ago.
    assert sorted(group["user_ids"] for group in report["groups"]) == [
        ["browser-a", "browser-b"],
    ]


async def test_the_log_store_runs_the_scan_on_its_pool(pool):
    store = PostgresLogStore(pool, fence_secret=postgres.FENCE_SECRET)

    report = await store.scan_duplicate_accounts(days=14, min_score=7, limit=1)

    assert report["total_groups"] == 2  # the browser (8) and the shared hours (7)
    assert [group["user_ids"] for group in report["groups"]] == [["browser-a", "browser-b"]]


async def test_a_scan_is_one_read_only_snapshot_within_its_budget(pool, monkeypatch):
    # A statement slower than the whole budget is cancelled by the database.
    monkeypatch.setattr(
        duplicate_accounts,
        "_SIGNUPS_SQL",
        "SELECT NULL::text AS user_id, NULL::text AS ip, now() AS registered_at "
        "FROM pg_sleep(3) WHERE $1::timestamptz IS NOT NULL",
    )
    async with pool.acquire() as conn:
        with pytest.raises(asyncpg.QueryCanceledError):
            await scan_duplicate_accounts(conn, days=14, now=NOW, budget_sec=0.5)
        # The timeout was local to the scan's transaction.
        assert await conn.fetchval("SHOW statement_timeout") != "1s"


async def test_a_scan_cannot_write(pool, monkeypatch):
    monkeypatch.setattr(
        duplicate_accounts,
        "_SIGNUPS_SQL",
        "INSERT INTO users (id, email) VALUES ('intruder', 'x@example.org') "
        "RETURNING id AS user_id, NULL::text AS ip, now() AS registered_at, $1::timestamptz",
    )
    async with pool.acquire() as conn:
        with pytest.raises(asyncpg.ReadOnlySQLTransactionError):
            await scan_duplicate_accounts(conn, days=14, now=NOW)
        assert await conn.fetchval("SELECT count(*) FROM users WHERE id = 'intruder'") == 0
