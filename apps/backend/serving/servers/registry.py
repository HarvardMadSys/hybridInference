"""Model registry and configuration loader.

This module builds provider adapters from configuration and registers them on a
``FixedRouter``. Runtime catalogs come from Admin/database documents; explicit
legacy file and environment registration functions remain compatibility APIs.
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING, Any

import yaml

logger = logging.getLogger(__name__)

from serving.adapters import (
    AnthropicAdapter,
    ClaudeAdapter,
    GeminiAdapter,
    ModelConfig,
    OpenAICompatAdapter,
    OpenRouterAdapter,
)
from serving.adapters.base import validate_deployment_type
from serving.adapters.reasoning_effort import EFFORT_MAP_FIELD, validate_effort_map
from serving.config.app_config import config_value
from serving.config.app_config_registry import static_entry
from serving.config.provider_labels import DISPLAY_NAME_METADATA_KEY
from serving.model_access import default_required_role_for_model
from serving.servers.embedding_fallback import FallbackEmbeddingAdapter

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from routing.routers import FixedRouter


@dataclass
class ModelRegistrationInfo:
    """Per-model metadata returned from YAML registration.

    Attributes:
        model_id: Canonical model identifier.
        aliases: Alternate model_ids that share this model's route.
        router: Strategy name from ``models.yaml`` ``router:`` field
            (e.g. ``"fixed"``, ``"routewise"``).  ``None`` means "use
            ``default_router`` from routing.yaml".
        router_params: Raw params dict from ``models.yaml`` ``router_params:``,
            passed to the strategy's Pydantic model by ``ModelRouterRegistry``.
            ``None`` means "use strategy defaults".
    """

    model_id: str
    aliases: list[str] = field(default_factory=list)
    router: str | None = None
    router_params: dict[str, Any] | None = None


@dataclass
class ModelLoadReport:
    """What :func:`register_from_models_yaml` had to leave out, for callers to surface.

    Attributes:
        skipped_models: Models skipped because a setting their routes need is
            unset, each with its aliases.
        unset_env_vars: The settings those models were waiting for, where the
            template names one.
    """

    skipped_models: dict[str, list[str]] = field(default_factory=dict)
    unset_env_vars: set[str] = field(default_factory=set)


class MissingEnvBackedKeyError(ValueError):
    """Raised when an env-backed route value (api_key/api_keys/base_url) resolves blank."""


def _template_var(value: object) -> str | None:
    """Return ``VAR`` for a whole-value ``${VAR}`` template, else ``None``."""
    if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
        return value[2:-1]
    return None


def resolve_reference(name: str) -> str | None:
    """Resolve the setting a ``${VAR}`` reference names, as the Settings tab reports it.

    The stored setting, then the environment (``config_value``), then the
    registered default of a static setting that has one — ``${CHUTES_BASE_URL}``
    works unset, the way the console shows it configured. ``None`` when nothing
    sets it. A stored or environment value wins even when empty.
    """
    value = config_value(name)
    if value is not None:
        return value
    entry = static_entry(name)
    return entry.default if entry is not None and entry.default else None


class RegistryConfigError(ValueError):
    """A catalog entry that is wrong, not merely unready: nothing is registered.

    Registration is all or nothing, so one bad entry cannot leave the process
    serving the models listed before it and silently dropping the rest. A
    missing setting is different: that model is skipped (see
    :class:`MissingEnvBackedKeyError`). The message names the model, the route
    and the field, never a configured value.
    """


class EmbeddingsPathConfigError(RegistryConfigError):
    """Reject an invalid embeddings path without serving a partial model registry."""


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
RESERVED_PROVIDER_LABELS = {
    "",
    "anthropic",
    "chutes",
    "claude",
    "cliproxy",
    "deepseek",
    "featherless",
    "gemini",
    "kimi",
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
_BUILT_IN_ADAPTER_KINDS = frozenset(RESERVED_PROVIDER_LABELS - {"", "openai", "router"})
ADAPTER_FACTORIES: dict[str, Callable[[dict[str, Any]], Any]] = {}


def register_adapter_factory(
    kind: str,
    factory: Callable[[dict[str, Any]], Any],
    *,
    override: bool = False,
) -> None:
    """Register a trusted startup extension's adapter factory.

    Factories receive ModelConfig keyword arguments before built-in defaults.
    A built-in kind requires an explicit override; an extension kind can only
    be registered once. The kind also becomes a reserved provider label.
    """
    if not isinstance(kind, str) or not _PROVIDER_LABEL_RE.fullmatch(kind):
        raise ValueError("Adapter kind must be a lowercase slug (max 64 characters)")
    if not callable(factory):
        raise TypeError("Adapter factory must be callable")
    if kind in ADAPTER_FACTORIES:
        raise ValueError(f"Adapter factory already registered: {kind}")
    if kind in RESERVED_PROVIDER_LABELS and kind not in _BUILT_IN_ADAPTER_KINDS:
        raise ValueError(f"Adapter kind is reserved: {kind}")
    if kind in _BUILT_IN_ADAPTER_KINDS and not override:
        raise ValueError(f"Overriding built-in adapter kind {kind!r} requires override=True")
    ADAPTER_FACTORIES[kind] = factory
    RESERVED_PROVIDER_LABELS.add(kind)
    logger.info(
        "Registered backend extension adapter kind %s (built-in override: %s)",
        kind,
        kind in _BUILT_IN_ADAPTER_KINDS,
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


# Kinds whose upstream answers ``stream_options.include_usage`` with a final
# usage chunk. This is a *default* only: a route may set
# ``include_usage_in_stream`` explicitly, true or false, and that value wins.
# The set is a statement about the protocol each kind speaks, never about who
# owns the endpoint -- a provider label in the catalog says nothing about it.
_STREAM_USAGE_DEFAULT_KINDS = frozenset({"deepseek", "zai", "kimi", "minimax", "sglang", "vllm"})


#: The largest context or output length a catalog entry may declare.
_MAX_TOKENS = 10_000_000

# Fields that state a fact about one endpoint, each with why a model cannot
# hold it -- not even a shorthand model, whose one synthesized route has no
# `route:` block for a reviewer to see the declaration on.
_ROUTE_ONLY_FIELDS = {
    "deployment_type": "",
    "embeddings_path": "",
    "include_usage_in_stream": (
        ": whether an upstream answers stream_options.include_usage is a fact about "
        "one endpoint, not about every route a model has"
    ),
    "null_cache_details_means_miss": (
        ": it states that one server was started with cache reporting on, which "
        "cannot be true of every route a model has"
    ),
}


def _json_type(value: object) -> str:
    """Name a value's JSON type, for a message that must not show the value."""
    if value is None:
        return "null"
    for kind, name in (
        (bool, "a boolean"),
        (str, "a string"),
        ((int, float), "a number"),
        (list, "a list"),
        (dict, "an object"),
    ):
        if isinstance(value, kind):
            return name
    return type(value).__name__


