"""Public site-config endpoint: distribution identity for frontends.

Serves distribution metadata and feature flags from the active manifest
(``serving.config.distribution``), while public site fields use the shared
environment-over-manifest identity resolver. Deliberately excludes the
manifest's local file paths (``terms_document``, ``branding`` etc.) — those
are server filesystem details; their *content* gets its own endpoints once
it moves into the overlay.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from serving.config.distribution import get_distribution_config
from serving.config.settings import get_settings
from serving.config.site_identity import get_site_identity

router = APIRouter()

_NEUTRAL: dict[str, Any] = {
    "distribution": {"id": "neutral", "display_name": "", "release": ""},
    "site": {"public_base_url": "", "support_email": ""},
    "features": {"routers": [], "public_signup": None, "rag": None},
}


@router.get("/site-config")
async def get_site_config() -> dict[str, Any]:
    """Return the active distribution and its resolved public site identity.

    Read-only and unauthenticated: this is the same information the public
    site renders. Falls back to a neutral document of identical shape when
    no distribution manifest is configured, so clients never need to
    special-case its absence.
    """
    if get_settings().distribution_config_mode.strip().lower() != "active":
        return _NEUTRAL
    config = get_distribution_config()
    if config is None:
        return _NEUTRAL
    site_identity = get_site_identity()
    return {
        "distribution": config.distribution.model_dump(),
        "site": {
            "public_base_url": site_identity.public_base_url,
            "support_email": site_identity.support_email,
        },
        "features": config.features.model_dump(),
    }
