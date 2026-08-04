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
from serving.config.provider_labels import DISPLAY_NAME_METADATA_KEY
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


_PROVIDER_LABEL_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

# Adapter kinds `_make_adapter` knows how to build, plus the provider labels it
# derives from them. A route may not borrow one of these as a custom label:
# `api_logs.provider` drives quota reporting, the admin disable switch, and
# weight overrides, so relabelling a local vLLM box as "zai" would fold its
# traffic into an unrelated provider's cohort. "" and "router" are the
# gateway's own synthetic labels (see admin/providers.py) and are reserved too.
#
# test_registry_provider_label.py asserts this set stays in sync with
# `_make_adapter`'s dispatch.
RESERVED_PROVIDER_LABELS = frozenset(
    {
        "",
        "anthropic",
        "chutes",
        "claude",
        "cliproxy",
        "deepseek",
        "featherless",
        "gemini",
        "kimi",
        "kimi_coding",
        "minimax",
        "ollama",
        "openai",
        "openai_compat",
        "openrouter",
        "router",
        "sglang",
        "staging",
        "vllm",
        "zai",
    }
)

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


def parse_route_provider_label(
    route: dict[str, Any],
    canonical_provider: str,
    model_id: str,
) -> tuple[str, str | None]:
    """Resolve one route's provider label and optional display name.

    A route may override the label it reports to analytics with ``provider:``,
    and give that label a human-readable name with ``provider_display_name:``::

        route:
          - kind: vllm
            provider: local-a               # api_logs.provider / dashboard cohort
            provider_display_name: "Local box A"
            base_url: ${LOCAL_A_URL}

    Without ``provider:`` the label is ``canonical_provider`` (derived from
    ``kind``) and the display name comes from the built-in table, exactly as
    before. Only the label changes: the adapter, its API-key pool, and quota
    accounting all stay bound to the route's kind.

    Args:
        route: One entry of a model's ``route:`` list.
        canonical_provider: Label the route would carry with no override.
        model_id: Model the route belongs to, for error messages.

    Returns:
        Tuple of (provider label, display name or ``None``).

    Raises:
        ValueError: When the label is not a slug, collides with a built-in
            provider other than the route's own, or the display name is blank.
    """
    raw_label = route.get("provider")
    raw_display_name = route.get("provider_display_name")

    if raw_display_name is not None and not (
        isinstance(raw_display_name, str) and raw_display_name.strip()
    ):
        raise ValueError(f"provider_display_name for model {model_id!r} must be a non-empty string")
    display_name = raw_display_name.strip() if isinstance(raw_display_name, str) else None

    if raw_label is None:
        return (canonical_provider, display_name)
    if not isinstance(raw_label, str) or not _PROVIDER_LABEL_RE.fullmatch(raw_label):
        raise ValueError(
            f"Route provider label {raw_label!r} for model {model_id!r} must use "
            f"lowercase letters, numbers, dashes, or underscores (max 64 chars)"
        )
    if raw_label != canonical_provider and raw_label in RESERVED_PROVIDER_LABELS:
        raise ValueError(
            f"Route provider label {raw_label!r} for model {model_id!r} is reserved "
            f"by a built-in provider; pick a distinct label such as "
            f"{canonical_provider}-1"
        )
    return (raw_label, display_name)


