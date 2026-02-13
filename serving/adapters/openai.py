"""Azure OpenAI adapter for Chat Completions API.

This adapter forwards chat completion requests to Azure OpenAI services,
handling Azure-specific authentication (api-key header), API versioning,
and reasoning model parameter requirements (GPT-5/o1/o3).

Streaming uses Server-Sent Events (SSE) in the same format as other
adapters for consistency.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from serving.stream import done_sentinel, make_final_usage_chunk

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
from serving.utils.tokens import estimate_prompt_tokens, estimate_text_tokens

from .base import BaseAdapter, UsageInfo


class OpenAIAdapter(BaseAdapter):  # type: ignore[no-any-unimported]
    """Adapter for Azure OpenAI GPT models over Chat Completions API.

    This adapter handles Azure OpenAI-specific requirements:
    - Authentication via 'api-key' header (not Bearer token)
    - API versioning via query parameter (api-version=2024-12-01-preview)
    - Reasoning models (GPT-5/o1/o3) require 'max_completion_tokens' and
      do not support temperature, top_p, etc.
    - Model selection is handled by deployment, not 'model' parameter

    It passes through optional parameters such as tools, tool_choice, and
    response_format when declared supported by the model configuration.
    """

    def _fix_tool_call_message_order(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Fix message ordering for OpenAI API compatibility.

        Some clients (e.g., Codex CLI) send interleaved messages where:
        1. Preamble assistant messages appear between tool_calls and tool response
        2. User messages appear between tool_calls and tool response

        OpenAI/Azure OpenAI requires that ``role="tool"`` messages immediately follow the
        assistant message containing ``tool_calls``.

        This function:
        1. Merges consecutive assistant messages without tool_calls into the preceding one
        2. Reorders tool messages to immediately follow their corresponding assistant(tool_calls)
        """
        from serving.utils.logging import get_logger

        logger = get_logger(__name__)

        if not messages or len(messages) < 2:
            return messages

        def _merge_content(existing: Any | None, preamble: Any | None) -> Any | None:
            """Merge assistant ``content`` fields conservatively."""
            if preamble is None:
                return existing
            if isinstance(preamble, str) and not preamble.strip():
                return existing
            if isinstance(preamble, list) and not preamble:
                return existing

            if existing is None:
                return preamble
            if isinstance(existing, str) and not existing.strip():
                existing = ""
            if isinstance(existing, list) and not existing:
                existing = []

            if isinstance(existing, str) and isinstance(preamble, str):
                if existing and preamble:
                    return f"{existing}\n{preamble}"
                return preamble or existing

            if isinstance(existing, list) and isinstance(preamble, list):
                return [*existing, *preamble]

            if isinstance(existing, list) and isinstance(preamble, str):
                return [*existing, {"type": "text", "text": preamble}]

            if isinstance(existing, str) and isinstance(preamble, list):
                if not existing:
                    return preamble
                return [{"type": "text", "text": existing}, *preamble]

            try:
                existing_str = json.dumps(existing, ensure_ascii=False)
            except TypeError:
                existing_str = str(existing)
            try:
                preamble_str = json.dumps(preamble, ensure_ascii=False)
            except TypeError:
                preamble_str = str(preamble)

            if existing_str and preamble_str:
                return f"{existing_str}\n{preamble_str}"
            return preamble_str or existing_str

        # Phase 1: Build tool_call_id -> tool_message mapping
        tool_msg_map: dict[str, dict[str, Any]] = {}
        for msg in messages:
            if msg.get("role") == "tool":
                tool_call_id = msg.get("tool_call_id")
                if tool_call_id:
                    tool_msg_map[tool_call_id] = msg

        # Phase 2: Process messages, merging assistant preambles and reordering tool messages
        result: list[dict[str, Any]] = []
        used_tool_ids: set[str] = set()
        i = 0

        while i < len(messages):
            msg = messages[i]
            role = msg.get("role")

            # Skip tool messages here - they'll be inserted after their corresponding assistant
            if role == "tool":
                i += 1
                continue

            if role == "assistant" and msg.get("tool_calls"):
                # Merge any following assistant preambles
                merged_msg = msg.copy()
                merged_count = 0
                j = i + 1
                while j < len(messages):
                    next_msg = messages[j]
                    next_role = next_msg.get("role")
                    # Stop if we hit a non-assistant or an assistant with tool_calls
                    if next_role != "assistant" or next_msg.get("tool_calls"):
                        break
                    merged_msg["content"] = _merge_content(
                        merged_msg.get("content"), next_msg.get("content")
                    )
                    merged_count += 1
                    j += 1

                if merged_count > 0:
                    logger.debug(
                        "[Azure OpenAI] Merged %d assistant preamble message(s) into tool_calls message",
                        merged_count,
                    )

                result.append(merged_msg)

                # Insert corresponding tool messages immediately after
                tool_calls = merged_msg.get("tool_calls", [])
                inserted_count = 0
                for tc in tool_calls:
                    tc_id = tc.get("id")
                    if tc_id and tc_id in tool_msg_map and tc_id not in used_tool_ids:
                        result.append(tool_msg_map[tc_id])
                        used_tool_ids.add(tc_id)
                        inserted_count += 1

                if inserted_count > 0:
                    logger.debug(
                        "[Azure OpenAI] Reordered %d tool message(s) to follow tool_calls",
                        inserted_count,
                    )

                i = j if merged_count > 0 else i + 1
                continue

            result.append(msg)
            i += 1

        # Phase 3: Append any orphaned tool messages at the end (shouldn't happen normally)
        for tool_id, tool_msg in tool_msg_map.items():
            if tool_id not in used_tool_ids:
                logger.warning(
                    "[Azure OpenAI] Orphaned tool message with id=%s appended at end",
                    tool_id,
                )
                result.append(tool_msg)

        return result

    async def chat_completion(
        self, messages: list[dict[str, Any]], **params: Any
    ) -> dict[str, Any]:
        """Execute a non-streaming chat completion request.

        Args:
            messages: Chat history in OpenAI-compatible format.
            **params: Sampling and feature parameters (temperature, top_p,
                max_tokens, stop, tools, tool_choice, response_format, etc.).

        Returns:
            OpenAI-compatible response dictionary.
        """
        validated_params = self.validate_params(params)

        # Initialize logger
        from serving.utils.logging import get_logger

        logger = get_logger(__name__)

        # Fix message ordering for OpenAI API compatibility
        fixed_messages = self._fix_tool_call_message_order(messages)

        # Build Azure OpenAI endpoint with api-version
        base_url = self.config.base_url.rstrip("/")
        endpoint = f"{base_url}/chat/completions?api-version=2024-12-01-preview"

        # Build payload - Azure OpenAI does not require 'model' field (determined by deployment)
        payload: dict[str, Any] = {
            "messages": fixed_messages,
            **validated_params,
        }

        # Azure OpenAI reasoning models (GPT-5/o1/o3) require 'max_completion_tokens'
        if "max_tokens" in payload:
            payload["max_completion_tokens"] = payload.pop("max_tokens")

        # Reasoning models have strict parameter requirements
        # GPT-5/o1/o3 do not support: temperature, top_p, frequency_penalty, presence_penalty
        # NOTE: tools ARE supported by reasoning models
        unsupported_params = [
            "temperature",
            "top_p",
            "frequency_penalty",
            "presence_penalty",
            "top_k",
            "min_p",
            "stop",
        ]
        for param in unsupported_params:
            payload.pop(param, None)

        logger.debug("[Azure OpenAI] Cleaned payload for reasoning model compatibility")

        # Add tools support if configured
        if params.get("tools") and self.config.supports_tools:
            payload["tools"] = params["tools"]
            logger.debug(f"[Azure OpenAI] Added {len(params['tools'])} tools to payload")
            if params.get("tool_choice") is not None:
                payload["tool_choice"] = params["tool_choice"]

        # Add structured output support if configured
        if params.get("response_format") and self.config.supports_structured_output:
            payload["response_format"] = params["response_format"]

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        # Azure OpenAI uses 'api-key' header for authentication
        if self.config.api_key:
            headers["api-key"] = self.config.api_key

        # Debug logging
        logger.debug(f"[Azure OpenAI] Endpoint: {endpoint}")
        logger.debug(f"[Azure OpenAI] Payload: {json.dumps(payload, indent=2)}")

        data = await self.http.json_post_with_retry(
            endpoint, json=payload, headers=headers, timeout=None, retries=3
        )

        # Extract primary fields from OpenAI response.
        choices = data.get("choices") or []
        if choices:
            message = choices[0].get("message") or {}
            content = message.get("content", "")
            tool_calls = message.get("tool_calls")
            finish_reason = choices[0].get("finish_reason", "stop")
        else:
            content = ""
            tool_calls = None
            finish_reason = "stop"

        # Use provided usage if available; otherwise estimate conservatively.
        usage_payload = data.get("usage") or {}
        if usage_payload:
            # Extract reasoning tokens from completion_tokens_details
            completion_details = usage_payload.get("completion_tokens_details", {})
            reasoning_tokens = int(completion_details.get("reasoning_tokens", 0) or 0)

            # Extract cached prompt tokens and compute non-cached prompt tokens
            prompt_details = usage_payload.get("prompt_tokens_details", {})
            cached_tokens = int(prompt_details.get("cached_tokens", 0) or 0)
            prompt_tokens_total = int(usage_payload.get("prompt_tokens", 0) or 0)
            prompt_tokens_non_cached = max(0, prompt_tokens_total - cached_tokens)

            usage = UsageInfo(
                prompt_tokens=prompt_tokens_non_cached,
                completion_tokens=int(usage_payload.get("completion_tokens", 0) or 0),
                total_tokens=int(usage_payload.get("total_tokens", 0) or 0),
                reasoning_tokens=reasoning_tokens,
                cache_read_tokens=cached_tokens,
            )
        else:
            prompt_tokens = int(estimate_prompt_tokens(fixed_messages))
            completion_tokens = int(estimate_text_tokens(content))
            usage = UsageInfo(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=prompt_tokens + completion_tokens,
            )

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

        Yields SSE-formatted chunks compatible with OpenAI clients. A final
        synthetic usage chunk is emitted based on provider usage (if present)
        or local token estimates.
        """
        validated_params = self.validate_params(params)

        # Initialize logger
        from serving.utils.logging import get_logger

        logger = get_logger(__name__)

        # Fix message ordering for OpenAI API compatibility
        fixed_messages = self._fix_tool_call_message_order(messages)

        # Build Azure OpenAI endpoint with api-version
        base_url = self.config.base_url.rstrip("/")
        endpoint = f"{base_url}/chat/completions?api-version=2024-12-01-preview"

        # Build payload - Azure OpenAI does not require 'model' field (determined by deployment)
        payload: dict[str, Any] = {
            "messages": fixed_messages,
            "stream": True,
            **validated_params,
        }

        # Azure OpenAI reasoning models (GPT-5/o1/o3) require 'max_completion_tokens'
        if "max_tokens" in payload:
            payload["max_completion_tokens"] = payload.pop("max_tokens")

        # Reasoning models have strict parameter requirements
        # GPT-5/o1/o3 do not support: temperature, top_p, frequency_penalty, presence_penalty
        # NOTE: tools ARE supported by reasoning models
        unsupported_params = [
            "temperature",
            "top_p",
            "frequency_penalty",
            "presence_penalty",
            "top_k",
            "min_p",
            "stop",
        ]
        for param in unsupported_params:
            payload.pop(param, None)

        # Add stream_options for proper usage tracking
        payload["stream_options"] = {"include_usage": True}

        logger.debug("[Azure OpenAI] Cleaned payload for reasoning model compatibility")

        # Add tools support if configured
        if params.get("tools") and self.config.supports_tools:
            payload["tools"] = params["tools"]
            logger.debug(f"[Azure OpenAI Stream] Added {len(params['tools'])} tools to payload")
            if params.get("tool_choice") is not None:
                payload["tool_choice"] = params["tool_choice"]

        # Add structured output support if configured
        if params.get("response_format") and self.config.supports_structured_output:
            payload["response_format"] = params["response_format"]

        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }

        # Azure OpenAI uses 'api-key' header for authentication
        if self.config.api_key:
            headers["api-key"] = self.config.api_key

        total_content = ""
        prompt_tokens_override: int | None = None
        final_usage_payload: dict[str, Any] | None = None
        finish_reason = "stop"
        line_count = 0

        logger.debug(f"[Azure OpenAI Stream] Starting stream to: {endpoint}")
        logger.debug(f"[Azure OpenAI Stream] Payload: {json.dumps(payload, indent=2)}")

        async for line in self.http.stream_post(endpoint, json=payload, headers=headers):
            line_count += 1
            # Log every line for first 10, then sample every 10th
            if line_count <= 10 or line_count % 10 == 0:
                logger.debug(f"[Azure OpenAI LINE {line_count}] Raw: {line[:300]}")

            if not line.startswith("data: "):
                logger.debug(
                    f"[Azure OpenAI LINE {line_count}] Skipping non-data line: {line[:100]}"
                )
                continue

            if line == "data: [DONE]":
                logger.debug(f"[Azure OpenAI Stream] Received [DONE] at line {line_count}")

                # Emit a final usage packet and stream terminator for consistency.
                if final_usage_payload:
                    completion_details = final_usage_payload.get("completion_tokens_details", {})
                    reasoning_tokens = int(completion_details.get("reasoning_tokens", 0) or 0)
                    prompt_details = final_usage_payload.get("prompt_tokens_details", {})
                    cached_tokens = int(prompt_details.get("cached_tokens", 0) or 0)
                    prompt_tokens_total = int(final_usage_payload.get("prompt_tokens", 0) or 0)
                    prompt_tokens_non_cached = max(0, prompt_tokens_total - cached_tokens)
                    completion_tokens_total = int(
                        final_usage_payload.get("completion_tokens", 0) or 0
                    )

                    final_usage = make_final_usage_chunk(
                        model=self.config.id,
                        messages=fixed_messages,
                        total_content=total_content,
                        prompt_tokens_override=prompt_tokens_non_cached,
                        completion_tokens_override=completion_tokens_total,
                        finish_reason=finish_reason,
                        provider=self.config.provider,
                        base_url=base_url,
                        reasoning_tokens=reasoning_tokens,
                        cache_read_tokens=cached_tokens,
                    )
                else:
                    final_usage = make_final_usage_chunk(
                        model=self.config.id,
                        messages=fixed_messages,
                        total_content=total_content,
                        prompt_tokens_override=prompt_tokens_override,
                        finish_reason=finish_reason,
                        provider=self.config.provider,
                        base_url=base_url,
                    )
                logger.debug(
                    f"[Azure OpenAI Yield Final] Yielding final usage chunk: {final_usage[:200]}"
                )
                yield final_usage

                done_msg = done_sentinel()
                logger.debug(f"[Azure OpenAI Yield Done] Yielding done sentinel: {done_msg[:50]}")
                yield done_msg

                # Log streaming completion summary
                logger.debug(
                    f"[Azure OpenAI Stream Complete] "
                    f"model={self.config.id}, "
                    f"prompt_tokens={prompt_tokens_override or 'estimated'}, "
                    f"total_chars={len(total_content)}, "
                    f"finish_reason={finish_reason}, "
                    f"total_lines={line_count}"
                )
                break

            try:
                chunk_data = json.loads(line[6:])
                if line_count <= 5:
                    logger.debug(
                        f"[Azure OpenAI Parse {line_count}] Parsed chunk: {json.dumps(chunk_data)[:200]}"
                    )
            except json.JSONDecodeError as e:
                logger.debug(
                    f"[Azure OpenAI LINE {line_count}] JSON decode error: {e}, line: {line[:100]}"
                )
                continue

            # Capture upstream usage if provided (usually present in final chunk)
            if chunk_data.get("usage"):
                final_usage_payload = chunk_data["usage"]
                pt = final_usage_payload.get("prompt_tokens")
                if isinstance(pt, int):
                    prompt_tokens_override = pt
                    logger.debug(
                        f"[Azure OpenAI Usage {line_count}] Captured upstream usage with prompt_tokens={pt}"
                    )

            choices = chunk_data.get("choices") or []
            if not choices:
                logger.debug(f"[Azure OpenAI LINE {line_count}] No choices in chunk")
                continue

            choice = choices[0]
            finish_reason = choice.get("finish_reason") or finish_reason
            delta = choice.get("delta") or {}

            # Log delta even if no content
            if line_count <= 5:
                logger.debug(f"[Azure OpenAI Delta {line_count}] Delta: {delta}")

            # Check if this chunk has content (role is handled by completions.py)
            content = delta.get("content")

            if content:
                total_content += content
                chunk_output = self.format_stream_chunk(content, self.config.id)
                if line_count <= 10:
                    logger.debug(
                        f"[Azure OpenAI Yield {line_count}] Yielding content ({len(content)} chars): {content[:100]}"
                    )
                    logger.debug(
                        f"[Azure OpenAI Yield {line_count}] Formatted output: {chunk_output[:200]}"
                    )
                yield chunk_output

            # Forward tool_calls delta if present (for GPT-5 and other tool-supporting models)
            tool_calls_delta = delta.get("tool_calls")
            if tool_calls_delta:
                # Pass through the chunk but replace model ID with our logical model ID
                chunk_copy = chunk_data.copy()
                chunk_copy["model"] = self.config.id
                chunk_output = f"data: {json.dumps(chunk_copy)}\n\n"
                if line_count <= 10:
                    logger.debug(
                        f"[Azure OpenAI Yield Tools {line_count}] Yielding tool_calls delta: {chunk_output[:200]}"
                    )
                yield chunk_output

        # Log if we exit without [DONE]
        if line_count == 0:
            logger.debug("[Azure OpenAI Stream] No lines received from stream!")
        else:
            logger.debug(
                f"[Azure OpenAI Stream End] Total lines: {line_count}, Total content chars: {len(total_content)}"
            )
