"""A bearer token with bytes outside ASCII is a wrong token, answered 401, not a 500.

``hmac.compare_digest`` refuses two ``str`` holding non-ASCII characters with a
``TypeError``, and an ``Authorization`` header can carry any byte (Starlette
decodes it as Latin-1). The shared-secret checks compared ``str`` with ``str``,
so ``Bearer \\xe9`` raised inside the check and answered 500.
"""

from __future__ import annotations

import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient

from serving.servers import deps
from serving.servers.routers import internal_auth
from serving.utils.identity_tokens import verify_pkce

NON_ASCII = [b"Bearer \xe9\xe9\xe9", b"Bearer secret\xff", "Bearer sécret".encode()]


@pytest.fixture
def dispatch_app(monkeypatch) -> TestClient:
    monkeypatch.setenv(internal_auth.ENV_DISPATCH_TOKEN, "s3cret-dispatch-token-value")
    app = FastAPI()

    @app.get("/probe", dependencies=[Depends(internal_auth.require_dispatch_token)])
    async def probe() -> dict[str, bool]:
        return {"ok": True}

    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize("header", NON_ASCII)
def test_a_non_ascii_dispatch_token_is_a_401(dispatch_app, header):
    response = dispatch_app.get("/probe", headers={"Authorization": header})

    assert response.status_code == 401


def test_the_right_dispatch_token_still_passes(dispatch_app):
    response = dispatch_app.get(
        "/probe", headers={"Authorization": "Bearer s3cret-dispatch-token-value"}
    )

    assert response.status_code == 200


@pytest.fixture
def admin_app(monkeypatch) -> TestClient:
    monkeypatch.setenv("ADMIN_TOKEN", "s3cret-admin-token-value")
    # Tried as a JWT first, which needs a signing secret to fail cleanly.
    monkeypatch.setenv("JWT_SECRET_KEY", "unit-test-jwt-secret-of-adequate-length")
    app = FastAPI()

    @app.get("/admin-probe")
    async def probe(request: Request, who: str = Depends(deps.verify_admin_access)):
        return {"who": who}

    app.dependency_overrides[deps.get_operational_store] = lambda: None
    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize("header", NON_ASCII)
def test_a_non_ascii_admin_token_is_a_401(admin_app, header):
    response = admin_app.get("/admin-probe", headers={"Authorization": header})

    assert response.status_code == 401


def test_the_admin_token_still_passes(admin_app):
    response = admin_app.get(
        "/admin-probe", headers={"Authorization": "Bearer s3cret-admin-token-value"}
    )

    assert response.status_code == 200


def test_a_non_ascii_pkce_challenge_does_not_match():
    assert verify_pkce(verifier="v" * 43, challenge="é" * 43) is False
