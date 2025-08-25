import aiohttp
import json
from typing import Dict, Any, List, AsyncGenerator, Optional
from .base import BaseAdapter, UsageInfo, ModelConfig


class VLLMAdapter(BaseAdapter):
    
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
        
        # Handle freeinference.org model ID format
        model_id = self.config.id
        if "llama-4-scout" in model_id.lower():
            model_id = "/models/meta-llama_Llama-4-Scout-17B-16E"
        
        payload = {
            "model": model_id,
            "messages": messages,
            **validated_params
        }
        
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
        
        async with self.session.post(
            f"{self.config.base_url}/chat/completions",
            json=payload,
            headers=headers
        ) as response:
            data = await response.json()
            
            if response.status != 200:
                raise Exception(f"VLLM API error: {data}")
            
            usage = None
            if "usage" in data:
                usage = UsageInfo(
                    prompt_tokens=data["usage"].get("prompt_tokens", 0),
                    completion_tokens=data["usage"].get("completion_tokens", 0),
                    total_tokens=data["usage"].get("total_tokens", 0)
                )
            else:
                content = data["choices"][0]["message"]["content"]
                prompt_tokens = sum(len(m.get("content", "").split()) * 1.3 for m in messages)
                completion_tokens = len(content.split()) * 1.3
                usage = UsageInfo(
                    prompt_tokens=int(prompt_tokens),
                    completion_tokens=int(completion_tokens),
                    total_tokens=int(prompt_tokens + completion_tokens)
                )
            
            tool_calls = None
            if "tool_calls" in data["choices"][0]["message"]:
                tool_calls = data["choices"][0]["message"]["tool_calls"]
            
            return self.format_response(
                content=data["choices"][0]["message"]["content"],
                model=self.config.id,
                usage=usage,
                tool_calls=tool_calls,
                finish_reason=data["choices"][0].get("finish_reason", "stop")
            )
    
    async def stream_chat_completion(
        self,
        messages: List[Dict[str, Any]],
        **params
    ) -> AsyncGenerator[str, None]:
        await self._ensure_session()
        
        validated_params = self.validate_params(params)
        
        # Handle freeinference.org model ID format
        model_id = self.config.id
        if "llama-4-scout" in model_id.lower():
            model_id = "/models/meta-llama_Llama-4-Scout-17B-16E"
        
        payload = {
            "model": model_id,
            "messages": messages,
            "stream": True,
            **validated_params
        }
        
        if params.get("tools") and self.config.supports_tools:
            payload["tools"] = params["tools"]
            if params.get("tool_choice"):
                payload["tool_choice"] = params["tool_choice"]
        
        if params.get("response_format") and self.config.supports_structured_output:
            payload["response_format"] = params["response_format"]
        
        headers = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        
        async with self.session.post(
            f"{self.config.base_url}/chat/completions",
            json=payload,
            headers=headers
        ) as response:
            async for line in response.content:
                line = line.decode('utf-8').strip()
                if line.startswith("data: "):
                    if line == "data: [DONE]":
                        yield self.format_stream_chunk("", self.config.id, "stop")
                        break
                    
                    try:
                        chunk_data = json.loads(line[6:])
                        if chunk_data["choices"][0]["delta"].get("content"):
                            yield self.format_stream_chunk(
                                chunk_data["choices"][0]["delta"]["content"],
                                self.config.id
                            )
                    except json.JSONDecodeError:
                        continue