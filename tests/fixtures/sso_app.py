"""A console with single sign-on, for HTTP tests against the real store.

Fixtures: ``sso`` (the stored providers, changed by a test), ``idp`` (an
identity provider in this process), ``app`` (the gateway's auth, account and
admin routes), ``browser`` (one cookie jar) and ``store``. The helpers walk a
browser through a sign-in: ``start`` it, let the provider ``answer``.

Use with ``pytest_plugins = ["tests.servers.conftest_auth", "tests.fixtures.sso_app"]``.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from serving.auth import oidc
from serving.config import sso as sso_config
from serving.config.settings import get_settings
from serving.servers.middleware.exception_handler import install_exception_handlers
from serving.servers.routers import admin, auth_routes, sso_routes, user_routes
from serving.utils import login_rate_limit, sso_rate_limit
from serving.utils.jwt import generate_ulid
from serving.utils.password import hash_password
from tests.fixtures.oidc_provider import (
    CLIENT_ID,
    ISSUER,
    FakeOidcProvider,
    id_token_claims,
    sign_id_token,
)

PASSWORD = "Existing-password-42"
EMAIL = "alice@example.edu"
CALLBACK = "/auth/sso/example-univ/callback"


class Sso:
    """The stored providers, changed by a test and served to the code under test."""

    def __init__(self) -> None:
        self.providers: list[dict[str, Any]] = [self.provider()]

    @staticmethod
    def provider(**overrides: Any) -> dict[str, Any]:
        fields = {
            "id": "example-univ",
            "type": "oidc",
            "display_name": "Example University",
            "issuer": ISSUER,
            "client_id": CLIENT_ID,
            "client_secret": "${SSO_EXAMPLE_UNIV_CLIENT_SECRET}",
            "domains": ["example.edu"],
        }
        fields.update(overrides)
        return fields

    def configure(self, **overrides: Any) -> None:
        self.providers = [self.provider(**overrides)]

    def document(self) -> sso_config.SsoProvidersDocument:
        return sso_config.SsoProvidersDocument.model_validate({"providers": self.providers})


@pytest.fixture
def sso(monkeypatch, auth_env) -> Sso:
    configured = Sso()
    monkeypatch.setattr(sso_config, "get_sso_document", configured.document)
    monkeypatch.setattr(sso_config, "client_secret", lambda provider: "test-client-secret")
    # The callback address is built from the console's public address.
    monkeypatch.setattr(get_settings(), "frontend_url", "http://test")
    sso_rate_limit.reset_sso_rate_limit_state()
    # Password sign-ins in these tests share one address; start each test with a clean count.
    login_rate_limit.reset_login_rate_limit_state()
    return configured


@pytest.fixture
def idp(monkeypatch) -> FakeOidcProvider:
    provider = FakeOidcProvider()
    oidc.clear_caches()
    monkeypatch.setattr(oidc, "_transport", httpx.MockTransport(provider.handler))
    yield provider
    oidc.clear_caches()


@pytest_asyncio.fixture
async def app(auth_app_services, sso, idp, clean_auth_tables):
    application = FastAPI(title="Single Sign-On Test API")
    application.state.services = auth_app_services
    install_exception_handlers(application)
    application.include_router(auth_routes.router)
    application.include_router(sso_routes.router)
    application.include_router(user_routes.router)
    application.include_router(admin.router)
    return application


def new_browser(app: FastAPI) -> AsyncClient:
    """One browser: its own cookie jar, and it does not follow redirects."""
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest_asyncio.fixture
async def browser(app):
    async with new_browser(app) as client:
        yield client


@pytest.fixture
def store(auth_app_services):
    return auth_app_services.operational_store


async def existing_user(store, email: str = EMAIL, **fields: Any) -> str:
    user_id = generate_ulid()
    await store.create_user(
        user_id=user_id,
        email=email,
        password_hash=hash_password(PASSWORD),
        email_verified=fields.pop("email_verified", True),
        **fields,
    )
    return user_id


async def start(browser: AsyncClient, email: str = EMAIL, **body: Any) -> dict[str, list[str]]:
    """Start a sign-in from the sign-in page; return the provider's query parameters."""
    response = await browser.post("/auth/sso/discover", json={"email": email, **body})
    assert response.status_code == 200, response.text
    return parse_qs(urlsplit(response.json()["authorization_url"]).query)


async def answer(
    browser: AsyncClient, idp: FakeOidcProvider, params: dict[str, list[str]], **claims: Any
) -> httpx.Response:
    """Let the provider sign the user in and send the browser back."""
    idp.id_token = sign_id_token(id_token_claims(params["nonce"][0], **claims))
    return await browser.get(CALLBACK, params={"code": "code-1", "state": params["state"][0]})


async def sign_in(browser: AsyncClient, idp: FakeOidcProvider, **claims: Any) -> httpx.Response:
    return await answer(browser, idp, await start(browser), **claims)


def location(response: httpx.Response) -> tuple[str, dict[str, list[str]]]:
    assert response.status_code == 302, response.text
    parts = urlsplit(response.headers["location"])
    return parts.path, parse_qs(parts.query)
