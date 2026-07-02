"""Anthropic Messages <-> OpenAI Chat Completions translator (reverse direction).

Forward direction (OpenAI -> Anthropic) lives in serving/adapters/claude_format.py.
This module handles the reverse direction needed when Anthropic-format requests
arrive on the northbound surface and must be dispatched to OpenAI-style backends.

Pure functions plus one stateful streaming translator. No I/O, no logging.
"""

from __future__ import annotations

import json
import uuid
from typing import TYPE_CHECKING, Any

from serving.utils.token_utils import extract_cache_tokens

if TYPE_CHECKING:
    from collections.abc import Iterator

# ---------------------------------------------------------------------------
# Request translation: Anthropic -> OpenAI
# ---------------------------------------------------------------------------


def anthropic_request_to_openai(
    body: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Translate an Anthropic Messages API request to OpenAI Chat Completions.

    Returns:
        (openai_messages, openai_params) where openai_params holds non-message
        fields like max_tokens, temperature, tools, etc.
    """
    messages: list[dict[str, Any]] = []

    # System prompt -> system message prepended.
    system = body.get("system")
    system_text = _flatten_system(system)
    if system_text:
        messages.append({"role": "system", "content": system_text})

    for msg in body.get("messages", []):
        messages.extend(_translate_message(msg))

    params = _translate_params(body)
    return messages, params


def _flatten_system(system: Any) -> str | None:
    if not system:
        return None
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        parts = []
        for block in system:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "\n\n".join(p for p in parts if p) or None
    return None


def _translate_message(msg: dict[str, Any]) -> list[dict[str, Any]]:
    """Translate a single Anthropic message.

    May produce multiple OpenAI messages (tool_result blocks become separate
    role:"tool" messages).
    """
    role = msg.get("role")
    content = msg.get("content")

    if isinstance(content, str):
        return [{"role": role, "content": content}]

    if not isinstance(content, list):
        return [{"role": role, "content": ""}]

    # Bucket blocks: text/image -> content parts, tool_use -> tool_calls,
    # tool_result -> separate role:"tool" messages.
    text_parts: list[dict[str, Any]] = []
    tool_calls: list[dict[str, Any]] = []
    tool_results: list[dict[str, Any]] = []
    text_only_buffer: list[str] = []

    for block in content:
        btype = block.get("type")

        if btype == "text":
            text_parts.append({"type": "text", "text": block.get("text", "")})
            text_only_buffer.append(block.get("text", ""))

        elif btype == "image":
            url = _image_block_to_url(block.get("source", {}))
            if url is not None:
                text_parts.append({"type": "image_url", "image_url": {"url": url}})

        elif btype == "tool_use":
            tool_calls.append(
                {
                    "id": block.get("id", ""),
                    "type": "function",
                    "function": {
                        "name": block.get("name", ""),
                        "arguments": json.dumps(block.get("input", {})),
                    },
                }
            )

        elif btype == "tool_result":
            result_text = _flatten_tool_result_content(block.get("content"))
            if block.get("is_error"):
                # OpenAI tool messages have no error flag; prepend a marker so
                # the model can distinguish a failed tool call from a successful
                # one instead of treating raw stderr as a valid result.
                result_text = f"[tool error]\n{result_text}" if result_text else "[tool error]"
            tool_results.append(
                {
                    "role": "tool",
                    "tool_call_id": block.get("tool_use_id", ""),
                    "content": result_text,
                }
            )

        # Unknown blocks ignored.

    out: list[dict[str, Any]] = []

    # tool_result blocks become standalone tool-role messages. OpenAI requires
    # each tool message to directly follow the assistant tool_calls message it
    # answers, so they must precede any user text in this same turn. Claude Code
    # routinely appends <system-reminder> text blocks alongside tool results, so
    # emitting the text first would produce assistant(tool_calls) -> user(text)
    # -> tool(...), which strict upstreams reject.
    out.extend(tool_results)

    # Build the primary translated message (text + image parts + tool_calls).
    has_image = any(p.get("type") == "image_url" for p in text_parts)
    if text_parts or tool_calls:
        primary: dict[str, Any] = {"role": role}
        if has_image:
            primary["content"] = text_parts
        else:
            # Join separate text blocks with blank lines (matching
            # _flatten_system) so block boundaries survive instead of fusing the
            # user's text onto a trailing <system-reminder>. Fall back to None
            # only when there were no text blocks at all: an all-empty buffer
            # keeps content="" (a valid message), never None without tool_calls.
            joined = "\n\n".join(t for t in text_only_buffer if t)
            primary["content"] = joined if text_only_buffer else None
        if tool_calls:
            primary["tool_calls"] = tool_calls
        out.append(primary)

    return out


def _image_block_to_url(source: dict[str, Any]) -> str | None:
    src_type = source.get("type")
    if src_type == "url":
        return source.get("url")
    if src_type == "base64":
        media_type = source.get("media_type", "application/octet-stream")
        data = source.get("data", "")
        return f"data:{media_type};base64,{data}"
    return None


def _flatten_tool_result_content(content: Any) -> str:
    """Flatten Anthropic tool_result content to a string.

    Anthropic tool_result content can be a string or list of blocks; OpenAI tool
    messages take a string. Text blocks are joined with newlines. Image (and
    other non-text) blocks can't ride along in a string tool message, so each is
    replaced with a ``[image]`` placeholder: this keeps the result non-empty so
    the model doesn't read an image-only tool result as "the tool returned
    nothing" and answer wrongly or loop retrying.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                text_val = block.get("text")
                if isinstance(text_val, str):
                    parts.append(text_val)
            elif btype == "image":
                parts.append("[image]")
        return "\n".join(p for p in parts if p)
    return ""


def _translate_params(body: dict[str, Any]) -> dict[str, Any]:
    params: dict[str, Any] = {}
    if "max_tokens" in body:
        params["max_tokens"] = body["max_tokens"]
    if "temperature" in body:
        params["temperature"] = body["temperature"]
    if "top_p" in body:
        params["top_p"] = body["top_p"]
    if "stop_sequences" in body:
        params["stop"] = body["stop_sequences"]
    if body.get("stream"):
        params["stream"] = True
    if body.get("tools"):
        params["tools"] = [_translate_tool(t) for t in body["tools"]]
    if "tool_choice" in body:
        tc = _translate_tool_choice(body["tool_choice"])
        if tc is not None:
            params["tool_choice"] = tc
    metadata = body.get("metadata") or {}
    user_id = metadata.get("user_id")
    if user_id:
        params["user"] = user_id
    # thinking and other Anthropic-only top-level fields are silently dropped.
    return params


def _translate_tool_choice(tc: Any) -> Any:
    """Translate Anthropic tool_choice to OpenAI tool_choice format."""
    if not isinstance(tc, dict):
        return None
    t = tc.get("type")
    if t == "auto":
        return "auto"
    if t == "any":
        return "required"
    if t == "tool" and tc.get("name"):
        return {"type": "function", "function": {"name": tc["name"]}}
    return None


def _translate_tool(tool: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": tool.get("name", ""),
            "description": tool.get("description", ""),
            "parameters": tool.get("input_schema", {"type": "object", "properties": {}}),
        },
    }


