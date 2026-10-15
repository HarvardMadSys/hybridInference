"""Auto mode on the inference surfaces: serving ``auto-*`` with a model chosen per request.

``routing.model_selection`` decides which candidate serves a request. Everything
around that decision that depends on the gateway is here:

- loading the ``auto:`` section of ``models.yaml`` at startup and holding the
  session store (:func:`load_auto_mode`, :class:`AutoMode`);
- the runtime switch, ``auto_mode_enabled``, off by default
  (:func:`auto_mode_for`);
- the catalog: the candidates this caller may use, as the route table declares
  them, and whether any endpoint is admitted for each;
- describing a request in protocol-neutral terms (:func:`openai_features`,
  :func:`anthropic_features`) and keying its session (:func:`session_key`);
- rewriting the request for the chosen model (:func:`apply_to_chat_request`,
  :func:`apply_to_anthropic_body`, :func:`strip_foreign_thinking`);
- keeping the chosen model from the caller (:func:`hide_served_model`,
  :class:`AnthropicStreamModelRenamer`).

Once the request names the chosen model it is served exactly as if the caller
had named that model. Its access is checked again, its own router picks the
endpoint, and the request is logged and billed under the model that served it,
with the decision in ``metadata.auto``.

The caller is not told which model that is. Responses name the auto model the
caller asked for, error messages do too, and the console's request history
shows the auto model. Operators see the served model in the log and the admin
views.

Design: ``docs/agents/specs/2026-10-13-auto-mode-model-selection-design.zh.md``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import yaml

from routing.model_selection import (
    AUTO_MODEL_ID,
    AUTO_MODEL_PREFIX,
    AutoConfig,
    AutoConfigError,
    Candidate,
    CandidateInfo,
    Decision,
    DropReason,
    RequestFeatures,
    SessionAction,
    SessionStore,
    TurnKind,
    check_against_catalog,
    drop_reason,
    is_auto_model_id,
    parse_auto_config,
    select,
)
from routing.prefill_load import estimate_prefill_tokens
from serving.model_access import model_access_denial, route_canonical_model_id
from serving.utils import context as req_ctx
from serving.utils.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Hashable, Mapping, Sequence
    from pathlib import Path

    from serving.adapters.base import ModelConfig
    from serving.utils.session_identity import SessionIdentity

logger = get_logger(__name__)

__all__ = [
    "AUTO_MODE_SETTING_KEY",
    "AnthropicStreamModelRenamer",
    "AutoMode",
    "AutoModelUnavailable",
    "AutoSelection",
    "anthropic_features",
    "apply_to_anthropic_body",
    "apply_to_chat_request",
    "auto_mode_for",
    "candidate_info",
    "hide_served_model",
    "load_auto_mode",
    "openai_features",
    "served_model_names",
    "session_key",
    "strip_foreign_thinking",
]

#: The runtime setting that turns auto mode on. Off by default: a deployment
#: with an ``auto:`` section serves the auto models only once an admin enables it.
AUTO_MODE_SETTING_KEY = "auto_mode_enabled"


def candidate_info(configs: Sequence[ModelConfig]) -> CandidateInfo:
    """Return what a model can serve whichever of its endpoints ``configs`` describe.

    The least of each limit and the capabilities every endpoint shares, the same
    conservative reading ``/v1/models`` gives a model with several routes: the
    endpoint that ends up serving the request is not known yet.
    """
    efforts = [
        frozenset(cfg.reasoning_efforts)
        if "reasoning_effort" in (cfg.supported_params or ())
        else frozenset()
        for cfg in configs
    ]
    return CandidateInfo(
        context_length=min(cfg.context_length for cfg in configs),
        max_output_length=min(cfg.max_output_length for cfg in configs),
        input_modalities=frozenset.intersection(
            *(frozenset(cfg.input_modalities or ["text"]) for cfg in configs)
        ),
        supports_tools=all(cfg.supports_tools for cfg in configs),
        reasoning_efforts=frozenset.intersection(*efforts),
    )


def _registered_catalog(routes: Mapping[str, Any]) -> dict[str, CandidateInfo]:
    """Every name the route table answers to, canonical or alias, and what it serves."""
    return {
        name: candidate_info([adapter.config for adapter, _ in route.adapters])
        for name, route in routes.items()
        if route.adapters
    }


class AutoModelUnavailable(Exception):
    """No candidate of the tier can serve the request.

    Attributes:
        status_code: 404 when the caller can use none of the tier's models (the
            answer an unknown model gets, so the tier's contents are not
            disclosed), 503 when one could serve but none of its endpoints is
            admitted, 400 when every model the caller can use is unable to serve
            this request.
        detail: The message for a 400 or a 503. A 404 is worded by the surface,
            as its unknown-model answer is.
    """

    def __init__(self, status_code: int, detail: str | None = None):
        """Record the status and, for a 400 or a 503, the message."""
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail or f"auto model unavailable ({status_code})")


@dataclass(frozen=True)
class _Reachable:
    """A candidate's model as this caller can reach it."""

    canonical: str
    info: CandidateInfo
    #: Whether an endpoint is admitted now (``eligible_adapters``).
    up: bool


