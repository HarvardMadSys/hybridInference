"""Tests for ops/h200_idle_proxy/replica_b_config.py.

Replica B derives its config from replica A's instead of shipping a copy, because
a checked-in near-duplicate drifts -- that is what happened to the per-node mirror
config this directory's ops README describes. These tests pin the two properties
that make the derivation safe: the four colliding keys are overridden, and
*nothing else* is.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "ops" / "h200_idle_proxy" / "replica_b_config.py"
MODELS_JSON = ROOT / "ops" / "h200_idle_proxy" / "models.json"


def _load_module():
    spec = importlib.util.spec_from_file_location("replica_b_config", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


replica_b_config = _load_module()


def test_derive_overrides_only_the_colliding_keys() -> None:
    """Everything that governs how the model runs must be inherited from A.

    A replica that quietly diverged on tensor_parallel_size or mem_fraction would
    serve the same model id at a different speed behind one route set, which shows
    up as unexplained latency skew rather than an error.
    """
    source = json.loads(MODELS_JSON.read_text())
    derived = replica_b_config.derive(source)

    assert set(derived) == set(source), "derivation must not add or drop models"

    for name, profile in derived.items():
        original = source[name]
        changed = {k for k in profile if profile[k] != original.get(k)}
        assert changed == set(replica_b_config.OVERRIDES), (
            f"{name}: replica B diverged on unexpected keys: "
            f"{changed - set(replica_b_config.OVERRIDES)}"
        )


def test_derive_does_not_collide_with_replica_a() -> None:
    """The three collidable resources must differ from A's, or the two fight."""
    source = json.loads(MODELS_JSON.read_text())
    derived = replica_b_config.derive(source)

    for name, profile in derived.items():
        a = source[name]
        assert profile["container"] != a["container"], f"{name}: container name collides"
        assert profile["backend_port"] != a["backend_port"], f"{name}: backend_port collides"
        assert profile["gpu_index"] != a["gpu_index"], f"{name}: gpu_index collides"
        # Sharing the JIT cache dir would have both replicas racing on the same
        # files when they cold-start together.
        assert profile["cache_dir"] != a.get("cache_dir"), f"{name}: cache_dir collides"


def test_derive_pins_two_gpus_disjoint_from_replica_a() -> None:
    """B pins GPUs 0,1 and A pins 2,3 — the two sets must not overlap."""
    source = json.loads(MODELS_JSON.read_text())
    derived = replica_b_config.derive(source)

    for name, profile in derived.items():
        b_gpus = set(str(profile["gpu_index"]).split(","))
        a_gpus = set(str(source[name]["gpu_index"]).split(","))
        assert not (b_gpus & a_gpus), f"{name}: replica B shares GPUs {b_gpus & a_gpus} with A"
        assert len(b_gpus) == int(profile["tensor_parallel_size"]), (
            f"{name}: pinned GPU count must equal tensor_parallel_size"
        )


def test_derive_rejects_a_config_with_no_models() -> None:
    with pytest.raises(ValueError, match="no models"):
        replica_b_config.derive({})


def test_derive_refuses_to_emit_an_unpinned_gpu_index() -> None:
    """An empty gpu_index would let both proxies auto-select the same free pair."""
    source = {"m": {"gpu_index": "2,3", "tensor_parallel_size": 2}}
    overrides = dict(replica_b_config.OVERRIDES)
    replica_b_config.OVERRIDES["gpu_index"] = ""
    try:
        with pytest.raises(ValueError, match="gpu_index"):
            replica_b_config.derive(source)
    finally:
        replica_b_config.OVERRIDES.clear()
        replica_b_config.OVERRIDES.update(overrides)
