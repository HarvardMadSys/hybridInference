r"""Container smoke for the overlay dark-load path.

Run inside the backend container:

    docker compose -f deploy/docker/docker-compose.yml exec backend \
        python distributions/freeinference/smoke_dark_load.py

(From a source checkout: PYTHONPATH=apps/backend python distributions/...)

The script validates the *inherited* configuration and supplies nothing
itself — a container that was never configured must FAIL, not pass on
defaults the smoke quietly injected:

1. DISTRIBUTION_CONFIG_PATH must be set and point at this overlay's
   manifest;
2. DISTRIBUTION_CONFIG_MODE must be explicitly ``dark`` (an ``active``
   container would trivially compare the manifest against itself);
3. the manifest must actually load from this filesystem (a missing bind
   mount otherwise fails open and boots the service with no comparison);
4. every configured path must compare byte-identical to the effective
   legacy path.

Exits non-zero on any failure, so "deploy succeeded" can never be mistaken
for "dark-load verified".
"""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

_EXPECTED_MANIFEST = Path(__file__).resolve().parent / "distribution.yaml"


def _env(name: str) -> str:
    for key, value in os.environ.items():
        if key.upper() == name:
            return value
    return ""


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


def main() -> int:
    """Verify the inherited dark-load configuration end to end."""
    configured = _env("DISTRIBUTION_CONFIG_PATH")
    if not configured:
        print(
            "FAIL: DISTRIBUTION_CONFIG_PATH is not set in this environment — "
            "the overlay is not configured at all (put it in the repo-root "
            ".env; compose passes it via env_file)."
        )
        return 2
    if Path(configured).resolve() != _EXPECTED_MANIFEST:
        print(
            f"FAIL: DISTRIBUTION_CONFIG_PATH={configured!r} does not point at "
            f"this overlay's manifest ({_EXPECTED_MANIFEST})."
        )
        return 2
    mode = _env("DISTRIBUTION_CONFIG_MODE").strip().lower()
    if mode != "dark":
        print(
            f"FAIL: DISTRIBUTION_CONFIG_MODE={mode or '<unset>'!r} — this smoke "
            "verifies the dark-load state and requires an explicit 'dark' "
            "(in 'active' the manifest would trivially compare against itself)."
        )
        return 2

    from serving.config.distribution import (
        get_distribution_config,
        resolve_config_path,
    )
    from serving.config.settings import get_settings

    get_settings.cache_clear()
    get_distribution_config.cache_clear()

    config = get_distribution_config()
    if config is None:
        print(
            f"FAIL: manifest at {configured!r} did not load — missing mount or "
            "invalid file (the loader fails open, so the service would still "
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