# ---------------------------------------------------------------------------
# Response translation: OpenAI -> Anthropic
# ---------------------------------------------------------------------------

_FINISH_REASON_MAP = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "content_filter": "refusal",
}


def _parse_tool_arguments(raw: Any) -> dict[str, Any]:
    """Parse an OpenAI tool_call ``arguments`` field into an Anthropic input dict.

    ``arguments`` is a JSON *string* per the OpenAI spec, but some vLLM/Ollama/GLM
    deployments return an already-decoded object. Accept both: pass a dict
    through unchanged (previously it was silently replaced with ``{}``), and
    json-decode a string, falling back to ``{}`` only on genuinely malformed
    input.
    """
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def openai_response_to_anthropic(resp: dict[str, Any], *, model: str) -> dict[str, Any]:
    """Translate an OpenAI ChatCompletion response to Anthropic Messages format."""
    choice = (resp.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    content_blocks: list[dict[str, Any]] = []

    # Reasoning models expose chain-of-thought on ``reasoning_content``. Surface
    # it as a thinking block (before the answer) so it isn't silently dropped --
    # otherwise a response truncated during thinking yields an empty content
    # array even though the provider produced (and billed) reasoning output.
    reasoning = message.get("reasoning_content")
    if reasoning:
        content_blocks.append({"type": "thinking", "thinking": reasoning, "signature": ""})

    text = message.get("content")
    if text:
        content_blocks.append({"type": "text", "text": text})

    for tc in message.get("tool_calls") or []:
        fn = tc.get("function") or {}
        content_blocks.append(
            {
                "type": "tool_use",
                "id": tc.get("id", ""),
                "name": fn.get("name", ""),
                "input": _parse_tool_arguments(fn.get("arguments")),
            }
        )

    finish = choice.get("finish_reason") or "stop"
    stop_reason = _FINISH_REASON_MAP.get(finish, "end_turn")
    # Some providers return tool_calls with finish_reason "stop" (the streaming
    # path defends against this too). If tool_use blocks are present but the
    # finish mapped to a plain end_turn, force "tool_use" so the client runs the
    # tools. A "length"/max_tokens finish is left intact -- a tool call
    # truncated at max_tokens must stay max_tokens, not look complete.
    if stop_reason == "end_turn" and any(b.get("type") == "tool_use" for b in content_blocks):
        stop_reason = "tool_use"

    raw_id = resp.get("id") or ""
    if raw_id.startswith("msg_"):
        msg_id = raw_id
    elif raw_id:
        msg_id = f"msg_{raw_id}"
    else:
        msg_id = f"msg_{uuid.uuid4().hex[:24]}"

    usage_in = resp.get("usage") or {}
    cache_read, cache_write = extract_cache_tokens(usage_in)
    prompt_tokens = int(usage_in.get("prompt_tokens", 0) or 0)
    # OpenAI prompt_tokens is cache-inclusive; Anthropic input_tokens is the
    # non-cached remainder. Subtract cache subset and clamp to >= 0 so the
    # translated shape stays disjoint when the upstream usage is inconsistent.
    input_tokens = max(0, prompt_tokens - (cache_read or 0) - (cache_write or 0))
    anthropic_usage: dict[str, int] = {
        "input_tokens": input_tokens,
        "output_tokens": int(usage_in.get("completion_tokens", 0) or 0),
    }
    if cache_read is not None:
        anthropic_usage["cache_read_input_tokens"] = cache_read
    if cache_write is not None:
        anthropic_usage["cache_creation_input_tokens"] = cache_write

    return {
        "id": msg_id,
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content_blocks,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": anthropic_usage,
    }


# ---------------------------------------------------------------------------
# Streaming translation: OpenAI SSE -> Anthropic SSE
# ---------------------------------------------------------------------------


class OpenAIToAnthropicStreamTranslator:
    """Stateful translator: feed OpenAI SSE chunks, emit Anthropic SSE bytes.

    OpenAI emits per-chunk JSON deltas. Anthropic emits a richer event sequence:
    message_start -> content_block_start -> content_block_delta...
    -> content_block_stop -> ... -> message_delta -> message_stop.

    Caller pattern:
        t = OpenAIToAnthropicStreamTranslator(model="m")
        async for chunk in upstream_openai_stream:
            for ant in t.feed(chunk):
                yield ant
        for ant in t.finalize():
            yield ant

    ``input_tokens_estimate`` seeds message_start.usage.input_tokens. OpenAI
    streams report prompt tokens only in the final chunk, so message_start would
    otherwise carry 0 -- but Anthropic clients read input usage from
    message_start, where a 0 breaks their context/cost accounting. The estimate
    is corrected to the exact value in the terminal message_delta once the
    upstream reports it.
    """

    def __init__(self, *, model: str, input_tokens_estimate: int = 0) -> None:
        self.model = model
        self._message_id = f"msg_{uuid.uuid4().hex[:24]}"
        self._started = False
        self._closed = False
        self._current_thinking_index: int | None = None
        self._current_text_index: int | None = None
        self._tool_blocks: dict[
            int, dict[str, Any]
        ] = {}  # openai-tool-index -> {anthropic_index, name, id}
        self._next_index = 0
        self._finish_reason: str | None = None
        self._usage: dict[str, int] = {
            "input_tokens": max(0, int(input_tokens_estimate)),
            "output_tokens": 0,
        }
        self._buffer = b""

    @property
    def usage(self) -> dict[str, int]:
        """Return a copy of current usage counters."""
        return dict(self._usage)

    def feed(self, chunk: bytes) -> Iterator[bytes]:
        """Consume one OpenAI SSE chunk and yield zero or more Anthropic SSE frames."""
        self._buffer += chunk
        while b"\n\n" in self._buffer:
            frame, self._buffer = self._buffer.split(b"\n\n", 1)
            yield from self._handle_frame(frame)

    def finalize(self) -> Iterator[bytes]:
        """Flush any buffered state and emit the terminal message_delta / message_stop events."""
        if self._closed:
            return
        if not self._started:
            yield from self._emit_message_start()
        # Close any open content block.
        yield from self._close_thinking()
        if self._current_text_index is not None:
            yield self._sse(
                "content_block_stop",
                {"type": "content_block_stop", "index": self._current_text_index},
            )
            self._current_text_index = None
        for tb in list(self._tool_blocks.values()):
            yield self._sse(
                "content_block_stop", {"type": "content_block_stop", "index": tb["anthropic_index"]}
            )
        self._tool_blocks.clear()
        # Emit message_delta with stop_reason + final usage. input_tokens are
        # unknown when message_start is emitted (upstream reports usage only in
        # its final chunk), so message_start carries 0; the client picks up the
        # real input/cache counts from this terminal message_delta.usage.
        stop_reason = _FINISH_REASON_MAP.get(self._finish_reason or "stop", "end_turn")
        delta_usage: dict[str, int] = {
            "input_tokens": self._usage.get("input_tokens", 0),
            "output_tokens": self._usage["output_tokens"],
        }
        if "cache_read_input_tokens" in self._usage:
            delta_usage["cache_read_input_tokens"] = self._usage["cache_read_input_tokens"]
        if "cache_creation_input_tokens" in self._usage:
            delta_usage["cache_creation_input_tokens"] = self._usage["cache_creation_input_tokens"]
        yield self._sse(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                "usage": delta_usage,
            },
        )
        yield self._sse("message_stop", {"type": "message_stop"})
        self._closed = True

    # -- internals --

    def _handle_frame(self, frame: bytes) -> Iterator[bytes]:
        for line in frame.split(b"\n"):
            if not line.startswith(b"data: "):
                continue
            payload = line[len(b"data: ") :].strip()
            if not payload or payload == b"[DONE]":
                continue
            try:
                obj = json.loads(payload)
            except json.JSONDecodeError:
                continue
            yield from self._handle_chunk(obj)

    def _handle_chunk(self, obj: dict[str, Any]) -> Iterator[bytes]:
        usage = obj.get("usage")
        if usage:
            cache_read, cache_write = extract_cache_tokens(usage)
            prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
            # OpenAI prompt_tokens is cache-inclusive; Anthropic input_tokens
            # is the non-cached remainder. Clamp to >= 0 if upstream usage is
            # inconsistent.
            input_tokens = max(0, prompt_tokens - (cache_read or 0) - (cache_write or 0))
            if "prompt_tokens" in usage:
                self._usage["input_tokens"] = input_tokens
            self._usage["output_tokens"] = int(
                usage.get("completion_tokens", self._usage["output_tokens"])
            )
            if cache_read is not None:
                self._usage["cache_read_input_tokens"] = cache_read
            if cache_write is not None:
                self._usage["cache_creation_input_tokens"] = cache_write

        choices = obj.get("choices") or []
        if not choices:
            return
        choice = choices[0]
        delta = choice.get("delta") or {}

        if not self._started:
            yield from self._emit_message_start()

        # Reasoning/thinking deltas (DeepSeek-R1, Zhipu, ...) arrive on their own
        # field. Emit them as thinking-block frames: this both preserves the
        # chain-of-thought for the client and keeps the stream producing bytes
        # during a long reasoning phase, so the router's idle watchdog doesn't
        # abort a healthy upstream that hasn't emitted visible text yet.
        reasoning = (
            delta.get("reasoning_content") or delta.get("reasoning") or delta.get("thinking")
        )
        if isinstance(reasoning, str) and reasoning:
            yield from self._emit_thinking(reasoning)

        text = delta.get("content")
        if text:
            yield from self._emit_text(text)

        for tc in delta.get("tool_calls") or []:
            yield from self._emit_tool_call_delta(tc)

        fr = choice.get("finish_reason")
        if fr:
            self._finish_reason = fr

    def _emit_message_start(self) -> Iterator[bytes]:
        self._started = True
        yield self._sse(
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": self._message_id,
                    "type": "message",
                    "role": "assistant",
                    "model": self.model,
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": dict(self._usage),
                },
            },
        )

    def _emit_thinking(self, text: str) -> Iterator[bytes]:
        if self._current_thinking_index is None:
            # Anthropic SSE allows one open block at a time. Reasoning usually
            # precedes visible output, but with interleaved thinking it can
            # follow text or a tool call, so close any open block first.
            if self._current_text_index is not None:
                yield self._sse(
                    "content_block_stop",
                    {"type": "content_block_stop", "index": self._current_text_index},
                )
                self._current_text_index = None
            for tb in list(self._tool_blocks.values()):
                yield self._sse(
                    "content_block_stop",
                    {"type": "content_block_stop", "index": tb["anthropic_index"]},
                )
            self._tool_blocks.clear()
            self._current_thinking_index = self._next_index
            self._next_index += 1
            yield self._sse(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": self._current_thinking_index,
                    "content_block": {"type": "thinking", "thinking": "", "signature": ""},
                },
            )
        yield self._sse(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": self._current_thinking_index,
                "delta": {"type": "thinking_delta", "thinking": text},
            },
        )

    def _close_thinking(self) -> Iterator[bytes]:
        if self._current_thinking_index is not None:
            yield self._sse(
                "content_block_stop",
                {"type": "content_block_stop", "index": self._current_thinking_index},
            )
            self._current_thinking_index = None

    def _emit_text(self, text: str) -> Iterator[bytes]:
        if self._current_text_index is None:
            # A thinking block precedes visible text; close it first. Anthropic
            # SSE requires exactly one open content block at a time.
            yield from self._close_thinking()
            # Close any open tool blocks first (Anthropic SSE requires one block at a time).
            for tb in list(self._tool_blocks.values()):
                yield self._sse(
                    "content_block_stop",
                    {"type": "content_block_stop", "index": tb["anthropic_index"]},
                )
            self._tool_blocks.clear()
            self._current_text_index = self._next_index
            self._next_index += 1
            yield self._sse(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": self._current_text_index,
                    "content_block": {"type": "text", "text": ""},
                },
            )
        yield self._sse(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": self._current_text_index,
                "delta": {"type": "text_delta", "text": text},
            },
        )

    def _emit_tool_call_delta(self, tc: dict[str, Any]) -> Iterator[bytes]:
        idx = tc.get("index", 0)
        fn = tc.get("function") or {}
        # Close thinking/text blocks if open before opening a tool block
        # (matches Anthropic ordering convention).
        if idx not in self._tool_blocks:
            yield from self._close_thinking()
            if self._current_text_index is not None:
                yield self._sse(
                    "content_block_stop",
                    {"type": "content_block_stop", "index": self._current_text_index},
                )
                self._current_text_index = None

        if idx not in self._tool_blocks:
            anthropic_index = self._next_index
            self._next_index += 1
            self._tool_blocks[idx] = {
                "anthropic_index": anthropic_index,
                "id": tc.get("id", ""),
                "name": fn.get("name", ""),
            }
            yield self._sse(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": anthropic_index,
                    "content_block": {
                        "type": "tool_use",
                        "id": tc.get("id", ""),
                        "name": fn.get("name", ""),
                        "input": {},
                    },
                },
            )

        anthropic_index = self._tool_blocks[idx]["anthropic_index"]
        partial = fn.get("arguments")
        # partial_json must be a JSON *string* fragment. Most providers stream
        # string fragments; some send the whole arguments object as a dict in one
        # delta -- serialize that to a string so the client's SSE parser doesn't
        # receive a partial_json that is an object.
        if isinstance(partial, dict):
            partial = json.dumps(partial)
        elif partial is not None and not isinstance(partial, str):
            partial = None
        if partial:
            yield self._sse(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": anthropic_index,
                    "delta": {"type": "input_json_delta", "partial_json": partial},
                },
            )

    def _sse(self, event: str, payload: dict[str, Any]) -> bytes:
        return f"event: {event}\ndata: {json.dumps(payload)}\n\n".encode()


