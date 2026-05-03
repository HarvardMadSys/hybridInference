"""Unit tests for the signup domain allowlist policy.

These exercise ``serving.auth.signup_policy`` against a fake operational
store that mirrors the PostgreSQL match semantics:

- Exact match against rows where ``is_wildcard=False``.
- Wildcard suffix match against rows where ``is_wildcard=True`` — walks
  parent labels, matches one or more subdomain levels, but does NOT
  match the bare suffix.
"""

from __future__ import annotations

import pytest

from serving.auth.signup_policy import (
    allowlist_is_empty,
    invalidate_allowlist_cache,
    is_domain_allowed,
)


class _FakeStore:
    """Minimal in-memory store implementing the allowlist contract."""

    def __init__(self, rows: list[tuple[str, bool]] | None = None) -> None:
        # Stored in normalized form: lowercase, no leading "*.".
        self.rows: set[tuple[str, bool]] = set(rows or [])
        self.is_empty_calls = 0

    async def signup_allowlist_is_empty(self) -> bool:
        self.is_empty_calls += 1
        return not self.rows

    async def is_signup_domain_allowed(self, email: str) -> bool:
        if "@" not in email:
            return False
        domain = email.rsplit("@", 1)[1].strip().lower()
        if not domain:
            return False
        if (domain, False) in self.rows:
            return True
        parts = domain.split(".")
        for i in range(1, len(parts) - 1):
            suffix = ".".join(parts[i:])
            if (suffix, True) in self.rows:
                return True
        return False


@pytest.fixture(autouse=True)
def _clear_cache():
    """Reset the in-process allowlist-empty cache between tests."""
    invalidate_allowlist_cache()
    yield
    invalidate_allowlist_cache()


# ---------------------------------------------------------------------------
# is_domain_allowed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exact_match_case_insensitive():
    """Exact rows ignore case and surrounding whitespace on the email side."""
    store = _FakeStore([("acme.com", False)])
    assert await is_domain_allowed("alice@ACME.com", store) is True
    assert await is_domain_allowed("alice@  acme.com  ", store) is True


@pytest.mark.asyncio
async def test_wildcard_matches_one_and_many_levels():
    """A *.acme.com wildcard matches a.acme.com and a.b.acme.com."""
    store = _FakeStore([("acme.com", True)])
    assert await is_domain_allowed("alice@a.acme.com", store) is True
    assert await is_domain_allowed("alice@a.b.acme.com", store) is True


@pytest.mark.asyncio
async def test_wildcard_does_not_match_bare_suffix():
    """*.acme.com must NOT match acme.com itself — admin must add it explicitly."""
    store = _FakeStore([("acme.com", True)])
    assert await is_domain_allowed("alice@acme.com", store) is False


@pytest.mark.asyncio
async def test_no_match_returns_false():
    """A domain not present in the allowlist returns False."""
    store = _FakeStore([("acme.com", False), ("foo.io", True)])
    assert await is_domain_allowed("alice@evil.example", store) is False
    assert await is_domain_allowed("alice@notacme.com", store) is False


@pytest.mark.asyncio
async def test_empty_list_returns_false():
    """An empty allowlist always returns False — caller treats empty as allow."""
    store = _FakeStore([])
    assert await is_domain_allowed("alice@anything.com", store) is False


@pytest.mark.asyncio
async def test_malformed_email_returns_false():
    """Email without @ or with empty domain returns False."""
    store = _FakeStore([("acme.com", False)])
    assert await is_domain_allowed("not-an-email", store) is False
    assert await is_domain_allowed("alice@", store) is False
    assert await is_domain_allowed("@acme.com", store) is True  # domain side OK
    assert await is_domain_allowed("", store) is False


@pytest.mark.asyncio
async def test_exact_and_wildcard_coexist():
    """Same suffix can be both exact and wildcard simultaneously."""
    store = _FakeStore([("acme.com", False), ("acme.com", True)])
    assert await is_domain_allowed("alice@acme.com", store) is True
    assert await is_domain_allowed("alice@x.acme.com", store) is True


# ---------------------------------------------------------------------------
# allowlist_is_empty (TTL cache behavior)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_allowlist_is_empty_caches_within_ttl():
    """Repeated calls within TTL don't hit the store again."""
    store = _FakeStore([])
    first = await allowlist_is_empty(store)
    second = await allowlist_is_empty(store)
    assert first is True
    assert second is True
    assert store.is_empty_calls == 1


@pytest.mark.asyncio
async def test_invalidate_drops_cache():
    """invalidate_allowlist_cache forces a refresh on the next call."""
    store = _FakeStore([])
    await allowlist_is_empty(store)
    invalidate_allowlist_cache()
    await allowlist_is_empty(store)
    assert store.is_empty_calls == 2


@pytest.mark.asyncio
async def test_allowlist_is_empty_reflects_population():
    """After invalidation, a populated store reports not-empty."""
    store = _FakeStore([("acme.com", False)])
    invalidate_allowlist_cache()
    assert await allowlist_is_empty(store) is False
