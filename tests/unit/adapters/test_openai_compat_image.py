"""Tests for OpenAICompatAdapter message cleaning and image handling."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from serving.adapters.base import ModelConfig
from serving.adapters.openai_compat import (
    OpenAICompatAdapter,
    _normalize_text_content,
)


def _make_adapter(
    *,
    id: str = "test-model",
    input_modalities: list[str] | None = None,
    provider: str = "zhipu",
    base_url: str = "http://mock.local/v1",
) -> OpenAICompatAdapter:
    config = ModelConfig(
        id=id,
        name="Test Model",
        provider=provider,
        base_url=base_url,
        input_modalities=input_modalities if input_modalities is not None else ["text"],
    )
    return OpenAICompatAdapter(config)


# --- _normalize_text_content ---


def test_normalize_text_content_string_passthrough():
    assert _normalize_text_content("hello") == "hello"


def test_normalize_text_content_list_of_strings():
    result = _normalize_text_content(["a", "b", "c"])
    assert result == "a\nb\nc"


def test_normalize_text_content_drops_image_url_blocks():
    """image_url blocks have no 'text' key — they get silently dropped."""
    content = [
        {"type": "text", "text": "Hello"},
        {"type": "image_url", "image_url": {"url": "https://example.com/img.png"}},
        {"type": "text", "text": "What is this?"},
    ]
    result = _normalize_text_content(content)
    assert result == "Hello\nWhat is this?"


def test_normalize_text_content_empty_result():
    content = [
        {"type": "image_url", "image_url": {"url": "https://example.com/img.png"}},
    ]
    result = _normalize_text_content(content)
    assert result == ""


# --- _clean_message with image handling ---


class TestCleanMessageNoImageSupport:
    """When input_modalities does not include 'image', image content is stripped."""

    def test_clean_message_strips_images_when_not_supported(self):
        adapter = _make_adapter(input_modalities=["text"])
        message = {
            "role": "user",
            "content": [
                {"type": "text", "text": "Describe this"},
                {
                    "type": "image_url",
                    "image_url": {"url": "https://example.com/x.png"},
                },
            ],
        }
        result = adapter._clean_message(message)
        # image_url block is silently removed
        assert isinstance(result["content"], str)
        assert "Describe this" in result["content"]

    def test_clean_message_plain_string_untouched(self):
        adapter = _make_adapter(input_modalities=["text"])
        message = {"role": "user", "content": "Just text"}
        result = adapter._clean_message(message)
        assert result["content"] == "Just text"

    def test_clean_message_with_image_support_passes_through(self):
        adapter = _make_adapter(input_modalities=["text", "image"])
        message = {
            "role": "user",
            "content": [
                {"type": "text", "text": "What is this?"},
                {
                    "type": "image_url",
                    "image_url": {"url": "https://example.com/x.png"},
                },
            ],
        }
        result = adapter._clean_message(message)
        assert isinstance(result["content"], list)
        assert len(result["content"]) == 2


class TestZaiModelsDoNotSupportImage:
    """All ZAI route models use input_modalities=['text'] — no images.

    This test verifies that sending image content to a ZAI model
    results in the images being stripped (silently) rather than
    causing a crash.  The ideal behavior would be to raise an error
    instead, but that requires upstream changes.

    ZAI models: glm-4.7, glm-5, glm-5.1, glm-5-turbo.
    """

    @pytest.mark.parametrize(
        "model_id",
        ["glm-4.7", "glm-5", "glm-5.1", "glm-5-turbo"],
    )
    def test_image_stripped_in_clean_message(self, model_id: str):
        adapter = _make_adapter(id=model_id, input_modalities=["text"])
        message = {
            "role": "user",
            "content": [
                {"type": "text", "text": "What is in this image?"},
                {
                    "type": "image_url",
                    "image_url": {"url": "https://example.com/photo.jpg"},
                },
            ],
        }
        result = adapter._clean_message(message)
        # Currently: image is silently stripped, content becomes plain text
        assert isinstance(result["content"], str)
        assert "What is in this image?" in result["content"]
        assert "image_url" not in str(result["content"])
        # The image URL itself should NOT appear in the final content
        assert "photo.jpg" not in result["content"]

    def test_all_zai_route_entries_text_only_modalities(self):
        """Verify all ZAI route entries in models.yaml use text-only input_modalities."""
        models_path = Path(__file__).resolve().parents[3] / "config" / "models.yaml"
        with open(models_path) as f:
            data = yaml.safe_load(f)

        for model in data["models"]:
            model_id = model.get("id", "")
            for route in model.get("route", []):
                if route.get("kind") != "zhipu":
                    continue
                modalities = model.get("input_modalities", [])
                assert "image" not in modalities, (
                    f"Model {model_id} with kind=zhipu should not support image (got {modalities})"
                )
