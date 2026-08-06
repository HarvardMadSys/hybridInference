"""Guard the H200 DeepSeek config against HiCache flags sglang will reject.

sglang's DeepSeek V4 HiCache path raises

    ValueError: DeepSeek V4 HiCache currently does not support --hicache-size;
                use --hicache-ratio instead

at scheduler init, so a config carrying ``hicache_size`` cannot start this model
at all. What makes it worth a test is *when* the breakage surfaces: a running
container keeps serving on the flags it launched with, so the node looks healthy
while the config on disk can no longer boot. The failure appears on the next cold
start, which the idle proxy performs unattended after its idle timeout.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODELS_JSON = ROOT / "ops" / "h200_idle_proxy" / "models.json"


def _deepseek_v4_profiles() -> list[tuple[str, dict]]:
    config = json.loads(MODELS_JSON.read_text())
    return [
        (name, profile)
        for name, profile in config.items()
        if "deepseek-v4" in str(profile.get("hf_repo", "")).lower() or "deepseek-v4" in name.lower()
    ]


def test_deepseek_v4_does_not_use_hicache_size() -> None:
    """``hicache_size`` is rejected outright for this architecture."""
    profiles = _deepseek_v4_profiles()
    assert profiles, "no DeepSeek V4 profile found in models.json"
    for name, profile in profiles:
        assert "hicache_size" not in profile, (
            f"{name}: sglang rejects --hicache-size for DeepSeek V4; use hicache_ratio instead"
        )


def test_deepseek_v4_hicache_ratio_fits_four_ranks() -> None:
    """The ratio is per scheduler process, and this box runs four of them.

    Two TP=2 replicas means four ranks, each pinning ``ratio x device_pool`` GB of
    unswappable host DRAM. The device KV pool is about 34 GB per rank (a 115 GB
    static budget at mem_fraction 0.80, less ~78 GB of TP=2 weights), and the box
    has 1507 GB of RAM, so the four ranks together must stay well under that --
    DeepSeek V4 also builds paged/state/indexer host pools the ratio does not
    cover.
    """
    device_pool_gb = 34
    ranks_per_box = 4
    ram_gb = 1507

    for name, profile in _deepseek_v4_profiles():
        ratio = profile.get("hicache_ratio")
        if ratio is None:
            continue
        total_gb = ratio * device_pool_gb * ranks_per_box
        assert total_gb <= ram_gb * 0.6, (
            f"{name}: hicache_ratio {ratio} asks for ~{total_gb} GB across "
            f"{ranks_per_box} ranks, too close to the box's {ram_gb} GB"
        )
