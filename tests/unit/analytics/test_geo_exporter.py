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
    assert payload["meta"]["hours"] == 24
    assert payload["bucket_cols"] == BUCKET_COLS
    assert payload["flow_cols"] == ["c", "cls", "p", "e", "n"]
    assert all(len(flow) == 5 for hour in payload["hours"] for flow in hour["f"])
    assert "wrote" in result.stdout
