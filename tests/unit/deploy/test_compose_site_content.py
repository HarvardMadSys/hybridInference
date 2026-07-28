"""The distribution's site content must survive compose interpolation.

FreeInference's people, sponsors and data-policy notice are no longer code
defaults: they are build args in deploy/docker/docker-compose.yml. That makes
YAML quoting the only thing standing between production and a silently empty
team section — ``fromJsonEnv`` falls back to an empty array when a value fails
to parse, so a broken default would remove the sections without any error.

These tests read the compose file directly (no Docker required) and assert the
defaults are present and well-formed.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

COMPOSE = Path(__file__).resolve().parents[3] / "deploy" / "docker" / "docker-compose.yml"

# ${VAR-default}: capture the default, which itself contains braces.
_INTERPOLATION = re.compile(r"^\$\{[A-Z_]+-(?P<default>.*)\}$", re.DOTALL)


def _build_arg_default(name: str) -> str:
    args = yaml.safe_load(COMPOSE.read_text())["services"]["frontend"]["build"]["args"]
    raw = str(args[name])
    match = _INTERPOLATION.match(raw)
    assert match, f"{name} should be overridable as ${{{name}-<default>}}, got {raw[:60]!r}"
    return match.group("default")


@pytest.mark.parametrize(
    ("arg", "expected_names"),
    [
        ("NEXT_PUBLIC_TEAM_JSON", {"Juncheng Yang", "Murphy Tian", "Haoran Ni"}),
        ("NEXT_PUBLIC_SPONSORS_JSON", {"NVIDIA", "Harvard SEAS"}),
    ],
)
def test_site_content_defaults_parse_as_json(arg: str, expected_names: set[str]) -> None:
    """Each site-content build arg holds valid JSON with the expected entries."""
    entries = json.loads(_build_arg_default(arg))
    assert isinstance(entries, list) and entries
    assert {entry["name"] for entry in entries} == expected_names


def test_data_policy_notice_default_is_present() -> None:
    """The data-handling statement survives interpolation as a compose default."""
    notice = _build_arg_default("NEXT_PUBLIC_DATA_POLICY_NOTICE")
    # A deployment that logs prompts must say so; the upstream default is
    # empty, so this value existing here is what keeps the statement on the
    # FreeInference site.
    assert "logged for research purposes" in notice
