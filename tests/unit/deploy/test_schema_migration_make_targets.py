"""The make recipes that start a database stack apply the schema migrations first.

The migrations run as a one-off container of the backend's own service, so they
use the image the backend is about to start, and never as a dependency of it:
restoring the previous image must not run that image's migrations.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[3]
MIGRATE = "run --rm --no-deps backend python -m migrations upgrade"
# What the migrations need running first: the database, started if it is not,
# never recreated.
DATABASE = "up -d --no-recreate --wait postgres"


def _compose_lines(target: str) -> list[str]:
    env = {k: v for k, v in os.environ.items() if k != "DISTRIBUTION"}
    proc = subprocess.run(
        ["make", "--no-print-directory", "-n", target, "DISTRIBUTION=none"],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    return [line for line in proc.stdout.splitlines() if "docker compose" in line]


@pytest.mark.parametrize("target", ["up", "build", "migrate"])
def test_a_database_stack_migrates_before_anything_starts(target):
    lines = _compose_lines(target)
    migrations = [i for i, line in enumerate(lines) if line.endswith(MIGRATE)]
    starts = [i for i, line in enumerate(lines) if " up -d" in line and not line.endswith(DATABASE)]
    assert len(migrations) == 1, lines
    assert all(i > migrations[0] for i in starts), lines


@pytest.mark.parametrize("target", ["up", "build", "migrate"])
def test_the_migration_neither_starts_nor_recreates_the_database(target):
    """`compose run` without --no-deps brings the backend's dependencies up itself.

    It recreated Postgres whenever its configuration had changed, under the
    backend still serving, before a migration that might then fail. The
    database is started beforehand instead, only if it is not running.
    """
    lines = _compose_lines(target)
    (migration,) = [i for i, line in enumerate(lines) if "migrations upgrade" in line]

    assert "--no-deps" in lines[migration].split(), lines[migration]
    assert [i for i, line in enumerate(lines) if line.endswith(DATABASE)] == [migration - 1]


def test_a_restart_migrates_nothing():
    assert not any("migrations" in line for line in _compose_lines("restart"))


def test_no_service_runs_or_waits_for_the_migrations():
    services = yaml.safe_load((REPO / "deploy/docker/docker-compose.yml").read_text())["services"]
    for name, service in services.items():
        assert "migrations" not in str(service.get("command", "")), name
        assert "migrat" not in str(service.get("depends_on", {})), name
