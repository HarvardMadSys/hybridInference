"""The ``SSO_PROVIDERS`` document: what it accepts, and how a provider is found."""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from serving.config import documents, sso


def _provider(**overrides: Any) -> dict[str, Any]:
    provider = {
        "id": "example-univ",
        "type": "oidc",
        "display_name": "Example University",
        "enabled": True,
        "issuer": "https://idp.example.edu/realms/main",
        "client_id": "hybridinference",
        "client_secret": "${SSO_EXAMPLE_UNIV_CLIENT_SECRET}",
        "client_auth_method": "client_secret_basic",
        "scopes": ["openid", "email", "profile"],
        "domains": ["example.edu", "*.example.edu"],
        "assume_email_verified": False,
        "auto_create": False,
        "require_approval": True,
        "auto_link": False,
        "default_role": "free",
    }
    provider.update(overrides)
    return provider


def _validate(*providers: dict[str, Any]) -> None:
    documents.validate("SSO_PROVIDERS", json.dumps({"providers": list(providers)}))


def _refusal(*providers: dict[str, Any]) -> str:
    with pytest.raises(documents.DocumentError) as excinfo:
        _validate(*providers)
    return str(excinfo.value)


# -- what the document accepts --------------------------------------------------


def test_the_documented_example_is_accepted():
    _validate(_provider())


def test_an_empty_document_means_no_single_sign_on():
    documents.validate("SSO_PROVIDERS", "")
    documents.validate("SSO_PROVIDERS", "{}")
    documents.validate("SSO_PROVIDERS", '{"providers": []}')


def test_only_the_required_fields_are_needed():
    minimal = {
        key: value
        for key, value in _provider().items()
        if key in ("id", "type", "display_name", "issuer", "client_id", "client_secret", "domains")
    }

    provider = sso.SsoProvidersDocument.model_validate({"providers": [minimal]}).providers[0]

    # The defaults are the careful ones: nothing is created or linked unasked.
    assert provider.enabled is True
    assert provider.auto_create is False
    assert provider.require_approval is True
    assert provider.auto_link is False
    assert provider.assume_email_verified is False
    assert provider.default_role == "free"
    assert provider.client_auth_method == "client_secret_basic"
    assert provider.scopes == ["openid", "email", "profile"]
    assert provider.required_claims == {}


def test_claims_an_id_token_must_carry_are_accepted():
    # Google has one issuer for every account and names the organization in hd.
    _validate(
        _provider(issuer="https://accounts.google.com", required_claims={"hd": "example.edu"})
    )


def test_a_client_secret_is_only_ever_a_reference():
    assert "must reference a separate secret setting" in _refusal(
        _provider(client_secret="a-secret-typed-into-the-document")
    )


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("type", "saml", "Input should be 'oidc'"),
        ("default_role", "admin", "'free', 'pro' or 'internal'"),
        ("domains", [], "at least 1 item"),
        ("domains", ["not a domain"], "must be a domain"),
        ("domains", ["example"], "must be a domain"),
        ("domains", ["*example.edu"], "must be a domain"),
        ("domains", ["example.edu", "EXAMPLE.edu"], "lists a domain twice"),
        ("issuer", "http://idp.example.edu", "must be an https URL"),
        ("issuer", "idp.example.edu", "must be an https URL"),
        ("scopes", ["email", "profile"], "must include openid"),
        ("scopes", ["openid", "two words"], "each scope must be one word"),
        ("id", "Example_Univ", "String should match pattern"),
        ("id", "-leading-dash", "String should match pattern"),
        ("id", "x" * 33, "String should match pattern"),
        ("display_name", "   ", "must not be blank"),
        ("client_auth_method", "private_key_jwt", "Input should be"),
        ("required_claims", {"hd": ""}, "needs a claim name and the value"),
        ("required_claims", {" ": "example.edu"}, "needs a claim name and the value"),
        ("required_claims", {"hd": True}, "Input should be a valid string"),
    ],
)
def test_a_field_that_is_wrong_is_refused_by_name(field, value, reason):
    message = _refusal(_provider(**{field: value}))

    assert f"/providers/0/{field}" in message
    assert reason in message


def test_a_field_this_version_does_not_know_is_refused():
    assert "Extra inputs are not permitted" in _refusal(_provider(show_button=True))


def test_a_field_named_like_a_credential_cannot_hold_a_plain_value():
    # Why the client authentication field is not named after the token endpoint:
    # the document rules read any field with "token" in its name as a credential.
    message = _refusal(_provider(token_endpoint_auth_method="client_secret_basic"))

    assert "/providers/0/token_endpoint_auth_method must reference" in message


def test_a_local_issuer_may_use_http():
    _validate(_provider(issuer="http://localhost:8080/realms/dev"))


