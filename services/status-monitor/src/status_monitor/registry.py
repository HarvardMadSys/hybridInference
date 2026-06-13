"""Model registry loading.

Reads ``config/models.yaml`` (the FreeInference model registry) to discover the
set of model ids the monitor should probe.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class ModelInfo:
    """Minimal registry metadata needed to choose how to probe a model."""

    model_id: str
    kind: str  # "chat" or "embedding"


def _kind_of(entry: dict) -> str:
    """Determines whether a registry entry is a chat or embedding model."""
    if str(entry.get("type", "")).lower() == "embedding":
        return "embedding"
    if "embedding" in (entry.get("output_modalities") or []):
        return "embedding"
    return "chat"


def load_models(path: str | Path) -> list[ModelInfo]:
    """Loads model metadata from a ``models.yaml`` registry file.

    Args:
        path: Filesystem path to the model registry YAML.

    Returns:
        The models in registry order. Returns an empty list if the file is
        missing or contains no models.
    """
    registry_path = Path(path)
    if not registry_path.is_file():
        return []
    raw = yaml.safe_load(registry_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        return []
    models = raw.get("models")
    if not isinstance(models, list):
        return []
    result: list[ModelInfo] = []
    for entry in models:
        if isinstance(entry, dict) and entry.get("id"):
            result.append(ModelInfo(model_id=str(entry["id"]), kind=_kind_of(entry)))
    return result


def load_model_ids(path: str | Path) -> list[str]:
    """Loads just the list of model ids from a registry file (in order)."""
    return [m.model_id for m in load_models(path)]
