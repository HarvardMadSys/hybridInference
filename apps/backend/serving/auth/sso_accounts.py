"""Which account an identity from an identity provider signs in to.

Works from a protocol-neutral :class:`~serving.auth.oidc.ExternalIdentity` and
the provider's shared settings; nothing here knows how the identity was
obtained. The decisions, in order:

1. An identity that is already linked signs in to its account.
2. Otherwise the provider's email must be trusted: verified, and in a domain
   this provider answers for. A provider vouches only for its own domains.
3. An account with that email, if not active, refuses the sign-in.
4. An active one is linked without asking only when every condition of
   automatic linking holds; otherwise the identity waits for the account's
   owner to sign in and accept it (a pending link).
5. With no such account, one is created when the provider allows it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from serving.auth.browser_sessions import email_verification_required
from serving.config import sso as sso_config
from serving.config.settings import is_admin_email
from serving.schemas_auth import SsoIdentity, SsoProviderRef
from serving.setup_state import refresh_setup_state
from serving.storage import sso as sso_store
from serving.utils.email_blocklist import is_email_domain_blocked
from serving.utils.jwt import generate_ulid
from serving.utils.logging import get_logger

if TYPE_CHECKING:
    from serving.auth.oidc import ExternalIdentity
    from serving.config.sso import SsoProvider

logger = get_logger(__name__)

#: Why a sign-in was refused, as the sign-in page is told.
RefusalCode = Literal[
    "email_untrusted",
    "identity_conflict",
    "identity_in_use",
    "no_account",
    "pending",
    "suspended",
    "inactive",
    "unavailable",
    "failed",
]


@dataclass(frozen=True)
class Resolution:
    """What a completed sign-in at an identity provider leads to.

    Exactly one of three: ``user`` to sign in (with ``identity_id``, the
    identity that did), ``pending_link`` when the identity must wait for an
    existing account's owner, or ``refused`` with the reason. ``created`` marks
    an account this sign-in created, also when it then waits for approval.
    """

    user: dict[str, Any] | None = None
    identity_id: str | None = None
    pending_link: bool = False
    refused: RefusalCode | None = None
    created: dict[str, Any] | None = None


def status_refusal(status: str | None) -> RefusalCode | None:
    """The refusal an account's status calls for; None when it may sign in."""
    if status == "active":
        return None
    if status == "pending_approval":
        return "pending"
    if status == "suspended":
        return "suspended"
    return "inactive"


def email_trusted(provider: SsoProvider, identity: ExternalIdentity) -> bool:
    """Whether the provider's email for this identity may be acted on.

    It must be in a domain the provider answers for, and verified -- by the
    provider's own word, or by the deployment's when the provider never says.
    Trusting ``email_verified`` from any provider for any domain is what lets
    an identity at one organization claim an account that belongs to another.
    """
    if not identity.email or not provider.answers_for(identity.email):
        return False
    return identity.email_verified is True or provider.assume_email_verified


def is_administrator(user: dict[str, Any]) -> bool:
    """Whether an account is an administrator, or will become one on its next token."""
    return user.get("role") == "admin" or is_admin_email(user.get("email"))


async def may_auto_link(provider: SsoProvider, user: dict[str, Any]) -> bool:
    """Whether an identity may be linked to *user* without its owner accepting.

    The account's ``email_verified`` flag alone is not proof: a deployment
    that does not require verification marks every new account verified. So
    the deployment must require verification now, and an administrator is
    never linked without accepting.
    """
    return bool(
        provider.auto_link
        and user.get("email_verified")
        and not is_administrator(user)
        and await email_verification_required()
    )


def identity_summary(row: dict[str, Any]) -> SsoIdentity:
    """Describe a linked identity, naming its provider as it is configured now."""
    provider = sso_config.provider_for_identity(row["provider_id"], row["issuer"])
    return SsoIdentity(
        id=row["id"],
        provider_id=provider.id if provider else row["provider_id"],
        provider_name=provider.display_name if provider else row["provider_id"],
        email=row.get("email"),
        linked_at=row["created_at"],
        last_login_at=row.get("last_login_at"),
        available=provider is not None,
    )


async def identity_summaries(op_store: Any, user_id: str) -> list[SsoIdentity]:
    """Describe an account's linked identities; none from a store that keeps none."""
    try:
        rows = list(await op_store.list_user_identities(user_id))
    except RuntimeError:
        return []
    return [identity_summary(row) for row in rows]


def provider_ref(provider: SsoProvider | None) -> SsoProviderRef | None:
    """A provider as an account's own pages may show it."""
    return SsoProviderRef(id=provider.id, name=provider.display_name) if provider else None


