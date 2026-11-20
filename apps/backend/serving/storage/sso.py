"""Single sign-on state: linked identities, sign-in flows and pending links.

Nothing here knows a protocol. An identity is the pair an identity provider
vouches for, ``(issuer, subject)``; a flow is one redirect to a provider and
back, with whatever the protocol needs meanwhile carried as one JSON value.
Every "at most once" in the sign-in path is one conditional ``UPDATE`` here,
so it holds across gateway processes.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from serving.storage.organizations import (
    in_other_organization,
    join_organization,
    lock_for_sign_in,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime

#: What :meth:`PostgresSsoMixin.link_user_identity` answers.
LINKED = "linked"
IDENTITY_IN_USE = "identity_in_use"
ISSUER_CONFLICT = "issuer_conflict"
ACCOUNT_INACTIVE = "inactive"
OTHER_ORGANIZATION = "other_organization"

#: What :meth:`PostgresSsoMixin.unlink_user_identity` answers.
UNLINKED = "unlinked"
NOT_FOUND = "not_found"
LAST_SIGN_IN = "last_sign_in"

_IDENTITY_COLUMNS = "id, user_id, provider_id, issuer, subject, email, created_at, last_login_at"
_FLOW_COLUMNS = (
    "state_hash, binding_hash, provider_id, purpose, return_to, protocol_state, "
    "user_id, sid, reauth_purpose, created_at, expires_at"
)
_PENDING_COLUMNS = "token_hash, provider_id, issuer, subject, email, created_at, expires_at"


class _IdentityTaken(Exception):
    """The identity was linked by another transaction while a user was being created."""


def _flow(row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    flow = dict(row)
    state = flow.get("protocol_state")
    # asyncpg returns JSONB as text unless a codec is registered on the pool.
    flow["protocol_state"] = json.loads(state) if isinstance(state, str) else (state or {})
    return flow


class PostgresSsoMixin:
    """Persist linked identities and the short-lived state of a sign-in redirect."""

    _pool: Any

    # -- identities ----------------------------------------------------------

    async def get_user_identity(self, issuer: str, subject: str) -> dict | None:
        """Fetch the identity an identity provider vouches for, if it is linked."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT {_IDENTITY_COLUMNS} FROM user_identities "
                "WHERE issuer = $1 AND subject = $2",
                issuer,
                subject,
            )
        return dict(row) if row else None

    async def list_user_identities(self, user_id: str) -> list[dict]:
        """List an account's linked identities, oldest first."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT {_IDENTITY_COLUMNS} FROM user_identities "
                "WHERE user_id = $1 ORDER BY created_at, id",
                user_id,
            )
        return [dict(row) for row in rows]

    async def link_user_identity(
        self,
        *,
        identity_id: str,
        user_id: str,
        provider_id: str,
        issuer: str,
        subject: str,
        email: str | None,
    ) -> str:
        """Link an identity to an active account; say why not when it cannot be.

        Returns ``"linked"`` (also when this account already holds this very
        identity), ``"identity_in_use"`` when another account holds it,
        ``"issuer_conflict"`` when this account already holds a different
        identity from the same issuer, ``"inactive"`` when the account is
        missing or not active, and ``"other_organization"`` when the provider
        belongs to an organization and the account to another one. The account
        row is locked first, so a link is never written to an account a
        concurrent suspension or deletion is closing.

        A new link through a provider of an organization makes the account a
        member of it, in the same transaction: the caller links only an
        account whose email the provider answers for.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            await lock_for_sign_in(conn)
            status = await conn.fetchval(
                "SELECT status FROM users WHERE id = $1 FOR UPDATE", user_id
            )
            if status != "active":
                return ACCOUNT_INACTIVE
            if await in_other_organization(conn, user_id=user_id, provider_id=provider_id):
                return OTHER_ORGANIZATION
            row = await conn.fetchrow(
                "INSERT INTO user_identities (id, user_id, provider_id, issuer, subject, email) "
                "VALUES ($1, $2, $3, $4, $5, $6) ON CONFLICT DO NOTHING RETURNING id",
                identity_id,
                user_id,
                provider_id,
                issuer,
                subject,
                email.lower() if email else None,
            )
            if row is not None:
                await join_organization(conn, user_id=user_id, provider_id=provider_id)
                return LINKED
            owner = await conn.fetchval(
                "SELECT user_id FROM user_identities WHERE issuer = $1 AND subject = $2",
                issuer,
                subject,
            )
        if owner == user_id:
            return LINKED
        return IDENTITY_IN_USE if owner is not None else ISSUER_CONFLICT

    async def touch_user_identity(self, identity_id: str) -> None:
        """Record that an identity just signed in."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE user_identities SET last_login_at = NOW() WHERE id = $1", identity_id
            )

    async def unlink_user_identity(
        self, *, identity_id: str, user_id: str, keep_last_sign_in: bool
    ) -> str:
        """Remove one of an account's identities.

        Returns ``"unlinked"``, ``"not_found"`` when the account holds no such
        identity, or -- with *keep_last_sign_in* -- ``"last_sign_in"`` when the
        account has no password, no verified phone and no other identity, so
        removing this one would leave its owner no way in. Decided under a lock
        on the account, so two removals cannot each see the other's identity as
        the way in that remains.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            user = await conn.fetchrow(
                "SELECT password_hash, phone, phone_verified FROM users WHERE id = $1 FOR UPDATE",
                user_id,
            )
            if user is None:
                return NOT_FOUND
            held = await conn.fetchval(
                "SELECT 1 FROM user_identities WHERE id = $1 AND user_id = $2",
                identity_id,
                user_id,
            )
            if held is None:
                return NOT_FOUND
            if keep_last_sign_in:
                others = await conn.fetchval(
                    "SELECT COUNT(*) FROM user_identities WHERE user_id = $1 AND id <> $2",
                    user_id,
                    identity_id,
                )
                has_password = bool(user["password_hash"])
                has_phone = bool(user["phone"] and user["phone_verified"])
                if not (has_password or has_phone or others):
                    return LAST_SIGN_IN
            await conn.execute(
                "DELETE FROM user_identities WHERE id = $1 AND user_id = $2",
                identity_id,
                user_id,
            )
        return UNLINKED

    async def create_sso_user(
        self,
        *,
        user_id: str,
        email: str,
        user_name: str | None,
        role: str,
        status: str,
        identity_id: str,
        provider_id: str,
        issuer: str,
        subject: str,
    ) -> bool:
        """Create an account for a first single sign-on and link the identity to it.

        One transaction: an account without its identity could never sign in
        again, and an identity without its account would point nowhere. The
        account has no password and its email counts as verified, because the
        identity provider vouched for it. Returns False, having written
        nothing, when the email or the identity was taken meanwhile.

        When the provider belongs to an organization, the account is created
        as its member.
        """
        from serving.storage.project_backfill import ensure_default_project

        try:
            async with self._pool.acquire() as conn, conn.transaction():
                await lock_for_sign_in(conn)
                row = await conn.fetchrow(
                    "INSERT INTO users (id, email, password_hash, user_name, "
                    "email_verified, status, role) VALUES ($1, $2, NULL, $3, TRUE, $4, $5) "
                    "ON CONFLICT DO NOTHING RETURNING id",
                    user_id,
                    email.lower(),
                    user_name,
                    status,
                    role,
                )
                if row is None:
                    return False
                await ensure_default_project(conn, user_id=user_id)
                linked = await conn.fetchrow(
                    "INSERT INTO user_identities "
                    "(id, user_id, provider_id, issuer, subject, email) "
                    "VALUES ($1, $2, $3, $4, $5, $6) ON CONFLICT DO NOTHING RETURNING id",
                    identity_id,
                    user_id,
                    provider_id,
                    issuer,
                    subject,
                    email.lower(),
                )
                if linked is None:
                    raise _IdentityTaken
                await join_organization(conn, user_id=user_id, provider_id=provider_id)
        except _IdentityTaken:
            return False
        return True

    # -- sign-in flows -------------------------------------------------------

    async def create_sso_flow(
        self,
        *,
        state_hash: str,
        binding_hash: str,
        provider_id: str,
        purpose: str,
        return_to: str,
        protocol_state: dict[str, Any],
        expires_at: datetime,
        user_id: str | None = None,
        sid: str | None = None,
        reauth_purpose: str | None = None,
    ) -> None:
        """Record one redirect to an identity provider.

        Only hashes of the ``state`` value and of the browser's binding cookie
        are kept. A flow lives ten minutes and is written once per sign-in, so
        the table is swept here rather than by a scheduled job to own.
        """
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO sso_flows (state_hash, binding_hash, provider_id, purpose, "
                "return_to, protocol_state, expires_at, user_id, sid, reauth_purpose) "
                "VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8, $9, $10)",
                state_hash,
                binding_hash,
                provider_id,
                purpose,
                return_to,
                json.dumps(protocol_state),
                expires_at,
                user_id,
                sid,
                reauth_purpose,
            )
            await conn.execute("DELETE FROM sso_flows WHERE expires_at < NOW() - INTERVAL '1 day'")

    async def consume_sso_flow(
        self, state_hash: str, *, provider_id: str, binding_hashes: Sequence[str]
    ) -> dict | None:
        """Atomically claim an unused, unexpired flow; None when there is none to claim.

        Only for the provider the flow was started at, and only for a browser
        that holds the flow's cookie: *binding_hashes* are the hashes of the
        flow cookies the request carried. Any other request claims nothing,
        so the flow stays good for the browser that started it.
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "UPDATE sso_flows SET consumed_at = NOW() "
                "WHERE state_hash = $1 AND provider_id = $2 "
                "AND binding_hash = ANY($3::text[]) "
                "AND consumed_at IS NULL AND expires_at > NOW() "
                f"RETURNING {_FLOW_COLUMNS}",
                state_hash,
                provider_id,
                list(binding_hashes),
            )
        return _flow(row)

    async def mark_sso_reauth_verified(self, state_hash: str) -> None:
        """Record that a claimed re-authentication flow ended in a fresh sign-in."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE sso_flows SET reauth_verified_at = NOW() "
                "WHERE state_hash = $1 AND purpose = 'reauth' AND consumed_at IS NOT NULL",
                state_hash,
            )

    async def consume_sso_reauth_proof(
        self, *, user_id: str, sid: str, purpose: str, max_age_seconds: int
    ) -> bool:
        """Spend a recent re-authentication for exactly this account, session and purpose.

        True at most once per re-authentication: the newest unspent one inside
        *max_age_seconds* is marked spent in the statement that finds it.
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "UPDATE sso_flows SET reauth_used_at = NOW() WHERE state_hash = ("
                "SELECT state_hash FROM sso_flows "
                "WHERE purpose = 'reauth' AND user_id = $1 AND sid = $2 AND reauth_purpose = $3 "
                "AND reauth_used_at IS NULL AND reauth_verified_at IS NOT NULL "
                "AND reauth_verified_at > NOW() - make_interval(secs => $4) "
                "ORDER BY reauth_verified_at DESC LIMIT 1 FOR UPDATE SKIP LOCKED"
                ") RETURNING state_hash",
                user_id,
                sid,
                purpose,
                float(max_age_seconds),
            )
        return row is not None

    # -- pending links -------------------------------------------------------

    async def create_sso_pending_link(
        self,
        *,
        token_hash: str,
        provider_id: str,
        issuer: str,
        subject: str,
        email: str,
        expires_at: datetime,
    ) -> None:
        """Hold an identity until the owner of the account it matched accepts it."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO sso_pending_links "
                "(token_hash, provider_id, issuer, subject, email, expires_at) "
                "VALUES ($1, $2, $3, $4, $5, $6)",
                token_hash,
                provider_id,
                issuer,
                subject,
                email.lower(),
                expires_at,
            )
            await conn.execute(
                "DELETE FROM sso_pending_links WHERE expires_at < NOW() - INTERVAL '1 day'"
            )

    async def get_sso_pending_link(self, token_hash: str) -> dict | None:
        """Read a pending link that is still waiting, without claiming it."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT {_PENDING_COLUMNS} FROM sso_pending_links "
                "WHERE token_hash = $1 AND consumed_at IS NULL AND expires_at > NOW()",
                token_hash,
            )
        return dict(row) if row else None

    async def consume_sso_pending_link(self, token_hash: str) -> dict | None:
        """Atomically claim a pending link; None when it expired or was already claimed."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "UPDATE sso_pending_links SET consumed_at = NOW() "
                "WHERE token_hash = $1 AND consumed_at IS NULL AND expires_at > NOW() "
                f"RETURNING {_PENDING_COLUMNS}",
                token_hash,
            )
        return dict(row) if row else None

    async def delete_sso_pending_link(self, token_hash: str) -> None:
        """Drop a pending link its browser declined."""
        async with self._pool.acquire() as conn:
            await conn.execute("DELETE FROM sso_pending_links WHERE token_hash = $1", token_hash)
