"""Why a requested model cannot be served.

A model whose routes need a setting nobody configured is skipped when the
registry loads. A request for it still gets the 404 any unknown model gets, but
the message says the deployment's configuration is incomplete, so the caller
asks the administrator instead of hunting for a typo in the model name.

Only the response carries the explanation. The request log keeps recording
``Model '<id>' not found``: the failed-request alerter recognizes a gateway 404
by that text and must keep ignoring it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
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


def model_not_found_detail(model: str, *other_names: str, noun: str = "Model") -> str:
    """Return the 404 message for a model that cannot be served.

    Args:
        model: The model as the caller named it.
        *other_names: Further names that resolve to the same model, such as
            the canonical id behind an alias.
        noun: How to call it, e.g. ``"Embedding model"``.
    """
    if any(name in _UNAVAILABLE for name in (model, *other_names)):
        return (
            f"{noun} '{model}' is unavailable because this deployment's configuration "
            "is incomplete. Contact the administrator."
        )
    return f"{noun} '{model}' not found"
