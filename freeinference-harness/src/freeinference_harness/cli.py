"""Command-line interface for the standalone FreeInference harness."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

from freeinference_harness.config import load_suite, load_targets
from freeinference_harness.reporting import write_run_artifacts
from freeinference_harness.runner import HarnessRunner


def build_parser() -> argparse.ArgumentParser:
    """Builds the CLI parser."""
    parser = argparse.ArgumentParser(description="Run black-box FreeInference harness suites.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="Run a configured suite.")
    run_parser.add_argument("--targets", required=True, help="Path to the target YAML file.")
    run_parser.add_argument("--scenarios", required=True, help="Path to the scenario YAML file.")
    run_parser.add_argument(
        "--target",
        action="append",
        default=[],
        help="Optional target name to run. Repeat to select more than one.",
    )
    run_parser.add_argument(
        "--output-dir",
        default="outputs",
        help="Directory where run artifacts will be written.",
    )
    run_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the selected targets and scenarios without making network calls.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Runs the CLI entrypoint."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "run":
        targets = load_targets(Path(args.targets))
        suite = load_suite(Path(args.scenarios))
        selected_names = set(args.target or [])
        if selected_names:
            targets = [target for target in targets if target.name in selected_names]
        if not targets:
            parser.error("No targets selected after applying filters.")

        if args.dry_run:
            _print_dry_run(targets=targets, suite_name=suite.suite_name, scenarios=suite.scenarios)
            return 0

        missing_api_key = [target.name for target in targets if not target.api_key]
        if missing_api_key:
            parser.error(
                "Missing API key for target(s): "
                + ", ".join(sorted(missing_api_key))
                + ". Set FREEINFERENCE_API_KEY or provide api_key/api_key_env in the target file."
            )

        runner = HarnessRunner()
        run_record = runner.run(targets=targets, suite=suite)
        run_dir = write_run_artifacts(Path(args.output_dir), run_record)
        print(f"Run complete. Artifacts written to: {run_dir}")

        has_failures = any(
            a.status == "fail" for s in run_record.scenario_summaries for a in s.attempts
        )
        return 1 if has_failures else 0

    parser.error(f"Unsupported command: {args.command}")
    return 2


def _print_dry_run(*, targets, suite_name, scenarios) -> None:
    """Prints the resolved run plan."""
    print(f"Suite: {suite_name}")
    print("Targets:")
    for target in targets:
        print(
            f"  - {target.name}: model={target.model}, "
            f"suite_type={target.suite_type}, samples={target.sampling_count}"
        )
    print("Scenarios:")
    for scenario in scenarios:
        required = ", ".join(scenario.required_capabilities) or "(none)"
        print(
            f"  - {scenario.scenario_id}: type={scenario.scenario_type}, "
            f"required={required}, repetitions={scenario.repetitions or 'target default'}"
        )


if __name__ == "__main__":
    raise SystemExit(main())
