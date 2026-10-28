"""The four batch targets: what each accepts, and the model it runs on.

A target is the inference surface a batch item runs through: ``chat``
(``/v1/chat/completions``), ``embeddings`` (``/v1/embeddings``), ``messages``
(the Anthropic Messages API) and ``responses`` (``/v1/responses``). This module
decides, before anything is stored, whether an item's body is one the target
can run *as written*, and pins the canonical model it runs on (an alias is
resolved now, so a later remapping cannot make the item run something else).
Execution is :mod:`serving.batch.dispatch`.

The rule throughout: a parameter is either honoured or refused. Nothing is
dropped on the way to a provider without the caller knowing:

- ``chat`` and ``embeddings`` accept what the interactive endpoints accept.
- ``messages`` accepts a fixed set of parameters, content blocks and tool kinds
  (:data:`_NATIVE_TOP_LEVEL`, :data:`_NATIVE_BLOCKS`): server tools, Files API
  sources, containers, MCP servers and betas are refused until each is
  accepted on its own. A route served through an OpenAI-compatible adapter only
  translates a subset (:func:`openai_translation_problem`); a request outside
  it needs a native Anthropic adapter on the route. The same rule filters the
  candidates when the item runs (:class:`AdapterRequirement`): a fallback that
  could not serve the item faithfully is never used, and the item fails rather
  than degrades when no compatible candidate is left.
- ``responses`` is restricted to independent, non-streaming, unstored
  requests: ``store`` must be omitted or false, and ``previous_response_id``,
  ``conversation``, ``background`` and ``stream`` are refused, as are hosted
  tools the translation cannot carry.

Auto models are refused for every target: a batch names a specific model.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from routing.model_selection import AUTO_MODEL_ID, AUTO_MODEL_PREFIX, is_auto_model_id
from serving.adapters.anthropic_aliases import resolve_anthropic_alias
from serving.auto_mode import auto_mode_for
from serving.batch.profiles import AnthropicProfile, profile_violation
from serving.model_access import model_access_denial, route_canonical_model_id
from serving.responses_translator import responses_request_to_chat_params
from serving.schemas import ChatCompletionRequest, EmbeddingRequest
from serving.servers.model_availability import model_not_found_detail
from serving.storage.batch_store import (
    TARGET_CHAT,
    TARGET_EMBEDDINGS,
    TARGET_MESSAGES,
    TARGET_RESPONSES,
)

if TYPE_CHECKING:
    from collections.abc import Callable

#: The ``url`` (inline) or ``endpoint`` (file) that names each target of
#: ``/v1/batches``. ``/v1/messages`` is this gateway's extension of the OpenAI
#: batch shape; a Messages batch also names its ``anthropic-version`` header.
TARGET_URLS: dict[str, str] = {
    "/v1/chat/completions": TARGET_CHAT,
    "/v1/embeddings": TARGET_EMBEDDINGS,
    "/v1/messages": TARGET_MESSAGES,
    "/v1/responses": TARGET_RESPONSES,
}
#: The endpoint each target is logged and described under.
TARGET_PATHS: dict[str, str] = {
    TARGET_CHAT: "/v1/chat/completions",
    TARGET_EMBEDDINGS: "/v1/embeddings",
    TARGET_RESPONSES: "/v1/responses",
    TARGET_MESSAGES: "/v1/messages",
}

# -- Anthropic Messages capabilities ---------------------------------------------

#: Top-level parameters a Messages batch item may carry on a native route.
_NATIVE_TOP_LEVEL = frozenset(
    {
        "model",
        "messages",
        "max_tokens",
        "system",
        "temperature",
        "top_p",
        "top_k",
        "stop_sequences",
        "stream",
        "tools",
        "tool_choice",
        "metadata",
        "thinking",
        "output_config",
        "service_tier",
    }
)
#: Content block types accepted in messages on a native route.
_NATIVE_BLOCKS = frozenset(
    {
        "text",
        "image",
        "document",
        "tool_use",
        "tool_result",
        "thinking",
        "redacted_thinking",
        "search_result",
    }
)
#: Top-level parameters an OpenAI-compatible route translates.
_TRANSLATED_TOP_LEVEL = frozenset(
    {
        "model",
        "messages",
        "max_tokens",
        "system",
        "temperature",
        "top_p",
        "stop_sequences",
        "stream",
        "tools",
        "tool_choice",
        "metadata",
        "output_config",
    }
)
_TRANSLATED_BLOCKS = frozenset({"text", "image", "tool_use", "tool_result"})


def _label(value: Any) -> str:
    """A field value as an error message names it: a string quoted, anything else by its type."""
    if isinstance(value, str):
        return repr(value[:80])
    return f"of JSON type {type(value).__name__}"


def _responses_tools_problem(tools: Any) -> str | None:
    """Why Responses ``tools`` would not be carried as written, or None.

    The translation skips anything that is not a function tool object, so
    every entry must be one.
    """
    if tools is None:
        return None
    if not isinstance(tools, list):
        return "tools: must be an array"
    for index, tool in enumerate(tools):
        if not isinstance(tool, dict):
            return f"tools.{index}: must be an object"
        kind = tool.get("type", "function")
        if kind != "function":
            return (
                f"tools.{index}: tool type {_label(kind)} is not supported in batches; "
                "only function tools are"
            )
        if "function" not in tool and not isinstance(tool.get("name"), str):
            return f"tools.{index}.name: a function tool needs a name"
    return None


class ItemRejected(Exception):
    """An item that cannot be admitted, with the error its result reports."""

    def __init__(self, code: str, message: str, *, status: int = 400, param: str | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.param = param

    def as_error(self) -> dict[str, Any]:
        """The stored error object (``{"code", "message", "status"}``)."""
        return {"code": self.code, "message": self.message, "status": self.status}


@dataclass
class AdmissionContext:
    """What admitting items needs: the caller, the route table, the services."""

    user_ctx: dict[str, Any]
    routes: Any
    services: Any
    visibility_resolver: Any
    embedding_adapters: dict[str, Any] | None
    profile: AnthropicProfile | None = None
    #: The router's embedding access gates (``FixedRouter.embedding_routes``).
    #: An embedding model with no gate here is refused, as /v1/embeddings does.
    embedding_routes: dict[str, Any] | None = None
    _models: dict[tuple[str, str], str] = field(default_factory=dict)


def _tools_problem(tools: Any, *, translated: bool) -> str | None:
    if tools is None:
        return None
    if not isinstance(tools, list):
        return "tools must be an array"
    for index, tool in enumerate(tools):
        if not isinstance(tool, dict):
            return f"tools.{index} must be an object"
        kind = tool.get("type")
        if kind is not None and kind != "custom":
            return (
                f"tools.{index}: tool type {_label(kind)} is not supported in batches; "
                "only client tools (name, description, input_schema) are"
            )
        if translated and ("cache_control" in tool or "strict" in tool):
            return f"tools.{index}: cache_control and strict need a native Anthropic route"
    return None


def _blocks_problem(
    content: Any, where: str, *, allowed: frozenset[str], translated: bool
) -> str | None:
    if isinstance(content, str) or content is None:
        return None
    if not isinstance(content, list):
        return f"{where}.content must be a string or an array"
    for index, block in enumerate(content):
        if not isinstance(block, dict):
            return f"{where}.content.{index} must be an object"
        kind = block.get("type")
        if not isinstance(kind, str) or kind not in allowed:
            return f"{where}.content.{index}: content block type {_label(kind)} is not supported"
        source = block.get("source")
        if isinstance(source, dict) and source.get("type") == "file":
            return f"{where}.content.{index}: Files API sources are not supported in batches"
        if translated:
            if "cache_control" in block:
                return f"{where}.content.{index}: cache_control needs a native Anthropic route"
            if block.get("citations"):
                return f"{where}.content.{index}: citations need a native Anthropic route"
            if kind == "tool_result" and isinstance(block.get("content"), list):
                for inner_index, inner in enumerate(block["content"]):
                    if not isinstance(inner, dict) or inner.get("type") != "text":
                        return (
                            f"{where}.content.{index}: a tool_result with non-text content "
                            "needs a native Anthropic route"
                        )
                    if "cache_control" in inner or inner.get("citations"):
                        return (
                            f"{where}.content.{index}.content.{inner_index}: cache_control "
                            "and citations need a native Anthropic route"
                        )
            if kind == "image" and not isinstance(source, dict):
                return f"{where}.content.{index}: image source is missing"
        elif kind == "tool_result" and isinstance(block.get("content"), list):
            nested = _blocks_problem(
                block["content"], f"{where}.content.{index}", allowed=allowed, translated=False
            )
            if nested:
                return nested
    return None


def native_messages_problem(body: dict[str, Any]) -> str | None:
    """Why *body* is outside what batch Messages items accept at all, or None.

    Every top-level key outside the accepted set is refused rather than
    ignored -- ``anthropic_version`` and ``betas`` included: the version is the
    batch's own (its ``anthropic-version`` header), and betas are not supported in batches.
    """
    unknown = sorted(set(body) - _NATIVE_TOP_LEVEL)
    if unknown:
        return f"{', '.join(unknown)}: not supported in Messages batch requests"
    if body.get("stream"):
        return "stream: must not be true in a batch"
    problem = _tools_problem(body.get("tools"), translated=False)
    if problem:
        return problem
    system = body.get("system")
    if isinstance(system, list):
        problem = _blocks_problem(system, "system", allowed=frozenset({"text"}), translated=False)
        if problem:
            return problem
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        return "messages: must be a non-empty array"
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            return f"messages.{index} must be an object"
        if message.get("role") not in ("user", "assistant"):
            return f"messages.{index}.role must be 'user' or 'assistant'"
        problem = _blocks_problem(
            message.get("content"), f"messages.{index}", allowed=_NATIVE_BLOCKS, translated=False
        )
        if problem:
            return problem
    max_tokens = body.get("max_tokens")
    if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens < 1:
        return "max_tokens: must be a positive integer"
    metadata = body.get("metadata")
    if metadata is not None and (not isinstance(metadata, dict) or set(metadata) - {"user_id"}):
        return "metadata: only user_id is accepted"
    return None


def openai_translation_problem(body: dict[str, Any], adapter: Any = None) -> str | None:
    """Why an OpenAI-compatible adapter could not carry *body* faithfully, or None.

    ``adapter``, when given, decides ``output_config.effort``: the effort must
    be one the adapter's model declares.
    """
    unknown = sorted(set(body) - _TRANSLATED_TOP_LEVEL)
    if unknown:
        return f"{', '.join(unknown)}: needs a native Anthropic route"
    problem = _tools_problem(body.get("tools"), translated=True)
    if problem:
        return problem
    choice = body.get("tool_choice")
    if choice is not None:
        if not isinstance(choice, dict) or choice.get("type") not in ("auto", "any", "tool"):
            return "tool_choice: only auto, any and tool are translated"
        if set(choice) - {"type", "name"}:
            return "tool_choice: disable_parallel_tool_use needs a native Anthropic route"
    system = body.get("system")
    if isinstance(system, list):
        problem = _blocks_problem(system, "system", allowed=frozenset({"text"}), translated=True)
        if problem:
            return problem
    for index, message in enumerate(body.get("messages") or []):
        if isinstance(message, dict):
            problem = _blocks_problem(
                message.get("content"),
                f"messages.{index}",
                allowed=_TRANSLATED_BLOCKS,
                translated=True,
            )
            if problem:
                return problem
    output_config = body.get("output_config")
    if output_config is not None:
        if not isinstance(output_config, dict) or set(output_config) - {"effort"}:
            return "output_config: only effort is translated; format needs a native route"
        effort = output_config.get("effort")
        if adapter is not None and effort is not None:
            config = getattr(adapter, "config", None)
            supported = getattr(config, "supported_params", None) or ()
            declared = getattr(config, "reasoning_efforts", None) or ()
            if "reasoning_effort" not in supported or effort not in declared:
                return f"output_config.effort {effort!r} is not supported by this model"
    return None


def adapter_problem(adapter: Any, body: dict[str, Any]) -> str | None:
    """Why *adapter* could not serve a Messages batch item faithfully, or None.

    The wire format is not enough: an OpenAI-compatible adapter whose model
    does not take tools (``supports_tools``) drops them, so an item with tools
    needs one that does.
    """
    if getattr(adapter, "native_format", None) == "openai":
        problem = openai_translation_problem(body, adapter)
        if problem:
            return problem
        config = getattr(adapter, "config", None)
        if body.get("tools") and not getattr(config, "supports_tools", True):
            return "tools: this model's route does not take tools"
    return None


@dataclass(frozen=True)
class AdapterRequirement:
    """The adapters that may serve one Messages batch item (published on req_ctx).

    Read by the Anthropic surface when it chooses among a route's candidates;
    see ``serving.servers.routers.anthropic_messages._pick_dispatch_adapter``.
    """

    body: dict[str, Any]

    def problem(self, adapter: Any) -> str | None:
        """Why *adapter* cannot serve the item, or None."""
        return adapter_problem(adapter, self.body)


def _route_adapters(route: Any) -> list[Any]:
    return [entry[0] for entry in getattr(route, "adapters", None) or []]


async def _admit_route_model(
    model: Any,
    ctx: AdmissionContext,
    *,
    target: str,
    alias_resolver: Callable[[str], str] | None = None,
) -> tuple[str, Any]:
    """Return ``(canonical id, route)`` of a chat-route model the caller may batch."""
    if not isinstance(model, str) or not model:
        raise ItemRejected("invalid_request", "model: a model name is required", param="model")
    if is_auto_model_id(model) and (
        model not in ctx.routes or await auto_mode_for(model, ctx.services) is not None
    ):
        raise ItemRejected(
            "invalid_request",
            f"auto models ({AUTO_MODEL_ID!r}, '{AUTO_MODEL_PREFIX}<tier>') cannot be used in a "
            "batch; name a specific model",
            param="model",
        )
    lookup = alias_resolver(model) if alias_resolver is not None else model
    route = ctx.routes.get(lookup)
    if (
        route is None
        or not _route_adapters(route)
        or (
            await model_access_denial(
                route, lookup, ctx.user_ctx, visibility_resolver=ctx.visibility_resolver
            )
            is not None
        )
    ):
        # Every reason the caller may not use a model reads as an unknown model,
        # as on the interactive surfaces.
        raise ItemRejected(
            "model_not_found",
            model_not_found_detail(model, user_ctx=ctx.user_ctx),
            status=404,
            param="model",
        )
    return route_canonical_model_id(route, lookup), route


@dataclass(frozen=True)
class ItemProblem:
    """Why an item was refused, as a value: the stored error and the caller's message."""

    code: str
    message: str
    status: int
    param: str | None

    def as_error(self) -> dict[str, Any]:
        """The stored error object (``{"code", "message", "status"}``)."""
        return {"code": self.code, "message": self.message, "status": self.status}


