"""Guard the H200 DeepSeek image against the streaming tool-call regression."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODELS_JSON = ROOT / "ops" / "h200_idle_proxy" / "models.json"

# Docker Hub manifest for nightly-dev-20260818-c0b6474b. That source commit is
# a descendant of SGLang 5899674, which fixes the DeepSeek-V4 streaming parser
# dropping buffered prose before a DSML tool call (hybridInference #1293).
PARSER_FIXED_IMAGE = (
    "lmsysorg/sglang:nightly-dev-20260818-c0b6474b@"
    "sha256:51e576f02368480c055c7aadb67590d82b172e2392123ce4cf4cc8251b2d8caf"
)


def test_deepseek_v4_image_contains_streaming_tool_call_fix() -> None:
    """Keep every H200 DeepSeek-V4 profile on the parser-fixed image."""
    config = json.loads(MODELS_JSON.read_text())
    profiles = {
        name: profile
        for name, profile in config.items()
        if "deepseek-v4" in name.lower() or "deepseek-v4" in str(profile.get("hf_repo", "")).lower()
    }

    assert profiles, "no DeepSeek-V4 profile found in models.json"
    for name, profile in profiles.items():
        assert profile.get("sglang_image") == PARSER_FIXED_IMAGE, (
            f"{name}: image must contain SGLang 5899674; v0.5.17 drops "
            "streamed prose before DeepSeek-V4 tool calls"
        )
