"""Regression test for the standalone geo exporter demo mode."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from serving.analytics.geo_demand import BUCKET_COLS


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
        check=True,
        capture_output=True,
        text=True,
    )
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
    assert "classes" not in payload
    assert all(len(flow) == 4 for hour in payload["hours"] for flow in hour["f"])
    assert "wrote" in result.stdout


def test_viewer_only_attributes_dbip_lite_data() -> None:
    viewer = (Path(__file__).resolve().parents[3] / "ops/db/analysis/geo_globe.html").read_text()

    assert "data.meta.geoip?.provider === 'dbip-lite'" in viewer
    assert 'href="https://db-ip.com"' in viewer
    assert "IP Geolocation by DB-IP" in viewer
