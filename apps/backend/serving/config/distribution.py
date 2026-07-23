"""Distribution runtime and source-bundle manifests.

Runtime schema v1 preserves the Phase 1 opt-in behavior: unknown keys are
ignored, a broken manifest fails open, and the global ``dark|active`` mode
controls all three legacy config paths.

Runtime schema v2 is the Phase 2 contract. Its input is strict, its
models/routing/alerts selectors are independent, and selected candidate paths
must remain inside the distribution root. ``DISTRIBUTION_CONFIG_REQUIRED=1``
turns static configuration failures into :class:`DistributionStartupError`
before service initialization.

``bundle.yaml`` is deliberately separate. It inventories distribution-owned
source inputs for CI/package validation; the backend startup path never loads
it.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, ValidationError, model_validator

from serving.config.settings import get_settings
from serving.utils.logging import get_logger

logger = get_logger(__name__)

ConfigKind = Literal["models", "routing", "alerts"]
ConfigMode = Literal["legacy", "shadow", "active"]
RuntimeResourceKind = Literal["models", "routing", "alerts", "rag"]
EnvironmentReference = tuple[str, bool, bool]

_CONFIG_KINDS: tuple[ConfigKind, ...] = ("models", "routing", "alerts")
_LEGACY_DEFAULTS: dict[ConfigKind, str] = {
    "models": "config/models.yaml",
    "routing": "config/routing.yaml",
    "alerts": "config/alerts.yaml",
}
_TARGETS = frozenset(
    {
        "local",
        "development",
        "test",
        "ci",
        "staging",
        "probe",
        "canary",
        "production",
    }
)
_ENV_REFERENCE_RE = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)(?::-([^}]*))?\}")
_UNBRACED_ENV_REFERENCE_RE = re.compile(r"\$(?!\{)([A-Z_][A-Z0-9_]*)")
_ENV_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
_SECRET_FIELD_NAMES = frozenset(
    {
        "api_key",
        "api_keys",
        "credential",
        "credentials",
        "password",
        "passwords",
        "private_key",
        "private_keys",
        "secret",
        "secrets",
        "token",
        "tokens",
    }
)
_SECRET_FIELD_SUFFIXES = tuple(
    f"_{name}"
    for name in (
        "api_key",
        "api_keys",
        "credential",
        "credentials",
        "password",
        "passwords",
        "private_key",
        "private_keys",
        "secret",
        "secrets",
        "token",
        "tokens",
    )
)
_SEMANTIC_RESERVED_ENV = frozenset(
    {
        "PATH",
        "PYTHONHOME",
        "PYTHONPATH",
        "PYTHONNOUSERSITE",
        "PYTHONDONTWRITEBYTECODE",
        "VIRTUAL_ENV",
        "LD_PRELOAD",
        "DYLD_INSERT_LIBRARIES",
        "DYLD_LIBRARY_PATH",
    }
)
_BUNDLE_EXCLUDED_PARTS = frozenset(
    {
        ".git",
        ".next",
        ".pytest_cache",
        ".ruff_cache",
        "__pycache__",
        "node_modules",
    }
)
_SEMANTIC_VALIDATOR = """\
import sys
from pathlib import Path

kind, raw_path = sys.argv[1:3]
path = Path(raw_path)
if kind == "models":
    from routing.executor import RouteExecutor
    from serving.servers.registry import register_from_models_yaml

    register_from_models_yaml(
        RouteExecutor(),
        path,
        embedding_adapters={},
        continue_on_missing_env=False,
    )
elif kind == "routing":
    from routing.config import load_routing_config

    load_routing_config(path)
elif kind == "alerts":
    from serving.observability.alert_config import load_alert_config

    load_alert_config(path)
else:
    raise ValueError("unsupported config kind")