@dataclass
class AutoSelection:
    """The model chosen for one request, and how to record the choice.

    Attributes:
        requested: The auto model the caller named.
        decision: What selection decided.
        candidate: The chosen candidate.
        model_id: The chosen model's canonical id; the request is rewritten to it.
        turn: Where the request falls in its session.
        prompt_tokens: The prompt estimate the context check used.
        session_source: ``declared`` when the client named its session,
            ``conversation`` when only the conversation's opening identifies it,
            ``None`` when nothing does and no state was kept.
        switches: How often the session's model has changed, this request
            included. Non-zero means the history may hold another model's output.
        served_names: Every name the chosen model goes by -- its id, aliases,
            display name and upstream model ids -- for keeping it out of what
            the caller is told.
        effort_applied: Whether the candidate's reasoning effort was written into
            the request. The caller's own setting is never overridden.
    """

    requested: str
    decision: Decision
    candidate: Candidate
    model_id: str
    turn: TurnKind
    prompt_tokens: int
    session_source: str | None
    switches: int
    served_names: frozenset[str] = frozenset()
    effort_applied: bool = False

    def log_metadata(self) -> dict[str, Any]:
        """Return the ``metadata.auto`` block for the request log."""
        decision = self.decision
        return {
            "requested": self.requested,
            "profile": decision.profile,
            "profile_version": decision.profile_version,
            "scorer_version": decision.scorer_version,
            "chosen": str(self.candidate),
            "ranked": [str(candidate) for candidate in decision.ranked],
            "dropped": {str(candidate): reason.value for candidate, reason in decision.dropped},
            "session_action": decision.session_action.value,
            "session_source": self.session_source,
            "switches": self.switches,
            "turn": self.turn.value,
            "prompt_tokens_estimate": self.prompt_tokens,
            "effort_applied": self.candidate.reasoning_effort if self.effort_applied else None,
        }


