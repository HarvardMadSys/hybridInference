"""Guard that the H200 DeepSeek profile exposes sglang Prometheus metrics."""

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


def test_deepseek_v4_enables_prometheus_metrics() -> None:
    """``--enable-metrics`` is opt-in in the proxy; this profile must ask for it.

    Metrics land on the node-local backend port (18003 / replica B 18004), not
    the tunnelled proxy listener. A config that drops the key silently stops
    scraping on the next cold start, while the replica still looks healthy.
    """
    profiles = _deepseek_v4_profiles()
    assert profiles, "no DeepSeek V4 profile found in models.json"
    for name, profile in profiles:
        assert profile.get("enable_metrics") is True, (
            f"{name}: enable_metrics must be true so sglang serves /metrics"
        )
