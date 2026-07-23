"""Validate detached distribution runtime manifests and source bundles."""

from __future__ import annotations

import argparse
import json
import sys
from enum import IntEnum
from pathlib import Path
from typing import TYPE_CHECKING

from serving.config.distribution import (
    DistributionConfigError,
    DistributionEnvironmentError,
    DistributionLockError,
    DistributionPathError,
    DistributionSchemaError,
    DistributionSemanticError,
    DistributionStartupError,
    validate_distribution_bundle_manifest,
    validate_distribution_runtime_manifest,
)

if TYPE_CHECKING:
    from collections.abc import Sequence


class ValidationExitCode(IntEnum):
    """Stable process exit codes for CI and packaging automation."""

    OK = 0
    SCHEMA = 10
    PATH = 11
    SEMANTIC = 12
    ENVIRONMENT = 13
    LOCK = 14


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hybridinference-distribution",
        description="Validate distribution artifacts without reading runtime secrets.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    runtime = subparsers.add_parser(
        "runtime-validate",
        help="validate one runtime manifest and every declared resource",
    )
    runtime.add_argument("manifest", type=Path)
    runtime.add_argument(
        "--root",
        type=Path,
        help="closed validation root; relative manifest paths resolve beneath it",
    )
    runtime.add_argument(
        "--strict",
        action="store_true",
        help="require the Phase 2 schema_version 2 runtime contract",
    )

    bundle = subparsers.add_parser(
        "bundle-validate",
        help="validate a bundle, runtime, environment contract, resources, and lock",
    )
    bundle.add_argument("bundle", type=Path)
    bundle.add_argument(
        "--root",
        type=Path,
        help="closed validation root; relative bundle and lock paths resolve beneath it",
    )
    bundle.add_argument(
        "--lock",
        type=Path,
        help="lock path relative to --root (defaults to bundle.lock.json beside the bundle)",
    )
    bundle.add_argument(
        "--strict",
        action="store_true",
        help="require the bundled runtime to use schema_version 2",
    )
    return parser


def _error_category(exc: DistributionConfigError) -> tuple[str, ValidationExitCode]:
    if isinstance(exc, DistributionLockError):
        return "lock", ValidationExitCode.LOCK
    if isinstance(exc, DistributionEnvironmentError):
        return "environment", ValidationExitCode.ENVIRONMENT
    if isinstance(exc, DistributionPathError):
        return "path", ValidationExitCode.PATH
    if isinstance(exc, (DistributionSemanticError, DistributionStartupError)):
        return "semantic", ValidationExitCode.SEMANTIC
    if isinstance(exc, DistributionSchemaError):
        return "schema", ValidationExitCode.SCHEMA
    return "schema", ValidationExitCode.SCHEMA


def _emit(payload: dict[str, object], *, stream) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True), file=stream)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the distribution validator and return a stable process exit code."""
    args = _parser().parse_args(argv)
    try:
        if args.command == "runtime-validate":
            runtime = validate_distribution_runtime_manifest(
                args.manifest,
                root=args.root,
                require_v2=args.strict,
            )
            payload = {
                "artifact": "runtime",
                "distribution_id": runtime.distribution.id,
                "schema_version": runtime.schema_version,
                "status": "valid",
                "strict": args.strict,
            }
        else:
            bundle = validate_distribution_bundle_manifest(
                args.bundle,
                root=args.root,
                lock_path=args.lock,
                require_runtime_v2=args.strict,
            )
            payload = {
                "artifact": "bundle",
                "bundle_schema_version": bundle.bundle_schema_version,
                "status": "valid",
                "strict": args.strict,
            }
    except DistributionConfigError as exc:
        category, exit_code = _error_category(exc)
        _emit(
            {
                "category": category,
                "error": str(exc),
                "exit_code": int(exit_code),
                "status": "invalid",
            },
            stream=sys.stderr,
        )
        return int(exit_code)

    _emit(payload, stream=sys.stdout)
    return int(ValidationExitCode.OK)


if __name__ == "__main__":
    raise SystemExit(main())
