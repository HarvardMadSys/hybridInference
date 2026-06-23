"""Translation between the OpenAI Responses API and Chat Completions.

The Responses API (``POST /v1/responses``) is a re-shaping of Chat
Completions: a different request/response envelope over the same underlying
model call. Rather than re-implement routing, cost accounting, logging and
metrics, the northbound router (``serving/servers/routers/responses.py``)
translates a Responses request into a Chat Completions request, dispatches it
through the existing ``/v1/chat/completions`` handler, and translates the
result back into a Responses object. This module owns that translation.

It is pure (no I/O, no logging). Three concerns:

1. **Request → chat** — :func:`responses_input_to_messages` turns the
   Responses ``input`` (+ ``instructions``) into OpenAI chat messages and
   :func:`responses_request_to_chat_params` extracts the sampling / tool /
   structured-output params.
2. **Chat → response (non-streaming)** — :func:`chat_response_to_responses`
   builds the Responses object; :func:`assistant_message_from_chat` extracts
   the assistant turn for conversation persistence.
3. **Chat → response (streaming)** — :class:`ResponsesStreamTranslator`
   consumes OpenAI SSE chunks and emits Responses-API SSE events.
"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator

__all__ = [
    "ResponsesStreamTranslator",
    "assistant_message_from_chat",
    "chat_response_to_responses",
    "new_response_id",
    "responses_input_to_messages",
    "responses_request_to_chat_params",
]


# --- ids -------------------------------------------------------------------


def new_response_id() -> str:
    """Return a fresh Responses object id (``resp_...``)."""
    import uuid

    return f"resp_{uuid.uuid4().hex}"


def _msg_item_id() -> str:
    import uuid

    return f"msg_{uuid.uuid4().hex}"


def _fc_item_id() -> str:
    import uuid

    return f"fc_{uuid.uuid4().hex}"


# --- request translation ---------------------------------------------------


def _content_parts_to_text_or_blocks(content: Any) -> Any:
    """Translate Responses content (string or part list) to chat content.

    A bare string passes through. A list of typed parts maps to OpenAI chat
    content blocks: ``input_text`` / ``output_text`` → ``text``;
    ``input_image`` → ``image_url``. Unknown part types are dropped.

    When the result is a single text block it is collapsed back to a plain
    string so the common text-only case yields ``content: "..."`` exactly as a
    hand-written chat request would.
    """
    if content is None:
        return None
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return content

    blocks: list[dict[str, Any]] = []
    for part in content:
        if isinstance(part, str):
            blocks.append({"type": "text", "text": part})
            continue
        if not isinstance(part, dict):
            continue
        ptype = part.get("type")
        if ptype in ("input_text", "output_text", "text", "summary_text"):
            blocks.append({"type": "text", "text": part.get("text", "")})
        elif ptype in ("input_image", "image_url", "image"):
            # Responses uses {type:input_image, image_url:"<url>"|{url}}; chat
            # uses {type:image_url, image_url:{url, detail?}}.
            image_url = part.get("image_url")
            detail = part.get("detail")
            if isinstance(image_url, str):
                url_obj: dict[str, Any] = {"url": image_url}
            elif isinstance(image_url, dict):
                url_obj = dict(image_url)
            else:
                continue
            if detail and "detail" not in url_obj:
                url_obj["detail"] = detail
            blocks.append({"type": "image_url", "image_url": url_obj})
        # input_file / other part types have no chat-completions equivalent.

    if not blocks:
        return ""
    if len(blocks) == 1 and blocks[0].get("type") == "text":
        return blocks[0]["text"]
    return blocks


def _function_call_output_to_text(output: Any) -> str:
    """Coerce a Responses ``function_call_output.output`` into chat tool text."""
    if isinstance(output, str):
        return output
    return json.dumps(output)


def responses_input_to_messages(
    input_value: Any, *, instructions: str | None = None
) -> list[dict[str, Any]]:
    """Translate the Responses ``input`` (+ ``instructions``) to chat messages.

    ``input`` may be a plain string (single user turn) or a list of input
    items. Supported item types:

    - ``message`` (or a bare ``{role, content}``) → a chat message, with
      content parts translated by :func:`_content_parts_to_text_or_blocks`.
    - ``function_call`` → merged into the preceding assistant message's
      ``tool_calls`` (a new assistant message is started if none is open).
    - ``function_call_output`` → a ``role: "tool"`` message keyed by
      ``call_id``.

    ``instructions``, when provided, is prepended as a ``system`` message.
    Reasoning items and other unknown item types are skipped.

    Prior-turn context (``previous_response_id``) is **not** handled here; the
    router composes stored conversation messages around this result.
    """
    messages: list[dict[str, Any]] = []
    if instructions:
        messages.append({"role": "system", "content": instructions})

    if input_value is None:
        return messages

    if isinstance(input_value, str):
        messages.append({"role": "user", "content": input_value})
        return messages

    if not isinstance(input_value, list):
        # Be permissive: stringify anything unexpected as a user turn.
        messages.append({"role": "user", "content": str(input_value)})
        return messages

    for item in input_value:
        if not isinstance(item, dict):
            if isinstance(item, str):
                messages.append({"role": "user", "content": item})
            continue
        itype = item.get("type")

        if itype in (None, "message"):
            role = item.get("role", "user")
            # The Responses API allows a "developer" role (high-priority app
            # instructions). The chat-completions schema only knows
            # system/user/assistant/tool, so fold developer → system.
            if role == "developer":
                role = "system"
            content = _content_parts_to_text_or_blocks(item.get("content"))
            msg: dict[str, Any] = {"role": role, "content": content}
            messages.append(msg)

        elif itype == "function_call":
            tool_call = {
                "id": item.get("call_id") or item.get("id") or "",
                "type": "function",
                "function": {
                    "name": item.get("name", ""),
                    "arguments": item.get("arguments", "") or "",
                },
            }
            # Attach to a trailing assistant message with no content, or open one.
            if (
                messages
                and messages[-1].get("role") == "assistant"
                and not messages[-1].get("content")
            ):
                messages[-1].setdefault("tool_calls", []).append(tool_call)
            else:
                messages.append({"role": "assistant", "content": None, "tool_calls": [tool_call]})

        elif itype == "function_call_output":
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": item.get("call_id") or item.get("id") or "",
                    "content": _function_call_output_to_text(item.get("output")),
                }
            )
        # reasoning / item_reference / other types: skipped (no chat analogue).

    return messages


def _convert_tools(tools: Any) -> tuple[list[dict[str, Any]], list[str]]:
    """Translate Responses tools to chat-completions tools.

    Responses function tools are flat (``{type:"function", name, description,
    parameters}``); chat tools nest the schema under ``function``. Built-in
    hosted tools (``web_search``, ``file_search``, ``code_interpreter``, ...)
    have no chat-completions equivalent and are dropped.

    Returns ``(chat_tools, dropped_tool_types)``.
    """
    if not isinstance(tools, list):
        return [], []
    chat_tools: list[dict[str, Any]] = []
    dropped: list[str] = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        ttype = tool.get("type", "function")
        if ttype == "function":
            # Already-nested chat shape passes through unchanged.
            if "function" in tool and "name" not in tool:
                chat_tools.append(tool)
                continue
            fn: dict[str, Any] = {"name": tool.get("name", "")}
            if tool.get("description") is not None:
                fn["description"] = tool["description"]
            if tool.get("parameters") is not None:
                fn["parameters"] = tool["parameters"]
            if tool.get("strict") is not None:
                fn["strict"] = tool["strict"]
            chat_tools.append({"type": "function", "function": fn})
        else:
            dropped.append(ttype)
    return chat_tools, dropped


def _convert_tool_choice(tool_choice: Any) -> Any:
    """Translate a Responses ``tool_choice`` to the chat-completions form."""
    if isinstance(tool_choice, str):
        return tool_choice  # "auto" | "none" | "required"
    if isinstance(tool_choice, dict):
        if tool_choice.get("type") == "function":
            func = tool_choice.get("function")
            name = tool_choice.get("name") or (func.get("name") if isinstance(func, dict) else None)
            if name:
                return {"type": "function", "function": {"name": name}}
        return tool_choice
    return tool_choice


def _convert_text_format(text: Any) -> dict[str, Any] | None:
    """Translate Responses ``text.format`` to a chat ``response_format``.

    ``{type:"json_object"}`` passes through. ``{type:"json_schema", name,
    schema, strict}`` (flat, Responses shape) becomes the nested chat shape
    ``{type:"json_schema", json_schema:{name, schema, strict}}``.
    """
    if not isinstance(text, dict):
        return None
    fmt = text.get("format")
    if not isinstance(fmt, dict):
        return None
    ftype = fmt.get("type")
    if ftype == "text":
        return None
    if ftype == "json_object":
        return {"type": "json_object"}
    if ftype == "json_schema":
        json_schema: dict[str, Any] = {}
        if fmt.get("name") is not None:
            json_schema["name"] = fmt["name"]
        if fmt.get("schema") is not None:
            json_schema["schema"] = fmt["schema"]
        if fmt.get("strict") is not None:
            json_schema["strict"] = fmt["strict"]
        if fmt.get("description") is not None:
            json_schema["description"] = fmt["description"]
        return {"type": "json_schema", "json_schema": json_schema}
    return None


def responses_request_to_chat_params(body: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Extract chat-completions params from a Responses request body.

    Returns ``(params, dropped)`` where ``params`` is a dict that can be merged
    into a chat request body alongside ``model`` and ``messages`` (it never
    contains either of those), and ``dropped`` lists unsupported hosted-tool
    types for best-effort warning logging.

    Field mapping: ``max_output_tokens`` → ``max_tokens``; ``reasoning.effort``
    → ``reasoning_effort``; ``text.format`` → ``response_format``; ``tools`` /
    ``tool_choice`` translated to chat shape. ``temperature``, ``top_p``,
    ``stop``, ``seed``, ``frequency_penalty``, ``presence_penalty``,
    ``parallel_tool_calls`` and ``stream`` pass through when present.
    """
    params: dict[str, Any] = {}
    dropped: list[str] = []

    if body.get("max_output_tokens") is not None:
        params["max_tokens"] = body["max_output_tokens"]
    # ``parallel_tool_calls`` is intentionally NOT forwarded: the chat-completions
    # request schema does not model it, so it would be silently dropped before
    # reaching any provider. It is still echoed in the Responses object.
    for key in (
        "temperature",
        "top_p",
        "stop",
        "seed",
        "frequency_penalty",
        "presence_penalty",
        "stream",
    ):
        if body.get(key) is not None:
            params[key] = body[key]

    reasoning = body.get("reasoning")
    if isinstance(reasoning, dict) and reasoning.get("effort"):
        params["reasoning_effort"] = reasoning["effort"]

    response_format = _convert_text_format(body.get("text"))
    if response_format is not None:
        params["response_format"] = response_format

    chat_tools, dropped_tools = _convert_tools(body.get("tools"))
    if chat_tools:
        params["tools"] = chat_tools
    dropped.extend(dropped_tools)

    if body.get("tool_choice") is not None:
        params["tool_choice"] = _convert_tool_choice(body["tool_choice"])

    return params, dropped