def check_models_document(data: Any) -> list[Any]:
    """Check a whole catalog the way :func:`check_model_entry` checks one entry.

    Keys other than ``models`` are left to their own readers (``auto``) or
    ignored, as registration ignores them: a catalog carried over from YAML
    keeps its anchor blocks. A name claimed by two models is registration's to
    warn about, not a reason to refuse the catalog.

    Returns:
        The model entries; an absent or null ``models`` is an empty catalog.

    Raises:
        RegistryConfigError: The first entry registration would refuse.
    """
    if not isinstance(data, dict):
        raise RegistryConfigError("the model catalog must be an object")
    models = data.get("models")
    if models is None:
        return []
    if not isinstance(models, list):
        raise RegistryConfigError("models must be a list")
    for index, model in enumerate(models):
        check_model_entry(model, index=index)
    return models


def check_model_entry(model: Any, *, index: int = 0) -> None:
    """Refuse a catalog entry that registration would refuse or build wrongly.

    The one rule set for an entry. Registration runs it over every entry
    before it builds anything, and Admin runs it (through
    ``serving.config.documents.validate``) before a catalog is saved, so a
    saved catalog registers and one registration refuses is never saved. It
    is pure: it reads no setting and touches no router, so a ``${VAR}`` that
    is unset or resolves to something unusable stays registration's to report.

    Each message names the entry, the route and the field, and what is wrong
    with it; never a configured value, so the Settings tab can show it.

    Args:
        model: One entry of the catalog's ``models`` list.
        index: Its position, to name an entry whose ``id`` is unusable.

    Raises:
        RegistryConfigError: The entry is not one registration accepts.
    """
    if not isinstance(model, dict):
        raise RegistryConfigError(f"each model must be an object (models[{index}])")
    model_id = model.get("id")
    if not isinstance(model_id, str) or not model_id.strip():
        raise RegistryConfigError(f"id must be a non-empty string (models[{index}])")
    where = f"model {model_id!r}"
    for name, reason in _ROUTE_ONLY_FIELDS.items():
        if name in model:
            error = EmbeddingsPathConfigError if name == "embeddings_path" else RegistryConfigError
            raise error(
                f"{name} must be declared on a route, not on the model (including shorthand "
                f"models without a route list){reason} ({where})"
            )
    aliases = model.get("aliases")
    if aliases and (
        not isinstance(aliases, list)
        or not all(isinstance(alias, str) and alias.strip() for alias in aliases)
    ):
        raise RegistryConfigError(f"aliases must be a list of non-empty strings ({where})")
    # An embedding model produces no output tokens, so it declares 0.
    for name, least in (("context_length", 1), ("max_output_length", 0)):
        limit = model.get(name)
        if limit is not None and (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not least <= limit <= _MAX_TOKENS
        ):
            raise RegistryConfigError(
                f"{name} must be a whole number of tokens from {least} to {_MAX_TOKENS:,} ({where})"
            )
    # Registration copies these into every route's adapter configuration.
    for name in ("extra_body", "route_metadata"):
        if model.get(name) and not isinstance(model[name], dict):
            raise RegistryConfigError(f"{name} must be an object ({where})")
    if model.get("router") is not None:
        _check_router(model["router"], model.get("router_params"), where)
    routes = model.get("route")
    if not routes:
        # Registration gives a model without a route list one route of the
        # kind its provider names.
        _check_route(model, {}, where)
        return
    if not isinstance(routes, list) or not all(isinstance(route, dict) for route in routes):
        raise RegistryConfigError(f"route must be a list of objects ({where})")
    for number, route in enumerate(routes, start=1):
        _check_route(model, route, f"{where}, route {number}")


