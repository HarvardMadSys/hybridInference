"""Export per-run comparisons while preserving exact workload/config identities."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path


def compare(directory: Path, output: Path) -> list[dict]:
    """Summarize immutable runs separately; never average their percentiles."""
    rows = []
    for manifest_path in sorted(directory.glob("*/manifest.json")):
        manifest = json.loads(manifest_path.read_text())
        summary_path = manifest_path.parent / "summary.json"
        summary = json.loads(summary_path.read_text()) if summary_path.exists() else {}
        row = {
            "directory": manifest_path.parent.name,
            "run_id": manifest["run_id"],
            "state": manifest["state"],
            "policy": manifest["policy"],
            "source_commit": manifest["source"]["commit"],
            "workload_sha256": manifest["workload_sha256"],
            "config_sha256": manifest["config_sha256"],
            "requests": summary.get("requests", manifest.get("recorded_requests", 0)),
            "successful_requests": summary.get("successful_requests"),
            "failed_requests": summary.get("failed_requests"),
            "local": summary.get("routes", {}).get("local", 0),
            "cloud": summary.get("routes", {}).get("cloud", 0),
            "truncated_requests": summary.get("truncated_requests"),
            "usage_coverage": summary.get("authoritative_usage_requests"),
            "estimated_cost_cny": summary.get("cost_cny_estimated"),
            "cost_complete": summary.get("cost_complete"),
            "unresolved_reserved_cny": summary.get("unresolved_reserved_cny"),
            "conservative_cost_upper_bound_cny": summary.get("conservative_cost_upper_bound_cny"),
            "joint_slo_satisfied_fraction_all_requests": summary.get(
                "joint_slo_satisfied_fraction_all_requests"
            ),
            "duration_s": summary.get("duration_s"),
        }
        for metric in ("ttft_s", "tpot_s", "e2e_s"):
            for quantile in (50, 90, 95, 99):
                row[f"{metric}_p{quantile}"] = summary.get(metric, {}).get(f"p{quantile}")
        rows.append(row)
    if not rows:
        raise ValueError("no run manifests found")
    with output.open("x", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    return rows


def main() -> None:
    """Export a new comparison artifact, leaving every raw run untouched."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    rows = compare(args.runs, args.output)
    print(json.dumps({"runs": len(rows), "output": str(args.output)}))


if __name__ == "__main__":
    main()