class AutoMode:
    """The loaded ``auto:`` section and the sessions it is keeping."""

    def __init__(self, config: AutoConfig, *, sessions: SessionStore | None = None):
        """Hold ``config``, with a session store sized by its session settings."""
        self.config = config
        self.sessions = sessions or SessionStore(
            idle_timeout_seconds=config.idle_timeout_seconds,
            max_sessions=config.max_sessions,
        )

    async def reachable(
        self,
        requested: str,
        router_exec: Any,
        user_ctx: dict[str, Any] | None,
        visibility_resolver: Any | None,
        *,
        grant_scope: bool = True,
    ) -> dict[str, _Reachable]:
        """Return the tier's models this caller may use, by the names candidates use.

        A model the caller could not use by name -- unregistered, unpublished,
        above its role, disabled by it, outside its grant -- is left out
        (``model_access_denial``). Only queries: admission is read through
        ``eligible_adapters``, which claims nothing.
        """
        profile = self.config.resolve(requested)
        if profile is None:
            return {}
        reachable: dict[str, _Reachable] = {}
        for model_id in dict.fromkeys(candidate.model_id for candidate in profile.candidates):
            route = router_exec.routes.get(model_id)
            if route is None or not route.adapters:
                continue
            denial = await model_access_denial(
                route,
                model_id,
                user_ctx,
                visibility_resolver=visibility_resolver,
                grant_scope=grant_scope,
            )
            if denial is not None:
                continue
            canonical = route_canonical_model_id(route, model_id)
            eligible = router_exec.eligible_adapters(canonical)
            entries = eligible or route.adapters
            reachable[model_id] = _Reachable(
                canonical=canonical,
                info=candidate_info([adapter.config for adapter, _ in entries]),
                up=bool(eligible),
            )
        return reachable

    async def select(
        self,
        requested: str,
        features: RequestFeatures,
        *,
        router_exec: Any,
        user_ctx: dict[str, Any] | None,
        visibility_resolver: Any | None,
        session: tuple[Hashable, str | None] | None,
    ) -> AutoSelection:
        """Choose the model for one request to ``requested`` and remember it for the session.

        Args:
            requested: The auto model the caller named; it must name a tier.
            features: The request.
            router_exec: The route table.
            user_ctx: The caller.
            visibility_resolver: Source of runtime visibility overrides.
            session: The session's key and its source, from :func:`session_key`,
                or ``None`` when the request belongs to no session.

        Raises:
            AutoModelUnavailable: No candidate can serve the request.
        """
        profile = self.config.resolve(requested)
        if profile is None:
            raise AutoModelUnavailable(404)
        reachable = await self.reachable(requested, router_exec, user_ctx, visibility_resolver)
        catalog = {model_id: entry.info for model_id, entry in reachable.items() if entry.up}
        key, source = session if session is not None else (None, None)
        state = self.sessions.get(key) if key is not None else None
        decision = select(profile, features, catalog, state)
        chosen = decision.chosen
        if chosen is None:
            raise _unavailable(requested, profile.candidates, reachable, features)
        if key is not None and decision.next_state is not None:
            self.sessions.put(key, decision.next_state)
        if decision.next_state is not None:
            switches = decision.next_state.switches
        else:
            switches = state.switches if state is not None else 0
        route = router_exec.routes[chosen.model_id]
        return AutoSelection(
            requested=requested,
            decision=decision,
            candidate=chosen,
            model_id=reachable[chosen.model_id].canonical,
            turn=features.turn,
            prompt_tokens=features.prompt_tokens,
            session_source=source if key is not None else None,
            switches=switches,
            served_names=served_model_names(
                [adapter.config for adapter, _ in route.adapters], chosen.model_id
            ),
        )


_CAPABILITY_REASONS = (DropReason.CONTEXT, DropReason.MODALITY, DropReason.TOOLS)


def _unavailable(
    requested: str,
    candidates: Sequence[Candidate],
    reachable: Mapping[str, _Reachable],
    features: RequestFeatures,
) -> AutoModelUnavailable:
    """Say why nothing could serve, in terms that do not name the tier's models."""
    reasons: list[DropReason] = []
    for candidate in candidates:
        entry = reachable.get(candidate.model_id)
        if entry is None:
            continue
        reason = drop_reason(candidate, entry.info, features)
        if reason is None:
            # Able to serve, so it was left out only for having no endpoint admitted.
            return AutoModelUnavailable(
                503, f"No provider is currently available for model '{requested}'"
            )
        reasons.append(reason)
    for reason in reasons:
        if reason is DropReason.CONTEXT:
            return AutoModelUnavailable(
                400, f"The request is too long for every model '{requested}' can use"
            )
        if reason is DropReason.MODALITY:
            kinds = ", ".join(sorted(features.input_modalities - {"text"})) or "this"
            return AutoModelUnavailable(
                400, f"No model '{requested}' can use accepts {kinds} input"
            )
        if reason is DropReason.TOOLS:
            return AutoModelUnavailable(400, f"No model '{requested}' can use supports tools")
    return AutoModelUnavailable(404)


async def auto_mode_enabled() -> bool:
    """Return whether the runtime switch is on, reading the registry default if it cannot be read."""
    from serving.config.runtime_settings import (
        RUNTIME_SETTINGS_REGISTRY,
        get_runtime_settings_instance,
    )

    try:
        return await get_runtime_settings_instance().get_bool(AUTO_MODE_SETTING_KEY)
    except Exception:
        return bool(RUNTIME_SETTINGS_REGISTRY[AUTO_MODE_SETTING_KEY]["default"])


