"""Who may reach other owners' stored batches and files by id (#46).

While user auth is disabled, every caller is one anonymous context holding the
admin role, which opens the API to whoever reaches it. That role must not also
open the batches and files stored under real accounts.
"""

from __future__ import annotations

import pytest

from serving.servers.auth import _anonymous_context, reads_every_owner
from serving.servers.routers import batches, files


def _key_context(role: str) -> dict:
    return {"user_id": f"u-{role}", "role": role, "authenticated": True}


@pytest.mark.parametrize(
    ("context", "expected"),
    [
        (_key_context("admin"), True),
        (_key_context("internal"), False),
        (_key_context("free"), False),
        (_anonymous_context(), False),
        # A context that does not say it authenticated is not trusted as admin.
        ({"user_id": "u-admin", "role": "admin"}, False),
    ],
)
def test_only_an_authenticated_admin_reads_every_owner(context, expected):
    assert reads_every_owner(context) is expected


@pytest.mark.parametrize("router", [batches, files], ids=["batches", "files"])
def test_lookups_are_scoped_to_the_anonymous_owner(router):
    assert router._lookup_scope(_anonymous_context()) == "anonymous"


@pytest.mark.parametrize("router", [batches, files], ids=["batches", "files"])
def test_an_authenticated_admin_still_reaches_every_owner(router):
    assert router._lookup_scope(_key_context("admin")) is None
    assert router._lookup_scope(_key_context("pro")) == "u-pro"


def test_the_anonymous_context_keeps_its_admin_role():
    """Model access, the batch surface and the like still open for it."""
    context = _anonymous_context()
    assert (context["role"], context["is_admin"], context["authenticated"]) == (
        "admin",
        True,
        False,
    )
