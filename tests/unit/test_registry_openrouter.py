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
        ("zai", ("zai", None)),  # non-openrouter passes through
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
    ],
)
def test_parse_rejects_invalid(kind: str) -> None:
    with pytest.raises(ValueError):
        parse_openrouter_kind(kind)


def test_make_adapter_rejects_pseudo_openrouter_kind() -> None:
    """`openrouter]deepinfra[` doesn't match the bracket form, so the parser
    returns it unchanged. _make_adapter then rejects it as unknown."""
    base_kind, pinned = parse_openrouter_kind("openrouter]deepinfra[")
    assert base_kind == "openrouter]deepinfra["
    assert pinned is None
    with pytest.raises(ValueError, match="Unknown adapter kind"):
        _make_adapter("openrouter]deepinfra[", _cfg())


def test_parse_unrelated_openrouter_prefix_passes_through() -> None:
    """Future kinds with `openrouter` as a name prefix (not bracket form)
    pass through unchanged — they get whatever dispatch the registry has,
    or raise 'Unknown adapter kind'."""
    assert parse_openrouter_kind("openrouter_v2") == ("openrouter_v2", None)
    assert parse_openrouter_kind("openrouterprovider") == ("openrouterprovider", None)


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


def test_make_adapter_bracket_normalizes_provider_field() -> None:
    """ModelConfig.provider should be 'openrouter' even for bracket form,
    so api_logs.provider sees one cohort rather than one cohort per pinned
    upstream."""
    from serving.servers.registry import parse_openrouter_kind

    base_kind, pinned = parse_openrouter_kind("openrouter[deepinfra]")
    assert base_kind == "openrouter"
    assert pinned == "deepinfra"