async def auto_mode_for(model_id: Any, services: Any) -> AutoMode | None:
    """Return the auto mode that serves ``model_id``, or ``None`` to serve it as named.

    Only a name in the auto namespace that a configured tier answers to reads the
    runtime switch, so a request naming a real model costs one string comparison.
    """
    if not isinstance(model_id, str) or not is_auto_model_id(model_id):
        return None
    auto_mode = getattr(services, "auto_mode", None)
    if auto_mode is None or auto_mode.config.resolve(model_id) is None:
        return None
    if not await auto_mode_enabled():
        return None
    return auto_mode


def load_auto_mode(path: Path, routes: Mapping[str, Any]) -> AutoMode | None:
    """Load the ``auto:`` section of the models registry at ``path``.

    Called once the route table is complete, so the section is checked against
    every model the gateway starts with. Returns ``None`` -- auto off, the models
    themselves unaffected -- when the file has no section, the section is
    invalid, or a tier is named like a registered model. Each problem is logged.
    """
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except (OSError, yaml.YAMLError) as exc:
        logger.error(f"Auto mode is off: could not read {path}: {exc}")
        return None
    raw = data.get("auto") if isinstance(data, dict) else None
    if raw is None:
        return None
    try:
        config = parse_auto_config(raw)
    except AutoConfigError as exc:
        for problem in exc.problems:
            logger.error(f"Auto mode is off: {problem}")
        return None
    check = check_against_catalog(config, _registered_catalog(routes))
    for warning in check.warnings:
        logger.warning(f"Auto mode: {warning}")
    if check.conflicts:
        for conflict in check.conflicts:
            logger.error(f"Auto mode is off: {conflict}")
        return None
    logger.info(
        f"Auto mode configured: {sorted(config.virtual_model_ids)}, "
        f"{AUTO_MODEL_ID!r} = {AUTO_MODEL_PREFIX}{config.default_profile}. "
        f"Served while the runtime setting {AUTO_MODE_SETTING_KEY} is on."
    )
    return AutoMode(config)


def session_key(
    user_ctx: dict[str, Any] | None,
    declared: SessionIdentity | None,
    opening_fingerprint: str | None,
) -> tuple[Hashable, str] | None:
    """Return the key a request's session state is stored under, and its source.

    The key is the caller, the session the client declared, and the opening of
    the conversation (``conversation_fingerprint`` of the messages alone):

    - The caller, because a session id is the client's own claim; without it
      one caller could steer another's session.
    - The opening, because one declared session carries several conversations.
      Claude Code sends its subagents and its title and safety calls under the
      session of the main conversation. Keyed by the session alone, a subagent
      starting would move the main conversation's model in the middle of its
      tool loop.
    - With no declared session, the opening alone identifies the conversation,
      as OpenRouter does.

    Returns ``None`` when neither is available: the request is ranked on its own
    and nothing is stored.
    """
    if declared is None and opening_fingerprint is None:
        return None
    ctx = user_ctx or {}
    caller = (ctx.get("user_id") or "anonymous", ctx.get("agent_grant_id"))
    key = (
        *caller,
        declared.session_id if declared is not None else None,
        opening_fingerprint,
    )
    return key, ("declared" if declared is not None else "conversation")


def openai_features(
    messages: Sequence[dict[str, Any]],
    *,
    tools: Any,
    response_format: Any,
    max_tokens: int | None,
    required_modalities: frozenset[str],
) -> RequestFeatures:
    """Describe a Chat Completions request.

    A conversation whose last message is a tool result is in the model's tool
    loop; anything else is a new turn.
    """
    last = messages[-1] if messages else None
    in_tool_loop = isinstance(last, dict) and last.get("role") == "tool"
    return RequestFeatures(
        prompt_tokens=estimate_prefill_tokens(
            messages, tools=tools, response_format=response_format
        ),
        turn=TurnKind.TOOL_CONTINUATION if in_tool_loop else TurnKind.NEW_TURN,
        max_tokens=max_tokens,
        input_modalities=frozenset({"text"}) | required_modalities,
        uses_tools=bool(tools),
    )


def _anthropic_blocks(messages: Any) -> list[tuple[str, dict[str, Any]]]:
    """Return ``(role, block)`` for every content block, a tool result's own blocks included."""
    found: list[tuple[str, dict[str, Any]]] = []
    for message in messages if isinstance(messages, list) else ():
        if not isinstance(message, dict) or not isinstance(message.get("content"), list):
            continue
        role = message.get("role")
        for block in message["content"]:
            if not isinstance(block, dict):
                continue
            found.append((role, block))
            if block.get("type") == "tool_result" and isinstance(block.get("content"), list):
                found.extend((role, inner) for inner in block["content"] if isinstance(inner, dict))
    return found


