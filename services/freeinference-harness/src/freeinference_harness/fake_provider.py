"""Deterministic OpenAI-compatible fake provider for agent-loop conformance.

The fake replays :mod:`freeinference_harness.agent_scripts` verbatim so the
``runtime -> gateway -> fake`` chain becomes fully deterministic. It is
stdlib-only (no server dependency) and supports:

- streaming and non-streaming ``/v1/chat/completions``
- scripted tool-call argument fragmentation (one SSE delta per fragment)
- scripted first-attempt HTTP errors (e.g. 429 then success)
- scripted mid-stream disconnects (no finish chunk, no ``[DONE]``)

Requests without an ``[[agent-script:<id>]]`` marker get a harmless default
text response so gateway health probes never trip a circuit breaker.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from freeinference_harness.agent_scripts import (
    AgentScript,
    ScriptTurn,
    count_assistant_turns,
    find_script_id,
    get_script,
)

_USAGE = {"prompt_tokens": 17, "completion_tokens": 5, "total_tokens": 22}


class _ServeState:
    """Thread-safe per-(script, turn) serve counters for first-attempt errors."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._serves: dict[tuple[str, int], int] = {}

    def bump(self, script_id: str, turn_index: int) -> int:
        """Increments and returns the serve count for one scripted turn."""
        key = (script_id, turn_index)
        with self._lock:
            self._serves[key] = self._serves.get(key, 0) + 1
            return self._serves[key]

    def reset(self) -> None:
        """Clears all serve counters."""
        with self._lock:
            self._serves.clear()


class _FakeHTTPServer(ThreadingHTTPServer):
    """Threading HTTP server that carries the shared serve state."""

    daemon_threads = True

    def __init__(self, address: tuple[str, int]) -> None:
        super().__init__(address, FakeProviderHandler)
        self.state = _ServeState()


