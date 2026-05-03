"""Admin signup-domains endpoint tests.

Mock-based: exercises validation, error handling, and audit-log writes
without spinning up PostgreSQL. Database-level wiring is covered by the
integration tests in ``test_signup_flow.py`` (which run under ``dbtest``).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from serving.servers.deps import AppServices
from serving.servers.routers import admin as admin_router

AUTH = {"Authorization": "Bearer test-admin"}
_NOW = datetime(2025, 6, 15, tzinfo=timezone.utc)


@pytest.fixture
def mock_stores():
    """Operational store with the signup-domains methods mocked."""
    op_store = MagicMock()
    op_store.list_signup_allowed_domains = AsyncMock(return_value=[])
    op_store.add_signup_allowed_domain = AsyncMock()
    op_store.remove_signup_allowed_domain = AsyncMock(return_value=True)
    op_store.signup_allowlist_is_empty = AsyncMock(return_value=True)
    op_store.is_signup_domain_allowed = AsyncMock(return_value=False)
    op_store.get_user_by_email = AsyncMock(return_value=None)
    op_store.get_user_by_id = AsyncMock(return_value=None)
    op_store.log_admin_action = AsyncMock()

    log_store = MagicMock()
    return op_store, log_store


@pytest.fixture
async def admin_client(monkeypatch, mock_stores):
    op_store, log_store = mock_stores
    app = FastAPI(title="Signup Domains Test")

    services = AppServices(
        router=MagicMock(),
        db_logger=MagicMock(),
        operational_store=op_store,
        log_store=log_store,
        routing_manager=None,
    )
    app.state.services = services  # type: ignore[attr-defined]
    app.include_router(admin_router.router)

    transport = ASGITransport(app=app)
    client = AsyncClient(transport=transport, base_url="http://test")

    mock_log_action = AsyncMock()
    monkeypatch.setattr("serving.servers.routers.admin.signup_domains.log_admin_action", mock_log_action)
    # verify_admin_access tries JWT first; provide a JWT secret so the
    # decode call doesn't crash the dependency before the ADMIN_TOKEN
    # fallback runs.
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-32-chars-long!!")
    monkeypatch.setenv("ADMIN_TOKEN", "test-admin")
    monkeypatch.setenv("API_KEY_SECRET", "unit-test-secret")
    monkeypatch.setenv("SIGNUP_REQUIRE_EMAIL_VERIFICATION", "0")

    try:
        yield client, op_store, log_store, mock_log_action
    finally:
        await client.aclose()


def _row(domain: str, *, is_wildcard: bool = False) -> dict[str, Any]:
    return {
        "domain": domain,
        "is_wildcard": is_wildcard,
        "created_at": _NOW,
        "created_by": None,
        "created_by_email": None,
    }


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_requires_admin(admin_client):
    """Missing token → 401."""
    client, _op, _log, _audit = admin_client
    response = await client.get("/admin/signup-domains")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_post_requires_admin(admin_client):
    """Missing token → 401 on POST."""
    client, _op, _log, _audit = admin_client
    response = await client.post("/admin/signup-domains", json={"domain": "acme.com"})
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_delete_requires_admin(admin_client):
    """Missing token → 401 on DELETE."""
    client, _op, _log, _audit = admin_client
    response = await client.delete("/admin/signup-domains/acme.com")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_non_admin_jwt_forbidden(admin_client):
    """Non-admin user gets 403 (not 401)."""
    client, op_store, _log, _audit = admin_client
    # Wire a JWT path that resolves to a free-tier user.
    op_store.get_user_by_id = AsyncMock(
        return_value={
            "id": "u1",
            "email": "user@example.com",
            "status": "active",
            "email_verified": True,
            "role": "free",
        }
    )

    from serving.utils.jwt import create_access_token

    token, _jti = create_access_token("u1", "user@example.com")
    response = await client.get(
        "/admin/signup-domains",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# GET
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_returns_rows(admin_client):
    client, op_store, _log, _audit = admin_client
    op_store.list_signup_allowed_domains.return_value = [
        _row("acme.com"),
        _row("partner.io", is_wildcard=True),
    ]
    response = await client.get("/admin/signup-domains", headers=AUTH)
    assert response.status_code == 200
    data = response.json()
    assert {d["domain"] for d in data["domains"]} == {"acme.com", "partner.io"}
    by_domain = {d["domain"]: d for d in data["domains"]}
    assert by_domain["acme.com"]["is_wildcard"] is False
    assert by_domain["partner.io"]["is_wildcard"] is True


# ---------------------------------------------------------------------------
# POST validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_normalizes_and_persists(admin_client):
    """Admin POST normalizes case/whitespace before storing."""
    client, op_store, _log, audit = admin_client
    op_store.add_signup_allowed_domain.return_value = _row("acme.com")
    response = await client.post(
        "/admin/signup-domains",
        headers=AUTH,
        json={"domain": "  ACME.com  "},
    )
    assert response.status_code == 201
    op_store.add_signup_allowed_domain.assert_awaited_once()
    kwargs = op_store.add_signup_allowed_domain.await_args.kwargs
    assert kwargs["domain"] == "acme.com"
    assert kwargs["is_wildcard"] is False
    audit.assert_awaited()
    audit_args = audit.await_args
    assert audit_args.args[2] == "signup_domain.add"
    assert audit_args.args[4] == {"domain": "acme.com", "is_wildcard": False}


@pytest.mark.asyncio
async def test_post_wildcard_prefix_detected(admin_client):
    """Leading '*.' flips is_wildcard and is stripped from stored value."""
    client, op_store, _log, _audit = admin_client
    op_store.add_signup_allowed_domain.return_value = _row("partner.io", is_wildcard=True)
    response = await client.post(
        "/admin/signup-domains",
        headers=AUTH,
        json={"domain": "*.partner.io"},
    )
    assert response.status_code == 201
    kwargs = op_store.add_signup_allowed_domain.await_args.kwargs
    assert kwargs["domain"] == "partner.io"
    assert kwargs["is_wildcard"] is True


@pytest.mark.parametrize(
    "value",
    [
        "",
        "   ",
        "*.",
        "no-tld",
        "ac me.com",
        "acme@com",
        "*acme.com",  # internal star
        "acme.*",
        "x.123",  # numeric TLD
        "*..acme.com",
        # Leading/trailing hyphens per label are invalid per RFC 1035.
        "-foo.com",
        "foo-.com",
        "sub.-foo.com",
        "sub.foo-.com",
        "-foo-.com",
    ],
)
@pytest.mark.asyncio
async def test_post_rejects_malformed(admin_client, value):
    """Malformed entries return 400 without touching the store."""
    client, op_store, _log, _audit = admin_client
    response = await client.post(
        "/admin/signup-domains",
        headers=AUTH,
        json={"domain": value},
    )
    assert response.status_code in (400, 422), value
    op_store.add_signup_allowed_domain.assert_not_awaited()


@pytest.mark.parametrize(
    "value",
    [
        "a.com",  # single-char label
        "a-b.com",  # interior hyphen
        "1foo.com",  # numeric leading char
        "x1-y2.example.io",
    ],
)
@pytest.mark.asyncio
async def test_post_accepts_valid_label_shapes(admin_client, value):
    """Labels with interior hyphens and digits are accepted."""
    client, op_store, _log, _audit = admin_client
    op_store.add_signup_allowed_domain.return_value = _row(value)
    response = await client.post(
        "/admin/signup-domains",
        headers=AUTH,
        json={"domain": value},
    )
    assert response.status_code == 201, value


@pytest.mark.asyncio
async def test_post_returns_409_on_asyncpg_unique_violation(admin_client):
    """A typed asyncpg.UniqueViolationError is recognized as 409.

    Duplicate-key detection now relies *only* on the typed exception
    path; untyped exceptions are propagated as 500 so unrelated DB
    errors aren't silently masked as duplicates.
    """
    import asyncpg

    client, op_store, _log, _audit = admin_client
    op_store.add_signup_allowed_domain.side_effect = asyncpg.UniqueViolationError("any message")
    response = await client.post(
        "/admin/signup-domains",
        headers=AUTH,
        json={"domain": "acme.com"},
    )
    assert response.status_code == 409


@pytest.mark.asyncio
async def test_post_does_not_mask_unrelated_db_errors_as_409(admin_client):
    """Plain Exception("...constraint...") is NOT classified as a duplicate.

    Previously a string-matching fallback could turn unrelated DB errors
    (FK violations, syntax errors mentioning "constraint", etc.) into a
    spurious 409. The new policy lets them propagate as a 500 — the
    endpoint does not catch the untyped exception.
    """
    client, op_store, _log, _audit = admin_client
    op_store.add_signup_allowed_domain.side_effect = Exception(
        "duplicate key value violates unique constraint"
    )
    # ASGITransport re-raises app exceptions to the caller by default.
    # That re-raise is itself proof the endpoint did not classify the
    # error as 409 — if it had, we'd get a Response back instead.
    with pytest.raises(Exception, match="duplicate key value"):
        await client.post(
            "/admin/signup-domains",
            headers=AUTH,
            json={"domain": "acme.com"},
        )


# ---------------------------------------------------------------------------
# DELETE
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_exact_default_wildcard_false(admin_client):
    client, op_store, _log, audit = admin_client
    op_store.remove_signup_allowed_domain.return_value = True
    response = await client.delete("/admin/signup-domains/acme.com", headers=AUTH)
    assert response.status_code == 204
    kwargs = op_store.remove_signup_allowed_domain.await_args.kwargs
    assert kwargs == {"domain": "acme.com", "is_wildcard": False}
    audit.assert_awaited()
    audit_args = audit.await_args
    assert audit_args.args[2] == "signup_domain.remove"
    assert audit_args.args[4] == {"domain": "acme.com", "is_wildcard": False}


@pytest.mark.asyncio
async def test_delete_wildcard_query_param(admin_client):
    """wildcard=true picks the wildcard row for the same domain string."""
    client, op_store, _log, _audit = admin_client
    op_store.remove_signup_allowed_domain.return_value = True
    response = await client.delete(
        "/admin/signup-domains/partner.io?wildcard=true",
        headers=AUTH,
    )
    assert response.status_code == 204
    kwargs = op_store.remove_signup_allowed_domain.await_args.kwargs
    assert kwargs == {"domain": "partner.io", "is_wildcard": True}


@pytest.mark.asyncio
async def test_delete_returns_404_when_missing(admin_client):
    client, op_store, _log, _audit = admin_client
    op_store.remove_signup_allowed_domain.return_value = False
    response = await client.delete("/admin/signup-domains/nope.com", headers=AUTH)
    assert response.status_code == 404
