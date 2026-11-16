"""A second sign-in factor, through the real routes and the real administrator check.

Each test signs in with a real access token, so ``get_current_user`` and
``verify_admin_access`` run as they do in the gateway; the store is a mock whose
second-factor rows are kept by ``tests.fixtures.two_factor``, so what a request
proved can be read back. What is asserted is what a client sees: who is let in,
who is asked for a code, and what an attempt leaves in the audit log.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from routing.routers import FixedRouter
from serving.auth import two_factor as tf
from serving.config.settings import get_settings
from serving.servers.deps import AppServices
from serving.servers.middleware.exception_handler import install_exception_handlers
from serving.servers.routers import admin as admin_router, two_factor_routes
from serving.storage.base import OperationalStore
from serving.utils import password as password_utils
from serving.utils.jwt import create_access_token
from serving.utils.login_rate_limit import reset_login_rate_limit_state
from tests.fixtures import two_factor as two_factor_fake

ADMIN_TOKEN = "unit-admin-token"
PASSWORD = "correct-horse-battery"
MODEL = "reserved-model"

CHROME = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)
CURL = "curl/7.88.1"

ADMIN = "u-admin"
OTHER_ADMIN = "u-admin-2"
MEMBER = "u-member"


def _account(user_id: str, role: str) -> dict[str, Any]:
    return {
        "id": user_id,
        "email": f"{user_id}@example.test",
        "login_name": None,
        "user_name": user_id,
        "role": role,
        "status": "active",
        "email_verified": True,
        "phone": None,
        "phone_verified": False,
        "password_hash": "stored-hash",
    }


class Gateway:
    """The app under test, its store, and the handful of things tests do to both."""

    def __init__(self, client: AsyncClient, store: Any, fake: two_factor_fake.TwoFactorFake):
        self.client = client
        self.store = store
        self.fake = fake
        self.accounts = {
            ADMIN: _account(ADMIN, "admin"),
            OTHER_ADMIN: _account(OTHER_ADMIN, "admin"),
            MEMBER: _account(MEMBER, "free"),
        }

    def sign_in(self, user_id: str, *, agent: str = CHROME, sid: str | None = None) -> dict:
        """Open a session for *user_id* and return the headers its browser sends."""
        sid = sid or f"sess_{user_id}"
        self.fake.open_session(user_id, sid)
        account = self.accounts[user_id]
        token, _jti = create_access_token(
            user_id, account["email"], session_id=sid, role=account["role"]
        )
        return {"Authorization": f"Bearer {token}", "User-Agent": agent}

    def code(self, user_id: str) -> str:
        """A code the account's authenticator shows now, not yet used."""
        row = self.fake.factors[user_id]
        row["last_used_step"] = None
        secret = tf.decrypt_secret(row["secret_encrypted"])
        return tf.totp_at(secret, tf.current_step())

    async def enroll(self, user_id: str, headers: dict) -> list[str]:
        """Add an authenticator to *user_id* through the routes; return its recovery codes."""
        setup = await self.client.post(
            "/auth/two-factor/setup", headers=headers, json={"password": PASSWORD}
        )
        assert setup.status_code == 200, setup.text
        code = tf.totp_at(setup.json()["secret"], tf.current_step())
        confirmed = await self.client.post(
            "/auth/two-factor/confirm", headers=headers, json={"code": code}
        )
        assert confirmed.status_code == 200, confirmed.text
        return confirmed.json()["recovery_codes"]

    def age_verification(self, user_id: str, minutes: int, *, sid: str | None = None) -> None:
        session = self.fake.sessions[(user_id, sid or f"sess_{user_id}")]
        session["verified_at"] = datetime.now(UTC) - timedelta(minutes=minutes)

    def audit(self, action: str) -> list[dict[str, Any]]:
        return [
            call.kwargs
            for call in self.store.log_admin_action.await_args_list
            if call.kwargs["action"] == action
        ]


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", ADMIN_TOKEN)
    monkeypatch.setenv("JWT_SECRET_KEY", "unit-test-jwt-secret-at-least-32-characters")
    monkeypatch.setenv("API_KEY_SECRET", "unit-test-secret")
    monkeypatch.setenv("SIGNUP_REQUIRE_EMAIL_VERIFICATION", "0")
    monkeypatch.delenv("ADMIN_TWO_FACTOR_REQUIRED", raising=False)
    monkeypatch.setattr(password_utils, "verify_password", lambda plain, _hash: plain == PASSWORD)
    get_settings.cache_clear()
    reset_login_rate_limit_state()
    yield monkeypatch
    reset_login_rate_limit_state()
    get_settings.cache_clear()


