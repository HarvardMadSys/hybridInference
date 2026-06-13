"""Configuration loading for the status monitor.

The config schema intentionally mirrors the deployment config so that the same
YAML works in Docker and locally. Environment variables of the form ``${VAR}``
and ``${VAR:-default}`` are expanded at load time.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

_ENV_PATTERN = re.compile(r"\$\{([^}^{]+)\}")


def _expand_env(value: str) -> str:
    """Expands ``${VAR}`` and ``${VAR:-default}`` references in a string."""

    def replace(match: re.Match[str]) -> str:
        expr = match.group(1)
        if ":-" in expr:
            name, default = expr.split(":-", 1)
        else:
            name, default = expr, ""
        return os.environ.get(name.strip(), default)

    return _ENV_PATTERN.sub(replace, value)


def _expand(obj: Any) -> Any:
    """Recursively expands env references in nested mappings and sequences."""
    if isinstance(obj, str):
        return _expand_env(obj)
    if isinstance(obj, dict):
        return {key: _expand(val) for key, val in obj.items()}
    if isinstance(obj, list):
        return [_expand(item) for item in obj]
    return obj


@dataclass(frozen=True)
class Settings:
    """General service settings."""

    port: int = 9101
    base_path: str = ""
    default_timeout: float = 30.0
    probe_prompt: str = "Write a short Python function that returns hello world."
    probe_max_tokens: int = 32
    probe_temperature: float = 0.0
    history_size: int = 100
    log_level: str = "INFO"
    state_path: str | None = None
    # Bounded probe fan-out. The gateway enforces a per-user concurrency cap
    # (3 for free/pro, 10 for internal/admin); exceeding it yields HTTP 429 and
    # false outages. Keep this at or below the prober account's cap.
    max_concurrency: int = 3
    # Role of the PROBER_API_KEY account. Registry models whose required_role
    # outranks this are skipped (the gateway 404s them, which would otherwise
    # look like an outage). One of: free, pro, internal, admin.
    prober_role: str = "internal"


@dataclass(frozen=True)
class GatewayConfig:
    """Connection settings for the FreeInference gateway."""

    base_url: str = "http://localhost:8080"
    api_key: str = ""
    e2e_interval: float = 300.0
    probe_header: str | None = None
    # Discover probe targets from the gateway's authenticated /models catalog
    # (reflects role + runtime visibility overrides). Falls back to the static
    # registry if discovery fails or this is disabled.
    discover_models: bool = True


@dataclass(frozen=True)
class RegistryConfig:
    """Model registry source used to auto-discover probe targets."""

    path: str | None = None


@dataclass(frozen=True)
class E2EModelOverride:
    """Manual override or addition for a probe target."""

    model_id: str
    streaming: bool = True
    probe_max_tokens: int | None = None


@dataclass(frozen=True)
class AppConfig:
    """Top-level application configuration."""

    settings: Settings = field(default_factory=Settings)
    gateway: GatewayConfig = field(default_factory=GatewayConfig)
    registry: RegistryConfig = field(default_factory=RegistryConfig)
    e2e_models: list[E2EModelOverride] = field(default_factory=list)


def _build_settings(raw: dict[str, Any]) -> Settings:
    """Builds a :class:`Settings` from a raw mapping."""
    return Settings(
        port=int(raw.get("port", 9101)),
        base_path=str(raw.get("base_path", "")).rstrip("/"),
        default_timeout=float(raw.get("default_timeout", 30.0)),
        probe_prompt=str(
            raw.get("probe_prompt", "Write a short Python function that returns hello world.")
        ),
        probe_max_tokens=int(raw.get("probe_max_tokens", 32)),
        probe_temperature=float(raw.get("probe_temperature", 0.0)),
        history_size=int(raw.get("history_size", 100)),
        log_level=str(raw.get("log_level", "INFO")),
        state_path=raw.get("state_path"),
        max_concurrency=max(1, int(raw.get("max_concurrency", 3))),
        prober_role=str(raw.get("prober_role", "internal")),
    )


def _build_gateway(raw: dict[str, Any]) -> GatewayConfig:
    """Builds a :class:`GatewayConfig` from a raw mapping."""
    interval = raw.get("e2e_interval", raw.get("health_interval", 300.0))
    return GatewayConfig(
        base_url=str(raw.get("base_url", "http://localhost:8080")),
        api_key=str(raw.get("api_key", "")),
        e2e_interval=float(interval),
        probe_header=raw.get("probe_header"),
        discover_models=bool(raw.get("discover_models", True)),
    )


def _build_e2e_models(raw: list[Any]) -> list[E2EModelOverride]:
    """Builds the list of manual probe overrides."""
    overrides: list[E2EModelOverride] = []
    for item in raw or []:
        if not isinstance(item, dict) or "model_id" not in item:
            continue
        max_tokens = item.get("probe_max_tokens")
        overrides.append(
            E2EModelOverride(
                model_id=str(item["model_id"]),
                streaming=bool(item.get("streaming", True)),
                probe_max_tokens=int(max_tokens) if max_tokens is not None else None,
            )
        )
    return overrides


def load_config(path: str | Path) -> AppConfig:
    """Loads and parses the YAML config at ``path``.

    Args:
        path: Filesystem path to the YAML config file.

    Returns:
        The parsed :class:`AppConfig`.
    """
    text = Path(path).read_text(encoding="utf-8")
    raw = _expand(yaml.safe_load(text) or {})
    return AppConfig(
        settings=_build_settings(raw.get("settings", {}) or {}),
        gateway=_build_gateway(raw.get("gateway", {}) or {}),
        registry=RegistryConfig(path=(raw.get("registry", {}) or {}).get("path")),
        e2e_models=_build_e2e_models(raw.get("e2e_models", []) or []),
    )
