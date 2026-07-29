"""Top-level shared pytest fixtures.

These fixtures apply to every test directory under ``test/`` (servers,
integration, unit, etc.).  Subdirectory ``conftest.py`` files extend
these without re-declaring them.
"""

from __future__ import annotations

import os
import sys

import pytest

# --- Never inherit whoever's .env is on this machine -------------------------
#
# ``bootstrap.initialize()`` calls ``load_dotenv()``, which searches upward from
# the working directory and injects what it finds into ``os.environ`` for the
# rest of the process. On a developer machine that resolves to a real
# deployment's file — including its credentials — so one test touching bootstrap
# silently rewrote settings for every later test in the same worker, and could
# hand live provider keys to tests that meant to use fakes. CI has no such file,
# which is why this only ever failed locally.
#
# Patched at conftest import time rather than in a fixture because a few test
# modules (tests/api/*) call load_dotenv() at module scope, which runs during
# collection — before any fixture. Tests that need a setting use monkeypatch;
# none should depend on a file outside the repository.
#
# The exception is the ``external`` tier, which exists to call live providers
# and reads its credentials from a `.env` on purpose. Running it is already an
# explicit opt-in (``-m external``), so opting back in here is too:
#
#     TESTS_ALLOW_DOTENV=1 uv run pytest -m external tests/api/
#
# The variable is deliberately not honoured from a `.env` file — that would be
# circular — so it has to be set on the command line, where it is visible.
_ALLOW_DOTENV = os.environ.get("TESTS_ALLOW_DOTENV", "").strip() not in ("", "0", "false")


def _no_dotenv(*args, **kwargs):
    """Stand in for ``dotenv.load_dotenv`` and load nothing."""
    return False


if not _ALLOW_DOTENV:
    try:
        import dotenv

        dotenv.load_dotenv = _no_dotenv
    except ImportError:  # pragma: no cover - dotenv is a hard dependency in dev
        pass

    # Belt and braces: any module that already bound the name keeps its own
    # reference, so replace it there too.
    _bootstrap = sys.modules.get("serving.servers.bootstrap")
    if _bootstrap is not None and hasattr(_bootstrap, "load_dotenv"):  # pragma: no cover
        _bootstrap.load_dotenv = _no_dotenv


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
