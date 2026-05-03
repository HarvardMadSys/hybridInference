"""Top-level shared pytest fixtures.

These fixtures apply to every test directory under ``test/`` (servers,
integration, unit, etc.).  Subdirectory ``conftest.py`` files extend
these without re-declaring them.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _reset_settings_cache():
    """Invalidate the cached ``Settings`` instance before/after each test.

    Several tests use ``monkeypatch.setenv`` to override settings-driven
    env vars (``ADMIN_TOKEN``, ``USER_AUTH_ENABLED``,
    ``SIGNUP_REQUIRE_EMAIL_VERIFICATION``, etc.).  Because
    ``serving.config.settings.get_settings`` is ``lru_cache``-d at the
    module level, the cached instance won't see per-test overrides
    unless we explicitly invalidate it.  Clearing here ensures the next
    call to ``get_settings()`` (e.g. inside ``verify_admin_token`` or
    ``is_user_auth_enabled``) reflects the current process environment.
    """
    from serving.config.settings import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
