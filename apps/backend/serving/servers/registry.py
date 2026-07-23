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
    CodingIdentityAdapter,
    GeminiAdapter,
    ModelConfig,
    OpenAICompatAdapter,
    OpenRouterAdapter,
)
from serving.servers.embedding_fallback import FallbackEmbeddingAdapter

if TYPE_CHECKING:
    from pathlib import Path

    from routing.executor import RouteExecutor


@dataclass
class ModelRegistrationInfo:
    """Per-model metadata returned from YAML registration.

    Attributes:
        model_id: Canonical model identifier.
        strategy: DEPRECATED — legacy ``routing_strategy:`` value.  Read by
            existing bootstrap code; new code should use ``router`` instead.
        aliases: Alternate model_ids that share this model's route.
        router: Strategy name from ``models.yaml`` ``router:`` field
            (e.g. ``"fixed"``, ``"routewise"``).  ``None`` means "use
            ``default_router`` from routing.yaml".
        router_params: Raw params dict from ``models.yaml`` ``router_params:``,
            passed to the strategy's Pydantic model by ``ModelRouterRegistry``.
            ``None`` means "use strategy defaults".
    """

    model_id: str
    strategy: str | None = None
    aliases: list[str] = field(default_factory=list)
    router: str | None = None
    router_params: dict[str, Any] | None = None


class MissingEnvBackedKeyError(ValueError):
    """Raised when an env-backed route value (api_key/api_keys/base_url) resolves blank."""


_LOCAL_HOSTS = frozenset(("localhost", "127.0.0.1", "0.0.0.0", "host.docker.internal"))


def _make_provider_id(model_id: str, kind: str, base_url: str) -> str:
    """Generate a unique, user-friendly endpoint identifier.

    This ensures each endpoint has independent availability tracking
    and circuit breaker state, while being easy to understand at a glance.

    Format: "{model}:{location}"

    Examples:
        - glm-4.6 + sglang + http://localhost:12003       -> "glm-4.6:local-12003"
        - glm-4.6 + zai + https://api.z.ai/v4/          -> "glm-4.6:zai-api"
        - qwen3-coder + sglang + http://localhost:8003     -> "qwen3-coder:local-8003"
        - glm-4.7 + compat + http://host.docker.internal:8004 -> "glm-4.7:local-8004"
        - qwen3-coder + chutes + https://llm.chutes.ai    -> "qwen3-coder:chutes-api"
        - minimax-m2.7 + compat + https://api.minimax.io  -> "minimax-m2.7:minimax-api"

    Args:
        model_id: The model identifier (e.g., "glm-4.6", "qwen3-coder").
        kind: Adapter kind (e.g., "sglang", "zai", "chutes").
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
        if kind in ("openai_compat", "staging", "vllm", "sglang", "kimi"):
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


_OPENROUTER_KIND_RE = re.compile(r"^openrouter\[([A-Za-z0-9_.\-]+(?:/[A-Za-z0-9_.\-]+)*)\]$")


def parse_openrouter_kind(kind: str) -> tuple[str, str | None]:
    """Parse an adapter kind string, recognizing the OpenRouter bracket form.

    Returns a (base_kind, pinned_provider) tuple:
    - "openrouter"               -> ("openrouter", None)
    - "openrouter[deepinfra]"    -> ("openrouter", "deepinfra")
    - "openrouter[deepinfra/turbo]" -> ("openrouter", "deepinfra/turbo")
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
                "matching [A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)*"
            )
        return ("openrouter", match.group(1))
    return (kind, None)


