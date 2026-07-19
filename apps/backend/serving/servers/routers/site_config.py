"""Public site-config endpoint: distribution identity for frontends.

Serves the safe subset of the active distribution manifest
(serving.config.distribution). Deliberately excludes the manifest's local
file paths (``terms_document``, ``branding`` etc.) — those are server
filesystem details; their *content* gets its own endpoints once it moves
into the overlay.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from serving.config.distribution import get_distribution_config
from serving.config.settings import get_settings

router = APIRouter()

_NEUTRAL: dict[str, Any] = {
    "distribution": {"id": "neutral", "display_name": "", "release": ""},
    "site": {"public_base_url": "", "support_email": "", "description": ""},
    "features": {"routers": [], "public_signup": None, "rag": None},
}


@router.get("/site-config")
async def get_site_config() -> dict[str, Any]:
    """Return the active distribution's site identity.

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
    return {
        "distribution": config.distribution.model_dump(),
        "site": {
            "public_base_url": config.site.public_base_url,
            "support_email": config.site.support_email,
            "description": config.site.description,
        },
        "features": config.features.model_dump(),
    }
