"""Comparison exports must not disguise unresolved cloud liability as free service."""

from __future__ import annotations

import csv
import json

from benchmark.nimbus.compare import compare
from benchmark.nimbus.report import summarize


def write_run(tmp_path, summary=None):
    directory = tmp_path / "runs" / "one-run"
    directory.mkdir(parents=True)
    manifest = {
        "run_id": "run-1",
        "state": "completed" if summary else "failed",
        "policy": "all_api",
        "source": {"commit": "test-commit"},
        "workload_sha256": "a" * 64,
        "config_sha256": "b" * 64,
        "recorded_requests": 0,
    }
    (directory / "manifest.json").write_text(json.dumps(manifest))
    if summary is not None:
        (directory / "summary.json").write_text(json.dumps(summary))
    return tmp_path / "runs"


def read_export(path):
    with path.open(newline="") as stream:
        return next(csv.DictReader(stream))


def test_unknown_cloud_usage_preserves_liability_failure_and_joint_slo(tmp_path):
    summary = summarize(
        [
            {
                "route": "cloud",
                "status": "error",
                "budget_status": "unknown",
                "unresolved_reserved_cny": "2.516736",
                "joint_slo_satisfied": False,
            },
            {
                "route": "cloud",
                "status": "success",
                "budget_status": "settled",
                "cost_cny_estimated": "0.002",
                "joint_slo_satisfied": True,
            },
        ],
        {"ttft_s": 1, "tpot_s": 0.1},
    )
    runs = write_run(tmp_path, summary)
    output = tmp_path / "comparison.csv"
    compare(runs, output)
    row = read_export(output)
    assert row["estimated_cost_cny"] == "0.002"
    assert row["cost_complete"] == "False"
    assert row["unresolved_reserved_cny"] == "2.516736"
    assert row["conservative_cost_upper_bound_cny"] == "2.518736"
    assert row["requests"] == "2"
    assert row["failed_requests"] == "1"
    assert row["joint_slo_satisfied_fraction_all_requests"] == "0.5"


def test_known_zero_api_cost_is_distinct_from_unknown_cost(tmp_path):
    summary = summarize(
        [{"route": "local", "status": "success", "joint_slo_satisfied": True}],
        {"ttft_s": 1, "tpot_s": 0.1},
    )
    runs = write_run(tmp_path, summary)
    output = tmp_path / "comparison.csv"
    compare(runs, output)
    row = read_export(output)
    assert row["estimated_cost_cny"] == "0"
    assert row["cost_complete"] == "True"
    assert row["unresolved_reserved_cny"] == "0"
    assert row["failed_requests"] == "0"


def test_missing_summary_leaves_cost_and_outcome_unknown(tmp_path):
    runs = write_run(tmp_path)
    output = tmp_path / "comparison.csv"
    compare(runs, output)
    row = read_export(output)
    assert row["state"] == "failed"
    for key in (
        "estimated_cost_cny",
        "cost_complete",
        "unresolved_reserved_cny",
        "conservative_cost_upper_bound_cny",
        "failed_requests",
        "joint_slo_satisfied_fraction_all_requests",
    ):
        assert row[key] == ""
