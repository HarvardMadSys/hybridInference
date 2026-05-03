#!/usr/bin/env python3
"""Lightweight webhook receiver that logs Alertmanager notifications to JSONL.

Listens on 127.0.0.1:5001 and appends each alert payload (with receive
timestamp) to var/log/alert_history.jsonl. Designed to run alongside
Alertmanager on the same machine.

Usage:
    python3 alert_logger.py [--port 5001] [--log-dir /path/to/var/log]
"""

import argparse
import contextlib
import json
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path


def resolve_log_dir(cli_arg: str | None) -> Path:
    """Return the log directory, defaulting to <project_root>/var/log."""
    if cli_arg:
        return Path(cli_arg)
    return Path(__file__).resolve().parent.parent.parent / "var" / "log"


class AlertHandler(BaseHTTPRequestHandler):
    """HTTP handler that writes Alertmanager webhook payloads to JSONL."""

    log_path: Path  # set by factory

    def do_POST(self):
        """Accept POST /alerts and append the JSON payload to the log file."""
        if self.path != "/alerts":
            self.send_response(404)
            self.end_headers()
            return

        content_length = int(self.headers.get("Content-Length", 0))
        if content_length == 0:
            self.send_response(400)
            self.end_headers()
            return

        body = self.rfile.read(content_length)
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            self.send_response(400)
            self.end_headers()
            return

        record = {
            "received_at": datetime.now(timezone.utc).isoformat(),
            "payload": payload,
        }
        with open(self.log_path, "a") as f:
            f.write(json.dumps(record, separators=(",", ":")) + "\n")

        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"status":"ok"}')

    def log_message(self, format, *args):
        """Suppress per-request access logs to stderr."""

    # needed: 'format' shadows builtin, but signature is defined by BaseHTTPRequestHandler


def main():
    """Start the alert webhook logger server."""
    parser = argparse.ArgumentParser(description="Alertmanager webhook JSONL logger")
    parser.add_argument("--port", type=int, default=5001)
    parser.add_argument("--bind", type=str, default="127.0.0.1")
    parser.add_argument("--log-dir", type=str, default=None)
    args = parser.parse_args()

    log_dir = resolve_log_dir(args.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "alert_history.jsonl"

    AlertHandler.log_path = log_path

    server = HTTPServer((args.bind, args.port), AlertHandler)
    print(f"alert-logger listening on {args.bind}:{args.port}, writing to {log_path}", flush=True)
    with contextlib.suppress(KeyboardInterrupt):
        server.serve_forever()
    server.server_close()


if __name__ == "__main__":
    main()
