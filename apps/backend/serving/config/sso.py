"""Single sign-on providers: the ``SSO_PROVIDERS`` application document.

One entry per identity provider. ``type`` names the protocol; only ``oidc``
exists today, and a later protocol adds a value and its own fields without
changing the entries already stored. The fields a provider shares whatever its
protocol -- the email domains it answers for and what a first sign-in may do --
are read by the account logic; the protocol module reads the rest.

The document holds no secret: ``client_secret`` is a ``${NAME}`` reference to a
separate secret setting (:mod:`serving.config.documents` refuses anything else).
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from serving.utils.logging import get_logger

logger = get_logger(__name__)

_DOMAIN = r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+"
_DOMAIN_PATTERN = re.compile(rf"^(\*\.)?{_DOMAIN}$")
_SCOPE = re.compile(r"^[\x21\x23-\x5b\x5d-\x7e]+$")  # RFC 6749 scope-token


class SsoProvider(BaseModel):
    """One identity provider users may sign in through."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?$")
    type: Literal["oidc"]
    display_name: str = Field(min_length=1, max_length=80)
    enabled: bool = True
    #: The email domains this provider answers for: ``example.edu`` matches that
    #: domain, ``*.example.edu`` its subdomains and not the domain itself.
    domains: list[str] = Field(min_length=1)
    #: Treat the provider's email as verified although it does not say so.
    assume_email_verified: bool = False
    #: A first sign-in with no account here creates one.
    auto_create: bool = False
    #: An account created that way waits for an administrator's approval.
    require_approval: bool = True
    #: An existing account with the same email is linked without asking.
    auto_link: bool = False
    default_role: Literal["free", "pro", "internal"] = "free"

    # -- OpenID Connect ------------------------------------------------------
    issuer: str = Field(min_length=1)
    client_id: str = Field(min_length=1)
    client_secret: str = Field(min_length=1)
    client_auth_method: Literal["client_secret_basic", "client_secret_post"] = "client_secret_basic"
    scopes: list[str] = Field(default_factory=lambda: ["openid", "email", "profile"])
    #: Claims the ID token must carry, with exactly these values. For an issuer
    #: that more than one organization signs in at: Google has one issuer for
    #: every account and names an account's organization in ``hd``.
    required_claims: dict[str, str] = Field(default_factory=dict)

    @field_validator("display_name")
    @classmethod
    def _display_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value.strip()

    @field_validator("issuer")
    @classmethod
    def _issuer(cls, value: str) -> str:
        from serving.auth.oidc import is_secure_url

        if not is_secure_url(value):
            raise ValueError("must be an https URL (http is accepted for localhost only)")
        return value

    @field_validator("scopes")
    @classmethod
    def _scopes(cls, value: list[str]) -> list[str]:
        if "openid" not in value:
            raise ValueError("must include openid")
        if any(not _SCOPE.fullmatch(scope) for scope in value):
            raise ValueError("each scope must be one word")
        return value

    @field_validator("required_claims")
    @classmethod
    def _required_claims(cls, value: dict[str, str]) -> dict[str, str]:
        if any(not name.strip() or not expected.strip() for name, expected in value.items()):
            raise ValueError("each entry needs a claim name and the value it must have")
        return value

    @field_validator("domains")
    @classmethod
    def _domains(cls, value: list[str]) -> list[str]:
        domains = [domain.strip().lower() for domain in value]
        if any(not _DOMAIN_PATTERN.fullmatch(domain) for domain in domains):
            raise ValueError("each entry must be a domain such as example.edu or *.example.edu")
        if len(set(domains)) != len(domains):
            raise ValueError("lists a domain twice")
        return domains

    def answers_for(self, email: str | None) -> bool:
        """Whether *email*'s domain is one this provider answers for."""
        domain = email_domain(email)
        return domain is not None and any(domain_matches(domain, entry) for entry in self.domains)


class SsoProvidersDocument(BaseModel):
    """The whole ``SSO_PROVIDERS`` document."""

    model_config = ConfigDict(extra="forbid")

    providers: list[SsoProvider] = Field(default_factory=list)

    @model_validator(mode="after")
    def _distinct(self) -> SsoProvidersDocument:
        ids = [provider.id for provider in self.providers]
        if len(set(ids)) != len(ids):
            raise ValueError("two providers share an id")
        # An email domain must lead to one provider: the sign-in page finds the
        # provider from the email alone, and a provider vouches only for emails
        # in its own domains.
        for index, provider in enumerate(self.providers):
            for other in self.providers[index + 1 :]:
                if any(_overlap(a, b) for a in provider.domains for b in other.domains):
                    raise ValueError(
                        f"providers {provider.id} and {other.id} claim overlapping domains"
                    )
        return self


