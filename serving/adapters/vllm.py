import json
from collections.abc import AsyncGenerator
from typing import Any

from serving.stream import done_sentinel, make_final_usage_chunk
from utils.tokens import estimate_prompt_tokens, estimate_text_tokens

from .base import BaseAdapter, UsageInfo


class VLLMAdapter(BaseAdapter):
    async def chat_completion(self, messages: list[dict[str, Any]], **params) -> dict[str, Any]:
        validated_params = self.validate_params(params)

        # Use provider-specific model id when provided
        model_id = self.config.provider_model_id or self.config.id

        payload = {"model": model_id, "messages": messages, **validated_params}

        if params.get("tools") and self.config.supports_tools:
            payload["tools"] = params["tools"]
            if params.get("tool_choice"):
                payload["tool_choice"] = params["tool_choice"]

        if params.get("response_format") and self.config.supports_structured_output:
            payload["response_format"] = params["response_format"]
            payload["guided_json"] = params["response_format"].get("schema")

        headers = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"

        data = await self.http.json_post_with_retry(
            f"{self.config.base_url}/chat/completions",
            json=payload,
            headers=headers,
        )

        usage = None
        if "usage" in data:
            usage = UsageInfo(
                prompt_tokens=data["usage"].get("prompt_tokens", 0),
                completion_tokens=data["usage"].get("completion_tokens", 0),
                total_tokens=data["usage"].get("total_tokens", 0),
            )
        else:
            content = data["choices"][0]["message"]["content"]
            prompt_tokens = estimate_prompt_tokens(messages)
            completion_tokens = estimate_text_tokens(content)
            usage = UsageInfo(
                prompt_tokens=int(prompt_tokens),
                completion_tokens=int(completion_tokens),
                total_tokens=int(prompt_tokens + completion_tokens),
            )

        tool_calls = None
        if "tool_calls" in data["choices"][0]["message"]:
            tool_calls = data["choices"][0]["message"]["tool_calls"]

        return self.format_response(
            content=data["choices"][0]["message"]["content"],
            model=self.config.id,
            usage=usage,
            tool_calls=tool_calls,
            finish_reason=data["choices"][0].get("finish_reason", "stop"),
        )

    async def stream_chat_completion(
        self, messages: list[dict[str, Any]], **params
    ) -> AsyncGenerator[str, None]:
        validated_params = self.validate_params(params)

        # Use provider-specific model id when provided
        model_id = self.config.provider_model_id or self.config.id

        payload = {"model": model_id, "messages": messages, "stream": True, **validated_params}

        if params.get("tools") and self.config.supports_tools:
            payload["tools"] = params["tools"]
            if params.get("tool_choice"):
                payload["tool_choice"] = params["tool_choice"]

        if params.get("response_format") and self.config.supports_structured_output:
            payload["response_format"] = params["response_format"]

        headers = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"

        total_content = ""

        async for line in self.http.stream_post(
            f"{self.config.base_url}/chat/completions",
            json=payload,
            headers=headers,
        ):
            if not line.startswith("data: "):
                continue
            if line == "data: [DONE]":
                # Emit final usage chunk with estimated tokens
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
                if chunk_data["choices"][0]["delta"].get("content"):
                    content = chunk_data["choices"][0]["delta"]["content"]
                    total_content += content
                    yield self.format_stream_chunk(content, self.config.id)
            except json.JSONDecodeError:
                continue
