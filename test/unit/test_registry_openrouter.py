"""Tests for parse_openrouter_kind and _make_adapter dispatch."""

from __future__ import annotations

import pytest

from serving.servers.registry import parse_openrouter_kind


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
