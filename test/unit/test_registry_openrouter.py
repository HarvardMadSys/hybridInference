"""Tests for parse_openrouter_kind and _make_adapter dispatch."""

from __future__ import annotations

import pytest

from serving.adapters import OpenRouterAdapter
from serving.servers.registry import _make_adapter, parse_openrouter_kind


@pytest.mark.parametrize(
    "kind,expected",
    [
        ("openrouter", ("openrouter", None)),
        ("openrouter[deepinfra]", ("openrouter", "deepinfra")),
        ("openrouter[fireworks]", ("openrouter", "fireworks")),
        ("openrouter[together-ai]", ("openrouter", "together-ai")),
        ("zhipu", ("zhipu", None)),  # non-openrouter passes through
        ("openai_compat", ("openai_compat", None)),
    ],
)
def test_parse_valid(kind: str, expected: tuple[str, str | None]) -> None:
    assert parse_openrouter_kind(kind) == expected


@pytest.mark.parametrize(
    "kind",
    [
        "openrouter[]",
        "openrouter[ ]",
        "openrouter[deep infra]",
        "openrouter[deep[infra]]",
        "openrouter[deepinfra",
        "openrouter]deepinfra[",
    ],
)
def test_parse_rejects_invalid(kind: str) -> None:
    with pytest.raises(ValueError):
        parse_openrouter_kind(kind)


def _cfg(**overrides):
    base = {
        "id": "m",
        "name": "M",
        "provider": "openrouter",
        "base_url": "https://openrouter.ai/api/v1",
        "api_key": "sk-or-test",
        "provider_model_id": "meta-llama/llama-3.3-70b-instruct",
    }
    base.update(overrides)
    return base


def test_make_adapter_bare_openrouter_returns_openrouter_adapter() -> None:
    adapter = _make_adapter("openrouter", _cfg())
    assert isinstance(adapter, OpenRouterAdapter)
    assert adapter.config.openrouter_pinned_provider is None


def test_make_adapter_bracket_openrouter_sets_pinned_provider() -> None:
    adapter = _make_adapter("openrouter[fireworks]", _cfg())
    assert isinstance(adapter, OpenRouterAdapter)
    assert adapter.config.openrouter_pinned_provider == "fireworks"


def test_make_adapter_invalid_openrouter_kind_raises() -> None:
    with pytest.raises(ValueError):
        _make_adapter("openrouter[]", _cfg())
