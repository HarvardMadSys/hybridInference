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


@dataclass(frozen=True)
class JobAnomaly:
    """A job whose timestamps the Actions API reported inconsistently."""

    job: str
    issue: str


def _parse_timestamp(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise MetricsError(f"job {field} must be a non-empty timestamp")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MetricsError(f"job {field} is not a valid ISO timestamp: {value!r}") from exc


def _optional_timestamp(
    value: Any,
    field: str,
    job_name: str,
    anomalies: list[JobAnomaly],
) -> datetime | None:
    """Parse a job timestamp, recording an anomaly rather than raising on bad data."""
    if value is None or (isinstance(value, str) and not value):
        return None
    try:
        return _parse_timestamp(value, field)
    except MetricsError as exc:
        anomalies.append(JobAnomaly(job=job_name, issue=str(exc)))
        return None


def _elapsed(
    start: datetime | None,
    end: datetime | None,
    label: str,
    job_name: str,
    anomalies: list[JobAnomaly],
) -> float | None:
    """Return a non-negative interval, clamping and recording out-of-order timestamps.

    The Actions API reports inverted timestamps for skipped jobs, matrix legs,
    reruns and cancelled-then-superseded runs. Those are reporting artifacts, not
    build failures, so the offending value is clamped to zero and annotated.
    """
    if start is None or end is None:
        return None
    seconds = (end - start).total_seconds()
    if seconds < 0:
        anomalies.append(
            JobAnomaly(
                job=job_name,
                issue=f"{label} was negative ({seconds:.1f}s); clamped to 0.0s",
            )
        )
        return 0.0
    return seconds


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
    """Build a versioned timing report from an Actions jobs API payload.

    Out-of-order or unparseable job timestamps are clamped and reported under
    ``anomalies`` instead of aborting: a timing report must never fail the build.
    """
    run_created = _parse_timestamp(run_created_at, "run_created_at")
    metrics: list[JobMetric] = []
    anomalies: list[JobAnomaly] = []

    for job in _jobs_from_payload(payload):
        name = job.get("name")
        if not isinstance(name, str) or not name:
            raise MetricsError("Actions job name must be a non-empty string")

        started = _optional_timestamp(job.get("started_at"), "started_at", name, anomalies)
        completed = _optional_timestamp(job.get("completed_at"), "completed_at", name, anomalies)
        created = _optional_timestamp(job.get("created_at"), "created_at", name, anomalies)
        if created is None:
            created = run_created
        if started is None and completed is not None:
            anomalies.append(
                JobAnomaly(job=name, issue="completed_at without a started_at; duration unknown")
            )

        conclusion = job.get("conclusion")
        runner_name = job.get("runner_name")
        metrics.append(
            JobMetric(
                name=name,
                conclusion=conclusion if isinstance(conclusion, str) else "",
                runner_name=runner_name if isinstance(runner_name, str) else "",
                queue_seconds=_elapsed(created, started, "queue time", name, anomalies),
                run_wait_seconds=_elapsed(
                    run_created, started, "time from run start", name, anomalies
                ),
                duration_seconds=_elapsed(started, completed, "duration", name, anomalies),
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
        "anomalies": [
            asdict(anomaly)
            for anomaly in sorted(anomalies, key=lambda item: (item.job, item.issue))
        ],
    }


def render_markdown(report: dict[str, Any], run_id: str) -> str:
    """Render a compact GitHub job-summary table."""
    summary = report["summary"]
    anomalies = report.get("anomalies") or []
    flagged = {anomaly["job"] for anomaly in anomalies}
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
        marker = " ⚠️" if job["name"] in flagged else ""
        lines.append(
            f"| {job['name']}{marker} | {job['runner_name'] or '—'} | "
            f"{_format_seconds(job['queue_seconds'])} | "
            f"{_format_seconds(job['run_wait_seconds'])} | "
            f"{_format_seconds(job['duration_seconds'])} | {job['conclusion'] or '—'} |"
        )
    if anomalies:
        lines.extend(
            [
                "",
                "> [!WARNING]",
                "> The Actions API reported inconsistent timestamps for some jobs. "
                "Affected values are clamped; the rest of the report is unaffected.",
            ]
        )
        lines.extend(f"> - `{anomaly['job']}`: {anomaly['issue']}" for anomaly in anomalies)
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
        for anomaly in report["anomalies"]:
            print(f"warning: {anomaly['job']}: {anomaly['issue']}", file=sys.stderr)
        _write_atomic(args.output, json.dumps(report, indent=2, sort_keys=True) + "\n")
        if args.markdown:
            _write_atomic(args.markdown, render_markdown(report, str(args.run_id)))
        return 0
    except (OSError, json.JSONDecodeError, MetricsError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