# --- response translation (non-streaming) ----------------------------------


def _map_status(finish_reason: str | None) -> tuple[str, dict[str, Any] | None]:
    """Map a chat ``finish_reason`` to a Responses ``(status, incomplete)``."""
    if finish_reason == "length":
        return "incomplete", {"reason": "max_output_tokens"}
    if finish_reason == "content_filter":
        return "incomplete", {"reason": "content_filter"}
    # "stop", "tool_calls", "function_call", None → completed.
    return "completed", None


def _map_usage(chat_usage: dict[str, Any] | None) -> dict[str, Any] | None:
    """Translate chat ``usage`` to the Responses usage shape."""
    if not chat_usage:
        return None
    input_tokens = int(chat_usage.get("prompt_tokens", 0) or 0)
    output_tokens = int(chat_usage.get("completion_tokens", 0) or 0)
    total = int(chat_usage.get("total_tokens", input_tokens + output_tokens) or 0)
    # Accept both the gateway-normalised flat keys and the raw OpenAI nested
    # shapes (prompt_tokens_details.cached_tokens /
    # completion_tokens_details.reasoning_tokens) so cache/reasoning tokens are
    # not under-reported for providers that emit the nested form.
    prompt_details = chat_usage.get("prompt_tokens_details") or {}
    completion_details = chat_usage.get("completion_tokens_details") or {}
    cached = int(chat_usage.get("cache_read_tokens") or prompt_details.get("cached_tokens") or 0)
    reasoning = int(
        chat_usage.get("reasoning_tokens") or completion_details.get("reasoning_tokens") or 0
    )
    return {
        "input_tokens": input_tokens,
        "input_tokens_details": {"cached_tokens": cached},
        "output_tokens": output_tokens,
        "output_tokens_details": {"reasoning_tokens": reasoning},
        "total_tokens": total,
    }


