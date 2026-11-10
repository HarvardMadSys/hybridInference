"""Routing configuration schema and loader (Pydantic-based)."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, cast

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from serving.config.app_config import config_value
from serving.config.app_config_registry import static_entry
from serving.utils.logging import get_logger

if TYPE_CHECKING:
    from pathlib import Path

_logger = get_logger(__name__)

_ENV_PATTERN = re.compile(r"\$\{([A-Z0-9_]+)(?::-(.*?))?\}")


def _registered_default(key: str) -> str | None:
    """Return the registered default of a static setting, the value the console reports unset."""
    entry = static_entry(key)
    return entry.default if entry is not None and entry.default else None


def _expand_env_value(val: Any) -> Any:
    """Recursively expand ``${VAR}`` references in configuration values.

    Supports ${VAR} and ${VAR:-default} syntax. Applies to strings, lists, and
    dicts. A variable resolves through ``config_value`` — its database-backed
    setting, then the environment — then the inline ``:-default``, then the
    registered default of a static setting that has one, and else to ``""``.

    Args:
        val: Configuration value to process (str, list, dict, or other).

    Returns:
        The value with environment variables expanded.
    """
    if isinstance(val, str):

        def repl(match: re.Match[str]) -> str:
            key = match.group(1)
            inline_default = match.group(2)
            value = config_value(key)
            if value is None:
                value = inline_default if inline_default is not None else _registered_default(key)
            return value or ""

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

    # Deprecated block.  Defaults to None so that its presence in YAML is
    # detectable for the warning below.
    routing_parameter: RoutingParameter | None = Field(default=None)

    @field_validator("timeout", "health_check")
    @classmethod
    def _validate_pos(cls, v: int) -> int:
        if v < 0:
            raise ValueError("value must be non-negative")
        return v

    @field_validator("default_router")
    @classmethod
    def _validate_default_router(cls, v: str) -> str:
        """Refuse a name no router can be built under.

        Startup builds a router with it for every model that omits ``router``
        and stops on the first that fails, so an unknown name was saved without
        complaint and then kept the gateway, its Admin console included, from
        starting. Refused here, a save says why and a stored policy that names
        one is reported instead of applied.
        """
        # Imported here: the strategy modules import the routers, which import
        # this module.
        from routing.strategies import strategy_name_problem

        problem = strategy_name_problem(v)
        if problem is not None:
            raise ValueError(problem)
        return v

    @model_validator(mode="after")
    def _warn_legacy_parameter(self) -> RoutingConfig:
        """Log a deprecation warning (once per load) for ``routing_parameter``."""
        if self.routing_parameter is not None:
            _logger.warning(
                "routing.yaml uses the deprecated 'routing_parameter' block; migrate to "
                "per-model 'router'/'router_params' in models.yaml. It will be removed in "
                "a future release."
            )
        return self


def _drop_unset_deployments(expanded: dict[str, Any]) -> dict[str, Any]:
    """Drop deployment entries whose endpoint expanded to an empty string.

    A ``${VAR}`` referencing an unset env var with no ``:-default`` expands to
    ``""`` (see ``_expand_env_value``). Such an entry would fail the
    ``Deployment.endpoint`` validator and invalidate the *entire*
    ``RoutingConfig``, degrading routing for every other (healthy) endpoint.
    Instead, prune those entries with a warning so the remaining deployments
    keep loading.

    Args:
        expanded: Raw config dict after environment-variable expansion.

    Returns:
        The dict with unset deployment entries removed from
        ``local_deployment`` and ``remote_deployment``.
    """
    for section in ("local_deployment", "remote_deployment"):
        entries = expanded.get(section)
        if not isinstance(entries, list):
            continue
        kept: list[Any] = []
        for entry in entries:
            endpoint = entry.get("endpoint") if isinstance(entry, dict) else None
            if isinstance(endpoint, str) and endpoint.strip():
                kept.append(entry)
                continue
            models = entry.get("models") if isinstance(entry, dict) else None
            _logger.warning(
                "routing.yaml %s entry has unset/blank endpoint; dropping it. "
                "Check the env var for this endpoint. Orphaned models: %s",
                section,
                models or "(none)",
            )
        expanded[section] = kept
    return expanded


def routing_config_from_data(raw: Any) -> RoutingConfig:
    """Validate a parsed routing document and expand its ``${VAR}`` references.

    Args:
        raw: The parsed document. A mapping is required.

    Returns:
        Validated RoutingConfig object with references expanded.

    Raises:
        ValueError: If the document is not a mapping or fails validation.
    """
    if not isinstance(raw, dict):
        raise ValueError("Invalid routing config: document must be a mapping")
    expanded = _expand_env_value(raw)
    expanded = _drop_unset_deployments(expanded)
    try:
        return cast("RoutingConfig", RoutingConfig.model_validate(expanded))
    except ValidationError as e:
        raise ValueError(f"Invalid routing config: {e}") from e


def load_routing_config_text(text: str) -> RoutingConfig:
    """Load and validate routing configuration from a YAML document.

    Args:
        text: YAML document. ``${VAR}`` references resolve the same way they do
            when the document is read from a file.

    Returns:
        Validated RoutingConfig object with references expanded.

    Raises:
        ValueError: If the document is invalid.
    """
    return routing_config_from_data(yaml.safe_load(text) or {})


def load_routing_config(path: Path) -> RoutingConfig:
    """Load and validate routing configuration from YAML file.

    Args:
        path: Path to the YAML configuration file.

    Returns:
        Validated RoutingConfig object with environment variables expanded.

    Raises:
        ValueError: If configuration is invalid or file cannot be read.
    """
    return routing_config_from_data(yaml.safe_load(path.read_text()) or {})
