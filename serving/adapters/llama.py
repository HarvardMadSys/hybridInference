import aiohttp
import json
from typing import Dict, Any, List, AsyncGenerator
from .base import BaseAdapter, UsageInfo


class LlamaAdapter(BaseAdapter):
    
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
        
        # Llama API expects the /inference endpoint
        payload = {
            "model": self.config.id,
            "messages": messages,
            **validated_params
        }
        
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
        
        async with self.session.post(url, json=payload, headers=headers) as response:
            data = await response.json()
            
            if response.status != 200:
                raise Exception(f"Llama API error: {data}")
            
            # Check if usage is provided, otherwise estimate
            usage = None
            if "usage" in data:
                usage = UsageInfo(
                    prompt_tokens=data["usage"].get("prompt_tokens", 0),
                    completion_tokens=data["usage"].get("completion_tokens", 0),
                    total_tokens=data["usage"].get("total_tokens", 0)
                )
            else:
                # Estimate tokens if not provided
                content = data.get("content", "")
                prompt_tokens = sum(len(m.get("content", "").split()) * 1.3 for m in messages)
                completion_tokens = len(content.split()) * 1.3
                usage = UsageInfo(
                    prompt_tokens=int(prompt_tokens),
                    completion_tokens=int(completion_tokens),
                    total_tokens=int(prompt_tokens + completion_tokens)
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
                finish_reason=data.get("stop_reason", "stop")
            )
    
    async def stream_chat_completion(
        self,
        messages: List[Dict[str, Any]],
        **params
    ) -> AsyncGenerator[str, None]:
        await self._ensure_session()
        
        validated_params = self.validate_params(params)
        
        payload = {
            "model": self.config.id,
            "messages": messages,
            "stream": True,
            **validated_params
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
        
        async with self.session.post(url, json=payload, headers=headers) as response:
            async for line in response.content:
                line = line.decode('utf-8').strip()
                if line.startswith("data: "):
                    if line == "data: [DONE]":
                        yield self.format_stream_chunk("", self.config.id, "stop")
                        break
                    
                    try:
                        chunk_data = json.loads(line[6:])
                        if "content" in chunk_data:
                            yield self.format_stream_chunk(
                                chunk_data["content"],
                                self.config.id
                            )
                    except json.JSONDecodeError:
                        continue