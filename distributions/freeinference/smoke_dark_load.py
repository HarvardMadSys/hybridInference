r"""Answer whether making the manifest the source of truth would change anything.

Run inside the backend container, or from a checkout with the deployment's
environment loaded::

    docker compose -f deploy/docker/docker-compose.yml exec backend \
        python distributions/freeinference/smoke_dark_load.py

    PYTHONPATH=apps/backend python distributions/freeinference/smoke_dark_load.py

This replaces a dark-load smoke that compared each overlay file against a
legacy copy at the repository root. Those copies are gone — the move happened —
so that comparison now reads a file against itself and proves nothing.

What is still undecided is the cutover. ``resolve_config_path`` applies
``env > manifest > legacy default``, and the env branch wins **even in active
mode**:

    if env_value:
        ... "explicit env override wins over manifest value"
        return effective

So setting ``DISTRIBUTION_CONFIG_MODE=active`` while ``deploy/backend.env``
still exports MODELS/ROUTING/ALERTS_CONFIG_PATH changes nothing at all. The
manifest only becomes the source of truth when those variables are *removed*,
and that is safe exactly when it resolves to the same files they name today.

This script reports that, per file, against whatever environment it inherits.
It supplies nothing itself: a container that was never configured must fail
rather than pass on a default the script quietly injected.

Exit codes: 0 the cutover is a no-op; 1 it would change what is served;
2 the question could not be answered.
"""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

_KINDS = ("models", "routing", "alerts")
_OVERLAY_MANIFEST = Path(__file__).resolve().parent / "distribution.yaml"


def _env(name: str) -> str:
    """Read NAME case-insensitively, the way pydantic Settings does."""
    for key, value in os.environ.items():
        if key.upper() == name:
            return value
    return ""


def _digest(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:12]
    except OSError:
        return ""


def main() -> int:
    """Compare what the environment supplies against what the manifest would."""
    configured = _env("DISTRIBUTION_CONFIG_PATH")
    if not configured:
        print(
            "FAIL: DISTRIBUTION_CONFIG_PATH is not set. Without it the manifest "
            "is not loaded at all, so there is nothing to compare against — set "
            "it in the repo-root .env, which compose passes via env_file."
        )
        return 2
    if Path(configured).resolve() != _OVERLAY_MANIFEST:
        print(
            f"FAIL: DISTRIBUTION_CONFIG_PATH={configured!r} does not point at "
            f"this overlay's manifest ({_OVERLAY_MANIFEST})."
        )
        return 2

    from serving.config.distribution import get_distribution_config
    from serving.config.settings import get_settings

    get_settings.cache_clear()
    get_distribution_config.cache_clear()

    config = get_distribution_config()
    if config is None:
        print(
            f"FAIL: the manifest at {configured!r} did not load — a missing "
            "mount or an invalid file. The loader fails open, so the service "
            "would start with no manifest and this check would be the only "
            "thing that noticed."
        )
        return 2

    print(f"manifest: {configured}")
    print(f"mode:     {_env('DISTRIBUTION_CONFIG_MODE') or '<unset>'}\n")

    changes = 0
    for kind in _KINDS:
        env_value = _env(f"{kind.upper()}_CONFIG_PATH")
        manifest_value = getattr(config.paths, kind, "") or ""

        if not env_value and not manifest_value:
            print(f"{kind:8} neither env nor manifest names a file — no change")
            continue
        if not env_value:
            print(f"{kind:8} env unset; manifest would supply {manifest_value} — no change")
            continue
        if not manifest_value:
            print(
                f"{kind:8} CHANGE: env supplies {env_value}, manifest names "
                "nothing — removing the variable would fall back to the "
                "built-in default"
            )
            changes += 1
            continue

        env_path, man_path = Path(env_value), Path(manifest_value)
        if env_path.resolve() == man_path.resolve():
            print(f"{kind:8} same file ({env_value}) — no change")
            continue

        env_digest, man_digest = _digest(env_path), _digest(man_path)
        if not env_digest or not man_digest:
            missing = env_path if not env_digest else man_path
            print(f"{kind:8} FAIL: cannot read {missing}")
            changes += 1
            continue
        if env_digest == man_digest:
            print(f"{kind:8} different paths, identical content — no change")
            continue
        print(
            f"{kind:8} CHANGE: env {env_value} ({env_digest}) differs from "
            f"manifest {manifest_value} ({man_digest})"
        )
        changes += 1

    if changes:
        print(
            f"\n{changes} file(s) would change. Removing MODELS/ROUTING/"
            "ALERTS_CONFIG_PATH from deploy/backend.env is NOT a no-op here."
        )
        return 1
    print(
        "\nThe cutover is a no-op: every file the environment supplies is the "
        "one the manifest names. Removing those variables from "
        "deploy/backend.env leaves what is served unchanged."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