def _check_router(name: Any, params: Any, where: str) -> None:
    """Refuse a model's ``router`` / ``router_params`` the strategy would refuse at boot."""
    from pydantic import ValidationError

    from routing.strategies import validate_router_config
    from serving.config.documents import describe_validation_error

    try:
        validate_router_config(name, params)
    except ValidationError as exc:
        raise RegistryConfigError(
            f"router_params is not valid for this router: {describe_validation_error(exc)} "
            f"({where})"
        ) from None
    except (TypeError, ValueError):
        raise RegistryConfigError(
            f"router names no routing strategy this gateway has ({where})"
        ) from None


def _check_route(model: dict[str, Any], route: dict[str, Any], where: str) -> None:
    """Refuse one route the way registration would; ``{}`` stands for a synthesized one."""
    # A route without a kind is served by the adapter its model's provider names.
    kind, kind_field = (
        (route["kind"], "kind") if route.get("kind") else (model.get("provider"), "provider")
    )
    if not kind:
        raise RegistryConfigError(
            f"kind is required: name the route's adapter, or the model's provider ({where})"
        )
    if not isinstance(kind, str):
        raise RegistryConfigError(f"{kind_field} must be a string ({where})")
    try:
        base_kind, _pinned = parse_openrouter_kind(kind)
    except ValueError:
        raise RegistryConfigError(
            f"{kind_field} must be openrouter or openrouter[<provider slug>] to pin an "
            f"OpenRouter upstream ({where})"
        ) from None
    if base_kind not in _BUILT_IN_ADAPTER_KINDS and base_kind not in ADAPTER_FACTORIES:
        raise RegistryConfigError(
            f"{kind_field} names no adapter kind this gateway has; another kind needs a "
            f"backend extension that registers it ({where})"
        )
    weight = route.get("weight", 1.0)
    if (
        isinstance(weight, bool)
        or not isinstance(weight, (int, float))
        or not math.isfinite(weight)
        or weight < 0
    ):
        raise RegistryConfigError(f"weight must be a number, 0 or more ({where})")
    if "deployment_type" in route:
        try:
            validate_deployment_type(route["deployment_type"])
        except ValueError as exc:
            raise RegistryConfigError(f"{exc} ({where})") from None
    for flag in ("include_usage_in_stream", "null_cache_details_means_miss"):
        # `bool("false")` is True: a quoted scalar would turn an explicit
        # opt-out into an opt-in with nothing in the log. null is refused too:
        # absent means "keep the kind default", which is not "explicitly off".
        if flag in route and not isinstance(route[flag], bool):
            raise RegistryConfigError(
                f"{flag} must be a boolean, not {_json_type(route[flag])} ({where})"
            )
    if route.get("embeddings_path") is not None:
        _check_embeddings_path(route["embeddings_path"], where)
    _check_provider_label(model, route, kind, base_kind, where)
    api_keys = route.get("api_keys")
    if api_keys is not None:
        if route.get("api_key") is not None:
            raise RegistryConfigError(f"api_key and api_keys are both set; pick one ({where})")
        if not isinstance(api_keys, list):
            raise RegistryConfigError(f"api_keys must be a list ({where})")
        if any(key is not None and not isinstance(key, str) for key in api_keys):
            raise RegistryConfigError(f"api_keys entries must be strings ({where})")
    # A route-level value replaces the model's, as registration copies them.
    processor = route["processor"] if "processor" in route else model.get("processor")
    if processor:
        # The table get_processor dispatches on; an unknown name fails there.
        from serving.adapters.processors import _PROCESSOR_MAP

        if not isinstance(processor, str) or processor not in _PROCESSOR_MAP:
            raise RegistryConfigError(
                f"processor must be one of: {', '.join(sorted(_PROCESSOR_MAP))} ({where})"
            )
    schedule = (
        route["pricing_schedule"] if "pricing_schedule" in route else model.get("pricing_schedule")
    )
    source = route["pricing_source"] if "pricing_source" in route else model.get("pricing_source")
    if schedule is not None or source is not None:
        # The adapter configuration validates them together; its messages
        # name fields and keys, never values.
        try:
            ModelConfig(
                id="",
                name="",
                provider="",
                base_url="",
                pricing_schedule=schedule,
                pricing_source=source,
            )
        except (TypeError, ValueError) as exc:
            raise RegistryConfigError(f"{exc} ({where})") from None


