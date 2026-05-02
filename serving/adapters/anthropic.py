"""Direct-Anthropic adapter (kind: anthropic).

Talks api.anthropic.com using x-api-key auth (no Vertex, no OAuth subscription).

Provides:
  - messages() / stream_messages()        Anthropic-format identity passthrough
                                          (Task 8 fills these in; placeholders
                                          raise NotImplementedError until then).
  - chat_completion() / stream_chat_completion()
                                          OpenAI-format northbound -> translates
                                          OpenAI -> Anthropic upstream using the
                                          existing claude_format helpers.

Sibling of the Vertex ClaudeAdapter (serving/adapters/claude.py); same forward
translation logic, different upstream auth/path.
"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any

import aiohttp

from serving.http import AsyncHTTPClient
from serving.stream import done_sentinel
from serving.utils.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator

from .base import BaseAdapter
from .claude_format import (
    ToolCallAccumulator,
    build_final_usage,
    convert_messages,
    convert_tool_choice,
    convert_tools,
    extract_system,
    handle_stream_event,
    map_stop_reason,
    parse_response_content,
    parse_usage,
)

logger = get_logger(__name__)


class AnthropicAdapter(BaseAdapter):
    """Direct Anthropic Messages API adapter."""

    native_format = "anthropic"

    ANTHROPIC_VERSION = "2023-06-01"
    DEFAULT_BASE = "https://api.anthropic.com"

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _base_url(self) -> str:
        """Return the base URL, stripped of trailing slash."""
        return (self.config.base_url or self.DEFAULT_BASE).rstrip("/")

    def _upstream_url(self) -> str:
        """Return the full Anthropic Messages API endpoint URL."""
        return f"{self._base_url()}/v1/messages"

    def _upstream_model(self) -> str:
        """Return the upstream model identifier."""
        return self.config.provider_model_id or self.config.id

    def _upstream_headers(self, *, streaming: bool) -> dict[str, str]:
        """Build request headers for upstream Anthropic API calls."""
        headers = {
            "x-api-key": self.config.api_key or "",
            "anthropic-version": self.ANTHROPIC_VERSION,
            "content-type": "application/json",
        }
        if streaming:
            headers["accept"] = "text/event-stream"
            headers["accept-encoding"] = "identity"
        else:
            headers["accept"] = "application/json"
        return headers

    # ------------------------------------------------------------------
    # OpenAI-format northbound -> Anthropic upstream
    # ------------------------------------------------------------------

    async def chat_completion(
        self, messages: list[dict[str, Any]], **params: Any
    ) -> dict[str, Any]:
        """Execute a non-streaming chat completion request.

        Translates OpenAI-format input to Anthropic upstream format and
        converts the response back to OpenAI-compatible Chat Completion shape.

        Args:
            messages: Chat history in OpenAI-compatible format.
            **params: Parameters including system, max_tokens, temperature, etc.

        Returns:
            OpenAI-compatible response dictionary.
        """
        validated = self.validate_params(params)

        payload: dict[str, Any] = {
            "model": self._upstream_model(),
            "messages": convert_messages(messages),
            "max_tokens": validated.get("max_tokens", self.config.max_output_length),
        }

        sys_text = params.get("system") or extract_system(messages)
        if sys_text:
            payload["system"] = sys_text

        for k in ("temperature", "top_p"):
            if k in validated:
                payload[k] = validated[k]

        if "top_k" in validated and "top_k" in self.config.supported_params:
            payload["top_k"] = validated["top_k"]

        if "stop" in validated:
            payload["stop_sequences"] = validated["stop"]

        if params.get("tools") and self.config.supports_tools:
            converted_tools = convert_tools(params["tools"])
            if converted_tools:
                payload["tools"] = converted_tools
                if params.get("tool_choice") is not None:
                    convert_tool_choice(params["tool_choice"], payload)

        http = AsyncHTTPClient.shared()
        upstream = await http.json_post_with_retry(
            self._upstream_url(),
            json=payload,
            headers=self._upstream_headers(streaming=False),
            timeout=None,
            retries=2,
        )

        # Translate Anthropic response -> OpenAI Chat Completion shape.
        text, tool_calls = parse_response_content(upstream.get("content", []))
        usage = parse_usage(upstream.get("usage", {}))
        finish_reason = map_stop_reason(upstream.get("stop_reason", "end_turn"))

        return self.format_response(
            content=text or None,
            model=self.config.id,
            usage=usage,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
        )

    async def stream_chat_completion(
        self, messages: list[dict[str, Any]], **params: Any
    ) -> AsyncGenerator[str, None]:
        """Execute a streaming chat completion request.

        Translates OpenAI-format input to Anthropic SSE upstream format and
        converts Anthropic SSE events to OpenAI-compatible delta chunks.

        Yields SSE-formatted chunks compatible with OpenAI clients.
        """
        validated = self.validate_params(params)

        payload: dict[str, Any] = {
            "model": self._upstream_model(),
            "messages": convert_messages(messages),
            "max_tokens": validated.get("max_tokens", self.config.max_output_length),
            "stream": True,
        }

        sys_text = params.get("system") or extract_system(messages)
        if sys_text:
            payload["system"] = sys_text

        for k in ("temperature", "top_p"):
            if k in validated:
                payload[k] = validated[k]

        if "stop" in validated:
            payload["stop_sequences"] = validated["stop"]

        if params.get("tools") and self.config.supports_tools:
            converted_tools = convert_tools(params["tools"])
            if converted_tools:
                payload["tools"] = converted_tools
                if params.get("tool_choice") is not None:
                    convert_tool_choice(params["tool_choice"], payload)

        http = AsyncHTTPClient.shared()
        session = await http._ensure_session()
        timeout = aiohttp.ClientTimeout(total=None)
        accum = ToolCallAccumulator()

        input_tokens = 0
        output_tokens = 0
        cache_read_input_tokens = 0
        cache_creation_input_tokens = 0
        finish_reason = "stop"
        total_content = ""

        async with session.post(
            self._upstream_url(),
            json=payload,
            headers=self._upstream_headers(streaming=True),
            timeout=timeout,
        ) as resp:
            buf = b""
            async for raw in resp.content.iter_any():
                buf += raw
                while b"\n\n" in buf:
                    frame, buf = buf.split(b"\n\n", 1)
                    for line in frame.split(b"\n"):
                        if not line.startswith(b"data: "):
                            continue
                        payload_bytes = line[len(b"data: ") :].strip()
                        if not payload_bytes:
                            continue
                        try:
                            evt = json.loads(payload_bytes)
                        except json.JSONDecodeError:
                            continue

                        result = handle_stream_event(evt, accum)

                        if result.input_tokens:
                            input_tokens = result.input_tokens
                        if result.output_tokens:
                            output_tokens = result.output_tokens
                        if result.cache_read_tokens:
                            cache_read_input_tokens = result.cache_read_tokens
                        if result.cache_write_tokens:
                            cache_creation_input_tokens = result.cache_write_tokens

                        if result.text_delta:
                            total_content += result.text_delta
                            yield self.format_stream_chunk(result.text_delta, self.config.id)

                        if result.finish_reason:
                            finish_reason = result.finish_reason

                        if result.is_done:
                            # Emit collected tool calls (if any) at message_stop.
                            completed_tools = accum.get_completed()
                            if completed_tools:
                                yield self.format_tool_chunk(completed_tools, self.config.id)
                                finish_reason = "tool_calls"

                            # Build and emit final usage chunk.
                            usage_obj = build_final_usage(
                                input_tokens=input_tokens,
                                output_tokens=output_tokens,
                                cache_read_input_tokens=cache_read_input_tokens,
                                cache_creation_input_tokens=cache_creation_input_tokens,
                            )
                            final_chunk: dict[str, Any] = {
                                "id": f"chatcmpl-{int(time.time() * 1000)}",
                                "object": "chat.completion.chunk",
                                "created": int(time.time()),
                                "model": self.config.id,
                                "choices": [
                                    {"index": 0, "delta": {}, "finish_reason": finish_reason}
                                ],
                                "usage": usage_obj,
                            }
                            yield f"data: {json.dumps(final_chunk)}\n\n"
                            yield done_sentinel()
                            return

    # ------------------------------------------------------------------
    # Anthropic-format northbound (Task 8 implements; placeholders here)
    # ------------------------------------------------------------------

    async def messages(self, body: dict[str, Any], *, request_id: str) -> dict[str, Any]:
        """Anthropic-format identity passthrough. Task 8 fills this in."""
        raise NotImplementedError("Anthropic-format passthrough lands in Task 8")

    async def stream_messages(
        self, body: dict[str, Any], *, request_id: str
    ) -> AsyncIterator[bytes]:
        """Anthropic-format streaming identity passthrough. Task 8 fills this in."""
        raise NotImplementedError("Anthropic-format passthrough lands in Task 8")
        yield b""  # pragma: no cover - keeps async-generator type
