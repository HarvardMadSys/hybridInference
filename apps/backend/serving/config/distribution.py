"""Database-backed public site configuration and explicit legacy import helpers.

Runtime consumers use get_active_distribution_config, which reads validated JSON
settings and never loads a manifest file. The file loader and path resolver are
retained only for explicit previous-release tooling, not application startup.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

from serving.config.branding import BrandingConfig, BrandingConfigError, load_branding_config
from serving.config.settings import get_settings
from serving.utils.logging import get_logger

logger = get_logger(__name__)

ConfigKind = Literal["models", "routing", "alerts", "mcp"]

# The bottom of the precedence, and what a checkout with no distribution and no
# environment gets. It used to name config/*.yaml, where these files lived until
# they moved into the overlay — after which the fallback named nothing and a
# clone served an empty catalogue. Pointing it at the reference registries makes
# the neutral case work *without* an environment variable, which matters: a
# variable would win over the manifest and make a deployment's manifest
# decorative. Alerts keeps a path nobody ships on purpose — there is no example
# to name, and the loader's answer to a missing alerts file is the built-in
# thresholds, which is the right neutral behaviour.
_LEGACY_DEFAULTS: dict[str, str] = {
    "models": "config/examples/models.openrouter.yaml",
    "routing": "config/examples/routing.minimal.yaml",
    "alerts": "config/alerts.yaml",
    # Accepted for compatibility and read by nothing: the MCP loader left with
    # the cloud agent (configuration.md, "Settings that currently have no effect").
    "mcp": "config/mcp.yaml",
}


class _ManifestModel(BaseModel):
    """Ignore unknown section keys unless a policy-bearing model opts out."""

    model_config = ConfigDict(extra="ignore")


class DistributionInfo(_ManifestModel):
    """Identity of the distribution consuming this deployment."""

    id: str
    display_name: str = ""
    release: str = ""


class DistributionSite(_ManifestModel):
    """Site identity and the path to the distribution-owned branding document."""

    public_base_url: str = ""
    support_email: str = ""
    terms_document: str = ""
    privacy_document: str = ""
    branding: str = ""


class DistributionFeatures(_ManifestModel):
    """Feature toggles a distribution opts into."""

    # Security restrictions must not disappear because a key was misspelled.
    model_config = ConfigDict(extra="forbid")

    routers: list[str] = Field(default_factory=list)
    # In active mode, false disables the signup API as well as its UI.
    # True/None defer to the effective runtime/environment signup setting.
    public_signup: bool | None = None
    rag: bool | None = None
    # Opt-in: publish aggregate usage stats at /stats (GET /public-stats).
    # None and False keep them private.
    public_stats: bool | None = None


class DistributionPaths(_ManifestModel):
    """Config-file locations; relative values resolve against the manifest."""

    models: str = ""
    routing: str = ""
    alerts: str = ""
    mcp: str = ""


class DistributionDeployment(_ManifestModel):
    """Deployment target label (informational)."""

    target: str = ""


class DistributionConfig(_ManifestModel):
    """Validated distribution manifest."""

    # In particular, reject feature fields accidentally placed at the root.
    model_config = ConfigDict(extra="forbid")

    _branding_config: BrandingConfig | None = PrivateAttr(default=None)

    schema_version: int = Field(ge=1, le=1)
    distribution: DistributionInfo
    site: DistributionSite = Field(default_factory=DistributionSite)
    features: DistributionFeatures = Field(default_factory=DistributionFeatures)
    paths: DistributionPaths = Field(default_factory=DistributionPaths)
    deployment: DistributionDeployment = Field(default_factory=DistributionDeployment)

    @property
    def branding_config(self) -> BrandingConfig | None:
        """Return the validated branding snapshot declared by this manifest."""
        return self._branding_config


class DistributionConfigError(Exception):
    """Raised when a distribution manifest cannot be loaded or validated."""


def load_distribution_config(path: Path) -> DistributionConfig:
    """Load and validate a manifest and its declared public documents.

    Relative ``paths:`` and ``site.branding`` values resolve against the
    manifest directory.

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
        resolved_paths = {
            kind: str((root / value).resolve()) if value else value
            for kind, value in config.paths.model_dump().items()
        }
        resolved_branding = (
            str((root / config.site.branding).resolve()) if config.site.branding else ""
        )
    except (OSError, RuntimeError, ValueError) as exc:
        raise DistributionConfigError(
            f"cannot resolve paths in distribution manifest {path}: {exc}"
        ) from exc

    branding_config: BrandingConfig | None = None
    if resolved_branding:
        try:
            branding_config = load_branding_config(Path(resolved_branding))
        except BrandingConfigError as exc:
            raise DistributionConfigError(
                f"branding declared by distribution manifest {path} did not load: {exc}"
            ) from exc

    resolved_site = config.site.model_copy(update={"branding": resolved_branding})
    resolved_config = config.model_copy(
        update={"paths": DistributionPaths(**resolved_paths), "site": resolved_site}
    )
    resolved_config._branding_config = branding_config
    return resolved_config


