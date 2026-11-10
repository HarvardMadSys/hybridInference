"""Every supervisor lets the backend finish its shutdown before it kills it.

A stop sends SIGTERM and, after a grace period, SIGKILL: Docker after 10 s
unless the service sets ``stop_grace_period``, systemd after its stop timeout,
and the admin restart's own watchdog after ``WATCHDOG_SECONDS``. uvicorn waited
for every open request, and a stream outlasts all of them, so the kill came
before the lifespan shutdown that writes queued request-log rows and the costs
the spend ledger still holds. ``--timeout-graceful-shutdown`` bounds that wait;
the rest of the grace period is the shutdown's.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from serving.servers.restart import WATCHDOG_SECONDS

REPO = Path(__file__).resolve().parents[3]
DOCKER = REPO / "deploy" / "docker"
SYSTEMD_UNITS = sorted((REPO / "deploy" / "systemd").glob("*.service"))

# The lifespan shutdown's own flushes are bounded at 10 s each (bootstrap.py),
# and a normal one takes a second or two; leave it at least this much.
_SHUTDOWN_SECONDS = 20


def _drain_seconds(argv: list[str]) -> int:
    assert "--timeout-graceful-shutdown" in argv, argv
    return int(argv[argv.index("--timeout-graceful-shutdown") + 1])


def _dockerfile_cmd() -> list[str]:
    folded = (DOCKER / "Dockerfile.backend").read_text().replace("\\\n", " ")
    (cmd,) = [line for line in folded.splitlines() if line.startswith("CMD ")]
    return json.loads(cmd.removeprefix("CMD "))


def test_the_image_bounds_the_wait_for_open_requests() -> None:
    assert 0 < _drain_seconds(_dockerfile_cmd()) <= 10


def test_compose_gives_the_shutdown_the_rest_of_the_grace_period() -> None:
    backend = yaml.safe_load((DOCKER / "docker-compose.yml").read_text())["services"]["backend"]

    assert backend["stop_grace_period"] == "30s"
    assert 30 - _drain_seconds(_dockerfile_cmd()) >= _SHUTDOWN_SECONDS


def test_the_admin_restart_watchdog_outlasts_the_drain() -> None:
    """The watchdog's os._exit skips the shutdown, so it must not fire during one."""
    assert WATCHDOG_SECONDS - _drain_seconds(_dockerfile_cmd()) >= _SHUTDOWN_SECONDS


@pytest.mark.parametrize("unit", SYSTEMD_UNITS, ids=lambda path: path.name)
def test_systemd_units_bound_the_wait_for_open_requests(unit: Path) -> None:
    folded = unit.read_text().replace("\\\n", " ")
    (exec_start,) = [line for line in folded.splitlines() if line.startswith("ExecStart=")]

    assert 0 < _drain_seconds(exec_start.split()) <= 10
    # systemd's default stop timeout is 90 s; a unit that shortened it would
    # need to leave the shutdown its time too.
    assert "TimeoutStopSec" not in unit.read_text()


def test_the_scan_still_sees_the_systemd_units() -> None:
    assert {unit.name for unit in SYSTEMD_UNITS} >= {
        "hybrid_inference.service",
        "hybrid_inference.staging.service",
    }
