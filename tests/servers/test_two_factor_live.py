"""An administrator's second factor, end to end: the real app, real sign-in, real PostgreSQL.

``test_two_factor.py`` covers every rule against a mock store. This follows one
administrator through the gateway as it runs -- password sign-in, the session
cookie, token refresh, the migrated tables -- to show the rules hold where the
pieces meet: what a session proved survives a refresh, belongs to that session
alone, and does not travel to another client.

Run with: make test-db-local ARGS="tests/servers/test_two_factor_live.py"
"""

from __future__ import annotations

import os

import pytest

from serving.auth import two_factor as tf
from serving.config.settings import get_settings
from serving.utils.login_rate_limit import reset_login_rate_limit_state
from tests.fixtures.auth_helpers import seed_test_user

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

CHROME = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)
CURL = "curl/7.88.1"
SETTING = "ADMIN_TWO_FACTOR_REQUIRED"


@pytest.fixture
def two_factor_required(auth_app):
    """Turn the requirement on for one test, as an operator's ``.env`` would."""
    before = os.environ.get(SETTING)
    os.environ[SETTING] = "true"
    get_settings.cache_clear()
    reset_login_rate_limit_state()
    try:
        yield
    finally:
        if before is None:
            os.environ.pop(SETTING, None)
        else:
            os.environ[SETTING] = before
        get_settings.cache_clear()
        reset_login_rate_limit_state()


async def _sign_in(client, user: dict, agent: str = CHROME) -> dict[str, str]:
    response = await client.post(
        "/auth/login",
        json={"email": user["email"], "password": user["password"]},
        headers={"User-Agent": agent},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}", "User-Agent": agent}


async def _admin(client, headers: dict[str, str]):
    return await client.get("/admin/audit-log/actions", headers=headers)


async def test_an_administrator_from_sign_in_to_a_copied_session(
    auth_client, require_db, two_factor_required
):
    user = await seed_test_user(require_db)
    await require_db.update_user_fields(user["id"], role="admin")
    browser = await _sign_in(auth_client, user)

    # The role is not enough: no authenticator, no administrator access.
    refused = await _admin(auth_client, browser)
    assert refused.status_code == 403, refused.text
    assert refused.json()["error_code"] == "TWO_FACTOR_ENROLLMENT_REQUIRED"

    setup = await auth_client.post(
        "/auth/two-factor/setup", headers=browser, json={"password": user["password"]}
    )
    assert setup.status_code == 200, setup.text
    secret = setup.json()["secret"]
    confirmed = await auth_client.post(
        "/auth/two-factor/confirm",
        headers=browser,
        json={"code": tf.totp_at(secret, tf.current_step())},
    )
    assert confirmed.status_code == 200, confirmed.text
    recovery_codes = confirmed.json()["recovery_codes"]
    assert (await _admin(auth_client, browser)).status_code == 200

    # The stored secret is not the one the app was shown.
    row = await require_db.get_two_factor(user["id"])
    assert secret not in row["secret_encrypted"]
    assert tf.decrypt_secret(row["secret_encrypted"]) == secret
    assert row["recovery_codes_left"] == tf.RECOVERY_CODE_COUNT

    # The same session, presented by a script: asked for a code, and the
    # browser it was copied from is not disturbed.
    copied = await _admin(auth_client, {**browser, "User-Agent": CURL})
    assert copied.status_code == 403
    assert copied.json() == {
        "error_code": "TWO_FACTOR_REQUIRED",
        "message": "This needs a code from your authenticator app.",
        "detail": "This needs a code from your authenticator app.",
        "reason": "environment_changed",
        "timestamp": copied.json()["timestamp"],
    }
    assert (await _admin(auth_client, browser)).status_code == 200

    # A refreshed token is the same session, and still verified.
    refreshed = await auth_client.post("/auth/refresh", headers={"User-Agent": CHROME})
    assert refreshed.status_code == 200, refreshed.text
    renewed = {**browser, "Authorization": f"Bearer {refreshed.json()['access_token']}"}
    assert renewed["Authorization"] != browser["Authorization"]
    assert (await _admin(auth_client, renewed)).status_code == 200

    # Signing in again, password and all, is a new session that owes a code.
    second = await _sign_in(auth_client, user)
    owed = await _admin(auth_client, second)
    assert owed.status_code == 403
    assert owed.json()["reason"] == "unverified"
    # The code the first session was confirmed with is spent.
    replayed = await auth_client.post(
        "/auth/two-factor/verify",
        headers=second,
        json={"code": tf.totp_at(secret, row["last_used_step"])},
    )
    assert replayed.status_code == 400
    # A recovery code stands in for the device, once.
    verified = await auth_client.post(
        "/auth/two-factor/verify", headers=second, json={"code": recovery_codes[0]}
    )
    assert verified.status_code == 200, verified.text
    assert verified.json()["recovery_codes_left"] == tf.RECOVERY_CODE_COUNT - 1
    assert (await _admin(auth_client, second)).status_code == 200

    # What happened is in the audit log, attributed to the account.
    _total, log = await require_db.list_audit_log(target_user_id=user["id"], limit=50, offset=0)
    recorded = [(entry["action"], entry["success"]) for entry in log]
    assert ("two_factor.enable", True) in recorded
    assert ("two_factor.verify", False) in recorded
    assert ("two_factor.verify", True) in recorded


async def test_without_the_setting_an_administrator_is_let_in_as_before(auth_client, require_db):
    user = await seed_test_user(require_db)
    await require_db.update_user_fields(user["id"], role="admin")
    reset_login_rate_limit_state()

    browser = await _sign_in(auth_client, user)

    assert (await _admin(auth_client, browser)).status_code == 200
    assert (await _admin(auth_client, {**browser, "User-Agent": CURL})).status_code == 200
