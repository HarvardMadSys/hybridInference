"""OpenAI adapter for OpenAI-compatible Chat Completions API.

This adapter forwards OpenAI-style chat completion requests to the
OpenAI API (or any service exposing an OpenAI-compatible endpoint), and
normalizes responses to the shared OpenAI schema used across the
gateway. Streaming uses Server-Sent Events (SSE) in the same format as
other adapters for consistency.
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from typing import Any
import time

from serving.stream import done_sentinel, make_final_usage_chunk
from serving.utils.tokens import estimate_prompt_tokens, estimate_text_tokens

from .base import BaseAdapter, UsageInfo


class OpenAIAdapter(BaseAdapter):  # type: ignore[no-any-unimported]
    """Adapter for OpenAI GPT models over Chat Completions API.

    This adapter assumes a standard OpenAI-compatible JSON request/response
    contract. It does not inject provider-specific roles or fields, and it
    passes through optional parameters such as tools, tool_choice, and
    response_format when declared supported by the model configuration.
    """

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

        # Prefer provider_model_id if set; otherwise use the logical id.
        model_id = self.config.provider_model_id or self.config.id

        # Build endpoint URL with optional api-version query param
        base_url = self.config.base_url.rstrip("/")
        endpoint = f"{base_url}/chat/completions"

        # Check if this is XHS runway API
        is_xhs_runway = "runway.devops" in base_url or "xiaohongshu" in base_url

        # Initialize logger early for debugging
        from serving.utils.logging import get_logger

        logger = get_logger(__name__)

        # Build payload in OpenAI format.
        payload: dict[str, Any] = {
            "messages": messages,
            **validated_params,
        }

        # XHS runway API doesn't accept 'model' parameter, other APIs require it
        if not is_xhs_runway:
            payload["model"] = model_id

        # XHS runway GPT-5 API requires 'max_completion_tokens' instead of 'max_tokens'
        # This is true for OpenAI's reasoning models (o1, o3, gpt-5)
        if is_xhs_runway and "max_tokens" in payload:
            payload["max_completion_tokens"] = payload.pop("max_tokens")

        # XHS runway GPT-5 (reasoning model) has strict parameter requirements
        if is_xhs_runway:
            # Remove unsupported parameters for reasoning models
            # GPT-5/o1/o3-style models don't support: temperature, top_p, frequency_penalty, presence_penalty
            # NOTE: tools ARE supported by GPT-5, so we keep them
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

            # Add api-version query param
            endpoint = f"{endpoint}?api-version=2024-12-01-preview"

            logger.warning("🧹 [XHS RUNWAY] Cleaned payload for reasoning model")

            # Add tools support for GPT-5 if configured
            if params.get("tools") and self.config.supports_tools:
                payload["tools"] = params["tools"]
                logger.warning(f"🔧 [XHS RUNWAY] Added {len(params['tools'])} tools to payload")
                if params.get("tool_choice") is not None:
                    payload["tool_choice"] = params["tool_choice"]

            if params.get("response_format") and self.config.supports_structured_output:
                payload["response_format"] = params["response_format"]
        else:
            # Standard OpenAI API - add optional features
            if params.get("tools") and self.config.supports_tools:
                payload["tools"] = params["tools"]
                if params.get("tool_choice") is not None:
                    payload["tool_choice"] = params["tool_choice"]

            if params.get("response_format") and self.config.supports_structured_output:
                payload["response_format"] = params["response_format"]

            # Common OpenAI sampling knobs when declared supported by config.
            for opt in (
                "frequency_penalty",
                "presence_penalty",
                "top_k",
                "min_p",
                "seed",
            ):
                if opt in params and opt in self.config.supported_params:
                    payload[opt] = params[opt]

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        # Debug: log the payload for XHS runway API
        if is_xhs_runway:
            logger.warning(f"🚀 [XHS RUNWAY] Endpoint: {endpoint}")
            logger.warning(f"🚀 [XHS RUNWAY] Payload: {json.dumps(payload, indent=2)}")
        else:
            logger.warning(f"🌐 [OPENAI] Endpoint: {endpoint}")
            logger.warning(f"🌐 [OPENAI] Payload: {json.dumps(payload, indent=2)}")

        # XHS runway API uses 'api-key' header instead of 'Authorization: Bearer'
        if self.config.api_key:
            if "runway.devops" in base_url or "xiaohongshu" in base_url:
                headers["api-key"] = self.config.api_key
            else:
                headers["Authorization"] = f"Bearer {self.config.api_key}"

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
            prompt_tokens = int(estimate_prompt_tokens(messages))
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

        model_id = self.config.provider_model_id or self.config.id

        # Build endpoint URL with optional api-version query param
        base_url = self.config.base_url.rstrip("/")
        endpoint = f"{base_url}/chat/completions"

        # Check if this is XHS runway API
        is_xhs_runway = "runway.devops" in base_url or "xiaohongshu" in base_url

        # Initialize logger early for debugging
        from serving.utils.logging import get_logger

        logger = get_logger(__name__)

        payload: dict[str, Any] = {
            "messages": messages,
            "stream": True,
            **validated_params,
        }

        # XHS runway API doesn't accept 'model' parameter, other APIs require it
        if not is_xhs_runway:
            payload["model"] = model_id

        # XHS runway GPT-5 API requires 'max_completion_tokens' instead of 'max_tokens'
        if is_xhs_runway and "max_tokens" in payload:
            payload["max_completion_tokens"] = payload.pop("max_tokens")

        # XHS runway GPT-5 (reasoning model) has strict parameter requirements
        if is_xhs_runway:
            # Remove unsupported parameters for reasoning models
            # GPT-5/o1/o3-style models don't support: temperature, top_p, frequency_penalty, presence_penalty
            # NOTE: tools ARE supported by GPT-5, so we keep them
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

            # Add api-version query param
            endpoint = f"{endpoint}?api-version=2024-12-01-preview"
            # Add stream_options for proper usage tracking
            payload["stream_options"] = {"include_usage": True}

            logger.warning("🧹 [XHS RUNWAY] Cleaned payload for reasoning model")

            # Add tools support for GPT-5 if configured
            if params.get("tools") and self.config.supports_tools:
                payload["tools"] = params["tools"]
                logger.warning(f"🔧 [XHS RUNWAY STREAM] Added {len(params['tools'])} tools to payload")
                if params.get("tool_choice") is not None:
                    payload["tool_choice"] = params["tool_choice"]

            if params.get("response_format") and self.config.supports_structured_output:
                payload["response_format"] = params["response_format"]
        else:
            # Standard OpenAI API - add optional features
            if params.get("tools") and self.config.supports_tools:
                payload["tools"] = params["tools"]
                if params.get("tool_choice") is not None:
                    payload["tool_choice"] = params["tool_choice"]

            if params.get("response_format") and self.config.supports_structured_output:
                payload["response_format"] = params["response_format"]

            for opt in (
                "frequency_penalty",
                "presence_penalty",
                "top_k",
                "min_p",
                "seed",
            ):
                if opt in params and opt in self.config.supported_params:
                    payload[opt] = params[opt]

        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }

        # XHS runway API uses 'api-key' header instead of 'Authorization: Bearer'
        if self.config.api_key:
            if "runway.devops" in base_url or "xiaohongshu" in base_url:
                headers["api-key"] = self.config.api_key
            else:
                headers["Authorization"] = f"Bearer {self.config.api_key}"

        total_content = ""
        prompt_tokens_override: int | None = None
        final_usage_payload: dict[str, Any] | None = None
        finish_reason = "stop"
        line_count = 0

        logger.warning(f"🌊 [OPENAI STREAM] Starting stream to: {endpoint}")
        logger.warning(f"🌊 [OPENAI STREAM] Payload: {json.dumps(payload, indent=2)}")

        async for line in self.http.stream_post(endpoint, json=payload, headers=headers):
            line_count += 1
            # Log every line for first 10, then sample every 10th
            if line_count <= 10 or line_count % 10 == 0:
                logger.warning(f"📨 [OPENAI LINE {line_count}] Raw: {line[:300]}")

            if not line.startswith("data: "):
                logger.warning(f"⚠️ [OPENAI LINE {line_count}] Skipping non-data line: {line[:100]}")
                continue

            if line == "data: [DONE]":
                logger.warning(f"🏁 [OPENAI STREAM] Received [DONE] at line {line_count}")

                # Emit a final usage packet and stream terminator for consistency.
                if final_usage_payload:
                    completion_details = final_usage_payload.get(
                        "completion_tokens_details", {}
                    )
                    reasoning_tokens = int(
                        completion_details.get("reasoning_tokens", 0) or 0
                    )
                    prompt_details = final_usage_payload.get("prompt_tokens_details", {})
                    cached_tokens = int(prompt_details.get("cached_tokens", 0) or 0)
                    prompt_tokens_total = int(
                        final_usage_payload.get("prompt_tokens", 0) or 0
                    )
                    prompt_tokens_non_cached = max(
                        0, prompt_tokens_total - cached_tokens
                    )

                    usage_obj: dict[str, int] = {
                        "prompt_tokens": prompt_tokens_non_cached,
                        "completion_tokens": int(
                            final_usage_payload.get("completion_tokens", 0) or 0
                        ),
                        "total_tokens": int(
                            final_usage_payload.get("total_tokens", 0) or 0
                        ),
                    }
                    if reasoning_tokens > 0:
                        usage_obj["reasoning_tokens"] = reasoning_tokens
                    if cached_tokens > 0:
                        usage_obj["cache_read_tokens"] = cached_tokens

                    final_chunk = {
                        "id": f"chatcmpl-{int(time.time() * 1000)}",
                        "object": "chat.completion.chunk",
                        "created": int(time.time()),
                        "model": self.config.id,
                        "choices": [
                            {"index": 0, "delta": {}, "finish_reason": finish_reason}
                        ],
                        "usage": usage_obj,
                        # Include routing info so server can attach pricing for DB logging
                        "_routing": {
                            "provider": self.config.provider,
                            "base_url": base_url,
                        },
                    }
                    final_usage = f"data: {json.dumps(final_chunk)}\n\n"
                else:
                    final_usage = make_final_usage_chunk(
                        model=self.config.id,
                        messages=messages,
                        total_content=total_content,
                        prompt_tokens_override=prompt_tokens_override,
                        finish_reason=finish_reason,
                    )
                logger.warning(
                    f"📤 [OPENAI YIELD FINAL] Yielding final usage chunk: {final_usage[:200]}"
                )
                yield final_usage

                done_msg = done_sentinel()
                logger.warning(f"📤 [OPENAI YIELD DONE] Yielding done sentinel: {done_msg[:50]}")
                yield done_msg

                # Log streaming completion summary
                logger.warning(
                    f"✅ [OPENAI STREAM COMPLETE] "
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
                    logger.warning(
                        f"🔍 [OPENAI PARSE {line_count}] Parsed chunk: {json.dumps(chunk_data)[:200]}"
                    )
            except json.JSONDecodeError as e:
                logger.warning(
                    f"⚠️ [OPENAI LINE {line_count}] JSON decode error: {e}, line: {line[:100]}"
                )
                continue

            # Capture upstream usage if provided (usually present in final chunk)
            if chunk_data.get("usage"):
                final_usage_payload = chunk_data["usage"]
                pt = final_usage_payload.get("prompt_tokens")
                if isinstance(pt, int):
                    prompt_tokens_override = pt
                    logger.warning(
                        f"📊 [OPENAI USAGE {line_count}] Captured upstream usage with prompt_tokens={pt}"
                    )

            choices = chunk_data.get("choices") or []
            if not choices:
                logger.warning(f"⚠️ [OPENAI LINE {line_count}] No choices in chunk")
                continue

            choice = choices[0]
            finish_reason = choice.get("finish_reason") or finish_reason
            delta = choice.get("delta") or {}

            # Log delta even if no content
            if line_count <= 5:
                logger.warning(f"🔍 [OPENAI DELTA {line_count}] Delta: {delta}")

            # Check if this chunk has content (role is handled by completions.py)
            content = delta.get("content")

            if content:
                total_content += content
                chunk_output = self.format_stream_chunk(content, self.config.id)
                if line_count <= 10:
                    logger.warning(
                        f"📤 [OPENAI YIELD {line_count}] Yielding content ({len(content)} chars): {content[:100]}"
                    )
                    logger.warning(
                        f"📤 [OPENAI YIELD {line_count}] Formatted output: {chunk_output[:200]}"
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
                    logger.warning(
                        f"📤 [OPENAI YIELD TOOLS {line_count}] Yielding tool_calls delta: {chunk_output[:200]}"
                    )
                yield chunk_output

        # Log if we exit without [DONE]
        if line_count == 0:
            logger.warning("⚠️ [OPENAI STREAM] No lines received from stream!")
        else:
            logger.warning(
                f"📊 [OPENAI STREAM END] Total lines: {line_count}, Total content chars: {len(total_content)}"
            )