@pytest.fixture
def required(env):
    """The deployment gives administrator access only to a verified session."""
    env.setenv("ADMIN_TWO_FACTOR_REQUIRED", "true")
    get_settings.cache_clear()


@pytest.fixture
async def gateway(env):
    store = MagicMock(spec=OperationalStore)
    for name in dir(OperationalStore):
        if not name.startswith("_") and callable(getattr(OperationalStore, name)):
            setattr(store, name, AsyncMock())
    fake = two_factor_fake.install(store)

    adapter = MagicMock()
    adapter.config.id = MODEL
    adapter.config.name = MODEL
    adapter.config.provider = "test"
    router = FixedRouter()
    router.register_route(MODEL, [(adapter, 1.0)], required_role="internal")

    app = FastAPI()
    install_exception_handlers(app)
    app.state.services = AppServices(
        router=router,
        operational_store=store,
        db_logger=MagicMock(),
        log_store=MagicMock(),
    )
    app.include_router(admin_router.router)
    app.include_router(two_factor_routes.router)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        gw = Gateway(client, store, fake)
        store.get_user_by_id = AsyncMock(side_effect=lambda user_id: gw.accounts.get(user_id))
        store.session_is_live = AsyncMock(
            side_effect=lambda user_id, sid: (user_id, sid) in fake.sessions
        )
        store.get_model_visibility_override = AsyncMock(return_value=None)
        store.list_audit_actions = AsyncMock(return_value=["update_user"])
        yield gw


async def _admin_read(gw: Gateway, headers: dict):
    return await gw.client.get("/admin/audit-log/actions", headers=headers)


async def _open_model(gw: Gateway, headers: dict):
    return await gw.client.patch(
        f"/admin/models/{MODEL}/visibility", headers=headers, json={"required_role": "free"}
    )


# --- adding an authenticator -------------------------------------------------


@pytest.mark.asyncio
async def test_an_account_starts_with_no_second_factor(gateway):
    headers = gateway.sign_in(MEMBER)

    status = await gateway.client.get("/auth/two-factor", headers=headers)

    assert status.status_code == 200, status.text
    assert status.json() == {
        "enabled": False,
        "pending": False,
        "required": False,
        "session_verified": False,
        "recovery_codes_left": 0,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [{}, {"password": "a-guess"}])
async def test_a_session_alone_cannot_add_an_authenticator(gateway, body):
    """Whoever copied the session would otherwise add a device of their own."""
    headers = gateway.sign_in(ADMIN)

    refused = await gateway.client.post("/auth/two-factor/setup", headers=headers, json=body)

    assert refused.status_code == 400
    assert refused.json()["detail"] == "Current password is incorrect."
    assert ADMIN not in gateway.fake.factors


@pytest.mark.asyncio
async def test_adding_an_authenticator_takes_the_password_then_a_first_code(gateway):
    headers = gateway.sign_in(ADMIN)

    setup = await gateway.client.post(
        "/auth/two-factor/setup", headers=headers, json={"password": PASSWORD}
    )
    assert setup.status_code == 200, setup.text
    secret = setup.json()["secret"]
    assert setup.json()["otpauth_uri"].startswith("otpauth://totp/")
    assert f"secret={secret}" in setup.json()["otpauth_uri"]
    # Shown, not yet in force, and never stored as shown.
    pending = await gateway.client.get("/auth/two-factor", headers=headers)
    assert pending.json()["enabled"] is False and pending.json()["pending"] is True
    assert secret not in repr(gateway.fake.factors)

    wrong = await gateway.client.post(
        "/auth/two-factor/confirm", headers=headers, json={"code": "000000"}
    )
    assert wrong.status_code == 400
    assert gateway.fake.factors[ADMIN]["confirmed_at"] is None

    confirmed = await gateway.client.post(
        "/auth/two-factor/confirm",
        headers=headers,
        json={"code": tf.totp_at(secret, tf.current_step())},
    )
    assert confirmed.status_code == 200, confirmed.text
    codes = confirmed.json()["recovery_codes"]
    assert len(codes) == tf.RECOVERY_CODE_COUNT
    # The codes are shown once; only their hashes are kept.
    assert not any(code in repr(gateway.fake.recovery) for code in codes)

    status = await gateway.client.get("/auth/two-factor", headers=headers)
    assert status.json() == {
        "enabled": True,
        "pending": False,
        "required": False,
        "session_verified": True,
        "recovery_codes_left": tf.RECOVERY_CODE_COUNT,
    }
    # The wrong first code is on record too: guesses at a setup should show.
    wrong_row, row = gateway.audit("two_factor.enable")
    assert (wrong_row["success"], row["success"]) == (False, True)
    assert row["details"] == {
        "sid": f"sess_{ADMIN}",
        "environment": "chrome/macos",
        "replaced": False,
    }
    assert (row["actor"].type, row["actor"].id) == ("user", ADMIN)


