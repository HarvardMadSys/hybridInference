"""Organizations: the member rule, and what an organization is shown as.

An organization is a record of its own (:mod:`serving.storage.organizations`)
that owns ``SSO_PROVIDERS`` entries, its SSO connections. An account is a
member when its email is in the ``domains`` of one of those connections and it
has an identity of that connection. The store writes the membership with the
event that makes the rule true (a first sign-in, a link, an attach); this
module holds the half of the rule the store cannot know, the provider
document, and tells the audit log when an account joined.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from serving.audit import AuditActor, AuditTarget
from serving.config import sso as sso_config
from serving.utils.logging import get_logger

if TYPE_CHECKING:
    from serving.config.sso import SsoProvider

logger = get_logger(__name__)


def configured_provider(provider_id: str) -> SsoProvider | None:
    """The ``SSO_PROVIDERS`` entry with this id, switched on or off.

    An organization keeps a connection whose provider is switched off, so this
    looks past ``enabled``, unlike :func:`serving.config.sso.provider_by_id`.
    """
    return next(
        (p for p in sso_config.get_sso_document().providers if p.id == provider_id), None
    )


def may_link(provider: SsoProvider, account_email: str | None) -> bool:
    """Whether an account with this email may link an identity of *provider*.

    Only an account the provider answers for. An account outside its domains
    would gain nothing from the organization, and a sign-in through the
    organization would then open a personal account.
    """
    return provider.answers_for(account_email)


async def accounts_of_connection(op_store: Any, provider: SsoProvider) -> list[str]:
    """The accounts attaching *provider* to an organization makes members.

    Every account that is in no organization, has an identity that signs in
    through *provider*, and has an email *provider* answers for.
    """
    accounts: list[str] = []
    for row in await op_store.list_identities_without_organization():
        served = sso_config.serves_identity(
            provider, row["provider_id"], row["issuer"], row.get("email")
        )
        if served and provider.answers_for(row.get("account_email")):
            accounts.append(row["user_id"])
    # An account holds one identity per issuer, so no account is listed twice.
    return accounts


async def membership_of(op_store: Any, user_id: str) -> dict[str, Any] | None:
    """An account's membership; None in a store that keeps no organizations."""
    try:
        row = await op_store.get_organization_membership(user_id)
    except RuntimeError:
        return None
    return dict(row) if row else None


async def log_join(
    db_logger: Any,
    op_store: Any,
    *,
    user_id: str,
    before: dict[str, Any] | None,
    provider: SsoProvider,
    source_ip: str | None,
) -> None:
    """Record that *user_id* joined an organization, when a sign-in or a link made it one.

    *before* is the account's membership before the event (None for an account
    the event created). Nothing is written when the account was a member
    already, or the provider belongs to no organization.
    """
    from serving.servers.auth import log_admin_action

    if before is not None:
        return
    membership = await membership_of(op_store, user_id)
    if membership is None:
        return
    await log_admin_action(
        db_logger,
        action="organization.member.join",
        details={"sso_provider": provider.id},
        actor=AuditActor.user(user_id, source_ip),
        target=AuditTarget.user(user_id),
        organization_id=membership["organization_id"],
    )
    logger.info(
        f"Account {user_id} joined organization {membership['organization_id']} "
        f"through {provider.id}"
    )


def connection_view(provider_id: str) -> dict[str, Any]:
    """An SSO connection as an administrator is shown it: no secret, no client id.

    ``available`` is False when ``SSO_PROVIDERS`` has no entry with this id any
    more: the id was changed or the entry removed, and the connection signs
    nobody in until the platform administrator attaches the new id.
    """
    provider = configured_provider(provider_id)
    if provider is None:
        return {"provider_id": provider_id, "available": False}
    return {
        "provider_id": provider.id,
        "available": True,
        "display_name": provider.display_name,
        "type": provider.type,
        "domains": list(provider.domains),
        "enabled": provider.enabled,
        "auto_create": provider.auto_create,
        "require_approval": provider.require_approval,
    }
