"""The small rules around an organization: a cursor, a name, and a store that cannot answer."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from serving import organizations
from serving.schemas_organization import CreateOrganizationRequest, UpdateOrganizationRequest
from serving.storage.base import OrganizationsUnavailableError
from serving.storage.pools import DatabaseBusyError

# -- the cursor of a request list ------------------------------------------------------


def test_a_cursor_is_read_back_as_it_was_made():
    row = {"timestamp": datetime(2026, 11, 20, 5, 30, 1, 250, tzinfo=UTC), "id": 4821}

    assert organizations.parse_request_cursor(organizations.request_cursor(row)) == (
        row["timestamp"],
        4821,
    )


@pytest.mark.parametrize(
    "cursor",
    [
        "not-a-cursor",
        "",
        "5",
        "-5",
        "5-",
        # Digits to ``str.isdigit`` that ``int`` refuses, or reads as other digits.
        "²-5",
        "5-²",
        # Fullwidth digits.
        "\uff11\uff17-\uff15",
        # No row has an id a BIGINT cannot hold, and no instant is this far away.
        "5-9223372036854775808",
        "5-99999999999999999999999",
        f"{10**30}-5",
    ],
)
def test_anything_else_is_not_a_cursor(cursor):
    assert organizations.parse_request_cursor(cursor) is None


def test_the_largest_row_id_is_a_cursor():
    assert organizations.parse_request_cursor(f"0-{2**63 - 1}") is not None


# -- the name of an organization -------------------------------------------------------


def test_a_name_is_kept_without_the_spaces_around_it():
    created = CreateOrganizationRequest(name="  Example University ", provider_ids=["campus"])
    renamed = UpdateOrganizationRequest(name="\tExample  ")

    assert (created.name, renamed.name) == ("Example University", "Example")


@pytest.mark.parametrize("name", ["", " ", "   \t "])
def test_a_name_of_spaces_is_no_name(name):
    with pytest.raises(ValidationError):
        CreateOrganizationRequest(name=name, provider_ids=["campus"])
    with pytest.raises(ValidationError):
        UpdateOrganizationRequest(name=name)


def test_a_name_is_measured_after_the_spaces_are_taken_off():
    padded = " " * 40 + "x" * 80 + " " * 40

    assert len(CreateOrganizationRequest(name=padded, provider_ids=["campus"]).name) == 80
    with pytest.raises(ValidationError):
        CreateOrganizationRequest(name="x" * 81, provider_ids=["campus"])


def test_an_update_that_sends_no_name_changes_none():
    assert UpdateOrganizationRequest().name is None


# -- a store that keeps no organizations, and one that cannot answer now ----------------


def _store(**methods) -> MagicMock:
    store = MagicMock(spec=list(methods))
    for name, effect in methods.items():
        setattr(store, name, AsyncMock(side_effect=effect))
    return store


async def test_a_store_that_keeps_no_organizations_has_no_member():
    store = _store(
        get_organization_membership=OrganizationsUnavailableError(),
        get_organization_spend_scope=OrganizationsUnavailableError(),
        clear_organization_member_removal=OrganizationsUnavailableError(),
    )

    assert await organizations.membership_of(store, "u1") is None
    assert await organizations.spend_scope_of(store, "u1") is None
    assert await organizations.forget_removal(store, "u1") is None


async def test_a_store_without_the_methods_has_no_member():
    store = MagicMock(spec=[])

    assert await organizations.membership_of(store, "u1") is None
    assert await organizations.spend_scope_of(store, "u1") is None
    assert await organizations.forget_removal(store, "u1") is None


@pytest.mark.parametrize(
    "error", [DatabaseBusyError("no connection in time"), RuntimeError("something else")]
)
async def test_a_store_that_cannot_answer_is_not_a_store_with_no_organization(error):
    """Read as "in no organization", a busy database would lift the organization's limit."""
    store = _store(
        get_organization_membership=error,
        get_organization_spend_scope=error,
        clear_organization_member_removal=error,
    )

    with pytest.raises(type(error)):
        await organizations.spend_scope_of(store, "u1")
    with pytest.raises(type(error)):
        await organizations.membership_of(store, "u1")
    with pytest.raises(type(error)):
        await organizations.forget_removal(store, "u1")
