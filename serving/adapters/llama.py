"""Llama API adapter for OpenAI-compatible interface."""

import json
from collections.abc import AsyncGenerator
from typing import Any

from serving.stream import done_sentinel, make_final_usage_chunk
from serving.utils.tokens import estimate_prompt_tokens, estimate_text_tokens

from .base import BaseAdapter, UsageInfo


class LlamaAdapter(BaseAdapter):  # type: ignore[no-any-unimported]
    """Adapter for Llama API using OpenAI-compatible endpoints."""

    async def chat_completion(
        self, messages: list[dict[str, Any]], **params: Any
    ) -> dict[str, Any]:
        """Send chat completion request to Llama API."""
        validated_params = self.validate_params(params)

        # Use provider_model_id if specified, otherwise fall back to id
        model_id = self.config.provider_model_id or self.config.id

        # Llama API requires a "developer" role message for proper operation
        # Add one if not present, converting the first user/system message if needed
        llama_messages = []
        has_developer = any(msg.get("role") == "developer" for msg in messages)

        if not has_developer and messages:
            # Add a developer message at the start
            llama_messages.append({"role": "developer", "content": "You are a helpful assistant."})
            llama_messages.extend(messages)
        else:
            llama_messages = messages

        payload = {"model": model_id, "messages": llama_messages, **validated_params}

        # Llama API specific parameters
        if "top_k" in params and "top_k" in self.config.supported_params:
            payload["top_k"] = params["top_k"]

        if "min_p" in params and "min_p" in self.config.supported_params:
            payload["min_p"] = params["min_p"]

        if "frequency_penalty" in params and "frequency_penalty" in self.config.supported_params:
            payload["frequency_penalty"] = params["frequency_penalty"]

        if "presence_penalty" in params and "presence_penalty" in self.config.supported_params:
            payload["presence_penalty"] = params["presence_penalty"]

        # Tool calling support for Llama 3.3
        if params.get("tools") and self.config.supports_tools:
            payload["tools"] = params["tools"]
            if params.get("tool_choice"):
                payload["tool_choice"] = params["tool_choice"]

        # Structured output support
        if params.get("response_format") and self.config.supports_structured_output:
            payload["response_format"] = params["response_format"]

        headers = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"

        # Use the /inference endpoint for Llama API
        # Use standard OpenAI-compatible endpoint for Llama
        url = f"{self.config.base_url}/chat/completions"

        data = await self.http.json_post_with_retry(url, json=payload, headers=headers)

        # Check if usage is provided, otherwise estimate
        usage = None
        if "usage" in data:
            usage = UsageInfo(
                prompt_tokens=data["usage"].get("prompt_tokens", 0),
                completion_tokens=data["usage"].get("completion_tokens", 0),
                total_tokens=data["usage"].get("total_tokens", 0),
            )
        else:
            # Estimate tokens if not provided
            content = data.get("content", "")
            prompt_tokens = estimate_prompt_tokens(messages)
            completion_tokens = estimate_text_tokens(content)
            usage = UsageInfo(
                prompt_tokens=int(prompt_tokens),
                completion_tokens=int(completion_tokens),
                total_tokens=int(prompt_tokens + completion_tokens),
            )

        # Extract tool calls if present
        tool_calls = None
        if "tool_calls" in data:
            tool_calls = data["tool_calls"]

        # Extract content from OpenAI-compatible response format
        content = ""
        if "choices" in data and data["choices"] and "message" in data["choices"][0]:
            content = data["choices"][0]["message"].get("content", "")
        else:
            # Fallback to direct content field for compatibility
            content = data.get("content", "")

        # Format response to OpenAI standard
        response = self.format_response(
            content=content,
            model=self.config.id,
            usage=usage,
            tool_calls=tool_calls,
            finish_reason=data.get("stop_reason", "stop"),
        )
        return response  # type: ignore[no-any-return]

    async def stream_chat_completion(
        self, messages: list[dict[str, Any]], **params: Any
    ) -> AsyncGenerator[str, None]:
        """Stream chat completion response from Llama API."""
        validated_params = self.validate_params(params)

        # Use provider_model_id if specified, otherwise fall back to id
        model_id = self.config.provider_model_id or self.config.id

        # Llama API requires a "developer" role message for proper operation
        # Add one if not present
        llama_messages = []
        has_developer = any(msg.get("role") == "developer" for msg in messages)

        if not has_developer and messages:
            # Add a developer message at the start
            llama_messages.append({"role": "developer", "content": "You are a helpful assistant."})
            llama_messages.extend(messages)
        else:
            llama_messages = messages

        payload = {
            "model": model_id,
            "messages": llama_messages,
            "stream": True,
            **validated_params,
        }

        # Add Llama-specific parameters
        for param in ["top_k", "min_p", "frequency_penalty", "presence_penalty"]:
            if param in params and param in self.config.supported_params:
                payload[param] = params[param]

        if params.get("tools") and self.config.supports_tools:
            payload["tools"] = params["tools"]
            if params.get("tool_choice"):
                payload["tool_choice"] = params["tool_choice"]

        if params.get("response_format") and self.config.supports_structured_output:
            payload["response_format"] = params["response_format"]

        headers = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"

        # Use standard OpenAI-compatible endpoint for Llama
        url = f"{self.config.base_url}/chat/completions"

        total_content = ""
        prompt_tokens_override: int | None = None
        finish_reason = "stop"

        async for line in self.http.stream_post(url, json=payload, headers=headers):
            if not line.startswith("data: "):
                continue
            if line == "data: [DONE]":
                yield make_final_usage_chunk(
                    model=self.config.id,
                    messages=messages,
                    total_content=total_content,
                    prompt_tokens_override=prompt_tokens_override,
                    finish_reason=finish_reason,
                )
                yield done_sentinel()
                break
            try:
                chunk_data = json.loads(line[6:])
            except json.JSONDecodeError:
                continue

            if "usage" in chunk_data:
                prompt_tokens_override = chunk_data["usage"].get(
                    "prompt_tokens", prompt_tokens_override
                )

            choices = chunk_data.get("choices") or []
            if choices:
                choice = choices[0]
                finish_reason = choice.get("finish_reason") or finish_reason
                delta = choice.get("delta") or {}
                content = delta.get("content")
                if content:
                    total_content += content
                    yield self.format_stream_chunk(content, self.config.id)
                continue
