#!/usr/bin/env python3
"""Generate the deterministic repository half of Phase 2 Wave 0 evidence.

This snapshot is deliberately limited to facts that can be reproduced from a
source checkout. It is not deployment evidence: it contains no live image,
database, dark-load, smoke-test, metric, or rollback result.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = REPO_ROOT / "contracts/evidence/phase2-wave0.repo.json"
FRONTEND_HTML_SMOKE_SPEC = "contracts/evidence/frontend-html-smoke.v1.json"
HTTP_METHODS = frozenset({"delete", "get", "head", "options", "patch", "post", "put", "trace"})
PAGE_FILENAMES = frozenset(
    {
        "page.js",
        "page.jsx",
        "page.ts",
        "page.tsx",
    }
)
DDL_SOURCE_ROOTS = ("apps/backend", "services", "ops/db", "deploy")
DDL_SOURCE_SUFFIXES = frozenset({".cjs", ".js", ".mjs", ".py", ".sql", ".ts"})
SKIPPED_DIRECTORIES = frozenset(
    {
        ".git",
        ".next",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
        "coverage",
        "dist",
        "node_modules",
    }
)
DDL_STATEMENT_RE = re.compile(
    r"\b(?:CREATE\s+(?:UNIQUE\s+)?INDEX|CREATE\s+TABLE|ALTER\s+TABLE)\b",
    re.IGNORECASE,
)

CONFIG_SOURCES = {
    "alerts": "config/alerts.yaml",
    "models": "config/models.yaml",
    "routing": "config/routing.yaml",
}
CONTRACT_SOURCES = {
    "auth_session_v1": "contracts/auth-session-v1.md",
    "control_errors_v1": "contracts/control-errors-v1.md",
    "sse_v1": "contracts/sse-v1.md",
}
RUNTIME_ARTIFACTS = {
    "bundle_lock_v1": "bundle.lock.json",
    "bundle_manifest_v1": "bundle.yaml",
    "environment_contract": "config/environment-contract.yaml",
    "runtime_manifest_v1": "distribution.yaml",
    "runtime_manifest_v2": "distribution.v2.yaml",
}
ROLLBACK_SOURCES = {
    "database_backup_script": "ops/db/backup.sh",
    "database_restore_script": "ops/db/restore.sh",
    "production_deploy_script": "ops/deploy/deploy_production.sh",
    "production_rollback_workflow": ".github/workflows/deploy-rollback.yml",
}
ROLLBACK_COMMAND_TEMPLATES = {
    "database_backup": "bash ops/db/backup.sh --compress",
    "database_restore": ("bash ops/db/restore.sh --backup-dir <backup-directory> --postgres-only"),
    "release_redeploy": (
        "DEPLOY_SHA=<promoted-release-commit> bash ops/deploy/deploy_production.sh"
    ),
}


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()


def _sha256_bytes(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def _raw_file_hash(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _canonical_yaml_hash(path: Path) -> str:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    return _sha256_bytes(_canonical_json_bytes(value))


def _count_by(values: list[str]) -> dict[str, int]:
    return dict(sorted(Counter(values).items()))


def _frontend_route_for_page(page: Path, app_root: Path) -> str:
    route_parts = [
        part
        for part in page.relative_to(app_root).parts[:-1]
        if not (part.startswith("(") and part.endswith(")")) and not part.startswith("@")
    ]
    return f"/{'/'.join(route_parts)}" if route_parts else "/"


def collect_frontend_pages(root: Path) -> list[dict[str, str]]:
    """Return the stable Next.js App Router page inventory."""

    app_root = root / "apps/frontend/src/app"
    pages: list[dict[str, str]] = []
    for page in app_root.rglob("*"):
        if (
            not page.is_file()
            or page.name not in PAGE_FILENAMES
            or any(part in SKIPPED_DIRECTORIES for part in page.parts)
        ):
            continue
        pages.append(
            {
                "route": _frontend_route_for_page(page, app_root),
                "source": page.relative_to(root).as_posix(),
            }
        )
    pages.sort(key=lambda item: (item["route"], item["source"]))
    routes = [item["route"] for item in pages]
    duplicates = sorted(route for route, count in Counter(routes).items() if count > 1)
    if duplicates:
        raise ValueError(f"frontend page routes are ambiguous: {duplicates}")
    return pages


def _load_json(root: Path, relative: str) -> dict[str, Any]:
    return json.loads((root / relative).read_text(encoding="utf-8"))


def _json_contract_hash(root: Path, relative: str) -> str:
    return _raw_file_hash(root / relative)


def _operation_count(openapi: dict[str, Any]) -> int:
    return sum(
        1
        for path_item in openapi["paths"].values()
        for method in path_item
        if method in HTTP_METHODS
    )


def _collect_api_contracts(root: Path) -> dict[str, Any]:
    rewrites_path = "contracts/frontend-backend-routes.v1.json"
    operations_path = "contracts/frontend-backend-operations.v1.json"
    allowlist_path = "contracts/openapi/control-v1.allowlist.json"
    snapshot_path = "contracts/openapi/control-v1.openapi.json"

    rewrites = _load_json(root, rewrites_path)
    operations = _load_json(root, operations_path)
    allowlist = _load_json(root, allowlist_path)
    snapshot = _load_json(root, snapshot_path)
    rewrite_rows = rewrites["routes"]
    operation_rows = operations["operations"]
    allowlist_rows = allowlist["operations"]

    return {
        "control_allowlist": {
            "control_api_version": allowlist["control_api_version"],
            "operation_count": len(allowlist_rows),
            "sha256": _json_contract_hash(root, allowlist_path),
        },
        "control_openapi": {
            "operation_count": _operation_count(snapshot),
            "path_count": len(snapshot["paths"]),
            "sha256": _json_contract_hash(root, snapshot_path),
            "version": snapshot["info"]["version"],
        },
        "frontend_operation_inventory": {
            "by_classification": _count_by(
                [str(operation["classification"]) for operation in operation_rows]
            ),
            "deprecated_count": sum(
                bool(operation.get("deprecated", False)) for operation in operation_rows
            ),
            "operation_count": len(operation_rows),
            "sha256": _json_contract_hash(root, operations_path),
        },
        "frontend_rewrites": {
            "by_classification": _count_by(
                [str(rewrite["classification"]) for rewrite in rewrite_rows]
            ),
            "rewrite_count": len(rewrite_rows),
            "sha256": _json_contract_hash(root, rewrites_path),
        },
    }


def _collect_frontend_html_smoke(
    root: Path,
    frontend_pages: list[dict[str, str]],
) -> dict[str, Any]:
    spec = _load_json(root, FRONTEND_HTML_SMOKE_SPEC)
    routes = spec.get("routes")
    if not isinstance(routes, list) or not routes:
        raise ValueError("frontend HTML smoke spec must contain routes")
    smoke_paths = [route.get("path") for route in routes if isinstance(route, dict)]
    if len(smoke_paths) != len(routes) or not all(isinstance(path, str) for path in smoke_paths):
        raise ValueError("every frontend HTML smoke route must have a string path")
    duplicates = sorted(path for path, count in Counter(smoke_paths).items() if count > 1)
    if duplicates:
        raise ValueError(f"frontend HTML smoke spec has duplicate routes: {duplicates}")

    page_paths = [page["route"] for page in frontend_pages]
    if set(smoke_paths) != set(page_paths):
        missing = sorted(set(page_paths) - set(smoke_paths))
        stale = sorted(set(smoke_paths) - set(page_paths))
        raise ValueError(
            "frontend HTML smoke routes do not match the App Router inventory "
            f"(missing={missing}, stale={stale})"
        )

    access_values = [str(route.get("access")) for route in routes]
    if any(access not in {"protected", "public"} for access in access_values):
        raise ValueError("frontend HTML smoke access must be public or protected")
    return {
        "by_access": _count_by(access_values),
        "evidence_status": spec.get("evidence_status"),
        "route_count": len(routes),
        "sha256": _json_contract_hash(root, FRONTEND_HTML_SMOKE_SPEC),
    }


def scan_ddl_sources(root: Path) -> list[dict[str, Any]]:
    """Find every production source file containing a table/index DDL statement."""

    sources: list[dict[str, Any]] = []
    for source_root in DDL_SOURCE_ROOTS:
        candidate = root / source_root
        if not candidate.is_dir():
            continue
        for path in candidate.rglob("*"):
            if (
                not path.is_file()
                or path.is_symlink()
                or path.suffix.lower() not in DDL_SOURCE_SUFFIXES
                or any(part in SKIPPED_DIRECTORIES for part in path.parts)
            ):
                continue
            try:
                source = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            statement_count = len(DDL_STATEMENT_RE.findall(source))
            if statement_count:
                sources.append(
                    {
                        "path": path.relative_to(root).as_posix(),
                        "sha256": _raw_file_hash(path),
                        "statement_count": statement_count,
                    }
                )
    return sorted(sources, key=lambda item: item["path"])


def ddl_sources_not_fingerprinted(root: Path, baseline: dict[str, Any]) -> list[str]:
    """Return newly detected DDL source paths absent from a checked baseline."""

    detected = {item["path"] for item in scan_ddl_sources(root)}
    recorded = {
        item["path"]
        for item in baseline.get("database_ddl", {}).get("sources", [])
        if isinstance(item, dict) and isinstance(item.get("path"), str)
    }
    return sorted(detected - recorded)


def _collect_database_ddl(root: Path) -> dict[str, Any]:
    sources = scan_ddl_sources(root)
    fingerprint_input = [
        {
            "path": source["path"],
            "sha256": source["sha256"],
        }
        for source in sources
    ]
    return {
        "repo_ddl_source_fingerprint": _sha256_bytes(_canonical_json_bytes(fingerprint_input)),
        "scan_roots": list(DDL_SOURCE_ROOTS),
        "source_count": len(sources),
        "sources": sources,
    }


def _collect_configuration(root: Path) -> dict[str, Any]:
    artifacts: dict[str, Any] = {}
    for artifact_id, relative in sorted(CONFIG_SOURCES.items()):
        source = (root / relative).read_text(encoding="utf-8")
        artifacts[artifact_id] = {
            "canonical_sha256": _canonical_yaml_hash(root / relative),
            "env_reference_count": len(re.findall(r"\$\{[^}]+\}", source)),
            "source": relative,
        }
    return {
        "hash_mode": "yaml-to-canonical-json-with-environment-references-unexpanded",
        "artifacts": artifacts,
    }


def _collect_runtime_artifacts(root: Path) -> dict[str, Any]:
    distribution_root = root / "distributions/freeinference"
    return {
        "hash_mode": "raw-file-bytes",
        "artifacts": {
            artifact_id: {
                "file": relative,
                "sha256": _raw_file_hash(distribution_root / relative),
            }
            for artifact_id, relative in sorted(RUNTIME_ARTIFACTS.items())
        },
    }


def _collect_rollback_controls(root: Path) -> dict[str, Any]:
    return {
        "command_templates": {
            template_id: {
                "sha256": _sha256_bytes(template.encode()),
            }
            for template_id, template in sorted(ROLLBACK_COMMAND_TEMPLATES.items())
        },
        "sources": {
            source_id: {
                "sha256": _raw_file_hash(root / relative),
                "source": relative,
            }
            for source_id, relative in sorted(ROLLBACK_SOURCES.items())
        },
    }


def build_repo_baseline(root: Path = REPO_ROOT) -> dict[str, Any]:
    """Build a deterministic, source-only Phase 2 baseline."""

    frontend_pages = collect_frontend_pages(root)
    return {
        "schema_version": 1,
        "evidence_scope": "repository-only-not-deployment-evidence",
        "frontend_pages": {
            "fingerprint": _sha256_bytes(_canonical_json_bytes(frontend_pages)),
            "page_count": len(frontend_pages),
            "routes": frontend_pages,
        },
        "frontend_html_smoke": _collect_frontend_html_smoke(root, frontend_pages),
        "api_contracts": _collect_api_contracts(root),
        "protocol_contracts": {
            contract_id: {
                "sha256": _raw_file_hash(root / relative),
                "source": relative,
            }
            for contract_id, relative in sorted(CONTRACT_SOURCES.items())
        },
        "configuration": _collect_configuration(root),
        "runtime_distribution_artifacts": _collect_runtime_artifacts(root),
        "database_ddl": _collect_database_ddl(root),
        "rollback_controls": _collect_rollback_controls(root),
    }


def render_repo_baseline(baseline: dict[str, Any]) -> str:
    """Serialize a baseline in the checked repository format."""

    return json.dumps(baseline, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail when the checked-in baseline differs instead of rewriting it",
    )
    args = parser.parse_args(argv)

    root = args.repo_root.resolve()
    output = args.output or root / "contracts/evidence/phase2-wave0.repo.json"
    rendered = render_repo_baseline(build_repo_baseline(root))
    if args.check:
        if output.is_file():
            checked = json.loads(output.read_text(encoding="utf-8"))
            missing_ddl = ddl_sources_not_fingerprinted(root, checked)
            if missing_ddl:
                print("Phase 2 repository baseline omits DDL sources: " + ", ".join(missing_ddl))
                return 1
        if not output.is_file() or output.read_text(encoding="utf-8") != rendered:
            print(
                "Phase 2 repository baseline is stale; run "
                "`uv run python ops/ci/generate_phase2_repo_baseline.py`"
            )
            return 1
        return 0

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(rendered, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
