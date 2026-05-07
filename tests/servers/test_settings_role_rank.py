"""Tests for role-rank ordering and USER_CONCURRENCY_LIMITS in settings.

Adding ``pro`` between ``free`` and ``internal`` must not break
``has_role`` semantics for existing roles.
"""

from serving.config.settings import (
    ROLE_RANK,
    VALID_ROLES,
    has_role,
)


def test_role_rank_contains_all_four_roles():
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
    # pro satisfies free (any role >= rank 0 passes free)
    assert has_role("pro", "free") is True
