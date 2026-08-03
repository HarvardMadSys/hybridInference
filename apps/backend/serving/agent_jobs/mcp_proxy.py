"""The JSON-RPC half of the MCP proxy: what may be called, and what is listed.

The proxy exists so that a sandbox with no egress can still use the MCP
ecosystem. It sits where the model gateway already sits — on the trusted side —
and the shape mirrors the publisher exactly: the sandbox holds a capability
token and nothing else, and the component outside the boundary holds the real
credential and decides what the request is allowed to be.

This module is the deciding part, kept free of HTTP so it can be tested by
calling it. It answers two questions:

- **May this request proceed?** A ``tools/call`` naming a tool outside the
  server's allowlist is refused here, before anything leaves the gateway. That
  is the enforcement point that matters: the agent is driving on untrusted
  repository content, so "the model was told not to" is not a control.
- **What does the agent get to see?** A ``tools/list`` result is filtered on
  the way back, so a tool that is not allowed is not merely refused when called
  — it is never advertised. A model cannot be talked into using a tool it was
  never offered, and a 40-tool server does not spend a small model's context on
  35 tools it may not touch.

Both directions are needed. Filtering only the listing would leave the tool
callable by name; refusing only the call would leave it advertised, and an
agent that keeps trying a refused tool wastes the job's budget discovering the
policy one error at a time.

**Responses are filtered by shape, not by request id.** Tracking which id was a
``tools/list`` looks tidier and is worse: streamable HTTP lets a server deliver
a reply on the long-lived ``GET`` stream rather than on the ``POST`` that asked
for it, and an id table built from one request cannot recognize a catalogue
arriving on the other. Matching ``result.tools`` catches it wherever it lands,
and needs no state to carry between requests.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from serving.agent_jobs.mcp_registry import McpServer

# JSON-RPC's "method not found". A blocked tool is reported as an ordinary
# protocol error rather than a transport failure, so the agent CLI surfaces it
# as a failed tool call — which is a thing models handle — instead of dropping
# the MCP connection.
METHOD_NOT_FOUND = -32601

TOOLS_LIST = "tools/list"
TOOLS_CALL = "tools/call"

# A single SSE frame is buffered to filter it. The cap bounds what a hostile or
# broken upstream can make the gateway hold for one frame; it is far above any
# real tool catalogue.
MAX_FRAME_BYTES = 4 * 1024 * 1024

# Non-streaming replies are read whole before filtering, under the same cap and
# for the same reason.
MAX_BUFFERED_RESPONSE_BYTES = MAX_FRAME_BYTES

_FRAME_SEPARATOR = b"\n\n"


@dataclass(frozen=True)
class RequestPlan:
    """What the proxy decided about one inbound JSON-RPC payload."""

    # A ready-made JSON-RPC error body when the request must not be forwarded.
    rejection: dict[str, Any] | list[dict[str, Any]] | None = None
    # Tool names refused, for the audit log.
    blocked_tools: tuple[str, ...] = ()

    @property
    def rejected(self) -> bool:
        """Whether the request was refused rather than forwarded."""
        return self.rejection is not None


def _error(payload: dict[str, Any], message: str) -> dict[str, Any]:
    """Build a JSON-RPC error reply mirroring one request's id."""
    return {
        "jsonrpc": "2.0",
        "id": payload.get("id"),
        "error": {"code": METHOD_NOT_FOUND, "message": message},
    }


def plan_request(body: bytes, server: McpServer) -> RequestPlan:
    """Decide whether a request may be forwarded.

    Unparseable bodies are forwarded untouched: the upstream server owns the
    protocol and will answer a malformed request better than a guess here
    would. Nothing is weakened by that — a body the proxy cannot parse is not a
    ``tools/call`` it failed to notice, since noticing one is the same parse,
    and the response filter is independent of this decision.
    """
    if server.unfiltered:
        return RequestPlan()
    try:
        document = json.loads(body or b"{}")
    except (json.JSONDecodeError, ValueError):
        return RequestPlan()

    batch = document if isinstance(document, list) else [document]
    blocked: list[str] = []
    errors: list[dict[str, Any]] = []
    for payload in batch:
        if not isinstance(payload, dict) or payload.get("method") != TOOLS_CALL:
            continue
        params = payload.get("params")
        tool = str(params.get("name") or "") if isinstance(params, dict) else ""
        if server.allows(tool):
            continue
        blocked.append(tool)
        errors.append(
            _error(
                payload,
                f"Tool {tool!r} is not available on the {server.name!r} server. "
                "This deployment exposes a specific set of its tools; call one of "
                "the tools listed for this server.",
            )
        )

    if not errors:
        return RequestPlan()
    # A batch containing one blocked tool is refused whole. Forwarding the rest
    # would mean re-associating replies across two upstream requests, and MCP
    # clients do not batch tool calls in practice.
    rejection: dict[str, Any] | list[dict[str, Any]] = (
        errors if isinstance(document, list) else errors[0]
    )
    return RequestPlan(rejection=rejection, blocked_tools=tuple(blocked))


