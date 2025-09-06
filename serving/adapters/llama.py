import json
from collections.abc import AsyncGenerator
from typing import Any

from serving.stream import done_sentinel, make_final_usage_chunk
from utils.tokens import estimate_prompt_tokens, estimate_text_tokens

from .base import BaseAdapter, UsageInfo


class LlamaAdapter(BaseAdapter):
    async def chat_completion(self, messages: list[dict[str, Any]], **params) -> dict[str, Any]:
        validated_params = self.validate_params(params)

        # Llama API expects the /inference endpoint
        payload = {"model": self.config.id, "messages": messages, **validated_params}

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
        url = f"{self.config.base_url}/inference"

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

        # Format response to OpenAI standard
        return self.format_response(
            content=data.get("content", ""),
            model=self.config.id,
            usage=usage,
            tool_calls=tool_calls,
            finish_reason=data.get("stop_reason", "stop"),
        )

    async def stream_chat_completion(
        self, messages: list[dict[str, Any]], **params
    ) -> AsyncGenerator[str, None]:
        validated_params = self.validate_params(params)

        payload = {
            "model": self.config.id,
            "messages": messages,
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

        url = f"{self.config.base_url}/inference"

        total_content = ""

        async for line in self.http.stream_post(url, json=payload, headers=headers):
            if not line.startswith("data: "):
                continue
            if line == "data: [DONE]":
                yield make_final_usage_chunk(
                    model=self.config.id,
                    messages=messages,
                    total_content=total_content,
                    finish_reason="stop",
                )
                yield done_sentinel()
                break
            try:
                chunk_data = json.loads(line[6:])
                if "content" in chunk_data:
                    content = chunk_data["content"]
                    total_content += content
                    yield self.format_stream_chunk(content, self.config.id)
            except json.JSONDecodeError:
                continue
