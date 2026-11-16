"""Which account an identity signs in to: every row of the decision, on a small store."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from serving.auth import sso_accounts
from serving.auth.oidc import ExternalIdentity
from serving.config.sso import SsoProvider
from serving.storage import sso as sso_store

ISSUER = "https://idp.example.edu/realms/main"


def _provider(**overrides: Any) -> SsoProvider:
    fields = {
        "id": "example-univ",
        "type": "oidc",
        "display_name": "Example University",
        "issuer": ISSUER,
        "client_id": "hybridinference",
        "client_secret": "${SSO_SECRET}",
        "domains": ["example.edu"],
    }
    fields.update(overrides)
    return SsoProvider.model_validate(fields)


def _identity(**overrides: Any) -> ExternalIdentity:
    fields = {
        "issuer": ISSUER,
        "subject": "subject-1",
        "email": "alice@example.edu",
        "email_verified": True,
        "name": "Alice Example",
    }
    fields.update(overrides)
    return ExternalIdentity(**fields)


class Store:
    """Users and identities in memory, with the store's own answers."""

    def __init__(self) -> None:
        self.users: dict[str, dict[str, Any]] = {}
        self.identities: list[dict[str, Any]] = []
        self.created: list[dict[str, Any]] = []
        self.link_outcome: str | None = None
        self.create_succeeds = True

    def add_user(self, user_id: str, email: str | None, **fields: Any) -> dict[str, Any]:
        user = {
            "id": user_id,
            "email": email,
            "status": "active",
            "role": "free",
            "email_verified": True,
            "password_hash": "hash",
            "user_name": None,
            **fields,
        }
        self.users[user_id] = user
        return user

    def add_identity(self, user_id: str, *, issuer: str = ISSUER, subject: str = "subject-1"):
        row = {
            "id": f"identity-{len(self.identities) + 1}",
            "user_id": user_id,
            "issuer": issuer,
            "subject": subject,
            "provider_id": "example-univ",
            "email": "alice@example.edu",
        }
        self.identities.append(row)
        return row

    async def get_user_identity(self, issuer: str, subject: str):
        return next(
            (i for i in self.identities if (i["issuer"], i["subject"]) == (issuer, subject)), None
        )

    async def list_user_identities(self, user_id: str):
        return [i for i in self.identities if i["user_id"] == user_id]

    async def get_user_by_id(self, user_id: str):
        return self.users.get(user_id)

    async def get_user_by_email(self, email: str):
        return next((u for u in self.users.values() if u["email"] == email.lower()), None)

    async def link_user_identity(self, **fields: Any) -> str:
        if self.link_outcome is not None:
            return self.link_outcome
        self.add_identity(fields["user_id"], issuer=fields["issuer"], subject=fields["subject"])
        return sso_store.LINKED

    async def create_sso_user(self, **fields: Any) -> bool:
        if not self.create_succeeds:
            return False
        self.created.append(fields)
        self.add_user(
            fields["user_id"],
            fields["email"],
            status=fields["status"],
            role=fields["role"],
            password_hash=None,
            user_name=fields["user_name"],
        )
        self.add_identity(fields["user_id"], issuer=fields["issuer"], subject=fields["subject"])
        return True


@pytest.fixture
def store(monkeypatch) -> Store:
    monkeypatch.setattr(sso_accounts, "email_verification_required", AsyncMock(return_value=True))
    monkeypatch.setattr(sso_accounts, "refresh_setup_state", AsyncMock(return_value=False))
    monkeypatch.setattr(sso_accounts, "is_admin_email", lambda email: email == "root@example.edu")
    return Store()


async def _resolve(store: Store, provider: SsoProvider | None = None, **identity: Any):
    return await sso_accounts.resolve_sign_in(store, provider or _provider(), _identity(**identity))


# -- 1. an identity that is already linked ---------------------------------------


async def test_a_linked_identity_signs_in_to_its_account(store):
    user = store.add_user("u-1", "old-address@elsewhere.org")
    identity = store.add_identity("u-1")

    # The email no longer matters once linked: it differs, is unverified, and
    # is outside the provider's domains.
    resolution = await _resolve(store, email="new@unrelated.org", email_verified=False)

    assert resolution.user == user
    assert resolution.identity_id == identity["id"]
    assert resolution.refused is None and not resolution.pending_link


