#!/usr/bin/env python3
"""Validate change-classifier outputs and enforce the fail-closed CI gate."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

BOOLEAN_OUTPUTS = (
    "backend",
    "frontend",
    "oncall",
    "status_monitor",
    "alert_control_plane",
    "docker_shared",
    "security_only",
    "full",
)
DOCKER_IMAGES = ("frontend", "backend", "oncall")
APP_JOB_CATEGORIES = {
    "backend-quality": "backend",
    "frontend-quality": "frontend",
    "alert-control-plane-check": "alert_control_plane",
    "test": "backend",
}
REQUIRED_JOBS = (
    "changes",
    *APP_JOB_CATEGORIES,
    "security",
    "docker-build",
)
SUPPORTED_EVENTS = frozenset({"pull_request", "push", "schedule", "workflow_dispatch"})
JOB_RESULTS = frozenset({"success", "failure", "cancelled", "skipped"})


@dataclass(frozen=True)
class ClassificationOutputs:
    """Strictly parsed workflow outputs from the change classifier."""

    booleans: Mapping[str, bool]
    docker_matrix: tuple[str, ...]

    def github_outputs(self) -> dict[str, str]:
        """Return canonical strings suitable for ``GITHUB_OUTPUT``."""
        return {
            **{name: str(value).lower() for name, value in self.booleans.items()},
            "docker_matrix": json.dumps(self.docker_matrix, separators=(",", ":")),
        }


def _parse_json_object(payload: str, description: str) -> dict[str, Any]:
    try:
        value = json.loads(payload)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{description} is not valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{description} must be a JSON object")
    return value


def parse_classification(payload: str) -> ClassificationOutputs:
    """Parse and semantically validate classifier workflow outputs."""
    raw = _parse_json_object(payload, "classification")
    expected = {*BOOLEAN_OUTPUTS, "docker_matrix"}
    missing = sorted(expected - raw.keys())
    extra = sorted(raw.keys() - expected)
    if missing or extra:
        raise ValueError(f"classification keys invalid: missing={missing}, extra={extra}")

    booleans: dict[str, bool] = {}
    for name in BOOLEAN_OUTPUTS:
        value = raw[name]
        if not isinstance(value, str) or value not in {"true", "false"}:
            raise ValueError(f"classification output {name!r} must be 'true' or 'false'")
        booleans[name] = value == "true"

    matrix_json = raw["docker_matrix"]
    if not isinstance(matrix_json, str):
        raise ValueError("classification output 'docker_matrix' must be a JSON string")
    try:
        matrix = json.loads(matrix_json)
    except json.JSONDecodeError as exc:
        raise ValueError(f"docker_matrix is not valid JSON: {exc}") from exc
    if not isinstance(matrix, list) or any(not isinstance(image, str) for image in matrix):
        raise ValueError("docker_matrix must encode a JSON array of image names")
    if len(matrix) != len(set(matrix)):
        raise ValueError("docker_matrix must not contain duplicate images")
    if any(image not in DOCKER_IMAGES for image in matrix):
        raise ValueError(f"docker_matrix may contain only {list(DOCKER_IMAGES)}")
    canonical = [image for image in DOCKER_IMAGES if image in matrix]
    if matrix != canonical:
        raise ValueError(f"docker_matrix must use stable order {list(DOCKER_IMAGES)}")

    narrow = [name for name in BOOLEAN_OUTPUTS if name not in {"security_only", "full"}]
    if booleans["security_only"]:
        if booleans["full"] or any(booleans[name] for name in narrow):
            raise ValueError("security_only must be exclusive")
        if matrix:
            raise ValueError("security_only must not build application images")
    elif not booleans["full"] and not any(booleans[name] for name in narrow):
        raise ValueError("classification must select full, security_only, or a narrow category")

    if booleans["full"] or booleans["docker_shared"]:
        if matrix != list(DOCKER_IMAGES):
            raise ValueError("full/docker_shared classification must build all images")
    else:
        required_images = {
            "frontend": booleans["frontend"],
            "oncall": booleans["oncall"],
        }
        for image, required in required_images.items():
            if required and image not in matrix:
                raise ValueError(f"{image} classification must include the {image} image")
        for image in matrix:
            if not booleans[image]:
                raise ValueError(f"{image} image has no matching classification category")

    return ClassificationOutputs(booleans=booleans, docker_matrix=tuple(matrix))


def parse_job_results(payload: str) -> dict[str, str]:
    """Parse the exact set of job results consumed by the gate."""
    raw = _parse_json_object(payload, "job results")
    missing = sorted(set(REQUIRED_JOBS) - raw.keys())
    extra = sorted(raw.keys() - set(REQUIRED_JOBS))
    if missing or extra:
        raise ValueError(f"job result keys invalid: missing={missing}, extra={extra}")
    results: dict[str, str] = {}
    for name in REQUIRED_JOBS:
        result = raw[name]
        if not isinstance(result, str) or result not in JOB_RESULTS:
            raise ValueError(f"job result {name!r} is missing or malformed")
        results[name] = result
    return results


def verify_gate(event_name: str, classification_payload: str, job_results_payload: str) -> None:
    """Raise ``ValueError`` unless every job has its exact expected result."""
    if event_name not in SUPPORTED_EVENTS:
        raise ValueError(f"unsupported event: {event_name!r}")
    classification = parse_classification(classification_payload)
    results = parse_job_results(job_results_payload)

    expected: dict[str, str] = {"changes": "success", "security": "success"}
    if event_name == "pull_request":
        for job, category in APP_JOB_CATEGORIES.items():
            should_run = classification.booleans["full"] or classification.booleans[category]
            expected[job] = "success" if should_run else "skipped"
        expected["docker-build"] = "success" if classification.docker_matrix else "skipped"
    else:
        expected.update(dict.fromkeys(APP_JOB_CATEGORIES, "success"))
        expected["docker-build"] = "skipped" if event_name == "push" else "success"
        if event_name in {"schedule", "workflow_dispatch"} and (
            not classification.booleans["full"] or classification.docker_matrix != DOCKER_IMAGES
        ):
            raise ValueError("scheduled/manual CI must classify as a full three-image run")

    mismatches = [
        f"{job}: expected {wanted}, got {results[job]}"
        for job, wanted in expected.items()
        if results[job] != wanted
    ]
    if mismatches:
        raise ValueError("CI Gate rejected job results: " + "; ".join(mismatches))


def _write_github_outputs(outputs: Mapping[str, str], output_path: Path) -> None:
    with output_path.open("a", encoding="utf-8") as handle:
        for name, value in outputs.items():
            handle.write(f"{name}={value}\n")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser(
        "validate-classification", help="validate and canonicalize classifier outputs"
    )
    validate.add_argument("--classification-json", required=True)
    validate.add_argument("--github-output", type=Path, required=True)

    gate = subparsers.add_parser("verify-gate", help="verify exact expected CI job results")
    gate.add_argument("--event-name", required=True)
    gate.add_argument("--classification-json", required=True)
    gate.add_argument("--job-results-json", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the selected strict validation command."""
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "validate-classification":
            classification = parse_classification(args.classification_json)
            _write_github_outputs(classification.github_outputs(), args.github_output)
            print("classification outputs are valid")
        else:
            verify_gate(args.event_name, args.classification_json, args.job_results_json)
            print("CI Gate accepted all required job results")
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