def anthropic_features(
    body: dict[str, Any], *, prompt_tokens: int, side_call: bool
) -> RequestFeatures:
    """Describe an Anthropic Messages request.

    The request is in the tool loop when its last message is a user message
    carrying a tool result. Claude Code adds its reminders to that message as
    text blocks, and a user who interrupts a tool sends the interrupted result
    with their text; both read as the loop going on. That errs toward keeping
    the session's model, which at worst defers a switch to the next turn, and
    never toward moving it in the middle of a loop.

    Images count wherever they are, tool results included: that is how a
    coding agent reads a screenshot.
    """
    messages = body.get("messages")
    last = messages[-1] if isinstance(messages, list) and messages else None
    in_tool_loop = (
        isinstance(last, dict)
        and last.get("role") == "user"
        and any(block.get("type") == "tool_result" for _, block in _anthropic_blocks([last]))
    )
    if side_call:
        turn = TurnKind.SIDE_CALL
    elif in_tool_loop:
        turn = TurnKind.TOOL_CONTINUATION
    else:
        turn = TurnKind.NEW_TURN
    has_image = any(block.get("type") == "image" for _, block in _anthropic_blocks(messages))
    max_tokens = body.get("max_tokens")
    return RequestFeatures(
        prompt_tokens=prompt_tokens,
        turn=turn,
        max_tokens=max_tokens if isinstance(max_tokens, int) else None,
        input_modalities=frozenset({"text", "image"} if has_image else {"text"}),
        uses_tools=bool(body.get("tools")),
    )


def apply_to_chat_request(selection: AutoSelection, payload: Any) -> None:
    """Rewrite a parsed Chat Completions request for the chosen model.

    The model goes on the payload as well as the handler's own variable: the
    request's routing record, and from it the price lookup, read the payload.
    """
    payload.model = selection.model_id
    effort = selection.candidate.reasoning_effort
    if effort is not None and payload.reasoning_effort is None:
        payload.reasoning_effort = effort
        selection.effort_applied = True


def apply_to_anthropic_body(selection: AutoSelection, body: dict[str, Any]) -> None:
    """Rewrite an Anthropic Messages body for the chosen model.

    The candidate's effort is written as ``output_config.effort``, the field
    Claude Code itself uses, so it takes the same path and the same per-model
    check as an effort the client chose (``_apply_reasoning_effort``).
    """
    body["model"] = selection.model_id
    effort = selection.candidate.reasoning_effort
    output_config = body.get("output_config")
    client_effort = body.get("reasoning_effort") is not None or (
        isinstance(output_config, dict) and output_config.get("effort") is not None
    )
    if effort is None or client_effort:
        return
    if output_config is None:
        body["output_config"] = {"effort": effort}
    elif isinstance(output_config, dict):
        output_config["effort"] = effort
    else:
        return
    selection.effort_applied = True


def served_model_names(configs: Sequence[ModelConfig], *extra: str) -> frozenset[str]:
    """Return every name a model goes by, from its route's adapter configs.

    Its id, aliases and display name, and the model id each upstream knows it
    by, which an upstream's own error message may quote. Names shorter than
    three characters are left out: replacing them would mangle unrelated text.
    """
    names: set[str] = set(extra)
    for cfg in configs:
        names.update((cfg.id, cfg.name, cfg.provider_model_id or "", *(cfg.aliases or ())))
    return frozenset(name for name in names if len(name) >= 3)


def hide_served_model(selection: AutoSelection, *also: ModelConfig) -> None:
    """Keep the model serving this request out of every error message the caller gets.

    Publishes the served model's names for ``serving.exceptions.conceal_served_model``,
    which every user-facing error text passes through. ``also`` adds the configs
    of a model the request moved on to after selection, such as the small-call
    reroute's target.
    """
    current = req_ctx.get().get(req_ctx.CONCEALED_MODEL)
    names = set(current[1]) if current else set(selection.served_names)
    names |= served_model_names(also)
    req_ctx.update({req_ctx.CONCEALED_MODEL: (selection.requested, frozenset(names))})


