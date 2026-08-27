"""Small deterministic OpenAI-compatible server for runnable examples."""

from __future__ import annotations

import argparse
import hmac
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

RESPONSE_TEXT = "RUNNABLE_EXAMPLE_OK"


def build_completion(payload: dict[str, Any], response_text: str = RESPONSE_TEXT) -> dict[str, Any]:
    """Build a deterministic non-streaming OpenAI completion."""
    model = str(payload.get("model") or "example-upstream")
    return {
        "id": "chatcmpl-runnable-example",
        "object": "chat.completion",
        "created": 0,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": response_text},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 4, "completion_tokens": 4, "total_tokens": 8},
    }


def build_stream_frames(
    payload: dict[str, Any], response_text: str = RESPONSE_TEXT
) -> tuple[bytes, ...]:
    """Build deterministic OpenAI-compatible SSE frames."""
    model = str(payload.get("model") or "example-upstream")

    def chunk(delta: dict[str, Any], finish_reason: str | None) -> dict[str, Any]:
        return {
            "id": "chatcmpl-runnable-example",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "delta": delta,
                    "finish_reason": finish_reason,
                }
            ],
        }

    events = (
        chunk({"role": "assistant", "content": ""}, None),
        chunk({"content": response_text}, None),
        chunk({}, "stop"),
    )
    frames = tuple(
        b"data: " + json.dumps(event, separators=(",", ":")).encode("utf-8") + b"\n\n"
        for event in events
    )
    return (*frames, b"data: [DONE]\n\n")


class FakeHandler(BaseHTTPRequestHandler):
    """Serve the minimal health, models, and chat-completions surface."""

    protocol_version = "HTTP/1.1"
    response_text = RESPONSE_TEXT
    expected_model: str | None = None
    expected_api_key: str | None = None

    def log_message(self, fmt: str, *args: Any) -> None:
        """Keep example output quiet; the gateway logs the routed request."""

    def do_GET(self) -> None:
        """Serve health and model discovery."""
        if self.path == "/health":
            self._send_json(200, {"status": "ok"})
            return
        if self.path == "/v1/models":
            model = self.expected_model or "example-upstream"
            self._send_json(
                200,
                {
                    "object": "list",
                    "data": [{"id": model, "object": "model"}],
                },
            )
            return
        self._send_json(404, {"error": {"message": f"Unknown path: {self.path}"}})

    def do_POST(self) -> None:
        """Serve one deterministic chat completion."""
        if self.path != "/v1/chat/completions":
            self._send_json(404, {"error": {"message": f"Unknown path: {self.path}"}})
            return

        try:
            payload = self._read_json()
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
            self._send_json(400, {"error": {"message": f"Invalid JSON body: {exc}"}})
            return

        if self.expected_api_key is not None:
            supplied = self.headers.get("Authorization", "")
            expected = f"Bearer {self.expected_api_key}"
            if not hmac.compare_digest(supplied, expected):
                self._send_json(401, {"error": {"message": "Invalid provider credential"}})
                return
        if self.expected_model is not None and payload.get("model") != self.expected_model:
            self._send_json(400, {"error": {"message": "Unexpected provider model"}})
            return

        if payload.get("stream"):
            self._send_stream(payload)
            return

        self._send_json(200, build_completion(payload, self.response_text))

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            raise ValueError("empty body")
        parsed = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(parsed, dict):
            raise ValueError("body must be a JSON object")
        return parsed

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def _send_stream(self, payload: dict[str, Any]) -> None:
        """Send deterministic SSE frames without timing-dependent delays."""
        self.close_connection = True
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            for frame in build_stream_frames(payload, self.response_text):
                self.wfile.write(frame)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return


def main() -> None:
    """Run the fake server until its container or process stops."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8351)
    parser.add_argument("--response-text", default=RESPONSE_TEXT)
    parser.add_argument("--expected-model")
    parser.add_argument("--expected-api-key")
    args = parser.parse_args()
    FakeHandler.response_text = args.response_text
    FakeHandler.expected_model = args.expected_model
    FakeHandler.expected_api_key = args.expected_api_key
    server = ThreadingHTTPServer((args.host, args.port), FakeHandler)
    server.daemon_threads = True
    print(f"OpenAI-compatible example provider listening on {args.host}:{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