def _make_adapter(kind: str, cfg: dict[str, Any]):
    """Construct a provider adapter from a kind string and model config.

    Args:
        kind: Adapter kind (``"vllm"``, ``"sglang"``, ``"claude"``, ``"deepseek"``, ``"gemini"``, ``"zai"``,
              ``"kimi"``, ``"kimi_coding"``, ``"minimax"``, ``"chutes"``, ``"featherless"``, ``"ollama"``,
              ``"cliproxy"``, ``"openai_compat"``, ``"staging"``, ``"openrouter"``, ``"openrouter[<slug>]"``).
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

    # DeepSeek routes through OpenAICompatAdapter with DeepSeek usage profile.
    # Request upstream usage in the stream so tool-call-only responses report
    # non-zero completion tokens instead of falling back to a text estimate.
    if kind == "deepseek":
        cfg = {**cfg, "provider_profile": "deepseek", "include_usage_in_stream": True}
    # ZAI is the Z.AI GLM coding plan: a non-/v1 chat path, and (like the Kimi
    # coding plan) gated on a coding-tool identity, so it uses CodingIdentityAdapter.
    elif kind == "zai":
        cfg = {
            **cfg,
            "provider_profile": "zai",
            "chat_path": "/chat/completions",
            "include_usage_in_stream": True,
        }
    # Kimi (Moonshot) routes through OpenAICompatAdapter; both the Kimi Code
    # coding-plan endpoint and the pay-per-token Moonshot API are OpenAI-compatible.
    # ``kimi_coding`` shares the usage profile but uses the dedicated
    # CodingIdentityAdapter (coding-tool User-Agent + leading OpenCode system message).
    elif kind in ("kimi", "kimi_coding"):
        cfg = {**cfg, "provider_profile": "kimi", "include_usage_in_stream": True}
    elif kind == "minimax":
        cfg = {**cfg, "provider_profile": "minimax", "include_usage_in_stream": True}
    elif kind == "sglang" or kind == "vllm":
        cfg = {**cfg, "include_usage_in_stream": True}

    model_cfg = ModelConfig(**cfg)

    # Coding-plan providers (Kimi coding plan, Z.AI GLM coding plan) gate access
    # on a coding-tool identity; CodingIdentityAdapter injects the User-Agent and
    # leading OpenCode system message (subject to the runtime toggle).
    if kind in ("kimi_coding", "zai"):
        return CodingIdentityAdapter(model_cfg)

    # All OpenAI-compatible services use the same adapter
    if kind in (
        "vllm",
        "sglang",
        "chutes",
        "featherless",
        "ollama",
        "cliproxy",
        "openai_compat",
        # "staging" is a clone of "openai_compat": same adapter, but its own
        # provider label so a second generic OpenAI-compatible endpoint can be
        # tracked independently in metrics/analytics.
        "staging",
        "deepseek",
        "kimi",
        "minimax",
    ):
        return OpenAICompatAdapter(model_cfg)

    if kind == "openrouter":
        return OpenRouterAdapter(model_cfg)

    if kind == "claude":
        return ClaudeAdapter(model_cfg)
    if kind == "gemini":
        return GeminiAdapter(model_cfg)
    if kind == "anthropic":
        return AnthropicAdapter(model_cfg)

    raise ValueError(f"Unknown adapter kind: {kind}")


def _dynamic_key_provider_name(kind: str, adapter_cfg: dict[str, Any]) -> str:
    from serving.adapters import dynamic_keys

    return dynamic_keys.normalize_key_provider(str(adapter_cfg.get("provider") or kind))


def register_from_models_yaml(
    router: RouteExecutor,
    path: Path,
    embedding_adapters: dict[str, Any] | None = None,
    *,
    continue_on_missing_env: bool = False,
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
              - kind: zai
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
        try:
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
                    "route_metadata",
                    "extra_body",
                )
            }
            # NOTE: top-level base_url is intentionally NOT expanded here. It is
            # expanded per-route in the loop below (raw_base_url -> base_url) so
            # the empty-base_url guard can still see the original ${VAR} template
            # when a route inherits the top-level value or the default single
            # route is synthesized. Expanding it in place would erase the
            # template and let an unset env-backed top-level base_url slip
            # through as a dead "<model>:unknown-api" endpoint.
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
            dynamic_key_registrations: list[tuple[str, object]] = []
            dynamic_key_providers: set[str] = set()
            for r in routes:
                kind = r.get("kind") or top_cfg.get("provider")
                raw_base_url = r.get("base_url") or top_cfg.get("base_url")
                base_url = expand_env(raw_base_url)
                weight = float(r.get("weight", 1.0))
                # Optional routes (e.g. a staging canary) degrade gracefully:
                # when their env-backed key resolves blank we skip just this
                # route, keeping the rest of the model, instead of dropping the
                # whole model via the model-level MissingEnvBackedKeyError catch.
                route_optional = bool(r.get("optional", False))

                # An env-backed base_url that resolves to unset/empty/host-less
                # cannot produce a working endpoint: _make_provider_id collapses
                # it to "<model>:unknown-api" and _build_url yields a host-less
                # "/v1/chat/completions", so every request stream-fails and trips
                # the circuit breaker. Treat it like a missing key — skip an
                # optional route, otherwise fail loudly — instead of silently
                # registering a dead provider.
                if isinstance(raw_base_url, str) and raw_base_url.startswith("${"):
                    from urllib.parse import urlparse

                    expanded_base = (base_url or "").strip()
                    if not expanded_base or urlparse(expanded_base).hostname is None:
                        if route_optional:
                            logger.warning(
                                "Skipping optional route (kind=%s) for model %s: "
                                "base_url resolved to unset/empty/host-less after "
                                "env expansion (template: %s)",
                                kind,
                                top_cfg.get("id"),
                                raw_base_url,
                            )
                            continue
                        raise MissingEnvBackedKeyError(
                            f"base_url for {top_cfg.get('id')!r} resolved to "
                            f"unset/empty/host-less after env expansion "
                            f"(template: {raw_base_url!r})"
                        )

                raw_api_keys = r.get("api_keys")
                raw_api_key = r.get("api_key") or top_cfg.get("api_key")

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
                                "Dropping whitespace-only api_keys entry for model %s "
                                "(template: %s)",
                                top_cfg.get("id"),
                                raw,
                            )
                            continue
                        kept.append(normalized)
                    if not kept:
                        if route_optional:
                            logger.warning(
                                "Skipping optional route (kind=%s) for model %s: "
                                "api_keys resolved to empty list after env expansion",
                                kind,
                                top_cfg.get("id"),
                            )
                            continue
                        raise MissingEnvBackedKeyError(
                            f"api_keys for {top_cfg.get('id')!r} resolved to "
                            f"empty list after env expansion"
                        )
                    api_keys = kept
                else:
                    api_key = expand_env(raw_api_key)
                    if (
                        isinstance(raw_api_key, str)
                        and raw_api_key.startswith("${")
                        and (api_key is None or not api_key.strip())
                    ):
                        if route_optional:
                            logger.warning(
                                "Skipping optional route (kind=%s) for model %s: "
                                "api_key resolved to empty/None after env expansion",
                                kind,
                                top_cfg.get("id"),
                            )
                            continue
                        raise MissingEnvBackedKeyError(
                            f"api_key for {top_cfg.get('id')!r} resolved to "
                            f"empty/None after env expansion"
                        )

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
                # preserved while the analytics column (api_logs.provider) sees
                # a single "openrouter" cohort.
                provider_for_cfg, _ = parse_openrouter_kind(kind)
                # Preserve explicit model-level provider only for logical OpenAI
                # adapters where route kind remains OpenAI-compatible and the
                # model declares provider: openai. This keeps GPT-style models
                # surfaced as owned_by=openai, while leaving other cross-kind
                # variants (e.g., zai->ollama/chutes, minimax->ollama) unchanged.
                if kind == "openai_compat" and top_cfg.get("provider") == "openai":
                    adapter_cfg["provider"] = "openai"
                else:
                    adapter_cfg["provider"] = provider_for_cfg
                # Generate unique endpoint_id for availability tracking and circuit breaker
                adapter_cfg["endpoint_id"] = _make_provider_id(str(top_cfg["id"]), kind, base_url)

                route_provider_model_id = r.get("provider_model_id")
                if route_provider_model_id is not None:
                    adapter_cfg["provider_model_id"] = expand_env(route_provider_model_id)

                # Route-level request body defaults extend or override model defaults.
                # Always write back (even when empty) so a None inherited from
                # top_cfg is normalized to {}; ModelConfig stores an explicit
                # None as-is, which then breaks `{**extra_body}` at request time.
                extra_body = dict(adapter_cfg.get("extra_body") or {})
                if isinstance(r.get("extra_body"), dict):
                    extra_body.update(r["extra_body"])
                adapter_cfg["extra_body"] = extra_body

                # Route-level pricing override (key for cost-aware routing in Phase 2)
                if "pricing" in r:
                    adapter_cfg["pricing"] = r["pricing"]

                # Route-level processor override (bypasses model-ID auto-detection)
                if "processor" in r:
                    adapter_cfg["processor"] = r["processor"]

                # Route-level input_modalities override (default: inherit the
                # model-level declaration). Lets a narrower fallback (e.g. a
                # text-only mirror of a vision model) advertise fewer modalities
                # than the model as a whole, so the modality-aware router never
                # dispatches media a route can't accept to that route.
                if "input_modalities" in r:
                    adapter_cfg["input_modalities"] = r["input_modalities"]

                # RouteWise provider category classification
                route_metadata = dict(adapter_cfg.get("route_metadata") or {})
                if isinstance(r.get("route_metadata"), dict):
                    route_metadata.update(r["route_metadata"])
                if "provider_type" in r:
                    adapter_cfg["provider_type"] = r["provider_type"]
                    route_metadata["provider_type"] = r["provider_type"]
                for routewise_key in (
                    "routewise_pool",
                    "quota_pool",
                    "concurrency_pool",
                    "quota_source",
                    "quota",
                    "concurrency",
                ):
                    if routewise_key in r:
                        adapter_cfg[routewise_key] = r[routewise_key]
                if route_metadata:
                    adapter_cfg["route_metadata"] = route_metadata

                adapter = _make_adapter(kind, adapter_cfg)
                adapters_with_weights.append((adapter, weight))

                # Register adapter for runtime key-pool management. We always
                # mark the provider as known (whitelist) and register every
                # pool-capable adapter — including single-``api_key`` ones,
                # which are promoted to a pool lazily when an admin adds a
                # runtime key (see ``OpenAICompatAdapter.add_runtime_key``).
                # Registering only pre-built pools here would make dashboard
                # keys silently no-op against single-key adapters.
                provider_key = _dynamic_key_provider_name(kind, adapter_cfg)
                dynamic_key_providers.add(provider_key)
                pool_capable = hasattr(adapter, "add_runtime_key") or (
                    getattr(adapter, "_key_pool", None) is not None
                )
                if pool_capable:
                    dynamic_key_registrations.append((provider_key, adapter))

            # Determine model type: "embedding" models bypass RouteExecutor
            model_type = top_cfg.get("type") or top_cfg.get("model_type") or "chat"

            model_id = str(top_cfg["id"])  # type: ignore
            aliases = (top_cfg.get("aliases") or []) or []

            if model_type == "embedding" and embedding_adapters is not None:
                # Embedding models bypass the weighted RouteExecutor. Register
                # the routes (in YAML order) as an ordered fallback chain: the
                # /v1/embeddings endpoint tries the primary first and falls
                # through to later routes (e.g. a staging canary) only when an
                # earlier one fails. A single-route model keeps using its plain
                # adapter so behavior is unchanged when there is no fallback.
                if adapters_with_weights:
                    ordered = [adapter for adapter, _ in adapters_with_weights]
                    emb_adapter = (
                        ordered[0] if len(ordered) == 1 else FallbackEmbeddingAdapter(ordered)
                    )
                    embedding_adapters[model_id] = emb_adapter
                    for alias in aliases:
                        embedding_adapters[alias] = emb_adapter
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

            from serving.adapters import dynamic_keys

            for provider_key in dynamic_key_providers:
                dynamic_keys.register_known_provider(provider_key)
            for provider_key, adapter in dynamic_key_registrations:
                dynamic_keys.register_adapter_for_provider(provider_key, adapter)

            # Collect per-model metadata for bootstrap (RouteWise strategy resolution)
            model_infos.append(
                ModelRegistrationInfo(
                    model_id=model_id,
                    strategy=m.get("routing_strategy"),
                    aliases=aliases,
                    router=m.get("router"),
                    router_params=m.get("router_params"),
                )
            )
        except MissingEnvBackedKeyError as exc:
            if not continue_on_missing_env:
                raise
            logger.warning("Skipping model %r from %s: %s", m.get("id"), path, exc)
            continue

    return count, model_infos