def _dynamic_key_provider_name(kind: str, adapter_cfg: dict[str, Any]) -> str:
    from serving.adapters import dynamic_keys

    # A relabelled route keeps its API keys in the pool of the provider it
    # actually talks to, not under its dashboard label — two local boxes with
    # distinct labels still share one LOCAL_API_KEY pool. The registry records
    # that canonical provider in route_metadata when it applies a label.
    metadata = adapter_cfg.get("route_metadata") or {}
    canonical = metadata.get("key_provider") or adapter_cfg.get("provider") or kind
    return dynamic_keys.normalize_key_provider(str(canonical))


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
              # Optional per-route relabelling: two endpoints of the same kind
              # report as separate providers in api_logs and the dashboard.
              - kind: vllm
                weight: 1.0
                provider: local-a
                provider_display_name: "Local box A"
                base_url: ${LOCAL_A_URL}

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
    # A model whose credential is unset is skipped, one warning per model, deep
    # in the log. Someone following the quickstart sees only an empty
    # /v1/models and then "Model not found", which sends them looking for a
    # typo. Collect what was dropped and say so once, plainly, at the end.
    skipped_for_missing_env: list[str] = []
    unset_env_vars: set[str] = set()
    # Every alias seen so far, and the model that claimed it. `register_route`
    # writes aliases into the route table unconditionally, so without this the
    # second model to claim a name silently wins and the first one's alias
    # resolves to somebody else's model — with nothing anywhere saying so.
    alias_owner: dict[str, str] = {}
    for m in models:
        try:
            # Environment expansion for base_url/api_key in both top-level and route entries
            def expand_env(val: str | None) -> str | None:
                if isinstance(val, str) and val.startswith("${") and val.endswith("}"):
                    return os.getenv(val[2:-1])
                return val

            # Build primary config
            # Only carry keys the YAML actually sets: `m.get(k)` would inject
            # None for absent optional fields, and that None overwrites the
            # ModelConfig dataclass default downstream (an omitted
            # `quantization` became None and made GET /v1/models fail schema
            # validation with a 500). Presence-based copying keeps an explicit
            # `key: null` meaningful while letting defaults apply otherwise.
            top_cfg = {
                k: m[k]
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
                    "processor",
                )
                if k in m
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
                            if isinstance(raw, str) and raw.startswith("${") and raw.endswith("}"):
                                unset_env_vars.add(raw[2:-1])
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
                    canonical_provider = "openai"
                else:
                    canonical_provider = provider_for_cfg
                # A route may relabel itself so that endpoints sharing a kind
                # (e.g. two local GPU boxes on vLLM) stay separate cohorts in
                # api_logs and every provider-scoped dashboard view.
                provider_label, provider_display_name = parse_route_provider_label(
                    r, canonical_provider, str(top_cfg.get("id"))
                )
                adapter_cfg["provider"] = provider_label
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
                # Pin a relabelled route's *upstream* identity in metadata. The
                # label owns analytics only; key pools, the admin Routing tab,
                # and route-target resolution keep following the route's kind.
                # Both keys carry the same value so RouteWise does not read the
                # pair as an override provider and switch to local quota state.
                #
                # setdefault, not assignment: an explicit route_metadata in the
                # YAML is the operator declaring the upstream themselves, which
                # is how the override-provider quota pattern is configured
                # (route_provider != upstream_provider). Overwriting it here
                # would silently undo that declaration.
                if provider_label != canonical_provider:
                    route_metadata.setdefault("key_provider", canonical_provider)
                    route_metadata.setdefault("route_provider", canonical_provider)
                    route_metadata.setdefault("upstream_provider", canonical_provider)
                if provider_display_name:
                    route_metadata[DISPLAY_NAME_METADATA_KEY] = provider_display_name
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

            # An ambiguous alias is a configuration error, and it is reported
            # rather than enforced. `register_route` writes aliases into the
            # route table unconditionally, so the second model to claim a name
            # already wins today — and refusing to start would turn a
            # deployment that has been serving that way into one that will not
            # boot, from a change whose whole purpose is to hand the cloud
            # agent a translation table.
            #
            # So: warn here, fail in CI against the shipped configuration
            # (`test_shipped_config_has_no_ambiguous_alias`), and make it
            # fail-closed at startup only once the live configs are known
            # clean. Routing is untouched either way.
            for alias in aliases:
                previous = alias_owner.get(str(alias))
                if previous is not None and previous != model_id:
                    import logging as _logging

                    _logging.getLogger(__name__).warning(
                        "alias %r is claimed by both %r and %r; it resolves to "
                        "whichever loads last, so the same request means "
                        "different things depending on YAML order",
                        alias,
                        previous,
                        model_id,
                    )
                alias_owner[str(alias)] = model_id

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
            skipped_for_missing_env.append(str(m.get("id")))
            continue

    if skipped_for_missing_env:
        missing = ", ".join(sorted(unset_env_vars)) or "the referenced environment variables"
        dropped = ", ".join(skipped_for_missing_env)
        if count == 0:
            logger.error(
                "No models are available: every model in %s was skipped because "
                "its credential is unset. Set %s and restart. /v1/models will "
                "stay empty until then, and requests will report the model as "
                "not found. Skipped: %s",
                path,
                missing,
                dropped,
            )
        else:
            logger.warning(
                "%d of %d models in %s are unavailable because their credentials "
                "are unset (%s). Skipped: %s",
                len(skipped_for_missing_env),
                len(models),
                path,
                missing,
                dropped,
            )

    return count, model_infos