def _check_embeddings_path(path: Any, where: str) -> None:
    """Refuse an explicit embeddings path; a ``${VAR}`` is checked once it resolves."""
    if not isinstance(path, str):
        raise EmbeddingsPathConfigError(f"embeddings_path must be a string or null ({where})")
    template = path.strip()
    if path and not template:
        raise EmbeddingsPathConfigError(f"embeddings_path must not be whitespace-only ({where})")
    if _template_var(template) is None and ".." in template.split("/"):
        raise EmbeddingsPathConfigError(
            f"embeddings_path must not contain '..' path segments ({where})"
        )


def _check_provider_label(
    model: dict[str, Any], route: dict[str, Any], kind: str, base_kind: str, where: str
) -> None:
    """Refuse a route label :func:`parse_route_provider_label` would refuse."""
    display_name = route.get("provider_display_name")
    if display_name is not None and not (isinstance(display_name, str) and display_name.strip()):
        raise RegistryConfigError(f"provider_display_name must be a non-empty string ({where})")
    label = route.get("provider")
    if label is None:
        return
    if not isinstance(label, str) or not _PROVIDER_LABEL_RE.fullmatch(label):
        raise RegistryConfigError(
            "provider must use lowercase letters, numbers, dashes or underscores "
            f"(at most 64 characters) ({where})"
        )
    canonical = (
        "openai" if kind == "openai_compat" and model.get("provider") == "openai" else base_kind
    )
    if label != canonical and label in RESERVED_PROVIDER_LABELS:
        raise RegistryConfigError(
            f"provider is reserved by a built-in provider; pick a distinct label such as "
            f"{canonical}-1 ({where})"
        )


