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
    from serving.config.distribution import get_distribution_config
    from serving.config.settings import get_settings

    get_settings.cache_clear()
    get_distribution_config.cache_clear()
    yield
    get_settings.cache_clear()
    get_distribution_config.cache_clear()


@pytest.fixture(autouse=True)
def _reset_runtime_settings_singleton():
    """Reset the global ``RuntimeSettings`` singleton between tests.

    ``bootstrap.initialize()`` warms the singleton from the (possibly
    mocked) operational store; without this reset, a later test calling
    ``is_user_auth_enabled()`` reads stale Mock values from the previous
    test and 401s on requests that should be auth-disabled.
    """
    import serving.config.runtime_settings as rs_mod

    rs_mod._runtime_settings = None
    yield
    rs_mod._runtime_settings = None


@pytest.fixture(autouse=True)
def _reset_dynamic_keys_registry():
    """Clear the in-process provider-key adapter registry between tests.

    ``serving.adapters.dynamic_keys`` keeps a process-global registry of
    adapters and known providers populated during model registration.
    Tests that call ``bootstrap.initialize()`` or otherwise build
    adapters would otherwise leak entries between cases.
    """
    try:
        from serving.adapters import dynamic_keys
    except ImportError:
        yield
        return

    dynamic_keys.reset()
    yield
    dynamic_keys.reset()
