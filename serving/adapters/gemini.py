"""Google Gemini API adapter with cache and thinking token tracking."""

import json
import time
from collections.abc import AsyncGenerator
from typing import Any

from serving.utils.tokens import estimate_prompt_tokens, estimate_text_tokens

from .base import BaseAdapter, UsageInfo


class GeminiAdapter(BaseAdapter):
    """Adapter for Google Gemini models (1.5/2.0/2.5 series)."""

    def _convert_messages_to_gemini(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        system_instruction = None
        contents = []

        for msg in messages:
            role = msg["role"]
            content = msg["content"]

            if role == "system":
                system_instruction = content
            elif role == "user":
                contents.append({"role": "user", "parts": [{"text": content}]})
            elif role == "assistant":
                contents.append({"role": "model", "parts": [{"text": content}]})

        request_body = {"contents": contents}
        if system_instruction:
            request_body["systemInstruction"] = {"parts": [{"text": system_instruction}]}

        return request_body

    def _apply_generation_config(self, params: dict[str, Any]) -> dict[str, Any]:
        config = {}

        if "max_tokens" in params:
            config["maxOutputTokens"] = params["max_tokens"]

        if "temperature" in params:
            config["temperature"] = params["temperature"]

        if "top_p" in params:
            config["topP"] = params["top_p"]

        if "stop" in params:
            config["stopSequences"] = (
                params["stop"] if isinstance(params["stop"], list) else [params["stop"]]
            )

        if params.get("response_format", {}).get("type") == "json_object":
            config["responseMimeType"] = "application/json"

        return config

    def _convert_tools(self, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        gemini_tools = []
        for tool in tools:
            if tool["type"] == "function":
                func = tool["function"]
                gemini_func = {
                    "name": func["name"],
                    "description": func.get("description", ""),
                }

                if "parameters" in func:
                    params = func["parameters"]
                    properties = {}
                    required = params.get("required", [])

                    for prop_name, prop_schema in params.get("properties", {}).items():
                        properties[prop_name] = {
                            "type": prop_schema.get("type", "string"),
                            "description": prop_schema.get("description", ""),
                        }

                    gemini_func["parameters"] = {
                        "type": "object",
                        "properties": properties,
                        "required": required,
                    }

                gemini_tools.append({"functionDeclarations": [gemini_func]})

        return gemini_tools

    async def chat_completion(self, messages: list[dict[str, Any]], **params) -> dict[str, Any]:
        """Execute non-streaming chat completion with thinking and cache token extraction."""
        request_body = self._convert_messages_to_gemini(messages)

        generation_config = self._apply_generation_config(params)
        if generation_config:
            request_body["generationConfig"] = generation_config

        if params.get("tools"):
            request_body["tools"] = self._convert_tools(params["tools"])

        # Use provider_model_id if available, otherwise fall back to config.id
        model_name = getattr(self.config, "provider_model_id", None) or self.config.id
        url = (
            f"{self.config.base_url}/models/{model_name}:generateContent?key={self.config.api_key}"
        )

        data = await self.http.json_post_with_retry(url, json=request_body)

        if "candidates" not in data or not data["candidates"]:
            raise ValueError(f"Unexpected Gemini response structure: {json.dumps(data)}")

        candidate = data["candidates"][0]

        # Handle case where content or parts might be missing
        # This can happen when model hits MAX_TOKENS during thinking phase
        if "content" not in candidate:
            raise ValueError(f"No content in candidate: {json.dumps(candidate)}")

        content = candidate.get("content", {})
        content_parts = content.get("parts", [])

        # If no parts, the model likely hit token limit during thinking
        # or content was filtered. Return empty response.

        text_content = ""
        tool_calls = []

        for part in content_parts:
            if "text" in part:
                text_content += part["text"]
            elif "functionCall" in part:
                func_call = part["functionCall"]
                tool_calls.append(
                    {
                        "id": f"call_{int(time.time() * 1000)}",
                        "type": "function",
                        "function": {
                            "name": func_call["name"],
                            "arguments": json.dumps(func_call.get("args", {})),
                        },
                    }
                )

        if "usageMetadata" in data:
            usage_meta = data["usageMetadata"]
            # Handle different field names in different API versions
            prompt_tokens = usage_meta.get("promptTokenCount", 0)
            total_tokens = usage_meta.get("totalTokenCount", 0)
            cached_tokens = usage_meta.get("cachedContentTokenCount", 0)

            # Preview models may not include candidatesTokenCount
            # In that case, calculate it from total - prompt - thoughts
            completion_tokens = usage_meta.get("candidatesTokenCount")
            reasoning_tokens = usage_meta.get("thoughtsTokenCount", 0)
            if completion_tokens is None:
                # Calculate completion tokens: total - prompt - thoughts
                calculated = total_tokens - prompt_tokens - reasoning_tokens
                # Validate calculation: fallback to estimation if invalid or zero with content
                if calculated > 0:
                    completion_tokens = calculated
                elif calculated == 0 and text_content:
                    # Edge case: totalTokenCount=0 but model returned text
                    # This means usage metadata is incomplete, estimate from response
                    completion_tokens = estimate_text_tokens(text_content)
                    total_tokens = prompt_tokens + int(completion_tokens) + reasoning_tokens
                elif calculated < 0:
                    # totalTokenCount missing or incomplete, estimate from response
                    completion_tokens = estimate_text_tokens(text_content)
                    # Recalculate total if it was missing
                    if total_tokens == 0:
                        total_tokens = prompt_tokens + int(completion_tokens) + reasoning_tokens
                else:
                    # calculated == 0 and no text_content, legitimately empty
                    completion_tokens = 0

            usage = UsageInfo(
                prompt_tokens=int(prompt_tokens),
                completion_tokens=int(completion_tokens),
                total_tokens=int(total_tokens),
                reasoning_tokens=int(reasoning_tokens),
                cache_read_tokens=int(cached_tokens),  # Gemini: cached tokens are read from cache
            )
        else:
            prompt_tokens = estimate_prompt_tokens(messages)
            completion_tokens = estimate_text_tokens(text_content)
            usage = UsageInfo(
                prompt_tokens=int(prompt_tokens),
                completion_tokens=int(completion_tokens),
                total_tokens=int(prompt_tokens + completion_tokens),
            )

        finish_reason_map = {
            "STOP": "stop",
            "MAX_TOKENS": "length",
            "SAFETY": "content_filter",
            "OTHER": "stop",
        }
        finish_reason = finish_reason_map.get(candidate.get("finishReason", "STOP"), "stop")

        return self.format_response(
            content=text_content,
            model=self.config.id,
            usage=usage,
            tool_calls=tool_calls if tool_calls else None,
            finish_reason=finish_reason,
        )

    async def stream_chat_completion(
        self, messages: list[dict[str, Any]], **params
    ) -> AsyncGenerator[str, None]:
        """Execute streaming chat completion with thinking and cache token extraction."""
        request_body = self._convert_messages_to_gemini(messages)

        generation_config = self._apply_generation_config(params)
        if generation_config:
            request_body["generationConfig"] = generation_config

        if params.get("tools"):
            request_body["tools"] = self._convert_tools(params["tools"])

        # Use provider_model_id if available, otherwise fall back to config.id
        model_name = getattr(self.config, "provider_model_id", None) or self.config.id
        url = f"{self.config.base_url}/models/{model_name}:streamGenerateContent?key={self.config.api_key}"

        total_content = ""
        prompt_tokens = 0

        async for line in self.http.stream_post(url, json=request_body, mode="ndjson"):
            if not line:
                continue
            try:
                data = json.loads(line)

                if "candidates" in data:
                    candidate = data["candidates"][0]
                    # Handle case where content or parts might be missing
                    if "content" not in candidate:
                        continue
                    content_parts = candidate["content"].get("parts", [])

                    for part in content_parts:
                        if "text" in part:
                            text = part["text"]
                            total_content += text
                            yield self.format_stream_chunk(text, self.config.id)

                if "usageMetadata" in data:
                    prompt_tokens = data["usageMetadata"].get("promptTokenCount", prompt_tokens)

            except json.JSONDecodeError:
                continue

        completion_tokens = estimate_text_tokens(total_content)
        final_prompt_tokens = prompt_tokens or estimate_prompt_tokens(messages)
        usage_chunk = {
            "id": f"chatcmpl-{int(time.time() * 1000)}",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": self.config.id,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": int(final_prompt_tokens),
                "completion_tokens": int(completion_tokens),
                "total_tokens": int(final_prompt_tokens + completion_tokens),
            },
        }
        yield f"data: {json.dumps(usage_chunk)}\n\n"
        yield "data: [DONE]\n\n"