async def check_item(
    target: str, body: Any, ctx: AdmissionContext
) -> tuple[str | None, ItemProblem | None]:
    """:func:`admit_item` as a value: ``(canonical model, None)`` or ``(None, problem)``."""
    try:
        return await admit_item(target, body, ctx), None
    except ItemRejected as rejected:
        return None, ItemProblem(rejected.code, rejected.message, rejected.status, rejected.param)


async def admit_item(target: str, body: Any, ctx: AdmissionContext) -> str:
    """Check an item's body for *target*; return the canonical model it runs on.

    Raises:
        ItemRejected: The item cannot run as written.
    """
    if not isinstance(body, dict):
        raise ItemRejected("invalid_request", "body must be a JSON object")
    if target == TARGET_CHAT:
        return await _admit_chat(body, ctx)
    if target == TARGET_EMBEDDINGS:
        return await _admit_embeddings(body, ctx)
    if target == TARGET_MESSAGES:
        return await _admit_messages(body, ctx)
    if target == TARGET_RESPONSES:
        return await _admit_responses(body, ctx)
    raise ItemRejected("invalid_request", f"unsupported batch target {target!r}")


async def _cached_route_model(
    model: Any, ctx: AdmissionContext, target: str, resolver: Callable[[str], str] | None = None
) -> tuple[str, Any]:
    key = (target, model if isinstance(model, str) else repr(model))
    canonical = ctx._models.get(key)
    if canonical is not None:
        route = ctx.routes.get(resolver(model) if resolver else model)
        return canonical, route
    canonical, route = await _admit_route_model(model, ctx, target=target, alias_resolver=resolver)
    ctx._models[key] = canonical
    return canonical, route


