import aiohttp
import json
import time
from typing import Dict, Any, List, AsyncGenerator, Optional
from .base import BaseAdapter, UsageInfo, ModelConfig


class DeepSeekAdapter(BaseAdapter):
    
    async def _ensure_session(self):
        if not self.session:
            self.session = aiohttp.ClientSession()
    
    async def chat_completion(
        self,
        messages: List[Dict[str, Any]],
        **params
    ) -> Dict[str, Any]:
        await self._ensure_session()
        
        validated_params = self.validate_params(params)
        
        payload = {
            "model": "deepseek-chat",
            "messages": messages,
            **validated_params
        }
        
        if params.get("tools"):
            payload["tools"] = params["tools"]
            if params.get("tool_choice"):
                payload["tool_choice"] = params["tool_choice"]
        
        if params.get("response_format"):
            if params["response_format"].get("type") == "json_object":
                payload["response_format"] = {"type": "json_object"}
        
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.config.api_key}"
        }
        
        async with self.session.post(
            f"{self.config.base_url}/chat/completions",
            json=payload,
            headers=headers
        ) as response:
            data = await response.json()
            
            if response.status != 200:
                raise Exception(f"DeepSeek API error: {data}")
            
            usage = UsageInfo(
                prompt_tokens=data["usage"]["prompt_tokens"],
                completion_tokens=data["usage"]["completion_tokens"],
                total_tokens=data["usage"]["total_tokens"]
            )
            
            tool_calls = None
            if "tool_calls" in data["choices"][0]["message"]:
                tool_calls = data["choices"][0]["message"]["tool_calls"]
            
            return self.format_response(
                content=data["choices"][0]["message"]["content"],
                model=self.config.id,
                usage=usage,
                tool_calls=tool_calls,
                finish_reason=data["choices"][0]["finish_reason"]
            )
    
    async def stream_chat_completion(
        self,
        messages: List[Dict[str, Any]],
        **params
    ) -> AsyncGenerator[str, None]:
        await self._ensure_session()
        
        validated_params = self.validate_params(params)
        
        payload = {
            "model": "deepseek-chat",
            "messages": messages,
            "stream": True,
            **validated_params
        }
        
        if params.get("tools"):
            payload["tools"] = params["tools"]
            if params.get("tool_choice"):
                payload["tool_choice"] = params["tool_choice"]
        
        if params.get("response_format"):
            if params["response_format"].get("type") == "json_object":
                payload["response_format"] = {"type": "json_object"}
        
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.config.api_key}"
        }
        
        total_content = ""
        prompt_tokens = 0
        
        async with self.session.post(
            f"{self.config.base_url}/chat/completions",
            json=payload,
            headers=headers
        ) as response:
            async for line in response.content:
                line = line.decode('utf-8').strip()
                if line.startswith("data: "):
                    if line == "data: [DONE]":
                        completion_tokens = len(total_content.split()) * 1.3
                        usage_chunk = {
                            "id": f"chatcmpl-{int(time.time() * 1000)}",
                            "object": "chat.completion.chunk",
                            "created": int(time.time()),
                            "model": self.config.id,
                            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                            "usage": {
                                "prompt_tokens": prompt_tokens,
                                "completion_tokens": int(completion_tokens),
                                "total_tokens": prompt_tokens + int(completion_tokens)
                            }
                        }
                        yield f"data: {json.dumps(usage_chunk)}\n\n"
                        yield "data: [DONE]\n\n"
                        break
                    
                    try:
                        chunk_data = json.loads(line[6:])
                        if "usage" in chunk_data:
                            prompt_tokens = chunk_data["usage"].get("prompt_tokens", prompt_tokens)
                        
                        if chunk_data["choices"][0]["delta"].get("content"):
                            content = chunk_data["choices"][0]["delta"]["content"]
                            total_content += content
                            yield self.format_stream_chunk(content, self.config.id)
                    except json.JSONDecodeError:
                        continue