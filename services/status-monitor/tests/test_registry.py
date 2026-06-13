"""Tests for registry loading and target resolution."""

from __future__ import annotations

from pathlib import Path

from status_monitor.config import AppConfig, E2EModelOverride, RegistryConfig, Settings
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
  - id: deepseek-v4-pro
    name: DeepSeek
    required_role: internal
  - name: no-id-should-be-skipped
""",
        encoding="utf-8",
    )
    return path


def test_load_model_ids(tmp_path: Path) -> None:
    path = _write_registry(tmp_path)
    assert load_model_ids(path) == ["glm-4.7", "minimax-m2.5", "deepseek-v4-pro"]


def test_load_model_ids_missing_file(tmp_path: Path) -> None:
    assert load_model_ids(tmp_path / "nope.yaml") == []


def test_load_model_ids_non_dict_yaml(tmp_path: Path) -> None:
    path = tmp_path / "models.yaml"
    path.write_text("- just\n- a\n- list\n", encoding="utf-8")
    assert load_model_ids(path) == []  # must not raise on non-mapping YAML


def test_resolve_targets_merges_overrides(tmp_path: Path) -> None:
    path = _write_registry(tmp_path)
    config = AppConfig(
        settings=Settings(prober_role="internal"),
        registry=RegistryConfig(path=str(path)),
        e2e_models=[
            E2EModelOverride(model_id="minimax-m2.5", streaming=True, probe_max_tokens=256),
            E2EModelOverride(model_id="extra-model", streaming=False, probe_max_tokens=16),
        ],
    )

    targets = resolve_targets(config)
    by_id = {t.model_id: t for t in targets}

    # An internal prober can access the internal-only deepseek model.
    assert [t.model_id for t in targets] == [
        "glm-4.7",
        "minimax-m2.5",
        "deepseek-v4-pro",
        "extra-model",
    ]
    assert by_id["glm-4.7"].streaming is True
    assert by_id["glm-4.7"].max_tokens is None
    assert by_id["minimax-m2.5"].max_tokens == 256  # override applied
    assert by_id["extra-model"].streaming is False  # added from overrides


def test_resolve_targets_skips_role_restricted_models(tmp_path: Path) -> None:
    path = _write_registry(tmp_path)
    # A free-tier prober can't reach the internal-only model, so it's excluded
    # rather than reported as an outage.
    config = AppConfig(
        settings=Settings(prober_role="free"),
        registry=RegistryConfig(path=str(path)),
    )

    targets = resolve_targets(config)

    assert [t.model_id for t in targets] == ["glm-4.7", "minimax-m2.5"]


def test_resolve_targets_override_forces_role_restricted_model(tmp_path: Path) -> None:
    path = _write_registry(tmp_path)
    # An explicit override is honored even when the role would otherwise exclude it.
    config = AppConfig(
        settings=Settings(prober_role="free"),
        registry=RegistryConfig(path=str(path)),
        e2e_models=[E2EModelOverride(model_id="deepseek-v4-pro")],
    )

    ids = [t.model_id for t in resolve_targets(config)]

    assert "deepseek-v4-pro" in ids
