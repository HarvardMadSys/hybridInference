"""Helpers for per-user model denylist normalization and checks."""

from __future__ import annotations

from typing import Any

DISABLED_MODELS_PREFERENCE_KEY = "disabled_models"


def normalize_disabled_models(value: Any) -> list[str]:
    """Return a sorted, deduplicated list of disabled canonical model ids."""
    if not isinstance(value, list):
        return []
    normalized = {item.strip() for item in value if isinstance(item, str) and item.strip()}
    return sorted(normalized)


def get_disabled_models_from_preferences(preferences: Any) -> list[str]:
    """Extract normalized disabled models from a preferences JSON object."""
    if not isinstance(preferences, dict):
        return []
    return normalize_disabled_models(preferences.get(DISABLED_MODELS_PREFERENCE_KEY))


def is_model_disabled_for_user(canonical_model_id: str, user_ctx: dict[str, Any] | None) -> bool:
    """Return True if the canonical model id is denied for the user context."""
    if not user_ctx:
        return False
    disabled_models = normalize_disabled_models(user_ctx.get(DISABLED_MODELS_PREFERENCE_KEY))
    return canonical_model_id in disabled_models


#: Set by the grant path on a request authenticated with an ``agr`` token; absent
#: on every other kind of caller. Absence and emptiness therefore mean opposite
#: things, and that asymmetry is the whole of the check below.
GRANT_ALLOWED_MODELS_KEY = "agent_allowed_models"


def is_model_outside_grant_scope(canonical_model_id: str, user_ctx: dict[str, Any] | None) -> bool:
    """Return True if a grant-authenticated caller may not call this model.

    **This is the only scope an inference grant carries.** It names one user,
    one attempt, a short lifetime and a model list; everything except that list
    is about *whether* the credential is live rather than *what* it may reach.
    An unenforced list leaves a leaked grant able to call anything the owner's
    role can, which is what the per-task budget used to bound before the design
    dropped it.

    Absent key means "not a grant" and denies nothing — an ordinary API key is
    governed by role and denylist as before. An **empty list** is a real grant
    that resolved to no models, and denies everything: the mint clamps a
    requested list to what the role can reach, and ``None`` there already means
    "all of them", so an empty list can only mean the intersection was empty.
    Reading it as "unrestricted" would invert the one case where the mint
    already decided the answer was nothing.
    """
    if not user_ctx:
        return False
    allowed = user_ctx.get(GRANT_ALLOWED_MODELS_KEY)
    if allowed is None:
        return False
    return canonical_model_id not in set(allowed)