def _describe(exc: ValidationError) -> str:
    errors = exc.errors(include_url=False, include_input=False, include_context=False)
    first = errors[0]
    where = ".".join(str(part) for part in first.get("loc", ()))
    text = f"{where}: {first['msg']}" if where else str(first["msg"])
    if len(errors) > 1:
        text += f" (and {len(errors) - 1} more)"
    return text


async def _admit_chat(body: dict[str, Any], ctx: AdmissionContext) -> str:
    try:
        chat = ChatCompletionRequest.model_validate(body)
    except ValidationError as exc:
        raise ItemRejected(
            "invalid_request", f"body is not a valid chat completion request: {_describe(exc)}"
        ) from None
    if chat.stream:
        raise ItemRejected("invalid_request", "stream must not be true in a batch", param="stream")
    canonical, _route = await _cached_route_model(chat.model, ctx, TARGET_CHAT)
    return canonical


async def _admit_embeddings(body: dict[str, Any], ctx: AdmissionContext) -> str:
    try:
        request = EmbeddingRequest.model_validate(body)
    except ValidationError as exc:
        raise ItemRejected(
            "invalid_request", f"body is not a valid embeddings request: {_describe(exc)}"
        ) from None
    adapters = ctx.embedding_adapters or {}
    adapter = adapters.get(request.model)
    gate = (ctx.embedding_routes or {}).get(request.model)
    if (
        adapter is None
        # The gate is mandatory, as on /v1/embeddings: an adapter without one
        # is refused, never admitted ungated.
        or gate is None
        or (
            await model_access_denial(
                gate, request.model, ctx.user_ctx, visibility_resolver=ctx.visibility_resolver
            )
            is not None
        )
    ):
        # Every reason reads as an unknown model, as on the interactive surface.
        raise ItemRejected(
            "model_not_found",
            model_not_found_detail(request.model, user_ctx=ctx.user_ctx, noun="Embedding model"),
            status=404,
            param="model",
        )
    # The embedding adapter is looked up by the name as given, as the
    # interactive endpoint does; its configured id is what the cap keys on.
    return getattr(getattr(adapter, "config", None), "id", None) or request.model


