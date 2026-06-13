"""Tests for registry loading and target resolution."""

from __future__ import annotations

from pathlib import Path

from status_monitor.config import AppConfig, E2EModelOverride, RegistryConfig
from status_monitor.registry import load_model_ids
from status_monitor.scheduler import resolve_targets


def _write_registry(tmp_path: Path) -> Path:
    path = tmp_path / "models.yaml"
    path.write_text(
        """
models:
  - id: glm-4.7
    name: GLM
  - id: minimax-m2.5
    name: Minimax
  - name: no-id-should-be-skipped
""",
        encoding="utf-8",
    )
    return path


def test_load_model_ids(tmp_path: Path) -> None:
    path = _write_registry(tmp_path)
    assert load_model_ids(path) == ["glm-4.7", "minimax-m2.5"]


def test_load_model_ids_missing_file(tmp_path: Path) -> None:
    assert load_model_ids(tmp_path / "nope.yaml") == []


def test_resolve_targets_merges_overrides(tmp_path: Path) -> None:
    path = _write_registry(tmp_path)
    config = AppConfig(
        registry=RegistryConfig(path=str(path)),
        e2e_models=[
            E2EModelOverride(model_id="minimax-m2.5", streaming=True, probe_max_tokens=256),
            E2EModelOverride(model_id="extra-model", streaming=False, probe_max_tokens=16),
        ],
    )

    targets = resolve_targets(config)
    by_id = {t.model_id: t for t in targets}

    assert [t.model_id for t in targets] == ["glm-4.7", "minimax-m2.5", "extra-model"]
    assert by_id["glm-4.7"].streaming is True
    assert by_id["glm-4.7"].max_tokens is None
    assert by_id["minimax-m2.5"].max_tokens == 256  # override applied
    assert by_id["extra-model"].streaming is False  # added from overrides