@pytest.mark.parametrize(
    ("status", "code"),
    [
        ("pending_approval", "pending"),
        ("suspended", "suspended"),
        ("rejected", "inactive"),
        ("deleted", "inactive"),
    ],
)
async def test_a_linked_account_that_is_not_active_is_refused_by_its_status(store, status, code):
    store.add_user("u-1", "alice@example.edu", status=status)
    store.add_identity("u-1")

    assert (await _resolve(store)).refused == code


@pytest.mark.parametrize(
    "own",
    [
        pytest.param([{}], id="its own provider is offered"),
        pytest.param([{"enabled": False}], id="its own provider is switched off"),
        pytest.param([{"id": "univ-2"}], id="its own provider's id has changed"),
        pytest.param([], id="its own provider is removed"),
    ],
)
async def test_a_linked_identity_never_signs_in_through_the_provider_beside_its_own(
    store, monkeypatch, own
):
    # Two providers at one issuer, as for two Google Workspace organizations.
    # The identity was linked through "example-univ" and comes back through
    # "personal", which requires no claim.
    personal = _provider(id="personal", domains=["gmail.com"])
    entries = [personal, *(_provider(**fields) for fields in own)]
    document = sso_accounts.sso_config.SsoProvidersDocument(providers=entries)
    monkeypatch.setattr(sso_accounts.sso_config, "get_sso_document", lambda: document)
    store.add_user("u-1", "alice@example.edu")
    store.add_identity("u-1")

    resolution = await _resolve(store, personal)

    assert resolution.refused == "failed" and resolution.user is None


async def test_a_linked_identity_follows_its_provider_to_a_changed_id(store, monkeypatch):
    # The provider that answers for the link's email took the link over.
    renamed = _provider(id="univ-2")
    document = sso_accounts.sso_config.SsoProvidersDocument(
        providers=[_provider(id="personal", domains=["gmail.com"]), renamed]
    )
    monkeypatch.setattr(sso_accounts.sso_config, "get_sso_document", lambda: document)
    user = store.add_user("u-1", "alice@example.edu")
    store.add_identity("u-1")

    assert (await _resolve(store, renamed)).user == user


# -- 2. the email must be trusted -------------------------------------------------


@pytest.mark.parametrize(
    "identity",
    [
        pytest.param({"email": None}, id="no email"),
        pytest.param({"email_verified": False}, id="not verified"),
        pytest.param({"email_verified": None}, id="provider did not say"),
        pytest.param({"email": "alice@elsewhere.org"}, id="another domain"),
        pytest.param({"email": "alice@cs.example.edu"}, id="a subdomain not claimed"),
    ],
)
async def test_an_unlinked_identity_without_a_trusted_email_is_refused(store, identity):
    store.add_user("u-1", "alice@example.edu")
    store.add_user("u-2", "alice@elsewhere.org")

    resolution = await _resolve(store, _provider(auto_create=True, auto_link=True), **identity)

    assert resolution.refused == "email_untrusted"
    assert store.identities == [] and store.created == []


async def test_a_deployment_may_vouch_for_a_provider_that_never_says_verified(store):
    provider = _provider(assume_email_verified=True, auto_create=True, require_approval=False)

    resolution = await _resolve(store, provider, email_verified=None)

    assert resolution.user["email"] == "alice@example.edu"


async def test_a_provider_never_vouches_outside_its_domains(store):
    provider = _provider(assume_email_verified=True, auto_create=True, require_approval=False)

    resolution = await _resolve(store, provider, email="alice@elsewhere.org")

    assert resolution.refused == "email_untrusted"


# -- 3. an account with that email that is not active ------------------------------


@pytest.mark.parametrize(
    ("status", "code"),
    [("deleted", "inactive"), ("suspended", "suspended"), ("pending_approval", "pending")],
)
async def test_an_inactive_account_with_the_email_is_neither_linked_nor_replaced(
    store, status, code
):
    store.add_user("u-1", "alice@example.edu", status=status)

    resolution = await _resolve(store, _provider(auto_create=True, auto_link=True))

    assert resolution.refused == code
    assert store.identities == [] and store.created == []


# -- 4. an active account with that email -------------------------------------------