async def _admit_messages(body: dict[str, Any], ctx: AdmissionContext) -> str:
    for required in ("model", "messages", "max_tokens"):
        if required not in body:
            raise ItemRejected("invalid_request", f"{required}: Field required", param=required)
    problem = native_messages_problem(body)
    if problem:
        raise ItemRejected("invalid_request", problem)
    if ctx.profile is None:
        # The batch's anthropic-version decides the rules below; without one a
        # request would be admitted under none of them.
        raise ItemRejected("invalid_request", "anthropic-version: a Messages batch needs one")
    violation = profile_violation(ctx.profile, body)
    if violation:
        raise ItemRejected("invalid_request", violation)
    canonical, route = await _cached_route_model(
        body["model"], ctx, TARGET_MESSAGES, resolve_anthropic_alias
    )
    problems = [adapter_problem(adapter, body) for adapter in _route_adapters(route)]
    if all(problems):
        raise ItemRejected(
            "invalid_request",
            f"model {body['model']!r} cannot serve this request as written: {problems[0]}",
        )
    return canonical


#: Responses parameters a batch item may carry (anything else is refused).
_RESPONSES_KEYS = frozenset(
    {
        "model",
        "input",
        "instructions",
        "max_output_tokens",
        "temperature",
        "top_p",
        "stop",
        "seed",
        "frequency_penalty",
        "presence_penalty",
        "reasoning",
        "text",
        "tools",
        "tool_choice",
        "metadata",
        "store",
        "stream",
        "background",
        "parallel_tool_calls",
        "user",
    }
)