class AnthropicStreamModelRenamer:
    """Rename the model in an Anthropic event stream's ``message_start``.

    The stream is forwarded as the upstream sends it, and its ``message_start``
    event names the model that serves the request. This holds back the bytes up
    to the end of that event, rewrites its ``message.model``, and passes every
    later byte through untouched. Only the opening of the stream is ever held.
    """

    #: Give up and pass the stream through if no message_start ends this early.
    _MAX_HELD = 65536

    def __init__(self, model: str):
        """Rename to ``model``."""
        self._model = model
        self._held = b""
        self._done = False

    def feed(self, data: bytes) -> bytes:
        """Return the bytes of ``data`` that can be sent now."""
        if self._done:
            return data
        self._held += data
        out = b""
        while not self._done:
            end = _event_end(self._held)
            if end is None:
                if len(self._held) > self._MAX_HELD:
                    self._done = True
                break
            event, self._held = self._held[:end], self._held[end:]
            renamed = self._rename(event)
            if renamed is not None:
                event = renamed
                self._done = True
            out += event
        if self._done:
            out, self._held = out + self._held, b""
        return out

    def flush(self) -> bytes:
        """Return whatever is still held, as it came; the stream has ended."""
        held, self._held, self._done = self._held, b"", True
        return held

    def _rename(self, event: bytes) -> bytes | None:
        """Return ``event`` with its model renamed, or ``None`` if it is not message_start."""
        lines = event.split(b"\n")
        for index, line in enumerate(lines):
            if not line.startswith(b"data:"):
                continue
            payload = line[5:].strip()
            try:
                data = json.loads(payload)
            except ValueError:
                return None
            if not isinstance(data, dict) or data.get("type") != "message_start":
                return None
            message = data.get("message")
            if not isinstance(message, dict):
                return None
            message["model"] = self._model
            lines[index] = (
                b"data: " + json.dumps(data).encode() + (b"\r" if line.endswith(b"\r") else b"")
            )
            return b"\n".join(lines)
        return None


def _event_end(buffer: bytes) -> int | None:
    """Return the index just past the first complete SSE event in ``buffer``, if any."""
    ends = [
        index + len(delimiter)
        for delimiter in (b"\n\n", b"\r\n\r\n")
        if (index := buffer.find(delimiter)) >= 0
    ]
    return min(ends) if ends else None


_THINKING_BLOCK_TYPES = ("thinking", "redacted_thinking")


def strip_foreign_thinking(selection: AutoSelection, body: dict[str, Any]) -> int:
    """Remove thinking another model may have written from an Anthropic body.

    Only for a body forwarded as it is to an Anthropic-native upstream; the
    OpenAI translator drops thinking blocks on its own. Such an upstream checks
    a thinking block's signature, and one written by another model -- or by the
    translator, which signs nothing -- is not its own. Applies once the session
    has changed models: until then every block in the history is the session
    model's.

    The last assistant message keeps its thinking while the session stays on its
    model in a tool loop: it is the current model's, and an upstream with
    thinking enabled requires it there.

    Returns the number of blocks removed. An assistant message left empty is
    dropped; the upstream joins the user messages either side of it.
    """
    if selection.switches == 0:
        return 0
    messages = body.get("messages")
    if not isinstance(messages, list):
        return 0
    keep_index = None
    if (
        selection.turn is TurnKind.TOOL_CONTINUATION
        and selection.decision.session_action is SessionAction.KEEP
    ):
        keep_index = max(
            (
                index
                for index, message in enumerate(messages)
                if isinstance(message, dict) and message.get("role") == "assistant"
            ),
            default=None,
        )
    removed = 0
    kept: list[Any] = []
    for index, message in enumerate(messages):
        if (
            index == keep_index
            or not isinstance(message, dict)
            or message.get("role") != "assistant"
            or not isinstance(message.get("content"), list)
        ):
            kept.append(message)
            continue
        content = message["content"]
        remaining = [
            block
            for block in content
            if not (isinstance(block, dict) and block.get("type") in _THINKING_BLOCK_TYPES)
        ]
        removed += len(content) - len(remaining)
        if remaining:
            kept.append({**message, "content": remaining})
    if removed:
        body["messages"] = kept
    return removed
