"""Public site-config endpoint: distribution identity for frontends.

Serves distribution metadata, feature flags, and validated public branding
from the active manifest (``serving.config.distribution``), while public site
fields use the shared environment-over-manifest identity resolver. Local file
paths remain private; only the branding document's explicitly public fields
are returned.

Two deployment-state flags ride along at the top level, where an older
console's parser ignores them: ``setup.required`` (first-run setup is pending,
so the console sends every route to ``/setup``) and
``configuration.incomplete`` (a required setting has no value). Neither names
a setting.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from serving.auth.signup_policy import is_public_signup_enabled
from serving.config.app_config import get_config_health
from serving.config.distribution import DistributionConfigError, get_active_distribution_config
from serving.config.settings import get_settings
from serving.config.site_identity import get_site_identity
from serving.setup_state import refresh_setup_state
from serving.utils.sms import is_sms_enabled

router = APIRouter()

_NEUTRAL: dict[str, Any] = {
    "schema_version": 1,
    "distribution": {"id": "neutral", "display_name": "", "release": ""},
    "site": {"public_base_url": "", "support_email": ""},
    "features": {"routers": [], "public_signup": None, "rag": None},
    "branding": None,
}


async def _deployment_state() -> dict[str, Any]:
    """Return the top-level ``setup`` and ``configuration`` flags."""
    return {
        "setup": {"required": await refresh_setup_state()},
        "configuration": {"incomplete": get_config_health().incomplete},
    }


# Feature flags that are off unless a deployment turns them on, each newer than
# some console still in use. Consoles read a missing one as off.
_OPT_IN_FEATURES = frozenset({"public_stats", "phone_auth"})


def _public_features(features: dict[str, Any]) -> dict[str, Any]:
    """Drop each opt-in flag (``public_stats``, ``phone_auth``) that is not enabled.

    Consoles parse ``features`` strictly, so an older console would reject the
    whole document, and with it every page, over a key it does not know.
    Omitting an opt-in flag while it is off keeps such a console working; it
    only ever sees the key on a deployment that turned the feature on, which
    needs a console that has the feature anyway.
    """
    return {k: v for k, v in features.items() if k not in _OPT_IN_FEATURES or v is True}


@router.get("/site-config")
async def get_site_config() -> dict[str, Any]:
    """Return the active distribution and its resolved public site identity.

    Read-only and unauthenticated: this is the same information the public
    site renders. Falls back to a neutral document of identical shape when
    no distribution manifest is configured, so clients never need to
    special-case its absence. Public signup always reports the effective
    backend policy, including environment and runtime settings. ``setup``
    and ``configuration`` are always present, manifest or not.
    """
    try:
        config = get_active_distribution_config()
    except DistributionConfigError:
        raise HTTPException(
            status_code=503,
            detail="Site configuration is unavailable. Check the distribution manifest and mode.",
        ) from None
    public_signup = await is_public_signup_enabled()
    deployment_state = await _deployment_state()
    phone_auth_enabled = is_sms_enabled()
    phone_policy = (
        {"phone_auth_policy": {"default_country_code": get_settings().sms_default_country_code}}
        if phone_auth_enabled
        else {}
    )
    if config is None:
        return {
            **_NEUTRAL,
            "features": _public_features(
                {
                    **_NEUTRAL["features"],
                    "public_signup": public_signup,
                    "phone_auth": phone_auth_enabled,
                }
            ),
            **deployment_state,
            **phone_policy,
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
        "features": _public_features(
            {
                **config.features.model_dump(),
                "public_signup": public_signup,
                "phone_auth": phone_auth_enabled,
            }
        ),
        "branding": (
            branding.public_payload(docs_url=site_identity.docs_url)
            if branding is not None
            else None
        ),
        **deployment_state,
        **phone_policy,
    }
