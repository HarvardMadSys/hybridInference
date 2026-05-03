"""Claude (Anthropic) adapter for Google Vertex AI API.

This adapter interfaces with Claude models through Google Vertex
hybrid API which uses custom endpoints:
- Non-streaming: /v1:rawPredict
- Streaming: /v1:streamRawPredict

Key differences from standard Anthropic API:
- Uses 'api-key' header instead of 'x-api-key'
- Requires 'anthropic_version' parameter
- Different endpoint paths (Google Vertex AI style)

Format translation (OpenAI ↔ Claude Messages API) is shared with
ClaudeSubscriptionAdapter via the ``claude_format`` module.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

from serving.stream import done_sentinel, make_final_usage_chunk
from serving.utils.logging import get_logger

from .base import BaseAdapter
from .claude_format import (
    ToolCallAccumulator,
    build_final_usage,
    convert_content_block,
    convert_content_blocks,
    convert_messages,
    convert_tool_choice,
    convert_tools,
    extract_system,
    handle_stream_event,
    infer_media_type_from_url,
    map_stop_reason,
    parse_data_url,
    parse_response_content,
    parse_usage,
)

logger = get_logger(__name__)


class ClaudeAdapter(BaseAdapter):
    """Adapter for Claude models via Google Vertex API."""

    # Anthropic API version
    ANTHROPIC_VERSION = "vertex-2023-10-16"

    def _convert_content_block(self, block: dict[str, Any] | str) -> dict[str, Any]:
        """Convert a single content block from OpenAI format to Claude format."""
        return convert_content_block(block)

    def _parse_data_url(self, data_url: str) -> dict[str, Any]:
        """Parse a data URL and convert to Claude image format."""
        return parse_data_url(data_url)

    def _infer_media_type_from_url(self, url: str) -> str:
        """Infer media type from URL extension."""
        return infer_media_type_from_url(url)

    def _convert_content_blocks(self, content: str | list[Any] | Any) -> list[dict[str, Any]]:
        """Convert content from OpenAI format to Claude content block list."""
        return convert_content_blocks(content)

    async def chat_completion(
        self, messages: list[dict[str, Any]], **params: Any
    ) -> dict[str, Any]:
        """Execute a non-streaming chat completion request.

        Args:
            messages: Chat history in OpenAI-compatible format.
            **params: Parameters including system, max_tokens, temperature, etc.

        Returns:
            OpenAI-compatible response dictionary.
        """
        validated_params = self.validate_params(params)

        # Build Claude-specific payload
        # Note: API uses Vertex AI style - model is in URL, not payload
        payload: dict[str, Any] = {
            "anthropic_version": self.ANTHROPIC_VERSION,
            "messages": self._convert_messages(messages),
            # Claude Vertex expects message blocks; stream flag left to endpoint semantics
        }

        # Extract system prompt if present; otherwise derive from messages
        if params.get("system"):
            payload["system"] = params["system"]
        else:
            sys_text = self._extract_system(messages)
            if sys_text:
                payload["system"] = sys_text

        # Add validated parameters
        payload["max_tokens"] = validated_params.get("max_tokens", self.config.max_output_length)

        if "temperature" in validated_params:
            payload["temperature"] = validated_params["temperature"]

        if "top_p" in validated_params:
            payload["top_p"] = validated_params["top_p"]

        if "top_k" in validated_params and "top_k" in self.config.supported_params:
            payload["top_k"] = validated_params["top_k"]

        if "stop" in validated_params:
            # Claude calls these "stop_sequences"
            payload["stop_sequences"] = validated_params["stop"]

        # Tools support
        if params.get("tools") and self.config.supports_tools:
            converted_tools = self._convert_tools(params["tools"])
            if converted_tools:
                payload["tools"] = converted_tools

                if params.get("tool_choice"):
                    convert_tool_choice(params["tool_choice"], payload)

        # Build endpoint URL (non-streaming)
        endpoint = f"{self.config.base_url.rstrip('/')}/v1:rawPredict"

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "api-key": self.config.api_key,
        }

        data = await self.http.json_post_with_retry(
            endpoint, json=payload, headers=headers, timeout=120, retries=3
        )

        if "Code" in data and "Error" in data:
            error_code = data.get("Code")
            error_msg = data.get("Error")
            logger.error(f"[CLAUDE UPSTREAM ERROR] Code: {error_code}, Error: {error_msg}")
            raise RuntimeError(f"Upstream API error: {error_msg}")

        content, tool_calls = parse_response_content(data.get("content", []))
        usage = parse_usage(data.get("usage", {}))
        finish_reason = map_stop_reason(data.get("stop_reason", "end_turn"))

        return self.format_response(
            content=content,
            model=self.config.id,
            usage=usage,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
        )

    async def stream_chat_completion(
        self, messages: list[dict[str, Any]], **params: Any
    ) -> AsyncGenerator[str, None]:
        """Execute a streaming chat completion request.

        Yields SSE-formatted chunks compatible with OpenAI clients.
        """
        validated_params = self.validate_params(params)

        # Build Claude-specific payload
        # Note: API uses Vertex AI style - model is in URL, not payload
        # Also: streaming is determined by endpoint, not "stream" parameter
        converted_msgs = self._convert_messages(messages)
        payload: dict[str, Any] = {
            "anthropic_version": self.ANTHROPIC_VERSION,
            "messages": converted_msgs,
        }

        # Extract system prompt if present; otherwise derive from messages
        if params.get("system"):
            payload["system"] = params["system"]
        else:
            sys_text = self._extract_system(messages)
            if sys_text:
                payload["system"] = sys_text

        # Add validated parameters
        payload["max_tokens"] = validated_params.get("max_tokens", self.config.max_output_length)

        if "temperature" in validated_params:
            payload["temperature"] = validated_params["temperature"]

        if "top_p" in validated_params:
            payload["top_p"] = validated_params["top_p"]

        if "top_k" in validated_params and "top_k" in self.config.supported_params:
            payload["top_k"] = validated_params["top_k"]

        if "stop" in validated_params:
            payload["stop_sequences"] = validated_params["stop"]

        # Tools support
        if params.get("tools") and self.config.supports_tools:
            converted_tools = self._convert_tools(params["tools"])
            if converted_tools:
                payload["tools"] = converted_tools

                if params.get("tool_choice"):
                    convert_tool_choice(params["tool_choice"], payload)

        # Build endpoint URL (streaming)
        endpoint = f"{self.config.base_url.rstrip('/')}/v1:streamRawPredict"

        headers = {
            "Content-Type": "application/json",
            # Allow server to choose SSE or NDJSON; HTTP client will auto-detect
            "Accept": "*/*",
            "api-key": self.config.api_key,
        }

        total_content = ""
        input_tokens = 0
        output_tokens = 0
        cache_read_input_tokens = 0
        cache_creation_input_tokens = 0
        finish_reason = "stop"

        accumulator = ToolCallAccumulator()

        logger.debug(f"Starting stream to: {endpoint}")
        logger.debug(f"Payload summary: {json.dumps(self._summarize_messages(converted_msgs))}")

        try:
            # Google Vertex API may return non-streaming JSON instead of SSE
            # Try to use ndjson mode which is more tolerant
            async for line in self.http.stream_post(
                endpoint, json=payload, headers=headers, mode="auto", timeout=120
            ):
                if not line.strip():
                    continue

                # Parse JSON line
                try:
                    chunk_data = json.loads(line)
                except json.JSONDecodeError:
                    logger.warning(f"Failed to parse JSON: {line[:100]}")
                    continue

                # Check for upstream API errors
                if "Code" in chunk_data and "Error" in chunk_data:
                    error_code = chunk_data.get("Code")
                    error_msg = chunk_data.get("Error")
                    logger.error(f"[CLAUDE UPSTREAM ERROR] Code: {error_code}, Error: {error_msg}")
                    logger.error(
                        "[CLAUDE UPSTREAM ERROR] This is likely an issue with the API, not our code."
                    )
                    logger.error(
                        "[CLAUDE UPSTREAM ERROR] Falling back to non-streaming endpoint..."
                    )
                    # Raise exception to trigger fallback
                    raise RuntimeError(f"Upstream API error: {error_msg}")

                chunk_type = chunk_data.get("type")

                # Google Vertex API sometimes returns a complete "message" object instead of streaming chunks
                if chunk_type == "message":
                    # Extract content blocks - both text and tool_use
                    content_blocks = chunk_data.get("content", [])
                    full_text = ""
                    tool_calls_list = []

                    # Router emits initial role chunk; no need to track here.
                    for block in content_blocks:
                        block_type = block.get("type")
                        if block_type == "text":
                            full_text += block.get("text", "")
                        elif block_type == "tool_use":
                            # Convert Claude tool_use to OpenAI streaming format
                            # CRITICAL: Must include "index" field for streaming!
                            tool_call = {
                                "index": len(tool_calls_list),  # contiguous index within tool_calls
                                "id": block.get("id"),
                                "type": "function",
                                "function": {
                                    "name": block.get("name"),
                                    "arguments": json.dumps(block.get("input", {})),
                                },
                            }
                            tool_calls_list.append(tool_call)
                            logger.debug(
                                f"Found tool_use: {block.get('name')}, id: {block.get('id')}, index: {tool_call['index']}"
                            )

                    # Extract usage
                    usage_data = chunk_data.get("usage", {})
                    input_tokens = int(usage_data.get("input_tokens", 0) or 0)
                    output_tokens = int(usage_data.get("output_tokens", 0) or 0)
                    cache_read_input_tokens = int(usage_data.get("cache_read_input_tokens", 0) or 0)
                    cache_creation_input_tokens = int(
                        usage_data.get("cache_creation_input_tokens", 0) or 0
                    )
                    thinking_tokens = int(usage_data.get("thinking_tokens", 0) or 0)

                    # Yield content chunk if present
                    if full_text:
                        total_content = full_text
                        # Log full text length and preview for debugging
                        logger.debug(
                            f"Full text length: {len(full_text)}, preview: {full_text[:500]}"
                        )
                        logger.debug(f"Full text end: ...{full_text[-200:]}")
                        # Don't include role - router layer handles initial role chunk
                        yield self.format_stream_chunk(full_text, self.config.id)

                    # Map stop_reason
                    stop_reason = chunk_data.get("stop_reason", "end_turn")
                    finish_reason = self._map_stop_reason(stop_reason)

                    # If tools present
                    if tool_calls_list:
                        # Forward tool_calls to client per OpenAI streaming protocol
                        # Router layer handles initial role chunk, we just send tools
                        tool_chunk = self.format_tool_chunk(tool_calls_list, self.config.id)
                        logger.debug(f"Yielding {len(tool_calls_list)} tool calls")
                        yield tool_chunk

                        # Ensure finish_reason reflects tool_calls for OpenAI clients
                        finish_reason = "tool_calls"
                        finish_chunk = self.format_stream_chunk(
                            "", self.config.id, finish_reason=finish_reason
                        )
                        logger.debug(f"Yielding finish_reason={finish_reason}")
                        yield finish_chunk

                        # Send usage even for tool_calls to enable proper logging and billing
                        yield make_final_usage_chunk(
                            model=self.config.id,
                            messages=messages,
                            total_content=total_content,
                            prompt_tokens_override=(
                                input_tokens + cache_read_input_tokens + cache_creation_input_tokens
                            )
                            if input_tokens > 0
                            else None,
                            completion_tokens_override=output_tokens if output_tokens > 0 else None,
                            finish_reason=finish_reason,
                            provider=self.config.provider,
                            base_url=self.config.base_url,
                            cache_read_tokens=cache_read_input_tokens,
                            cache_write_tokens=cache_creation_input_tokens,
                            reasoning_tokens=thinking_tokens,
                        )
                        yield done_sentinel()
                        return

                    # Yield final usage chunk
                    yield make_final_usage_chunk(
                        model=self.config.id,
                        messages=messages,
                        total_content=total_content,
                        prompt_tokens_override=(
                            input_tokens + cache_read_input_tokens + cache_creation_input_tokens
                        )
                        if input_tokens > 0
                        else None,
                        completion_tokens_override=output_tokens if output_tokens > 0 else None,
                        finish_reason=finish_reason,
                        provider=self.config.provider,
                        base_url=self.config.base_url,
                        cache_read_tokens=cache_read_input_tokens,
                        cache_write_tokens=cache_creation_input_tokens,
                        reasoning_tokens=thinking_tokens,
                    )
                    yield done_sentinel()
                    return

                # Normal streaming events — delegate to shared handler
                result = handle_stream_event(chunk_data, accumulator)

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
                    # Emit collected tool calls (if any) at message_stop
                    completed_tools = accumulator.get_completed()
                    if completed_tools:
                        yield self.format_tool_chunk(completed_tools, self.config.id)
                        finish_reason = "tool_calls"

                    # Build and emit final usage chunk with _routing metadata
                    usage_obj = build_final_usage(
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        cache_read_input_tokens=cache_read_input_tokens,
                        cache_creation_input_tokens=cache_creation_input_tokens,
                    )
                    final_chunk = {
                        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
                        "object": "chat.completion.chunk",
                        "created": int(time.time()),
                        "model": self.config.id,
                        "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}],
                        "usage": usage_obj,
                        "_routing": {
                            "provider": self.config.provider,
                            "base_url": self.config.base_url,
                            "endpoint_id": getattr(self.config, "endpoint_id", None)
                            or self.config.provider,
                        },
                    }
                    yield f"data: {json.dumps(final_chunk)}\n\n"
                    yield done_sentinel()
                    break
        except Exception as e:
            import aiohttp

            logger.error(f"[CLAUDE STREAM ERROR] {type(e).__name__}: {e}")
            if isinstance(e, aiohttp.ClientResponseError):
                logger.error(f"Response status: {e.status}, message: {e.message}")
                logger.error(f"Request info: {e.request_info}")
                # Try to get detailed error message from attached error_body
                error_body = getattr(e, "error_body", None)
                if error_body:
                    logger.error(f"Response body: {error_body[:1000]}")
                else:
                    logger.error("Response body: No response body available")
            # Fail-fast: propagate error immediately instead of fallback
            raise

    def _convert_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Convert OpenAI-format messages to Claude format.

        Delegates to ``claude_format.convert_messages``.
        """
        return convert_messages(messages)

    def _convert_tools(self, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Convert OpenAI tool format to Claude tool format."""
        return convert_tools(tools)

    def _map_stop_reason(self, claude_stop_reason: str) -> str:
        """Map Claude's stop_reason to OpenAI's finish_reason."""
        return map_stop_reason(claude_stop_reason)

    def _extract_system(self, messages: list[dict[str, Any]]) -> str | None:
        """Extract system text from input messages if present."""
        return extract_system(messages)

    def _summarize_messages(self, msgs: list[dict[str, Any]]) -> dict[str, Any]:
        """Summarize message roles and block types for safe logging."""
        summary: list[dict[str, Any]] = []
        for m in msgs:
            role = m.get("role")
            content = m.get("content")
            kinds: list[str] = []
            if isinstance(content, list):
                for b in content[:10]:  # cap for safety
                    if isinstance(b, dict):
                        kinds.append(str(b.get("type", "?")))
                    else:
                        kinds.append(type(b).__name__)
            else:
                kinds.append("text")
            summary.append({"role": role, "blocks": kinds[:10]})
        return {"count": len(msgs), "messages": summary}
