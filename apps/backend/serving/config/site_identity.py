"""Site identity for backend-embedded user-facing content.

Transactional emails, broadcast templates, the docs-assistant system prompt,
quota help text, and OpenRouter attribution headers all carry the operator's
identity. This module gives them a single resolution chain instead of
hardcoded strings:

1. explicit environment variables (``SITE_NAME``, ``SITE_PUBLIC_BASE_URL``,
   ``SITE_DOCS_URL``, ``SITE_SUPPORT_EMAIL``);
2. the active distribution manifest (``distribution.display_name`` plus the
   ``site:`` section) — only when ``DISTRIBUTION_CONFIG_MODE=active``,
   mirroring the gating of ``GET /site-config``;
3. a neutral default that names no distribution.

A deployment supplies its own identity through ``SITE_*``, which
``deploy/docker/docker-compose.yml`` pins for both staging and production (and
``.env`` still overrides), so a configured site's rendered output is unchanged
by the neutral default. The manifest's versioned branding document supplies
``docs_url`` while ``SITE_DOCS_URL`` remains the higher-precedence operational
override.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from serving.config.branding import validate_public_https_base_url
from serving.utils.logging import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class SiteIdentity:
    """Operator identity rendered into backend-produced content."""

    name: str
    public_base_url: str
    docs_url: str
    support_email: str


# Step 3 of the migration: the upstream default names no distribution.
# A deployment supplies its own through SITE_* (deploy/docker/docker-compose.yml
# pins them, and .env still overrides), so a configured site renders unchanged.
#
# public_base_url and support_email are empty rather than invented: a
# deployment that has not declared them has none, and consumers phrase around
# the gap instead of printing a placeholder address.
NEUTRAL_DEFAULT = SiteIdentity(
    name="HybridInference",
    public_base_url="",
    docs_url="",
    support_email="",
)


def _pick(env_key: str, manifest_value: str | None, legacy: str) -> str:
    env_value = os.getenv(env_key, "").strip()
    if env_value:
        return env_value
    if manifest_value and manifest_value.strip():
        return manifest_value.strip()
    return legacy


def _pick_docs_url(manifest_value: str | None) -> str:
    """Resolve the docs URL without publishing an invalid env override."""
    env_value = os.getenv("SITE_DOCS_URL", "").strip()
    if env_value:
        try:
            return validate_public_https_base_url(env_value)
        except ValueError:
            logger.error(
                "Ignoring invalid SITE_DOCS_URL; expected an empty or path-joinable HTTPS URL"
            )

    candidate = manifest_value.strip() if manifest_value and manifest_value.strip() else ""
    # A manifest branding document has already passed this validator. Retain
    # the check here so mocked/custom config providers cannot publish a shape
    # the runtime console must reject.
    try:
        return validate_public_https_base_url(candidate)
    except ValueError:
        logger.error("Ignoring invalid manifest docs URL; expected a path-joinable HTTPS URL")
        return NEUTRAL_DEFAULT.docs_url


def get_site_identity() -> SiteIdentity:
    """Resolve the identity as env > active manifest > legacy defaults.

    Resolved per call (no cache): the values feed rendered content, not hot
    request paths, and per-call resolution keeps tests and env changes simple.
    """
    # Imported lazily to keep this module import-cycle-free and importable in
    # isolation (e.g. by offline tools).
    from serving.config.distribution import get_distribution_config
    from serving.config.settings import get_settings

    manifest_name: str | None = None
    manifest_base_url: str | None = None
    manifest_docs_url: str | None = None
    manifest_support: str | None = None
    if get_settings().distribution_config_mode.strip().lower() == "active":
        config = get_distribution_config()
        if config is not None:
            manifest_name = config.distribution.display_name
            manifest_base_url = config.site.public_base_url
            manifest_support = config.site.support_email
            branding = getattr(config, "branding_config", None)
            if branding is not None:
                manifest_docs_url = branding.links.docs_url

    return SiteIdentity(
        name=_pick("SITE_NAME", manifest_name, NEUTRAL_DEFAULT.name),
        public_base_url=_pick(
            "SITE_PUBLIC_BASE_URL", manifest_base_url, NEUTRAL_DEFAULT.public_base_url
        ),
        docs_url=_pick_docs_url(manifest_docs_url),
        support_email=_pick("SITE_SUPPORT_EMAIL", manifest_support, NEUTRAL_DEFAULT.support_email),
    )
