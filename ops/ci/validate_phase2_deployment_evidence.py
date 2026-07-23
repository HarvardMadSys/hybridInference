#!/usr/bin/env python3
"""Validate captured Phase 2 deployment evidence and cross-field invariants."""

from __future__ import annotations

import argparse
import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SCHEMA = REPO_ROOT / "contracts/evidence/phase2-deployment-evidence.schema.json"


class DeploymentEvidenceValidationError(ValueError):
    """Raised when structural or relational evidence checks fail."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = tuple(errors)
        super().__init__("; ".join(errors))


def _schema_errors(evidence: Any, schema: dict[str, Any]) -> list[str]:
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    errors: list[str] = []
    for error in sorted(validator.iter_errors(evidence), key=lambda item: list(item.path)):
        location = ".".join(str(part) for part in error.absolute_path) or "<root>"
        errors.append(f"{location}: schema {error.validator} check failed")
    return errors


def _timestamp(value: Any, location: str, errors: list[str]) -> datetime | None:
    if not isinstance(value, str):
        return None
    normalized = f"{value[:-1]}+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        errors.append(f"{location}: timestamp must include a timezone")
        return None
    return parsed


def _ordered_window(
    section: dict[str, Any],
    location: str,
    errors: list[str],
) -> tuple[datetime | None, datetime | None]:
    start = _timestamp(section.get("window_start"), f"{location}.window_start", errors)
    end = _timestamp(section.get("window_end"), f"{location}.window_end", errors)
    if start is not None and end is not None and end <= start:
        errors.append(f"{location}: window_end must be later than window_start")
    return start, end


def semantic_evidence_errors(evidence: dict[str, Any]) -> list[str]:
    """Return relationship failures that JSON Schema cannot express."""
    errors: list[str] = []

    dark_load = evidence.get("dark_load")
    dark_start: datetime | None = None
    dark_end: datetime | None = None
    if isinstance(dark_load, dict):
        dark_start, dark_end = _ordered_window(dark_load, "dark_load", errors)
        request_count = dark_load.get("request_count")
        success_count = dark_load.get("success_count")
        success_rate = dark_load.get("success_rate")
        required_rate = dark_load.get("required_success_rate")
        if (
            isinstance(request_count, int)
            and not isinstance(request_count, bool)
            and isinstance(success_count, int)
            and not isinstance(success_count, bool)
        ):
            if success_count > request_count:
                errors.append("dark_load: success_count must not exceed request_count")
            elif request_count > 0 and isinstance(success_rate, (int, float)):
                expected_rate = success_count / request_count
                if not math.isclose(float(success_rate), expected_rate, abs_tol=1e-12):
                    errors.append("dark_load: success_rate must equal success_count/request_count")
        if (
            isinstance(success_rate, (int, float))
            and isinstance(required_rate, (int, float))
            and float(success_rate) < float(required_rate)
        ):
            errors.append("dark_load: success_rate is below required_success_rate")

    smoke_tests = evidence.get("smoke_tests")
    if isinstance(smoke_tests, dict):
        frontend = smoke_tests.get("frontend_html")
        if isinstance(frontend, dict):
            route_count = frontend.get("route_count")
            passed_count = frontend.get("passed_route_count")
            failure_count = frontend.get("failure_count")
            if (
                isinstance(route_count, int)
                and not isinstance(route_count, bool)
                and isinstance(passed_count, int)
                and not isinstance(passed_count, bool)
                and isinstance(failure_count, int)
                and not isinstance(failure_count, bool)
                and (passed_count + failure_count != route_count or passed_count != route_count)
            ):
                errors.append(
                    "smoke_tests.frontend_html: every declared route must pass exactly once"
                )

    metrics = evidence.get("metrics")
    metrics_end: datetime | None = None
    if isinstance(metrics, dict):
        _metrics_start, metrics_end = _ordered_window(metrics, "metrics", errors)
        if metrics.get("critical_alert_count") != 0:
            errors.append("metrics: passed evidence requires zero critical alerts")
        thresholds = metrics.get("abort_thresholds")
        observed = metrics.get("observed_maxima")
        if isinstance(thresholds, dict) and isinstance(observed, dict):
            for metric in ("latency_p95_ms", "error_rate", "cost_usd"):
                threshold = thresholds.get(metric)
                value = observed.get(metric)
                if (
                    isinstance(threshold, (int, float))
                    and isinstance(value, (int, float))
                    and float(value) > float(threshold)
                ):
                    errors.append(f"metrics.observed_maxima.{metric}: exceeds abort threshold")

    source_build = evidence.get("source_build")
    built_at = (
        _timestamp(source_build.get("built_at"), "source_build.built_at", errors)
        if isinstance(source_build, dict)
        else None
    )
    live_database = evidence.get("live_database")
    backup_at: datetime | None = None
    if isinstance(live_database, dict):
        backup = live_database.get("backup")
        if isinstance(backup, dict):
            backup_at = _timestamp(
                backup.get("captured_at"),
                "live_database.backup.captured_at",
                errors,
            )
    if dark_start is not None:
        if built_at is not None and built_at > dark_start:
            errors.append("source_build: image must be built before dark_load starts")
        if backup_at is not None and backup_at > dark_start:
            errors.append("live_database.backup: backup must precede dark_load")

    attestation = evidence.get("attestation")
    recorded_at = (
        _timestamp(attestation.get("recorded_at"), "attestation.recorded_at", errors)
        if isinstance(attestation, dict)
        else None
    )
    completed = [timestamp for timestamp in (dark_end, metrics_end) if timestamp is not None]
    if recorded_at is not None and completed and recorded_at < max(completed):
        errors.append("attestation.recorded_at must follow all observation windows")
    return errors


def validate_phase2_deployment_evidence(
    evidence: Any,
    schema: dict[str, Any],
) -> None:
    """Validate JSON Schema first, then fail closed on cross-field relations."""
    structural = _schema_errors(evidence, schema)
    if structural:
        raise DeploymentEvidenceValidationError(structural)
    assert isinstance(evidence, dict)
    relational = semantic_evidence_errors(evidence)
    if relational:
        raise DeploymentEvidenceValidationError(relational)


def main(argv: list[str] | None = None) -> int:
    """Validate one captured evidence JSON document without echoing its values."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence", type=Path)
    parser.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA)
    args = parser.parse_args(argv)
    try:
        evidence = json.loads(args.evidence.read_text(encoding="utf-8"))
        schema = json.loads(args.schema.read_text(encoding="utf-8"))
        validate_phase2_deployment_evidence(evidence, schema)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        print("Phase 2 deployment evidence could not be read.")
        return 1
    except DeploymentEvidenceValidationError as exc:
        print("Phase 2 deployment evidence is invalid:")
        for error in exc.errors:
            print(f"- {error}")
        return 1
    print("Phase 2 deployment evidence is valid.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