"""


class _V1ManifestModel(BaseModel):
    """Schema v1 sections ignore additions for compatibility."""

    model_config = ConfigDict(extra="ignore")


class _StrictManifestModel(BaseModel):
    """Schema v2 and bundle sections reject unknown input."""

    model_config = ConfigDict(extra="forbid", strict=True)


class DistributionInfo(_V1ManifestModel):
    """Canonical distribution identity exposed to existing consumers."""

    id: str
    display_name: str = ""
    release: str = ""


class DistributionSite(_V1ManifestModel):
    """Canonical safe site identity."""

    public_base_url: str = ""
    base_url: str = ""
    docs_url: str = ""
    status_url: str = ""
    support_email: str = ""
    terms_document: str = ""
    privacy_document: str = ""
    branding: str = ""


class DistributionFeatures(_V1ManifestModel):
    """Canonical compatibility view for public feature expectations."""

    routers: list[str] = Field(default_factory=list)
    public_signup: bool | None = None
    rag: bool | None = None


class DistributionPaths(_V1ManifestModel):
    """Canonical gateway config-file locations."""

    models: str = ""
    routing: str = ""
    alerts: str = ""


class DistributionRagResources(_V1ManifestModel):
    """Backend-consumed RAG resources."""

    settings: str = ""
    corpus: str = ""
    index: str = ""
    metadata: str = ""


class DistributionRuntimeResources(_V1ManifestModel):
    """Canonical runtime resource view."""

    gateway: DistributionPaths = Field(default_factory=DistributionPaths)
    rag: DistributionRagResources = Field(default_factory=DistributionRagResources)


class DistributionDeployment(_V1ManifestModel):
    """Schema v1 compatibility field; absent from the v2 input contract."""

    target: str = ""


class DistributionConfig(BaseModel):
    """Canonical runtime manifest returned by both schema loaders."""

    schema_version: Literal[1, 2]
    distribution: DistributionInfo
    site: DistributionSite = Field(default_factory=DistributionSite)
    features: DistributionFeatures = Field(default_factory=DistributionFeatures)
    paths: DistributionPaths = Field(default_factory=DistributionPaths)
    resources: DistributionRuntimeResources = Field(default_factory=DistributionRuntimeResources)
    environment_contract: str = ""
    deployment: DistributionDeployment = Field(default_factory=DistributionDeployment)
    _root: Path = PrivateAttr(default_factory=Path)
    _capability_expectations: dict[str, bool] = PrivateAttr(default_factory=dict)


class _DistributionConfigV1(_V1ManifestModel):
    schema_version: Literal[1]
    distribution: DistributionInfo
    site: DistributionSite = Field(default_factory=DistributionSite)
    features: DistributionFeatures = Field(default_factory=DistributionFeatures)
    paths: DistributionPaths = Field(default_factory=DistributionPaths)
    deployment: DistributionDeployment = Field(default_factory=DistributionDeployment)


class _DistributionInfoV2(_StrictManifestModel):
    id: str = Field(min_length=1)
    display_name: str = ""


class _DistributionSiteV2(_StrictManifestModel):
    base_url: str = ""
    docs_url: str = ""
    status_url: str = ""
    support_email: str = ""


class _DistributionFeaturesV2(_StrictManifestModel):
    auth_public_signup: bool | None = Field(default=None, alias="auth.public_signup")
    rag_chat: bool | None = Field(default=None, alias="rag.chat")
    admin_routing_routewise: bool | None = Field(
        default=None,
        alias="admin.routing.routewise",
    )


class _DistributionGatewayResourcesV2(_StrictManifestModel):
    models: str = ""
    routing: str = ""
    alerts: str = ""


class _DistributionRagResourcesV2(_StrictManifestModel):
    settings: str = ""
    corpus: str = ""
    index: str = ""
    metadata: str = ""


class _DistributionRuntimeResourcesV2(_StrictManifestModel):
    gateway: _DistributionGatewayResourcesV2 = Field(
        default_factory=_DistributionGatewayResourcesV2
    )
    rag: _DistributionRagResourcesV2 = Field(default_factory=_DistributionRagResourcesV2)


class _DistributionConfigV2(_StrictManifestModel):
    schema_version: Literal[2]
    distribution: _DistributionInfoV2
    site: _DistributionSiteV2 = Field(default_factory=_DistributionSiteV2)
    features: _DistributionFeaturesV2 = Field(default_factory=_DistributionFeaturesV2)
    resources: _DistributionRuntimeResourcesV2 = Field(
        default_factory=_DistributionRuntimeResourcesV2
    )
    environment_contract: str = Field(min_length=1)


class _DistributionRagSettingsV1(_StrictManifestModel):
    """Minimal strict schema until the RAG runtime owns a YAML loader."""

    schema_version: Literal[1]
    embedder_mode: str = "gateway"
    embed_model: str = "bge-m3"
    chat_model: str = ""
    top_k: int | str = 4
    chunk_max_chars: int | str = 1200
    chunk_overlap_chars: int | str = 150
    max_tokens: int | str = 1024
    temperature: float | str = 0.3
    api_base_url: str = ""
    api_key: str = ""
    gateway_base_url: str = ""
    gateway_api_key: str = ""

    @model_validator(mode="after")
    def _validate_settings(self) -> _DistributionRagSettingsV1:
        if self.embedder_mode not in {"gateway", "hash"} and not _is_env_reference(
            self.embedder_mode
        ):
            raise ValueError("embedder_mode must be gateway, hash, or an environment reference")
        for field_name, minimum in (
            ("top_k", 1),
            ("chunk_max_chars", 1),
            ("chunk_overlap_chars", 0),
            ("max_tokens", 1),
        ):
            value = getattr(self, field_name)
            if isinstance(value, str) and _is_env_reference(value):
                continue
            try:
                parsed = int(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{field_name} must be an integer") from exc
            if parsed < minimum:
                raise ValueError(f"{field_name} must be >= {minimum}")
        if not (isinstance(self.temperature, str) and _is_env_reference(self.temperature)):
            try:
                float(self.temperature)
            except (TypeError, ValueError) as exc:
                raise ValueError("temperature must be numeric") from exc
        for secret_field in ("api_key", "gateway_api_key"):
            value = getattr(self, secret_field)
            if value and not _is_env_reference(value):
                raise ValueError(f"{secret_field} must use an environment reference")
        return self


class _DistributionRagMetadataV1(_StrictManifestModel):
    """Strict metadata contract paired with a generated RAG index."""

    schema_version: Literal[1]
    corpus_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    chunk_text_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    embedding_model: str = Field(min_length=1)
    embedding_dimension: int = Field(gt=0)
    chunker_version: str = Field(min_length=1)


class _DistributionRagIndexEnvelopeV1(_StrictManifestModel):
    """Strict envelope checked before the production vector-store parser."""

    version: Literal[1]
    embed_model: str = Field(min_length=1)
    embedder_mode: Literal["gateway", "hash"]
    dim: int = Field(gt=0)
    records: list[dict] = Field(min_length=1)


class EnvironmentVariableContract(_StrictManifestModel):
    """One named environment reference; never contains its value."""

    name: str
    classification: Literal["secret", "config"]
    consumer: Literal[
        "backend",
        "frontend",
        "deploy",
        "ops",
        "docs",
        "monitoring",
        "targets",
    ]
    required: bool = False
    default: bool = False
    resources: list[RuntimeResourceKind] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_static_contract(self) -> EnvironmentVariableContract:
        if not _ENV_NAME_RE.fullmatch(self.name):
            raise ValueError("environment variable name must use [A-Z_][A-Z0-9_]*")
        if self.classification == "secret" and self.default:
            raise ValueError("secret environment references cannot declare a default")
        if self.consumer != "backend" and self.resources:
            raise ValueError("resource selectors are only valid for the backend consumer")
        return self


class EnvironmentContract(_StrictManifestModel):
    """Strict, value-free environment contract."""

    environment_schema_version: Literal[1]
    variables: list[EnvironmentVariableContract] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_unique_entries(self) -> EnvironmentContract:
        seen: set[tuple[str, str]] = set()
        for variable in self.variables:
            key = (variable.consumer, variable.name)
            if key in seen:
                raise ValueError(
                    f"duplicate environment contract entry for {variable.consumer}:{variable.name}"
                )
            seen.add(key)
        return self


class BundleExternalInput(_StrictManifestModel):
    """One upstream artifact injected by environment reference."""

    kind: Literal["image-ref", "npm-tarball-ref", "wheel-ref"]
    from_env: str
    consumer: Literal[
        "backend",
        "frontend",
        "deploy",
        "ops",
        "docs",
        "monitoring",
        "targets",
    ] = "deploy"
    required: bool = True

    @model_validator(mode="after")
    def _validate_env_name(self) -> BundleExternalInput:
        if not _ENV_NAME_RE.fullmatch(self.from_env):
            raise ValueError("external input from_env must name an environment variable")
        return self


class BundleResource(_StrictManifestModel):
    """One distribution-owned source path and its publication class."""

    path: str
    classification: Literal["public", "private", "generated", "secret-reference"]
    consumer: Literal[
        "backend",
        "frontend",
        "deploy",
        "ops",
        "docs",
        "monitoring",
        "targets",
    ]


class DistributionBundle(BaseModel):
    """Canonical strict bundle inventory."""

    bundle_schema_version: Literal[1]
    runtime_manifest: str
    external_inputs: dict[str, BundleExternalInput] = Field(default_factory=dict)
    resources: dict[str, BundleResource] = Field(default_factory=dict)
    environment_contract: str
    _root: Path = PrivateAttr(default_factory=Path)

    model_config = ConfigDict(extra="forbid", strict=True)


class DistributionConfigError(Exception):
    """Raised when a distribution-owned manifest cannot be validated."""


class DistributionSchemaError(DistributionConfigError):
    """Raised when a manifest or contract does not satisfy its schema."""


class DistributionPathError(DistributionConfigError):
    """Raised when a declared path is missing, mistyped, or escapes its root."""


class DistributionSemanticError(DistributionConfigError):
    """Raised when a declared resource fails its production parser."""


class DistributionEnvironmentError(DistributionConfigError):
    """Raised when environment references violate their value-free contract."""


class DistributionLockError(DistributionConfigError):
    """Raised when a bundle lock is missing, malformed, or stale."""


class DistributionStartupError(DistributionConfigError):
    """Fail-closed static distribution error raised during startup preflight."""


@dataclass(frozen=True)
class _CandidateSnapshot:
    references: frozenset[EnvironmentReference]
    placeholders: tuple[tuple[str, str], ...]
    canonical_sha256: str


@dataclass(frozen=True)
class DistributionConfigComparisonState:
    """Value-free, machine-readable comparison state for one gateway resource."""

    resource: ConfigKind
    selector: ConfigMode
    source: Literal["env", "distribution", "default"]
    status: Literal[
        "match",
        "mismatch",
        "candidate_missing",
        "candidate_invalid",
        "effective_missing",
        "effective_invalid",
        "uncomparable",
    ]
    mismatch: Literal[0, 1]


_distribution_config_comparison_state: dict[ConfigKind, DistributionConfigComparisonState] = {}


def _is_env_reference(value: str) -> bool:
    return _ENV_REFERENCE_RE.fullmatch(value) is not None


def _reference_is_secret_context(text: str, match: re.Match[str]) -> bool:
    """Infer secret-bearing config fields without reading any referenced value."""
    line_start = text.rfind("\n", 0, match.start()) + 1
    line_context = text[line_start : match.start()]
    field_names = {
        line_context.casefold()
        .rsplit(",", maxsplit=1)[-1]
        .split(":", maxsplit=1)[0]
        .strip(" -'\"{}[]")
        .replace("-", "_")
    }
    current_indent = len(line_context) - len(line_context.lstrip())
    for previous_line in reversed(text[:line_start].splitlines()):
        if not previous_line.strip() or previous_line.lstrip().startswith("#"):
            continue
        indent = len(previous_line) - len(previous_line.lstrip())
        if indent >= current_indent or ":" not in previous_line:
            continue
        field_names.add(
            previous_line.casefold()
            .lstrip()
            .lstrip("- ")
            .split(":", maxsplit=1)[0]
            .strip(" '\"{}[]")
            .replace("-", "_")
        )
        current_indent = indent
        if current_indent == 0:
            break
    return any(
        field_name in _SECRET_FIELD_NAMES or field_name.endswith(_SECRET_FIELD_SUFFIXES)
        for field_name in field_names
    )


def _scan_environment_references(
    text: str,
    *,
    label: str,
) -> frozenset[EnvironmentReference]:
    """Return strict references while rejecting unsupported placeholder syntax."""
    matches = list(_ENV_REFERENCE_RE.finditer(text))
    matches_by_start = {match.start(): match for match in matches}
    for marker in re.finditer(r"\$\{", text):
        if marker.start() not in matches_by_start:
            raise DistributionEnvironmentError(
                f"{label} contains malformed environment reference syntax"
            )
    if _UNBRACED_ENV_REFERENCE_RE.search(text):
        raise DistributionEnvironmentError(
            f"{label} contains unsupported unbraced environment reference syntax"
        )
    return frozenset(
        (
            match.group(1),
            match.group(2) is not None,
            _reference_is_secret_context(text, match),
        )
        for match in matches
    )


def _safe_schema_error_details(exc: Exception) -> str:
    """Describe schema failures by location and type without echoing input values."""
    if not isinstance(exc, ValidationError):
        return ""
    details: list[str] = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error.get("loc", ())) or "<root>"
        error_type = str(error.get("type") or "validation_error")
        details.append(f"{location} ({error_type})")
    return ", ".join(details)


def _read_yaml_mapping(path: Path, *, label: str) -> dict:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        raise DistributionSchemaError(f"cannot read {label} {path}") from None
    if not isinstance(data, dict):
        raise DistributionSchemaError(f"{label} {path} must be a YAML mapping")
    return data


def _resolve_path(root: Path, value: str) -> str:
    if not value:
        return ""
    return str((root / value).resolve())


def _resolve_paths(root: Path, values: dict[str, str], *, label: str) -> dict[str, str]:
    try:
        return {name: _resolve_path(root, value) for name, value in values.items()}
    except (OSError, RuntimeError, ValueError):
        raise DistributionPathError(f"cannot resolve paths in {label}") from None


def load_distribution_config(path: Path) -> DistributionConfig:
    """Load runtime schema v1 or v2 and return its canonical compatibility view."""
    data = _read_yaml_mapping(path, label="distribution manifest")
    root = path.resolve().parent
    schema_version = data.get("schema_version")
    try:
        if schema_version == 1:
            raw_v1 = _DistributionConfigV1.model_validate(data)
            resolved_gateway = DistributionPaths(
                **_resolve_paths(
                    root,
                    raw_v1.paths.model_dump(),
                    label=f"distribution manifest {path}",
                )
            )
            base_url = raw_v1.site.base_url or raw_v1.site.public_base_url
            site = raw_v1.site.model_copy(
                update={"base_url": base_url, "public_base_url": base_url}
            )
            config = DistributionConfig(
                schema_version=1,
                distribution=raw_v1.distribution,
                site=site,
                features=raw_v1.features,
                paths=resolved_gateway,
                resources=DistributionRuntimeResources(gateway=resolved_gateway),
                deployment=raw_v1.deployment,
            )
            raw_features = data.get("features")
            if isinstance(raw_features, dict):
                if "public_signup" in raw_features and raw_v1.features.public_signup is not None:
                    config._capability_expectations["auth.public_signup"] = (
                        raw_v1.features.public_signup
                    )
                if "rag" in raw_features and raw_v1.features.rag is not None:
                    config._capability_expectations["rag.chat"] = raw_v1.features.rag
                if "routers" in raw_features:
                    config._capability_expectations["admin.routing.routewise"] = (
                        "routewise" in raw_v1.features.routers
                    )
        elif schema_version == 2:
            raw_v2 = _DistributionConfigV2.model_validate(data)
            gateway = DistributionPaths(
                **_resolve_paths(
                    root,
                    raw_v2.resources.gateway.model_dump(),
                    label=f"distribution manifest {path}",
                )
            )
            rag = DistributionRagResources(
                **_resolve_paths(
                    root,
                    raw_v2.resources.rag.model_dump(),
                    label=f"distribution manifest {path}",
                )
            )
            contract_path = _resolve_path(root, raw_v2.environment_contract)
            routewise = raw_v2.features.admin_routing_routewise
            routers = [] if routewise is None else ["fixed", *(["routewise"] if routewise else [])]
            config = DistributionConfig(
                schema_version=2,
                distribution=DistributionInfo(
                    id=raw_v2.distribution.id,
                    display_name=raw_v2.distribution.display_name,
                ),
                site=DistributionSite(
                    public_base_url=raw_v2.site.base_url,
                    base_url=raw_v2.site.base_url,
                    docs_url=raw_v2.site.docs_url,
                    status_url=raw_v2.site.status_url,
                    support_email=raw_v2.site.support_email,
                ),
                features=DistributionFeatures(
                    routers=routers,
                    public_signup=raw_v2.features.auth_public_signup,
                    rag=raw_v2.features.rag_chat,
                ),
                paths=gateway,
                resources=DistributionRuntimeResources(gateway=gateway, rag=rag),
                environment_contract=contract_path,
            )
            for capability_id, expected in (
                ("auth.public_signup", raw_v2.features.auth_public_signup),
                ("rag.chat", raw_v2.features.rag_chat),
                (
                    "admin.routing.routewise",
                    raw_v2.features.admin_routing_routewise,
                ),
            ):
                if expected is not None:
                    config._capability_expectations[capability_id] = expected
        else:
            raise ValueError("schema_version must be 1 or 2")
    except DistributionConfigError:
        raise
    except Exception as exc:
        details = _safe_schema_error_details(exc)
        suffix = f": {details}" if details else ""
        raise DistributionSchemaError(f"invalid distribution manifest schema{suffix}") from None
    config._root = root
    return config


def get_distribution_capability_expectations() -> dict[str, bool]:
    """Return distribution policy expectations without treating them as runtime truth."""
    config = get_distribution_config()
    if config is None:
        return {}
    return dict(config._capability_expectations)


def load_environment_contract(path: Path) -> EnvironmentContract:
    """Load the strict value-free environment contract."""
    data = _read_yaml_mapping(path, label="environment contract")
    try:
        return EnvironmentContract.model_validate(data)
    except Exception as exc:
        details = _safe_schema_error_details(exc)
        suffix = f": {details}" if details else ""
        raise DistributionSchemaError(f"invalid environment contract schema{suffix}") from None


def _closed_root_path(
    root: Path,
    value: str,
    *,
    label: str,
    require_file: bool | None = None,
    reject_symlinks: bool = False,
) -> Path:
    if not value:
        raise DistributionPathError(f"{label} path is required")
    raw = Path(value)
    if raw.is_absolute() or ".." in raw.parts:
        raise DistributionPathError(f"{label} must be a root-relative path")
    try:
        lexical = root / raw
        candidate = lexical.resolve(strict=True)
        candidate.relative_to(root)
    except (OSError, RuntimeError, ValueError):
        raise DistributionPathError(
            f"{label} escapes the distribution root or is missing"
        ) from None
    if reject_symlinks:
        cursor = root
        for part in raw.parts:
            cursor /= part
            if cursor.is_symlink():
                raise DistributionPathError(f"{label} must not use symlinks")
    if require_file is True and not candidate.is_file():
        raise DistributionPathError(f"{label} must be a regular file")
    if require_file is False and not candidate.is_dir():
        raise DistributionPathError(f"{label} must be a directory")
    return candidate


def _runtime_raw_paths(data: dict) -> dict[str, str]:
    if data.get("schema_version") == 1:
        values = data.get("paths") or {}
        return {
            f"paths.{name}": value
            for name, value in values.items()
            if isinstance(value, str) and value
        }
    resources = data.get("resources") or {}
    gateway = resources.get("gateway") or {}
    rag = resources.get("rag") or {}
    values = {
        **{f"resources.gateway.{name}": value for name, value in gateway.items()},
        **{f"resources.rag.{name}": value for name, value in rag.items()},
        "environment_contract": data.get("environment_contract") or "",
    }
    return {name: value for name, value in values.items() if isinstance(value, str) and value}


def _validate_runtime_declared_paths(
    runtime_root: Path,
    runtime_manifest: Path,
    runtime: DistributionConfig,
) -> frozenset[EnvironmentReference]:
    """Validate every non-empty runtime path, independent of rollout selectors.

    The RAG service currently has no YAML settings or metadata loader. Until it
    owns those parsers, this module applies the smallest strict contract needed
    to make the files portable: a closed schema for settings and metadata, the
    production ``VectorStore.load`` parser for the index, and cross-file model
    and dimension checks.
    """
    raw = _read_yaml_mapping(runtime_manifest, label="bundle runtime manifest")
    for label, value in _runtime_raw_paths(raw).items():
        declared = Path(value)
        if declared.is_absolute() or ".." in declared.parts:
            raise DistributionPathError(
                f"runtime {label} must be a canonical root-relative closed-root path"
            )

    file_resources = {
        **runtime.paths.model_dump(),
        "rag.settings": runtime.resources.rag.settings,
        "rag.index": runtime.resources.rag.index,
        "rag.metadata": runtime.resources.rag.metadata,
        "environment_contract": runtime.environment_contract,
    }
    for label, value in file_resources.items():
        if not value:
            continue
        candidate = Path(value)
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(runtime_root)
        except (OSError, RuntimeError, ValueError):
            raise DistributionPathError(
                f"runtime resource {label} is missing or escapes the distribution root"
            ) from None
        if not resolved.is_file():
            raise DistributionPathError(f"runtime resource {label} must be a file")

    rag_values = runtime.resources.rag.model_dump()
    if any(rag_values.values()) or runtime.features.rag is True:
        missing_rag = sorted(name for name, value in rag_values.items() if not value)
        if missing_rag:
            raise DistributionPathError(
                "runtime RAG resources must be declared as one complete set; missing: "
                + ", ".join(missing_rag)
            )

    if runtime.resources.rag.corpus:
        corpus = Path(runtime.resources.rag.corpus)
        try:
            resolved_corpus = corpus.resolve(strict=True)
            resolved_corpus.relative_to(runtime_root)
        except (OSError, RuntimeError, ValueError):
            raise DistributionPathError(
                "runtime resource rag.corpus is missing or escapes the distribution root"
            ) from None
        if not resolved_corpus.is_dir():
            raise DistributionPathError("runtime resource rag.corpus must be a directory")
        markdown = sorted(resolved_corpus.rglob("*.md"))
        if not markdown:
            raise DistributionSemanticError("runtime resource rag.corpus has no Markdown documents")
        for document in markdown:
            try:
                document.resolve(strict=True).relative_to(runtime_root)
            except (OSError, RuntimeError, ValueError):
                raise DistributionPathError(
                    "runtime resource rag.corpus contains an escaping document"
                ) from None

    rag_references: frozenset[EnvironmentReference] = frozenset()
    rag_settings: _DistributionRagSettingsV1 | None = None
    if runtime.resources.rag.settings:
        settings_path = Path(runtime.resources.rag.settings)
        settings_data = _read_yaml_mapping(settings_path, label="RAG settings")
        try:
            rag_settings = _DistributionRagSettingsV1.model_validate(settings_data)
        except Exception:
            raise DistributionSemanticError(
                "RAG settings failed strict semantic validation"
            ) from None
        text = settings_path.read_text(encoding="utf-8")
        rag_references = _scan_environment_references(
            text,
            label="runtime RAG settings",
        )

    index_store = None
    if runtime.resources.rag.index:
        try:
            from serving.rag.store import VectorStore

            index_path = Path(runtime.resources.rag.index)
            index_data = json.loads(index_path.read_text(encoding="utf-8"))
            _DistributionRagIndexEnvelopeV1.model_validate(index_data)
            index_store = VectorStore.load(runtime.resources.rag.index)
            if (
                index_store.dim <= 0
                or not index_store.embed_model.strip()
                or index_store.embedder_mode not in {"gateway", "hash"}
                or not index_store.records
            ):
                raise ValueError("index metadata or records are incomplete")
            if any(
                not isinstance(value, (int, float)) or not math.isfinite(float(value))
                for record in index_store.records
                for value in record.embedding
            ):
                raise ValueError("index embeddings must contain only finite numbers")
        except Exception:
            raise DistributionSemanticError("RAG index failed semantic validation") from None

    metadata = None
    if runtime.resources.rag.metadata:
        metadata_path = Path(runtime.resources.rag.metadata)
        try:
            metadata_data = json.loads(metadata_path.read_text(encoding="utf-8"))
            metadata = _DistributionRagMetadataV1.model_validate(metadata_data)
        except Exception:
            raise DistributionSemanticError(
                "RAG metadata failed strict semantic validation"
            ) from None

    if (
        index_store is not None
        and metadata is not None
        and (
            metadata.embedding_model != index_store.embed_model
            or metadata.embedding_dimension != index_store.dim
        )
    ):
        raise DistributionSemanticError("RAG metadata does not match the generated index")
    if (
        rag_settings is not None
        and metadata is not None
        and not _is_env_reference(rag_settings.embed_model)
        and rag_settings.embed_model != metadata.embedding_model
    ):
        raise DistributionSemanticError("RAG settings embed_model does not match metadata")
    return rag_references


def _resolve_validation_input(
    path: Path,
    root: Path | None,
    *,
    label: str,
) -> tuple[Path, Path]:
    """Resolve one CLI/library input beneath an explicit detached-copy root."""
    try:
        if root is None:
            candidate = path.resolve(strict=True)
            validation_root = candidate.parent
        else:
            validation_root = root.resolve(strict=True)
            if not validation_root.is_dir():
                raise DistributionPathError(f"{label} root must be a directory")
            candidate = (
                path.resolve(strict=True)
                if path.is_absolute()
                else (validation_root / path).resolve(strict=True)
            )
            candidate.relative_to(validation_root)
    except DistributionConfigError:
        raise
    except (OSError, RuntimeError, ValueError):
        raise DistributionPathError(f"{label} is missing or escapes the validation root") from None
    if not candidate.is_file():
        raise DistributionPathError(f"{label} must be a regular file")
    return validation_root, candidate


def _validate_declared_gateway_candidates(
    config: DistributionConfig,
) -> tuple[
    dict[ConfigKind, _CandidateSnapshot | None],
    dict[ConfigKind, ResolvedConfigPath | None],
]:
    """Run raw validation for every declared candidate."""
    snapshots: dict[ConfigKind, _CandidateSnapshot | None] = {
        "models": None,
        "routing": None,
        "alerts": None,
    }
    candidates: dict[ConfigKind, ResolvedConfigPath | None] = {
        "models": None,
        "routing": None,
        "alerts": None,
    }
    for kind in _CONFIG_KINDS:
        value = getattr(config.paths, kind)
        if not value:
            continue
        candidate = Path(value)
        snapshot = _validate_raw_candidate(kind, candidate)
        snapshots[kind] = snapshot
        candidates[kind] = ResolvedConfigPath(path=candidate, source="distribution")
    return snapshots, candidates


def _validate_declared_gateway_semantics(
    snapshots: dict[ConfigKind, _CandidateSnapshot | None],
    candidates: dict[ConfigKind, ResolvedConfigPath | None],
) -> None:
    """Run each production parser after environment declarations are validated."""
    for kind, snapshot in snapshots.items():
        candidate = candidates[kind]
        if snapshot is not None and candidate is not None:
            _validate_candidate_semantics(kind, candidate.path, snapshot)


def validate_distribution_runtime_manifest(
    path: Path,
    *,
    root: Path | None = None,
    require_v2: bool = True,
) -> DistributionConfig:
    """Strictly validate a runtime manifest from a repository or detached copy."""
    validation_root, manifest = _resolve_validation_input(
        path,
        root,
        label="runtime manifest",
    )
    config = load_distribution_config(manifest)
    if require_v2 and config.schema_version != 2:
        raise DistributionSchemaError("strict runtime validation requires schema_version 2")

    rag_references = _validate_runtime_declared_paths(validation_root, manifest, config)
    if config.schema_version == 2:
        snapshots, candidates = _validate_declared_gateway_candidates(config)
        references: dict[RuntimeResourceKind, frozenset[EnvironmentReference]] = {
            kind: snapshot.references if snapshot else frozenset()
            for kind, snapshot in snapshots.items()
        }
        references["rag"] = rag_references
        _validate_environment_references(
            config,
            references,
            active_resources=set(),
            check_required_values=False,
        )
        _validate_declared_gateway_semantics(snapshots, candidates)
    return config


def _iter_bundle_resource_files(root: Path, declared: Path) -> list[Path]:
    """Return closed-root files below one declared bundle resource."""
    if declared.is_file():
        candidates = [declared]
    elif declared.is_dir():
        candidates = [
            candidate
            for candidate in declared.rglob("*")
            if not any(part in _BUNDLE_EXCLUDED_PARTS for part in candidate.relative_to(root).parts)
        ]
    else:
        raise DistributionPathError("bundle resources must be regular files or directories")

    files: list[Path] = []
    for candidate in candidates:
        if candidate.is_symlink():
            raise DistributionPathError(
                f"bundle resource {candidate.relative_to(root)} must not use symlinks"
            )
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(root)
        except (OSError, RuntimeError, ValueError):
            raise DistributionPathError(
                f"bundle resource {candidate.relative_to(root)} escapes the distribution root"
            ) from None
        if resolved.is_file():
            files.append(candidate)
    return sorted(files, key=lambda item: item.relative_to(root).as_posix().encode())


def _validate_bundle_resource_environment_references(
    root: Path,
    resources: dict[str, BundleResource],
    contract: EnvironmentContract,
) -> None:
    """Validate value-free references in every declared textual source file."""
    declarations = {(variable.consumer, variable.name): variable for variable in contract.variables}
    for resource_name, resource in resources.items():
        for candidate in _iter_bundle_resource_files(root, Path(resource.path)):
            try:
                text = candidate.resolve(strict=True).read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            except OSError:
                raise DistributionPathError(
                    f"cannot scan bundle resource {resource_name}"
                ) from None
            for name, reference_has_default, secret_context in _scan_environment_references(
                text,
                label=f"bundle resource {resource_name}",
            ):
                declaration = declarations.get((resource.consumer, name))
                if declaration is None:
                    raise DistributionEnvironmentError(
                        f"bundle resource {resource_name} environment reference {name} "
                        f"is not declared for consumer {resource.consumer}"
                    )
                if reference_has_default and not declaration.default:
                    raise DistributionEnvironmentError(
                        f"bundle resource {resource_name} environment reference {name} "
                        "uses a default not declared by the contract"
                    )
                if secret_context and declaration.classification != "secret":
                    raise DistributionEnvironmentError(
                        f"bundle resource {resource_name} environment reference {name} "
                        "must be classified secret"
                    )
                if declaration.classification == "secret" and resource.classification in {
                    "public",
                    "generated",
                }:
                    raise DistributionEnvironmentError(
                        f"bundle resource {resource_name} is classified "
                        f"{resource.classification} but references secret {name}"
                    )


def _validate_runtime_resource_inventory(
    runtime: DistributionConfig,
    resources: dict[str, BundleResource],
) -> None:
    """Require every v2 runtime dependency to have an explicit backend owner."""
    if runtime.schema_version != 2:
        return
    dependencies = {
        **{f"gateway.{name}": value for name, value in runtime.paths.model_dump().items() if value},
        **{
            f"rag.{name}": value
            for name, value in runtime.resources.rag.model_dump().items()
            if value
        },
    }
    for dependency_name, dependency_value in dependencies.items():
        dependency = Path(dependency_value)
        covered = False
        for resource in resources.values():
            if resource.consumer != "backend":
                continue
            declared = Path(resource.path)
            try:
                dependency.relative_to(declared)
            except ValueError:
                continue
            covered = True
            break
        if not covered:
            raise DistributionPathError(
                f"runtime resource {dependency_name} is not covered by a bundle backend resource"
            )


def load_distribution_bundle(path: Path) -> DistributionBundle:
    """Load a strict bundle manifest and validate all declared local paths."""
    data = _read_yaml_mapping(path, label="distribution bundle")
    try:
        bundle = DistributionBundle.model_validate(data)
    except Exception as exc:
        details = _safe_schema_error_details(exc)
        suffix = f": {details}" if details else ""
        raise DistributionSchemaError(f"invalid distribution bundle schema{suffix}") from None

    root = path.resolve().parent
    runtime_manifest = _closed_root_path(
        root,
        bundle.runtime_manifest,
        label="runtime_manifest",
        require_file=True,
        reject_symlinks=True,
    )
    environment_contract = _closed_root_path(
        root,
        bundle.environment_contract,
        label="environment_contract",
        require_file=True,
        reject_symlinks=True,
    )
    runtime = validate_distribution_runtime_manifest(
        runtime_manifest,
        root=root,
        require_v2=False,
    )
    resolved_resources: dict[str, BundleResource] = {}
    for name, resource in bundle.resources.items():
        resolved = _closed_root_path(
            root,
            resource.path,
            label=f"resources.{name}",
            reject_symlinks=True,
        )
        resolved_resources[name] = resource.model_copy(update={"path": str(resolved)})

    contract = load_environment_contract(environment_contract)
    if runtime.schema_version == 2 and Path(runtime.environment_contract) != environment_contract:
        raise DistributionEnvironmentError(
            "bundle and runtime manifests must declare the same environment_contract"
        )
    _validate_runtime_resource_inventory(runtime, resolved_resources)
    declarations = {(item.consumer, item.name): item for item in contract.variables}
    missing_external = sorted(
        item.from_env
        for item in bundle.external_inputs.values()
        if (item.consumer, item.from_env) not in declarations
    )
    if missing_external:
        raise DistributionEnvironmentError(
            "bundle external inputs missing matching environment-contract entries: "
            + ", ".join(missing_external)
        )
    incompatible_external = sorted(
        item.from_env
        for item in bundle.external_inputs.values()
        if (declaration := declarations.get((item.consumer, item.from_env))) is not None
        and (declaration.required != item.required or (item.required and declaration.default))
    )
    if incompatible_external:
        raise DistributionEnvironmentError(
            "bundle external inputs have incompatible required/default environment "
            "contracts: " + ", ".join(incompatible_external)
        )
    _validate_bundle_resource_environment_references(root, resolved_resources, contract)

    resolved_bundle = bundle.model_copy(
        update={
            "runtime_manifest": str(runtime_manifest),
            "environment_contract": str(environment_contract),
            "resources": resolved_resources,
        }
    )
    resolved_bundle._root = root
    return resolved_bundle


def _bundle_file_candidates(
    bundle_path: Path,
    bundle: DistributionBundle,
) -> list[tuple[Path, str]]:
    root = bundle._root
    roots: list[tuple[Path, str]] = [
        (bundle_path.resolve(), "private"),
        (Path(bundle.runtime_manifest), "private"),
        (Path(bundle.environment_contract), "secret-reference"),
        *((Path(item.path), item.classification) for item in bundle.resources.values()),
    ]
    candidates: dict[Path, str] = {}
    for declared, classification in roots:
        if declared.is_dir():
            for candidate in declared.rglob("*"):
                if any(
                    part in _BUNDLE_EXCLUDED_PARTS for part in candidate.relative_to(root).parts
                ):
                    continue
                if candidate.is_file() or candidate.is_symlink():
                    previous = candidates.setdefault(candidate, classification)
                    if previous != classification:
                        raise DistributionConfigError(
                            f"bundle file {candidate.relative_to(root)} has conflicting "
                            "classifications"
                        )
        else:
            previous = candidates.setdefault(declared, classification)
            if previous != classification:
                raise DistributionConfigError(
                    f"bundle file {declared.relative_to(root)} has conflicting classifications"
                )
    return sorted(
        candidates.items(),
        key=lambda item: item[0].relative_to(root).as_posix().encode(),
    )


def build_distribution_bundle_lock(bundle_path: Path) -> dict:
    """Build the deterministic, root-independent lock document."""
    bundle = load_distribution_bundle(bundle_path)
    root = bundle._root
    files: list[dict[str, str]] = []
    for candidate, classification in _bundle_file_candidates(bundle_path, bundle):
        relative = candidate.relative_to(root).as_posix()
        if relative == "bundle.lock.json":
            continue
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(root)
            raw = resolved.read_bytes()
            metadata = candidate.lstat()
        except (OSError, RuntimeError, ValueError):
            raise DistributionConfigError(f"bundle lock input {relative} is invalid") from None
        files.append(
            {
                "path": relative,
                "type": "symlink" if candidate.is_symlink() else "file",
                "mode": f"{stat.S_IMODE(metadata.st_mode):04o}",
                "classification": classification,
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
        )
    return {
        "bundle_lock_schema_version": 1,
        "bundle_schema_version": bundle.bundle_schema_version,
        "files": files,
    }


def render_distribution_bundle_lock(bundle_path: Path) -> str:
    """Return canonical JSON for a bundle lock."""
    return (
        json.dumps(
            build_distribution_bundle_lock(bundle_path),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def validate_distribution_bundle_lock(
    bundle_path: Path,
    lock_path: Path | None = None,
) -> None:
    """Reject a missing, malformed, or stale deterministic bundle lock."""
    expected = build_distribution_bundle_lock(bundle_path)
    target = lock_path or bundle_path.with_name("bundle.lock.json")
    try:
        actual = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise DistributionLockError(f"cannot read bundle lock {target}") from None
    if actual != expected:
        raise DistributionLockError(f"bundle lock {target} is stale")


def validate_distribution_bundle_manifest(
    path: Path,
    *,
    root: Path | None = None,
    lock_path: Path | None = None,
    require_runtime_v2: bool = True,
) -> DistributionBundle:
    """Validate a bundle, runtime, contract, resources, and deterministic lock."""
    validation_root, bundle_path = _resolve_validation_input(
        path,
        root,
        label="bundle manifest",
    )
    bundle = load_distribution_bundle(bundle_path)
    runtime = load_distribution_config(Path(bundle.runtime_manifest))
    if require_runtime_v2 and runtime.schema_version != 2:
        raise DistributionSchemaError(
            "strict bundle validation requires a schema_version 2 runtime manifest"
        )

    try:
        lock_candidate = (
            bundle_path.with_name("bundle.lock.json")
            if lock_path is None
            else lock_path
            if lock_path.is_absolute()
            else validation_root / lock_path
        )
        resolved_lock = lock_candidate.resolve(strict=True)
        resolved_lock.relative_to(validation_root)
        if not resolved_lock.is_file():
            raise ValueError("not a file")
    except (OSError, RuntimeError, ValueError):
        raise DistributionLockError(
            "bundle lock is missing or escapes the validation root"
        ) from None
    validate_distribution_bundle_lock(bundle_path, resolved_lock)
    return bundle


@lru_cache(maxsize=1)
def get_distribution_config() -> DistributionConfig | None:
    """Return the process manifest, preserving v1 fail-open unless required."""
    settings = get_settings()
    configured = settings.distribution_config_path
    if not configured:
        if settings.distribution_config_required:
            raise DistributionStartupError(
                "DISTRIBUTION_CONFIG_REQUIRED=1 but DISTRIBUTION_CONFIG_PATH is unset"
            )
        return None
    try:
        config = load_distribution_config(Path(configured))
    except Exception:
        if settings.distribution_config_required:
            raise DistributionStartupError(
                "required distribution manifest failed static validation"
            ) from None
        logger.error("Distribution manifest failed to load; using legacy config resolution")
        return None

    expected_id = settings.distribution_expected_id.strip()
    if expected_id and config.distribution.id != expected_id:
        raise DistributionStartupError(
            "distribution identity mismatch: "
            f"expected {expected_id!r}, loaded {config.distribution.id!r}"
        )

    logger.info(
        "Distribution manifest loaded: id=%r schema_version=%s target=%r",
        config.distribution.id,
        config.schema_version,
        settings.distribution_target.strip() or "unset",
    )
    if config.schema_version == 1 and not _explicitly_configured("distribution_config_mode"):
        _log_once(
            ("mode-defaulted",),
            "DISTRIBUTION_CONFIG_PATH is set but DISTRIBUTION_CONFIG_MODE is "
            "not: defaulting to 'dark' (manifest compared, not applied). Set "
            "DISTRIBUTION_CONFIG_MODE=active to apply manifest paths.",
            level="warning",
        )
    return config


@dataclass(frozen=True)
class ResolvedConfigPath:
    """A config file location plus which layer selected it."""

    path: Path
    source: Literal["env", "distribution", "default"]


_VALID_V1_MODES = {"active", "dark"}
_VALID_V2_MODES = {"legacy", "shadow", "active", "dark"}

# Resolver decisions are logged once per unique situation, not per call.
_logged_once: set[tuple[str, ...]] = set()


def _log_once(key: tuple[str, ...], message: str, *, level: str = "info") -> None:
    if key in _logged_once:
        return
    _logged_once.add(key)
    getattr(logger, level)(message)


def _effective_v1_mode() -> Literal["active", "dark"]:
    raw = get_settings().distribution_config_mode.strip().lower()
    if raw in _VALID_V1_MODES:
        return raw  # type: ignore[return-value]
    _log_once(
        ("invalid-mode", raw),
        f"Invalid DISTRIBUTION_CONFIG_MODE={raw!r} (expected 'active' or 'dark'); "
        "treating as 'dark': manifest loads and is compared, legacy resolution "
        "stays effective",
        level="warning",
    )
    return "dark"


def _explicitly_configured(field_name: str) -> bool:
    return field_name in get_settings().model_fields_set


def _v2_mode(kind: ConfigKind) -> ConfigMode:
    field_name = f"distribution_{kind}_mode"
    raw: str = getattr(get_settings(), field_name).strip().lower()
    if not raw:
        return "legacy"
    if raw not in _VALID_V2_MODES:
        raise DistributionStartupError(
            f"invalid DISTRIBUTION_{kind.upper()}_MODE={raw!r}; expected legacy, shadow, or active"
        )
    return "shadow" if raw == "dark" else raw  # type: ignore[return-value]


def distribution_config_mode(kind: ConfigKind) -> ConfigMode:
    """Return the normalized effective selector for one runtime resource."""
    config = get_distribution_config()
    if config is None:
        return "legacy"
    if config.schema_version == 1:
        return "active" if _effective_v1_mode() == "active" else "shadow"
    return _v2_mode(kind)


def _env_override(kind: ConfigKind) -> str:
    settings = get_settings()
    value: str = getattr(settings, f"{kind}_config_path")
    if kind == "alerts" and not _explicitly_configured("alerts_config_path"):
        return ""
    return value


def _require_closed_v2_candidate(
    config: DistributionConfig,
    kind: ConfigKind,
    value: str,
) -> Path:
    if not value:
        raise DistributionStartupError(
            f"distribution resources.gateway.{kind} is required for selector "
            f"{distribution_config_mode(kind)!r}"
        )
    candidate = Path(value)
    try:
        candidate.relative_to(config._root)
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(config._root)
    except (OSError, RuntimeError, ValueError):
        raise DistributionStartupError(
            f"distribution resources.gateway.{kind} must be an existing closed-root regular file"
        ) from None
    if not resolved.is_file():
        raise DistributionStartupError(
            f"distribution resources.gateway.{kind} must be a regular file"
        )
    return resolved


def resolve_config_path(kind: ConfigKind) -> ResolvedConfigPath:
    """Resolve one config path under v1 compatibility or v2 selectors."""
    env_value = _env_override(kind)
    if env_value:
        effective = ResolvedConfigPath(path=Path(env_value), source="env")
    else:
        effective = ResolvedConfigPath(path=Path(_LEGACY_DEFAULTS[kind]), source="default")

    config = get_distribution_config()
    manifest_value: str = getattr(config.paths, kind) if config else ""
    if config is None or not manifest_value:
        if config is not None and config.schema_version == 2 and _v2_mode(kind) != "legacy":
            _require_closed_v2_candidate(config, kind, manifest_value)
        return effective

    manifest = ResolvedConfigPath(path=Path(manifest_value), source="distribution")
    if config.schema_version == 1:
        if _effective_v1_mode() == "dark":
            _log_shadow_comparison(
                kind,
                effective=effective,
                manifest=manifest,
                mode_label="dark",
            )
            return effective
        if env_value:
            _log_env_shadow(kind, effective, manifest)
            return effective
        return manifest

    mode = _v2_mode(kind)
    if mode == "legacy":
        return effective
    closed_candidate = _require_closed_v2_candidate(config, kind, manifest_value)
    manifest = ResolvedConfigPath(path=closed_candidate, source="distribution")
    if mode == "shadow":
        _log_shadow_comparison(kind, effective=effective, manifest=manifest)
        return effective
    if env_value:
        if get_settings().distribution_config_required:
            raise DistributionStartupError(
                f"required active distribution {kind} config cannot be overridden by "
                f"{kind.upper()}_CONFIG_PATH"
            )
        _log_env_shadow(kind, effective, manifest)
        return effective
    return manifest


def _log_env_shadow(
    kind: ConfigKind,
    effective: ResolvedConfigPath,
    manifest: ResolvedConfigPath,
) -> None:
    _log_once(
        ("env-shadow", kind, str(effective.path), str(manifest.path)),
        f"{kind} config: explicit env override {effective.path} wins over "
        f"manifest value {manifest.path}",
    )


def _file_digest(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:12]
    except (OSError, ValueError):
        return "missing"


def _log_shadow_comparison(
    kind: ConfigKind,
    *,
    effective: ResolvedConfigPath,
    manifest: ResolvedConfigPath,
    mode_label: Literal["dark", "shadow"] = "shadow",
) -> None:
    effective_digest = _file_digest(effective.path)
    manifest_digest = _file_digest(manifest.path)
    verdict = "identical" if effective_digest == manifest_digest != "missing" else "DIFFERENT"
    _log_once(
        (
            mode_label,
            kind,
            str(effective.path),
            str(manifest.path),
            effective_digest,
            manifest_digest,
        ),
        f"[distribution {mode_label} mode] {kind} config stays {effective.path} "
        f"(source={effective.source}, sha256={effective_digest}); manifest would "
        f"use {manifest.path} (sha256={manifest_digest}) — {verdict}",
    )


def _canonicalize_config(value):
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise DistributionConfigError("config mapping keys must be strings")
        return {key: _canonicalize_config(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [_canonicalize_config(item) for item in value]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if hasattr(value, "isoformat"):
        return value.isoformat()
    raise DistributionConfigError(f"unsupported config scalar type {type(value).__name__}")


def _canonical_hash_from_parsed(parsed: dict) -> str:
    try:
        canonical = json.dumps(
            _canonicalize_config(parsed),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    except (TypeError, ValueError):
        raise DistributionConfigError("config cannot be canonically normalized") from None
    return hashlib.sha256(canonical).hexdigest()


def canonical_config_hash(path: Path) -> str:
    """Hash raw parsed config deterministically without expanding environment values."""
    parsed = _read_yaml_mapping(path, label="runtime config")
    return _canonical_hash_from_parsed(parsed)


def _semantic_placeholders(text: str) -> tuple[tuple[str, str], ...]:
    placeholders: dict[str, str] = {}
    for match in _ENV_REFERENCE_RE.finditer(text):
        name = match.group(1)
        default = match.group(2)
        line_start = text.rfind("\n", 0, match.start()) + 1
        line_end = text.find("\n", match.end())
        if line_end < 0:
            line_end = len(text)
        line = text[line_start:line_end].lower()
        if default is not None:
            placeholder = default
        elif any(token in line for token in ("url", "endpoint", "host")):
            placeholder = "https://distribution.invalid"
        else:
            placeholder = "1"
        previous = placeholders.get(name)
        if previous is None or placeholder.startswith("https://"):
            placeholders[name] = placeholder
    return tuple(sorted(placeholders.items()))


def _validate_raw_candidate(kind: ConfigKind, path: Path) -> _CandidateSnapshot:
    try:
        text = path.read_text(encoding="utf-8")
        parsed = yaml.safe_load(text)
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        raise DistributionStartupError(
            f"distribution {kind} candidate failed raw YAML validation"
        ) from None
    if not isinstance(parsed, dict):
        raise DistributionStartupError(f"distribution {kind} candidate must be a YAML mapping")
    if kind == "models" and not isinstance(parsed.get("models"), list):
        raise DistributionStartupError("distribution models candidate must contain a models list")
    references = _scan_environment_references(
        text,
        label=f"distribution {kind} candidate",
    )
    referenced_names = {name for name, _has_default, _secret_context in references}
    reserved = sorted(referenced_names.intersection(_SEMANTIC_RESERVED_ENV))
    if reserved:
        raise DistributionStartupError(
            "distribution config references reserved validator environment names: "
            + ", ".join(reserved)
        )
    try:
        canonical_sha256 = _canonical_hash_from_parsed(parsed)
    except DistributionConfigError:
        raise DistributionStartupError(
            f"distribution {kind} candidate cannot be canonically normalized"
        ) from None
    return _CandidateSnapshot(
        references=references,
        placeholders=_semantic_placeholders(text),
        canonical_sha256=canonical_sha256,
    )


def _validate_candidate_semantics(
    kind: ConfigKind,
    path: Path,
    snapshot: _CandidateSnapshot,
) -> None:
    python_path = os.pathsep.join(entry for entry in sys.path if entry)
    child_env = {
        **dict(snapshot.placeholders),
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": python_path,
    }
    try:
        completed = subprocess.run(
            [sys.executable, "-c", _SEMANTIC_VALIDATOR, kind, str(path)],
            check=False,
            cwd=tempfile.gettempdir(),
            env=child_env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise DistributionStartupError(
            f"distribution {kind} semantic validator could not complete"
        ) from None
    if completed.returncode != 0:
        raise DistributionStartupError(f"distribution {kind} candidate failed semantic validation")


def _effective_legacy_path(kind: ConfigKind) -> ResolvedConfigPath:
    env_value = _env_override(kind)
    if env_value:
        return ResolvedConfigPath(path=Path(env_value), source="env")
    return ResolvedConfigPath(path=Path(_LEGACY_DEFAULTS[kind]), source="default")


def _canonical_hash_or_status(path: Path) -> str:
    if not path.is_file():
        return "missing"
    try:
        return canonical_config_hash(path)
    except DistributionConfigError:
        return "invalid"


def _emit_config_hash_signal(
    kind: ConfigKind,
    mode: ConfigMode,
    candidate: ResolvedConfigPath | None,
    candidate_hash: str,
) -> None:
    legacy = _effective_legacy_path(kind)
    if mode == "active" and candidate is not None and not _env_override(kind):
        effective = candidate
        effective_hash = candidate_hash
    else:
        effective = legacy
        effective_hash = _canonical_hash_or_status(effective.path)
    if candidate_hash in {"none", "missing"}:
        comparison_status = "candidate_missing"
    elif candidate_hash == "invalid":
        comparison_status = "candidate_invalid"
    elif effective_hash == "missing":
        comparison_status = "effective_missing"
    elif effective_hash == "invalid":
        comparison_status = "effective_invalid"
    elif not candidate_hash or not effective_hash:
        comparison_status = "uncomparable"
    elif candidate_hash == effective_hash:
        comparison_status = "match"
    else:
        comparison_status = "mismatch"
    mismatch = 0 if comparison_status == "match" else 1
    _distribution_config_comparison_state[kind] = DistributionConfigComparisonState(
        resource=kind,
        selector=mode,
        source=effective.source,
        status=comparison_status,
        mismatch=mismatch,
    )
    log = logger.warning if mismatch else logger.info
    log(
        "distribution_config_state kind=%s selector=%s source=%s status=%s "
        "effective_canonical_sha256=%s candidate_canonical_sha256=%s "
        "distribution_config_mismatch=%d",
        kind,
        mode,
        effective.source,
        comparison_status,
        effective_hash,
        candidate_hash,
        mismatch,
    )


def get_distribution_config_comparison_state() -> dict[str, dict[str, str | int]]:
    """Return comparison status without paths, hashes, environment values, or secrets."""
    return {
        kind: {
            "resource": state.resource,
            "selector": state.selector,
            "source": state.source,
            "status": state.status,
            "mismatch": state.mismatch,
        }
        for kind, state in sorted(_distribution_config_comparison_state.items())
    }


def _validate_environment_references(
    config: DistributionConfig,
    references: dict[RuntimeResourceKind, frozenset[EnvironmentReference]],
    *,
    active_resources: set[RuntimeResourceKind],
    check_required_values: bool,
) -> None:
    if not config.environment_contract:
        if any(references.values()):
            raise DistributionEnvironmentError(
                "distribution resources use environment references but environment_contract "
                "is unset"
            )
        return
    try:
        contract_path = Path(config.environment_contract).resolve(strict=True)
        contract_path.relative_to(config._root)
        if not contract_path.is_file():
            raise ValueError("not a file")
    except (OSError, RuntimeError, ValueError):
        raise DistributionPathError(
            "distribution environment_contract must be an existing closed-root file"
        ) from None
    try:
        contract = load_environment_contract(contract_path)
    except DistributionConfigError:
        raise DistributionSchemaError("distribution environment contract is invalid") from None

    backend = {
        variable.name: variable for variable in contract.variables if variable.consumer == "backend"
    }
    for kind, refs in references.items():
        for name, reference_has_default, secret_context in refs:
            declaration = backend.get(name)
            if declaration is None:
                raise DistributionEnvironmentError(
                    f"environment reference {name} is not declared for consumer backend"
                )
            if declaration.resources and kind not in declaration.resources:
                raise DistributionEnvironmentError(
                    f"environment reference {name} is not declared for resource {kind}"
                )
            if reference_has_default and not declaration.default:
                raise DistributionEnvironmentError(
                    f"environment reference {name} uses a default not declared by the contract"
                )
            if secret_context and declaration.classification != "secret":
                raise DistributionEnvironmentError(
                    f"environment reference {name} in secret-bearing field "
                    "must be classified secret"
                )

    if not check_required_values:
        return
    missing: list[str] = []
    for variable in backend.values():
        applies = not variable.resources or bool(active_resources.intersection(variable.resources))
        if (
            applies
            and variable.required
            and not variable.default
            and not os.environ.get(variable.name, "").strip()
        ):
            missing.append(variable.name)
    if missing:
        raise DistributionEnvironmentError(
            "required backend environment variables are missing: " + ", ".join(sorted(missing))
        )


def preflight_distribution_config() -> DistributionConfig | None:
    """Validate static v2/required state before initializing any service."""
    settings = get_settings()
    _distribution_config_comparison_state.clear()
    config = get_distribution_config()
    if config is None:
        if settings.distribution_expected_id.strip():
            raise DistributionStartupError(
                "DISTRIBUTION_EXPECTED_ID is set but no distribution manifest loaded"
            )
        return None

    target = settings.distribution_target.strip().lower()
    if target and target not in _TARGETS:
        raise DistributionStartupError(
            f"invalid DISTRIBUTION_TARGET={target!r}; expected one of {sorted(_TARGETS)}"
        )

    if config.schema_version == 1:
        if settings.distribution_config_required:
            raise DistributionStartupError(
                "DISTRIBUTION_CONFIG_REQUIRED=1 requires runtime schema_version 2"
            )
        return config

    if settings.distribution_config_required:
        if _explicitly_configured("distribution_config_mode"):
            raise DistributionStartupError(
                "runtime schema v2 required mode rejects DISTRIBUTION_CONFIG_MODE; "
                "set all three per-resource selectors"
            )
        missing_selectors = [
            kind
            for kind in _CONFIG_KINDS
            if not _explicitly_configured(f"distribution_{kind}_mode")
        ]
        if missing_selectors:
            raise DistributionStartupError(
                "runtime schema v2 required mode needs explicit selectors for: "
                + ", ".join(missing_selectors)
            )
    elif _explicitly_configured("distribution_config_mode"):
        _log_once(
            ("v2-global-mode-ignored",),
            "DISTRIBUTION_CONFIG_MODE is ignored by runtime schema v2; use per-resource selectors",
            level="warning",
        )

    selected = {kind: _v2_mode(kind) for kind in _CONFIG_KINDS}
    try:
        runtime_manifest = Path(settings.distribution_config_path).resolve(strict=True)
        runtime_manifest.relative_to(config._root)
        rag_references = _validate_runtime_declared_paths(
            config._root,
            runtime_manifest,
            config,
        )
        snapshots, candidates = _validate_declared_gateway_candidates(config)
    except DistributionStartupError:
        raise
    except DistributionConfigError as exc:
        raise DistributionStartupError(
            f"distribution runtime strict validation failed: {exc}"
        ) from None

    for kind, mode in selected.items():
        if mode == "active" and settings.distribution_config_required and _env_override(kind):
            raise DistributionStartupError(
                f"required active distribution {kind} config cannot be overridden by "
                f"{kind.upper()}_CONFIG_PATH"
            )
        if mode == "legacy":
            continue
        if candidates[kind] is None:
            _require_closed_v2_candidate(config, kind, getattr(config.paths, kind))

    references: dict[RuntimeResourceKind, frozenset[EnvironmentReference]] = {
        kind: snapshot.references if snapshot else frozenset()
        for kind, snapshot in snapshots.items()
    }
    references["rag"] = rag_references
    active_resources: set[RuntimeResourceKind] = {
        kind for kind, mode in selected.items() if mode != "legacy"
    }
    if any(config.resources.rag.model_dump().values()):
        active_resources.add("rag")
    try:
        _validate_environment_references(
            config,
            references,
            active_resources=active_resources,
            check_required_values=settings.distribution_config_required,
        )
        _validate_declared_gateway_semantics(snapshots, candidates)
    except DistributionConfigError as exc:
        if isinstance(exc, DistributionStartupError):
            raise
        raise DistributionStartupError(str(exc)) from None

    for kind, snapshot in snapshots.items():
        candidate = candidates[kind]
        _emit_config_hash_signal(
            kind,
            selected[kind],
            candidate,
            snapshot.canonical_sha256 if snapshot else "none",
        )
    logger.info(
        "Distribution preflight passed: id=%r target=%r selectors=%s",
        config.distribution.id,
        target or "unset",
        ",".join(f"{kind}={selected[kind]}" for kind in _CONFIG_KINDS),
    )
    return config


def distribution_resource_is_fail_closed(kind: ConfigKind) -> bool:
    """Whether an active v2 resource error must escape compatibility catches."""
    settings = get_settings()
    if not settings.distribution_config_required:
        return False
    config = get_distribution_config()
    return bool(config and config.schema_version == 2 and _v2_mode(kind) == "active")


def raise_if_distribution_resource_required(kind: ConfigKind, exc: Exception) -> None:
    """Turn an active required resource failure into a value-safe startup error."""
    if distribution_resource_is_fail_closed(kind):
        raise DistributionStartupError(
            f"required distribution {kind} config failed bootstrap validation "
            f"({type(exc).__name__})"
        ) from None