@pytest.mark.asyncio
async def test_a_setup_is_not_a_place_to_guess_the_first_code(gateway):
    """Confirming hands over recovery codes and a verified session: five tries, then start again."""
    headers = gateway.sign_in(ADMIN)
    setup = await gateway.client.post(
        "/auth/two-factor/setup", headers=headers, json={"password": PASSWORD}
    )
    right = tf.totp_at(setup.json()["secret"], tf.current_step())

    for _ in range(tf.SETUP_ATTEMPTS):
        wrong = await gateway.client.post(
            "/auth/two-factor/confirm", headers=headers, json={"code": "000000"}
        )
        assert wrong.status_code == 400
    # Out of tries: the right code is too late, and nothing waits any more.
    late = await gateway.client.post(
        "/auth/two-factor/confirm", headers=headers, json={"code": right}
    )
    assert late.status_code == 409
    assert "Start again" in late.json()["detail"]
    status = (await gateway.client.get("/auth/two-factor", headers=headers)).json()
    assert (status["enabled"], status["pending"]) == (False, False)
    assert len(gateway.audit("two_factor.enable")) == tf.SETUP_ATTEMPTS

    # Starting again costs the password again, and works.
    assert await gateway.enroll(ADMIN, headers)


@pytest.mark.asyncio
async def test_only_the_session_that_started_a_setup_confirms_it(gateway):
    """Another session of the account, however it came by one, cannot finish somebody's setup."""
    owner = gateway.sign_in(ADMIN, sid="owner")
    other = gateway.sign_in(ADMIN, sid="other", agent=CURL)
    setup = await gateway.client.post(
        "/auth/two-factor/setup", headers=owner, json={"password": PASSWORD}
    )
    right = tf.totp_at(setup.json()["secret"], tf.current_step())

    for _ in range(tf.SETUP_ATTEMPTS + 1):
        refused = await gateway.client.post(
            "/auth/two-factor/confirm", headers=other, json={"code": right}
        )
        assert refused.status_code == 409
    assert (await gateway.client.get("/auth/two-factor", headers=other)).json()["pending"] is False
    assert gateway.fake.factors[ADMIN]["confirmed_at"] is None
    # It cannot cancel it or use up its tries either.
    assert (await gateway.client.delete("/auth/two-factor/setup", headers=other)).status_code == 204

    confirmed = await gateway.client.post(
        "/auth/two-factor/confirm", headers=owner, json={"code": right}
    )
    assert confirmed.status_code == 200, confirmed.text
    assert gateway.fake.sessions[(ADMIN, "owner")]["verified_at"] is not None
    assert gateway.fake.sessions[(ADMIN, "other")]["verified_at"] is None


@pytest.mark.asyncio
async def test_a_setup_left_waiting_runs_out_and_a_cancelled_one_is_gone(gateway):
    headers = gateway.sign_in(ADMIN)
    setup = await gateway.client.post(
        "/auth/two-factor/setup", headers=headers, json={"password": PASSWORD}
    )
    right = tf.totp_at(setup.json()["secret"], tf.current_step())
    gateway.fake.factors[ADMIN]["pending_expires_at"] = datetime.now(UTC) - timedelta(seconds=1)

    expired = await gateway.client.post(
        "/auth/two-factor/confirm", headers=headers, json={"code": right}
    )
    assert expired.status_code == 409

    setup = await gateway.client.post(
        "/auth/two-factor/setup", headers=headers, json={"password": PASSWORD}
    )
    cancelled = await gateway.client.delete("/auth/two-factor/setup", headers=headers)
    assert cancelled.status_code == 204
    gone = await gateway.client.post(
        "/auth/two-factor/confirm",
        headers=headers,
        json={"code": tf.totp_at(setup.json()["secret"], tf.current_step())},
    )
    assert gone.status_code == 409
    assert gateway.fake.factors[ADMIN]["confirmed_at"] is None


