import json
import time
from collections.abc import AsyncGenerator
from typing import Any

from serving.utils.tokens import estimate_prompt_tokens, estimate_text_tokens

from .base import BaseAdapter, UsageInfo


class GeminiAdapter(BaseAdapter):
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
        request_body = self._convert_messages_to_gemini(messages)

        generation_config = self._apply_generation_config(params)
        if generation_config:
            request_body["generationConfig"] = generation_config

        if params.get("tools"):
            request_body["tools"] = self._convert_tools(params["tools"])

        url = f"{self.config.base_url}/models/gemini-2.5-flash:generateContent?key={self.config.api_key}"

        data = await self.http.json_post_with_retry(url, json=request_body)

        candidate = data["candidates"][0]
        content_parts = candidate["content"]["parts"]

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
            usage = UsageInfo(
                prompt_tokens=data["usageMetadata"]["promptTokenCount"],
                completion_tokens=data["usageMetadata"]["candidatesTokenCount"],
                total_tokens=data["usageMetadata"]["totalTokenCount"],
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
        request_body = self._convert_messages_to_gemini(messages)

        generation_config = self._apply_generation_config(params)
        if generation_config:
            request_body["generationConfig"] = generation_config

        if params.get("tools"):
            request_body["tools"] = self._convert_tools(params["tools"])

        url = f"{self.config.base_url}/models/gemini-2.5-flash:streamGenerateContent?key={self.config.api_key}"

        total_content = ""
        prompt_tokens = 0

        async for line in self.http.stream_post(url, json=request_body, mode="ndjson"):
            if not line:
                continue
            try:
                data = json.loads(line)

                if "candidates" in data:
                    candidate = data["candidates"][0]
                    content_parts = candidate["content"]["parts"]

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
