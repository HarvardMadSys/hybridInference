"""Database-backed public site configuration.

Runtime consumers use get_active_distribution_config, which reads the validated
SITE_CONFIGURATION and SITE_BRANDING settings. No manifest file is read.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

from serving.config.branding import BrandingConfig
from serving.utils.logging import get_logger

logger = get_logger(__name__)


class _ManifestModel(BaseModel):
    """Ignore unknown section keys unless a policy-bearing model opts out."""

    model_config = ConfigDict(extra="ignore")


class DistributionInfo(_ManifestModel):
    """Identity of the distribution consuming this deployment."""

    id: str
    display_name: str = ""
    release: str = ""


class DistributionSite(_ManifestModel):
    """Site identity. ``branding`` is a retired file path; validation refuses one."""

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
    # False disables the signup API as well as its UI.
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
    """Retired config-file locations, kept in the schema so validation can refuse them."""

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


def get_active_distribution_config() -> DistributionConfig | None:
    """Return the database site policy, effective in every process after refresh."""
    return get_distribution_config()


# Settings changes invalidate the parsed public configuration in this process,
# and let a configuration still invalid after one be reported again.
from serving.config.app_config import on_change

on_change(get_distribution_config.cache_clear)
on_change(_forget_report)