def _refuse_or_fall_back(configured: str, exc: Exception, *, unexpected: bool = False) -> None:
    """Log a manifest load failure, and refuse to continue when it is the truth.

    #952 made failing open a property of this module, and it was the right one
    while the manifest was dark-only: a file that will not load costs a
    comparison and nothing else, because the environment still supplies every
    effective path. Active mode changes that arithmetic. There the manifest
    *is* where the paths come from, so falling back to legacy resolution means
    quietly serving a different registry than the deployment named — which is
    what a lost ``distributions/`` mount would produce, and what nobody would
    notice. A deployment that asked for the manifest should stop instead.
    """
    kind = "Unexpected error loading" if unexpected else "Distribution manifest failed to load:"
    if _effective_mode() == "active":
        logger.critical(f"{kind} {configured!r} — refusing to start in active mode")
        raise DistributionConfigError(
            f"distribution manifest {configured!r} is the configured source of "
            f"deployment identity and config paths (DISTRIBUTION_CONFIG_MODE=active) "
            f"and did not load: {exc}"
        ) from exc
    logger.exception(f"{kind} {configured!r}; using legacy config resolution")


@lru_cache(maxsize=1)
def get_distribution_config() -> DistributionConfig | None:
    """Return validated database site configuration; never inspect a manifest file."""
    from serving.config.documents import get

    try:
        value = get("distribution")
        branding = get("branding")
        if not value and not branding:
            return None
        config = DistributionConfig.model_validate(
            value or {"schema_version": 1, "distribution": {"id": "neutral"}}
        )
        config._branding_config = BrandingConfig.model_validate(branding) if branding else None
        return config
    except ValueError:
        raise DistributionConfigError(
            "Ask admin to configure the site in Admin Settings."
        ) from None


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

    This fallback applies to config-path resolution, where it prevents an
    accidental activation. Feature policies and public identity use
    ``get_active_distribution_config`` to reject unknown modes instead of
    silently discarding a restriction.
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


def get_active_distribution_config() -> DistributionConfig | None:
    """Return the database site policy, effective in every process after refresh."""
    return get_distribution_config()


def _explicitly_configured(field_name: str) -> bool:
    """True when the operator supplied the field from ANY settings source.

    ``model_fields_set`` covers process env (any case — Settings runs
    ``case_sensitive=False``), pydantic's native ``.env`` loading, aliases,
    and constructor input alike; an ``os.environ`` key check would miss
    everything but exact-case process env.
    """
    return field_name in get_settings().model_fields_set


def _env_override(kind: ConfigKind) -> str:
    settings = get_settings()
    value: str = getattr(settings, f"{kind}_config_path")
    if kind == "alerts" and not _explicitly_configured("alerts_config_path"):
        # alerts_config_path predates this module and its Settings default is
        # the legacy path instead of "". Explicitness therefore means "the
        # operator supplied the field" (env, .env, or constructor) — one who
        # sets it to the default value still wins over the manifest.
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


# Settings changes invalidate the parsed public configuration in this process.
from serving.config.app_config import on_change

on_change(get_distribution_config.cache_clear)
