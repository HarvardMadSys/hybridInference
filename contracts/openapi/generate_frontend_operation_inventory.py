#!/usr/bin/env python3
"""Expand frontend backend rewrites into an exact OpenAPI operation inventory."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from serving.servers.app import create_app

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_REWRITES = REPO_ROOT / "contracts/frontend-backend-routes.v1.json"
DEFAULT_OUTPUT = REPO_ROOT / "contracts/frontend-backend-operations.v1.json"
HTTP_METHODS = frozenset({"delete", "get", "head", "options", "patch", "post", "put", "trace"})


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def rewrite_covers_path(source: str, path: str) -> bool:
    """Return whether a Next rewrite source covers an OpenAPI path."""
    if source.endswith("/:path*"):
        base = source.removesuffix("/:path*")
        return path == base or path.startswith(f"{base}/")
    return path == source


def generate_inventory(
    openapi: dict[str, Any],
    rewrite_inventory: dict[str, Any],
) -> dict[str, Any]:
    """Return every registered operation reachable through a frontend rewrite."""
    rewrites = rewrite_inventory["routes"]
    operations: list[dict[str, Any]] = []

    for path, path_item in sorted(openapi["paths"].items()):
        matching_rewrites = [
            rewrite for rewrite in rewrites if rewrite_covers_path(rewrite["source"], path)
        ]
        if len(matching_rewrites) > 1:
            sources = [rewrite["source"] for rewrite in matching_rewrites]
            raise ValueError(f"OpenAPI path {path!r} matches overlapping rewrites: {sources}")
        if not matching_rewrites:
            continue

        rewrite = matching_rewrites[0]
        for method, operation in sorted(path_item.items()):
            if method not in HTTP_METHODS:
                continue
            item = {
                "path": path,
                "method": method,
                "deprecated": bool(operation.get("deprecated", False)),
                "classification": rewrite["classification"],
                "rewrite_source": rewrite["source"],
            }
            if rewrite["classification"] == "stable-control":
                operation_id = operation.get("operationId")
                if not isinstance(operation_id, str) or not operation_id:
                    raise ValueError(f"{method.upper()} {path} has no operationId")
                item["operation_id"] = operation_id
            operations.append(item)

    return {
        "schema_version": 1,
        "source_files": [
            rewrite_inventory["source_file"],
            "contracts/frontend-backend-routes.v1.json",
            "FastAPI openapi.json",
        ],
        "operations": operations,
    }


def render_inventory(inventory: dict[str, Any]) -> str:
    """Serialize the generated inventory in canonical repository format."""
    return json.dumps(inventory, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main() -> int:
    """Generate or verify the checked-in frontend operation inventory."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rewrites", type=Path, default=DEFAULT_REWRITES)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail when the checked-in inventory differs instead of rewriting it",
    )
    args = parser.parse_args()

    rewrites = _load_json(args.rewrites)
    rendered = render_inventory(generate_inventory(create_app().openapi(), rewrites))
    if args.check:
        if not args.output.is_file() or args.output.read_text(encoding="utf-8") != rendered:
            raise SystemExit(
                "frontend backend operation inventory is stale; run "
                "`uv run python contracts/openapi/generate_frontend_operation_inventory.py`"
            )
        return 0

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(rendered, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