@pytest.mark.asyncio
async def test_confirming_without_a_setup_is_refused(gateway):
    headers = gateway.sign_in(ADMIN)

    refused = await gateway.client.post(
        "/auth/two-factor/confirm", headers=headers, json={"code": "123456"}
    )

    assert refused.status_code == 409


# --- giving a code -----------------------------------------------------------


@pytest.mark.asyncio
async def test_a_second_session_owes_a_code_and_a_code_is_good_once(gateway):
    await gateway.enroll(ADMIN, gateway.sign_in(ADMIN))
    laptop = gateway.sign_in(ADMIN, sid="sess_laptop")
    assert (await gateway.client.get("/auth/two-factor", headers=laptop)).json()[
        "session_verified"
    ] is False

    wrong = await gateway.client.post(
        "/auth/two-factor/verify", headers=laptop, json={"code": "000000"}
    )
    assert wrong.status_code == 400
    [failed] = gateway.audit("two_factor.verify")
    assert failed["success"] is False

    code = gateway.code(ADMIN)
    verified = await gateway.client.post(
        "/auth/two-factor/verify", headers=laptop, json={"code": code}
    )
    assert verified.status_code == 200, verified.text
    assert verified.json() == {"verified": True, "recovery_codes_left": tf.RECOVERY_CODE_COUNT}
    assert (await gateway.client.get("/auth/two-factor", headers=laptop)).json()[
        "session_verified"
    ] is True
    assert gateway.audit("two_factor.verify")[-1]["details"]["method"] == "totp"

    # Read over a shoulder and typed into a third session: already spent.
    phone = gateway.sign_in(ADMIN, sid="sess_phone")
    replayed = await gateway.client.post(
        "/auth/two-factor/verify", headers=phone, json={"code": code}
    )
    assert replayed.status_code == 400


@pytest.mark.asyncio
async def test_a_recovery_code_verifies_once(gateway):
    codes = await gateway.enroll(ADMIN, gateway.sign_in(ADMIN))
    laptop = gateway.sign_in(ADMIN, sid="sess_laptop")
    typed = codes[0].lower().replace("-", " ")

    verified = await gateway.client.post(
        "/auth/two-factor/verify", headers=laptop, json={"code": typed}
    )
    assert verified.status_code == 200, verified.text
    assert verified.json()["recovery_codes_left"] == tf.RECOVERY_CODE_COUNT - 1
    assert gateway.audit("two_factor.verify")[-1]["details"]["method"] == "recovery_code"

    again = await gateway.client.post(
        "/auth/two-factor/verify",
        headers=gateway.sign_in(ADMIN, sid="sess_phone"),
        json={"code": codes[0]},
    )
    assert again.status_code == 400


@pytest.mark.asyncio
async def test_there_is_nothing_to_verify_without_an_authenticator(gateway):
    refused = await gateway.client.post(
        "/auth/two-factor/verify", headers=gateway.sign_in(MEMBER), json={"code": "123456"}
    )

    assert refused.status_code == 409


@pytest.mark.asyncio
async def test_wrong_codes_in_a_row_hold_the_accounts_codes_off(gateway):
    codes = await gateway.enroll(ADMIN, gateway.sign_in(ADMIN))
    laptop = gateway.sign_in(ADMIN, sid="sess_laptop")

    async def guess(code: str):
        return await gateway.client.post(
            "/auth/two-factor/verify", headers=laptop, json={"code": code}
        )

    statuses = [(await guess(f"{n:06d}")).status_code for n in range(tf.LOCK_EVERY_ATTEMPTS)]
    assert statuses == [400] * tf.LOCK_EVERY_ATTEMPTS

    # Held off: the right code is not looked at, and not spent.
    right = gateway.fake.factors[ADMIN]
    current = tf.totp_at(tf.decrypt_secret(right["secret_encrypted"]), tf.current_step() + 1)
    held = await guess(current)
    assert held.status_code == 429
    assert 0 < int(held.headers["retry-after"]) <= tf.LOCK_SECONDS
    assert "recovery code" in held.json()["detail"]
    # What was refused unlooked-at left no trace of a failed check.
    assert len(gateway.audit("two_factor.verify")) == tf.LOCK_EVERY_ATTEMPTS

    # A wrong recovery code is held off too; a right one is how the owner gets
    # past a hold someone else's guesses caused, and it ends the hold.
    assert (await guess("AAAAA-AAAAA")).status_code == 429
    assert (await guess(codes[0])).status_code == 200
    assert right["failed_attempts"] == 0 and right["locked_until"] is None


