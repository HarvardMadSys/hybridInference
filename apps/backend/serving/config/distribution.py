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
``DISTRIBUTION_CONFIG_MODE`` defaults to ``dark``: the manifest loads,
validates, and logs what WOULD change while current resolution stays
effective — the DARK_LOADED / SHADOW_COMPARE migration states from the
design doc. Applying manifest paths requires an explicit
``DISTRIBUTION_CONFIG_MODE=active``, so setting only the path can never
change behavior.

``site:`` and ``features:`` are parsed and validated here but not yet
consumed; the frontend site-config endpoint wires them up in a later PR.
Manifest values must not contain secrets; env interpolation is deliberately
unsupported in ``schema_version: 1``.
"""

from __future__ import annotations

import hashlib
import os
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
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise DistributionConfigError(f"cannot read distribution manifest {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise DistributionConfigError(f"distribution manifest {path} must be a YAML mapping")
    try:
        config = DistributionConfig.model_validate(data)
    except Exception as exc:
        raise DistributionConfigError(f"invalid distribution manifest {path}: {exc}") from exc

    try:
        root = path.resolve().parent
        # `root / value` keeps absolute values as-is and anchors relative ones
        # at the manifest directory; resolving unconditionally validates both
        # forms (e.g. embedded NUL bytes raise here instead of at use time).
        resolved = {
            kind: str((root / value).resolve()) if value else value
            for kind, value in config.paths.model_dump().items()
        }
    except (OSError, RuntimeError, ValueError) as exc:
        raise DistributionConfigError(
            f"cannot resolve paths in distribution manifest {path}: {exc}"
        ) from exc
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
    except Exception:
        logger.exception(
            "Unexpected error loading distribution manifest; using legacy config resolution"
        )
        return None
    logger.info(
        f"Distribution manifest loaded: id={config.distribution.id!r} "
        f"release={config.distribution.release!r} "
        f"mode={get_settings().distribution_config_mode!r}"
    )
    if not _env_has("DISTRIBUTION_CONFIG_MODE"):
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
    """A config file location plus which layer of the precedence chose it."""

    path: Path
    source: str  # "env" | "distribution" | "default"


_VALID_MODES = {"active", "dark"}

# Resolver decisions are logged once per unique situation, not per call: the
# admin provider registry resolves the models path on every request.
_logged_once: set[tuple[str, ...]] = set()


def _log_once(key: tuple[str, ...], message: str, *, level: str = "info") -> None:
    if key in _logged_once:
        return
    _logged_once.add(key)
    getattr(logger, level)(message)


def _effective_mode() -> str:
    """Normalize the configured mode; unknown values degrade to ``dark``.

    ``dark`` is the fail-safe direction: the manifest is loaded and compared
    but never changes effective resolution, so a typo can only suppress a
    planned activation — never activate one.
    """
    raw = get_settings().distribution_config_mode.strip().lower()
    if raw in _VALID_MODES:
        return raw
    _log_once(
        ("invalid-mode", raw),
        f"Invalid DISTRIBUTION_CONFIG_MODE={raw!r} (expected 'active' or 'dark'); "
        "treating as 'dark': manifest loads and is compared, legacy resolution "
        "stays effective",
        level="warning",
    )
    return "dark"


def _env_has(name: str) -> bool:
    """Case-insensitive os.environ presence check.

    Settings runs with ``case_sensitive=False``, so ``alerts_config_path=x``
    in the environment reaches pydantic; an exact-case ``in os.environ`` test
    would miss it and let the manifest shadow an explicit override.
    """
    upper = name.upper()
    return any(key.upper() == upper for key in os.environ)


def _env_override(kind: ConfigKind) -> str:
    settings = get_settings()
    value: str = getattr(settings, f"{kind}_config_path")
    if kind == "alerts" and not _env_has("ALERTS_CONFIG_PATH"):
        # alerts_config_path predates this module and its Settings default is
        # the legacy path instead of "". Explicitness therefore comes from the
        # variable actually being present in the environment — an operator who
        # sets ALERTS_CONFIG_PATH to the default value still wins over the
        # manifest. (bootstrap's load_dotenv() puts .env values into
        # os.environ, so file-configured deployments are covered.)
        return ""
    return value


def resolve_config_path(kind: ConfigKind) -> ResolvedConfigPath:
    """Apply the env > distribution > legacy-default precedence for one file.

    The manifest, when configured, is always loaded and validated — even when
    an env override wins — so dark mode compares against what is actually
    effective and a broken manifest surfaces at startup rather than at
    cutover.
    """
    env_value = _env_override(kind)
    if env_value:
        effective = ResolvedConfigPath(path=Path(env_value), source="env")
    else:
        effective = ResolvedConfigPath(path=Path(_LEGACY_DEFAULTS[kind]), source="default")

    dist = get_distribution_config()
    manifest_value: str = getattr(dist.paths, kind) if dist else ""
    if not manifest_value:
        return effective

    manifest = ResolvedConfigPath(path=Path(manifest_value), source="distribution")
    if _effective_mode() == "dark":
        _log_dark_comparison(kind, effective=effective, manifest=manifest)
        return effective
    if env_value:
        _log_once(
            ("env-shadow", kind, env_value, manifest_value),
            f"{kind} config: explicit env override {effective.path} wins over "
            f"manifest value {manifest.path}",
        )
        return effective
    return manifest


def _file_digest(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:12]
    except (OSError, ValueError):
        return "missing"


def _log_dark_comparison(
    kind: ConfigKind, *, effective: ResolvedConfigPath, manifest: ResolvedConfigPath
) -> None:
    effective_digest = _file_digest(effective.path)
    manifest_digest = _file_digest(manifest.path)
    verdict = "identical" if effective_digest == manifest_digest != "missing" else "DIFFERENT"
    _log_once(
        ("dark", kind, str(effective.path), str(manifest.path), effective_digest, manifest_digest),
        f"[distribution dark mode] {kind} config stays {effective.path} "
        f"(source={effective.source}, sha256={effective_digest}); manifest would "
        f"use {manifest.path} (sha256={manifest_digest}) — {verdict}",
    )