def _make_adapter(kind: str, cfg: dict[str, Any]):
    """Construct a provider adapter from a kind string and model config.

    Args:
        kind: Adapter kind (``"vllm"``, ``"sglang"``, ``"claude"``, ``"deepseek"``, ``"gemini"``, ``"zai"``,
              ``"kimi"``, ``"minimax"``, ``"chutes"``, ``"featherless"``, ``"ollama"``,
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
            "openrouter_pinned_provider": pinned_provider,
        }
        kind = base_kind  # subsequent dispatch checks compare against the bare kind

    factory = ADAPTER_FACTORIES.get(kind)
    if factory is not None:
        return factory(dict(cfg))

    if kind == "openrouter":
        cfg = {**cfg, "provider_profile": "openrouter"}

    # DeepSeek routes through OpenAICompatAdapter with DeepSeek usage profile.
    if kind == "deepseek":
        cfg = {**cfg, "provider_profile": "deepseek"}
    # ZAI uses an OpenAI-compatible API with a non-/v1 chat path.
    elif kind == "zai":
        cfg = {
            **cfg,
            "provider_profile": "zai",
            "chat_path": "/chat/completions",
        }
    elif kind == "kimi":
        cfg = {**cfg, "provider_profile": "kimi"}
    elif kind == "minimax":
        cfg = {**cfg, "provider_profile": "minimax"}
    elif kind == "vllm":
        cfg = {**cfg, "vllm_structured_outputs": True}

    # Request upstream usage in the stream so cached tokens, and completion
    # tokens on tool-call-only responses, are measured instead of estimated.
    # Presence, not truthiness: `include_usage_in_stream` is a route-level
    # capability, so a value the deployment configured -- including False on a
    # backend that rejects unknown body fields -- must never be overwritten by
    # a kind default. The kinds below are only where it is assumed on.
    if kind in _STREAM_USAGE_DEFAULT_KINDS and "include_usage_in_stream" not in cfg:
        cfg = {**cfg, "include_usage_in_stream": True}

    model_cfg = ModelConfig(**cfg)

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
        "zai",
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


def _parse_role_gate(model: dict[str, Any], model_id: str) -> tuple[bool, str]:
    """Parse a model entry's access gate: ``(admin_only, required_role)``.

    Shared by the chat and embedding registration branches so both spellings
    of the model-level tier declaration validate identically: an omitted
    ``required_role`` reads as the same cost-derived default the registry and
    the public page apply (:func:`default_required_role_for_model` — paid or
    unknown-cost models publish as ``pro``; provably free ones as ``free``),
    an invalid ``required_role`` fails closed to ``admin`` (a typo must gate
    the model, not open it), and ``admin_only: true`` stays the older
    spelling of ``required_role: admin``. ``admin_only`` is kept as written:
    beside an explicit ``free`` it still means admin (the legacy precedence
    ``register_route`` applies too), beside any other explicit role that
    role is the gate.
    """
    admin_only = bool(model.get("admin_only", False))
    required_role = default_required_role_for_model(model)
    # Validate required_role to prevent fail-open on typos
    from serving.config.settings import VALID_ROLES

    if required_role not in VALID_ROLES:
        logging.getLogger(__name__).warning(
            "Model %s has invalid required_role '%s', defaulting to 'admin'",
            model_id,
            required_role,
        )
        required_role = "admin"
    return admin_only, required_role


def _register_embedding_model(
    router: FixedRouter,
    embedding_adapters: dict[str, Any],
    model_id: str,
    adapters_with_weights: list[tuple[Any, float]],
    emb_adapter: Any,
    *,
    aliases: list[str],
    admin_only: bool,
    required_role: str,
) -> None:
    """Publish one embedding model's adapter and its access gate, aliases included."""
    embedding_adapters[model_id] = emb_adapter
    for alias in aliases:
        embedding_adapters[alias] = emb_adapter
    router.register_embedding_route(
        model_id,
        adapters_with_weights,
        aliases=aliases,
        admin_only=admin_only,
        required_role=required_role,
    )
    # Every adapter key, including aliases, must have a gate.
    _gate = router.embedding_routes
    for _key in [model_id, *aliases]:
        assert _key in _gate and _key in embedding_adapters, (
            f"embedding registration invariant violated for {_key!r}: "
            "adapter table and access-gate record must both exist"
        )


def _register_key_adapters(providers: set[str], registrations: list[tuple[str, object]]) -> None:
    """Hand one model's adapters to runtime key-pool management."""
    from serving.adapters import dynamic_keys

    for provider_key in providers:
        dynamic_keys.register_known_provider(provider_key)
    for provider_key, adapter in registrations:
        dynamic_keys.register_adapter_for_provider(provider_key, adapter)