@pytest.mark.asyncio
async def test_a_right_code_ends_the_run_of_wrong_ones(gateway):
    await gateway.enroll(ADMIN, gateway.sign_in(ADMIN))
    laptop = gateway.sign_in(ADMIN, sid="sess_laptop")

    for round_ in range(3):
        for n in range(tf.LOCK_EVERY_ATTEMPTS - 1):
            wrong = await gateway.client.post(
                "/auth/two-factor/verify", headers=laptop, json={"code": f"{n:06d}"}
            )
            assert wrong.status_code == 400, (round_, n)
        # Four typos and then the code: never held off, however often.
        row = gateway.fake.factors[ADMIN]
        secret = tf.decrypt_secret(row["secret_encrypted"])
        step = max(tf.current_step(), (row["last_used_step"] or 0) + 1)
        if step > tf.current_step() + tf.TOTP_SKEW_STEPS:
            row["last_used_step"] = None
            step = tf.current_step()
        ok = await gateway.client.post(
            "/auth/two-factor/verify", headers=laptop, json={"code": tf.totp_at(secret, step)}
        )
        assert ok.status_code == 200, ok.text
        assert row["failed_attempts"] == 0


@pytest.mark.asyncio
async def test_each_further_run_of_wrong_codes_is_held_off_for_longer(gateway):
    await gateway.enroll(ADMIN, gateway.sign_in(ADMIN))
    laptop = gateway.sign_in(ADMIN, sid="sess_laptop")
    row = gateway.fake.factors[ADMIN]
    holds = []

    for _run in range(4):
        for n in range(tf.LOCK_EVERY_ATTEMPTS):
            await gateway.client.post(
                "/auth/two-factor/verify", headers=laptop, json={"code": f"{n:06d}"}
            )
        holds.append((row["locked_until"] - datetime.now(UTC)).total_seconds())
        row["locked_until"] = datetime.now(UTC) - timedelta(seconds=1)  # the hold runs out

    assert [round(hold / tf.LOCK_SECONDS) for hold in holds] == [1, 2, 4, 8]


# --- the administrator check --------------------------------------------------


@pytest.mark.asyncio
async def test_nothing_changes_while_the_deployment_does_not_require_it(gateway):
    """The default: an administrator session and the admin token work as before."""
    headers = gateway.sign_in(ADMIN)

    assert (await _admin_read(gateway, headers)).status_code == 200
    # The check itself is skipped, not passed: no extra lookup per request.
    gateway.store.get_session_two_factor.assert_not_awaited()
    assert (await _open_model(gateway, headers)).status_code == 200
    token = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
    assert (await _open_model(gateway, token)).status_code == 200


@pytest.mark.asyncio
async def test_an_administrator_without_an_authenticator_has_no_admin_access(gateway, required):
    headers = gateway.sign_in(ADMIN)

    refused = await _admin_read(gateway, headers)

    assert refused.status_code == 403
    assert refused.json()["error_code"] == "TWO_FACTOR_ENROLLMENT_REQUIRED"
    assert refused.json()["reason"] == "enrollment"
    # The way out is the account's own routes, which this does not block.
    status = await gateway.client.get("/auth/two-factor", headers=headers)
    assert status.json()["required"] is True
    await gateway.enroll(ADMIN, headers)
    assert (await _admin_read(gateway, headers)).status_code == 200


@pytest.mark.asyncio
async def test_each_session_gives_a_code_before_it_is_an_administrators(gateway, required):
    await gateway.enroll(ADMIN, gateway.sign_in(ADMIN))
    laptop = gateway.sign_in(ADMIN, sid="sess_laptop")

    refused = await _admin_read(gateway, laptop)
    assert refused.status_code == 403
    assert refused.json()["error_code"] == "TWO_FACTOR_REQUIRED"
    assert refused.json()["reason"] == "unverified"

    await gateway.client.post(
        "/auth/two-factor/verify", headers=laptop, json={"code": gateway.code(ADMIN)}
    )
    assert (await _admin_read(gateway, laptop)).status_code == 200
    # Once per session, not once per visit: a day later it is still let in.
    gateway.age_verification(ADMIN, 24 * 60, sid="sess_laptop")
    assert (await _admin_read(gateway, laptop)).status_code == 200


