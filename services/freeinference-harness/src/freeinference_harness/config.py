"""YAML loading helpers for targets and suites."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml

from freeinference_harness.models import Capabilities, ScenarioConfig, SuiteConfig, TargetConfig

_ENV_RE = re.compile(r"\$\{([A-Z0-9_]+)\}")


def _expand_env(value: Any) -> Any:
    """Expands ${ENV_VAR} tokens inside nested YAML values."""
    if isinstance(value, dict):
        return {key: _expand_env(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_expand_env(item) for item in value]
    if not isinstance(value, str):
        return value

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        env_value = os.getenv(name)
        if env_value is None:
            raise ValueError(f"Missing required environment variable: {name}")
        return env_value

    return _ENV_RE.sub(replace, value)


def _read_yaml(path: Path) -> dict[str, Any]:
    """Reads a YAML file into a dictionary."""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data or {}


def load_targets(path: Path) -> list[TargetConfig]:
    """Loads target definitions from YAML."""
    raw = _expand_env(_read_yaml(path))
    defaults = raw.get("defaults", {})
    targets = raw.get("targets", [])
    resolved: list[TargetConfig] = []

    for item in targets:
        merged = {**defaults, **item}
        # Deep-merge extra_headers: defaults + target-specific (target wins on conflict)
        merged["extra_headers"] = {
            **defaults.get("extra_headers", {}),
            **(item.get("extra_headers") or {}),
        }
        api_key = merged.get("api_key")
        api_key_env = merged.get("api_key_env")
        if not api_key and api_key_env:
            api_key = os.getenv(api_key_env)

        caps_raw = merged.get("capabilities", {})
        capabilities = Capabilities(
            chat=bool(caps_raw.get("chat", True)),
            streaming=bool(caps_raw.get("streaming", True)),
            tools=bool(caps_raw.get("tools", False)),
            structured_output=bool(caps_raw.get("structured_output", False)),
            embeddings=bool(caps_raw.get("embeddings", False)),
            anthropic_messages=bool(caps_raw.get("anthropic_messages", False)),
            admin_required=bool(caps_raw.get("admin_required", False)),
        )
        resolved.append(
            TargetConfig(
                name=str(merged["name"]),
                model=str(merged["model"]),
                base_url=str(merged["base_url"]),
                api_key=str(api_key or ""),
                suite_type=str(merged.get("suite_type", "gateway-pinned")),
                timeout_seconds=float(merged.get("timeout_seconds", 240)),
                sampling_count=int(merged.get("sampling_count", 5)),
                capabilities=capabilities,
                tags=tuple(str(tag) for tag in merged.get("tags", [])),
                extra_headers={
                    str(k): str(v) for k, v in (merged.get("extra_headers") or {}).items()
                },
            )
        )

    return resolved


def load_suite(path: Path) -> SuiteConfig:
    """Loads scenario definitions from YAML."""
    raw = _expand_env(_read_yaml(path))
    scenarios: list[ScenarioConfig] = []
    for item in raw.get("scenarios", []):
        scenarios.append(
            ScenarioConfig(
                scenario_id=str(item["id"]),
                scenario_type=str(item["type"]),
                required_capabilities=tuple(
                    str(cap) for cap in item.get("required_capabilities", [])
                ),
                repetitions=int(item["repetitions"])
                if item.get("repetitions") is not None
                else None,
                max_tokens=int(item["max_tokens"]) if item.get("max_tokens") is not None else None,
                tools_fixture=str(item["tools_fixture"]) if item.get("tools_fixture") else None,
                forced_tool_name=str(item["forced_tool_name"])
                if item.get("forced_tool_name")
                else None,
                user_prompt=str(item["user_prompt"]) if item.get("user_prompt") else None,
                agent_script=str(item["agent_script"]) if item.get("agent_script") else None,
            )
        )

    return SuiteConfig(
        suite_name=str(raw.get("suite_name", path.stem)),
        scenarios=tuple(scenarios),
    )


def load_tools_fixture(fixture_name: str) -> list[dict[str, Any]]:
    """Loads a tool fixture YAML and returns the tools list in OpenAI format.

    Fixture files are looked up relative to ``configs/fixtures/`` from the
    repository root (two levels above this source file).
    """
    fixtures_dir = Path(__file__).resolve().parent.parent.parent / "configs" / "fixtures"
    path = fixtures_dir / fixture_name
    if not path.exists():
        raise FileNotFoundError(f"Tool fixture not found: {path}")
    raw = _read_yaml(path)
    tools: list[dict[str, Any]] = raw.get("tools", [])
    return tools
