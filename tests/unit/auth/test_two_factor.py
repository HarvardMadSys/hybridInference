"""The second factor's arithmetic and policy (``serving.auth.two_factor``), without a database."""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta

import pytest

from serving.auth import two_factor as tf
from serving.config.settings import get_settings
from serving.servers.auth import encrypt_api_key

# RFC 6238 appendix B: the SHA-1 secret and what it yields at each time. The
# RFC prints eight digits; an authenticator app shows the last six.
RFC_SECRET = base64.b32encode(b"12345678901234567890").decode().rstrip("=")
RFC_VECTORS = [
    (59, "94287082"),
    (1111111109, "07081804"),
    (1111111111, "14050471"),
    (1234567890, "89005924"),
    (2000000000, "69279037"),
    (20000000000, "65353130"),
]

CHROME_MAC = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)


@pytest.fixture
def secret_env(monkeypatch):
    monkeypatch.setenv("API_KEY_SECRET", "unit-test-secret")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.mark.parametrize(("at", "eight_digits"), RFC_VECTORS)
def test_codes_match_the_rfc_vectors(at, eight_digits):
    assert tf.totp_at(RFC_SECRET, at // tf.TOTP_PERIOD_SECONDS) == eight_digits[-6:]


def test_a_generated_secret_is_what_an_app_expects():
    secret = tf.generate_secret()
    # 160 bits as unpadded base32, and never the same twice.
    assert len(secret) == 32
    assert set(secret) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZ234567")
    assert secret != tf.generate_secret()
    # The arithmetic accepts it as generated.
    assert len(tf.totp_at(secret, 1)) == 6


@pytest.mark.parametrize("offset", [-1, 0, 1])
def test_a_code_is_accepted_one_step_either_side(offset):
    now = 1_700_000_000.0
    step = tf.current_step(now) + offset
    assert tf.matching_step(RFC_SECRET, tf.totp_at(RFC_SECRET, step), now=now) == step


@pytest.mark.parametrize("offset", [-2, 2])
def test_a_code_from_further_away_is_not(offset):
    now = 1_700_000_000.0
    stale = tf.totp_at(RFC_SECRET, tf.current_step(now) + offset)
    assert tf.matching_step(RFC_SECRET, stale, now=now) is None


def test_a_code_is_good_once():
    """The step a code was accepted for, and every earlier one, are spent."""
    now = 1_700_000_000.0
    step = tf.current_step(now)
    code = tf.totp_at(RFC_SECRET, step)
    assert tf.matching_step(RFC_SECRET, code, now=now, after_step=step) is None
    assert tf.matching_step(RFC_SECRET, code, now=now, after_step=step - 1) == step
    earlier = tf.totp_at(RFC_SECRET, step - 1)
    assert tf.matching_step(RFC_SECRET, earlier, now=now, after_step=step) is None


@pytest.mark.parametrize("code", ["", "12345", "1234567", "12345a", "١٢٣٤٥٦", "ABCDE-FGHJK"])
def test_what_is_not_six_ascii_digits_is_never_a_code(code):
    assert not tf.looks_like_totp(code)
    assert tf.matching_step(RFC_SECRET, code, now=59) is None


def test_the_enrollment_link_names_the_site_and_the_account():
    uri = tf.provisioning_uri("ABC234", account="ann@example.com", issuer="Free Inference")
    assert uri == (
        "otpauth://totp/Free%20Inference%3Aann%40example.com?secret=ABC234&issuer=Free%20Inference"
    )


def test_recovery_codes_are_distinct_and_hash_however_they_are_typed():
    codes = tf.generate_recovery_codes()
    assert len(codes) == len(set(codes)) == tf.RECOVERY_CODE_COUNT
    code = codes[0]
    assert len(code) == 11 and code[5] == "-"
    assert not tf.looks_like_totp(tf.normalize_code(code))
    typed = f"  {code.lower().replace('-', ' ')} "
    assert tf.hash_recovery_code(typed) == tf.hash_recovery_code(code)
    assert tf.hash_recovery_code(codes[1]) != tf.hash_recovery_code(code)


def test_a_stored_secret_opens_with_this_deployments_key_only(secret_env, monkeypatch):
    secret = tf.generate_secret()
    stored = tf.encrypt_secret(secret)
    assert secret not in stored
    assert tf.decrypt_secret(stored) == secret
    # Not interchangeable with the saved-API-key cipher under the same master.
    assert tf.decrypt_secret(encrypt_api_key(secret)) is None
    assert tf.decrypt_secret("not a token") is None

    monkeypatch.setenv("API_KEY_SECRET", "another-deployment")
    get_settings.cache_clear()
    assert tf.decrypt_secret(stored) is None


@pytest.mark.parametrize(
    ("user_agent", "environment"),
    [
        (CHROME_MAC, "chrome/macos"),
        (CHROME_MAC.replace("Chrome/130.0.0.0", "Chrome/131.0.0.0"), "chrome/macos"),
        (CHROME_MAC + " Edg/130.0.0.0", "edge/macos"),
        (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
            "(KHTML, like Gecko) Version/18.0 Safari/605.1.15",
            "safari/macos",
        ),
        (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:132.0) Gecko/20100101 Firefox/132.0",
            "firefox/windows",
        ),
        (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 "
            "(KHTML, like Gecko) CriOS/130.0 Mobile/15E148 Safari/604.1",
            "chrome/ios",
        ),
        ("curl/7.88.1", "curl/other"),
        ("python-requests/2.32.3", "python/other"),
        ("", "other/other"),
        (None, "other/other"),
    ],
)
def test_the_environment_is_the_client_family_without_versions(user_agent, environment):
    assert tf.client_environment(user_agent) == environment


def test_what_a_session_has_proved():
    now = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)
    verified = tf.SessionTwoFactor(
        enrolled=True, verified_at=now - timedelta(minutes=30), environment="chrome/macos"
    )
    assert tf.SessionTwoFactor().refusal("chrome/macos") == tf.REASON_ENROLLMENT
    assert tf.SessionTwoFactor(enrolled=True).refusal("chrome/macos") == tf.REASON_UNVERIFIED
    assert verified.refusal("chrome/macos") is None
    assert verified.refusal("curl/other") == tf.REASON_ENVIRONMENT
    # A code half an hour old signs the session in, and no longer confirms a change.
    ten = timedelta(minutes=10)
    assert verified.refusal("chrome/macos", within=ten, now=now) == tf.REASON_STALE
    fresh = tf.SessionTwoFactor(
        enrolled=True, verified_at=now - timedelta(minutes=9), environment="chrome/macos"
    )
    assert fresh.refusal("chrome/macos", within=ten, now=now) is None
    # The environment is checked before the age: a fresh code from elsewhere is no code.
    assert fresh.refusal("curl/other", within=ten, now=now) == tf.REASON_ENVIRONMENT


@pytest.mark.parametrize("row", [None, object(), "row", {"enrolled": "yes"}, {}])
def test_an_answer_that_is_not_a_row_proves_nothing(row):
    assert tf.SessionTwoFactor.from_row(row) == tf.SessionTwoFactor()


def test_only_a_confirmed_row_is_an_authenticator():
    assert not tf.has_authenticator(None)
    assert not tf.has_authenticator({"confirmed_at": None, "pending_secret_encrypted": "x"})
    assert not tf.has_authenticator(object())
    assert tf.has_authenticator({"confirmed_at": datetime(2026, 11, 15, tzinfo=UTC)})