@pytest.mark.asyncio
async def test_a_session_that_turns_up_in_another_client_is_asked_again(gateway, required):
    """A browser's session, copied and presented by a script."""
    browser = gateway.sign_in(ADMIN)
    await gateway.enroll(ADMIN, browser)
    assert (await _admin_read(gateway, browser)).status_code == 200
    copied = {**browser, "User-Agent": CURL}

    refused = await _admin_read(gateway, copied)
    assert refused.status_code == 403
    assert refused.json()["error_code"] == "TWO_FACTOR_REQUIRED"
    assert refused.json()["reason"] == "environment_changed"
    assert (await _open_model(gateway, copied)).status_code == 403
    gateway.store.set_model_visibility_override.assert_not_awaited()

    # The owner, still in the browser, is not disturbed by the attempt.
    assert (await _admin_read(gateway, browser)).status_code == 200
    # A newer browser version is the same environment.
    updated = {**browser, "User-Agent": CHROME.replace("Chrome/130", "Chrome/131")}
    assert (await _admin_read(gateway, updated)).status_code == 200


@pytest.mark.asyncio
async def test_the_admin_token_only_reads_where_a_second_factor_is_required(gateway, required):
    token = {"Authorization": f"Bearer {ADMIN_TOKEN}"}

    assert (await _admin_read(gateway, token)).status_code == 200
    refused = await _open_model(gateway, token)

    assert refused.status_code == 403
    assert "read-only" in refused.json()["detail"]
    gateway.store.set_model_visibility_override.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_member_is_not_an_administrator_whatever_it_proves(gateway, required):
    headers = gateway.sign_in(MEMBER)
    await gateway.enroll(MEMBER, headers)

    refused = await _admin_read(gateway, headers)

    assert refused.status_code == 403
    assert refused.json()["detail"] == "Admin access required."


# --- changes that outlast the session ----------------------------------------


@pytest.mark.asyncio
async def test_a_recent_code_confirms_a_role_change_and_an_old_one_does_not(gateway):
    headers = gateway.sign_in(ADMIN)
    await gateway.enroll(ADMIN, headers)

    raised = await gateway.client.patch(
        f"/admin/users/{MEMBER}", headers=headers, json={"role": "pro"}
    )
    assert raised.status_code == 200, raised.text
    gateway.store.update_user_fields.assert_awaited_once_with(MEMBER, role="pro")

    gateway.store.update_user_fields.reset_mock()
    gateway.age_verification(ADMIN, 11)
    for body in ({"role": "internal"}, {"role": "internal", "password": PASSWORD}):
        stale = await gateway.client.patch(f"/admin/users/{MEMBER}", headers=headers, json=body)
        # The password no longer answers for an account that has an authenticator.
        assert stale.status_code == 403
        assert stale.json()["error_code"] == "TWO_FACTOR_REQUIRED"
        assert stale.json()["reason"] == "stale"
        assert "role needs a code" in stale.json()["message"]
    gateway.store.update_user_fields.assert_not_awaited()

    await gateway.client.post(
        "/auth/two-factor/verify", headers=headers, json={"code": gateway.code(ADMIN)}
    )
    again = await gateway.client.patch(
        f"/admin/users/{MEMBER}", headers=headers, json={"role": "internal"}
    )
    assert again.status_code == 200, again.text


@pytest.mark.asyncio
async def test_an_administrator_without_an_authenticator_still_confirms_by_password(gateway):
    headers = gateway.sign_in(ADMIN)

    refused = await gateway.client.patch(
        f"/admin/users/{MEMBER}", headers=headers, json={"role": "pro"}
    )
    assert refused.status_code == 400
    accepted = await gateway.client.patch(
        f"/admin/users/{MEMBER}", headers=headers, json={"role": "pro", "password": PASSWORD}
    )

    assert accepted.status_code == 200, accepted.text


@pytest.mark.asyncio
async def test_opening_a_model_needs_a_recent_code_from_an_enrolled_administrator(gateway):
    headers = gateway.sign_in(ADMIN)
    await gateway.enroll(ADMIN, headers)
    gateway.age_verification(ADMIN, 11)

    stale = await _open_model(gateway, headers)
    assert stale.status_code == 403
    assert stale.json()["reason"] == "stale"
    gateway.store.set_model_visibility_override.assert_not_awaited()

    await gateway.client.post(
        "/auth/two-factor/verify", headers=headers, json={"code": gateway.code(ADMIN)}
    )
    assert (await _open_model(gateway, headers)).status_code == 200
    gateway.store.set_model_visibility_override.assert_awaited_once()


