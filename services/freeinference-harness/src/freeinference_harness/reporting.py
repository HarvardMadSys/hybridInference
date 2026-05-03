"""Artifact writers for harness executions."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

    from freeinference_harness.models import RunRecord


def write_run_artifacts(output_root: Path, run_record: RunRecord) -> Path:
    """Writes run artifacts and returns the run directory."""
    run_dir = output_root / run_record.run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    run_json = run_record.to_dict()
    (run_dir / "run.json").write_text(json.dumps(run_json, indent=2), encoding="utf-8")

    with (run_dir / "attempts.jsonl").open("w", encoding="utf-8") as handle:
        for summary in run_record.scenario_summaries:
            for attempt in summary.attempts:
                handle.write(json.dumps(attempt.to_dict(), ensure_ascii=False) + "\n")

    (run_dir / "summary.md").write_text(_render_summary(run_json), encoding="utf-8")
    return run_dir


def _render_summary(run_json: dict[str, Any]) -> str:
    """Renders a concise markdown summary for one run."""
    lines = [
        f"# Harness Summary: {run_json['run_id']}",
        "",
        f"- Timestamp: `{run_json['timestamp']}`",
        f"- Suite: `{run_json['suite_name']}`",
        "",
        "| Target | Scenario | Passed | Failed | Skipped | Pass rate |",
        "| --- | --- | --- | --- | --- | --- |",
    ]

    for summary in run_json["scenario_summaries"]:
        lines.append(
            "| "
            f"{summary['target_name']} | "
            f"{summary['scenario_id']} | "
            f"{summary['passed']} | "
            f"{summary['failed']} | "
            f"{summary['skipped']} | "
            f"{summary['pass_rate']:.2%} |"
        )

    lines.extend(
        [
            "",
            "## Notes",
            "",
            "- `run.json` contains the full structured output.",
            "- `attempts.jsonl` contains one record per sampled attempt.",
        ]
    )
    return "\n".join(lines) + "\n"