def filter_message(payload: Any, server: McpServer) -> Any:
    """Drop disallowed tools from anything shaped like a ``tools/list`` result."""
    if not isinstance(payload, dict):
        return payload
    result = payload.get("result")
    if not isinstance(result, dict):
        return payload
    tools = result.get("tools")
    if not isinstance(tools, list):
        return payload
    kept = [
        tool
        for tool in tools
        if not isinstance(tool, dict) or server.allows(str(tool.get("name") or ""))
    ]
    if len(kept) == len(tools):
        return payload
    return {**payload, "result": {**result, "tools": kept}}


def filter_json_body(body: bytes, server: McpServer) -> bytes:
    """Filter a buffered ``application/json`` reply, if it needs it."""
    if server.unfiltered:
        return body
    try:
        document = json.loads(body or b"null")
    except (json.JSONDecodeError, ValueError):
        return body
    filtered = (
        [filter_message(item, server) for item in document]
        if isinstance(document, list)
        else filter_message(document, server)
    )
    return json.dumps(filtered).encode()


def filter_sse_frame(frame: bytes, server: McpServer) -> bytes:
    """Filter one SSE frame, passing through everything that is not a catalogue.

    Event names, ids, retry directives and comments are left exactly as the
    upstream wrote them: the transport is its business, and only the tool list
    is ours.
    """
    if server.unfiltered:
        return frame
    lines = frame.split(b"\n")
    data = b"\n".join(line[5:].lstrip() for line in lines if line.startswith(b"data:"))
    if not data:
        return frame
    try:
        payload = json.loads(data)
    except (json.JSONDecodeError, ValueError):
        return frame
    filtered = filter_message(payload, server)
    if filtered is payload:
        return frame
    rewritten = [line for line in lines if not line.startswith(b"data:")]
    rewritten.append(b"data: " + json.dumps(filtered).encode())
    return b"\n".join(rewritten)


class SseFilter:
    """Rewrites an SSE stream frame by frame as it passes through.

    Streaming rather than buffering is what makes this usable on the long-lived
    ``GET`` stream, and on a ``tools/call`` that reports progress before it
    answers: neither can be held until it completes, because neither does.
    """

    def __init__(self, server: McpServer) -> None:
        """Filter frames for one server."""
        self._server = server
        self._buffer = b""

    def feed(self, chunk: bytes) -> bytes:
        """Consume a chunk of upstream bytes, returning complete filtered frames."""
        self._buffer += chunk
        out: list[bytes] = []
        while (index := self._buffer.find(_FRAME_SEPARATOR)) != -1:
            frame, self._buffer = self._buffer[:index], self._buffer[index + 2 :]
            out.append(filter_sse_frame(frame, self._server) + _FRAME_SEPARATOR)
        if len(self._buffer) > MAX_FRAME_BYTES:
            # An upstream that never terminates a frame would otherwise grow
            # this without bound. Release it unfiltered and start over rather
            # than hold it: the request has already gone wrong, and the tool
            # allowlist is still enforced on the way *in*.
            out.append(self._buffer)
            self._buffer = b""
        return b"".join(out)

    def flush(self) -> bytes:
        """Return whatever trailing bytes never formed a complete frame."""
        tail, self._buffer = self._buffer, b""
        return filter_sse_frame(tail, self._server) if tail else b""


__all__ = [
    "MAX_BUFFERED_RESPONSE_BYTES",
    "MAX_FRAME_BYTES",
    "METHOD_NOT_FOUND",
    "TOOLS_CALL",
    "TOOLS_LIST",
    "RequestPlan",
    "SseFilter",
    "filter_json_body",
    "filter_message",
    "filter_sse_frame",
    "plan_request",
]
