"""The served-identity entitlement must come from the key, not the request.

Issue #1504 wants the served identity on real workloads, but only for a
caller an operator has authorized. These tests pin the source of that
authorization: the presented key's own metadata, carried into the request
context by the auth path. A request header cannot grant it, an agent grant
cannot use it, and an ordinary key does not have it.
"""

from __future__ import annotations

from serving.servers.auth import _key_diagnostic_entitlements, _user_context

SERVED_IDENTITY = "served_identity"


def test_a_key_without_metadata_holds_no_entitlement() -> None:
    """The default is fail-closed: an ordinary key authorizes nothing."""
    assert _key_diagnostic_entitlements({}) == frozenset()
    assert _key_diagnostic_entitlements({"key_metadata": None}) == frozenset()
    assert _key_diagnostic_entitlements({"key_metadata": {}}) == frozenset()


def test_a_key_carrying_the_entitlement_holds_it() -> None:
    identity = {"key_metadata": {"diagnostic_entitlements": [SERVED_IDENTITY]}}
    assert _key_diagnostic_entitlements(identity) == frozenset({SERVED_IDENTITY})


def test_a_malformed_entitlement_grants_nothing() -> None:
    """A key's metadata is admin-set JSON; a wrong shape must not grant."""
    for raw in (
        "served_identity",
        {"diagnostic_entitlements": "served_identity"},
        {"diagnostic_entitlements": 7},
        {"diagnostic_entitlements": [1, 2, 3]},
        {"diagnostic_entitlements": ["future_unknown_entitlement"]},
        {"diagnostic_entitlements": None},
    ):
        assert _key_diagnostic_entitlements({"key_metadata": raw}) == frozenset()


def test_metadata_that_is_not_a_mapping_grants_nothing() -> None:
    assert _key_diagnostic_entitlements({"key_metadata": ["served_identity"]}) == frozenset()
    assert _key_diagnostic_entitlements({"key_metadata": "served_identity"}) == frozenset()


def test_the_entitlement_is_derived_only_from_the_presented_key() -> None:
    """The key's metadata is the only source; nothing else on the row counts."""
    assert _key_diagnostic_entitlements(
        {"key_metadata": {"diagnostic_entitlements": [SERVED_IDENTITY]}}
    ) == frozenset({SERVED_IDENTITY})
    # An unrelated metadata key does not grant the entitlement.
    assert (
        _key_diagnostic_entitlements({"key_metadata": {"entitlements": [SERVED_IDENTITY]}})
        == frozenset()
    )


def test_the_entitlement_reaches_the_request_context() -> None:
    """The entitlement the key holds is the entitlement the handler sees."""
    context = _user_context(
        {
            "user_id": "u1",
            "role": "free",
            "key_metadata": {"diagnostic_entitlements": [SERVED_IDENTITY]},
        },
        key_hash="k",
        api_key_id=1,
        quota_daily_cost_usd=10.0,
        spent_today_usd=0.0,
    )
    assert context["diagnostic_entitlements"] == frozenset({SERVED_IDENTITY})


def test_an_ordinary_key_carries_no_entitlement_in_the_context() -> None:
    """The default is fail-closed: no metadata, no entitlement, no header."""
    for identity in (
        {"user_id": "u1", "role": "free"},
        {"user_id": "u1", "role": "free", "key_metadata": {}},
    ):
        context = _user_context(
            identity,
            key_hash="k",
            api_key_id=1,
            quota_daily_cost_usd=10.0,
            spent_today_usd=0.0,
        )
        assert "diagnostic_entitlements" not in context