def confirms_through_provider(user: dict[str, Any]) -> bool:
    """Whether an account has neither a password nor a verified phone to confirm with."""
    return not user.get("password_hash") and not (user.get("phone") and user.get("phone_verified"))


async def account_provider(op_store: Any, user_id: str) -> SsoProvider | None:
    """The offered provider an account most recently signed in through, if any."""
    try:
        identities = list(await op_store.list_user_identities(user_id))
    except RuntimeError:
        # A store that does not keep identities has none to offer.
        return None
    identities.sort(key=lambda row: row.get("last_login_at") or row["created_at"], reverse=True)
    for identity in identities:
        provider = sso_config.provider_for_identity(identity["provider_id"], identity["issuer"])
        if provider is not None:
            return provider
    return None


async def resolve_sign_in(
    op_store: Any, provider: SsoProvider, identity: ExternalIdentity
) -> Resolution:
    """Decide what a completed sign-in at *provider* leads to.

    Writes at most one of: a link to an existing account, or a new account
    with its identity. A pending link is the caller's to record, because it is
    bound to the caller's browser.
    """
    resolution = await _resolve(op_store, provider, identity)
    if resolution is None:
        # The email or the identity was taken between the lookups and the
        # write; a second pass sees whoever took it.
        resolution = await _resolve(op_store, provider, identity)
    return resolution or Resolution(refused="failed")


async def _resolve(
    op_store: Any, provider: SsoProvider, identity: ExternalIdentity
) -> Resolution | None:
    linked = await op_store.get_user_identity(identity.issuer, identity.subject)
    if linked is not None:
        user = await op_store.get_user_by_id(linked["user_id"])
        if user is None:
            return Resolution(refused="failed")
        refusal = status_refusal(user.get("status"))
        if refusal is not None:
            return Resolution(refused=refusal)
        return Resolution(user=user, identity_id=linked["id"])

    if not email_trusted(provider, identity):
        return Resolution(refused="email_untrusted")
    email = identity.email or ""

    user = await op_store.get_user_by_email(email)
    if user is not None:
        refusal = status_refusal(user.get("status"))
        if refusal is not None:
            # Also an account that was deleted: its email stays taken, as it
            # does for signup.
            return Resolution(refused=refusal)
        held = await op_store.list_user_identities(user["id"])
        if any(row["issuer"] == identity.issuer for row in held):
            # The provider recreated the user, or gave the address to someone
            # else. The gateway cannot tell which, so it replaces nothing.
            return Resolution(refused="identity_conflict")
        if not await may_auto_link(provider, user):
            return Resolution(pending_link=True)
        identity_id = generate_ulid()
        outcome = await op_store.link_user_identity(
            identity_id=identity_id,
            user_id=user["id"],
            provider_id=provider.id,
            issuer=identity.issuer,
            subject=identity.subject,
            email=email,
        )
        if outcome == sso_store.LINKED:
            linked = await op_store.get_user_identity(identity.issuer, identity.subject)
            return Resolution(user=user, identity_id=linked["id"] if linked else identity_id)
        if outcome == sso_store.ISSUER_CONFLICT:
            return Resolution(refused="identity_conflict")
        if outcome == sso_store.ACCOUNT_INACTIVE:
            return Resolution(refused="inactive")
        return None

    if not provider.auto_create:
        return Resolution(refused="no_account")
    if is_admin_email(email) or is_email_domain_blocked(email):
        # An ADMIN_EMAILS address becomes an administrator on its next token:
        # such an account is made the existing way and then linked by its
        # owner, never created from a provider's assertion.
        return Resolution(refused="no_account")
    if await refresh_setup_state():
        # First-run setup has not created the administrator yet.
        return Resolution(refused="unavailable")

    user_id, identity_id = generate_ulid(), generate_ulid()
    status = "pending_approval" if provider.require_approval else "active"
    created = await op_store.create_sso_user(
        user_id=user_id,
        email=email,
        user_name=identity.name,
        role=provider.default_role,
        status=status,
        identity_id=identity_id,
        provider_id=provider.id,
        issuer=identity.issuer,
        subject=identity.subject,
    )
    if not created:
        return None
    user = await op_store.get_user_by_id(user_id)
    if user is None:
        return Resolution(refused="failed")
    logger.info(f"Single sign-on created user {user_id} via {provider.id} [status={status}]")
    if status != "active":
        return Resolution(refused="pending", created=user)
    return Resolution(user=user, identity_id=identity_id, created=user)
