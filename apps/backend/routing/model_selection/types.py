"""The values auto model selection reads and returns.

Everything here is protocol-neutral. The serving layer turns a Chat Completions
or Anthropic Messages request into :class:`RequestFeatures`, and the route table
into :class:`CandidateInfo`; selection never sees a request body or an adapter.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass

__all__ = [
    "Candidate",
    "CandidateInfo",
    "Decision",
    "DropReason",
    "RequestFeatures",
    "SessionAction",
    "SessionState",
    "TurnKind",
]


@dataclass(frozen=True, slots=True)
class Candidate:
    """One choice a tier offers: a model, and optionally the effort to run it at.

    The same model at two efforts is two candidates. They differ in cost and
    quality as much as two models do, so a tier lists and ranks them separately.

    Attributes:
        model_id: The name the model is registered under.
        reasoning_effort: The effort to request when the caller names none, or
            ``None`` to leave the request's own setting alone.
    """

    model_id: str
    reasoning_effort: str | None = None

    def __str__(self) -> str:
        """Return ``model`` or ``model@effort``, the form logs record."""
        if self.reasoning_effort is None:
            return self.model_id
        return f"{self.model_id}@{self.reasoning_effort}"


@dataclass(frozen=True, slots=True)
class CandidateInfo:
    """What one model can serve, as the route table declares it.

    Attributes:
        context_length: The context window, in tokens.
        max_output_length: The output limit, in tokens. Adapters clamp a larger
            ``max_tokens`` to it, so it caps the output a request reserves room
            for rather than ruling the model out.
        input_modalities: Input kinds the model accepts (``text``, ``image``...).
        supports_tools: Whether the model is sent tool definitions. Adapters drop
            a request's tools for a model without it, which an agent cannot
            survive.
        reasoning_efforts: The efforts the model accepts. Empty when the model
            does not take ``reasoning_effort`` at all.
    """

    context_length: int
    max_output_length: int
    input_modalities: frozenset[str] = frozenset({"text"})
    supports_tools: bool = False
    reasoning_efforts: frozenset[str] = frozenset()


class TurnKind(enum.Enum):
    """Where a request falls in its session, which decides whether it may switch models."""

    #: The user wrote something. Selection may move the session to a better candidate.
    NEW_TURN = "new_turn"
    #: The model's own tool loop: the last user message carries only tool results.
    #: The session's model keeps going unless it can no longer serve.
    TOOL_CONTINUATION = "tool_continuation"
    #: A call that rides on the session without being one of its turns, such as
    #: Claude Code's tool-permission check. It uses the session's model, so it is
    #: never answered by a weaker one, and it leaves the session as it was.
    SIDE_CALL = "side_call"


@dataclass(frozen=True, slots=True)
class RequestFeatures:
    """What selection needs to know about one request.

    Attributes:
        prompt_tokens: Estimated prompt size, as
            ``routing.prefill_load.estimate_prefill_tokens`` counts it.
        turn: Where the request falls in its session.
        max_tokens: The output limit the caller asked for, if any.
        input_modalities: Input kinds the request carries.
        uses_tools: Whether the request sends tool definitions.
    """

    prompt_tokens: int
    turn: TurnKind
    max_tokens: int | None = None
    input_modalities: frozenset[str] = frozenset({"text"})
    uses_tools: bool = False


class DropReason(enum.Enum):
    """Why a candidate cannot serve a request."""

    #: Not in the catalog: unregistered, closed to the caller, or no endpoint
    #: admitted. The serving layer knows which; selection does not need to.
    UNAVAILABLE = "unavailable"
    #: The model does not accept the candidate's reasoning effort.
    EFFORT = "effort"
    #: The request sends tools and the model is not given them.
    TOOLS = "tools"
    #: The request carries an input kind the model does not accept.
    MODALITY = "modality"
    #: The prompt and the output it reserves do not fit the context window.
    CONTEXT = "context"


class SessionAction(enum.Enum):
    """What selection did with the session's model."""

    #: No usable session state: the request was ranked from scratch.
    NEW = "new"
    #: The session stayed on its model.
    KEEP = "keep"
    #: A new user turn moved the session to a better-ranked candidate.
    TURN_SWITCH = "turn_switch"
    #: The session's model could not serve this request, so it moved.
    FORCED_SWITCH = "forced_switch"
    #: A side call: answered without changing the session.
    SIDE_CALL = "side_call"


@dataclass(frozen=True, slots=True)
class SessionState:
    """What a session has settled on.

    Attributes:
        candidate: The candidate serving the session.
        profile_version: The version of the tier that chose it. A request for
            another tier, or for this tier after its configuration changed, does
            not inherit it.
        turns: User turns the session has had.
        switches: Times the session's candidate has changed.
    """

    candidate: Candidate
    profile_version: str
    turns: int = 0
    switches: int = 0


@dataclass(frozen=True, slots=True)
class Decision:
    """The outcome of selecting for one request.

    Attributes:
        profile: The tier's name.
        ranked: The candidates able to serve, first choice first. Empty when none
            can, in which case ``dropped`` says why for each.
        dropped: The tier's other candidates, each with why it cannot serve.
        session_action: What happened to the session's model.
        next_state: The session state to store, or ``None`` to leave the stored
            state alone (a side call, or no candidate at all).
        profile_version: The tier's version.
        scorer_version: The version of the scorer that ranked the candidates.
    """

    profile: str
    ranked: tuple[Candidate, ...]
    dropped: tuple[tuple[Candidate, DropReason], ...]
    session_action: SessionAction
    next_state: SessionState | None
    profile_version: str
    scorer_version: str

    @property
    def chosen(self) -> Candidate | None:
        """Return the first choice, or ``None`` when no candidate can serve."""
        return self.ranked[0] if self.ranked else None
