from abc import ABC, abstractmethod
from typing import Dict, Any, Optional, AsyncGenerator, List
from dataclasses import dataclass, field
import time
import json


@dataclass
class UsageInfo:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    
    def to_dict(self) -> Dict[str, int]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens
        }


@dataclass
class ModelConfig:
    id: str
    name: str
    provider: str
    base_url: str
    api_key: Optional[str] = None
    quantization: str = "bf16"
    input_modalities: List[str] = field(default_factory=lambda: ["text"])
    output_modalities: List[str] = field(default_factory=lambda: ["text"])
    context_length: int = 8192
    max_output_length: int = 4096
    supports_tools: bool = False
    supports_structured_output: bool = False
    supported_params: List[str] = field(default_factory=lambda: [
        "temperature", "top_p", "max_tokens"
    ])
    pricing: Dict[str, str] = field(default_factory=lambda: {
        "prompt": "0",
        "completion": "0",
        "image": "0",
        "request": "0",
        "input_cache_reads": "0",
        "input_cache_writes": "0"
    })


class BaseAdapter(ABC):
    
    def __init__(self, config: ModelConfig):
        self.config = config
        self.session = None
    
    @abstractmethod
    async def chat_completion(
        self,
        messages: List[Dict[str, Any]],
        **params
    ) -> Dict[str, Any]:
        pass
    
    @abstractmethod
    async def stream_chat_completion(
        self,
        messages: List[Dict[str, Any]],
        **params
    ) -> AsyncGenerator[str, None]:
        pass
    
    def validate_params(self, params: Dict[str, Any]) -> Dict[str, Any]:
        validated = {}
        
        if "max_tokens" in params:
            validated["max_tokens"] = min(
                params["max_tokens"],
                self.config.max_output_length
            )
        
        if "temperature" in params:
            validated["temperature"] = max(0.0, min(2.0, params["temperature"]))
        
        if "top_p" in params:
            validated["top_p"] = max(0.0, min(1.0, params["top_p"]))
        
        if "stop" in params:
            validated["stop"] = params["stop"]
        
        if "seed" in params and "seed" in self.config.supported_params:
            validated["seed"] = params["seed"]
        
        return validated
    
    def format_response(
        self,
        content: str,
        model: str,
        usage: Optional[UsageInfo] = None,
        tool_calls: Optional[List[Dict]] = None,
        finish_reason: str = "stop"
    ) -> Dict[str, Any]:
        response = {
            "id": f"chatcmpl-{int(time.time() * 1000)}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": content
                    },
                    "finish_reason": finish_reason
                }
            ]
        }
        
        if tool_calls:
            response["choices"][0]["message"]["tool_calls"] = tool_calls
        
        if usage:
            response["usage"] = usage.to_dict()
        
        return response
    
    def format_stream_chunk(
        self,
        content: str,
        model: str,
        finish_reason: Optional[str] = None
    ) -> str:
        chunk = {
            "id": f"chatcmpl-{int(time.time() * 1000)}",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "delta": {"content": content} if content else {},
                    "finish_reason": finish_reason
                }
            ]
        }
        return f"data: {json.dumps(chunk)}\n\n"
    
    async def cleanup(self):
        if self.session:
            await self.session.close()