def _output_items_from_message(
    message: dict[str, Any],
) -> list[dict[str, Any]]:
    """Build Responses ``output`` items from an assistant chat message.

    A non-empty ``content`` yields a ``message`` item with an ``output_text``
    content part; ``tool_calls`` each yield a ``function_call`` item.
    """
    output: list[dict[str, Any]] = []
    content = message.get("content")
    if isinstance(content, str) and content:
        output.append(
            {
                "type": "message",
                "id": _msg_item_id(),
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": content, "annotations": []}],
            }
        )
    for tc in message.get("tool_calls") or []:
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") or {}
        output.append(
            {
                "type": "function_call",
                "id": _fc_item_id(),
                "call_id": tc.get("id", ""),
                "name": fn.get("name", ""),
                "arguments": fn.get("arguments", "") or "",
                "status": "completed",
            }
        )
    return output


def assistant_message_from_chat(chat: dict[str, Any]) -> dict[str, Any]:
    """Extract the assistant chat message (content + tool_calls) for storage.

    Used to append the model's turn onto the persisted conversation so a
    follow-up request via ``previous_response_id`` can replay it.
    """
    choices = chat.get("choices") or []
    message = (choices[0].get("message") if choices else None) or {}
    out: dict[str, Any] = {"role": "assistant", "content": message.get("content")}
    if message.get("tool_calls"):
        out["tool_calls"] = message["tool_calls"]
    return out


