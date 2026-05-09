"""Tests for role-rank ordering and USER_CONCURRENCY_LIMITS in settings.

``trial`` ranks below ``free`` (probationary). Existing role semantics
must continue to hold.
"""

from serving.config.settings import (
    ROLE_RANK,
    VALID_ROLES,
    has_role,
)


def test_role_rank_contains_all_five_roles():
    assert set(ROLE_RANK) == {"trial", "free", "pro", "internal", "admin"}


def test_role_rank_ordering_is_strictly_ascending():
    # trial < free < pro < internal < admin
    assert (
        ROLE_RANK["trial"]
        < ROLE_RANK["free"]
        < ROLE_RANK["pro"]
        < ROLE_RANK["internal"]
        < ROLE_RANK["admin"]
    )


def test_valid_roles_matches_role_rank():
    assert frozenset({"trial", "free", "pro", "internal", "admin"}) == VALID_ROLES


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


def test_trial_below_free():
    # trial does NOT satisfy free (trial is probationary, ranks below free)
    assert has_role("trial", "free") is False
    # trial satisfies trial
    assert has_role("trial", "trial") is True
    # free satisfies trial
    assert has_role("free", "trial") is True
