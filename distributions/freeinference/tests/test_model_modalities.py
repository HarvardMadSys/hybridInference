"""What this deployment's models claim they can take."""

from __future__ import annotations

from pathlib import Path

import yaml


def test_all_zai_route_entries_are_text_only():
    """No ZAI-routed model in this catalogue may advertise image input.

    The adapter drops image blocks silently, so a model that claims the
    modality would accept a request and answer about nothing. Which models
    exist and what they claim is this deployment's catalogue, so the check
    lives with the catalogue rather than with the adapter.
    """
    models_path = Path(__file__).resolve().parents[1] / "config" / "models.yaml"
    with open(models_path) as f:
        data = yaml.safe_load(f)

    for model in data["models"]:
        model_id = model.get("id", "")
        for route in model.get("route", []):
            if route.get("kind") != "zai":
                continue
            modalities = model.get("input_modalities", [])
            assert "image" not in modalities, (
                f"Model {model_id} with kind=zai should not support image (got {modalities})"
            )