def _echo_request_fields(target: dict[str, Any], request_body: dict[str, Any]) -> None:
    """Copy request-echo fields onto a Responses object, mirroring OpenAI."""
    target["instructions"] = request_body.get("instructions")
    target["max_output_tokens"] = request_body.get("max_output_tokens")
    target["temperature"] = request_body.get("temperature")
    target["top_p"] = request_body.get("top_p")
    target["tools"] = request_body.get("tools") or []
    target["tool_choice"] = request_body.get("tool_choice", "auto")
    target["parallel_tool_calls"] = request_body.get("parallel_tool_calls", True)
    target["text"] = request_body.get("text") or {"format": {"type": "text"}}
    target["reasoning"] = request_body.get("reasoning") or {"effort": None, "summary": None}
    target["metadata"] = request_body.get("metadata") or {}
    target["truncation"] = request_body.get("truncation", "disabled")


def chat_response_to_responses(
    chat: dict[str, Any],
    *,
    response_id: str,
    created_at: int,
    model: str,
    request_body: dict[str, Any],
    previous_response_id: str | None = None,
    store: bool = True,
) -> dict[str, Any]:
    """Translate a ``chat.completion`` dict into a Responses object dict."""
    choices = chat.get("choices") or []
    choice = choices[0] if choices else {}
    message = choice.get("message") or {}
    finish_reason = choice.get("finish_reason")
    status, incomplete = _map_status(finish_reason)

    response: dict[str, Any] = {
        "id": response_id,
        "object": "response",
        "created_at": created_at,
        "status": status,
        "error": None,
        "incomplete_details": incomplete,
        "model": model,
        "output": _output_items_from_message(message),
        "previous_response_id": previous_response_id,
        "store": store,
    }
    _echo_request_fields(response, request_body)
    usage = _map_usage(chat.get("usage"))
    if usage is not None:
        response["usage"] = usage
    return response


