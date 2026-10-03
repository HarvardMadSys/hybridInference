"""Load fixed-history, closed-loop session workloads without future-label leakage."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def canonical_json(value: Any) -> str:
    """Serialize an artifact or hash input deterministically."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_bytes(value: bytes) -> str:
    """Return the digest used by experiment manifests."""
    return hashlib.sha256(value).hexdigest()


def _integer(value: Any, name: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
    return value


def _seconds(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be finite and nonnegative")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ValueError(f"{name} must be finite and nonnegative")
    return result


@dataclass(frozen=True)
class ReplayRequest:
    """One invocation with a fixed prompt; generated answers never alter later prompts.

    The first round of each session uses ``arrival_s``. Subsequent rounds become
    eligible after the previous round finishes plus its ``tool_wait_s``. Recorded
    output lengths and cache-hit labels, if present in a source file, are not
    admitted into this type or passed to the online policy.
    """

    request_id: str
    session_id: str
    round_index: int
    arrival_s: float
    tool_wait_s: float
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]] | None
    max_tokens: int
    payload_sha256: str

    def prompt_token_estimate(self, chars_per_token: float) -> int:
        """Return an explicitly approximate, prompt-only online size feature."""
        if not math.isfinite(chars_per_token) or chars_per_token <= 0:
            raise ValueError("prompt_chars_per_token must be finite and positive")
        body: dict[str, Any] = {"messages": self.messages}
        if self.tools:
            body["tools"] = self.tools
        return max(1, math.ceil(len(canonical_json(body)) / chars_per_token))


def load_workload(path: str | Path) -> list[ReplayRequest]:
    """Validate a JSONL selection before any request is sent."""
    requests: list[ReplayRequest] = []
    seen: set[str] = set()
    rounds: set[tuple[str, int]] = set()
    for line_number, line in enumerate(Path(path).read_text().splitlines(), 1):
        if not line.strip():
            continue
        row = json.loads(line)
        request_id = row.get("id")
        session_id = row.get("session_id")
        if not isinstance(request_id, str) or not request_id:
            raise ValueError(f"line {line_number}: id must be a nonempty string")
        if not isinstance(session_id, str) or not session_id:
            raise ValueError(f"line {line_number}: session_id must be a nonempty string")
        if request_id in seen:
            raise ValueError(f"duplicate request id: {request_id}")
        round_index = _integer(row.get("round_index"), "round_index")
        if (session_id, round_index) in rounds:
            raise ValueError(f"duplicate session round: {session_id}/{round_index}")
        messages = row.get("messages")
        if not isinstance(messages, list) or not messages:
            raise ValueError(f"line {line_number}: messages must be a nonempty list")
        if any(not isinstance(m, dict) or not isinstance(m.get("role"), str) for m in messages):
            raise ValueError(f"line {line_number}: malformed message")
        tools = row.get("tools")
        if tools is not None and (
            not isinstance(tools, list) or any(not isinstance(t, dict) for t in tools)
        ):
            raise ValueError(f"line {line_number}: tools must be a list of objects")
        max_tokens = _integer(row.get("max_tokens"), "max_tokens", 1)
        payload = {"messages": messages, "tools": tools, "max_tokens": max_tokens}
        requests.append(
            ReplayRequest(
                request_id=request_id,
                session_id=session_id,
                round_index=round_index,
                arrival_s=_seconds(row.get("arrival_s", 0), "arrival_s"),
                tool_wait_s=_seconds(row.get("tool_wait_s", 0), "tool_wait_s"),
                messages=messages,
                tools=tools,
                max_tokens=max_tokens,
                payload_sha256=sha256_bytes(canonical_json(payload).encode()),
            )
        )
        seen.add(request_id)
        rounds.add((session_id, round_index))
    if not requests:
        raise ValueError("workload must contain at least one request")
    return requests


def group_sessions(requests: list[ReplayRequest]) -> list[list[ReplayRequest]]:
    """Group and order sessions deterministically, preserving independent arrivals."""
    sessions: dict[str, list[ReplayRequest]] = {}
    for request in requests:
        sessions.setdefault(request.session_id, []).append(request)
    groups = [sorted(group, key=lambda r: r.round_index) for group in sessions.values()]
    return sorted(groups, key=lambda group: (group[0].arrival_s, group[0].session_id))
