"""Verify phone authentication invariants against real PostgreSQL concurrency."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio

from serving.storage.postgres_operational import PostgresOperationalStore
from tests.fixtures import postgres

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

PHONE = "+8613800138000"
IP = "192.0.2.10"
CODE_HASH = "test-code-hmac"
LIMITS = {
    "cooldown": 60,
    "expire": 300,
    "phone_hour": 10,
    "phone_day": 20,
    "ip_hour": 20,
    "ip_day": 100,
}


@pytest_asyncio.fixture
async def phone_store():
    """Use a disposable schema, never another test's or deployment's rows."""
    admin = await postgres.connect(timeout=2)
    schema = f"phone_auth_test_{uuid.uuid4().hex}"
    pool = None
    try:
        await admin.execute(f'CREATE SCHEMA "{schema}"')
        pool = await postgres.create_pool(
            min_size=2, max_size=12, server_settings={"search_path": schema}
        )
        store = PostgresOperationalStore(pool)
        await store.initialize()
        yield store
    finally:
        if pool is not None:
            await pool.close()
        await admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await admin.close()


async def _reserve(store, *, phone=PHONE, purpose="login", user_id=None, **overrides):
    challenge_id = uuid.uuid4().hex
    retry = await store.reserve_phone_code(
        phone=phone,
        purpose=purpose,
        user_id=user_id,
        challenge_id=challenge_id,
        code_hash=CODE_HASH,
        ip=overrides.pop("ip", IP),
        limits={**LIMITS, **overrides},
    )
    return retry, challenge_id


async def _issue(store, **kwargs):
    retry, challenge_id = await _reserve(store, **kwargs)
    assert retry == 0
    assert await store.activate_phone_code(challenge_id)
    return challenge_id


async def _consume(store, **overrides):
    values = {
        "phone": PHONE,
        "purpose": "login",
        "user_id": None,
        "code_hash": CODE_HASH,
        "ip": IP,
        "max_attempts": 5,
        "phone_limit": 100,
        "ip_limit": 100,
    }
    values.update(overrides)
    return await store.consume_phone_code(**values)


async def test_concurrent_sends_reserve_one_challenge(phone_store):
    results = await asyncio.gather(*(_reserve(phone_store) for _ in range(20)))
    assert sum(retry == 0 for retry, _challenge in results) == 1
    async with phone_store._pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM phone_auth_attempts") == 1
        assert await conn.fetchval("SELECT count(*) FROM phone_auth_codes") == 1


async def test_shared_ip_send_limit_is_atomic_across_phone_numbers(phone_store):
    results = await asyncio.gather(
        *(_reserve(phone_store, phone=f"+86138001380{i:02d}", ip_hour=3) for i in range(20))
    )
    assert sum(retry == 0 for retry, _challenge in results) == 3


async def test_concurrent_consumes_from_separate_stores_have_one_winner(phone_store):
    await _issue(phone_store)
    other = PostgresOperationalStore(phone_store._pool)
    results = await asyncio.gather(*(_consume(phone_store if i % 2 else other) for i in range(20)))
    assert results.count("ok") == 1
    assert results.count("invalid") == 19


async def test_failed_attempts_persist_across_store_instances(phone_store):
    await _issue(phone_store)
    for _ in range(5):
        restarted = PostgresOperationalStore(phone_store._pool)
        assert await _consume(restarted, code_hash="wrong") == "invalid"
    assert await _consume(phone_store) == "invalid"
    async with phone_store._pool.acquire() as conn:
        assert await conn.fetchval("SELECT attempts FROM phone_auth_codes") == 5
        assert (
            await conn.fetchval("SELECT count(*) FROM phone_auth_attempts WHERE kind='verify'") == 6
        )


async def test_verification_ip_quota_limits_unknown_phone_spray(phone_store):
    results = await asyncio.gather(
        *(_consume(phone_store, phone=f"+86138001380{i:02d}", ip_limit=3) for i in range(20))
    )
    assert results.count("invalid") == 3
    assert results.count("rate_limited") == 17


async def test_code_is_bound_to_purpose_and_authenticated_user(phone_store):
    await phone_store.create_user(
        user_id="user-a", email="owner@example.test", password_hash="test-hash"
    )
    await _issue(phone_store, purpose="bind", user_id="user-a")
    assert await _consume(phone_store) == "invalid"
    assert await _consume(phone_store, purpose="signup") == "invalid"
    assert await _consume(phone_store, purpose="bind", user_id="user-b") == "invalid"
    assert await _consume(phone_store, purpose="bind", user_id="user-a") == "ok"


async def test_expired_or_undelivered_code_never_authenticates(phone_store):
    retry, challenge = await _reserve(phone_store)
    assert retry == 0
    assert await _consume(phone_store) == "invalid"
    assert await phone_store.activate_phone_code(challenge)
    async with phone_store._pool.acquire() as conn:
        await conn.execute("UPDATE phone_auth_codes SET expires_at = NOW() - INTERVAL '1 second'")
    assert await _consume(phone_store) == "invalid"
    assert not await phone_store.activate_phone_code(challenge)


async def test_stale_sender_cannot_activate_or_delete_replacement_code(phone_store):
    _, stale = await _reserve(phone_store, cooldown=0)
    _, replacement = await _reserve(phone_store, cooldown=0)
    assert not await phone_store.activate_phone_code(stale)
    await phone_store.invalidate_phone_code(stale)
    assert await phone_store.activate_phone_code(replacement)
    assert await _consume(phone_store) == "ok"


async def test_phone_uniqueness_and_no_synthetic_email_under_signup_race(phone_store):
    results = await asyncio.gather(
        *(
            phone_store.create_phone_user(
                user_id=f"phone-user-{i}",
                phone=PHONE,
                user_name="Phone user",
                status="active",
                signup_reason=None,
            )
            for i in range(20)
        )
    )
    assert sum(results) == 1
    user = await phone_store.get_user_by_phone(PHONE)
    assert user["email"] is None
    assert user["password_hash"] is None
    assert user["phone_verified"] is True


async def test_binding_preserves_uniqueness_existing_phone_and_account_status(phone_store):
    for user_id in ("first", "second", "deleted"):
        await phone_store.create_user(
            user_id=user_id, email=f"{user_id}@example.test", password_hash="test-hash"
        )
    assert await phone_store.bind_user_phone("first", PHONE)
    assert not await phone_store.bind_user_phone("second", PHONE)
    assert not await phone_store.bind_user_phone("first", "+8613900139000")
    await phone_store.update_user_fields("deleted", status="deleted")
    assert not await phone_store.bind_user_phone("deleted", "+8613900139000")
    assert (await phone_store.get_user_by_id("second"))["phone"] is None


async def test_soft_delete_revokes_recipient_and_owned_bind_challenges(phone_store):
    user_id = "deleted-phone-user"
    await phone_store.create_phone_user(
        user_id=user_id,
        phone=PHONE,
        user_name="Phone user",
        status="active",
        signup_reason=None,
    )
    login_challenge = await _issue(phone_store, user_id=user_id)
    bind_phone = "+8613900139000"
    bind_challenge = await _issue(phone_store, phone=bind_phone, purpose="bind", user_id=user_id)
    await phone_store.delete_user(user_id, admin_ip=IP, admin_id="synthetic-admin")
    assert not await phone_store.activate_phone_code(login_challenge)
    assert not await phone_store.activate_phone_code(bind_challenge)
    assert await _consume(phone_store, user_id=user_id) == "invalid"
    retry, _ = await _reserve(phone_store, user_id=user_id, cooldown=0)
    assert retry > 0
    async with phone_store._pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM phone_auth_codes") == 0


async def test_hard_delete_purges_recipient_abuse_state_and_prevents_orphan_reservation(
    phone_store,
):
    user_id = "erased-phone-user"
    await phone_store.create_phone_user(
        user_id=user_id,
        phone=PHONE,
        user_name="Phone user",
        status="active",
        signup_reason=None,
    )
    await _issue(phone_store, user_id=user_id)
    assert await _consume(phone_store, user_id=user_id, code_hash="wrong") == "invalid"
    await phone_store.delete_user(user_id, admin_ip=IP, admin_id="synthetic-admin")
    claim = await phone_store.begin_hard_delete_user(user_id)
    await phone_store.hard_delete_user(
        user_id, claim_token=claim.token, admin_ip=IP, admin_id="synthetic-admin"
    )
    retry, _ = await _reserve(phone_store, user_id=user_id, cooldown=0)
    assert retry > 0
    async with phone_store._pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM phone_auth_codes") == 0
        assert await conn.fetchval("SELECT count(*) FROM phone_auth_attempts") == 0
    assert await phone_store.get_user_by_phone(PHONE) is None


async def test_delete_racing_send_leaves_no_owner_challenge(phone_store):
    user_id = "race-deleted-user"
    await phone_store.create_phone_user(
        user_id=user_id,
        phone=PHONE,
        user_name="Phone user",
        status="active",
        signup_reason=None,
    )
    await asyncio.gather(
        phone_store.delete_user(user_id, admin_ip=IP, admin_id="synthetic-admin"),
        *(_reserve(phone_store, user_id=user_id, cooldown=0) for _ in range(20)),
    )
    async with phone_store._pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM phone_auth_codes") == 0


async def _record_phone_login(store, user_id):
    await store.record_phone_login_event(
        phone=PHONE,
        email=None,
        outcome="failure",
        failure_reason="invalid_code",
        user_id=user_id,
        ip=IP,
        user_agent="synthetic-security-test",
    )


async def _erase_phone_user(store, user_id):
    await store.delete_user(user_id, admin_ip=IP, admin_id="synthetic-admin")
    claim = await store.begin_hard_delete_user(user_id)
    await store.hard_delete_user(
        user_id, claim_token=claim.token, admin_ip=IP, admin_id="synthetic-admin"
    )


async def test_hard_delete_racing_verification_and_login_diagnostics_leaves_no_identity_rows(
    phone_store,
):
    user_id = "erase-diagnostic-race"
    await phone_store.create_phone_user(
        user_id=user_id,
        phone=PHONE,
        user_name="Phone user",
        status="active",
        signup_reason=None,
    )
    await _issue(phone_store, user_id=user_id)
    await asyncio.gather(
        _erase_phone_user(phone_store, user_id),
        *(_consume(phone_store, user_id=user_id) for _ in range(20)),
        *(_record_phone_login(phone_store, user_id) for _ in range(20)),
    )
    async with phone_store._pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM phone_auth_codes") == 0
        assert await conn.fetchval("SELECT count(*) FROM phone_auth_attempts") == 0
        assert await conn.fetchval("SELECT count(*) FROM login_events") == 0


async def test_stale_erased_owner_cannot_reintroduce_otp_attempts_or_login_events(phone_store):
    user_id = "stale-erased-owner"
    await phone_store.create_phone_user(
        user_id=user_id,
        phone=PHONE,
        user_name="Phone user",
        status="active",
        signup_reason=None,
    )
    await _erase_phone_user(phone_store, user_id)
    assert await _consume(phone_store, user_id=user_id) == "invalid"
    await _record_phone_login(phone_store, user_id)
    async with phone_store._pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM phone_auth_attempts") == 0
        assert await conn.fetchval("SELECT count(*) FROM login_events") == 0
    # Anonymous brute-force accounting remains in place for unissued numbers.
    assert await _consume(phone_store) == "invalid"
    await _record_phone_login(phone_store, None)
    async with phone_store._pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM phone_auth_attempts") == 1
        assert await conn.fetchval("SELECT count(*) FROM login_events") == 1


async def _create_phone_session(store, user_id, *, phone=PHONE):
    session_id = uuid.uuid4().hex
    created = await store.create_phone_session(
        session_id=session_id,
        user_id=user_id,
        phone=phone,
        refresh_token_hash=f"refresh-{session_id}",
        jti=f"jti-{session_id}",
        sid=f"sid-{session_id}",
        expires_at=datetime.now(UTC) + timedelta(days=1),
        ip=IP,
        user_agent="synthetic-session-test",
    )
    return created, session_id


async def test_phone_session_sets_session_timestamp_and_success_event_atomically(phone_store):
    user_id = "session-phone-user"
    await phone_store.create_phone_user(
        user_id=user_id,
        phone=PHONE,
        user_name="Phone user",
        status="active",
        signup_reason=None,
    )
    assert (await phone_store.get_user_by_id(user_id))["last_login_at"] is None
    assert not (await _create_phone_session(phone_store, user_id, phone="+8613900139000"))[0]
    created, session_id = await _create_phone_session(phone_store, user_id)
    assert created
    session = await phone_store.get_session_by_token_hash(f"refresh-{session_id}")
    assert session["user_id"] == user_id and session["revoked"] is False
    assert (await phone_store.get_user_by_id(user_id))["last_login_at"] is not None
    async with phone_store._pool.acquire() as conn:
        events = await conn.fetch(
            "SELECT user_id,email,phone,outcome,failure_reason FROM login_events"
        )
    assert [dict(row) for row in events] == [
        {
            "user_id": user_id,
            "email": None,
            "phone": PHONE,
            "outcome": "success",
            "failure_reason": None,
        }
    ]


async def test_hard_delete_racing_phone_session_issuance_leaves_no_orphan_sessions(phone_store):
    user_id = "session-erasure-race"
    await phone_store.create_phone_user(
        user_id=user_id,
        phone=PHONE,
        user_name="Phone user",
        status="active",
        signup_reason=None,
    )
    await asyncio.gather(
        _erase_phone_user(phone_store, user_id),
        *(_create_phone_session(phone_store, user_id) for _ in range(20)),
    )
    assert not (await _create_phone_session(phone_store, user_id))[0]
    async with phone_store._pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM auth_sessions") == 0
        assert await conn.fetchval("SELECT count(*) FROM login_events") == 0
        assert await conn.fetchval("SELECT count(*) FROM users") == 0


async def test_broadcast_recipient_query_excludes_accounts_without_email(phone_store):
    from serving.servers.routers.admin.broadcast import _recipient_where

    await phone_store.create_phone_user(
        user_id="phone-user",
        phone=PHONE,
        user_name="Phone user",
        status="active",
        signup_reason=None,
    )
    await phone_store.create_user(
        user_id="email-user", email="email@example.test", password_hash="test-hash"
    )
    async with phone_store._pool.acquire() as conn:
        for user_id, email in (("empty", ""), ("whitespace", "   ")):
            await conn.execute(
                "INSERT INTO users(id,email,password_hash) VALUES ($1,$2,'test-hash')",
                user_id,
                email,
            )
        where, params = _recipient_where(["free"], ["active"], None)
        count = await conn.fetchval(f"SELECT count(*) FROM users WHERE {where}", *params)
        recipients = await conn.fetch(f"SELECT id,email FROM users WHERE {where}", *params)
    assert count == 1
    assert [row["id"] for row in recipients] == ["email-user"]


async def test_purpose_check_from_before_delete_account_is_rebuilt(phone_store):
    from serving.storage.log_schema import constraint_admitted_values, constraint_definition
    from serving.storage.phone_auth import initialize_phone_auth

    async with phone_store._pool.acquire() as conn:
        await conn.execute(
            "ALTER TABLE phone_auth_codes DROP CONSTRAINT phone_auth_codes_purpose_check"
        )
        await conn.execute(
            "ALTER TABLE phone_auth_codes ADD CONSTRAINT phone_auth_codes_purpose_check "
            "CHECK (purpose IN ('signup', 'login', 'bind'))"
        )
        await initialize_phone_auth(conn)
        definition = await constraint_definition(
            conn, "phone_auth_codes", "phone_auth_codes_purpose_check"
        )
    assert constraint_admitted_values(definition) == {
        "signup",
        "login",
        "bind",
        "delete_account",
        "bind_email",
        "set_password",
    }
    await phone_store.create_user(
        user_id="user-a", email="owner@example.test", password_hash="test-hash"
    )
    await _issue(phone_store, purpose="delete_account", user_id="user-a")
    assert await _consume(phone_store, purpose="delete_account", user_id="user-a") == "ok"


async def test_delete_account_code_needs_an_active_owner(phone_store):
    await phone_store.create_user(
        user_id="user-a", email="owner@example.test", password_hash="test-hash"
    )
    await _issue(phone_store, purpose="delete_account", user_id="user-a")
    async with phone_store._pool.acquire() as conn:
        await conn.execute("UPDATE users SET status = 'suspended' WHERE id = 'user-a'")
    assert await _consume(phone_store, purpose="delete_account", user_id="user-a") == "invalid"
    retry, _challenge = await _reserve(phone_store, purpose="delete_account", user_id="user-a")
    assert retry == 86400


async def test_self_service_soft_delete_is_marked_in_the_audit_log(phone_store):
    import json

    await phone_store.create_user(
        user_id="user-a", email="owner@example.test", password_hash="test-hash"
    )
    await phone_store.delete_user(
        "user-a",
        admin_ip=IP,
        admin_id="user-a",
        reason="Deleted by the account owner",
        email="owner@example.test",
        self_service=True,
    )
    async with phone_store._pool.acquire() as conn:
        status = await conn.fetchval("SELECT status FROM users WHERE id = 'user-a'")
        details = await conn.fetchval(
            "SELECT details FROM admin_audit_log WHERE target_user_id = 'user-a' "
            "AND action = 'delete_user'"
        )
    assert status == "deleted"
    assert json.loads(details)["self_service"] is True


async def test_self_service_delete_refuses_an_account_promoted_meanwhile(phone_store):
    from serving.exceptions import SelfDeleteRefused

    await phone_store.create_user(
        user_id="user-a", email="owner@example.test", password_hash="test-hash"
    )
    async with phone_store._pool.acquire() as promoter:
        # An administrator's promotion holds the row when the deletion starts.
        tx = promoter.transaction()
        await tx.start()
        await promoter.execute("UPDATE users SET role = 'admin' WHERE id = 'user-a'")
        deletion = asyncio.create_task(
            phone_store.delete_user("user-a", admin_ip=IP, admin_id="user-a", self_service=True)
        )
        await asyncio.sleep(0.3)
        assert not deletion.done()
        await tx.commit()
        with pytest.raises(SelfDeleteRefused):
            await deletion
    async with phone_store._pool.acquire() as conn:
        row = await conn.fetchrow("SELECT status, role FROM users WHERE id = 'user-a'")
        audited = await conn.fetchval(
            "SELECT count(*) FROM admin_audit_log WHERE target_user_id = 'user-a'"
        )
    assert (row["status"], row["role"]) == ("active", "admin")
    assert audited == 0


async def test_admin_soft_delete_is_not_gated_on_role(phone_store):
    await phone_store.create_user(
        user_id="user-a", email="owner@example.test", password_hash="test-hash"
    )
    async with phone_store._pool.acquire() as conn:
        await conn.execute("UPDATE users SET role = 'admin' WHERE id = 'user-a'")
    await phone_store.delete_user("user-a", admin_ip=IP, admin_id="someone-else")
    async with phone_store._pool.acquire() as conn:
        assert await conn.fetchval("SELECT status FROM users WHERE id = 'user-a'") == "deleted"


async def test_portrait_is_kept_out_of_the_user_row(phone_store):
    await phone_store.create_user(
        user_id="user-a", email="owner@example.test", password_hash="test-hash"
    )
    portrait = "data:image/webp;base64,UklGRg=="
    await phone_store.set_user_avatar("user-a", portrait)
    assert await phone_store.get_user_avatar("user-a") == portrait
    row = await phone_store.get_user_by_id("user-a")
    assert "avatar_data_url" not in row
    assert "avatar_url" not in (row.get("preferences") or {})
    await phone_store.set_user_avatar("user-a", None)
    assert await phone_store.get_user_avatar("user-a") is None
