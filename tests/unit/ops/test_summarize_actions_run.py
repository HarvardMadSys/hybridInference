"""Tests for GitHub Actions queue and duration summaries."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from ops.ci.summarize_actions_run import MetricsError, render_markdown, summarize_jobs

REPO_ROOT = Path(__file__).resolve().parents[3]
SUMMARIZER = REPO_ROOT / "ops/ci/summarize_actions_run.py"


def _job(
    name: str,
    *,
    created: str,
    started: str,
    completed: str,
    runner: str = "runner-1",
) -> dict[str, str]:
    return {
        "name": name,
        "conclusion": "success",
        "runner_name": runner,
        "created_at": created,
        "started_at": started,
        "completed_at": completed,
    }


def test_summarizes_queue_wait_and_duration_deterministically() -> None:
    payload = {
        "jobs": [
            _job(
                "Test B",
                created="2026-07-22T08:41:14Z",
                started="2026-07-22T08:43:14Z",
                completed="2026-07-22T08:44:44Z",
            ),
            _job(
                "Test A",
                created="2026-07-22T08:41:24Z",
                started="2026-07-22T08:41:44Z",
                completed="2026-07-22T08:42:44Z",
            ),
        ]
    }

    report = summarize_jobs(payload, "2026-07-22T08:41:14Z")

    assert report["version"] == 1
    assert report["summary"] == {
        "job_count": 2,
        "measured_job_count": 2,
        "queue_p50_seconds": 20.0,
        "queue_p95_seconds": 120.0,
        "max_duration_seconds": 90.0,
    }
    assert [job["name"] for job in report["jobs"]] == ["Test A", "Test B"]
    assert report["jobs"][0]["run_wait_seconds"] == 30.0


def test_accepts_paginated_gh_api_slurp_payload() -> None:
    payload = [
        {
            "jobs": [
                _job(
                    "Backend",
                    created="2026-07-22T08:41:14Z",
                    started="2026-07-22T08:41:15Z",
                    completed="2026-07-22T08:41:35Z",
                )
            ]
        },
        {
            "jobs": [
                _job(
                    "Frontend",
                    created="2026-07-22T08:41:14Z",
                    started="2026-07-22T08:41:16Z",
                    completed="2026-07-22T08:41:46Z",
                )
            ]
        },
    ]

    report = summarize_jobs(payload, "2026-07-22T08:41:14Z")

    assert report["summary"]["job_count"] == 2
    assert {job["name"] for job in report["jobs"]} == {"Backend", "Frontend"}


def test_rejects_out_of_order_timestamps() -> None:
    payload = {
        "jobs": [
            _job(
                "broken",
                created="2026-07-22T08:42:00Z",
                started="2026-07-22T08:41:00Z",
                completed="2026-07-22T08:43:00Z",
            )
        ]
    }

    with pytest.raises(MetricsError, match="out of order"):
        summarize_jobs(payload, "2026-07-22T08:40:00Z")


def test_keeps_unstarted_cancelled_job_without_fabricating_timings() -> None:
    payload = {
        "jobs": [
            {
                "name": "Test",
                "conclusion": "cancelled",
                "runner_name": None,
                "created_at": "2026-07-22T08:41:14Z",
                "started_at": None,
                "completed_at": None,
            }
        ]
    }

    report = summarize_jobs(payload, "2026-07-22T08:41:14Z")

    assert report["summary"] == {
        "job_count": 1,
        "measured_job_count": 0,
        "queue_p50_seconds": None,
        "queue_p95_seconds": None,
        "max_duration_seconds": None,
    }
    assert report["jobs"][0]["queue_seconds"] is None
    assert "| Test | — | — | — | — | cancelled |" in render_markdown(report, "123")


def test_markdown_includes_runner_and_summary() -> None:
    report = summarize_jobs(
        {
            "jobs": [
                _job(
                    "Backend",
                    created="2026-07-22T08:41:14Z",
                    started="2026-07-22T08:41:15Z",
                    completed="2026-07-22T08:41:35Z",
                    runner="self-hosted-1",
                )
            ]
        },
        "2026-07-22T08:41:14Z",
    )

    markdown = render_markdown(report, "123")

    assert "Run `123`" in markdown
    assert "| Backend | self-hosted-1 | 1.0s | 1.0s | 20.0s | success |" in markdown


def test_cli_writes_json_and_markdown(tmp_path: Path) -> None:
    jobs_path = tmp_path / "jobs.json"
    output_path = tmp_path / "metrics.json"
    markdown_path = tmp_path / "metrics.md"
    jobs_path.write_text(
        json.dumps(
            {
                "jobs": [
                    _job(
                        "Backend",
                        created="2026-07-22T08:41:14Z",
                        started="2026-07-22T08:41:15Z",
                        completed="2026-07-22T08:41:35Z",
                    )
                ]
            }
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            str(SUMMARIZER),
            "--jobs",
            str(jobs_path),
            "--run-id",
            "123",
            "--run-created-at",
            "2026-07-22T08:41:14Z",
            "--output",
            str(output_path),
            "--markdown",
            str(markdown_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(output_path.read_text(encoding="utf-8"))["run_id"] == "123"
    assert "CI timing" in markdown_path.read_text(encoding="utf-8")