#: Responses input item types the translation carries (anything else is dropped there).
_RESPONSES_ITEMS = frozenset({None, "message", "function_call", "function_call_output"})
#: Content part types the translation carries.
_RESPONSES_PARTS = frozenset(
    {"input_text", "output_text", "text", "summary_text", "input_image", "image_url", "image"}
)


def _responses_input_problem(value: Any) -> str | None:
    """Why a Responses ``input`` holds something the translation would drop, or None."""
    if value is None or isinstance(value, str):
        return None
    if not isinstance(value, list):
        return "input: must be a string or an array of input items"
    for index, item in enumerate(value):
        if isinstance(item, str):
            continue
        if not isinstance(item, dict):
            return f"input.{index}: must be an object"
        kind = item.get("type")
        if (kind is not None and not isinstance(kind, str)) or kind not in _RESPONSES_ITEMS:
            return f"input.{index}: input item type {_label(kind)} is not supported in batches"
        content = item.get("content")
        if kind in (None, "message") and isinstance(content, list):
            for part_index, part in enumerate(content):
                if isinstance(part, str):
                    continue
                if not isinstance(part, dict):
                    return f"input.{index}.content.{part_index}: must be an object"
                part_type = part.get("type")
                if not isinstance(part_type, str) or part_type not in _RESPONSES_PARTS:
                    return (
                        f"input.{index}.content.{part_index}: content type {_label(part_type)} "
                        "is not supported in batches"
                    )
                if part_type in ("input_image", "image_url", "image") and not isinstance(
                    part.get("image_url"), (str, dict)
                ):
                    return (
                        f"input.{index}.content.{part_index}: images must be given by image_url "
                        "(file references are not supported in batches)"
                    )
        elif kind in (None, "message") and content is not None and not isinstance(content, str):
            return f"input.{index}.content: must be a string or an array"
        if kind == "function_call_output" and not isinstance(item.get("output"), str):
            # An object would be sent as its JSON text and a file part dropped
            # (serving.responses_translator); only text is carried as written.
            return f"input.{index}.output: only text tool outputs are supported in batches"
    return None


