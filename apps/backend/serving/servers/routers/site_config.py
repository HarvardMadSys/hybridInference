"""Public site-config endpoint: distribution identity for frontends.

Serves distribution metadata, feature flags, and validated public branding
from the active manifest (``serving.config.distribution``), while public site
fields use the shared environment-over-manifest identity resolver. Local file
paths remain private; only the branding document's explicitly public fields
are returned.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from serving.auth.signup_policy import is_public_signup_enabled
from serving.config.distribution import get_distribution_config
from serving.config.settings import get_settings
from serving.config.site_identity import get_site_identity

router = APIRouter()

_NEUTRAL: dict[str, Any] = {
    "schema_version": 1,
    "distribution": {"id": "neutral", "display_name": "", "release": ""},
    "site": {"public_base_url": "", "support_email": ""},
    "features": {"routers": [], "public_signup": None, "rag": None},
    "branding": None,
}


@router.get("/site-config")
async def get_site_config() -> dict[str, Any]:
    """Return the active distribution and its resolved public site identity.

    Read-only and unauthenticated: this is the same information the public
    site renders. Falls back to a neutral document of identical shape when
    no distribution manifest is configured, so clients never need to
    special-case its absence. Public signup always reports the effective
    backend policy, including environment and runtime settings.
    """
    config = (
        get_distribution_config()
        if get_settings().distribution_config_mode.strip().lower() == "active"
        else None
    )
    public_signup = await is_public_signup_enabled()
    if config is None:
        return {
            **_NEUTRAL,
            "features": {**_NEUTRAL["features"], "public_signup": public_signup},
        }
    site_identity = get_site_identity()
    branding = config.branding_config
    return {
        "schema_version": config.schema_version,
        "distribution": config.distribution.model_dump(),
        "site": {
            "public_base_url": site_identity.public_base_url,
            "support_email": site_identity.support_email,
        },
        "features": {**config.features.model_dump(), "public_signup": public_signup},
        "branding": (
            branding.public_payload(docs_url=site_identity.docs_url)
            if branding is not None
            else None
        ),
    }