def test_two_providers_cannot_share_an_id():
    assert "share an id" in _refusal(_provider(), _provider(domains=["other.edu"]))


@pytest.mark.parametrize(
    ("first", "second"),
    [
        (["example.edu"], ["example.edu"]),
        (["*.example.edu"], ["cs.example.edu"]),
        (["cs.example.edu"], ["*.example.edu"]),
        (["*.example.edu"], ["*.cs.example.edu"]),
        (["*.cs.example.edu"], ["*.example.edu"]),
    ],
)
def test_an_email_domain_leads_to_one_provider(first, second):
    message = _refusal(_provider(domains=first), _provider(id="second", domains=second))

    assert "example-univ and second claim overlapping domains" in message


def test_domains_that_only_look_alike_do_not_overlap():
    _validate(
        _provider(domains=["example.edu"]),
        # The wildcard does not cover the bare domain, and a suffix is not a subdomain.
        _provider(id="second", domains=["*.example.edu"]),
        _provider(id="third", domains=["notexample.edu", "example.edu.cn"]),
    )


# -- domain matching ------------------------------------------------------------


@pytest.mark.parametrize(
    ("domain", "entry", "matches"),
    [
        ("example.edu", "example.edu", True),
        ("cs.example.edu", "example.edu", False),
        ("cs.example.edu", "*.example.edu", True),
        ("a.b.example.edu", "*.example.edu", True),
        ("example.edu", "*.example.edu", False),
        ("notexample.edu", "*.example.edu", False),
        ("example.edu.evil.test", "example.edu", False),
    ],
)
def test_domain_matching_follows_the_signup_allowlist_rules(domain, entry, matches):
    assert sso.domain_matches(domain, entry) is matches


@pytest.mark.parametrize(
    ("email", "domain"),
    [
        ("Alice@Example.EDU", "example.edu"),
        ("a@b@example.edu", "example.edu"),
        ("no-at-sign", None),
        ("trailing@", None),
        ("", None),
        (None, None),
    ],
)
def test_the_domain_of_an_email(email, domain):
    assert sso.email_domain(email) == domain


# -- finding a provider ---------------------------------------------------------


@pytest.fixture
def stored(monkeypatch):
    """Serve a document as the stored one."""

    def store(*providers: dict[str, Any]) -> None:
        value = {"providers": copy.deepcopy(list(providers))}
        monkeypatch.setattr(documents, "get", lambda kind, **_: value)
        sso.get_sso_document.cache_clear()

    yield store
    sso.get_sso_document.cache_clear()


def test_a_provider_is_found_by_email_domain_and_id(stored):
    stored(
        _provider(),
        _provider(id="company", domains=["example.com"], issuer="https://sso.example.com"),
    )

    assert sso.provider_for_email("Alice@CS.Example.edu").id == "example-univ"
    assert sso.provider_for_email("bob@example.com").id == "company"
    assert sso.provider_for_email("carol@elsewhere.org") is None
    assert sso.provider_for_email("not-an-email") is None
    assert sso.provider_by_id("company").display_name == "Example University"
    assert sso.provider_by_id("missing") is None


GOOGLE = "https://accounts.google.com"
MEMBER = "alice@example.edu"


def test_an_identitys_provider_is_the_one_it_was_linked_through(stored):
    stored(
        _provider(id="univ", issuer=GOOGLE),
        _provider(id="company", domains=["example.com"], issuer=GOOGLE),
    )

    # Two organizations at one issuer: the issuer alone does not say which.
    assert sso.provider_for_identity("univ", GOOGLE, MEMBER).id == "univ"
    assert sso.provider_for_identity("company", GOOGLE, "bob@example.com").id == "company"
    # The link's email does not move it while its provider is in the document.
    assert sso.provider_for_identity("company", GOOGLE, MEMBER).id == "company"
    # An id that names a provider at another issuer is not the identity's provider.
    assert sso.provider_for_identity("company", "https://unknown.example.com", MEMBER) is None

    # Each provider serves the identities linked through it, and not the other's.
    univ, company = sso.provider_by_id("univ"), sso.provider_by_id("company")
    assert sso.serves_identity(univ, "univ", GOOGLE, MEMBER)
    assert not sso.serves_identity(company, "univ", GOOGLE, MEMBER)
    assert not sso.serves_identity(univ, "univ", "https://unknown.example.com", MEMBER)


def test_a_provider_that_is_switched_off_is_not_replaced_by_one_with_its_issuer(stored):
    stored(
        _provider(id="univ", issuer=GOOGLE, enabled=False, required_claims={"hd": "example.edu"}),
        _provider(id="personal", domains=["mail.example.org"], issuer=GOOGLE),
    )
    personal = sso.provider_by_id("personal")
    in_its_domain = "alice@mail.example.org"

    # Linked through the provider that is switched off: no other one serves it,
    # whatever email the link was made with.
    assert sso.provider_for_identity("univ", GOOGLE, MEMBER) is None
    assert not sso.serves_identity(personal, "univ", GOOGLE, MEMBER)
    assert not sso.serves_identity(personal, "univ", GOOGLE, in_its_domain)
    assert sso.serves_identity(personal, "personal", GOOGLE, in_its_domain)


