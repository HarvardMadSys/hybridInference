"""Per-migration roundtrip — confirm baseline produces a usable schema.

Marked with the ``dbtest`` marker so it runs against a real Postgres test
DB. Skipped automatically when no DB is available.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio

pytestmark = pytest.mark.dbtest

REPO_ROOT = Path(__file__).resolve().parents[3]


def _alembic_env() -> dict[str, str]:
    """Return an env dict pointing alembic at the test DB."""
    env = os.environ.copy()
    env.setdefault("DB_HOST", os.environ.get("DB_HOST", "localhost"))
    env.setdefault("DB_PORT", os.environ.get("DB_PORT", "5432"))
    env.setdefault("DB_USER", os.environ.get("DB_USER", "postgres"))
    env.setdefault("DB_PASSWORD", os.environ.get("DB_PASSWORD", "postgres"))
    env.setdefault("DB_NAME", os.environ.get("DB_NAME", "freeinference_test_db"))
    return env


def _run_alembic(*args: str) -> subprocess.CompletedProcess:
    """Run ``uv run alembic <args>`` from the repo root with test DB env."""
    return subprocess.run(
        ["uv", "run", "alembic", *args],
        env=_alembic_env(),
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


async def _connect():
    """Connect to the test DB; skip if unavailable."""
    env = _alembic_env()
    try:
        return await asyncpg.connect(
            host=env["DB_HOST"],
            port=int(env["DB_PORT"]),
            user=env["DB_USER"],
            password=env["DB_PASSWORD"],
            database=env["DB_NAME"],
            timeout=2,
        )
    except Exception as exc:
        pytest.skip(f"PostgreSQL test database not available: {exc}")


@pytest_asyncio.fixture
async def fresh_db():
    """Drop all public-schema objects and yield a connection.

    The autouse ``auth_test_env`` fixture in ``tests/servers/conftest.py``
    only runs for tests under ``tests/servers``; here we resolve the DB
    config directly from env vars (matching CI's ``DB_*`` setup).
    """
    conn = await _connect()
    db_name = await conn.fetchval("SELECT current_database()")
    if "_test_" not in (db_name or ""):
        await conn.close()
        pytest.fail(
            f"SAFETY: refusing to drop schema in database '{db_name}'; name must contain '_test_'."
        )
    # Wipe everything so we test a fresh-from-empty migration apply.
    await conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
    await conn.execute("CREATE SCHEMA public")
    await conn.close()
    try:
        yield
    finally:
        # Tests reseed schema on next run; nothing to do here.
        pass


async def test_baseline_upgrade_creates_all_tables(fresh_db):
    """``alembic upgrade head`` against a fresh DB creates every table."""
    result = _run_alembic("upgrade", "head")
    assert result.returncode == 0, (
        f"alembic upgrade head failed:\nstdout={result.stdout}\nstderr={result.stderr}"
    )

    expected_tables = {
        "alembic_version",
        "admin_audit_log",
        "api_keys",
        "api_logs",
        "api_stats_hourly",
        "auth_sessions",
        "email_broadcast_recipients",
        "email_broadcasts",
        "email_verification_tokens",
        "password_reset_tokens",
        "provider_hourly_stats",
        "signup_allowed_domains",
        "site_settings",
        "user_daily_cost",
        "users",
    }

    conn = await _connect()
    try:
        rows = await conn.fetch("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
    finally:
        await conn.close()
    actual = {row["tablename"] for row in rows}
    missing = expected_tables - actual
    assert not missing, f"Missing tables after baseline: {missing}"


async def test_baseline_stamps_correct_revision(fresh_db):
    """After upgrade, ``alembic current`` should report the baseline revision."""
    result = _run_alembic("upgrade", "head")
    assert result.returncode == 0, result.stderr

    conn = await _connect()
    try:
        version = await conn.fetchval("SELECT version_num FROM alembic_version")
    finally:
        await conn.close()
    assert version == "0001_baseline", f"Expected 0001_baseline, got {version!r}"
