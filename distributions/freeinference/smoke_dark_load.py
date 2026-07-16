r"""Container smoke for the overlay dark-load path.

Run inside the backend container (or from the repo root):

    docker compose -f deploy/docker/docker-compose.yml exec backend \\
        python distributions/freeinference/smoke_dark_load.py

Asserts what a silent fail-open would hide: the manifest is actually
visible and valid from this filesystem, and every configured path compares
byte-identical to the effective legacy path. Exits non-zero on any failure,
so "deploy succeeded" can never be mistaken for "dark-load verified".
"""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

_DEFAULT_MANIFEST = Path(__file__).resolve().parent / "distribution.yaml"


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


def main() -> int:
    """Load the manifest and verify all three dark comparisons are identical."""
    os.environ.setdefault("DISTRIBUTION_CONFIG_PATH", str(_DEFAULT_MANIFEST))
    os.environ.setdefault("DISTRIBUTION_CONFIG_MODE", "dark")

    from serving.config.distribution import (
        get_distribution_config,
        resolve_config_path,
    )
    from serving.config.settings import get_settings

    get_settings.cache_clear()
    get_distribution_config.cache_clear()

    manifest_path = os.environ["DISTRIBUTION_CONFIG_PATH"]
    config = get_distribution_config()
    if config is None:
        print(
            f"FAIL: manifest at {manifest_path!r} did not load — missing mount "
            "or invalid file (loader fails open, so the service would still "
            "start WITHOUT any dark comparison)."
        )
        return 2

    failures = 0
    for kind in ("models", "routing", "alerts"):
        manifest_value = getattr(config.paths, kind)
        if not manifest_value:
            print(f"SKIP {kind}: manifest declares no path")
            continue
        effective = resolve_config_path(kind).path
        manifest = Path(manifest_value)
        if not (effective.exists() and manifest.exists()):
            print(f"FAIL {kind}: effective={effective} manifest={manifest} (missing file)")
            failures += 1
            continue
        eff_digest, man_digest = _digest(effective), _digest(manifest)
        verdict = "identical" if eff_digest == man_digest else "DIFFERENT"
        print(f"{kind}: effective={effective} manifest={manifest} -> {verdict}")
        if verdict != "identical":
            failures += 1

    if failures:
        print(f"FAIL: {failures} path(s) not identical")
        return 1
    print(f"OK: manifest {config.distribution.id!r} loaded; all comparisons identical")
    return 0


if __name__ == "__main__":
    sys.exit(main())
