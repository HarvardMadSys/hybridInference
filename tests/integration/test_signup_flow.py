"""Integration tests for the signup flow with domain allowlist.

Covers the four spec scenarios plus disposable-domain precedence:

- Empty allowlist → ``active``.
- Allowlisted exact domain → ``active``.
- Allowlisted wildcard match → ``active``.
- Non-listed domain → ``pending_approval`` and admin notify email sent.
- Disposable-domain blocklist precedes allowlist.
"""

from unittest.mock import patch

import pytest
from httpx import AsyncClient

from serving.auth.signup_policy import invalidate_allowlist_cache
from tests.fixtures.auth_factories import create_signup_request

pytest_plugins = ["tests.servers.conftest_auth"]
pytestmark = pytest.mark.dbtest


@pytest.fixture(autouse=True)
def _reset_allowlist_cache():
    invalidate_allowlist_cache()
    yield
    invalidate_allowlist_cache()


async def _add_domain(op_store, domain: str, *, is_wildcard: bool = False) -> None:
    """Insert an allowlist row directly via the store."""
    await op_store.add_signup_allowed_domain(
        domain=domain,
        is_wildcard=is_wildcard,
        created_by=None,
    )


async def _user_status(op_store, email: str) -> str:
    user = await op_store.get_user_by_email(email)
    assert user is not None, f"user {email} not created"
    return user["status"]


