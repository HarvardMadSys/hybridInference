"""Validated application documents stored with the other administrator settings.

There are no runtime file fallbacks. Secrets belong in separate secret settings;
catalogs refer to them with ``${NAME}``, so the normal configuration export can
carry a complete, editable catalog without disclosing credentials.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import fields
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

DOCUMENT_KEYS = {
    "models": "MODEL_CATALOG",
    "routing": "ROUTING_POLICY",
    "alerts": "ALERT_RULES",
    "distribution": "SITE_CONFIGURATION",
    "branding": "SITE_BRANDING",
}
KEY_KINDS = {key: kind for kind, key in DOCUMENT_KEYS.items()}
_REFERENCE = re.compile(r"^\$\{[A-Za-z_][A-Za-z0-9_]*\}$")
_CREDENTIAL = re.compile(r"(^|_)(api_keys?|token|password|secret|authorization|headers)(_|$)", re.I)


def parse(text: str) -> dict[str, Any]:
    """Read a JSON object; blank means an intentionally empty document."""
    try:
        value = json.loads(text) if text.strip() else {}
    except (ValueError, TypeError):
        raise ValueError("must be a valid JSON object") from None
    if not isinstance(value, dict):
        raise ValueError("must be a JSON object")
    return value


def encode(value: dict[str, Any]) -> str:
    """Return a deterministic JSON document, rejecting non-finite numbers."""
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)


def get(kind: str, *, configured: bool = False) -> dict[str, Any]:
    """Return the stored application document; never read a configuration file."""
    from serving.config import app_config

    key = DOCUMENT_KEYS[kind]
    if kind in ("distribution", "branding") and app_config.validation_problem(key):
        raise ValueError("Invalid public site configuration")
    reader = app_config.configured_value if configured else app_config.config_value
    return parse(reader(key) or "")


def _credentials(value: Any, *, path: str = "", credential: bool = False) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            _credentials(
                item,
                path=f"{path}/{key}",
                credential=credential or bool(_CREDENTIAL.search(key.replace("-", "_"))),
            )
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _credentials(item, path=f"{path}/{index}", credential=credential)
    elif credential and value and (not isinstance(value, str) or not _REFERENCE.fullmatch(value)):
        raise ValueError(f"{path} must reference a separate secret setting as ${{NAME}}")


def validate(key: str, text: str) -> None:
    """Validate a document without upstream I/O or mutating the running router."""
    kind = KEY_KINDS[key]
    value = parse(text)
    # The public documents have strict, closed schemas. In particular,
    # branding's api_key_env_var is a displayed variable name, not a secret.
    if kind not in ("distribution", "branding"):
        _credentials(value)
    if kind == "models":
        from routing.strategies import validate_router_config
        from serving.adapters.base import ModelConfig, validate_deployment_type
        from serving.servers.registry import (
            _BUILT_IN_ADAPTER_KINDS,
            ADAPTER_FACTORIES,
            parse_openrouter_kind,
        )

        if set(value) - {"models", "auto"}:
            raise ValueError("model catalog accepts only models and auto")
        models = value.get("models", [])
        if not isinstance(models, list):
            raise ValueError("models must be a list")
        names: set[str] = set()
        for model in models:
            if not isinstance(model, dict) or not isinstance(model.get("id"), str):
                raise ValueError("every model needs an id")
            allowed = {field.name for field in fields(ModelConfig)}
            candidate = {key: val for key, val in model.items() if key in allowed}
            candidate.setdefault("name", model["id"])
            candidate.setdefault("provider", "openai_compat")
            candidate.setdefault("base_url", "")
            ModelConfig(**candidate)
            if "deployment_type" in model or "embeddings_path" in model:
                raise ValueError("deployment type and embeddings path belong on individual routes")
            for length in ("context_length", "max_output_length"):
                limit = model.get(length)
                if limit is not None and (
                    isinstance(limit, bool)
                    or not isinstance(limit, int)
                    or not 0 < limit <= 10_000_000
                ):
                    raise ValueError(f"{length} must be a positive whole number of tokens")
            if model.get("router") is not None:
                validate_router_config(model["router"], model.get("router_params"))
            for name in [model["id"], *(model.get("aliases") or [])]:
                if not isinstance(name, str) or not name.strip() or name in names:
                    raise ValueError("model ids and aliases must be nonempty and unique")
                names.add(name)
            routes = model.get("route", [])
            if not isinstance(routes, list) or any(not isinstance(r, dict) for r in routes):
                raise ValueError("route must be a list of objects")
            for route in routes:
                if not isinstance(route.get("kind"), str) or not route["kind"].strip():
                    raise ValueError("every route needs an adapter kind")
                kind, _ = parse_openrouter_kind(route["kind"])
                if kind not in _BUILT_IN_ADAPTER_KINDS and kind not in ADAPTER_FACTORIES:
                    raise ValueError("unknown adapter kind")
                if "deployment_type" in route:
                    validate_deployment_type(route["deployment_type"])
                if route.get("embeddings_path") is not None and not isinstance(
                    route["embeddings_path"], str
                ):
                    raise ValueError("embeddings path must be a string or null")
                weight = route.get("weight", 1)
                if isinstance(weight, bool) or not isinstance(weight, (int, float)) or weight < 0:
                    raise ValueError("route weights must be nonnegative numbers")
    elif kind == "routing":
        from routing.config import routing_config_from_data

        routing_config_from_data(value)
    elif kind == "alerts":
        from serving.observability.alert_config import alert_config_from_data

        alert_config_from_data(value)
    elif kind == "distribution":
        from serving.config.distribution import DistributionConfig

        if value:
            config = DistributionConfig.model_validate(value)
            if any(config.paths.model_dump().values()) or config.site.branding:
                raise ValueError("file paths are retired; configure application documents in Admin")
    elif kind == "branding":
        from serving.config.branding import BrandingConfig

        if value:
            BrandingConfig.model_validate(value)
    encode(value)


def separate_secrets(
    value: dict[str, Any], namespace: str
) -> tuple[dict[str, Any], dict[str, str]]:
    """Replace legacy embedded credentials with stable secret-setting references.

    Names depend only on the document and field path, never on secret contents.
    The caller stores both the returned document and secrets in one transaction.
    """
    secrets: dict[str, str] = {}

    def visit(item: Any, path: str, credential: bool = False) -> Any:
        if isinstance(item, dict):
            return {
                key: visit(
                    val,
                    f"{path}/{key}",
                    credential or bool(_CREDENTIAL.search(key.replace("-", "_"))),
                )
                for key, val in item.items()
            }
        if isinstance(item, list):
            return [visit(val, f"{path}/{index}", credential) for index, val in enumerate(item)]
        if credential and isinstance(item, str) and item and not _REFERENCE.fullmatch(item):
            suffix = hashlib.sha256(f"{namespace}:{path}".encode()).hexdigest()[:20].upper()
            name = f"CATALOG_SECRET_{suffix}"
            secrets[name] = item
            return "${" + name + "}"
        return item

    return visit(value, ""), secrets


def resolve_credentials(value: Any, resolver: Callable[[str], str]) -> Any:
    """Expand whole-value references in nested credential fields for adapter use.

    Non-credential metadata and template strings stay literal. The caller owns
    missing-setting behavior so optional routes keep their existing semantics.
    """

    def visit(item: Any, credential: bool = False) -> Any:
        if isinstance(item, dict):
            return {
                key: visit(val, credential or bool(_CREDENTIAL.search(key.replace("-", "_"))))
                for key, val in item.items()
            }
        if isinstance(item, list):
            return [visit(val, credential) for val in item]
        if credential and isinstance(item, str) and _REFERENCE.fullmatch(item):
            return resolver(item[2:-1])
        return item

    return visit(value)