class FakeProviderHandler(BaseHTTPRequestHandler):
    """Request handler implementing the deterministic OpenAI surface."""

    protocol_version = "HTTP/1.1"
    server: _FakeHTTPServer

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        """Silences default per-request stderr logging."""

    # ── Routing ────────────────────────────────────────────────────────

    def do_GET(self) -> None:
        """Serves the model listing and health endpoints."""
        if self.path == "/v1/models":
            self._send_json(
                200,
                {"object": "list", "data": [{"id": "agent-loop-fake", "object": "model"}]},
            )
            return
        if self.path == "/health":
            self._send_json(200, {"status": "ok"})
            return
        self._send_json(404, {"error": {"message": f"Unknown path: {self.path}"}})

    def do_POST(self) -> None:
        """Serves chat completions and the reset control endpoint."""
        if self.path == "/v1/chat/completions":
            self._chat_completions()
            return
        if self.path == "/__fake__/reset":
            self.server.state.reset()
            self._send_json(200, {"ok": True})
            return
        self._send_json(404, {"error": {"message": f"Unknown path: {self.path}"}})

    # ── Chat completions ───────────────────────────────────────────────

    def _chat_completions(self) -> None:
        """Replays the scripted turn selected by marker and turn index."""
        try:
            payload = self._read_json()
        except ValueError as exc:
            self._send_json(400, {"error": {"message": f"Invalid JSON body: {exc}"}})
            return

        messages = payload.get("messages") or []
        model = str(payload.get("model") or "agent-loop-fake")
        stream = bool(payload.get("stream"))

        script_id = find_script_id(messages)
        if script_id is None:
            # Health probes and misc traffic: never fail, never touch state.
            default_turn = ScriptTurn(
                kind="text", text="agent-loop-fake: no script marker; serving default text."
            )
            self._serve_turn(
                default_turn, model=model, stream=stream, chunk_id="chatcmpl-fake-default"
            )
            return

        try:
            script = get_script(script_id)
        except KeyError as exc:
            self._send_json(400, {"error": {"message": str(exc)}})
            return

        turn_index = count_assistant_turns(messages)
        turn = script.turn_for(turn_index)
        attempt = self.server.state.bump(script.script_id, turn_index)

        if turn.status_first_attempt is not None and attempt == 1:
            self._send_json(
                turn.status_first_attempt,
                {
                    "error": {
                        "message": f"agent-loop-fake scripted {turn.status_first_attempt}",
                        "type": "rate_limit_error",
                    }
                },
                extra_headers={"Retry-After": "0"},
            )
            return

        chunk_id = f"chatcmpl-fake-{script.script_id}-{turn_index}"
        self._serve_turn(turn, model=model, stream=stream, chunk_id=chunk_id)

    def _serve_turn(self, turn: ScriptTurn, *, model: str, stream: bool, chunk_id: str) -> None:
        """Serves one scripted turn in streaming or non-streaming form."""
        if stream:
            self._stream_turn(turn, model=model, chunk_id=chunk_id)
        else:
            self._json_turn(turn, model=model, chunk_id=chunk_id)

    def _json_turn(self, turn: ScriptTurn, *, model: str, chunk_id: str) -> None:
        """Serves the non-streaming completion for one scripted turn."""
        message: dict[str, Any] = {"role": "assistant", "content": None}
        finish_reason = "stop"
        if turn.kind == "tool_call":
            message["tool_calls"] = [
                {
                    "id": f"call_fake_{chunk_id}",
                    "type": "function",
                    "function": {"name": turn.tool_name, "arguments": turn.joined_arguments},
                }
            ]
            finish_reason = "tool_calls"
        elif turn.kind in ("text", "disconnect"):
            # Non-streaming requests cannot model a mid-stream disconnect;
            # serve the joined text so the turn stays deterministic.
            message["content"] = turn.text or "".join(turn.text_fragments)
        elif turn.kind == "empty":
            message["content"] = ""

        self._send_json(
            200,
            {
                "id": chunk_id,
                "object": "chat.completion",
                "created": 0,
                "model": model,
                "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
                "usage": dict(_USAGE),
            },
        )

    def _stream_turn(self, turn: ScriptTurn, *, model: str, chunk_id: str) -> None:
        """Serves the streaming completion for one scripted turn."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

        def chunk(delta: dict[str, Any], finish_reason: str | None = None) -> dict[str, Any]:
            return {
                "id": chunk_id,
                "object": "chat.completion.chunk",
                "created": 0,
                "model": model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
            }

        self._sse(chunk({"role": "assistant"}))

        if turn.kind == "text":
            self._sse(chunk({"content": turn.text}))
            self._sse(chunk({}, finish_reason="stop"))
        elif turn.kind == "tool_call":
            for index, fragment in enumerate(turn.argument_fragments):
                tool_delta: dict[str, Any] = {
                    "index": 0,
                    "function": {"arguments": fragment},
                }
                if index == 0:
                    tool_delta["id"] = f"call_fake_{chunk_id}"
                    tool_delta["type"] = "function"
                    tool_delta["function"]["name"] = turn.tool_name
                self._sse(chunk({"tool_calls": [tool_delta]}))
            self._sse(chunk({}, finish_reason="tool_calls"))
        elif turn.kind == "empty":
            self._sse(chunk({}, finish_reason="stop"))
        elif turn.kind == "disconnect":
            for fragment in turn.text_fragments:
                self._sse(chunk({"content": fragment}))
            # Abrupt close: no finish chunk, no usage, no [DONE].
            return

        usage_chunk = {
            "id": chunk_id,
            "object": "chat.completion.chunk",
            "created": 0,
            "model": model,
            "choices": [],
            "usage": dict(_USAGE),
        }
        self._sse(usage_chunk)
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    # ── Low-level helpers ──────────────────────────────────────────────

    def _sse(self, obj: dict[str, Any]) -> None:
        """Writes one SSE data event."""
        body = json.dumps(obj, separators=(",", ":"))
        self.wfile.write(b"data: " + body.encode("utf-8") + b"\n\n")
        self.wfile.flush()

    def _read_json(self) -> dict[str, Any]:
        """Reads and parses the request body as JSON."""
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        if not raw:
            raise ValueError("empty body")
        parsed = json.loads(raw.decode("utf-8"))
        if not isinstance(parsed, dict):
            raise ValueError("body must be a JSON object")
        return parsed

    def _send_json(
        self,
        status: int,
        obj: dict[str, Any],
        *,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        """Sends a JSON response with an explicit content length."""
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True


class FakeProviderServer:
    """Threaded fake provider with deterministic agent-loop scripts."""

    def __init__(self, *, host: str = "127.0.0.1", port: int = 0) -> None:
        self._host = host
        self._port = port
        self._server: _FakeHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        """Returns the bound base URL (valid after start())."""
        if self._server is None:
            raise RuntimeError("FakeProviderServer is not running.")
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def start(self) -> FakeProviderServer:
        """Starts serving on a daemon thread and returns self."""
        if self._server is not None:
            return self
        self._server = _FakeHTTPServer((self._host, self._port))
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="agent-loop-fake-provider",
            daemon=True,
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        """Stops the server and joins the serving thread."""
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._server = None
        self._thread = None

    def reset(self) -> None:
        """Clears serve counters (first-attempt errors fire again)."""
        if self._server is not None:
            self._server.state.reset()


def serve_forever(*, host: str, port: int) -> int:
    """Runs the fake provider in the foreground until interrupted."""
    server = FakeProviderServer(host=host, port=port).start()
    print(f"agent-loop fake provider listening on {server.base_url}")
    print("Register it in a dev gateway as an openai_compat route, e.g. model 'agent-loop-fake'.")
    try:
        while True:
            threading.Event().wait(3600)
    except KeyboardInterrupt:
        server.stop()
    return 0


__all__ = ["AgentScript", "FakeProviderServer", "serve_forever"]
