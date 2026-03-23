"""OpenAI-compatible black-box client helpers."""

from __future__ import annotations

import json
from typing import Any

import httpx
from openai import OpenAI


class OpenAICompatClient:
    """Thin wrapper over the OpenAI SDK and raw SSE parsing."""

    def __init__(self, *, base_url: str, api_key: str, timeout_seconds: float) -> None:
        root_base_url = base_url.rstrip("/").removesuffix("/v1")
        openai_base_url = base_url.rstrip("/")
        if not openai_base_url.endswith("/v1"):
            openai_base_url = f"{openai_base_url}/v1"

        self._root_base_url = root_base_url
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        self._timeout = httpx.Timeout(
            connect=20.0,
            read=timeout_seconds,
            write=20.0,
            pool=20.0,
        )
        self._sdk = OpenAI(api_key=api_key, base_url=openai_base_url)

    def create_chat_completion(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Executes a non-streaming chat completion through the OpenAI SDK."""
        response = self._sdk.chat.completions.create(**payload)
        return response.model_dump(mode="json")

    def create_embeddings(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Executes an embeddings request through raw HTTP."""
        with httpx.Client(timeout=self._timeout) as client:
            response = client.post(
                f"{self._root_base_url}/v1/embeddings",
                headers=self._headers,
                json=payload,
            )
            response.raise_for_status()
            return response.json()

    def collect_stream(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Collects a streaming chat completion into a summarized observation."""
        stats: dict[str, Any] = {
            "events": 0,
            "keepalives": 0,
            "saw_reasoning": False,
            "saw_content": False,
            "saw_tool_calls": False,
            "saw_usage": False,
            "done": False,
            "content_parts": [],
            "samples": [],
            "finish_reasons": [],
            "usage": None,
        }

        with (
            httpx.Client(timeout=self._timeout) as client,
            client.stream(
                "POST",
                f"{self._root_base_url}/v1/chat/completions",
                headers=self._headers,
                json=payload,
            ) as response,
        ):
            response.raise_for_status()
            for line in response.iter_lines():
                if not line:
                    continue
                if line.startswith(":"):
                    stats["keepalives"] += 1
                    continue
                if not line.startswith("data: "):
                    continue

                body = line[6:].strip()
                if body == "[DONE]":
                    stats["done"] = True
                    break

                try:
                    chunk = json.loads(body)
                except json.JSONDecodeError:
                    continue

                stats["events"] += 1
                if chunk.get("usage"):
                    stats["saw_usage"] = True
                    stats["usage"] = chunk["usage"]

                choices = chunk.get("choices") or []
                if not choices:
                    continue

                choice = choices[0]
                delta = choice.get("delta", {}) or {}
                finish_reason = choice.get("finish_reason")
                if finish_reason:
                    stats["finish_reasons"].append(finish_reason)

                reasoning = delta.get("reasoning_content")
                if isinstance(reasoning, str) and reasoning:
                    stats["saw_reasoning"] = True

                content = delta.get("content")
                if isinstance(content, str) and content:
                    stats["saw_content"] = True
                    stats["content_parts"].append(content)

                tool_calls = delta.get("tool_calls")
                if isinstance(tool_calls, list) and tool_calls:
                    stats["saw_tool_calls"] = True

                if len(stats["samples"]) < 5:
                    sample: dict[str, Any] = {"delta_keys": list(delta.keys())}
                    if isinstance(content, str) and content:
                        sample["content_preview"] = content[:120]
                    if isinstance(reasoning, str) and reasoning:
                        sample["reasoning_preview"] = reasoning[:120]
                    if isinstance(tool_calls, list) and tool_calls:
                        sample["tool_calls_len"] = len(tool_calls)
                    if finish_reason:
                        sample["finish_reason"] = finish_reason
                    stats["samples"].append(sample)

        stats["full_content"] = "".join(stats["content_parts"])
        return stats
