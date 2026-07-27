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
3. legacy FreeInference defaults, kept so output is byte-identical on
   deployments that configure nothing.

Three-step migration note: step 1 keeps the FreeInference values as code
defaults (this module); the deliberate neutral-defaults flip replaces
``LEGACY_DEFAULT`` and updates the frozen contract-test assertions, at which
point FreeInference supplies its identity through the overlay/env instead.
``docs_url`` has no manifest field yet; it joins the manifest schema with the
config-migration wave.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class SiteIdentity:
    """Operator identity rendered into backend-produced content."""

    name: str
    public_base_url: str
    docs_url: str
    support_email: str


# Three-step migration, step 1: FreeInference values remain the code default
# until the deliberate neutral-defaults flip (branding PR).
LEGACY_DEFAULT = SiteIdentity(
    name="FreeInference",
    public_base_url="https://freeinference.org",
    docs_url="https://doc.freeinference.org",
    support_email="admin@freeinference.org",
)


def _pick(env_key: str, manifest_value: str | None, legacy: str) -> str:
    env_value = os.getenv(env_key, "").strip()
    if env_value:
        return env_value
    if manifest_value and manifest_value.strip():
        return manifest_value.strip()
    return legacy


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
    manifest_support: str | None = None
    if get_settings().distribution_config_mode.strip().lower() == "active":
        config = get_distribution_config()
        if config is not None:
            manifest_name = config.distribution.display_name
            manifest_base_url = config.site.public_base_url
            manifest_support = config.site.support_email

    return SiteIdentity(
        name=_pick("SITE_NAME", manifest_name, LEGACY_DEFAULT.name),
        public_base_url=_pick(
            "SITE_PUBLIC_BASE_URL", manifest_base_url, LEGACY_DEFAULT.public_base_url
        ),
        docs_url=_pick("SITE_DOCS_URL", None, LEGACY_DEFAULT.docs_url),
        support_email=_pick("SITE_SUPPORT_EMAIL", manifest_support, LEGACY_DEFAULT.support_email),
    )
