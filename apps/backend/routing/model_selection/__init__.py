"""Auto model selection: serving a virtual ``auto-*`` model with a real one.

A tier (``auto-intelligence``, ``auto-balance``, ``auto-fast``...) lists candidate
models. For each request, selection leaves out the candidates that cannot serve
it, ranks the rest, and keeps a session on one model except at a safe boundary.
The chosen model is then served exactly as a request naming it would be: its own
router picks the endpoint, and the caller's access to it is checked as usual.

This package only decides. It reads no request and performs no I/O; the serving
layer extracts :class:`RequestFeatures`, builds the catalog, stores session
state and rewrites the request. Design:
``docs/agents/specs/2026-10-13-auto-mode-model-selection-design.zh.md``; plan:
``docs/agents/plans/2026-10-13-auto-mode-implementation.zh.md``.
"""

from routing.model_selection.config import (
    AUTO_MODEL_ID,
    AUTO_MODEL_PREFIX,
    AutoConfig,
    AutoConfigError,
    AutoProfile,
    CatalogCheck,
    check_against_catalog,
    parse_auto_config,
)
from routing.model_selection.constraints import PROMPT_ESTIMATE_HEADROOM, drop_reason
from routing.model_selection.scoring import Estimate, MaxQuality, Objective, Scorer, StaticScorer
from routing.model_selection.selector import select
from routing.model_selection.session import SessionStore, continue_session
from routing.model_selection.types import (
    Candidate,
    CandidateInfo,
    Decision,
    DropReason,
    RequestFeatures,
    SessionAction,
    SessionState,
    TurnKind,
)

__all__ = [
    "AUTO_MODEL_ID",
    "AUTO_MODEL_PREFIX",
    "PROMPT_ESTIMATE_HEADROOM",
    "AutoConfig",
    "AutoConfigError",
    "AutoProfile",
    "Candidate",
    "CandidateInfo",
    "CatalogCheck",
    "Decision",
    "DropReason",
    "Estimate",
    "MaxQuality",
    "Objective",
    "RequestFeatures",
    "Scorer",
    "SessionAction",
    "SessionState",
    "SessionStore",
    "StaticScorer",
    "TurnKind",
    "check_against_catalog",
    "continue_session",
    "drop_reason",
    "parse_auto_config",
    "select",
]