@pytest.mark.asyncio
async def test_an_account_gets_the_admin_role_only_once_it_has_an_authenticator(gateway, required):
    headers = gateway.sign_in(ADMIN)
    await gateway.enroll(ADMIN, headers)

    refused = await gateway.client.patch(
        f"/admin/users/{MEMBER}", headers=headers, json={"role": "admin"}
    )
    assert refused.status_code == 409
    assert "has not set up two-factor authentication" in refused.json()["detail"]
    gateway.store.update_user_fields.assert_not_awaited()
    # A role below admin does not ask it of the account.
    assert (
        await gateway.client.patch(f"/admin/users/{MEMBER}", headers=headers, json={"role": "pro"})
    ).status_code == 200

    await gateway.enroll(MEMBER, gateway.sign_in(MEMBER))
    promoted = await gateway.client.patch(
        f"/admin/users/{MEMBER}", headers=headers, json={"role": "admin"}
    )
    assert promoted.status_code == 200, promoted.text


# --- changing and removing the authenticator ----------------------------------


@pytest.mark.asyncio
async def test_a_code_checked_before_the_device_was_replaced_verifies_nothing(gateway):
    """Reading the device and accepting its code are not one step; a replacement can fall between."""
    headers = gateway.sign_in(ADMIN)
    await gateway.enroll(ADMIN, headers)
    laptop = gateway.sign_in(ADMIN, sid="sess_laptop")
    old_code = gateway.code(ADMIN)
    setup = await gateway.client.post("/auth/two-factor/setup", headers=headers, json={})
    new_secret = setup.json()["secret"]

    # The laptop's request has read the old device; before its code is
    # accepted, the other session confirms the new one.
    count_attempt = gateway.store.begin_two_factor_attempt.side_effect

    async def replace_then_count(*args, **kwargs):
        confirmed = await gateway.client.post(
            "/auth/two-factor/confirm",
            headers=headers,
            json={"code": tf.totp_at(new_secret, tf.current_step())},
        )
        assert confirmed.status_code == 200, confirmed.text
        return await count_attempt(*args, **kwargs)

    gateway.store.begin_two_factor_attempt.side_effect = replace_then_count

    late = await gateway.client.post(
        "/auth/two-factor/verify", headers=laptop, json={"code": old_code}
    )

    assert late.status_code == 400
    assert gateway.fake.sessions[(ADMIN, "sess_laptop")]["verified_at"] is None
    assert tf.decrypt_secret(gateway.fake.factors[ADMIN]["secret_encrypted"]) == new_secret


@pytest.mark.asyncio
async def test_replacing_the_device_takes_a_code_from_the_old_one(gateway):
    headers = gateway.sign_in(ADMIN)
    await gateway.enroll(ADMIN, headers)
    laptop = gateway.sign_in(ADMIN, sid="sess_laptop")
    await gateway.client.post(
        "/auth/two-factor/verify", headers=laptop, json={"code": gateway.code(ADMIN)}
    )
    old_secret = tf.decrypt_secret(gateway.fake.factors[ADMIN]["secret_encrypted"])

    # A session that knows only the password cannot swap the device.
    stranger = gateway.sign_in(ADMIN, sid="sess_stranger")
    refused = await gateway.client.post(
        "/auth/two-factor/setup", headers=stranger, json={"password": PASSWORD}
    )
    assert refused.status_code == 403
    assert refused.json()["error_code"] == "TWO_FACTOR_REQUIRED"

    setup = await gateway.client.post("/auth/two-factor/setup", headers=headers, json={})
    assert setup.status_code == 200, setup.text
    new_secret = setup.json()["secret"]
    # Until the new device proves itself, the old one is the account's.
    assert tf.decrypt_secret(gateway.fake.factors[ADMIN]["secret_encrypted"]) == old_secret

    confirmed = await gateway.client.post(
        "/auth/two-factor/confirm",
        headers=headers,
        json={"code": tf.totp_at(new_secret, tf.current_step())},
    )
    assert confirmed.status_code == 200, confirmed.text
    assert tf.decrypt_secret(gateway.fake.factors[ADMIN]["secret_encrypted"]) == new_secret
    assert gateway.audit("two_factor.enable")[-1]["details"]["replaced"] is True
    # What the other session proved, it proved with the old device.
    assert (await gateway.client.get("/auth/two-factor", headers=laptop)).json()[
        "session_verified"
    ] is False
    assert (await gateway.client.get("/auth/two-factor", headers=headers)).json()[
        "session_verified"
    ] is True


