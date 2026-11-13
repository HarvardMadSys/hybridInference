"""The small decisions of the single sign-on routes that need no store."""

from __future__ import annotations

import pytest

from serving.config.settings import get_settings
from serving.config.sso import SsoProvider
from serving.servers.routers import sso_routes


def _provider() -> SsoProvider:
    return SsoProvider.model_validate(
        {
            "id": "example-univ",
            "type": "oidc",
            "display_name": "Example University",
            "issuer": "https://idp.example.edu",
            "client_id": "hybridinference",
            "client_secret": "${SSO_SECRET}",
            "domains": ["example.edu"],
        }
    )


@pytest.mark.parametrize(
    "path",
    ["/dashboard", "/dashboard/keys?tab=1", "/dashboard/settings#sso", "/a/b/c", "/"],
)
def test_a_path_inside_the_console_is_kept(path):
    assert sso_routes.internal_path(path) == path


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "dashboard",
        "https://evil.test/",
        "//evil.test/path",
        "/\\evil.test",
        "/\\/evil.test",
        "/dash board",
        "/\tevil",
        "/\n/evil.test",
        "javascript:alert(1)",
    ],
)
def test_anything_that_could_leave_the_console_falls_back(raw):
    assert sso_routes.internal_path(raw) == "/dashboard"
    assert sso_routes.internal_path(raw, "/login") == "/login"


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/login", "/login?sso_error=email_untrusted"),
        (
            "/dashboard/settings?tab=account",
            "/dashboard/settings?tab=account&sso_error=email_untrusted",
        ),
        ("/dashboard/settings#sso", "/dashboard/settings?sso_error=email_untrusted#sso"),
    ],
)
def test_a_parameter_is_added_whatever_the_path_already_carries(path, expected):
    assert sso_routes._with_param(path, "sso_error", "email_untrusted") == expected


def test_a_parameter_value_is_encoded():
    assert sso_routes._with_param("/login", "next", "/dashboard/keys?a=1&b=2") == (
        "/login?next=%2Fdashboard%2Fkeys%3Fa%3D1%26b%3D2"
    )


def test_the_callback_address_comes_from_the_consoles_public_address(monkeypatch):
    monkeypatch.setattr(get_settings(), "frontend_url", "https://console.example.org/")

    assert sso_routes._redirect_uri(_provider()) == (
        "https://console.example.org/auth/sso/example-univ/callback"
    )


@pytest.mark.parametrize("frontend_url", ["", "console.example.org", "/relative", "ftp://x.test"])
def test_without_an_absolute_public_address_there_is_no_callback_address(monkeypatch, frontend_url):
    monkeypatch.setattr(get_settings(), "frontend_url", frontend_url)

    assert sso_routes._redirect_uri(_provider()) is None


def test_the_cookies_are_named_after_the_deployments_session_cookie(monkeypatch):
    monkeypatch.setattr(get_settings(), "refresh_token_cookie_name", "campus_session")

    assert sso_routes._flow_cookie() == "campus_session_sso_flow"
    assert sso_routes._link_cookie() == "campus_session_sso_link"
