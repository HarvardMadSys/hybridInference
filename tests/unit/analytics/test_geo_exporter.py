"""Regression test for the standalone geo exporter demo mode."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from ops.db.analysis.geo_hourly_export import BUCKET_COLS, ROWS_QUERY
from tests.conftest import subprocess_env


def test_exporter_demo_command(tmp_path) -> None:
    output = tmp_path / "data.json"
    result = subprocess.run(
        [
            sys.executable,
            "ops/db/analysis/geo_hourly_export.py",
            "--demo",
            "--demo-days",
            "1",
            "--out",
            str(output),
        ],
        cwd=Path(__file__).resolve().parents[3],
        # This child gets none of pytest's import path, so without an explicit
        # PYTHONPATH it resolves ``serving`` through the editable install -- a
        # different checkout than the one under test whenever this runs from a
        # worktree, which AGENTS.md mandates. See ``subprocess_env``.
        env=subprocess_env(),
        capture_output=True,
        text=True,
    )
    # Not check=True: with capture_output that raises a bare CalledProcessError and
    # throws the child's traceback away, which is the whole diagnosis.
    assert result.returncode == 0, f"exporter failed:\n{result.stderr}"
    payload = json.loads(output.read_text())
    assert payload["meta"]["source"] == "synthetic-demo"
    assert payload["meta"]["geoip"] == {
        "country": False,
        "provider": None,
        "attribution": None,
    }
    assert payload["meta"]["hours"] == 24
    assert payload["bucket_cols"] == BUCKET_COLS
    assert payload["flow_cols"] == ["c", "p", "e", "n"]
    assert payload["providers"]
    assert "classes" not in payload
    assert all(len(flow) == 4 for hour in payload["hours"] for flow in hour["f"])
    assert "wrote" in result.stdout


def test_exporter_excludes_synthetic_probes() -> None:
    assert "COALESCE(metadata->>'synthetic_probe', 'false') <> 'true'" in ROWS_QUERY


def test_viewer_only_attributes_dbip_lite_data() -> None:
    viewer = (Path(__file__).resolve().parents[3] / "ops/db/analysis/geo_globe.html").read_text()

    assert "data.meta.geoip?.provider === 'dbip-lite'" in viewer
    assert 'href="https://db-ip.com"' in viewer
    assert "IP Geolocation by DB-IP" in viewer


def test_viewer_tolerates_minimal_product_contract() -> None:
    viewer = (Path(__file__).resolve().parents[3] / "ops/db/analysis/geo_globe.html").read_text()

    assert "(data.flow_cols || []).map" in viewer
    assert "data.providers || []" in viewer
    assert "data.hours[h]?.f || []" in viewer
    assert "B[column] === undefined ? fallback" in viewer
    assert "if (B[option.value] === undefined) option.remove()" in viewer
    assert "d.coord && d.v > 0" in viewer
    assert "contTotal > 0" in viewer
    assert "Total latency (s)" in viewer
    assert "compute-sec" not in viewer
