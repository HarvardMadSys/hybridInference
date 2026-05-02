"""Model registry and configuration loader.

This module builds provider adapters from configuration and registers them on a
``RouteExecutor``. It supports both environment-based and YAML-based
configuration. Prefer YAML (``config/models.yaml``) for reproducibility.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import yaml

logger = logging.getLogger(__name__)

from serving.adapters import (
    AnthropicAdapter,
    ClaudeAdapter,
    ClaudeSubscriptionAdapter,
    CodexSubscriptionAdapter,
    GeminiAdapter,
    ModelConfig,
    OpenAICompatAdapter,
    OpenRouterAdapter,
)

if TYPE_CHECKING:
    from pathlib import Path

    from routing.executor import RouteExecutor


@dataclass
class ModelRegistrationInfo:
    """Per-model metadata returned from YAML registration."""

    model_id: str
    strategy: str | None = None
    aliases: list[str] = field(default_factory=list)
    route_subscription_types: list[str] = field(default_factory=list)


_LOCAL_HOSTS = frozenset(("localhost", "127.0.0.1", "0.0.0.0", "host.docker.internal"))


def _make_provider_id(model_id: str, kind: str, base_url: str) -> str:
    """Generate a unique, user-friendly endpoint identifier.

    This ensures each endpoint has independent availability tracking
    and circuit breaker state, while being easy to understand at a glance.

    Format: "{model}:{location}"

    Examples:
        - glm-4.6 + sglang + http://localhost:12003       -> "glm-4.6:local-12003"
        - glm-4.6 + zhipu + https://api.z.ai/v4/          -> "glm-4.6:zhipu-api"
        - qwen3-coder + sglang + http://localhost:8003     -> "qwen3-coder:local-8003"
        - glm-4.7 + compat + http://host.docker.internal:8004 -> "glm-4.7:local-8004"
        - qwen3-coder + chutes + https://llm.chutes.ai    -> "qwen3-coder:chutes-api"
        - minimax-m2.7 + compat + https://api.minimax.io  -> "minimax-m2.7:minimax-api"

    Args:
        model_id: The model identifier (e.g., "glm-4.6", "qwen3-coder").
        kind: Adapter kind (e.g., "sglang", "zhipu", "chutes").
        base_url: The base URL of the endpoint.

    Returns:
        A unique, human-readable endpoint identifier string.
    """
    try:
        from urllib.parse import urlparse

        parsed = urlparse(base_url)
        host = parsed.hostname or "unknown"

        # Local endpoints: include port to disambiguate multiple local services
        if host in _LOCAL_HOSTS:
            port = parsed.port
            if port:
                return f"{model_id}:local-{port}"
            return f"{model_id}:local"

        # For generic adapters, extract service name from hostname
        if kind in ("openai_compat", "vllm", "sglang"):
            # Extract service name: "api.minimax.io" -> "minimax"
            # Remove common prefixes and get the main domain part
            name = host.replace("api.", "").replace("llm.", "").split(".")[0]
            if name and name not in ("com", "io", "ai", "org", "net", "xyz"):
                return f"{model_id}:{name}-api"

        # Remote APIs: use "{model}:{kind}-api" format
        return f"{model_id}:{kind}-api"
    except Exception:
        # Fallback if URL parsing fails
        return f"{model_id}:{kind}"


_OPENROUTER_KIND_RE = re.compile(r"^openrouter\[([A-Za-z0-9_.\-]+)\]$")


def parse_openrouter_kind(kind: str) -> tuple[str, str | None]:
    """Parse an adapter kind string, recognizing the OpenRouter bracket form.

    Returns a (base_kind, pinned_provider) tuple:
    - "openrouter"               -> ("openrouter", None)
    - "openrouter[deepinfra]"    -> ("openrouter", "deepinfra")
    - any other kind             -> (kind, None) (no parsing)

    Raises ValueError for malformed bracket forms (empty pin, whitespace,
    nested brackets, unmatched brackets).
    """
    if kind == "openrouter":
        return ("openrouter", None)
    if kind.startswith("openrouter["):
        match = _OPENROUTER_KIND_RE.match(kind)
        if match is None:
            raise ValueError(
                f"Invalid OpenRouter kind {kind!r}: expected "
                "'openrouter' or 'openrouter[<slug>]' with slug "
                "matching [A-Za-z0-9_.-]+"
            )
        return ("openrouter", match.group(1))
    return (kind, None)


def _make_adapter(kind: str, cfg: dict[str, Any]):
    """Construct a provider adapter from a kind string and model config.

    Args:
        kind: Adapter kind (``"vllm"``, ``"sglang"``, ``"claude"``, ``"deepseek"``, ``"gemini"``, ``"openai"``, ``"zhipu"``,
              ``"minimax"``, ``"chutes"``, ``"featherless"``, ``"ollama"``, ``"openai_compat"``, ``"openrouter"``,
              ``"openrouter[<slug>]"``).
        cfg: ``ModelConfig`` keyword arguments.

    Returns:
        A concrete adapter instance.

    Raises:
        ValueError: When ``kind`` is unknown or the OpenRouter bracket form
            is malformed.
    """
    # Resolve OpenRouter bracket syntax up front so the rest of the dispatch
    # operates on the bare base kind. parse_openrouter_kind raises on
    # malformed inputs (empty pin, whitespace, nested brackets).
    base_kind, pinned_provider = parse_openrouter_kind(kind)
    if base_kind == "openrouter":
        cfg = {
            **cfg,
            "provider_profile": "openrouter",
            "openrouter_pinned_provider": pinned_provider,
        }
        kind = base_kind  # subsequent dispatch checks compare against the bare kind

    # DeepSeek routes through OpenAICompatAdapter with DeepSeek usage profile
    if kind == "deepseek":
        cfg = {**cfg, "provider_profile": "deepseek"}
    elif kind == "openai":
        cfg = {
            **cfg,
            "provider_profile": "azure_openai",
            "chat_path": "/chat/completions",
            "use_bearer_auth": False,
            "auth_header_name": "api-key",
            "auth_format": "{api_key}",
            "extra_query": {"api-version": "2024-12-01-preview"},
        }
    # Zhipu routes through OpenAICompatAdapter with a non-/v1 chat path.
    elif kind == "zhipu":
        cfg = {**cfg, "provider_profile": "zhipu", "chat_path": "/chat/completions"}
    elif kind == "minimax":
        cfg = {**cfg, "provider_profile": "minimax"}

    model_cfg = ModelConfig(**cfg)

    # All OpenAI-compatible services use the same adapter
    if kind in (
        "vllm",
        "sglang",
        "chutes",
        "featherless",
        "ollama",
        "openai_compat",
        "deepseek",
        "openai",
        "zhipu",
        "minimax",
    ):
        return OpenAICompatAdapter(model_cfg)

    if kind == "openrouter":
        return OpenRouterAdapter(model_cfg)

    if kind == "claude":
        return ClaudeAdapter(model_cfg)
    if kind == "gemini":
        return GeminiAdapter(model_cfg)
    if kind == "codex_sub":
        return CodexSubscriptionAdapter(model_cfg)
    if kind == "claude_sub":
        return ClaudeSubscriptionAdapter(model_cfg)
    if kind == "anthropic":
        return AnthropicAdapter(model_cfg)

    raise ValueError(f"Unknown adapter kind: {kind}")


def register_from_models_yaml(
    router: RouteExecutor,
    path: Path,
    embedding_adapters: dict[str, Any] | None = None,
) -> tuple[int, list[ModelRegistrationInfo]]:
    """Register models and routes from a YAML configuration file.

    Example schema::

        models:
            context_length: 131072
            max_output_length: 8192
            supports_tools: true
            supports_structured_output: true
            supported_params: [temperature, top_p, top_k, min_p, max_tokens, stop, seed]
            aliases: ["test-model-instruct"]
            route:
              - kind: zhipu
                weight: 1.0
                base_url: ${LLAMA_BASE_URL}
                api_key: ${LLAMA_API_KEY}

    Args:
        router: Executor to receive registered routes.
        path: Path to the YAML configuration file.

    Returns:
        Tuple of (count of registered route identifiers including aliases,
        list of ModelRegistrationInfo for each model).
    """
    if not path.exists():
        return 0, []
    data = yaml.safe_load(path.read_text()) or {}
    models: list[dict[str, Any]] = data.get("models", [])
    count = 0
    model_infos: list[ModelRegistrationInfo] = []
    for m in models:
        # Environment expansion for base_url/api_key in both top-level and route entries
        def expand_env(val: str | None) -> str | None:
            if isinstance(val, str) and val.startswith("${") and val.endswith("}"):
                return os.getenv(val[2:-1])
            return val

        # Build primary config
        top_cfg = {
            k: m.get(k)
            for k in (
                "id",
                "name",
                "type",
                "model_type",
                "provider",
                "base_url",
                "api_key",
                "aliases",
                "provider_model_id",
                "quantization",
                "input_modalities",
                "output_modalities",
                "context_length",
                "max_output_length",
                "supports_tools",
                "supports_structured_output",
                "supported_params",
                "pricing",
            )
        }
        # Save raw (unexpanded) values for template-based blank-key detection
        raw_top_api_key = m.get("api_key")

        if top_cfg.get("base_url"):
            top_cfg["base_url"] = expand_env(top_cfg["base_url"])  # type: ignore
        if top_cfg.get("api_key"):
            top_cfg["api_key"] = expand_env(top_cfg["api_key"])  # type: ignore
        if top_cfg.get("provider_model_id"):
            top_cfg["provider_model_id"] = expand_env(top_cfg["provider_model_id"])  # type: ignore

        # If no explicit route list, use a single route targeting the primary config
        routes = m.get("route") or [
            {
                "kind": top_cfg.get("provider"),
                "weight": 1.0,
                **{k: top_cfg.get(k) for k in ("base_url", "api_key")},
            }
        ]

        adapters_with_weights = []
        for r in routes:
            kind = r.get("kind") or top_cfg.get("provider")
            base_url = expand_env(r.get("base_url") or top_cfg.get("base_url"))
            weight = float(r.get("weight", 1.0))

            raw_api_keys = r.get("api_keys")
            raw_api_key = r.get("api_key") or top_cfg.get("api_key")
            raw_api_key_template = r.get("api_key") or raw_top_api_key

            if raw_api_keys is not None and r.get("api_key") is not None:
                raise ValueError(
                    f"Route for model {top_cfg.get('id')!r} sets both "
                    f"api_key and api_keys; pick one."
                )

            api_key: str | None = None
            api_keys: list[str] | None = None
            if raw_api_keys is not None:
                if not isinstance(raw_api_keys, list):
                    raise ValueError(f"api_keys for {top_cfg.get('id')!r} must be a list")
                expanded = [expand_env(k) for k in raw_api_keys]
                kept: list[str] = []
                for raw, val in zip(raw_api_keys, expanded, strict=True):
                    if val is None or val == "":
                        logger.warning(
                            "Dropping blank api_keys entry for model %s "
                            "(template: %s) - env var unset or empty",
                            top_cfg.get("id"),
                            raw,
                        )
                        continue
                    if not isinstance(val, str):
                        raise ValueError(
                            f"api_keys entry for {top_cfg.get('id')!r} resolved to "
                            f"non-string value {val!r} (template: {raw!r})"
                        )
                    normalized = val.strip()
                    if not normalized:
                        logger.warning(
                            "Dropping whitespace-only api_keys entry for model %s (template: %s)",
                            top_cfg.get("id"),
                            raw,
                        )
                        continue
                    kept.append(normalized)
                if not kept:
                    raise ValueError(
                        f"api_keys for {top_cfg.get('id')!r} resolved to "
                        f"empty list after env expansion"
                    )
                api_keys = kept
            else:
                api_key = expand_env(raw_api_key)
                if isinstance(api_key, str):
                    api_key = api_key.strip()

                if (
                    isinstance(raw_api_key_template, str)
                    and raw_api_key_template.startswith("${")
                    and not api_key
                ):
                    logger.warning(
                        "Dropping blank api_key for model %s "
                        "(template: %s) - env var unset or empty",
                        top_cfg.get("id"),
                        raw_api_key_template,
                    )
                    continue

            # Adapter config inherits from top-level model config
            adapter_cfg = dict(top_cfg)
            # "type" is routing-only metadata, not a ModelConfig field
            adapter_cfg.pop("type", None)
            adapter_cfg["base_url"] = base_url
            adapter_cfg["api_key"] = api_key
            adapter_cfg["api_keys"] = api_keys
            # Normalize bracket-form openrouter kind to base "openrouter" for the
            # provider field. The bracketed form survives in `endpoint_id`
            # (via _make_provider_id called below) and `openrouter_pinned_provider`
            # (set inside _make_adapter), so per-pin circuit-breaker isolation is
            # preserved while analytics columns (api_logs.provider, Prometheus
            # labels) see a single "openrouter" cohort.
            provider_for_cfg, _ = parse_openrouter_kind(kind)
            adapter_cfg["provider"] = provider_for_cfg
            # Generate unique endpoint_id for availability tracking and circuit breaker
            adapter_cfg["endpoint_id"] = _make_provider_id(str(top_cfg["id"]), kind, base_url)

            route_provider_model_id = r.get("provider_model_id")
            if route_provider_model_id is not None:
                adapter_cfg["provider_model_id"] = expand_env(route_provider_model_id)

            # Route-level pricing override (key for cost-aware routing in Phase 2)
            if "pricing" in r:
                adapter_cfg["pricing"] = r["pricing"]

            # Route-level processor override (bypasses model-ID auto-detection)
            if "processor" in r:
                adapter_cfg["processor"] = r["processor"]

            # RouteWise subscription classification
            if "subscription_type" in r:
                adapter_cfg["subscription_type"] = r["subscription_type"]

            adapter = _make_adapter(kind, adapter_cfg)
            adapters_with_weights.append((adapter, weight))

        if not adapters_with_weights:
            logger.warning(
                "Skipping model %s - no valid routes after env expansion",
                top_cfg.get("id"),
            )
            continue

        # Determine model type: "embedding" models bypass RouteExecutor
        model_type = top_cfg.get("type") or top_cfg.get("model_type") or "chat"

        model_id = str(top_cfg["id"])  # type: ignore
        aliases = (top_cfg.get("aliases") or []) or []

        if model_type == "embedding" and embedding_adapters is not None:
            # Embedding models use a simple adapter dict (no weighted routing)
            if adapters_with_weights:
                adapter = adapters_with_weights[0][0]
                embedding_adapters[model_id] = adapter
                for alias in aliases:
                    embedding_adapters[alias] = adapter
            count += 1 + len(aliases)
        else:
            # Chat models go through the full RouteExecutor
            admin_only = bool(m.get("admin_only", False))
            required_role = str(m.get("required_role", "free"))
            # Validate required_role to prevent fail-open on typos
            from serving.config.settings import VALID_ROLES

            if required_role not in VALID_ROLES:
                import logging as _logging

                _logging.getLogger(__name__).warning(
                    "Model %s has invalid required_role '%s', defaulting to 'admin'",
                    model_id,
                    required_role,
                )
                required_role = "admin"
            router.register_route(
                model_id,
                adapters_with_weights,
                aliases=aliases,
                admin_only=admin_only,
                required_role=required_role,
            )
            count += 1 + len(aliases)

        # Collect per-model metadata for bootstrap (RouteWise strategy resolution)
        route_sub_types = [r.get("subscription_type", "api") for r in routes]
        model_infos.append(
            ModelRegistrationInfo(
                model_id=model_id,
                strategy=m.get("routing_strategy"),
                aliases=aliases,
                route_subscription_types=route_sub_types,
            )
        )

    return count, model_infos
