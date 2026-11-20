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


# -- a member's row -------------------------------------------------------------------


def _member_row(**fields):
    return {
        "user_id": "u-1",
        "role": "member",
        "account_role": "free",
        "status": "active",
        "joined_at": datetime(2026, 11, 1, tzinfo=UTC),
        **fields,
    }


@pytest.mark.parametrize(
    ("fields", "protected"),
    [
        ({}, False),
        # An administrator of the organization, and one of the platform.
        ({"role": "admin"}, True),
        ({"account_role": "admin"}, True),
    ],
)
def test_a_member_row_says_whether_the_organization_manages_the_account(fields, protected):
    view = organizations.member_view(_member_row(**fields), {})

    assert view["protected"] is protected


# -- the series of a day by its hours ------------------------------------------------


@pytest.mark.parametrize(
    ("now", "hours"),
    [
        # The first hour of the day has begun, and no other.
        (datetime(2026, 11, 20, 0, 0, tzinfo=UTC), 1),
        (datetime(2026, 11, 20, 9, 30, tzinfo=UTC), 10),
        (datetime(2026, 11, 20, 23, 59, 59, tzinfo=UTC), 24),
        # A day that is over has all of its hours, and one to come has none.
        (datetime(2026, 11, 21, 3, 0, tzinfo=UTC), 24),
        (datetime(2026, 11, 19, 22, 0, tzinfo=UTC), 0),
    ],
)
def test_a_day_by_its_hours_has_a_point_for_each_hour_that_has_begun(now, hours):
    day = datetime(2026, 11, 20, tzinfo=UTC).date()
    usage = {
        "buckets": [
            {"bucket": datetime(2026, 11, 20, 8), "requests": 3, "total_tokens": 90, "members": 2}
        ],
        "spend_days": [],
    }

    series = organizations._series(usage, day, day, hourly=True, now=now)

    assert [point["bucket"].hour for point in series] == list(range(hours))
    # A quiet hour that has begun is a point of zero; the logged one has its figures.
    assert [point["requests"] for point in series] == [
        3 if hour == 8 else 0 for hour in range(hours)
    ]


def test_a_period_by_its_days_has_a_point_for_each_day():
    first, last = (
        datetime(2026, 11, 14, tzinfo=UTC).date(),
        datetime(2026, 11, 20, tzinfo=UTC).date(),
    )

    series = organizations._series({"buckets": [], "spend_days": []}, first, last, hourly=False)

    # Today is one of them, as far as it has come.
    assert [point["bucket"].day for point in series] == [14, 15, 16, 17, 18, 19, 20]
