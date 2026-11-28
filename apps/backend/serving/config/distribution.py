"""Database-backed public site configuration and explicit legacy import helpers.

Runtime consumers use get_active_distribution_config, which reads validated JSON
settings and never loads a manifest file. The file loader is retained only for
the explicit legacy import (``python -m serving.config.manage import-legacy``).
"""

from __future__ import annotations

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
    # Opt-in: publish the GPU fleet and engine metrics the OpenInfra reporters
    # collect at /openinfra (GET /public-openinfra and its history). None and
    # False keep them private; collecting needs only OPENINFRA_INGEST_TOKEN.
    openinfra: bool | None = None


class DistributionPaths(_ManifestModel):
    """Config-file locations; relative values resolve against the manifest."""

    models: str = ""
    routing: str = ""
    alerts: str = ""
    mcp: str = ""


class DistributionDeployment(_ManifestModel):
    """Deployment target label (informational)."""

    target: str = ""


class DistributionAccounting(_ManifestModel):
    """Expected ledger currency; existing databases require an explicit migration."""

    model_config = ConfigDict(extra="forbid")
    currency: Literal["USD", "CNY"] = "USD"


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
    accounting: DistributionAccounting = Field(default_factory=DistributionAccounting)

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
        _report_invalid()
        raise DistributionConfigError(
            "Ask admin to configure the site in Admin Settings."
        ) from None


#: The invalid site configuration has been reported since the last change.
#: Every reader runs into it, OpenRouter attribution and ``/site-config`` on
#: each request, so it is logged once per configuration change, not per call.
_invalid_reported = False


def _report_invalid() -> None:
    """Log, once per configuration change, what is wrong with the site documents."""
    global _invalid_reported
    if _invalid_reported:
        return
    _invalid_reported = True
    from serving.config.app_config import validation_problem

    problems = "; ".join(
        f"{key}: {problem}"
        for key in ("SITE_CONFIGURATION", "SITE_BRANDING")
        if (problem := validation_problem(key))
    )
    logger.error(
        "The public site configuration is not valid (%s). Until it is corrected in Admin "
        "Settings, the site has the neutral identity, public signup is closed and the "
        "console reports the configuration incomplete.",
        problems or "the documents do not validate together",
    )


def _forget_report() -> None:
    global _invalid_reported
    _invalid_reported = False


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

    This fallback applies to the legacy import, where it prevents an
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


# Settings changes invalidate the parsed public configuration in this process,
# and let a configuration still invalid after one be reported again.
from serving.config.app_config import on_change

on_change(get_distribution_config.cache_clear)
on_change(_forget_report)