def register_from_models_yaml(
    router: FixedRouter,
    path: Path,
    embedding_adapters: dict[str, Any] | None = None,
    *,
    continue_on_missing_env: bool = False,
    report: ModelLoadReport | None = None,
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
        embedding_adapters: Receives the embedding models' adapters.
        continue_on_missing_env: Skip a model whose required setting is unset
            instead of raising.
        report: Receives the models skipped that way.

    Returns:
        Tuple of (count of registered route identifiers including aliases,
        list of ModelRegistrationInfo for each model).
    """
    if not path.exists():
        return 0, []
    return register_from_models_text(
        router,
        path.read_text(),
        embedding_adapters=embedding_adapters,
        continue_on_missing_env=continue_on_missing_env,
        report=report,
        origin=str(path),
    )


def register_from_models_text(
    router: FixedRouter,
    text: str,
    embedding_adapters: dict[str, Any] | None = None,
    *,
    continue_on_missing_env: bool = False,
    report: ModelLoadReport | None = None,
    origin: str = "the database route catalog",
) -> tuple[int, list[ModelRegistrationInfo]]:
    """Register models and routes from a YAML document.

    Args:
        router: Executor to receive registered routes.
        text: YAML document. ``${VAR}`` references stay unresolved in the text
            and resolve here, the same way the file loader resolves them.
        embedding_adapters: Receives the embedding models' adapters.
        continue_on_missing_env: Skip a model whose required setting is unset
            instead of raising.
        report: Receives the models skipped that way.
        origin: Named in the skip logs. The file loader passes the path.

    Returns:
        Tuple of (count of registered route identifiers including aliases,
        list of ModelRegistrationInfo for each model).
    """
    return register_from_models_document(
        router,
        yaml.safe_load(text) or {},
        embedding_adapters=embedding_adapters,
        continue_on_missing_env=continue_on_missing_env,
        report=report,
        origin=origin,
    )


def register_from_models_document(
    router: FixedRouter,
    data: dict[str, Any],
    embedding_adapters: dict[str, Any] | None = None,
    *,
    continue_on_missing_env: bool = False,
    report: ModelLoadReport | None = None,
    origin: str = "Admin model catalog",
) -> tuple[int, list[ModelRegistrationInfo]]:
    """Register a database model document through the existing adapter boundary.

    All of it or none of it. Every entry is checked (:func:`check_model_entry`)
    and every adapter built before anything reaches *router*,
    *embedding_adapters* or the provider-key registry, so an entry that cannot
    register leaves them as they were instead of serving the models listed
    before it. A model whose setting is unset is skipped instead, when
    *continue_on_missing_env* allows.

    Raises:
        RegistryConfigError: An entry cannot be registered; the message names
            the model.
        MissingEnvBackedKeyError: A setting a model needs is unset, and
            *continue_on_missing_env* is false.
    """
    models: list[dict[str, Any]] = check_models_document(data)
    count = 0
    model_infos: list[ModelRegistrationInfo] = []
    # What registration writes, applied only once every model is built.
    registrations: list[Callable[[], None]] = []
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
            # ${VAR} expansion for base_url/api_key in both top-level and route
            # entries: the database-backed setting, then the environment, then
            # the setting's registered default (resolve_reference).
            def expand_env(val: str | None) -> str | None:
                var = _template_var(val)
                return resolve_reference(var) if var is not None else val

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
                    "on_demand",
                    "supported_params",
                    "reasoning_efforts",
                    "pricing",
                    "pricing_schedule",
                    "pricing_source",
                    "route_metadata",
                    "extra_body",
                    "processor",
                    "priority_scheduling",
                )
                if k in m
            }
            # NOTE: top-level base_url is intentionally NOT expanded here. It is
            # expanded per-route in the loop below (raw_base_url -> base_url) so
            # the empty-base_url guard can still see the original ${VAR} template
            # when a route inherits the top-level value or the default single
            # route is synthesized. Expanding it in place would erase the
            # template and let an unset env-backed top-level base_url slip
            # through as a dead "<model>:unknown-api" endpoint. The top-level
            # api_key is left unexpanded for the same reason: expanded here, an
            # unset ${VAR} reached the route as None, past the empty-api_key
            # guard, and the model was registered with no credential.
            if top_cfg.get("provider_model_id"):
                top_cfg["provider_model_id"] = expand_env(top_cfg["provider_model_id"])  # type: ignore
            # The display name defaults to the id, as Admin's catalog check assumes.
            if top_cfg.get("name") is None:
                top_cfg["name"] = top_cfg["id"]

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
            # check_models_document has refused every malformed declaration
            # already, so a missing credential that skips an optional route
            # below can no longer hide one.
            for route_index, r in enumerate(routes, start=1):
                deployment_type = validate_deployment_type(r.get("deployment_type", "auto"))
                kind = r.get("kind") or top_cfg.get("provider")
                raw_base_url = r.get("base_url") or top_cfg.get("base_url")
                base_url = expand_env(raw_base_url)
                weight = float(r.get("weight", 1.0))
                # Optional routes (e.g. a staging canary) degrade gracefully:
                # when their env-backed key resolves blank we skip just this
                # route, keeping the rest of the model, instead of dropping the
                # whole model via the model-level MissingEnvBackedKeyError catch.
                route_optional = bool(r.get("optional", False))

                if "embeddings_path" in r:
                    path_context = f"model {top_cfg.get('id')!r}, route {route_index}"
                    embeddings_path = r["embeddings_path"]
                    if embeddings_path is not None:
                        template = embeddings_path.strip()
                        embeddings_path = expand_env(template)
                        if (
                            template.startswith("${")
                            and template.endswith("}")
                            and (embeddings_path is None or not embeddings_path.strip())
                        ):
                            message = (
                                f"embeddings_path resolved to unset or empty ({path_context}, "
                                f"template: {template!r})"
                            )
                            if route_optional:
                                logger.warning("Skipping optional route: %s", message)
                                continue
                            raise EmbeddingsPathConfigError(message)
                        embeddings_path = embeddings_path.strip()
                        if ".." in embeddings_path.split("/"):
                            raise EmbeddingsPathConfigError(
                                f"embeddings_path must not contain '..' path segments "
                                f"({path_context})"
                            )

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
                        if (var := _template_var(raw_base_url)) is not None:
                            unset_env_vars.add(var)
                        raise MissingEnvBackedKeyError(
                            f"base_url for {top_cfg.get('id')!r} resolved to "
                            f"unset/empty/host-less after env expansion "
                            f"(template: {raw_base_url!r})"
                        )

                raw_api_keys = r.get("api_keys")
                raw_api_key = r.get("api_key") or top_cfg.get("api_key")

                api_key: str | None = None
                api_keys: list[str] | None = None
                if raw_api_keys is not None:
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
                        if (var := _template_var(raw_api_key)) is not None:
                            unset_env_vars.add(var)
                        raise MissingEnvBackedKeyError(
                            f"api_key for {top_cfg.get('id')!r} resolved to "
                            f"empty/None after env expansion"
                        )

                # Adapter config inherits from top-level model config
                adapter_cfg = dict(top_cfg)
                # "type" is routing-only metadata, not a ModelConfig field
                adapter_cfg.pop("type", None)
                adapter_cfg["base_url"] = base_url
                adapter_cfg["deployment_type"] = deployment_type
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
                if "pricing_schedule" in r:
                    adapter_cfg["pricing_schedule"] = r["pricing_schedule"]
                if "pricing_source" in r:
                    adapter_cfg["pricing_source"] = r["pricing_source"]

                # Route-level processor override (bypasses model-ID auto-detection)
                if "processor" in r:
                    adapter_cfg["processor"] = r["processor"]

                # The embeddings path belongs to this endpoint, not the model:
                # a custom-version gateway may have a standard /v1 fallback.
                if "embeddings_path" in r:
                    adapter_cfg["embeddings_path"] = embeddings_path

                # Whether this endpoint's server runs sglang priority scheduling.
                # Route-level, because it is a fact about one server rather than
                # about the model: the same model routinely has a local sglang
                # route and a remote fallback that must not receive the field.
                if "priority_scheduling" in r:
                    adapter_cfg["priority_scheduling"] = bool(r["priority_scheduling"])
                elif "priority_scheduling" in adapter_cfg:
                    adapter_cfg["priority_scheduling"] = bool(adapter_cfg["priority_scheduling"])

                # Whether a null `*_tokens_details` block from this endpoint is a
                # reported cache miss (0) rather than "no cache reporting" (NULL).
                #
                # Route-level ONLY -- deliberately not inheritable from the model,
                # unlike priority_scheduling. The declaration is a claim about one
                # server's startup flags that someone verified by probing it, and a
                # model-level claim would silently ride along to every fallback,
                # including a synthesized shorthand route with no `route:` block to
                # review. Fabricating a measured 0 on an endpoint that reports
                # nothing is the failure this whole field exists to prevent, so an
                # inheritable spelling is refused (check_model_entry) rather than
                # quietly honored, and so is a value that is not a boolean.
                if "null_cache_details_means_miss" in r:
                    adapter_cfg["null_cache_details_means_miss"] = r[
                        "null_cache_details_means_miss"
                    ]
                else:
                    adapter_cfg.pop("null_cache_details_means_miss", None)

                # Whether this upstream answers `stream_options.include_usage`
                # with a final usage chunk. Route-level, because it is a fact
                # about one endpoint's protocol: the same model routinely has a
                # deepseek route and an ollama fallback that disagree, and a
                # deployment may run an OpenAI-compatible stack that rejects
                # unknown body fields.
                #
                # Presence, not truthiness. `_make_adapter` assumes the flag on
                # for its default kinds only when the key is absent, so an
                # explicit `false` survives even on a kind that normally
                # requests usage, and an explicit `true` opts in a kind that
                # normally does not.
                if "include_usage_in_stream" in r:
                    adapter_cfg["include_usage_in_stream"] = r["include_usage_in_stream"]

                # The route's strict public -> upstream reasoning-effort map
                # (serving.adapters.reasoning_effort). Route-level ONLY: it is a
                # claim about which values one server accepts, and a model-level
                # map would ride along to every fallback, translating levels for
                # a server that never takes those names.
                if EFFORT_MAP_FIELD in m:
                    raise ValueError(
                        f"{EFFORT_MAP_FIELD} for model {top_cfg.get('id')!r} must be "
                        "declared on a route, not on the model (including shorthand "
                        "models without a route list): it states which values one "
                        "server accepts."
                    )
                if EFFORT_MAP_FIELD in r:
                    try:
                        adapter_cfg[EFFORT_MAP_FIELD] = validate_effort_map(r[EFFORT_MAP_FIELD])
                    except ValueError as exc:
                        raise ValueError(
                            f"{exc} (model {top_cfg.get('id')!r}, route {route_index})"
                        ) from exc

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

                from serving.config.documents import resolve_credentials

                def credential_value(name: str) -> str:
                    value = resolve_reference(name)
                    if value is None or not value.strip():
                        unset_env_vars.add(name)
                        raise MissingEnvBackedKeyError(f"Credential setting {name} is missing")
                    return value

                try:
                    adapter_cfg = resolve_credentials(adapter_cfg, credential_value)
                except MissingEnvBackedKeyError:
                    if route_optional:
                        logger.warning(
                            "Skipping optional route with an unset credential for %s",
                            top_cfg.get("id"),
                        )
                        continue
                    raise
                adapter = _make_adapter(kind, adapter_cfg)
                if adapter_cfg.get(EFFORT_MAP_FIELD) and not getattr(
                    adapter, "forwards_reasoning_effort", False
                ):
                    raise ValueError(
                        f"{EFFORT_MAP_FIELD} on model {top_cfg.get('id')!r}, route "
                        f"{route_index} (kind {kind!r}): this adapter does not send "
                        "reasoning_effort, so there is nothing to translate"
                    )
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

            # Determine model type: "embedding" models bypass FixedRouter
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
                # Embedding models bypass the weighted FixedRouter. Register
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
                    # Embedding models bypass the weighted FixedRouter.
                    # Record their gate only when an adapter was configured;
                    # an optional route whose environment is unset is skipped.
                    admin_only, required_role = _parse_role_gate(m, model_id)
                    registrations.append(
                        partial(
                            _register_embedding_model,
                            router,
                            embedding_adapters,
                            model_id,
                            adapters_with_weights,
                            emb_adapter,
                            aliases=aliases,
                            admin_only=admin_only,
                            required_role=required_role,
                        )
                    )
                    count += 1 + len(aliases)
            else:
                # Chat models go through the full FixedRouter
                admin_only, required_role = _parse_role_gate(m, model_id)
                registrations.append(
                    partial(
                        router.register_route,
                        model_id,
                        adapters_with_weights,
                        aliases=aliases,
                        admin_only=admin_only,
                        required_role=required_role,
                    )
                )
                count += 1 + len(aliases)

            registrations.append(
                partial(
                    _register_key_adapters,
                    dynamic_key_providers,
                    dynamic_key_registrations,
                )
            )

            # Collect per-model metadata for bootstrap (RouteWise strategy resolution)
            model_infos.append(
                ModelRegistrationInfo(
                    model_id=model_id,
                    aliases=aliases,
                    router=m.get("router"),
                    router_params=m.get("router_params"),
                )
            )
        except MissingEnvBackedKeyError as exc:
            if not continue_on_missing_env:
                raise
            logger.warning("Skipping model %r from %s: %s", m.get("id"), origin, exc)
            skipped_for_missing_env.append(str(m.get("id")))
            if report is not None:
                report.skipped_models[str(m.get("id"))] = [
                    str(alias) for alias in (m.get("aliases") or [])
                ]
            continue
        except RegistryConfigError:
            raise
        except Exception as exc:
            # An adapter that would not build (a backend extension's factory,
            # say): name the model, as a refused entry is named.
            raise RegistryConfigError(
                f"model {m.get('id')!r} could not be registered: {exc!r}"
            ) from exc

    # Every model is built: only now does anything reach the router.
    for register in registrations:
        register()

    if report is not None:
        report.unset_env_vars.update(unset_env_vars)
    if skipped_for_missing_env:
        missing = ", ".join(sorted(unset_env_vars)) or "the referenced environment variables"
        dropped = ", ".join(skipped_for_missing_env)
        if count == 0:
            logger.error(
                "No models are available: every model in %s was skipped because "
                "its credential is unset. Set %s (Admin > Configuration) and "
                "restart. /v1/models will "
                "stay empty until then, and requests will report the model as "
                "not found. Skipped: %s",
                origin,
                missing,
                dropped,
            )
        else:
            logger.warning(
                "%d of %d models in %s are unavailable because their credentials "
                "are unset (%s). Skipped: %s",
                len(skipped_for_missing_env),
                len(models),
                origin,
                missing,
                dropped,
            )

    return count, model_infos
