"""Distribution manifest: schema, loader, and config-path resolution.

A distribution manifest (design doc:
``docs/agents/specs/2026-07-16-hybridinference-neutral-upstream-multi-distribution-design.zh.md``)
lets a deployment supply site identity and config-file locations as one
versioned document instead of scattered env vars. Config-path resolution
precedence, per the design doc's compatibility rules:

1. explicit env var (``MODELS_CONFIG_PATH`` etc.) — behavior unchanged;
2. the manifest's ``paths:`` section;
3. the legacy ``config/*.yaml`` defaults.

The loader is opt-in (``DISTRIBUTION_CONFIG_PATH`` unset means pure legacy
behavior) and fail-open: a broken manifest logs an error and the process
falls back to legacy resolution instead of refusing to start.
``DISTRIBUTION_CONFIG_MODE=dark`` loads and validates the manifest and logs
what WOULD change while legacy resolution stays effective — the DARK_LOADED /
SHADOW_COMPARE migration states from the design doc.

``site:`` and ``features:`` are parsed and validated here but not yet
consumed; the frontend site-config endpoint wires them up in a later PR.
Manifest values must not contain secrets; env interpolation is deliberately
unsupported in ``schema_version: 1``.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from serving.config.settings import get_settings
from serving.utils.logging import get_logger

logger = get_logger(__name__)

ConfigKind = Literal["models", "routing", "alerts"]

_LEGACY_DEFAULTS: dict[str, str] = {
    "models": "config/models.yaml",
    "routing": "config/routing.yaml",
    "alerts": "config/alerts.yaml",
}


class _ManifestModel(BaseModel):
    """Base for manifest sections: unknown keys ignored for forward compat."""

    model_config = ConfigDict(extra="ignore")


class DistributionInfo(_ManifestModel):
    """Identity of the distribution consuming this deployment."""

    id: str
    display_name: str = ""
    release: str = ""


class DistributionSite(_ManifestModel):
    """Site identity handed to the frontend site-config endpoint (later PR)."""

    public_base_url: str = ""
    support_email: str = ""
    terms_document: str = ""
    privacy_document: str = ""
    branding: str = ""


class DistributionFeatures(_ManifestModel):
    """Feature toggles a distribution opts into."""

    routers: list[str] = Field(default_factory=list)
    public_signup: bool | None = None
    rag: bool | None = None


class DistributionPaths(_ManifestModel):
    """Config-file locations; relative values resolve against the manifest."""

    models: str = ""
    routing: str = ""
    alerts: str = ""


class DistributionDeployment(_ManifestModel):
    """Deployment target label (informational)."""

    target: str = ""


class DistributionConfig(_ManifestModel):
    """Validated distribution manifest."""

    schema_version: int = Field(ge=1, le=1)
    distribution: DistributionInfo
    site: DistributionSite = Field(default_factory=DistributionSite)
    features: DistributionFeatures = Field(default_factory=DistributionFeatures)
    paths: DistributionPaths = Field(default_factory=DistributionPaths)
    deployment: DistributionDeployment = Field(default_factory=DistributionDeployment)


class DistributionConfigError(Exception):
    """Raised when a distribution manifest cannot be loaded or validated."""


def load_distribution_config(path: Path) -> DistributionConfig:
    """Load and validate a manifest; relative ``paths:`` resolve against it.

    Raises:
        DistributionConfigError: On unreadable files, YAML errors, or
            schema validation failures (including unsupported
            ``schema_version``).
    """
    try:
        data = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as exc:
        raise DistributionConfigError(f"cannot read distribution manifest {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise DistributionConfigError(f"distribution manifest {path} must be a YAML mapping")
    try:
        config = DistributionConfig.model_validate(data)
    except Exception as exc:
        raise DistributionConfigError(f"invalid distribution manifest {path}: {exc}") from exc

    root = path.resolve().parent
    resolved = {
        kind: str((root / value).resolve()) if value and not Path(value).is_absolute() else value
        for kind, value in config.paths.model_dump().items()
    }
    return config.model_copy(update={"paths": DistributionPaths(**resolved)})


@lru_cache(maxsize=1)
def get_distribution_config() -> DistributionConfig | None:
    """Cached manifest for this process; None when unset or failed to load."""
    configured = get_settings().distribution_config_path
    if not configured:
        return None
    try:
        config = load_distribution_config(Path(configured))
    except DistributionConfigError:
        logger.exception("Distribution manifest failed to load; using legacy config resolution")
        return None
    logger.info(
        f"Distribution manifest loaded: id={config.distribution.id!r} "
        f"release={config.distribution.release!r} "
        f"mode={get_settings().distribution_config_mode!r}"
    )
    return config


@dataclass(frozen=True)
class ResolvedConfigPath:
    """A config file location plus which layer of the precedence chose it."""

    path: Path
    source: str  # "env" | "distribution" | "default"


def _env_override(kind: ConfigKind) -> str:
    settings = get_settings()
    value: str = getattr(settings, f"{kind}_config_path")
    if kind == "alerts":
        # alerts_config_path predates this module and defaults to the legacy
        # path instead of "": only a non-default value counts as explicit.
        return value if value != _LEGACY_DEFAULTS["alerts"] else ""
    return value


def resolve_config_path(kind: ConfigKind) -> ResolvedConfigPath:
    """Apply the env > distribution > legacy-default precedence for one file."""
    env_value = _env_override(kind)
    if env_value:
        return ResolvedConfigPath(path=Path(env_value), source="env")

    legacy = ResolvedConfigPath(path=Path(_LEGACY_DEFAULTS[kind]), source="default")
    dist = get_distribution_config()
    manifest_value: str = getattr(dist.paths, kind) if dist else ""
    if not manifest_value:
        return legacy

    manifest = ResolvedConfigPath(path=Path(manifest_value), source="distribution")
    if get_settings().distribution_config_mode == "dark":
        _log_dark_comparison(kind, legacy=legacy, manifest=manifest)
        return legacy
    return manifest


def _file_digest(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:12]
    except OSError:
        return "missing"


def _log_dark_comparison(
    kind: ConfigKind, *, legacy: ResolvedConfigPath, manifest: ResolvedConfigPath
) -> None:
    legacy_digest = _file_digest(legacy.path)
    manifest_digest = _file_digest(manifest.path)
    verdict = "identical" if legacy_digest == manifest_digest != "missing" else "DIFFERENT"
    logger.info(
        f"[distribution dark mode] {kind} config stays {legacy.path} "
        f"(sha256={legacy_digest}); manifest would use {manifest.path} "
        f"(sha256={manifest_digest}) — {verdict}"
    )