async def test_an_account_holding_another_identity_from_the_provider_is_a_conflict(store):
    store.add_user("u-1", "alice@example.edu")
    store.add_identity("u-1", subject="the-old-subject")

    resolution = await _resolve(store, _provider(auto_link=True), subject="a-new-subject")

    assert resolution.refused == "identity_conflict"
    assert len(store.identities) == 1


async def test_by_default_the_identity_waits_for_the_accounts_owner(store):
    store.add_user("u-1", "alice@example.edu")

    resolution = await _resolve(store)

    assert resolution.pending_link is True
    assert resolution.user is None and resolution.refused is None
    assert store.identities == []


async def test_automatic_linking_links_and_signs_in(store):
    user = store.add_user("u-1", "alice@example.edu")

    resolution = await _resolve(store, _provider(auto_link=True))

    assert resolution.user == user
    assert resolution.identity_id == store.identities[0]["id"]
    assert store.identities[0]["user_id"] == "u-1"


@pytest.mark.parametrize(
    "case",
    ["unverified account", "administrator", "administrator to be", "verification not required"],
)
async def test_automatic_linking_needs_every_condition(store, monkeypatch, case):
    fields: dict[str, Any] = {}
    email = "alice@example.edu"
    if case == "unverified account":
        fields["email_verified"] = False
    elif case == "administrator":
        fields["role"] = "admin"
    elif case == "administrator to be":
        # On the ADMIN_EMAILS list, still at the default role.
        email = "root@example.edu"
    else:
        # Every new account is then marked verified without proof.
        monkeypatch.setattr(
            sso_accounts, "email_verification_required", AsyncMock(return_value=False)
        )
    store.add_user("u-1", email, **fields)

    resolution = await _resolve(store, _provider(auto_link=True), email=email)

    assert resolution.pending_link is True
    assert store.identities == []


@pytest.mark.parametrize(
    ("outcome", "code"),
    [
        (sso_store.ISSUER_CONFLICT, "identity_conflict"),
        (sso_store.ACCOUNT_INACTIVE, "inactive"),
    ],
)
async def test_a_link_the_store_refuses_is_reported(store, outcome, code):
    store.add_user("u-1", "alice@example.edu")
    store.link_outcome = outcome

    assert (await _resolve(store, _provider(auto_link=True))).refused == code


async def test_an_identity_taken_during_linking_is_not_signed_in_blindly(store):
    store.add_user("u-1", "alice@example.edu")
    store.link_outcome = sso_store.IDENTITY_IN_USE

    # Both passes see the identity unlinked and the link refused: nothing is assumed.
    assert (await _resolve(store, _provider(auto_link=True))).refused == "failed"


# -- 5. no account with that email -------------------------------------------------


async def test_without_an_account_and_without_automatic_creation_the_sign_in_is_refused(store):
    resolution = await _resolve(store)

    assert resolution.refused == "no_account"
    assert store.created == []


async def test_a_first_sign_in_creates_an_account_that_waits_for_approval(store):
    resolution = await _resolve(store, _provider(auto_create=True, default_role="pro"))

    assert resolution.refused == "pending"
    assert resolution.user is None
    assert resolution.created["status"] == "pending_approval"
    (created,) = store.created
    assert created["email"] == "alice@example.edu"
    assert (created["status"], created["role"]) == ("pending_approval", "pro")
    assert created["user_name"] == "Alice Example"
    assert (created["issuer"], created["subject"]) == (ISSUER, "subject-1")
    assert created["provider_id"] == "example-univ"


async def test_a_first_sign_in_without_approval_signs_straight_in(store):
    resolution = await _resolve(store, _provider(auto_create=True, require_approval=False))

    assert resolution.refused is None
    assert resolution.user["status"] == "active"
    assert resolution.user["password_hash"] is None
    assert resolution.created == resolution.user
    assert resolution.identity_id == store.created[0]["identity_id"]


async def test_an_administrator_is_never_created_from_a_providers_assertion(store):
    provider = _provider(auto_create=True, require_approval=False)

    resolution = await _resolve(store, provider, email="root@example.edu")

    assert resolution.refused == "no_account"
    assert store.created == []


async def test_a_blocked_email_domain_creates_nothing(store, monkeypatch):
    monkeypatch.setattr(sso_accounts, "is_email_domain_blocked", lambda email: True)

    resolution = await _resolve(store, _provider(auto_create=True))

    assert resolution.refused == "no_account"
    assert store.created == []


