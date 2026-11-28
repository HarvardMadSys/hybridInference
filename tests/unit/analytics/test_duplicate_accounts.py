"""Unit tests for the duplicate-account scan's pure part.

``DuplicateAccountScan`` takes the rows the gather's queries return as plain
tuples, so these drive every kind of evidence, the limits on shared
infrastructure and the grouping without a database. The SQL itself is covered
by ``tests/integration/test_duplicate_accounts_postgres.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from itertools import pairwise
from typing import Any

import pytest

from serving.analytics import duplicate_accounts as dup
from serving.analytics.duplicate_accounts import (
    AccountRecord,
    DuplicateAccountScan,
    address_key,
    cosine_similarity,
    weekly_profile,
)

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
SINCE = NOW - timedelta(days=14)
HOME = "198.51.100.7"
OFFICE = "192.0.2.10"


def _accounts(*user_ids: str, **overrides: AccountRecord) -> dict[str, AccountRecord]:
    accounts = {
        user_id: AccountRecord(
            user_id, email=f"{user_id}@example.org", created_at=NOW - timedelta(days=30)
        )
        for user_id in user_ids
    }
    accounts.update(overrides)
    return accounts


def _report(scan: DuplicateAccountScan, **kwargs: Any) -> dict[str, Any]:
    return scan.report(days=14, since=SINCE, **kwargs)


def _groups(scan: DuplicateAccountScan, **kwargs: Any) -> list[list[str]]:
    return [group["user_ids"] for group in _report(scan, **kwargs)["groups"]]


def _kinds(scan: DuplicateAccountScan, a: str, b: str) -> set[str]:
    return set(scan.pairs.get((min(a, b), max(a, b)), {}))


def _hours(user_id: str, address: str | None, *offsets: int) -> list[tuple[Any, ...]]:
    """One request row per offset, in the UTC hour that many hours before NOW."""
    return [
        (user_id, address, (NOW - timedelta(hours=offset)).replace(tzinfo=None), 3)
        for offset in offsets
    ]


# --- addresses ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "key"),
    [
        ("198.51.100.7", "198.51.100.7"),
        (" 198.51.100.7 ", "198.51.100.7"),
        ("::ffff:198.51.100.7", "198.51.100.7"),
        ("198.51.100.7, 10.0.0.1", "198.51.100.7"),
        ("2a00:1450:4001:81c::200e", "2a00:1450:4001:81c::/64"),
        ("2a00:1450:4001:81c:aaaa:bbbb:cccc:dddd", "2a00:1450:4001:81c::/64"),
        # Recorded only for an authorized direct client network, so it counts.
        ("10.20.30.40", "10.20.30.40"),
        ("unknown", None),
        ("", None),
        (None, None),
        (42, None),
        ("127.0.0.1", None),
        ("::1", None),
        ("169.254.10.1", None),
        ("fe80::1%eth0", None),
        ("0.0.0.0", None),
        ("224.0.0.1", None),
        ("240.0.0.1", None),
    ],
)
def test_address_key(raw: object, key: str | None) -> None:
    assert address_key(raw) == key


def test_a_hosts_rotating_ipv6_addresses_are_one_address() -> None:
    scan = DuplicateAccountScan(
        _accounts("a", "b"),
        logins=[("a", "2a00:1450:4001:81c::1", 1), ("b", "2a00:1450:4001:81c::2", 1)],
        requests=[("a", "2a00:1450:4001:81c::3", 5), ("b", "2a00:1450:4001:81c::4", 5)],
    )

    assert _kinds(scan, "a", "b") == {"login_ip", "request_ip"}
    assert _groups(scan) == [["a", "b"]]


# --- registering close together --------------------------------------------------


def test_registering_from_one_address_within_the_hour_is_a_burst() -> None:
    scan = DuplicateAccountScan(
        _accounts("a", "b"),
        signups=[("a", HOME, NOW), ("b", HOME, NOW + timedelta(minutes=4))],
    )

    report = _report(scan)

    assert [group["user_ids"] for group in report["groups"]] == [["a", "b"]]
    group = report["groups"][0]
    assert group["score"] == 6
    assert group["signals"] == ["signup_burst", "signup_ip"]
    [link] = group["links"]
    assert link["user_ids"] == ["a", "b"]
    assert link["weight"] == 6
    assert [item["kind"] for item in link["evidence"]] == ["signup_burst", "signup_ip"]
    assert link["evidence"][0] == {
        "kind": "signup_burst",
        "weight": 3,
        "addresses": [HOME],
        "detail": f"Registered 4 minutes apart from {HOME}",
    }


def test_registrations_far_apart_share_only_the_address() -> None:
    scan = DuplicateAccountScan(
        _accounts("a", "b"),
        signups=[("a", HOME, NOW), ("b", HOME, NOW + timedelta(minutes=61))],
    )

    assert _kinds(scan, "a", "b") == {"signup_ip"}
    assert _groups(scan) == []


def test_a_burst_keeps_the_closest_gap_in_its_wording() -> None:
    scan = DuplicateAccountScan(
        _accounts("a", "b"),
        signups=[("a", HOME, NOW), ("b", HOME, NOW + timedelta(seconds=20))],
    )

    [link] = _report(scan)["groups"][0]["links"]

    assert link["evidence"][0]["detail"] == f"Registered less than a minute apart from {HOME}"


# --- an address alone links nobody ------------------------------------------------


@pytest.mark.parametrize(
    "observed",
    [
        {"logins": [("a", OFFICE, 2), ("b", OFFICE, 1)]},
        {"requests": [("a", OFFICE, 9), ("b", OFFICE, 9)]},
        {"signups": [("a", OFFICE, NOW), ("b", OFFICE, NOW + timedelta(days=3))]},
        {"requests": [("a", OFFICE, 9)], "logins": [("b", OFFICE, 1)]},
    ],
)
def test_one_kind_of_address_evidence_alone_makes_no_group(observed: dict[str, Any]) -> None:
    scan = DuplicateAccountScan(_accounts("a", "b"), **observed)

    assert scan.pairs, "the accounts are linked..."
    assert _groups(scan) == [], "...but not merged"


def test_signing_in_and_sending_requests_from_one_address_is_a_group() -> None:
    scan = DuplicateAccountScan(
        _accounts("a", "b"),
        logins=[("a", OFFICE, 2), ("b", OFFICE, 1)],
        requests=[("a", OFFICE, 9), ("b", OFFICE, 9)],
    )

    assert _kinds(scan, "a", "b") == {"login_ip", "request_ip"}
    assert _groups(scan) == [["a", "b"]]


def test_one_account_with_a_single_request_from_an_address_is_not_linked_by_it() -> None:
    scan = DuplicateAccountScan(
        _accounts("a", "b"),
        requests=[("a", OFFICE, 9), ("b", OFFICE, 1)],
    )

    assert _kinds(scan, "a", "b") == set()


def test_api_traffic_from_where_the_other_signs_in_is_cross_evidence() -> None:
    scan = DuplicateAccountScan(
        _accounts("a", "b"),
        requests=[("a", OFFICE, 9)],
        logins=[("b", OFFICE, 1)],
        signups=[("b", HOME, NOW)],
    )

    assert _kinds(scan, "a", "b") == {"cross_ip"}
    clue = scan.pairs[("a", "b")]["cross_ip"]
    assert dup.describe(clue) == (
        f"One sent API requests from {OFFICE}, where the other registered or signed in"
    )


def test_both_sending_requests_is_not_also_cross_evidence() -> None:
    scan = DuplicateAccountScan(
        _accounts("a", "b"),
        requests=[("a", OFFICE, 9), ("b", OFFICE, 9)],
        logins=[("b", OFFICE, 1)],
    )

    assert _kinds(scan, "a", "b") == {"request_ip"}


def test_each_kind_counts_once_however_many_addresses_carry_it() -> None:
    addresses = ["192.0.2.1", "192.0.2.2", "192.0.2.3", "192.0.2.4"]
    scan = DuplicateAccountScan(
        _accounts("a", "b"),
        requests=[(user, address, 5) for user in "ab" for address in addresses],
    )

    assert scan.pairs[("a", "b")]["request_ip"].keys == set(addresses)
    assert _groups(scan) == [], "four addresses are one fact about the pair: weight 2"
    assert dup.describe(scan.pairs[("a", "b")]["request_ip"]) == (
        "Both sent API requests from 192.0.2.1, 192.0.2.2, 192.0.2.3 and 1 more"
    )


# --- shared infrastructure --------------------------------------------------------


def test_an_address_more_accounts_share_than_the_limit_links_nobody() -> None:
    users = [f"u{i}" for i in range(dup.ADDRESS_FANOUT_MAX + 1)]
    scan = DuplicateAccountScan(
        _accounts(*users),
        logins=[(user, OFFICE, 1) for user in users],
        requests=[(user, OFFICE, 9) for user in users],
    )

    assert scan.pairs == {}
    assert scan.shared_addresses == 1
    assert _report(scan)["shared_addresses"] == 1


def test_the_operators_own_accounts_count_towards_an_addresss_fanout() -> None:
    customers = [f"u{i}" for i in range(dup.ADDRESS_FANOUT_MAX)]
    accounts = _accounts(*customers, staff=AccountRecord("staff", role="admin"))
    scan = DuplicateAccountScan(
        accounts,
        logins=[(user, OFFICE, 1) for user in [*customers, "staff"]],
        requests=[(user, OFFICE, 9) for user in [*customers, "staff"]],
    )

    assert scan.pairs == {}
    assert scan.shared_addresses == 1


def test_an_account_missing_from_users_counts_towards_fanout_but_is_never_linked() -> None:
    scan = DuplicateAccountScan(
        _accounts("a"),
        logins=[("a", OFFICE, 1), ("gone", OFFICE, 1)],
        requests=[("a", OFFICE, 9), ("gone", OFFICE, 9)],
    )

    assert scan.pairs == {}
    assert scan.seen == {"a"}


@pytest.mark.parametrize(
    "account",
    [
        AccountRecord("op", role="admin"),
        AccountRecord("op", role="internal"),
        AccountRecord("op", service_account=True),
    ],
)
def test_operator_and_service_accounts_are_never_linked(account: AccountRecord) -> None:
    scan = DuplicateAccountScan(
        _accounts("a", op=account),
        signups=[("a", HOME, NOW), ("op", HOME, NOW)],
        browsers=[("browser", "a", NOW), ("browser", "op", NOW)],
    )

    assert scan.pairs == {}
    assert _report(scan)["accounts_scanned"] == 1


def test_a_browser_with_more_accounts_than_the_limit_is_a_shared_computer() -> None:
    users = [f"u{i}" for i in range(dup.BROWSER_FANOUT_MAX + 1)]
    scan = DuplicateAccountScan(
        _accounts(*users),
        requests=[(user, None, 1) for user in users],
        browsers=[("kiosk", user, NOW) for user in users],
    )

    assert scan.pairs == {}
    assert scan.shared_browsers == 1


# --- identity -------------------------------------------------------------------


def test_accounts_signed_in_together_on_one_browser_are_a_group() -> None:
    scan = DuplicateAccountScan(
        _accounts("a", "b", "c"),
        requests=[("a", None, 4)],
        browsers=[
            ("browser", "a", NOW - timedelta(days=3)),
            ("browser", "b", NOW - timedelta(days=1)),
            ("other", "c", NOW),
        ],
    )

    report = _report(scan)

    assert [group["user_ids"] for group in report["groups"]] == [["a", "b"]]
    [link] = report["groups"][0]["links"]
    expected_day = (NOW - timedelta(days=1)).date().isoformat()
    assert link["evidence"] == [
        {
            "kind": "same_browser",
            "weight": 8,
            "addresses": [],
            "detail": f"Signed in together on one browser since {expected_day}",
        }
    ]


def test_a_mailbox_links_a_dormant_alias_of_an_active_account() -> None:
    scan = DuplicateAccountScan(
        _accounts("active", "dormant"),
        requests=[("active", HOME, 5)],
        mailboxes=[("alice@example.org", ["active", "dormant"])],
    )

    report = _report(scan)

    assert [group["user_ids"] for group in report["groups"]] == [["active", "dormant"]]
    [link] = report["groups"][0]["links"]
    assert link["evidence"][0]["detail"] == (
        "Their email addresses reach one mailbox, alice@example.org"
    )
    assert report["accounts_scanned"] == 1


def test_identity_evidence_needs_one_account_active_in_the_window() -> None:
    scan = DuplicateAccountScan(
        _accounts("a", "b", "c", "d"),
        browsers=[("browser", "a", NOW), ("browser", "b", NOW)],
        mailboxes=[("carol@example.org", ["c", "d"])],
    )

    assert scan.pairs == {}


def test_a_mailbox_holding_many_accounts_is_one_group_not_shared_infrastructure() -> None:
    users = [f"u{i:02d}" for i in range(dup.MAX_GROUP_SIZE + 5)]
    scan = DuplicateAccountScan(
        _accounts(*users),
        requests=[(users[0], None, 3)],
        mailboxes=[("farm@example.org", users)],
    )

    report = _report(scan)

    assert [group["user_ids"] for group in report["groups"]] == [sorted(users)]
    assert report["oversized"] == []


# --- time and behaviour -----------------------------------------------------------


def test_requests_from_one_address_in_the_same_hours_make_a_group() -> None:
    address = "2a00:1450:4001:81c::/64"
    scan = DuplicateAccountScan(
        _accounts("a", "b"),
        requests=[("a", "2a00:1450:4001:81c::1", 30), ("b", "2a00:1450:4001:81c::2", 30)],
    )
    assert sorted(scan.candidates) == ["a", "b"]

    scan.add_activity(
        hours=[
            *_hours("a", "2a00:1450:4001:81c::1", 1, 2, 30),
            *_hours("b", "2a00:1450:4001:81c::9", 1, 2, 50),
        ]
    )

    assert _kinds(scan, "a", "b") == {"request_ip", "co_active"}
    clue = scan.pairs[("a", "b")]["co_active"]
    assert clue.value == 2
    assert dup.describe(clue) == f"Sent requests from {address} in the same clock hour, 2 times"
    assert _groups(scan) == [["a", "b"]]


def test_one_hour_together_is_not_enough() -> None:
    scan = DuplicateAccountScan(
        _accounts("a", "b"), requests=[("a", OFFICE, 30), ("b", OFFICE, 30)]
    )

    scan.add_activity(hours=[*_hours("a", OFFICE, 1, 2), *_hours("b", OFFICE, 1, 3)])

    assert _kinds(scan, "a", "b") == {"request_ip"}


def test_hours_together_count_only_on_the_shared_address() -> None:
    scan = DuplicateAccountScan(
        _accounts("a", "b"), requests=[("a", OFFICE, 30), ("b", OFFICE, 30)]
    )

    scan.add_activity(
        hours=[*_hours("a", OFFICE, 1, 2), *_hours("b", "203.0.113.5", 1, 2)],
    )

    assert "co_active" not in _kinds(scan, "a", "b")


def test_the_same_weekly_rhythm_strengthens_a_link() -> None:
    scan = DuplicateAccountScan(
        _accounts("a", "b", "c"),
        logins=[("a", OFFICE, 1), ("b", OFFICE, 1)],
    )
    # Same twelve hours, a week apart; c is active but linked to nobody.
    rhythm = [24 * 7 * week + hour for week in (0, 1) for hour in range(1, 7)]
    scan.add_activity(
        hours=[
            *_hours("a", HOME, *rhythm),
            *_hours("b", "203.0.113.5", *(offset + 24 * 7 * 2 for offset in rhythm)),
            *_hours("c", HOME, *rhythm),
        ]
    )

    assert _kinds(scan, "a", "b") == {"login_ip", "activity_profile"}
    assert scan.pairs[("a", "b")]["activity_profile"].value == 1.0
    assert "c" not in scan.candidates
    assert _groups(scan) == [["a", "b"]]


def test_a_rhythm_needs_enough_active_hours() -> None:
    scan = DuplicateAccountScan(_accounts("a", "b"), logins=[("a", OFFICE, 1), ("b", OFFICE, 1)])
    few = list(range(1, dup.PROFILE_MIN_ACTIVE_HOURS))

    scan.add_activity(hours=[*_hours("a", HOME, *few), *_hours("b", HOME, *few)])

    assert _kinds(scan, "a", "b") == {"login_ip"}


def test_an_uncommon_client_strengthens_a_link_and_a_common_one_does_not() -> None:
    scan = DuplicateAccountScan(
        _accounts("a", "b", "x", "y"),
        logins=[("a", OFFICE, 1), ("b", OFFICE, 1)],
    )
    common = "python-requests/2.32.3"
    scan.add_activity(
        clients=[
            ("a", "acme-farm/0.1", 5),
            ("b", "acme-farm/0.1", 2),
            *[(user, common, 9) for user in ("a", "b", "x", "y")],
        ]
    )

    assert _kinds(scan, "a", "b") == {"login_ip", "same_client"}
    assert scan.pairs[("a", "b")]["same_client"].keys == {"acme-farm/0.1"}
    assert _groups(scan) == [["a", "b"]]


def test_a_client_needs_two_requests_from_each_account() -> None:
    scan = DuplicateAccountScan(_accounts("a", "b"), logins=[("a", OFFICE, 1), ("b", OFFICE, 1)])

    scan.add_activity(clients=[("a", "acme-farm/0.1", 5), ("b", "acme-farm/0.1", 1)])

    assert _kinds(scan, "a", "b") == {"login_ip"}


def test_behaviour_alone_links_nobody() -> None:
    scan = DuplicateAccountScan(_accounts("a", "b"))
    rhythm = list(range(1, 20))

    scan.add_activity(
        hours=[*_hours("a", HOME, *rhythm), *_hours("b", OFFICE, *rhythm)],
        clients=[("a", "acme-farm/0.1", 5), ("b", "acme-farm/0.1", 5)],
    )

    assert scan.pairs == {}


def test_weekly_profile_and_cosine_similarity() -> None:
    monday_9 = datetime(2026, 11, 16, 9, 30, tzinfo=UTC)
    profile = weekly_profile(
        [monday_9, monday_9 + timedelta(days=7), monday_9 + timedelta(hours=1)]
    )

    assert len(profile) == 168
    assert profile[9] == 2
    assert profile[10] == 1
    assert cosine_similarity([1, 0], [1, 0]) == pytest.approx(1.0)
    assert cosine_similarity([1, 0], [0, 1]) == 0.0
    assert cosine_similarity([0, 0], [1, 1]) == 0.0


# --- grouping ---------------------------------------------------------------------


def test_a_weak_link_does_not_chain_a_third_account_in() -> None:
    scan = DuplicateAccountScan(
        _accounts("a", "b", "c"),
        signups=[("a", HOME, NOW), ("b", HOME, NOW + timedelta(minutes=5))],
        logins=[("b", OFFICE, 1), ("c", OFFICE, 1)],
    )

    assert _kinds(scan, "b", "c") == {"login_ip"}
    assert _groups(scan) == [["a", "b"]]
    [group] = _report(scan)["groups"]
    assert [link["user_ids"] for link in group["links"]] == [["a", "b"]]


def test_a_groups_score_sums_its_links_and_lists_them_heaviest_first() -> None:
    scan = DuplicateAccountScan(
        _accounts("a", "b", "c"),
        signups=[("a", HOME, NOW), ("b", HOME, NOW + timedelta(minutes=5))],
        logins=[("b", OFFICE, 1), ("c", OFFICE, 1)],
        requests=[("b", OFFICE, 5), ("c", OFFICE, 5)],
        browsers=[("browser", "a", NOW), ("browser", "c", NOW)],
    )

    [group] = _report(scan)["groups"]

    assert group["user_ids"] == ["a", "b", "c"]
    assert [(link["user_ids"], link["weight"]) for link in group["links"]] == [
        (["a", "c"], 8),
        (["a", "b"], 6),
        (["b", "c"], 5),
    ]
    assert group["score"] == 19
    assert group["signals"] == [
        "same_browser",
        "signup_burst",
        "signup_ip",
        "login_ip",
        "request_ip",
    ]


def test_a_component_too_large_to_be_one_person_is_set_aside() -> None:
    # A campus: neighbours share an address each, signing in and sending
    # requests from it, and the pairs chain past the group limit.
    users = [f"u{i:02d}" for i in range(dup.MAX_GROUP_SIZE + 2)]
    logins: list[tuple[str, str, int]] = []
    requests: list[tuple[str, str, int]] = []
    for index, (first, second) in enumerate(pairwise(users)):
        address = f"192.0.2.{index + 1}"
        for user in (first, second):
            logins.append((user, address, 1))
            requests.append((user, address, 5))
    scan = DuplicateAccountScan(_accounts(*users), logins=logins, requests=requests)

    report = _report(scan)

    assert report["groups"] == []
    assert report["oversized"] == [{"user_ids": users}]


def test_a_dense_core_inside_an_oversized_component_is_still_reported() -> None:
    users = [f"u{i:02d}" for i in range(dup.MAX_GROUP_SIZE + 2)]
    logins: list[tuple[str, str, int]] = []
    requests: list[tuple[str, str, int]] = []
    for index, (first, second) in enumerate(pairwise(users)):
        address = f"192.0.2.{index + 1}"
        for user in (first, second):
            logins.append((user, address, 1))
            requests.append((user, address, 5))
    scan = DuplicateAccountScan(
        _accounts(*users),
        logins=logins,
        requests=requests,
        signups=[("u00", HOME, NOW), ("u01", HOME, NOW + timedelta(minutes=2))],
    )

    report = _report(scan)

    assert [group["user_ids"] for group in report["groups"]] == [["u00", "u01"]]
    assert report["oversized"] == []


# --- the report -------------------------------------------------------------------


def _three_pairs() -> DuplicateAccountScan:
    return DuplicateAccountScan(
        _accounts("a", "b", "c", "d", "e", "f"),
        signups=[("a", HOME, NOW), ("b", HOME, NOW + timedelta(minutes=5))],
        browsers=[("one", "c", NOW), ("one", "d", NOW)],
        logins=[("e", OFFICE, 1), ("f", OFFICE, 1)],
        requests=[("c", None, 1), ("e", OFFICE, 4), ("f", OFFICE, 4)],
    )


def test_groups_are_listed_highest_score_first() -> None:
    assert _groups(_three_pairs()) == [["c", "d"], ["a", "b"], ["e", "f"]]


def test_min_score_and_limit_bound_the_listing() -> None:
    scan = _three_pairs()

    assert _groups(scan, min_score=6) == [["c", "d"], ["a", "b"]]
    limited = _report(scan, limit=1)
    assert [group["user_ids"] for group in limited["groups"]] == [["c", "d"]]
    assert limited["total_groups"] == 3


def test_the_report_describes_the_window_and_its_members() -> None:
    older = AccountRecord(
        "b",
        email="bob@example.org",
        user_name="Bob",
        role="pro",
        status="suspended",
        created_at=NOW - timedelta(days=90),
    )
    scan = DuplicateAccountScan(
        _accounts("a", b=older),
        signups=[("a", HOME, NOW), ("b", HOME, NOW + timedelta(minutes=5))],
        requests=[("a", HOME, 3), ("a", None, 2)],
    )

    report = _report(scan)

    assert report["days"] == 14
    assert report["since"] == SINCE
    assert report["accounts_scanned"] == 2
    assert report["total_groups"] == 1
    # Oldest account first.
    assert report["groups"][0]["members"] == [
        {
            "user_id": "b",
            "email": "bob@example.org",
            "login_name": None,
            "phone": None,
            "user_name": "Bob",
            "role": "pro",
            "status": "suspended",
            "created_at": NOW - timedelta(days=90),
            "requests": 0,
        },
        {
            "user_id": "a",
            "email": "a@example.org",
            "login_name": None,
            "phone": None,
            "user_name": None,
            "role": "free",
            "status": "active",
            "created_at": NOW - timedelta(days=30),
            "requests": 5,
        },
    ]


def test_an_empty_window_reports_nothing() -> None:
    report = _report(DuplicateAccountScan({}))

    assert report == {
        "days": 14,
        "since": SINCE,
        "accounts_scanned": 0,
        "shared_addresses": 0,
        "shared_browsers": 0,
        "total_groups": 0,
        "groups": [],
        "oversized": [],
    }


def test_the_weights_keep_every_address_kind_below_the_merge_threshold() -> None:
    for kind in dup.ADDRESS_KINDS:
        assert dup.SIGNAL_WEIGHTS[kind] < dup.MERGE_THRESHOLD, kind
    for kind in dup.IDENTITY_KINDS:
        assert dup.SIGNAL_WEIGHTS[kind] >= dup.MERGE_THRESHOLD, kind
    assert dup.SIGNAL_WEIGHTS["activity_profile"] + dup.SIGNAL_WEIGHTS["same_client"] < (
        dup.MERGE_THRESHOLD
    ), "behaviour alone must never merge"
