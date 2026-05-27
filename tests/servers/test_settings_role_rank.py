"""Tests for role-rank ordering semantics in settings."""

from serving.config.settings import (
    ROLE_RANK,
    VALID_ROLES,
    has_role,
)


def test_role_rank_contains_supported_roles():
    assert set(ROLE_RANK) == {"free", "pro", "internal", "admin"}


def test_role_rank_ordering_is_strictly_ascending():
    # free < pro < internal < admin
    assert ROLE_RANK["free"] < ROLE_RANK["pro"] < ROLE_RANK["internal"] < ROLE_RANK["admin"]


def test_valid_roles_matches_role_rank():
    assert frozenset({"free", "pro", "internal", "admin"}) == VALID_ROLES


def test_has_role_existing_semantics_preserved():
    # admin still satisfies internal
    assert has_role("admin", "internal") is True
    # internal still satisfies internal
    assert has_role("internal", "internal") is True
    # free does not satisfy internal
    assert has_role("free", "internal") is False
    # pro does NOT satisfy internal (pro < internal in rank)
    assert has_role("pro", "internal") is False
    # admin satisfies admin
    assert has_role("admin", "admin") is True
    # pro satisfies free (any role at/above free in rank passes free)
    assert has_role("pro", "free") is True


def test_unknown_role_fails_closed_against_free_and_above():
    assert has_role("legacy-trial", "free") is True
    assert has_role("legacy-trial", "pro") is False