async def test_nothing_is_created_before_first_run_setup(store, monkeypatch):
    monkeypatch.setattr(sso_accounts, "refresh_setup_state", AsyncMock(return_value=True))

    resolution = await _resolve(store, _provider(auto_create=True, require_approval=False))

    assert resolution.refused == "unavailable"
    assert store.created == []


async def test_an_account_created_meanwhile_is_seen_on_the_second_pass(store):
    # The email is taken between the lookup and the write: the store creates
    # nothing, and the second pass finds the account that took it.
    store.create_succeeds = False
    create = store.create_sso_user

    async def taken_meanwhile(**fields: Any) -> bool:
        store.add_user("u-other", "alice@example.edu")
        return await create(**fields)

    store.create_sso_user = taken_meanwhile

    resolution = await _resolve(store, _provider(auto_create=True, require_approval=False))

    assert resolution.pending_link is True
    assert store.created == []


# -- the pieces -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "code"),
    [
        ("active", None),
        ("pending_approval", "pending"),
        ("suspended", "suspended"),
        ("rejected", "inactive"),
        ("deleted", "inactive"),
        (None, "inactive"),
    ],
)
def test_an_accounts_status_decides_the_refusal(status, code):
    assert sso_accounts.status_refusal(status) == code


# -- an account's provider ---------------------------------------------------------


@pytest.mark.parametrize(
    ("fields", "confirms"),
    [
        ({"password_hash": None, "phone": None}, True),
        ({"password_hash": None, "phone": "+8613800138000", "phone_verified": False}, True),
        ({"password_hash": "hash", "phone": None}, False),
        ({"password_hash": None, "phone": "+8613800138000", "phone_verified": True}, False),
    ],
)
def test_only_an_account_with_no_password_and_no_verified_phone_confirms_at_its_provider(
    fields, confirms
):
    assert sso_accounts.confirms_through_provider(fields) is confirms


async def test_an_accounts_provider_is_the_offered_one_it_last_signed_in_through(monkeypatch):
    from datetime import UTC, datetime

    offered = {"https://new.example.edu": _provider(id="new", issuer="https://new.example.edu")}
    monkeypatch.setattr(
        sso_accounts.sso_config,
        "provider_for_identity",
        lambda _id, issuer, _email: offered.get(issuer),
    )
    rows = [
        {
            "provider_id": "new",
            "issuer": "https://new.example.edu",
            "created_at": datetime(2026, 1, 1, tzinfo=UTC),
        },
        {
            "provider_id": "gone",
            "issuer": "https://gone.example.edu",
            "created_at": datetime(2026, 1, 1, tzinfo=UTC),
            "last_login_at": datetime(2026, 6, 1, tzinfo=UTC),
        },
    ]
    store = AsyncMock()
    store.list_user_identities.return_value = rows

    # The most recently used identity belongs to a provider that is gone.
    assert (await sso_accounts.account_provider(store, "u-1")).id == "new"

    store.list_user_identities.return_value = rows[1:]
    assert await sso_accounts.account_provider(store, "u-1") is None

    # A store that keeps no identities has none to offer.
    store.list_user_identities.side_effect = RuntimeError("unavailable in this store")
    assert await sso_accounts.account_provider(store, "u-1") is None
    assert await sso_accounts.identity_summaries(store, "u-1") == []


def test_an_identity_is_described_with_its_provider_as_configured_now(monkeypatch):
    from datetime import UTC, datetime

    row = {
        "id": "i-1",
        "issuer": ISSUER,
        "provider_id": "old-id",
        "email": "alice@example.edu",
        "created_at": datetime(2026, 1, 1, tzinfo=UTC),
        "last_login_at": None,
    }
    monkeypatch.setattr(
        sso_accounts.sso_config, "provider_for_identity", lambda _id, _issuer, _email: _provider()
    )

    summary = sso_accounts.identity_summary(row)
    assert (summary.provider_id, summary.provider_name) == ("example-univ", "Example University")
    assert summary.available is True

    # A provider that is no longer offered keeps the id it was linked under.
    monkeypatch.setattr(
        sso_accounts.sso_config, "provider_for_identity", lambda _id, _issuer, _email: None
    )
    summary = sso_accounts.identity_summary(row)
    assert (summary.provider_id, summary.provider_name) == ("old-id", "old-id")
    assert summary.available is False