# ---------------------------------------------------------------------------
# Anthropic-native SSE usage extraction (moved from anthropic_proxy.py)
# ---------------------------------------------------------------------------


def extract_anthropic_usage_from_sse(raw: bytes, usage: dict[str, int]) -> None:
    """Best-effort parse of Anthropic SSE frames to extract usage counters.

    Mutates ``usage`` in-place. Never raises -- failures are silently ignored
    so the forwarded stream is never affected.

    Anthropic sends cumulative values, not per-event deltas:
      message_start.message.usage.input_tokens     -- total input tokens
      message_delta.usage.output_tokens             -- total output tokens so far
    """
    try:
        text = raw.decode("utf-8", errors="replace")
        for line in text.split("\n"):
            if not line.startswith("data: "):
                continue
            payload = line[len("data: ") :].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                obj = json.loads(payload)
            except json.JSONDecodeError:
                continue
            event_type = obj.get("type", "")
            if event_type == "message_start":
                msg_usage = (obj.get("message") or {}).get("usage") or {}
                if "input_tokens" in msg_usage:
                    usage["input_tokens"] = int(msg_usage["input_tokens"])
                if "cache_creation_input_tokens" in msg_usage:
                    usage["cache_creation_input_tokens"] = int(
                        msg_usage["cache_creation_input_tokens"]
                    )
                if "cache_read_input_tokens" in msg_usage:
                    usage["cache_read_input_tokens"] = int(msg_usage["cache_read_input_tokens"])
            elif event_type == "message_delta":
                delta_usage = obj.get("usage") or {}
                if "output_tokens" in delta_usage:
                    usage["output_tokens"] = int(delta_usage["output_tokens"])
                for k in ("cache_read_input_tokens", "cache_creation_input_tokens"):
                    if k in delta_usage:
                        usage[k] = int(delta_usage[k])
    except Exception:
        # Best-effort: never disturb the streaming pass-through. Failures
        # here only affect DB-logged usage counts.
        pass