async def _admit_responses(body: dict[str, Any], ctx: AdmissionContext) -> str:
    for refused in ("previous_response_id", "conversation"):
        if body.get(refused) is not None:
            raise ItemRejected(
                "invalid_request",
                f"{refused}: batch Responses requests are independent; chaining is not supported",
                param=refused,
            )
    unknown = sorted(set(body) - _RESPONSES_KEYS)
    if unknown:
        raise ItemRejected(
            "invalid_request", f"{', '.join(unknown)}: not supported in batch Responses requests"
        )
    if body.get("store") is True:
        raise ItemRejected(
            "invalid_request",
            "store: batch Responses requests are not stored; omit store or set it to false",
            param="store",
        )
    if body.get("stream"):
        raise ItemRejected("invalid_request", "stream must not be true in a batch", param="stream")
    if body.get("background"):
        raise ItemRejected(
            "invalid_request", "background is not supported in a batch", param="background"
        )
    if body.get("parallel_tool_calls") is False:
        raise ItemRejected(
            "invalid_request",
            "parallel_tool_calls: false is not supported in batch Responses requests",
            param="parallel_tool_calls",
        )
    if "input" not in body:
        raise ItemRejected("invalid_request", "input: Field required", param="input")
    problem = _responses_input_problem(body.get("input"))
    if problem:
        raise ItemRejected("invalid_request", problem, param="input")
    text = body.get("text")
    if text is not None:
        fmt = text.get("format") if isinstance(text, dict) else None
        if (
            not isinstance(text, dict)
            or set(text) - {"format"}
            or (fmt is not None and not isinstance(fmt, dict))
            or (
                isinstance(fmt, dict)
                and fmt.get("type") not in ("text", "json_object", "json_schema")
            )
        ):
            raise ItemRejected(
                "invalid_request",
                "text: only format (text, json_object or json_schema) is supported in batches",
                param="text",
            )
    reasoning = body.get("reasoning")
    if reasoning is not None and (not isinstance(reasoning, dict) or set(reasoning) - {"effort"}):
        raise ItemRejected(
            "invalid_request", "reasoning: only effort is supported", param="reasoning"
        )
    problem = _responses_tools_problem(body.get("tools"))
    if problem:
        raise ItemRejected("invalid_request", problem, param="tools")
    _params, dropped = responses_request_to_chat_params(body)
    if dropped:
        raise ItemRejected(
            "invalid_request",
            f"tools: hosted tool types {sorted(set(dropped))} are not supported in batches",
            param="tools",
        )
    canonical, _route = await _cached_route_model(body.get("model"), ctx, TARGET_RESPONSES)
    return canonical
