"""Routing configuration schema and loader (Pydantic-based)."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, cast

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from serving.utils.logging import get_logger

if TYPE_CHECKING:
    from pathlib import Path

_logger = get_logger(__name__)

_ENV_PATTERN = re.compile(r"\$\{([A-Z0-9_]+)(?::-(.*?))?\}")


def _expand_env_value(val: Any) -> Any:
    """Recursively expand environment variables in configuration values.

    Supports ${VAR} and ${VAR:-default} syntax. Applies to strings, lists, and dicts.

    Args:
        val: Configuration value to process (str, list, dict, or other).

    Returns:
        The value with environment variables expanded.
    """
    if isinstance(val, str):

        def repl(match: re.Match[str]) -> str:
            import os

            key = match.group(1)
            default = match.group(2)
            return os.getenv(key, default if default is not None else "")

        return _ENV_PATTERN.sub(repl, val)
    if isinstance(val, list):
        return [_expand_env_value(v) for v in val]
    if isinstance(val, dict):
        return {k: _expand_env_value(v) for k, v in val.items()}
    return val


class Deployment(BaseModel):  # type: ignore[no-any-unimported]
    """Configuration for a single deployment endpoint.

    Attributes:
        endpoint: HTTP(S) URL of the deployment.
        models: List of model IDs available at this endpoint.
    """

    endpoint: str
    models: list[str] = Field(default_factory=list)

    @field_validator("endpoint")
    @classmethod
    def _validate_endpoint(cls, v: str) -> str:
        if not (v.startswith("http://") or v.startswith("https://")):
            raise ValueError("endpoint must start with http:// or https://")
        return v.rstrip("/")

    @field_validator("models")
    @classmethod
    def _validate_models(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("models list must not be empty")
        return v


class RoutingParameter(BaseModel):  # type: ignore[no-any-unimported]
    """Parameters for routing strategies.

    Attributes:
        local_fraction: Fraction of traffic routed to local deployments (0.0-1.0).
    """

    local_fraction: float = 0.5

    @field_validator("local_fraction")
    @classmethod
    def _validate_fraction(cls, v: float) -> float:
        if not (0.0 <= v <= 1.0):
            raise ValueError("local_fraction must be between 0 and 1")
        return v


class RoutingConfig(BaseModel):  # type: ignore[no-any-unimported]
    """Complete routing configuration schema.

    Attributes:
        default_router: Strategy name used when a model in models.yaml
            omits its own ``router:`` field.  Defaults to ``"fixed"``.
        timeout: HTTP timeout in seconds for health checks.
        health_check: Health check interval in seconds (0 to disable).
        logging: Logging configuration dictionary.
        local_deployment: List of local deployment configurations.
        remote_deployment: List of remote deployment configurations.

        routing_strategy: DEPRECATED — use ``default_router``.  Migrated by
            ``_migrate_legacy_fields``.  Read by ``RoutingManager`` for now;
            new code should consult ``default_router`` instead.
        routing_parameter: DEPRECATED — move per-strategy params into
            ``router_params`` per model in ``models.yaml``.  Kept for one
            release so existing ``routing.yaml`` files keep loading and so
            ``RoutingManager`` (which reads ``local_fraction`` from this
            block) keeps working.
    """

    default_router: str = Field(default="fixed")
    timeout: int = 2
    health_check: int = 0  # seconds; 0 disables health checking
    logging: dict[str, Any] = Field(default_factory=dict)

    local_deployment: list[Deployment] = Field(default_factory=list)
    remote_deployment: list[Deployment] = Field(default_factory=list)

    # Deprecated aliases — keep for one release.  Default to None so that
    # presence in YAML is detectable for the migration / warning logic.
    routing_strategy: str | None = Field(default=None)
    routing_parameter: RoutingParameter | None = Field(default=None)

    @field_validator("timeout", "health_check")
    @classmethod
    def _validate_pos(cls, v: int) -> int:
        if v < 0:
            raise ValueError("value must be non-negative")
        return v

    @model_validator(mode="after")
    def _migrate_legacy_fields(self) -> RoutingConfig:
        """Migrate deprecated ``routing_strategy`` / ``routing_parameter`` aliases.

        - When ``routing_strategy`` is set and ``default_router`` was not
          explicitly overridden in YAML, promote the legacy value.
        - When either deprecated field is present, log a deprecation warning
          (once per load).
        """
        legacy_strategy_present = self.routing_strategy is not None
        legacy_parameter_present = self.routing_parameter is not None

        if legacy_strategy_present and "default_router" not in self.model_fields_set:
            object.__setattr__(self, "default_router", self.routing_strategy)

        if legacy_strategy_present or legacy_parameter_present:
            _logger.warning(
                "routing.yaml uses deprecated 'routing_strategy'/'routing_parameter' "
                "fields; migrate to 'default_router' + per-model 'router'/'router_params' "
                "in models.yaml. The legacy fields will be removed in a future release."
            )
        return self


def load_routing_config(path: Path) -> RoutingConfig:
    """Load and validate routing configuration from YAML file.

    Args:
        path: Path to the YAML configuration file.

    Returns:
        Validated RoutingConfig object with environment variables expanded.

    Raises:
        ValueError: If configuration is invalid or file cannot be read.
    """
    raw = yaml.safe_load(path.read_text()) or {}
    expanded = _expand_env_value(raw)
    try:
        return cast("RoutingConfig", RoutingConfig.model_validate(expanded))
    except ValidationError as e:
        raise ValueError(f"Invalid routing config: {e}") from e
