"""Validated application documents stored with the other administrator settings.

There are no runtime file fallbacks. Secrets belong in separate secret settings;
catalogs refer to them with ``${NAME}``, so the normal configuration export can
carry a complete, editable catalog without disclosing credentials.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

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
_TITLES = {
    "models": "model catalog",
    "routing": "routing policy",
    "alerts": "alert rules document",
    "distribution": "site configuration",
    "branding": "site branding",
}


class DocumentError(ValueError):
    """Why a document cannot be saved, in words an administrator can be shown.

    The message names where the problem is -- a field, a model, a route -- and
    what kind of problem it is, never a value from the document: a credential
    typed into the wrong field must not travel on, to the Settings tab or the
    log, in the error that refuses it.
    """


#: How many of a Pydantic error's reasons :func:`describe_validation_error` names.
_REASONS_SHOWN = 3


def describe_validation_error(exc: ValidationError) -> str:
    """Name the fields a Pydantic error is about and what is wrong, never their input.

    Each reason is a JSON pointer and the message, as Admin words a refused
    setting (``app_config._validator_problem``): ``/timeout value must be
    non-negative``.
    """
    errors = exc.errors(include_url=False, include_context=False, include_input=False)
    reasons = []
    for error in errors[:_REASONS_SHOWN]:
        location = "/".join(str(part) for part in error.get("loc") or ())
        message = str(error.get("msg") or "invalid value").removeprefix("Value error, ")
        reasons.append(f"/{location} {message}" if location else message)
    if len(errors) > _REASONS_SHOWN:
        reasons.append(f"{len(errors) - _REASONS_SHOWN} more")
    return "; ".join(reasons)


def parse(text: str) -> dict[str, Any]:
    """Read a JSON object; blank means an intentionally empty document."""
    try:
        value = json.loads(text) if text.strip() else {}
    except (ValueError, TypeError):
        raise DocumentError("must be a valid JSON object") from None
    if not isinstance(value, dict):
        raise DocumentError("must be a JSON object")
    return value


def encode(value: dict[str, Any]) -> str:
    """Encode JSON, preserving legacy YAML dates and rejecting non-finite numbers."""

    def scalar(item: Any) -> str:
        if isinstance(item, date):
            return item.isoformat()
        raise TypeError("Unsupported application document value")

    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, default=scalar)


def get(kind: str, *, configured: bool = False) -> dict[str, Any]:
    """Return the stored application document; never read a configuration file."""
    from serving.config import app_config

    key = DOCUMENT_KEYS[kind]
    if kind in ("distribution", "branding") and app_config.validation_problem(key):
        raise ValueError("Invalid public site configuration")
    reader = app_config.configured_value if configured else app_config.config_value
    return parse(reader(key) or "")


def _credentials(
    value: Any, *, path: str = "", credential: bool = False, references: bool = True
) -> None:
    """Refuse a credential stored inline instead of in a secret setting.

    With *references* false the document holds no credentials at all, not even
    a ``${NAME}`` reference: nothing would expand it.
    """
    if isinstance(value, dict):
        for key, item in value.items():
            _credentials(
                item,
                path=f"{path}/{key}",
                credential=credential or bool(_CREDENTIAL.search(str(key).replace("-", "_"))),
                references=references,
            )
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _credentials(item, path=f"{path}/{index}", credential=credential, references=references)
    elif credential and value:
        if not references:
            raise DocumentError(f"{path} names a credential, and this document holds none")
        if not isinstance(value, str) or not _REFERENCE.fullmatch(value):
            raise DocumentError(f"{path} must reference a separate secret setting as ${{NAME}}")


def validate(key: str, text: str) -> None:
    """Validate a document without upstream I/O or mutating the running router.

    Raises:
        DocumentError: Naming where the problem is and what kind it is, never
            a value from the document, so Admin can show the message as it is.
    """
    check(KEY_KINDS[key], parse(text))


def check(kind: str, value: dict[str, Any]) -> None:
    """Validate a document of *kind* that has already been read, as :func:`validate` does.

    The model catalog is held to the registry's own rules
    (``serving.servers.registry.check_models_document``): what registration
    accepts is accepted here, so a catalog carried over from YAML -- anchor
    blocks, a route without a kind, an alias two models claim -- stays usable,
    and what it refuses is refused here, with the same reason.

    Raises:
        DocumentError: As :func:`validate`.
    """
    from serving.servers.registry import RegistryConfigError

    try:
        _check(kind, value)
    except DocumentError:
        raise
    except ValidationError as exc:
        raise DocumentError(describe_validation_error(exc)) from None
    except RegistryConfigError as exc:
        # Worded for this: the model, the route and the field, never a value.
        raise DocumentError(str(exc)) from None
    except (TypeError, ValueError) as exc:
        if isinstance(exc.__cause__, ValidationError):
            raise DocumentError(describe_validation_error(exc.__cause__)) from None
        # Any other message may quote the document; say only which one it is.
        raise DocumentError(f"not a valid {_TITLES[kind]}") from None
    try:
        encode(value)
    except (TypeError, ValueError):
        raise DocumentError("numbers must be finite: JSON has no NaN or Infinity") from None


def _check(kind: str, value: dict[str, Any]) -> None:
    # Branding's schema is closed, and its api_key_env_var is a displayed
    # variable name, not a secret. The site configuration is checked below.
    if kind not in ("distribution", "branding"):
        _credentials(value)
    if kind == "models":
        from serving.servers.registry import check_models_document

        check_models_document(value)
    elif kind == "routing":
        from routing.config import routing_config_from_data

        routing_config_from_data(value)
    elif kind == "alerts":
        from serving.observability.alert_config import alert_config_from_data

        alert_config_from_data(value)
    elif kind == "distribution":
        from serving.config.distribution import DistributionConfig

        if value:
            # Its root refuses a key it does not define; a section ignores one.
            config = DistributionConfig.model_validate(value)
            if any(config.paths.model_dump().values()) or config.site.branding:
                raise DocumentError(
                    "file paths are retired; configure application documents in Admin"
                )
        # A public document: no credential belongs in it, not even under a
        # key a section ignores.
        _credentials(value, references=False)
    elif kind == "branding":
        from serving.config.branding import BrandingConfig

        if value:
            BrandingConfig.model_validate(value)


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