class TestSignupAllowlist:
    """Validate that signup status reflects the allowlist policy."""

    @pytest.mark.asyncio
    async def test_empty_allowlist_auto_approves(
        self,
        auth_app_client: AsyncClient,
        auth_app_services,
        clean_auth_tables,
        mock_email_service,
    ):
        # Use a non-blocklisted domain (RFC-2606 reserves .example, .test,
        # .invalid, .localhost). anything-domain.io is safe.
        signup_data = create_signup_request(email="alice@anything-domain.io")
        response = await auth_app_client.post("/auth/signup", json=signup_data)
        assert response.status_code == 201
        op_store = auth_app_services.operational_store
        assert await _user_status(op_store, signup_data["email"]) == "active"

    @pytest.mark.asyncio
    async def test_exact_match_auto_approves(
        self,
        auth_app_client: AsyncClient,
        auth_app_services,
        clean_auth_tables,
        mock_email_service,
    ):
        op_store = auth_app_services.operational_store
        await _add_domain(op_store, "trusted-corp.io", is_wildcard=False)
        invalidate_allowlist_cache()

        signup_data = create_signup_request(email="bob@trusted-corp.io")
        response = await auth_app_client.post("/auth/signup", json=signup_data)
        assert response.status_code == 201
        assert await _user_status(op_store, "bob@trusted-corp.io") == "active"

    @pytest.mark.asyncio
    async def test_wildcard_match_auto_approves(
        self,
        auth_app_client: AsyncClient,
        auth_app_services,
        clean_auth_tables,
        mock_email_service,
    ):
        op_store = auth_app_services.operational_store
        await _add_domain(op_store, "trusted-corp.io", is_wildcard=True)
        invalidate_allowlist_cache()

        signup_data = create_signup_request(email="carol@team.trusted-corp.io")
        response = await auth_app_client.post("/auth/signup", json=signup_data)
        assert response.status_code == 201
        assert await _user_status(op_store, "carol@team.trusted-corp.io") == "active"

    @pytest.mark.asyncio
    async def test_non_listed_domain_pending_and_emails_admin(
        self,
        auth_app_client: AsyncClient,
        auth_app_services,
        clean_auth_tables,
        monkeypatch,
    ):
        op_store = auth_app_services.operational_store
        await _add_domain(op_store, "trusted-corp.io", is_wildcard=False)
        invalidate_allowlist_cache()

        # Force admin notify path. Pydantic settings cache env at import
        # time, so patching env vars here has no effect; patch the
        # resolved values at the auth_routes import sites instead.
        from serving.servers.routers import auth_routes as auth_routes_mod

        monkeypatch.setattr(auth_routes_mod.settings, "admin_emails", "admin@trusted-corp.io")

        sent_to: list[str] = []

        def _capture(to_email, user_email, user_name, user_id):
            sent_to.append(to_email)
            return True

        # Patch the import site used by auth_routes (function reference,
        # not the source module) and force is_email_enabled() to True.
        with (
            patch(
                "serving.servers.routers.auth_routes.send_new_registration_admin_email",
                side_effect=_capture,
            ),
            patch("serving.servers.routers.auth_routes.is_email_enabled", return_value=True),
        ):
            signup_data = create_signup_request(email="dan@outside-vendor.net")
            response = await auth_app_client.post("/auth/signup", json=signup_data)
            assert response.status_code == 201

        assert await _user_status(op_store, "dan@outside-vendor.net") == "pending_approval"
        # BackgroundTasks runs synchronously when no real ASGI worker is in
        # play (httpx test client awaits them after response). So sent_to
        # should contain the admin email.
        assert sent_to == ["admin@trusted-corp.io"]

    @pytest.mark.asyncio
    async def test_admin_notify_disabled_suppresses_email(
        self,
        auth_app_client: AsyncClient,
        auth_app_services,
        clean_auth_tables,
        monkeypatch,
    ):
        """signup_admin_notify_enabled=false stops the new-registration email."""
        op_store = auth_app_services.operational_store
        await _add_domain(op_store, "trusted-corp.io", is_wildcard=False)
        invalidate_allowlist_cache()

        from serving.servers.routers import auth_routes as auth_routes_mod

        monkeypatch.setattr(auth_routes_mod.settings, "admin_emails", "admin@trusted-corp.io")

        # Disable the admin-notify toggle via the Settings fallback. The runtime
        # settings singleton is reset to None between tests (tests/conftest.py)
        # and auth_app_services never initializes it, so signup() falls back to
        # settings.signup_admin_notify_enabled. monkeypatch keeps the override
        # scoped to this test (auto-restored on teardown) and — unlike writing a
        # site_settings row — cannot leak into later signup-flow tests.
        monkeypatch.setattr(auth_routes_mod.settings, "signup_admin_notify_enabled", False)

        sent_to: list[str] = []

        def _capture(to_email, user_email, user_name, user_id, use_case=None):
            sent_to.append(to_email)
            return True

        with (
            patch(
                "serving.servers.routers.auth_routes.send_new_registration_admin_email",
                side_effect=_capture,
            ),
            patch("serving.servers.routers.auth_routes.is_email_enabled", return_value=True),
        ):
            signup_data = create_signup_request(email="dan@outside-vendor.net")
            response = await auth_app_client.post("/auth/signup", json=signup_data)
            assert response.status_code == 201

        # Still pending approval, but no admin notification email was sent.
        assert await _user_status(op_store, "dan@outside-vendor.net") == "pending_approval"
        assert sent_to == []

    @pytest.mark.asyncio
    async def test_pending_user_persists_signup_reason(
        self,
        auth_app_client: AsyncClient,
        auth_app_services,
        clean_auth_tables,
        mock_email_service,
    ):
        """The submitted use case is stored so admins can review pending users."""
        op_store = auth_app_services.operational_store
        await _add_domain(op_store, "trusted-corp.io", is_wildcard=False)
        invalidate_allowlist_cache()

        reason = "Research project on retrieval-augmented generation."
        signup_data = create_signup_request(email="erin@outside-vendor.net", use_case=reason)
        response = await auth_app_client.post("/auth/signup", json=signup_data)
        assert response.status_code == 201

        user = await op_store.get_user_by_email("erin@outside-vendor.net")
        assert user is not None
        assert user["status"] == "pending_approval"
        assert user["signup_reason"] == reason

    @pytest.mark.asyncio
    async def test_signup_notify_emails_overrides_admin_emails(
        self,
        auth_app_client: AsyncClient,
        auth_app_services,
        clean_auth_tables,
        monkeypatch,
    ):
        op_store = auth_app_services.operational_store
        await _add_domain(op_store, "trusted-corp.io", is_wildcard=False)
        invalidate_allowlist_cache()

        # admin_emails still controls who is an admin, but the notification
        # recipient list is narrowed via signup_notify_emails.
        from serving.servers.routers import auth_routes as auth_routes_mod

        monkeypatch.setattr(
            auth_routes_mod.settings,
            "admin_emails",
            "murphy@trusted-corp.io,haoran@trusted-corp.io,peter@trusted-corp.io",
        )
        monkeypatch.setattr(
            auth_routes_mod.settings, "signup_notify_emails", "peter@trusted-corp.io"
        )

        sent_to: list[str] = []

        def _capture(to_email, user_email, user_name, user_id, use_case=None):
            sent_to.append(to_email)
            return True

        with (
            patch(
                "serving.servers.routers.auth_routes.send_new_registration_admin_email",
                side_effect=_capture,
            ),
            patch("serving.servers.routers.auth_routes.is_email_enabled", return_value=True),
        ):
            signup_data = create_signup_request(email="dan@outside-vendor.net")
            response = await auth_app_client.post("/auth/signup", json=signup_data)
            assert response.status_code == 201

        # Only the notify recipient is emailed, not all three admins.
        assert sent_to == ["peter@trusted-corp.io"]

    @pytest.mark.asyncio
    async def test_disposable_blocklist_precedes_allowlist(
        self,
        auth_app_client: AsyncClient,
        auth_app_services,
        clean_auth_tables,
        mock_email_service,
    ):
        op_store = auth_app_services.operational_store
        # Even if an admin foot-guns example.com onto the allowlist, the
        # blocklist runs first and rejects with 400.
        await _add_domain(op_store, "example.com", is_wildcard=False)
        invalidate_allowlist_cache()

        signup_data = create_signup_request(email="eve@example.com")
        response = await auth_app_client.post("/auth/signup", json=signup_data)
        assert response.status_code == 400
        # User must not have been created.
        user = await op_store.get_user_by_email("eve@example.com")
        assert user is None
