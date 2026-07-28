"""Export every DB-resident runtime override for split-migration triage (C0).

The admin UI can change routing weights, disable providers, and adjust
visibility/concurrency at runtime; those changes live only in Postgres and are
invisible to git. Before ``config/models.yaml`` / ``routing.yaml`` move into
a distribution overlay, each active override must be triaged: incident
leftovers get deleted, long-term intent gets folded into the YAML. This script
produces that triage inventory.

Strictly read-only: it opens its own small pool and never calls the store
``initialize()`` hooks (both ``DatabaseLogger.initialize`` and
``PostgresOperationalStore.initialize`` run DDL by design).

Run from the repo root on the target host (reads ``.env`` like the backend):

    PYTHONPATH=apps/backend uv run python ops/db/export_runtime_overrides.py \
        --out runtime_overrides.$(hostname).json

Without ``--out`` the JSON report goes to stdout. Settings values whose key
looks secret-bearing are redacted defensively; nothing in this report should
ever contain a credential, but the export may be shared during triage.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import sys
from datetime import UTC, datetime
from typing import Any

# Keys in the site_settings table carrying per-model RouteWise tuning; split
# out so the triage report groups them apart from generic feature flags.
# Mirrors serving.config.routewise_model_settings.MODEL_ROUTEWISE_SETTING_PREFIX.
ROUTEWISE_SETTING_PREFIX = "model_routewise_setting:"

_SENSITIVE_KEY_MARKERS = ("key", "token", "secret", "password", "credential")

# Section name -> OperationalStore method. Every method is a plain SELECT.
SECTIONS: dict[str, str] = {
    "site_settings": "list_settings",
    "model_visibility_overrides": "list_model_visibility_overrides",
    "disabled_providers": "list_disabled_providers",
    "model_concurrency_exemptions": "list_model_concurrency_exemptions",
    "weight_overrides": "list_all_weight_overrides",
    "provider_definitions": "list_provider_definitions",
}


def _jsonable(value: Any) -> Any:
    """Convert store rows (dicts or dataclasses) into JSON-friendly values."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {k: _jsonable(v) for k, v in dataclasses.asdict(value).items()}
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def redact_sensitive_settings(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Mask setting values whose key looks like it could carry a credential."""
    redacted: list[dict[str, Any]] = []
    for row in rows:
        key = str(row.get("key", ""))
        if any(marker in key.lower() for marker in _SENSITIVE_KEY_MARKERS):
            row = {**row, "value": "<redacted>"}
        redacted.append(row)
    return redacted


def split_routewise_settings(
    rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split site_settings rows into (routewise per-model, everything else)."""
    routewise = [r for r in rows if str(r.get("key", "")).startswith(ROUTEWISE_SETTING_PREFIX)]
    other = [r for r in rows if not str(r.get("key", "")).startswith(ROUTEWISE_SETTING_PREFIX)]
    return routewise, other


async def collect_sections(store: Any) -> dict[str, Any]:
    """Read every override section, capturing per-section errors.

    A section that fails (e.g. a table that does not exist on this
    environment) is reported as ``{"error": ...}`` instead of aborting the
    whole export — the point of the tool is the inventory, not strictness.
    """
    sections: dict[str, Any] = {}
    for name, method in SECTIONS.items():
        try:
            rows = await getattr(store, method)()
            sections[name] = [_jsonable(row) for row in rows]
        except Exception as exc:
            sections[name] = {"error": f"{type(exc).__name__}: {exc}"}
    return sections


def build_report(sections: dict[str, Any], *, database: str) -> dict[str, Any]:
    """Assemble the final report with counts and the routewise split."""
    site_settings = sections.get("site_settings")
    if isinstance(site_settings, list):
        routewise, other = split_routewise_settings(site_settings)
        sections = {
            **sections,
            "site_settings": redact_sensitive_settings(other),
            "routewise_model_settings": redact_sensitive_settings(routewise),
        }
    counts = {
        name: (len(rows) if isinstance(rows, list) else "error") for name, rows in sections.items()
    }
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "database": database,
        "counts": counts,
        "sections": sections,
    }


async def _run(out_path: str | None) -> int:
    # Imports that need PYTHONPATH=apps/backend and a live environment stay
    # inside the entrypoint so unit tests can import the helpers above freely.
    import asyncpg

    from serving.config.settings import Settings
    from serving.storage.postgres_operational import PostgresOperationalStore

    settings = Settings()
    database = f"{settings.db_user}@{settings.db_host}:{settings.db_port}/{settings.db_name}"
    pool = await asyncpg.create_pool(
        host=settings.db_host,
        port=settings.db_port,
        database=settings.db_name,
        user=settings.db_user,
        password=settings.db_password,
        min_size=1,
        max_size=2,
        command_timeout=60,
    )
    try:
        # Deliberately no store.initialize(): that path runs DDL.
        store = PostgresOperationalStore(pool)
        sections = await collect_sections(store)
    finally:
        await pool.close()

    report = build_report(sections, database=database)
    payload = json.dumps(report, indent=2, ensure_ascii=False, default=str)
    if out_path:
        with open(out_path, "w", encoding="utf-8") as handle:
            handle.write(payload + "\n")
        print(f"wrote {out_path} ({report['counts']})")
    else:
        print(payload)
    return 0


def main() -> int:
    """Parse arguments and run the export."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", default=None, help="write the JSON report here (default: stdout)")
    args = parser.parse_args()
    return asyncio.run(_run(args.out))


if __name__ == "__main__":
    sys.exit(main())
