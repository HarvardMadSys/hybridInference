"""The OpenID Connect client, against a provider that lives in this process.

The provider is an ``httpx.MockTransport``: it serves a discovery document,
its keys, a token endpoint and UserInfo, and signs ID tokens with a real RSA
key, so every check in ``serving.auth.oidc`` runs on real signatures.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import time
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest

from serving.auth import oidc
from tests.fixtures.oidc_provider import (
    CLIENT_ID,
    ISSUER,
    OTHER_KEY,
    FakeOidcProvider as Provider,
    id_token_claims as _claims,
    sign_id_token as _sign,
)

CLIENT_SECRET = "s3cret/with:odd chars"
REDIRECT_URI = "https://console.example.org/auth/sso/example-univ/callback"

SETTINGS = oidc.OidcSettings(issuer=ISSUER, client_id=CLIENT_ID, client_secret=CLIENT_SECRET)


@pytest.fixture
def provider(monkeypatch) -> Provider:
    fake = Provider()
    oidc.clear_caches()
    monkeypatch.setattr(oidc, "_transport", httpx.MockTransport(fake.handler))
    yield fake
    oidc.clear_caches()


async def _begin(**kwargs: Any) -> tuple[dict[str, list[str]], dict[str, Any]]:
    url, state = await oidc.begin(SETTINGS, redirect_uri=REDIRECT_URI, state="state-1", **kwargs)
    return parse_qs(urlsplit(url).query), state


async def _nonce_and_state(**kwargs: Any) -> tuple[str, dict[str, Any]]:
    params, state = await _begin(**kwargs)
    return params["nonce"][0], state


async def _complete(state: dict[str, Any], settings: oidc.OidcSettings = SETTINGS):
    return await oidc.complete(
        settings, redirect_uri=REDIRECT_URI, code="code-1", protocol_state=state
    )


async def _refused(provider: Provider, **claim_overrides: Any) -> oidc.SsoProtocolError:
    nonce, state = await _nonce_and_state()
    provider.id_token = _sign(_claims(**{"nonce": nonce, **claim_overrides}))
    with pytest.raises(oidc.SsoProtocolError) as excinfo:
        await _complete(state)
    return excinfo.value


# -- the redirect ---------------------------------------------------------------


async def test_begin_sends_the_browser_to_the_authorization_endpoint(provider):
    url, state = await oidc.begin(
        SETTINGS, redirect_uri=REDIRECT_URI, state="state-1", login_hint="alice@example.edu"
    )

    assert url.startswith(f"{ISSUER}/authorize?")
    params = parse_qs(urlsplit(url).query)
    assert params["response_type"] == ["code"]
    assert params["client_id"] == [CLIENT_ID]
    assert params["redirect_uri"] == [REDIRECT_URI]
    assert params["scope"] == ["openid email profile"]
    assert params["state"] == ["state-1"]
    assert params["login_hint"] == ["alice@example.edu"]
    assert params["code_challenge_method"] == ["S256"]
    assert "prompt" not in params and "max_age" not in params

    # The stored state holds the verifier for the challenge and only a hash of the nonce.
    digest = hashlib.sha256(state["code_verifier"].encode()).digest()
    assert params["code_challenge"] == [base64.urlsafe_b64encode(digest).rstrip(b"=").decode()]
    assert state["nonce_hash"] == oidc.hash_value(params["nonce"][0])
    assert params["nonce"][0] not in state.values()
    assert state["fresh_login"] is False


async def test_a_reauthentication_asks_the_provider_for_a_fresh_sign_in(provider):
    params, state = await _begin(fresh_login=True)

    assert params["prompt"] == ["login"]
    assert params["max_age"] == ["0"]
    assert state["fresh_login"] is True


async def test_each_redirect_has_its_own_nonce_and_verifier(provider):
    first_params, first = await _begin()
    second_params, second = await _begin()

    assert first_params["nonce"] != second_params["nonce"]
    assert first["code_verifier"] != second["code_verifier"]


async def test_an_endpoint_that_already_has_a_query_keeps_it(provider):
    provider.discovery_overrides["authorization_endpoint"] = f"{ISSUER}/authorize?tenant=main"

    url, _state = await oidc.begin(SETTINGS, redirect_uri=REDIRECT_URI, state="state-1")

    params = parse_qs(urlsplit(url).query)
    assert params["tenant"] == ["main"]
    assert params["state"] == ["state-1"]


# -- a good answer ----------------------------------------------------------------


async def test_complete_returns_who_the_provider_says_signed_in(provider):
    nonce, state = await _nonce_and_state()
    provider.id_token = _sign(_claims(nonce))

    identity = await _complete(state)

    assert identity == oidc.ExternalIdentity(
        issuer=ISSUER,
        subject="subject-1",
        email="alice@example.edu",
        email_verified=True,
        name="Alice Example",
        authenticated_at=None,
    )


async def test_the_code_is_exchanged_with_the_verifier_and_basic_client_authentication(provider):
    nonce, state = await _nonce_and_state()
    provider.id_token = _sign(_claims(nonce))

    await _complete(state)

    token_request = next(r for r in provider.requests if r.url.path.endswith("/token"))
    form = parse_qs(token_request.content.decode())
    assert form == {
        "grant_type": ["authorization_code"],
        "code": ["code-1"],
        "redirect_uri": [REDIRECT_URI],
        "code_verifier": [state["code_verifier"]],
    }
    # Both parts are form-encoded before Basic encoding (RFC 6749 2.3.1).
    credentials = base64.b64decode(token_request.headers["authorization"].split()[1]).decode()
    assert credentials == "hybridinference:s3cret%2Fwith%3Aodd%20chars"


async def test_post_client_authentication_sends_the_secret_in_the_form(provider):
    settings = oidc.OidcSettings(
        issuer=ISSUER,
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        client_auth_method="client_secret_post",
    )
    nonce, state = await _nonce_and_state()
    provider.id_token = _sign(_claims(nonce))

    await _complete(state, settings)

    token_request = next(r for r in provider.requests if r.url.path.endswith("/token"))
    form = parse_qs(token_request.content.decode())
    assert form["client_id"] == [CLIENT_ID]
    assert form["client_secret"] == [CLIENT_SECRET]
    assert "authorization" not in token_request.headers


async def test_an_email_the_provider_did_not_vouch_for_is_reported_as_such(provider):
    nonce, state = await _nonce_and_state()
    provider.id_token = _sign(_claims(nonce, email_verified=None))
    assert (await _complete(state)).email_verified is None

    nonce, state = await _nonce_and_state()
    provider.id_token = _sign(_claims(nonce, email_verified="true"))
    assert (await _complete(state)).email_verified is True

    nonce, state = await _nonce_and_state()
    provider.id_token = _sign(_claims(nonce, email_verified=False))
    assert (await _complete(state)).email_verified is False


async def test_a_claim_the_provider_is_required_to_send_must_have_the_required_value(provider):
    # One issuer for every Google account: only hd says the account is the organization's.
    settings = oidc.OidcSettings(
        issuer=ISSUER,
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        required_claims=(("hd", "example.edu"),),
    )

    nonce, state = await _nonce_and_state()
    provider.id_token = _sign(_claims(nonce, hd="example.edu"))
    assert (await _complete(state, settings)).subject == "subject-1"

    for claims in ({}, {"hd": "elsewhere.org"}, {"hd": ["example.edu"]}):
        nonce, state = await _nonce_and_state()
        provider.id_token = _sign(_claims(nonce, **claims))
        with pytest.raises(oidc.SsoProtocolError) as excinfo:
            await _complete(state, settings)
        assert excinfo.value.code == "failed"
        assert "hd" in excinfo.value.reason


async def test_an_audience_list_is_accepted_when_this_client_is_the_authorized_party(provider):
    nonce, state = await _nonce_and_state()
    provider.id_token = _sign(_claims(nonce, aud=[CLIENT_ID, "another-api"], azp=CLIENT_ID))

    assert (await _complete(state)).subject == "subject-1"


async def test_a_token_without_a_key_id_uses_the_providers_only_key(provider):
    nonce, state = await _nonce_and_state()
    provider.id_token = _sign(_claims(nonce), kid=None)

    assert (await _complete(state)).subject == "subject-1"


# -- UserInfo ---------------------------------------------------------------------


async def test_an_email_missing_from_the_id_token_is_read_from_userinfo(provider):
    nonce, state = await _nonce_and_state()
    provider.id_token = _sign(_claims(nonce, email=None, email_verified=None, name=None))
    provider.userinfo = {
        "sub": "subject-1",
        "email": "alice@example.edu",
        "email_verified": True,
        "name": "Alice Example",
    }

    identity = await _complete(state)

    assert (identity.email, identity.email_verified, identity.name) == (
        "alice@example.edu",
        True,
        "Alice Example",
    )
    userinfo_request = next(r for r in provider.requests if r.url.path.endswith("/userinfo"))
    assert userinfo_request.headers["authorization"] == "Bearer at-1"


async def test_userinfo_is_not_asked_when_the_id_token_has_the_email(provider):
    nonce, state = await _nonce_and_state()
    provider.id_token = _sign(_claims(nonce))
    provider.userinfo = {"sub": "subject-1", "email": "other@example.edu"}

    identity = await _complete(state)

    assert identity.email == "alice@example.edu"
    assert provider.count("/userinfo") == 0


async def test_userinfo_for_another_subject_is_refused(provider):
    nonce, state = await _nonce_and_state()
    provider.id_token = _sign(_claims(nonce, email=None))
    provider.userinfo = {"sub": "someone-else", "email": "alice@example.edu"}

    with pytest.raises(oidc.SsoProtocolError) as excinfo:
        await _complete(state)

    assert excinfo.value.code == "failed"


async def test_no_email_anywhere_is_an_identity_without_one(provider):
    provider.discovery_overrides["userinfo_endpoint"] = None
    nonce, state = await _nonce_and_state()
    provider.id_token = _sign(_claims(nonce, email=None, email_verified=None))

    identity = await _complete(state)

    assert identity.email is None
    assert identity.email_verified is None
    assert provider.count("/userinfo") == 0


async def test_a_userinfo_endpoint_that_fails_leaves_the_identity_without_an_email(provider):
    nonce, state = await _nonce_and_state()
    provider.id_token = _sign(_claims(nonce, email=None, email_verified=None))
    provider.userinfo = None  # the endpoint answers 404

    identity = await _complete(state)

    assert (identity.subject, identity.email) == ("subject-1", None)
    assert provider.count("/userinfo") == 1


# -- answers that are refused -------------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"iss": "https://evil.example.com"}, id="another issuer"),
        pytest.param({"aud": "another-client"}, id="another audience"),
        pytest.param({"aud": [CLIENT_ID, "another-api"]}, id="audience list without azp"),
        pytest.param({"azp": "another-client"}, id="another authorized party"),
        pytest.param({"exp": int(time.time()) - 3600}, id="expired"),
        pytest.param({"iat": int(time.time()) + 3600}, id="issued in the future"),
        pytest.param({"nonce": "not-this-flow"}, id="another flow's nonce"),
        pytest.param({"nonce": None}, id="no nonce"),
        pytest.param({"sub": None}, id="no subject"),
        pytest.param({"sub": ""}, id="empty subject"),
    ],
)
async def test_an_id_token_that_does_not_validate_is_refused(provider, overrides):
    error = await _refused(provider, **overrides)

    assert error.code == "failed"


async def test_a_clock_a_minute_apart_is_tolerated(provider):
    nonce, state = await _nonce_and_state()
    provider.id_token = _sign(_claims(nonce, exp=int(time.time()) - 30))

    assert (await _complete(state)).subject == "subject-1"


async def test_an_unsigned_token_is_refused(provider):
    nonce, state = await _nonce_and_state()
    provider.id_token = jwt.encode(_claims(nonce), None, algorithm="none")

    with pytest.raises(oidc.SsoProtocolError) as excinfo:
        await _complete(state)

    assert excinfo.value.code == "failed"
    # Refused on the algorithm alone: the keys are never fetched for it.
    assert provider.count("/keys") == 0


async def test_a_token_signed_with_the_client_secret_is_refused(provider):
    nonce, state = await _nonce_and_state()
    provider.id_token = jwt.encode(_claims(nonce), "x" * 40, algorithm="HS256")

    with pytest.raises(oidc.SsoProtocolError) as excinfo:
        await _complete(state)

    assert excinfo.value.code == "failed"


async def test_a_token_signed_with_a_key_the_provider_does_not_publish_is_refused(provider):
    nonce, state = await _nonce_and_state()
    provider.id_token = _sign(_claims(nonce), key=OTHER_KEY)

    with pytest.raises(oidc.SsoProtocolError) as excinfo:
        await _complete(state)

    assert excinfo.value.code == "failed"


async def test_a_malformed_token_is_refused(provider):
    _nonce, state = await _nonce_and_state()
    provider.id_token = "not-a-jwt"

    with pytest.raises(oidc.SsoProtocolError) as excinfo:
        await _complete(state)

    assert excinfo.value.code == "failed"


async def test_a_reauthentication_needs_the_provider_to_say_when_the_user_signed_in(provider):
    nonce, state = await _nonce_and_state(fresh_login=True)
    provider.id_token = _sign(_claims(nonce))
    with pytest.raises(oidc.SsoProtocolError) as excinfo:
        await _complete(state)
    assert excinfo.value.code == "failed"

    signed_in = int(time.time()) - 5
    nonce, state = await _nonce_and_state(fresh_login=True)
    provider.id_token = _sign(_claims(nonce, auth_time=signed_in))
    identity = await _complete(state)
    assert int(identity.authenticated_at.timestamp()) == signed_in


# -- keys -------------------------------------------------------------------------


async def test_the_providers_keys_and_configuration_are_fetched_once(provider):
    for _ in range(3):
        nonce, state = await _nonce_and_state()
        provider.id_token = _sign(_claims(nonce))
        await _complete(state)

    assert provider.count("/.well-known/openid-configuration") == 1
    assert provider.count("/keys") == 1


async def test_a_rotated_key_is_picked_up_by_refetching(provider, monkeypatch):
    monkeypatch.setattr(oidc, "_JWKS_REFETCH_INTERVAL_SECONDS", 0.0)
    nonce, state = await _nonce_and_state()
    provider.id_token = _sign(_claims(nonce))
    await _complete(state)

    provider.keys = {"key-2": OTHER_KEY}
    nonce, state = await _nonce_and_state()
    provider.id_token = _sign(_claims(nonce), key=OTHER_KEY, kid="key-2")

    assert (await _complete(state)).subject == "subject-1"
    assert provider.count("/keys") == 2


async def test_unknown_key_ids_do_not_refetch_more_than_once_a_minute(provider):
    nonce, state = await _nonce_and_state()
    provider.id_token = _sign(_claims(nonce))
    await _complete(state)

    for _ in range(3):
        nonce, state = await _nonce_and_state()
        provider.id_token = _sign(_claims(nonce), key=OTHER_KEY, kid="forged")
        with pytest.raises(oidc.SsoProtocolError):
            await _complete(state)

    assert provider.count("/keys") == 1


async def test_one_providers_slow_keys_do_not_hold_up_anothers(provider, monkeypatch):
    answered = asyncio.Event()
    fetched: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        fetched.append(request.url.host)
        if request.url.host == "slow.example.edu":
            await answered.wait()
        return httpx.Response(200, json={"keys": []})

    monkeypatch.setattr(oidc, "_transport", httpx.MockTransport(handler))
    slow = [asyncio.create_task(oidc._keys("https://slow.example.edu/keys")) for _ in range(3)]
    await asyncio.sleep(0.01)

    # Another provider's keys are fetched while the slow one has not answered.
    assert await asyncio.wait_for(oidc._keys("https://quick.example.edu/keys"), 1) == []
    assert not any(task.done() for task in slow)

    answered.set()
    assert await asyncio.gather(*slow) == [[], [], []]
    # The sign-ins that waited for the slow provider fetched its keys once.
    assert sorted(fetched) == ["quick.example.edu", "slow.example.edu"]


async def test_cached_keys_are_read_while_the_same_providers_keys_are_refetched(
    provider, monkeypatch
):
    monkeypatch.setattr(oidc, "_JWKS_REFETCH_INTERVAL_SECONDS", 0.0)
    answered = asyncio.Event()
    answered.set()

    async def handler(request: httpx.Request) -> httpx.Response:
        await answered.wait()
        return httpx.Response(200, json={"keys": []})

    monkeypatch.setattr(oidc, "_transport", httpx.MockTransport(handler))
    address = "https://idp.example.edu/keys"
    await oidc._keys(address)

    answered.clear()
    refetch = asyncio.create_task(oidc._keys(address, refetch=True))
    await asyncio.sleep(0.01)

    assert await asyncio.wait_for(oidc._keys(address), 1) == []
    assert not refetch.done()
    answered.set()
    await refetch


# -- the provider itself ------------------------------------------------------------


async def test_an_unreachable_provider_is_unavailable(provider):
    provider.unreachable = True

    with pytest.raises(oidc.SsoProtocolError) as excinfo:
        await oidc.begin(SETTINGS, redirect_uri=REDIRECT_URI, state="state-1")

    assert excinfo.value.code == "unavailable"


async def test_a_discovery_document_for_another_issuer_is_not_trusted(provider):
    provider.issuer = "https://idp.example.edu/realms/other"

    with pytest.raises(oidc.SsoProtocolError) as excinfo:
        await oidc.begin(SETTINGS, redirect_uri=REDIRECT_URI, state="state-1")

    assert excinfo.value.code == "unavailable"


@pytest.mark.parametrize("endpoint", ["authorization_endpoint", "token_endpoint", "jwks_uri"])
async def test_an_endpoint_that_is_not_https_makes_the_provider_unusable(provider, endpoint):
    provider.discovery_overrides[endpoint] = "http://idp.example.edu/insecure"

    with pytest.raises(oidc.SsoProtocolError) as excinfo:
        await oidc.begin(SETTINGS, redirect_uri=REDIRECT_URI, state="state-1")

    assert excinfo.value.code == "unavailable"


async def test_an_issuer_that_is_not_https_is_never_contacted(provider):
    settings = oidc.OidcSettings(
        issuer="http://idp.example.edu", client_id=CLIENT_ID, client_secret=CLIENT_SECRET
    )

    with pytest.raises(oidc.SsoProtocolError) as excinfo:
        await oidc.begin(settings, redirect_uri=REDIRECT_URI, state="state-1")

    assert excinfo.value.code == "unavailable"
    assert provider.requests == []


@pytest.mark.parametrize(
    ("url", "secure"),
    [
        ("https://idp.example.edu/x", True),
        ("http://localhost:8080/realms/dev", True),
        ("http://127.0.0.1:8080/x", True),
        ("http://idp.example.edu/x", False),
        ("ftp://idp.example.edu/x", False),
        ("https:///no-host", False),
        ("not a url", False),
    ],
)
def test_only_https_and_local_http_count_as_secure(url, secure):
    assert oidc.is_secure_url(url) is secure


async def test_a_refused_code_is_a_failed_sign_in(provider):
    _nonce, state = await _nonce_and_state()
    provider.token_status = 400
    provider.token_response = {"error": "invalid_grant"}

    with pytest.raises(oidc.SsoProtocolError) as excinfo:
        await _complete(state)

    assert excinfo.value.code == "failed"
    assert "invalid_grant" not in excinfo.value.reason


async def test_a_token_response_without_an_id_token_is_refused(provider):
    _nonce, state = await _nonce_and_state()
    provider.token_response = {"access_token": "at-1", "token_type": "Bearer"}

    with pytest.raises(oidc.SsoProtocolError) as excinfo:
        await _complete(state)

    assert excinfo.value.code == "failed"


async def test_a_redirect_from_the_token_endpoint_is_not_followed(provider):
    _nonce, state = await _nonce_and_state()
    provider.token_status = 307
    provider.token_response = {}

    with pytest.raises(oidc.SsoProtocolError):
        await _complete(state)

    assert provider.count("/token") == 1


async def test_reasons_never_carry_the_code_the_secret_or_a_token(provider):
    error = await _refused(provider, aud="another-client")

    for secret in ("code-1", CLIENT_SECRET, provider.id_token):
        assert secret not in error.reason
        assert secret not in str(error)
