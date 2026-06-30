from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from serving.servers.routers.auth_routes import verify_email


class _UsedTokenStore:
    def __init__(self, *, email_verified: bool, expires_at: datetime | None = None) -> None:
        self.email_verified = email_verified
        self.expires_at = expires_at or datetime.now(timezone.utc) + timedelta(hours=24)
        self.marked_verified = False
        self.marked_token_used = False

    async def get_verification_token(self, token: str) -> dict[str, object]:
        return {
            "token": token,
            "user_id": "user-1",
            "created_at": datetime.now(timezone.utc) - timedelta(minutes=5),
            "expires_at": self.expires_at,
            "used_at": datetime.now(timezone.utc) - timedelta(minutes=1),
        }

    async def get_user_by_id(self, user_id: str) -> dict[str, object]:
        return {"id": user_id, "email_verified": self.email_verified}

    async def mark_user_email_verified(self, user_id: str) -> None:
        self.marked_verified = True
        self.email_verified = True

    async def mark_verification_used(self, token: str) -> None:
        self.marked_token_used = True


@pytest.mark.asyncio
async def test_verify_email_used_token_repairs_unverified_user() -> None:
    """A used token should not trap its user in an unverified login state."""
    store = _UsedTokenStore(email_verified=False)

    response = await verify_email("already-used-token", op_store=store)

    assert response.email_verified is True
    assert store.email_verified is True
    assert store.marked_verified is True
    assert store.marked_token_used is False


@pytest.mark.asyncio
async def test_verify_email_used_token_skips_repair_for_verified_user() -> None:
    """A used token for an already-verified user should remain read-only."""
    store = _UsedTokenStore(
        email_verified=True,
        expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )

    response = await verify_email("already-used-token", op_store=store)

    assert response.email_verified is True
    assert store.email_verified is True
    assert store.marked_verified is False
    assert store.marked_token_used is False


@pytest.mark.asyncio
async def test_verify_email_expired_used_token_does_not_repair_unverified_user() -> None:
    """An expired used token should not be replayable to repair verification."""
    store = _UsedTokenStore(
        email_verified=False,
        expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )

    with pytest.raises(HTTPException) as exc_info:
        await verify_email("expired-used-token", op_store=store)

    assert exc_info.value.status_code == 400
    assert "expired" in str(exc_info.value.detail).lower()
    assert store.email_verified is False
    assert store.marked_verified is False
    assert store.marked_token_used is False