@pytest.mark.asyncio
async def test_new_recovery_codes_replace_the_old_ones(gateway):
    headers = gateway.sign_in(ADMIN)
    old_codes = await gateway.enroll(ADMIN, headers)

    fresh = await gateway.client.post("/auth/two-factor/recovery-codes", headers=headers)
    assert fresh.status_code == 200, fresh.text
    new_codes = fresh.json()["recovery_codes"]
    assert set(new_codes).isdisjoint(old_codes)

    laptop = gateway.sign_in(ADMIN, sid="sess_laptop")
    assert (
        await gateway.client.post(
            "/auth/two-factor/verify", headers=laptop, json={"code": old_codes[0]}
        )
    ).status_code == 400
    assert (
        await gateway.client.post(
            "/auth/two-factor/verify", headers=laptop, json={"code": new_codes[0]}
        )
    ).status_code == 200

    gateway.age_verification(ADMIN, 11)
    stale = await gateway.client.post("/auth/two-factor/recovery-codes", headers=headers)
    assert stale.status_code == 403


@pytest.mark.asyncio
async def test_a_member_removes_its_authenticator_with_a_recent_code(gateway):
    headers = gateway.sign_in(MEMBER)
    await gateway.enroll(MEMBER, headers)
    gateway.age_verification(MEMBER, 11)

    stale = await gateway.client.delete("/auth/two-factor", headers=headers)
    assert stale.status_code == 403
    assert MEMBER in gateway.fake.factors

    await gateway.client.post(
        "/auth/two-factor/verify", headers=headers, json={"code": gateway.code(MEMBER)}
    )
    removed = await gateway.client.delete("/auth/two-factor", headers=headers)
    assert removed.status_code == 200, removed.text
    assert removed.json()["enabled"] is False
    assert MEMBER not in gateway.fake.factors
    assert gateway.audit("two_factor.disable")


@pytest.mark.asyncio
async def test_an_administrator_keeps_its_authenticator_where_one_is_required(gateway, required):
    headers = gateway.sign_in(ADMIN)
    await gateway.enroll(ADMIN, headers)

    refused = await gateway.client.delete("/auth/two-factor", headers=headers)

    assert refused.status_code == 409
    assert ADMIN in gateway.fake.factors


@pytest.mark.asyncio
async def test_an_administrator_removes_a_lost_authenticator_of_another_account(gateway, required):
    headers = gateway.sign_in(ADMIN)
    await gateway.enroll(ADMIN, headers)
    await gateway.enroll(OTHER_ADMIN, gateway.sign_in(OTHER_ADMIN))
    other = gateway.sign_in(OTHER_ADMIN, sid="sess_other")
    await gateway.client.post(
        "/auth/two-factor/verify", headers=other, json={"code": gateway.code(OTHER_ADMIN)}
    )
    assert (await _admin_read(gateway, other)).status_code == 200

    own = await gateway.client.post(f"/admin/users/{ADMIN}/two-factor/reset", headers=headers)
    assert own.status_code == 409
    token = await gateway.client.post(
        f"/admin/users/{OTHER_ADMIN}/two-factor/reset",
        headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
    )
    assert token.status_code == 403
    gateway.age_verification(ADMIN, 11)
    stale = await gateway.client.post(
        f"/admin/users/{OTHER_ADMIN}/two-factor/reset", headers=headers
    )
    assert stale.status_code == 403
    assert OTHER_ADMIN in gateway.fake.factors

    await gateway.client.post(
        "/auth/two-factor/verify", headers=headers, json={"code": gateway.code(ADMIN)}
    )
    reset = await gateway.client.post(
        f"/admin/users/{OTHER_ADMIN}/two-factor/reset", headers=headers
    )
    assert reset.status_code == 200, reset.text
    assert reset.json() == {"user_id": OTHER_ADMIN, "removed": True}
    [row] = gateway.audit("two_factor.reset")
    assert (row["actor"].type, row["actor"].id) == ("admin", ADMIN)
    assert row["target"].id == OTHER_ADMIN
    # The account is an administrator in name until it adds a device again.
    refused = await _admin_read(gateway, other)
    assert refused.json()["error_code"] == "TWO_FACTOR_ENROLLMENT_REQUIRED"
