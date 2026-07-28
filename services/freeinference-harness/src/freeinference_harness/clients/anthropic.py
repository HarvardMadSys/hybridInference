"""Anthropic-surface black-box client helpers."""

from __future__ import annotations

import json
from typing import Any

import httpx


class AnthropicMessagesClient:
    """Thin wrapper over raw HTTP for the gateway's Anthropic surface."""

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
            "x-api-key": api_key,
            "Authorization": f"Bearer {api_key}",
            "anthropic-version": "2023-06-01",
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

    def create_message(self, payload: dict[str, Any]) -> httpx.Response:
        """Executes a non-streaming Messages request and returns the raw response.

        Returns the response without raising so callers can assert on 4xx
        behavior (e.g. poisoned-history regressions) explicitly.
        """
        with httpx.Client(timeout=self._timeout) as client:
            return client.post(
                f"{self._root_base_url}/v1/messages",
                headers=self._headers,
                json=payload,
            )

    def count_tokens(self, payload: dict[str, Any]) -> httpx.Response:
        """Executes a count_tokens request and returns the raw response."""
        with httpx.Client(timeout=self._timeout) as client:
            return client.post(
                f"{self._root_base_url}/v1/messages/count_tokens",
                headers=self._headers,
                json=payload,
            )

    def collect_stream(
        self,
        payload: dict[str, Any],
        *,
        tolerate_truncation: bool = False,
    ) -> dict[str, Any]:
        """Collects a streaming Messages response into a summarized observation."""
        stats: dict[str, Any] = {
            "events": 0,
            "done": False,
            "truncated": False,
            "stop_reason": None,
            "usage": None,
            "text_parts": [],
            "content_blocks": [],
            "error_events": [],
        }
        open_blocks: dict[int, dict[str, Any]] = {}

        try:
            with (
                httpx.Client(timeout=self._timeout) as client,
                client.stream(
                    "POST",
                    f"{self._root_base_url}/v1/messages",
                    headers=self._headers,
                    json={**payload, "stream": True},
                ) as response,
            ):
                response.raise_for_status()
                for line in response.iter_lines():
                    if not line or line.startswith(":") or line.startswith("event:"):
                        continue
                    if not line.startswith("data:"):
                        continue
                    body = line[5:].strip()
                    try:
                        event = json.loads(body)
                    except json.JSONDecodeError:
                        continue
                    stats["events"] += 1
                    self._apply_event(event, stats, open_blocks)
                    if stats["done"]:
                        break
        except (httpx.RemoteProtocolError, httpx.ReadError) as exc:
            if not tolerate_truncation:
                raise
            stats["truncated"] = True
            stats["truncation_error"] = f"{exc.__class__.__name__}: {exc}"

        self._finalize_blocks(stats, open_blocks)
        stats["full_text"] = "".join(stats["text_parts"])
        return stats

    def _apply_event(
        self,
        event: dict[str, Any],
        stats: dict[str, Any],
        open_blocks: dict[int, dict[str, Any]],
    ) -> None:
        """Folds one SSE event into the stream observation."""
        event_type = event.get("type")
        if event_type == "content_block_start":
            index = int(event.get("index", 0))
            block = event.get("content_block") or {}
            open_blocks[index] = {
                "type": block.get("type"),
                "id": block.get("id"),
                "name": block.get("name"),
                "input_raw": "",
                "text": "",
            }
        elif event_type == "content_block_delta":
            index = int(event.get("index", 0))
            delta = event.get("delta") or {}
            entry = open_blocks.setdefault(
                index, {"type": None, "id": None, "name": None, "input_raw": "", "text": ""}
            )
            if delta.get("type") == "text_delta":
                text = delta.get("text") or ""
                entry["text"] += text
                stats["text_parts"].append(text)
            elif delta.get("type") == "input_json_delta":
                entry["input_raw"] += delta.get("partial_json") or ""
        elif event_type == "message_delta":
            delta = event.get("delta") or {}
            if delta.get("stop_reason"):
                stats["stop_reason"] = delta["stop_reason"]
            if event.get("usage"):
                stats["usage"] = event["usage"]
        elif event_type == "message_stop":
            stats["done"] = True
        elif event_type == "error":
            stats["error_events"].append(event)

    def _finalize_blocks(
        self,
        stats: dict[str, Any],
        open_blocks: dict[int, dict[str, Any]],
    ) -> None:
        """Parses accumulated tool inputs and orders blocks by index."""
        for index in sorted(open_blocks):
            entry = open_blocks[index]
            if entry.get("type") == "tool_use":
                raw = entry.get("input_raw") or ""
                if not raw:
                    entry["input"] = {}
                else:
                    try:
                        entry["input"] = json.loads(raw)
                    except json.JSONDecodeError:
                        entry["input"] = None
                        entry["input_parse_error"] = True
            stats["content_blocks"].append(entry)
