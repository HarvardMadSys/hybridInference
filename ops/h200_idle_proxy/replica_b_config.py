#!/usr/bin/env python3
"""Derive replica B's ``models.json`` from replica A's, at unit start.

Why this exists rather than a second checked-in JSON
----------------------------------------------------
A 4xH200 box now runs **two** TP=2 DeepSeek replicas rather than one TP=4
instance, because TP=4 scales at only ~0.71 efficiency on these cards: measured
on h200a, one TP=2 replica does 4,215 decode tok/s at c=128 against TP=4's
6,014, so two of them project to ~8,430 (+40%). Two replicas need two proxy
instances, and therefore two configs that differ in exactly four keys.

Checking in that second config is the one thing this directory's README warns
against: ``models.h200.json`` used to mirror this deployment and drifted (FP8 at
PP=3 on GPUs 0,2,3 on one side, NVFP4 at TP=2 on the other) until #1185
reconverged them, and #1187's ``mem_fraction`` change *still* had to be applied
by hand twice. A replica that is a near-duplicate of its sibling drifts the same
way, and the failure is silent: the two replicas serve the same model id behind
one gateway route set, so a divergence shows up as unexplained latency skew
between them, not as an error.

So replica B carries no config of its own. It derives one from A's at every unit
start (``ExecStartPre``), overriding only the four keys that *must* differ, and
writes it to a tmpfs path. Edit ``models.json`` and restart; B follows.

The four keys, and why each must differ:

``container``      Docker names are unique per host. Sharing one would have the
                   two proxies destroying and re-creating each other's backend.
``backend_port``   The published host port. Sharing one makes the second
                   ``docker run`` fail on an address already in use.
``gpu_index``      A pins 2,3 and B pins 0,1. Both are pinned rather than
                   auto-selected: GPU auto-selection consults the containers a
                   *single* proxy manages, so two proxies would both pick the
                   same free pair.
``cache_dir``      The DeepGEMM/JIT cache. The two replicas compile identical
                   TP=2 kernels, so sharing the directory is tempting, but they
                   cold-start concurrently and would race on the same files.
                   Seed B's from A's to keep the warm start (see install.sh).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# Only these keys differ from replica A. Everything that governs *how the model
# runs* -- tensor_parallel_size, mem_fraction, max_model_len, the DSpark and
# marlin settings -- is inherited, which is the whole point.
OVERRIDES = {
    "container": "deepseek-v4-flash-sglang-b",
    "backend_port": 18004,
    "gpu_index": "0,1",
    "cache_dir": "/var/tmp/sglang-cache/deepseek-v4-flash-0731-b",
}


def derive(source: dict) -> dict:
    """Return ``source`` with replica B's overrides applied to every model."""
    if not source:
        raise ValueError("source config defines no models")
    out = {}
    for name, profile in source.items():
        merged = dict(profile)
        merged.update(OVERRIDES)
        # A pinned gpu_index is load-bearing (see module docstring); refuse to
        # emit a config that would let two proxies auto-select the same pair.
        if not merged.get("gpu_index"):
            raise ValueError(f"{name}: refusing to emit replica config with no gpu_index")
        out[name] = merged
    return out


def main() -> int:
    """Derive replica B's config from replica A's and write it to ``--out``."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        default=str(Path(__file__).resolve().parent / "models.json"),
        help="replica A's config (default: models.json beside this script)",
    )
    parser.add_argument(
        "--out",
        required=True,
        help="path to write the derived config to (a tmpfs path under /run)",
    )
    args = parser.parse_args()

    try:
        source = json.loads(Path(args.source).read_text())
    except (OSError, json.JSONDecodeError) as exc:
        print(f"ERROR: cannot read source config {args.source}: {exc}", file=sys.stderr)
        return 1

    try:
        derived = derive(source)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    # Write-then-rename: the proxy may be starting concurrently on a restart, and
    # must never read a half-written config.
    tmp = out.with_suffix(out.suffix + ".tmp")
    tmp.write_text(json.dumps(derived, indent=4) + "\n")
    os.replace(tmp, out)
    print(f"derived replica B config for {sorted(derived)} -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
