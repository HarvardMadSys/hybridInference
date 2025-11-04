"""SGLang local inference adapter.

This adapter integrates with a locally-running SGLang server for high-throughput
local inference. It supports both regular chat completions and streaming responses.

SGLang typically runs on http://localhost:30000 and exposes OpenAI-compatible
endpoints at /v1/chat/completions and /v1/completions.
"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any

from serving.stream import done_sentinel, make_final_usage_chunk
from serving.utils.logging import get_logger
from serving.utils.tokens import estimate_prompt_tokens, estimate_text_tokens

from .base import BaseAdapter, UsageInfo

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

logger = get_logger(__name__)


class SGLangAdapter(BaseAdapter):
    """Adapter for SGLang local inference engine.

    SGLang provides high-throughput local inference with features like:
    - RadixAttention for efficient KV cache management
    - Continuous batching for optimal GPU utilization
    - OpenAI-compatible API endpoints
    """

    async def chat_completion(
        self, messages: list[dict[str, Any]], **params: Any
    ) -> dict[str, Any]:
        """Execute a non-streaming chat completion request.

        Args:
            messages: Chat history in OpenAI-compatible format.
            **params: Sampling parameters (temperature, top_p, max_tokens, etc.).

        Returns:
            OpenAI-compatible response dictionary.
        """
        validated_params = self.validate_params(params)

        # Build SGLang endpoint
        base_url = self.config.base_url.rstrip("/")
        endpoint = f"{base_url}/v1/chat/completions"

        # Build payload - SGLang uses OpenAI-compatible format
        payload: dict[str, Any] = {
            "model": self.config.provider_model_id or self.config.id,
            "messages": messages,
            **validated_params,
        }

        # Add additional supported parameters
        if params.get("stop"):
            payload["stop"] = params["stop"]
        if params.get("seed") is not None and "seed" in self.config.supported_params:
            payload["seed"] = params["seed"]

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        # SGLang doesn't typically require authentication for local deployments
        # but support it if configured
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"

        logger.debug(f"[SGLang] Sending request to: {endpoint}")
        logger.debug(f"[SGLang] Payload: {json.dumps(payload, indent=2)}")

        try:
            data = await self.http.json_post_with_retry(
                endpoint, json=payload, headers=headers, timeout=None, retries=2
            )
        except Exception as e:
            logger.error(f"[SGLang] Request failed: {e}")
            raise

        # Extract response fields
        choices = data.get("choices") or []
        if choices:
            message = choices[0].get("message") or {}
            content = message.get("content", "")
            finish_reason = choices[0].get("finish_reason", "stop")
        else:
            content = ""
            finish_reason = "stop"

        # Extract usage information
        usage_payload = data.get("usage") or {}
        if usage_payload:
            usage = UsageInfo(
                prompt_tokens=int(usage_payload.get("prompt_tokens", 0) or 0),
                completion_tokens=int(usage_payload.get("completion_tokens", 0) or 0),
                total_tokens=int(usage_payload.get("total_tokens", 0) or 0),
            )
        else:
            # Estimate if not provided
            prompt_tokens = int(estimate_prompt_tokens(messages))
            completion_tokens = int(estimate_text_tokens(content))
            usage = UsageInfo(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=prompt_tokens + completion_tokens,
            )

        logger.debug(
            f"[SGLang] Response received: {len(content)} chars, "
            f"usage={usage.prompt_tokens}/{usage.completion_tokens} tokens"
        )

        return self.format_response(
            content=content,
            model=self.config.id,
            usage=usage,
            finish_reason=finish_reason,
        )

    async def stream_chat_completion(
        self, messages: list[dict[str, Any]], **params: Any
    ) -> AsyncGenerator[str, None]:
        """Execute a streaming chat completion request.

        Yields SSE-formatted chunks compatible with OpenAI clients.
        """
        validated_params = self.validate_params(params)

        # Build SGLang endpoint
        base_url = self.config.base_url.rstrip("/")
        endpoint = f"{base_url}/v1/chat/completions"

        # Build payload
        payload: dict[str, Any] = {
            "model": self.config.provider_model_id or self.config.id,
            "messages": messages,
            "stream": True,
            **validated_params,
        }

        # Add additional supported parameters
        if params.get("stop"):
            payload["stop"] = params["stop"]
        if params.get("seed") is not None and "seed" in self.config.supported_params:
            payload["seed"] = params["seed"]

        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }

        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"

        total_content = ""
        prompt_tokens_override: int | None = None
        finish_reason = "stop"
        line_count = 0

        logger.debug(f"[SGLang Stream] Starting stream to: {endpoint}")
        logger.debug(f"[SGLang Stream] Payload: {json.dumps(payload, indent=2)}")

        try:
            async for line in self.http.stream_post(endpoint, json=payload, headers=headers):
                line_count += 1

                # Log sampling for debugging
                if line_count <= 10 or line_count % 50 == 0:
                    logger.debug(f"[SGLang LINE {line_count}] Raw: {line[:200]}")

                if not line.startswith("data: "):
                    continue

                if line == "data: [DONE]":
                    logger.debug(f"[SGLang Stream] Received [DONE] at line {line_count}")

                    # Emit final usage chunk
                    final_usage = make_final_usage_chunk(
                        model=self.config.id,
                        messages=messages,
                        total_content=total_content,
                        prompt_tokens_override=prompt_tokens_override,
                        finish_reason=finish_reason,
                    )
                    logger.debug(f"[SGLang Yield Final] Usage chunk")
                    yield final_usage

                    # Emit done sentinel
                    yield done_sentinel()
                    logger.debug(
                        f"[SGLang Stream Complete] "
                        f"lines={line_count}, chars={len(total_content)}"
                    )
                    break

                try:
                    chunk_data = json.loads(line[6:])
                except json.JSONDecodeError as e:
                    logger.debug(f"[SGLang LINE {line_count}] JSON decode error: {e}")
                    continue

                # Capture usage if provided
                if chunk_data.get("usage"):
                    usage_data = chunk_data["usage"]
                    pt = usage_data.get("prompt_tokens")
                    if isinstance(pt, int):
                        prompt_tokens_override = pt

                choices = chunk_data.get("choices") or []
                if not choices:
                    continue

                choice = choices[0]
                finish_reason = choice.get("finish_reason") or finish_reason
                delta = choice.get("delta") or {}

                # Extract content from delta
                content = delta.get("content")
                if content:
                    total_content += content
                    chunk_output = self.format_stream_chunk(content, self.config.id)
                    if line_count <= 10:
                        logger.debug(
                            f"[SGLang Yield {line_count}] Content ({len(content)} chars)"
                        )
                    yield chunk_output

        except Exception as e:
            logger.error(f"[SGLang Stream] Error during streaming: {e}")
            raise

        if line_count == 0:
            logger.warning("[SGLang Stream] No lines received from stream")