# --- response translation (streaming) --------------------------------------


def _sse(event_type: str, payload: dict[str, Any]) -> str:
    """Serialize one Responses SSE event."""
    return f"event: {event_type}\ndata: {json.dumps(payload)}\n\n"


class ResponsesStreamTranslator:
    """Convert an OpenAI chat-completions SSE stream to Responses SSE events.

    Fed the chunk strings emitted by the ``/v1/chat/completions`` streaming
    handler (each a complete ``data: {...}`` frame, a ``: keepalive`` comment,
    or ``data: [DONE]``), it emits the Responses event sequence:

        response.created → response.in_progress
        → response.output_item.added (message)
        → response.content_part.added (output_text)
        → response.output_text.delta (per token)
        → response.output_text.done → response.content_part.done
        → response.output_item.done
        [ → function_call items with response.function_call_arguments.delta ]
        → response.completed

    On an upstream error chunk it emits ``response.failed`` + ``error``.

    After :meth:`finalize` the accumulated :attr:`final_response`,
    :attr:`assistant_message` and :attr:`usage` are available for persistence.
    """

    def __init__(
        self,
        *,
        response_id: str,
        created_at: int,
        model: str,
        request_body: dict[str, Any],
        previous_response_id: str | None = None,
        store: bool = True,
    ) -> None:
        self._response_id = response_id
        self._created_at = created_at
        self._model = model
        self._request_body = request_body
        self._previous_response_id = previous_response_id
        self._store = store

        self._seq = 0
        self._created_emitted = False
        # Position in the response ``output`` array, assigned-then-incremented
        # per output item so the first emitted item is always index 0 (whether
        # it is text or a tool call).
        self._next_output_index = 0

        # Open text message item state.
        self._text_item_id: str | None = None
        self._text_output_index: int | None = None
        self._text_accum = ""

        # Tool call accumulation, keyed by chat delta index.
        self._tool_calls: dict[int, dict[str, Any]] = {}
        self._tool_order: list[int] = []

        self._finish_reason: str | None = None
        self._usage: dict[str, Any] | None = None
        self._failed = False
        self._error_payload: dict[str, Any] | None = None

        self._final_response: dict[str, Any] | None = None
        self._assistant_message: dict[str, Any] | None = None

    # -- public accessors ----------------------------------------------------

    @property
    def final_response(self) -> dict[str, Any] | None:
        """The terminal Responses object, available after :meth:`finalize`."""
        return self._final_response

    @property
    def assistant_message(self) -> dict[str, Any] | None:
        """The assistant chat message (for persistence), set by :meth:`finalize`."""
        return self._assistant_message

    @property
    def usage(self) -> dict[str, Any] | None:
        """Raw chat usage captured from the final upstream chunk, if any."""
        return self._usage

    @property
    def failed(self) -> bool:
        """True when an upstream error chunk was seen."""
        return self._failed

    # -- event helpers -------------------------------------------------------

    def _next_seq(self) -> int:
        n = self._seq
        self._seq += 1
        return n

    def _skeleton(self, status: str) -> dict[str, Any]:
        """Build a Responses object skeleton at the given status."""
        resp: dict[str, Any] = {
            "id": self._response_id,
            "object": "response",
            "created_at": self._created_at,
            "status": status,
            "error": None,
            "incomplete_details": None,
            "model": self._model,
            "output": [],
            "previous_response_id": self._previous_response_id,
            "store": self._store,
        }
        _echo_request_fields(resp, self._request_body)
        return resp

    def _emit(self, event_type: str, extra: dict[str, Any]) -> str:
        payload = {"type": event_type, "sequence_number": self._next_seq()}
        payload.update(extra)
        return _sse(event_type, payload)

    # -- feed / finalize -----------------------------------------------------

    def feed(self, chunk: str) -> Iterator[str]:
        """Process one upstream SSE chunk; yield Responses SSE events."""
        for line in chunk.splitlines():
            line = line.strip()
            if not line or line.startswith(":"):
                continue  # SSE comment / keepalive — swallowed.
            if not line.startswith("data:"):
                continue
            data = line[len("data:") :].strip()
            if not data or data == "[DONE]":
                continue
            try:
                obj = json.loads(data)
            except (json.JSONDecodeError, ValueError):
                continue
            yield from self._process_obj(obj)

    def _process_obj(self, obj: dict[str, Any]) -> Iterator[str]:
        if not isinstance(obj, dict):
            return

        error = obj.get("error")
        if isinstance(error, dict):
            self._failed = True
            self._error_payload = {
                "code": str(error.get("code") or "server_error"),
                "message": error.get("message") or "upstream error",
            }
            return

        if not self._created_emitted:
            self._created_emitted = True
            yield self._emit("response.created", {"response": self._skeleton("in_progress")})
            yield self._emit("response.in_progress", {"response": self._skeleton("in_progress")})

        if obj.get("usage"):
            self._usage = obj["usage"]

        choices = obj.get("choices") or []
        if not choices:
            return
        choice = choices[0]
        delta = choice.get("delta") or {}
        if choice.get("finish_reason"):
            self._finish_reason = choice["finish_reason"]

        content = delta.get("content")
        if isinstance(content, str) and content:
            yield from self._feed_text(content)

        tool_calls = delta.get("tool_calls")
        if tool_calls:
            yield from self._feed_tool_calls(tool_calls)

    def _feed_text(self, content: str) -> Iterator[str]:
        if self._text_item_id is None:
            self._text_item_id = _msg_item_id()
            self._text_output_index = self._next_output_index
            self._next_output_index += 1
            item = {
                "type": "message",
                "id": self._text_item_id,
                "status": "in_progress",
                "role": "assistant",
                "content": [],
            }
            yield self._emit(
                "response.output_item.added",
                {"output_index": self._text_output_index, "item": item},
            )
            yield self._emit(
                "response.content_part.added",
                {
                    "item_id": self._text_item_id,
                    "output_index": self._text_output_index,
                    "content_index": 0,
                    "part": {"type": "output_text", "text": "", "annotations": []},
                },
            )
        self._text_accum += content
        yield self._emit(
            "response.output_text.delta",
            {
                "item_id": self._text_item_id,
                "output_index": self._text_output_index,
                "content_index": 0,
                "delta": content,
            },
        )

    def _feed_tool_calls(self, tool_calls: list[dict[str, Any]]) -> Iterator[str]:
        # Close an open text block before any tool call opens.
        if self._text_item_id is not None:
            yield from self._close_text_block()

        for tc in tool_calls:
            if not isinstance(tc, dict):
                continue
            idx = tc.get("index", 0)
            fn = tc.get("function") or {}
            is_new = idx not in self._tool_calls
            if is_new:
                self._tool_calls[idx] = {
                    "item_id": _fc_item_id(),
                    "output_index": self._next_output_index,
                    "call_id": "",
                    "name": "",
                    "arguments": "",
                }
                self._next_output_index += 1
                self._tool_order.append(idx)
            state = self._tool_calls[idx]
            # Capture id/name fragments (name may arrive in pieces) before
            # emitting the item-added event so it carries what we know.
            if tc.get("id"):
                state["call_id"] = tc["id"]
            if fn.get("name"):
                state["name"] += fn["name"]
            if is_new:
                yield self._emit(
                    "response.output_item.added",
                    {
                        "output_index": state["output_index"],
                        "item": {
                            "type": "function_call",
                            "id": state["item_id"],
                            "call_id": state["call_id"],
                            "name": state["name"],
                            "arguments": "",
                            "status": "in_progress",
                        },
                    },
                )
            arg_delta = fn.get("arguments")
            if arg_delta:
                state["arguments"] += arg_delta
                yield self._emit(
                    "response.function_call_arguments.delta",
                    {
                        "item_id": state["item_id"],
                        "output_index": state["output_index"],
                        "delta": arg_delta,
                    },
                )

    def _close_text_block(self) -> Iterator[str]:
        item_id = self._text_item_id
        if item_id is None:
            return
        self._text_item_id = None  # mark closed so we don't double-close
        text_oi = self._text_output_index
        yield self._emit(
            "response.output_text.done",
            {
                "item_id": item_id,
                "output_index": text_oi,
                "content_index": 0,
                "text": self._text_accum,
            },
        )
        part = {"type": "output_text", "text": self._text_accum, "annotations": []}
        yield self._emit(
            "response.content_part.done",
            {
                "item_id": item_id,
                "output_index": text_oi,
                "content_index": 0,
                "part": part,
            },
        )
        yield self._emit(
            "response.output_item.done",
            {
                "output_index": text_oi,
                "item": {
                    "type": "message",
                    "id": item_id,
                    "status": "completed",
                    "role": "assistant",
                    "content": [part],
                },
            },
        )
        self._text_done_item_id = item_id

    def finalize(self) -> Iterator[str]:
        """Emit the closing events and the terminal ``response.completed``.

        On a prior upstream error, emits ``response.failed`` + ``error``
        instead. Populates :attr:`final_response` / :attr:`assistant_message`.
        """
        # Close a still-open text block (no tool call followed it).
        had_open_text = self._text_item_id is not None
        if had_open_text:
            yield from self._close_text_block()

        if self._failed:
            err = self._error_payload or {"code": "server_error", "message": "upstream error"}
            failed = self._skeleton("failed")
            failed["error"] = err
            self._final_response = failed
            yield self._emit("response.failed", {"response": failed})
            yield self._emit("error", dict(err))
            return

        # Finalize function-call items.
        output: list[dict[str, Any]] = []
        text_item = self._build_text_item()
        if text_item is not None:
            output.append(text_item)
        tool_calls_for_msg: list[dict[str, Any]] = []
        for idx in self._tool_order:
            st = self._tool_calls[idx]
            item = {
                "type": "function_call",
                "id": st["item_id"],
                "call_id": st["call_id"],
                "name": st["name"],
                "arguments": st["arguments"],
                "status": "completed",
            }
            yield self._emit(
                "response.function_call_arguments.done",
                {
                    "item_id": st["item_id"],
                    "output_index": st["output_index"],
                    "arguments": st["arguments"],
                },
            )
            yield self._emit(
                "response.output_item.done",
                {"output_index": st["output_index"], "item": item},
            )
            output.append(item)
            tool_calls_for_msg.append(
                {
                    "id": st["call_id"],
                    "type": "function",
                    "function": {"name": st["name"], "arguments": st["arguments"]},
                }
            )

        status, incomplete = _map_status(self._finish_reason)
        final = self._skeleton(status)
        final["incomplete_details"] = incomplete
        final["output"] = output
        usage = _map_usage(self._usage)
        if usage is not None:
            final["usage"] = usage
        self._final_response = final

        assistant_msg: dict[str, Any] = {
            "role": "assistant",
            "content": self._text_accum or None,
        }
        if tool_calls_for_msg:
            assistant_msg["tool_calls"] = tool_calls_for_msg
        self._assistant_message = assistant_msg

        # Truncated responses (length / content_filter) terminate with
        # ``response.incomplete``, not ``response.completed`` — clients dispatch
        # on the terminal event type.
        terminal = "response.incomplete" if status == "incomplete" else "response.completed"
        yield self._emit(terminal, {"response": final})

    def _build_text_item(self) -> dict[str, Any] | None:
        item_id = getattr(self, "_text_done_item_id", None)
        if item_id is None or not self._text_accum:
            return None
        return {
            "type": "message",
            "id": item_id,
            "status": "completed",
            "role": "assistant",
            "content": [{"type": "output_text", "text": self._text_accum, "annotations": []}],
        }


def now_ts() -> int:
    """Return the current Unix timestamp (seconds)."""
    return int(time.time())