def test_a_changed_id_passes_its_links_to_the_provider_that_answers_for_their_email(stored):
    # The provider that requires no claim comes first: the order decides nothing.
    stored(
        _provider(id="personal", domains=["gmail.com"], issuer=GOOGLE),
        _provider(id="univ-2", issuer=GOOGLE, required_claims={"hd": "example.edu"}),
    )
    personal, renamed = sso.provider_by_id("personal"), sso.provider_by_id("univ-2")

    # Linked through "univ", an id no provider has any more.
    assert sso.provider_for_identity("univ", GOOGLE, MEMBER).id == "univ-2"
    assert sso.serves_identity(renamed, "univ", GOOGLE, MEMBER)
    assert not sso.serves_identity(personal, "univ", GOOGLE, MEMBER)
    # A link made without an email, or with one no provider answers for, has
    # no successor.
    for email in (None, "alice@elsewhere.org"):
        assert sso.provider_for_identity("univ", GOOGLE, email) is None


def test_a_removed_provider_leaves_its_links_to_no_other(stored):
    stored(_provider(id="personal", domains=["gmail.com"], issuer=GOOGLE))

    assert sso.provider_for_identity("univ", GOOGLE, MEMBER) is None
    assert not sso.serves_identity(sso.provider_by_id("personal"), "univ", GOOGLE, MEMBER)


def test_an_id_that_now_names_a_provider_at_another_issuer_is_a_changed_id(stored):
    stored(
        _provider(id="univ", domains=["other.example.org"]),
        _provider(id="univ-2", issuer=GOOGLE),
    )

    # The identity was linked through "univ" when that id had Google's issuer.
    assert sso.provider_for_identity("univ", GOOGLE, MEMBER).id == "univ-2"


def test_a_disabled_provider_is_not_offered(stored):
    stored(_provider(enabled=False))

    assert sso.enabled_providers() == []
    assert sso.provider_for_email("alice@example.edu") is None
    assert sso.provider_by_id("example-univ") is None


def test_a_stored_document_that_does_not_validate_offers_nothing(stored):
    stored(_provider(default_role="admin"))

    assert sso.enabled_providers() == []


def test_the_client_secret_is_read_from_the_setting_it_references(monkeypatch):
    from serving.config import app_config

    values = {"SSO_EXAMPLE_UNIV_CLIENT_SECRET": "the-real-secret"}
    monkeypatch.setattr(app_config, "config_value", lambda key, default=None: values.get(key))
    provider = sso.SsoProvider.model_validate(_provider())

    assert sso.client_secret(provider) == "the-real-secret"

    values.clear()
    assert sso.client_secret(provider) is None


def test_the_secret_a_document_references_is_listed_as_a_setting_to_fill():
    from serving.config import app_config_registry as registry

    found = registry.discover_documents({}, {}, {}, {"providers": [_provider()]})

    reference = found["SSO_EXAMPLE_UNIV_CLIENT_SECRET"]
    assert reference.category == "security"
    assert reference.credential is True


def test_the_referenced_secret_says_what_references_it_and_waits_for_a_restart():
    """Like every setting a document references, it is read when the gateway starts."""
    from serving.config import app_config_registry as registry

    found = registry.discover_documents({}, {}, {}, {"providers": [_provider()]})
    entry = registry.build_entries(found, {}, {})["SSO_EXAMPLE_UNIV_CLIENT_SECRET"]

    assert entry.description == "Referenced by the single sign-on providers."
    assert entry.secret is True
    assert entry.restart_required is True


def test_a_secret_the_alert_rules_also_reference_stays_theirs():
    from serving.config import app_config_registry as registry

    alerts = {"channels": [{"url": "${SSO_EXAMPLE_UNIV_CLIENT_SECRET}"}]}
    found = registry.discover_documents({}, {}, alerts, {"providers": [_provider()]})
    entry = registry.build_entries(found, {}, {})["SSO_EXAMPLE_UNIV_CLIENT_SECRET"]

    assert entry.category == "alerts"
    assert entry.description == "Referenced by the alert rules."


def test_the_document_is_a_security_relevant_application_setting():
    from serving.config import app_config
    from serving.config.app_config_registry import static_entry

    entry = static_entry("SSO_PROVIDERS")

    assert entry.category == "application"
    assert entry.restart_required is False
    # Changing who may sign in asks for the administrator's password.
    assert app_config.security_relevant("SSO_PROVIDERS") is True