def email_domain(email: str | None) -> str | None:
    """The lower-cased domain of *email*, or None when it has none."""
    if not email or "@" not in email:
        return None
    domain = email.rsplit("@", 1)[1].strip().lower()
    return domain or None


def domain_matches(domain: str, entry: str) -> bool:
    """Whether *domain* is covered by one ``domains`` entry.

    The rules of the signup allowlist: an exact entry matches that domain, and
    ``*.example.edu`` matches any subdomain of ``example.edu`` but not
    ``example.edu`` itself.
    """
    if entry.startswith("*."):
        return domain.endswith(entry[1:]) and domain != entry[2:]
    return domain == entry


def _overlap(first: str, second: str) -> bool:
    """Whether some domain is covered by both entries."""
    if first == second:
        return True
    first_wild, second_wild = first.startswith("*."), second.startswith("*.")
    if first_wild and second_wild:
        return first.endswith(second[1:]) or second.endswith(first[1:])
    if first_wild:
        return domain_matches(second, first)
    if second_wild:
        return domain_matches(first, second)
    return False


@lru_cache(maxsize=1)
def get_sso_document() -> SsoProvidersDocument:
    """Return the stored providers; an absent or unreadable document is no providers.

    Cached until the configuration changes. A document that does not validate
    was refused when it was saved, so this is reached only for one imported from
    the environment or edited by hand; sign-in then reports single sign-on as
    unavailable rather than half-applying it.
    """
    from serving.config.documents import get

    try:
        return SsoProvidersDocument.model_validate(get("sso"))
    except (ValidationError, ValueError):
        logger.error(
            "SSO_PROVIDERS is not valid; single sign-on is unavailable until it is corrected "
            "in Admin Settings."
        )
        return SsoProvidersDocument()


def enabled_providers() -> list[SsoProvider]:
    """The providers users may sign in through right now."""
    return [provider for provider in get_sso_document().providers if provider.enabled]


def provider_by_id(provider_id: str) -> SsoProvider | None:
    """Find an enabled provider by its id."""
    return next((p for p in enabled_providers() if p.id == provider_id), None)


def provider_for_email(email: str | None) -> SsoProvider | None:
    """Find the enabled provider that answers for *email*'s domain."""
    return next((p for p in enabled_providers() if p.answers_for(email)), None)


def serves_identity(provider: SsoProvider, provider_id: str, issuer: str) -> bool:
    """Whether a linked identity signs in through *provider*.

    An identity signs in through the provider it was linked through, and
    through no other while the document still has that provider for the
    identity's issuer -- switched off included. Two providers may share an
    issuer (every Google Workspace organization signs in at
    ``https://accounts.google.com``), so the issuer alone does not say which
    one: a provider that is switched off, or that requires a claim, would
    otherwise be passed by a sign-in through its neighbour.

    Only when no provider has that id for the issuer any more does another
    provider with the issuer serve the identity: an identity belongs to its
    issuer, and so outlives a change of its provider's id.
    """
    if provider.issuer != issuer:
        return False
    if provider.id == provider_id:
        return True
    return not any(
        entry.id == provider_id and entry.issuer == issuer for entry in get_sso_document().providers
    )


def provider_for_identity(provider_id: str, issuer: str) -> SsoProvider | None:
    """Find the enabled provider a linked identity signs in through, if any.

    None while the provider it was linked through is switched off; see
    :func:`serves_identity`.
    """
    return next((p for p in enabled_providers() if serves_identity(p, provider_id, issuer)), None)


def client_secret(provider: SsoProvider) -> str | None:
    """Resolve the secret setting the provider's ``client_secret`` references."""
    from serving.config.app_config import config_value

    reference = provider.client_secret
    if not (reference.startswith("${") and reference.endswith("}")):
        return None
    return config_value(reference[2:-1]) or None


def _on_configuration_change() -> None:
    from serving.auth import oidc

    get_sso_document.cache_clear()
    # A provider's endpoints and keys may have moved with its configuration.
    oidc.clear_caches()


def _register() -> None:
    from serving.config.app_config import on_change

    on_change(_on_configuration_change)


_register()
