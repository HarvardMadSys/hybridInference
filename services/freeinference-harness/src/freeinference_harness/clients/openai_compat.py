"""OpenAI-compatible black-box client helpers."""

from __future__ import annotations

import json
from typing import Any

import httpx


class OpenAICompatClient:
    """Thin wrapper over raw HTTP for OpenAI-compatible endpoints."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        timeout_seconds: float,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self._root_base_url = base_url.rstrip("/").removesuffix("/v1")
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        if extra_headers:
            self._headers.update(extra_headers)
        self._timeout = httpx.Timeout(
            connect=20.0,
            read=timeout_seconds,
            write=20.0,
            pool=20.0,
        )

    def create_chat_completion(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Executes a non-streaming chat completion via raw HTTP."""
        with httpx.Client(timeout=self._timeout) as client:
            response = client.post(
                f"{self._root_base_url}/v1/chat/completions",
                headers=self._headers,
                json=payload,
            )
            response.raise_for_status()
            return response.json()

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

    def collect_stream(
        self,
        payload: dict[str, Any],
        *,
        tolerate_truncation: bool = False,
    ) -> dict[str, Any]:
        """Collects a streaming chat completion into a summarized observation.

        With ``tolerate_truncation`` the partial observation is returned (with
        ``truncated`` set) instead of raising when the upstream closes the
        stream mid-body, which agent-loop disconnect scenarios rely on.
        """
        stats: dict[str, Any] = {
            "events": 0,
            "keepalives": 0,
            "saw_reasoning": False,
            "saw_content": False,
            "saw_tool_calls": False,
            "saw_usage": False,
            "done": False,
            "truncated": False,
            "content_parts": [],
            "samples": [],
            "finish_reasons": [],
            "usage": None,
            "tool_calls": [],
            "stream_errors": [],
        }
        # Accumulator for incremental tool call fragments keyed by index.
        tc_acc: dict[int, dict[str, Any]] = {}

        try:
            self._consume_stream(payload, stats, tc_acc)
        except (httpx.RemoteProtocolError, httpx.ReadError) as exc:
            if not tolerate_truncation:
                raise
            stats["truncated"] = True
            stats["truncation_error"] = f"{exc.__class__.__name__}: {exc}"

        stats["full_content"] = "".join(stats["content_parts"])
        # Flatten accumulated tool calls ordered by index.
        if tc_acc:
            stats["tool_calls"] = [tc_acc[idx] for idx in sorted(tc_acc)]
        return stats

    def _consume_stream(
        self,
        payload: dict[str, Any],
        stats: dict[str, Any],
        tc_acc: dict[int, dict[str, Any]],
    ) -> None:
        """Consumes SSE lines from one streaming request into the accumulators."""
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

                # An upstream failure that happens after the response has
                # started is delivered as an in-stream error frame rather than
                # an HTTP status. A driver that only watches HTTP status codes
                # reports "no error seen" for a stream that plainly failed, so
                # record these explicitly.
                error = chunk.get("error")
                if isinstance(error, dict):
                    stats["stream_errors"].append(
                        {
                            "code": error.get("code"),
                            "type": error.get("type"),
                            "message": (error.get("message") or "")[:300],
                        }
                    )
                    continue

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
                    for tc_fragment in tool_calls:
                        idx = tc_fragment.get("index", 0)
                        if idx not in tc_acc:
                            tc_acc[idx] = {
                                "id": tc_fragment.get("id") or "",
                                "name": "",
                                "arguments": "",
                            }
                        entry = tc_acc[idx]
                        if tc_fragment.get("id"):
                            entry["id"] = tc_fragment["id"]
                        func = tc_fragment.get("function") or {}
                        if func.get("name"):
                            entry["name"] = func["name"]
                        if func.get("arguments"):
                            entry["arguments"] += func["arguments"]

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
