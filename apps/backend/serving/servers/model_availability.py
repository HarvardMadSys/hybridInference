"""Why a requested model cannot be served.

A model whose routes need a setting nobody configured is skipped when the
registry loads. A request for it still gets the 404 any unknown model gets.
When an administrator calls with their own credentials, the message says the
deployment's configuration is incomplete, so they fix it instead of hunting for
a typo in the model name. Everyone else gets the plain "not found": a skipped
model never reaches the role, visibility and grant gates, so telling it apart
from an unknown name would let any caller, a grant-scoped sandbox included,
list models the deployment defines beyond what it may reach.

Only the response carries the explanation. The request log keeps recording
``Model '<id>' not found``: the failed-request alerter recognizes a gateway 404
by that text and must keep ignoring it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from serving.config.settings import has_role
from serving.model_access import GRANT_ALLOWED_MODELS_KEY

if TYPE_CHECKING:
    from collections.abc import Mapping

    from serving.servers.registry import ModelLoadReport

# Model ids and aliases skipped at load for missing configuration.
_UNAVAILABLE: frozenset[str] = frozenset()


def record_skipped_models(report: ModelLoadReport) -> None:
    """Remember the models the registry skipped, under every name they answer to."""
    global _UNAVAILABLE
    names: set[str] = set()
    for model_id, aliases in report.skipped_models.items():
        names.add(model_id)
        names.update(aliases)
    _UNAVAILABLE = frozenset(names)


def reset() -> None:
    """Forget the skipped models (tests)."""
    global _UNAVAILABLE
    _UNAVAILABLE = frozenset()


def _may_explain(user_ctx: Mapping[str, Any] | None) -> bool:
    """Return whether the caller may learn that a model exists but was skipped.

    An administrator passes every role gate and can already read the missing
    settings, and the models that need them, on the Configuration tab. A grant
    names the only models its holder may know of, whoever's role it carries.
    """
    if not user_ctx or user_ctx.get(GRANT_ALLOWED_MODELS_KEY) is not None:
        return False
    return has_role(str(user_ctx.get("role") or "free"), "admin")


def model_not_found_detail(
    model: str,
    *other_names: str,
    user_ctx: Mapping[str, Any] | None,
    noun: str = "Model",
) -> str:
    """Return the 404 message for a model that cannot be served.

    Args:
        model: The model as the caller named it.
        *other_names: Further names that resolve to the same model, such as
            the canonical id behind an alias.
        user_ctx: The caller, as the auth dependency resolved it. Required, so
            that no call site can explain a skipped model to just anyone.
        noun: How to call it, e.g. ``"Embedding model"``.
    """
    if _may_explain(user_ctx) and any(name in _UNAVAILABLE for name in (model, *other_names)):
        return (
            f"{noun} '{model}' is unavailable because this deployment's configuration "
            "is incomplete. Contact the administrator."
        )
    return f"{noun} '{model}' not found"
