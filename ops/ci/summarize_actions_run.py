#!/usr/bin/env python3
"""Summarize GitHub Actions job queue and execution timing from API JSON."""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Sequence


class MetricsError(ValueError):
    """Raised when an Actions jobs payload cannot produce trustworthy metrics."""


@dataclass(frozen=True)
class JobMetric:
    """Timing data for one completed GitHub Actions job."""

    name: str
    conclusion: str
    runner_name: str
    queue_seconds: float | None
    run_wait_seconds: float | None
    duration_seconds: float | None


def _parse_timestamp(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise MetricsError(f"job {field} must be a non-empty timestamp")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MetricsError(f"job {field} is not a valid ISO timestamp: {value!r}") from exc


def _jobs_from_payload(payload: Any) -> list[dict[str, Any]]:
    pages = payload if isinstance(payload, list) else [payload]
    jobs: list[dict[str, Any]] = []
    for page in pages:
        if not isinstance(page, dict) or not isinstance(page.get("jobs"), list):
            raise MetricsError("Actions payload must contain a jobs array")
        for job in page["jobs"]:
            if not isinstance(job, dict):
                raise MetricsError("Actions jobs entries must be objects")
            jobs.append(job)
    if not jobs:
        raise MetricsError("Actions payload contains no jobs")
    return jobs


def _nearest_rank(values: Sequence[float], percentile: float) -> float:
    if not values:
        raise MetricsError("cannot calculate a percentile from no values")
    if not 0 < percentile <= 1:
        raise MetricsError("percentile must be in the interval (0, 1]")
    ordered = sorted(values)
    return ordered[math.ceil(percentile * len(ordered)) - 1]


def summarize_jobs(payload: Any, run_created_at: str) -> dict[str, Any]:
    """Build a versioned timing report from an Actions jobs API payload."""
    run_created = _parse_timestamp(run_created_at, "run_created_at")
    metrics: list[JobMetric] = []

    for job in _jobs_from_payload(payload):
        name = job.get("name")
        if not isinstance(name, str) or not name:
            raise MetricsError("Actions job name must be a non-empty string")

        started_value = job.get("started_at")
        completed_value = job.get("completed_at")
        started = (
            _parse_timestamp(started_value, "started_at")
            if isinstance(started_value, str) and started_value
            else None
        )
        completed = (
            _parse_timestamp(completed_value, "completed_at")
            if isinstance(completed_value, str) and completed_value
            else None
        )
        created = None
        if started is not None:
            created = _parse_timestamp(job.get("created_at", run_created_at), "created_at")
            if started < created or started < run_created:
                raise MetricsError(f"Actions job timestamps are out of order: {name}")
        if completed is not None and (started is None or completed < started):
            raise MetricsError(f"Actions job timestamps are out of order: {name}")

        conclusion = job.get("conclusion")
        runner_name = job.get("runner_name")
        metrics.append(
            JobMetric(
                name=name,
                conclusion=conclusion if isinstance(conclusion, str) else "",
                runner_name=runner_name if isinstance(runner_name, str) else "",
                queue_seconds=(started - created).total_seconds() if created else None,
                run_wait_seconds=(started - run_created).total_seconds() if started else None,
                duration_seconds=(completed - started).total_seconds()
                if started and completed
                else None,
            )
        )

    queue_values = [metric.queue_seconds for metric in metrics if metric.queue_seconds is not None]
    duration_values = [
        metric.duration_seconds for metric in metrics if metric.duration_seconds is not None
    ]
    return {
        "version": 1,
        "summary": {
            "job_count": len(metrics),
            "measured_job_count": len(duration_values),
            "queue_p50_seconds": _nearest_rank(queue_values, 0.50) if queue_values else None,
            "queue_p95_seconds": _nearest_rank(queue_values, 0.95) if queue_values else None,
            "max_duration_seconds": max(duration_values) if duration_values else None,
        },
        "jobs": [asdict(metric) for metric in sorted(metrics, key=lambda item: item.name)],
    }


def render_markdown(report: dict[str, Any], run_id: str) -> str:
    """Render a compact GitHub job-summary table."""
    summary = report["summary"]
    lines = [
        "## CI timing",
        "",
        f"Run `{run_id}` · queue P50 `{_format_seconds(summary['queue_p50_seconds'])}` · "
        f"queue P95 `{_format_seconds(summary['queue_p95_seconds'])}` · "
        f"longest job `{_format_seconds(summary['max_duration_seconds'])}`",
        "",
        "| Job | Runner | Queue | From run start | Duration | Result |",
        "|---|---|---:|---:|---:|---|",
    ]
    for job in report["jobs"]:
        lines.append(
            f"| {job['name']} | {job['runner_name'] or '—'} | "
            f"{_format_seconds(job['queue_seconds'])} | "
            f"{_format_seconds(job['run_wait_seconds'])} | "
            f"{_format_seconds(job['duration_seconds'])} | {job['conclusion'] or '—'} |"
        )
    return "\n".join(lines) + "\n"


def _format_seconds(value: float | None) -> str:
    return f"{value:.1f}s" if value is not None else "—"


def _write_atomic(path: Path, contents: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(contents, encoding="utf-8")
    temporary.replace(path)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jobs", type=Path, required=True, help="Actions jobs API JSON")
    parser.add_argument("--run-id", required=True, help="GitHub Actions run identifier")
    parser.add_argument("--run-created-at", required=True, help="Actions run creation timestamp")
    parser.add_argument("--output", type=Path, required=True, help="versioned JSON output")
    parser.add_argument("--markdown", type=Path, help="optional Markdown summary output")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the Actions timing summarizer."""
    args = _build_parser().parse_args(argv)
    try:
        payload = json.loads(args.jobs.read_text(encoding="utf-8"))
        report = summarize_jobs(payload, args.run_created_at)
        report["run_id"] = str(args.run_id)
        _write_atomic(args.output, json.dumps(report, indent=2, sort_keys=True) + "\n")
        if args.markdown:
            _write_atomic(args.markdown, render_markdown(report, str(args.run_id)))
        return 0
    except (OSError, json.JSONDecodeError, MetricsError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
