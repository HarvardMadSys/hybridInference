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
    assert report["anomalies"] == []


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


def test_clamps_skipped_job_completed_before_it_started() -> None:
    """Regression: run 34700471765 aborted the whole report over this one job.

    GitHub reported the skipped ``docker-build`` job as completing one second
    before it started. A timing report must survive that.
    """
    payload = {
        "jobs": [
            {
                "name": "docker-build",
                "conclusion": "skipped",
                "runner_name": None,
                "created_at": "2026-09-12T14:54:24Z",
                "started_at": "2026-09-12T14:54:24Z",
                "completed_at": "2026-09-12T14:54:23Z",
            },
            _job(
                "Backend Quality",
                created="2026-09-12T14:54:23Z",
                started="2026-09-12T14:54:24Z",
                completed="2026-09-12T14:54:43Z",
            ),
        ]
    }

    report = summarize_jobs(payload, "2026-09-12T14:50:44Z")

    assert report["anomalies"] == [
        {"job": "docker-build", "issue": "duration was negative (-1.0s); clamped to 0.0s"}
    ]
    skipped = next(job for job in report["jobs"] if job["name"] == "docker-build")
    assert skipped["duration_seconds"] == 0.0
    assert skipped["run_wait_seconds"] == 220.0
    # The healthy sibling still reports real numbers.
    assert report["summary"]["job_count"] == 2
    assert report["summary"]["max_duration_seconds"] == 19.0


def test_clamps_start_preceding_creation_and_run_start() -> None:
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

    report = summarize_jobs(payload, "2026-07-22T08:41:30Z")

    assert [anomaly["issue"] for anomaly in report["anomalies"]] == [
        "queue time was negative (-60.0s); clamped to 0.0s",
        "time from run start was negative (-30.0s); clamped to 0.0s",
    ]
    assert report["jobs"][0]["queue_seconds"] == 0.0
    assert report["jobs"][0]["run_wait_seconds"] == 0.0
    assert report["jobs"][0]["duration_seconds"] == 120.0


def test_keeps_job_with_unparseable_timestamp() -> None:
    payload = {
        "jobs": [
            {
                "name": "rerun",
                "conclusion": "success",
                "runner_name": "runner-1",
                "created_at": "2026-07-22T08:41:14Z",
                "started_at": "not-a-timestamp",
                "completed_at": "2026-07-22T08:43:00Z",
            }
        ]
    }

    report = summarize_jobs(payload, "2026-07-22T08:41:14Z")

    assert [anomaly["issue"] for anomaly in report["anomalies"]] == [
        "completed_at without a started_at; duration unknown",
        "job started_at is not a valid ISO timestamp: 'not-a-timestamp'",
    ]
    assert report["summary"]["job_count"] == 1
    assert report["summary"]["measured_job_count"] == 0
    assert report["jobs"][0]["duration_seconds"] is None


def test_markdown_annotates_anomalous_job_and_warns() -> None:
    report = summarize_jobs(
        {
            "jobs": [
                {
                    "name": "docker-build",
                    "conclusion": "skipped",
                    "runner_name": None,
                    "created_at": "2026-09-12T14:54:24Z",
                    "started_at": "2026-09-12T14:54:24Z",
                    "completed_at": "2026-09-12T14:54:23Z",
                }
            ]
        },
        "2026-09-12T14:50:44Z",
    )

    markdown = render_markdown(report, "34700471765")

    assert "| docker-build ⚠️ | — | 0.0s | 220.0s | 0.0s | skipped |" in markdown
    assert "> [!WARNING]" in markdown
    assert "> - `docker-build`: duration was negative (-1.0s); clamped to 0.0s" in markdown


def test_still_rejects_structurally_invalid_payload() -> None:
    """Timing quirks are tolerated; a payload with no jobs array is still fatal."""
    with pytest.raises(MetricsError, match="jobs array"):
        summarize_jobs({"total_count": 0}, "2026-07-22T08:41:14Z")


def test_cli_exits_zero_on_out_of_order_timestamps(tmp_path: Path) -> None:
    jobs_path = tmp_path / "jobs.json"
    output_path = tmp_path / "metrics.json"
    markdown_path = tmp_path / "metrics.md"
    jobs_path.write_text(
        json.dumps(
            {
                "jobs": [
                    {
                        "name": "docker-build",
                        "conclusion": "skipped",
                        "runner_name": None,
                        "created_at": "2026-09-12T14:54:24Z",
                        "started_at": "2026-09-12T14:54:24Z",
                        "completed_at": "2026-09-12T14:54:23Z",
                    }
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
            "34700471765",
            "--run-created-at",
            "2026-09-12T14:50:44Z",
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
    assert "warning: docker-build: duration was negative" in result.stderr
    assert json.loads(output_path.read_text(encoding="utf-8"))["anomalies"]
    assert "> [!WARNING]" in markdown_path.read_text(encoding="utf-8")


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
