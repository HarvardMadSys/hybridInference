"""Tests for the offline DB-IP Country Lite updater."""

from __future__ import annotations

import gzip
import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
UPDATER = REPO_ROOT / "ops/setup/update_dbip_country_lite.sh"


def _write_plausible_mmdb_archive(path: Path) -> None:
    payload = bytearray(1_048_576)
    payload.extend(b"\xab\xcd\xefMaxMind.com")
    with gzip.open(path, "wb") as archive:
        archive.write(payload)


def _run_updater(source: Path, destination: Path, release: str) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "DBIP_COUNTRY_RELEASE": release,
        "DBIP_COUNTRY_URL": source.as_uri(),
        "DBIP_COUNTRY_DESTINATION": str(destination),
    }
    return subprocess.run(
        ["bash", str(UPDATER)],
        cwd=REPO_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )


def test_installs_atomically_and_skips_current_release(tmp_path: Path) -> None:
    archive = tmp_path / "country.mmdb.gz"
    destination = tmp_path / "geoip/dbip-country-lite.mmdb"
    _write_plausible_mmdb_archive(archive)

    installed = _run_updater(archive, destination, "2026-07")

    assert installed.returncode == 0, installed.stderr
    assert destination.stat().st_size > 1_048_576
    assert destination.with_suffix(".mmdb.release").read_text() == "2026-07\n"

    archive.unlink()
    skipped = _run_updater(archive, destination, "2026-07")

    assert skipped.returncode == 0, skipped.stderr
    assert "already installed" in skipped.stdout


def test_failed_update_keeps_last_good_database_and_marker(tmp_path: Path) -> None:
    invalid_archive = tmp_path / "invalid.mmdb.gz"
    invalid_archive.write_bytes(b"not gzip")
    destination = tmp_path / "geoip/dbip-country-lite.mmdb"
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"last-good-database")
    marker = destination.with_suffix(".mmdb.release")
    marker.write_text("2026-06\n")

    result = _run_updater(invalid_archive, destination, "2026-07")

    assert result.returncode != 0
    assert destination.read_bytes() == b"last-good-database"
    assert marker.read_text() == "2026-06\n"
