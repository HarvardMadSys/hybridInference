"""Guard the H200 DeepSeek HiCache flags sglang will actually boot with.

sglang's DeepSeek V4 HiCache path raises

    ValueError: DeepSeek V4 HiCache currently does not support --hicache-size;
                use --hicache-ratio instead

at scheduler init, so a config carrying ``hicache_size`` cannot start this model
at all. What makes it worth a test is *when* the breakage surfaces: a running
container keeps serving on the flags it launched with, so the node looks healthy
while the config on disk can no longer boot. The failure appears on the next cold
start, which the idle proxy performs unattended after its idle timeout.

HiCache is on for this profile, on the parser-fixed nightly that attaches the
DSpark draft pool (``hicache_attached=True``). v0.5.17 skipped that pool and
wedged the scheduler; do not combine these keys with that image.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODELS_JSON = ROOT / "ops" / "h200_idle_proxy" / "models.json"

# Measured on h200b against the pinned nightly: ratio 5 is ~170 GB/rank of host
# DRAM and fits two TP=2 replicas (four ranks) on 1507 GB with headroom.
EXPECTED_HICACHE_RATIO = 5
EXPECTED_HICACHE_WRITE_POLICY = "write_through_selective"


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


def test_deepseek_v4_hicache_on_with_dspark() -> None:
    """Keep HiCache on alongside DSpark, sized the way this nightly boots.

    On sglang v0.5.17 the tree cache came up hierarchical and then logged

        Draft pool type DeepSeekV4TokenToKVPool not supported for HiCache, skipping.

    so the target KV pool was written back to host DRAM while the speculative
    draft pool was not tracked. Under ordinary traffic both TP ranks then
    stopped mid-decode and the scheduler watchdog SIGQUIT'd the server
    (~20 min into serving, twice each, on both h200a replicas, 2026-08-06).

    The pinned nightly (``c0b6474b``) attaches the draft pool
    (``hicache_attached=True``) and no longer emits that skip. Measured on
    h200b 2026-08-22: overflowed shared-prefix replay median TTFT dropped
    61 s → 4.6 s, single-stream DSpark ITL stayed 1.85 ms. Any hicache key
    turns the feature on -- ``_hicache_args`` emits
    ``--enable-hierarchical-cache`` as soon as one is present -- so the
    sizing keys have to be the ones this architecture accepts.
    """
    profiles = _deepseek_v4_profiles()
    assert profiles, "no DeepSeek V4 profile found in models.json"
    for name, profile in profiles:
        assert profile.get("speculative_algorithm") == "DSPARK" or profile.get("mtp"), (
            f"{name}: expected DSpark on this profile"
        )
        assert profile.get("hicache_ratio") == EXPECTED_HICACHE_RATIO, (
            f"{name}: hicache_ratio must be {EXPECTED_HICACHE_RATIO} "
            f"(got {profile.get('hicache_ratio')!r})"
        )
        assert profile.get("hicache_write_policy") == EXPECTED_HICACHE_WRITE_POLICY, (
            f"{name}: hicache_write_policy must be {EXPECTED_HICACHE_WRITE_POLICY!r} "
            f"(got {profile.get('hicache_write_policy')!r})"
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

    profiles = _deepseek_v4_profiles()
    assert profiles, "no DeepSeek V4 profile found in models.json"
    for name, profile in profiles:
        ratio = profile.get("hicache_ratio")
        assert ratio is not None, f"{name}: hicache_ratio must be set"
        total_gb = ratio * device_pool_gb * ranks_per_box
        assert total_gb <= ram_gb * 0.6, (
            f"{name}: hicache_ratio {ratio} asks for ~{total_gb} GB across "
            f"{ranks_per_box} ranks, too close to the box's {ram_gb} GB"
        )
