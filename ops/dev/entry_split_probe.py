#!/usr/bin/env python3
r"""Probe a deployment's inference path from outside, without retries.

Run against the public base URL of a deployment -- through its entry, the way
clients reach it -- while a release, restart or entry change is under way, to
see whether inference requests survive it. It is the probe of the inference
entry split design (docs/agents/specs/2026-11-11-inference-entry-split-design.zh.md,
section 8.1), and it works the same on either topology: the console's
built-in forwarding or an entry that sends ``/v1`` and ``/anthropic`` straight
to the gateway.

What one run does:

* **Long streams.** ``--long-streams`` streaming requests, spread over Chat
  Completions, Responses and Messages. Each records when every frame arrived,
  checks the terminal marker (``[DONE]``, ``response.completed``,
  ``message_stop``) and the usage report, and, against the example's fake
  provider started with ``--stream-frames N --frame-interval-ms T``, checks
  that frames ``[000000]`` .. ``N-1`` all arrived once, in order, ``T`` apart
  within ``--interval-tolerance-ms``.
* **Short requests at a fixed rate**, streaming and not, on
  ``/v1/chat/completions``, ``/v1/responses`` and ``/anthropic/v1/messages``,
  for as long as the long streams run (or ``--duration``).
* **Boundary checks**, driven by ``deploy/public-paths.json``: each
  ``entry_split`` prefix reaches the gateway and carries
  ``X-Content-Type-Options: nosniff``; a trailing slash redirects to the same
  origin; the gateway's unpublished paths (``/docs``, ``/openapi.json``,
  ``/health/deep``, dot-segment escapes such as ``/v1/%2e%2e/openapi.json``,
  ``//v1``) do not answer; a missing or wrong key gets a 401.
* ``/health``, every few seconds, recorded but never counted: a health check
  says nothing about whether requests got through.

Raw ``http.client``, one connection per request, and **no retries**: every
failed request is a failure in the report. The API key is read from the
environment variable ``--api-key-env`` names (``PROBE_API_KEY`` by default)
and is never printed or written to the report.

The report is JSON, on stdout or in ``--output``. The exit status is 0 when
nothing failed, 1 when anything did, 2 for a usage error. Typical run, every
value your own::

    PROBE_API_KEY=... python ops/dev/entry_split_probe.py \
        --base-url https://api.example.org --model probe-long \
        --short-model probe-short --expect-frames 600 --frame-interval-ms 100 \
        --short-rate 2 --output probe.json
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import statistics
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from itertools import pairwise
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

DEFAULT_MANIFEST = Path(__file__).resolve().parents[2] / "deploy" / "public-paths.json"

#: Where each protocol is served, under the entry-split prefixes.
ENDPOINTS = {
    "chat": "/v1/chat/completions",
    "responses": "/v1/responses",
    "messages": "/anthropic/v1/messages",
}
PROTOCOLS = tuple(ENDPOINTS)

#: A numbered frame of the example's fake provider: ``[000042]``.
FRAME_MARKER = re.compile(r"\[(\d{6})\]")

#: The gateway's paths that no entry may publish, and the ways an entry rule
#: matched before normalization could still reach them. The split prefixes'
#: own escapes are added from the manifest (``boundary_paths``).
UNPUBLISHED_GATEWAY_PATHS = (
    "/docs",
    "/redoc",
    "/openapi.json",
    "/health/deep",
    "/health/ready",
    "/health/model-activity",
    "/internal/entry-split-probe",
)

#: Sent as the wrong key. Not a key of any deployment.
INVALID_KEY = "entry-split-probe-not-a-key"

_SNIPPET = 300


# --- parsing ---------------------------------------------------------------


@dataclass
class SSEEvent:
    """One server-sent event; ``comment`` for a ``:`` line (a keepalive)."""

    event: str | None = None
    data: str = ""
    comment: bool = False


class SSEParser:
    """Turn the bytes of an event stream into events, however they were split."""

    def __init__(self) -> None:
        self._buffer = b""
        self._event: str | None = None
        self._data: list[str] = []

    def feed(self, chunk: bytes) -> list[SSEEvent]:
        """Add *chunk* and return every event it completed."""
        self._buffer += chunk
        events: list[SSEEvent] = []
        while True:
            line, sep, rest = self._buffer.partition(b"\n")
            if not sep:
                return events
            self._buffer = rest
            text = line.rstrip(b"\r").decode("utf-8", errors="replace")
            if not text:
                if self._data or self._event is not None:
                    events.append(SSEEvent(self._event, "\n".join(self._data)))
                self._event, self._data = None, []
            elif text.startswith(":"):
                events.append(SSEEvent(comment=True, data=text[1:].strip()))
            else:
                name, _, value = text.partition(":")
                value = value[1:] if value.startswith(" ") else value
                if name == "event":
                    self._event = value
                elif name == "data":
                    self._data.append(value)

    @property
    def pending(self) -> bool:
        """Whether bytes of an unfinished event are still held."""
        return bool(self._buffer.strip() or self._data)


@dataclass
class Reading:
    """What one event says, for the protocol it arrived on."""

    text: str | None = None
    terminal: bool = False
    usage: dict[str, Any] | None = None
    error: str | None = None


def read_event(protocol: str, event: SSEEvent) -> Reading:
    """Interpret *event* as part of a *protocol* stream."""
    if event.comment:
        return Reading()
    if protocol == "chat" and event.data.strip() == "[DONE]":
        return Reading(terminal=True)
    try:
        body = json.loads(event.data) if event.data else {}
    except json.JSONDecodeError:
        return Reading(error=f"unparseable event: {event.data[:_SNIPPET]!r}")
    if not isinstance(body, dict):
        return Reading(error=f"unexpected event: {event.data[:_SNIPPET]!r}")
    kind = body.get("type") or event.event
    if "error" in body or kind in {"error", "response.failed", "response.incomplete"}:
        return Reading(error=f"error event: {event.data[:_SNIPPET]}")
    if protocol == "chat":
        choices = body.get("choices") or []
        delta = choices[0].get("delta", {}) if choices else {}
        return Reading(text=delta.get("content") or None, usage=body.get("usage") or None)
    if protocol == "responses":
        if kind == "response.output_text.delta":
            return Reading(text=body.get("delta") or None)
        if kind == "response.completed":
            return Reading(terminal=True, usage=(body.get("response") or {}).get("usage"))
        return Reading()
    if kind == "content_block_delta":
        delta = body.get("delta") or {}
        return Reading(text=delta.get("text") if delta.get("type") == "text_delta" else None)
    if kind == "message_delta":
        return Reading(usage=body.get("usage") or None)
    if kind == "message_stop":
        return Reading(terminal=True)
    return Reading()


def check_sequence(seen: list[int], expected: int) -> list[str]:
    """What is wrong with the frame numbers *seen*, when 0..expected-1 were sent."""
    problems: list[str] = []
    counts: dict[int, int] = {}
    for number in seen:
        counts[number] = counts.get(number, 0) + 1
    missing = [n for n in range(expected) if n not in counts]
    repeated = sorted(n for n, count in counts.items() if count > 1)
    unexpected = sorted(n for n in counts if n >= expected)
    if missing:
        problems.append(f"missing frames: {_numbers(missing)}")
    if repeated:
        problems.append(f"repeated frames: {_numbers(repeated)}")
    if unexpected:
        problems.append(f"frames never sent: {_numbers(unexpected)}")
    if not repeated and seen != sorted(seen):
        problems.append("frames out of order")
    return problems


def _numbers(values: list[int]) -> str:
    shown = ", ".join(str(v) for v in values[:10])
    return f"{shown} (+{len(values) - 10} more)" if len(values) > 10 else shown


def usage_complete(protocol: str, usage: dict[str, Any] | None) -> bool:
    """Whether *usage* reports the tokens a *protocol* response must report."""
    if not isinstance(usage, dict):
        return False
    keys = ("input_tokens", "output_tokens")
    if protocol == "chat":
        keys = ("prompt_tokens", "completion_tokens")
    return all(isinstance(usage.get(key), int) for key in keys)


@dataclass
class StreamReport:
    """The outcome of one streaming request."""

    protocol: str
    path: str
    status: int | None = None
    started_at: float = 0.0
    first_byte_s: float | None = None
    first_text_s: float | None = None
    duration_s: float | None = None
    frames: int = 0
    keepalives: int = 0
    terminal: bool = False
    usage: dict[str, Any] | None = None
    sequence_checked: bool = False
    gaps_ms: dict[str, float] | None = None
    off_gaps: list[list[float]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Whether the stream arrived whole."""
        return not self.errors


class StreamCheck:
    """Accumulate a stream's events and judge it once it has ended."""

    def __init__(
        self,
        protocol: str,
        report: StreamReport,
        *,
        expected_frames: int | None = None,
        interval_ms: float | None = None,
        tolerance_ms: float = 50.0,
    ) -> None:
        self.protocol = protocol
        self.report = report
        self.expected_frames = expected_frames
        self.interval_ms = interval_ms
        self.tolerance_ms = tolerance_ms
        self.parser = SSEParser()
        self.text = ""
        self._scanned = 0
        self.sequence: list[int] = []
        self.arrivals: list[float] = []
        self.after_terminal = False

    def feed(self, chunk: bytes, at: float) -> None:
        """Take *chunk*, which arrived *at* seconds into the request."""
        for event in self.parser.feed(chunk):
            if event.comment:
                self.report.keepalives += 1
                continue
            reading = read_event(self.protocol, event)
            if reading.error:
                self.report.errors.append(reading.error)
            if reading.usage:
                self.report.usage = reading.usage
            if reading.text:
                if self.report.terminal:
                    self.after_terminal = True
                if self.report.first_text_s is None:
                    self.report.first_text_s = at
                self.report.frames += 1
                self.text += reading.text
                # A number may arrive split over two deltas: it counts when complete.
                for match in FRAME_MARKER.finditer(self.text, self._scanned):
                    self.sequence.append(int(match.group(1)))
                    self.arrivals.append(at)
                    self._scanned = match.end()
            if reading.terminal:
                self.report.terminal = True

    def finish(self, transport_error: str | None = None) -> StreamReport:
        """Judge the stream; *transport_error* is how it broke off, if it did."""
        report = self.report
        end_marker = {"chat": "[DONE]", "responses": "response.completed"}.get(
            self.protocol, "message_stop"
        )
        if transport_error and report.status is None:
            report.errors.append(f"no response: {transport_error}")
            return report
        if transport_error:
            report.errors.append(f"truncated: {transport_error}")
        elif not report.terminal:
            report.errors.append(f"truncated: the stream ended without {end_marker}")
        if self.parser.pending:
            report.errors.append("truncated: the stream ended inside an event")
        if self.after_terminal:
            report.errors.append(f"content after {end_marker}")
        if report.terminal and not usage_complete(self.protocol, report.usage):
            report.errors.append("usage missing or incomplete")
        if self.expected_frames is not None:
            report.sequence_checked = True
            report.errors.extend(check_sequence(self.sequence, self.expected_frames))
        if self.interval_ms is not None and len(self.arrivals) > 1:
            gaps = [(later - earlier) * 1000.0 for earlier, later in pairwise(self.arrivals)]
            report.gaps_ms = {
                "min": round(min(gaps), 1),
                "median": round(statistics.median(gaps), 1),
                "max": round(max(gaps), 1),
                "expected": self.interval_ms,
            }
            off = [g for g in gaps if abs(g - self.interval_ms) > self.tolerance_ms]
            # Which gaps, by the frame that closed them: [frame, ms], the first ten.
            report.off_gaps = [
                [self.sequence[i + 1] if i + 1 < len(self.sequence) else i + 1, round(g, 1)]
                for i, g in enumerate(gaps)
                if abs(g - self.interval_ms) > self.tolerance_ms
            ][:10]
            if off:
                report.errors.append(
                    f"{len(off)} of {len(gaps)} frame gaps off {self.interval_ms} ms "
                    f"by more than {self.tolerance_ms} ms"
                )
        return report


def check_completion(protocol: str, status: int, body: bytes) -> list[str]:
    """What is wrong with a non-streaming *protocol* answer."""
    if status != 200:
        return [f"status {status}: {body[:_SNIPPET].decode('utf-8', 'replace')}"]
    try:
        answer = json.loads(body)
    except json.JSONDecodeError:
        return [f"unparseable body: {body[:_SNIPPET]!r}"]
    text: Any = None
    if protocol == "chat":
        text = ((answer.get("choices") or [{}])[0].get("message") or {}).get("content")
    elif protocol == "responses":
        if answer.get("status") != "completed":
            return [f"response status {answer.get('status')!r}"]
        parts = [p for item in answer.get("output") or [] for p in item.get("content") or []]
        text = "".join(p.get("text", "") for p in parts if p.get("type") == "output_text")
    else:
        text = "".join(
            b.get("text", "") for b in answer.get("content") or [] if b.get("type") == "text"
        )
    problems = [] if text else ["no text in the answer"]
    if not usage_complete(protocol, answer.get("usage")):
        problems.append("usage missing or incomplete")
    return problems


# --- the boundary ------------------------------------------------------------


def load_manifest(path: Path) -> list[dict[str, Any]]:
    """The entries of the public path manifest."""
    return json.loads(path.read_text())["paths"]


def resolved(path: str) -> str:
    """*path* as an entry must match it (E1): dots and slashes decoded, dot segments resolved."""
    decoded = re.sub(r"%2[eE]", ".", re.sub(r"%2[fF]", "/", path))
    output: list[str] = []
    for segment in decoded.split("/")[1:]:
        if segment == "..":
            if output:
                output.pop()
        elif segment != ".":
            output.append(segment)
    tail = "/" if output and decoded.endswith(("/.", "/..")) else ""
    return "/" + "/".join(output) + tail


def covered(path: str, entries: list[dict[str, Any]]) -> bool:
    """Whether the manifest publishes *path*, once its dot segments are resolved."""
    path = resolved(path)
    for entry in entries:
        pattern = re.escape(entry["path"]).replace(r"\:", ":")
        pattern = re.sub(r":[A-Za-z]+", "[^/]+", pattern)
        tail = "(/.*)?" if entry["match"] == "prefix" else ""
        if re.fullmatch(pattern + tail, path):
            return True
    return False


def boundary_paths(entries: list[dict[str, Any]]) -> list[str]:
    """Paths that must not reach anything the manifest does not publish."""
    escapes: list[str] = []
    for entry in entries:
        if not entry["entry_split"]:
            continue
        prefix = entry["path"]
        escapes += [
            f"{prefix}/%2e%2e/openapi.json",
            f"{prefix}/%2E%2E/health/deep",
            f"{prefix}/../openapi.json",
            f"{prefix}/..%2fopenapi.json",
            f"/{prefix}",
            f"/{prefix}/../docs",
        ]
    return [*UNPUBLISHED_GATEWAY_PATHS, *escapes]


def leak(status: int, headers: dict[str, str], body: bytes) -> str | None:
    """Why this answer shows an unpublished gateway path was reached, or None."""
    text = body[:65536].decode("utf-8", errors="replace")
    try:
        document = json.loads(text) if status == 200 else None
    except json.JSONDecodeError:
        document = None
    if isinstance(document, dict) and "openapi" in document and "paths" in document:
        return "served the OpenAPI document"
    # The scripts FastAPI's two documentation pages load; a page of the
    # console's that merely names the path it was asked for has neither.
    if "swagger-ui-bundle.js" in text:
        return "served Swagger UI"
    if "redoc.standalone.js" in text:
        return "served ReDoc"
    if "x-request-id" in headers and status != 404 and not 300 <= status < 400:
        return f"the gateway answered {status}"
    return None


# --- HTTP ----------------------------------------------------------------------


@dataclass
class Target:
    """Where the probe sends requests."""

    scheme: str
    host: str
    port: int
    prefix: str

    @classmethod
    def parse(cls, base_url: str) -> Target:
        """Read a base URL such as ``https://api.example.org``."""
        parts = urlsplit(base_url)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            raise ValueError(f"not an http(s) base URL: {base_url!r}")
        port = parts.port or (443 if parts.scheme == "https" else 80)
        return cls(parts.scheme, parts.hostname, port, parts.path.rstrip("/"))

    @property
    def origin(self) -> str:
        """``scheme://host[:port]``."""
        default = 443 if self.scheme == "https" else 80
        port = "" if self.port == default else f":{self.port}"
        return f"{self.scheme}://{self.host}{port}"

    def connect(self, timeout: float) -> http.client.HTTPConnection:
        """A new connection; the probe never reuses one."""
        if self.scheme == "https":
            return http.client.HTTPSConnection(self.host, self.port, timeout=timeout)
        return http.client.HTTPConnection(self.host, self.port, timeout=timeout)


def _headers(api_key: str | None, protocol: str | None = None) -> dict[str, str]:
    headers = {"Content-Type": "application/json", "User-Agent": "entry-split-probe/1"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
        if protocol == "messages":
            headers["x-api-key"] = api_key
    if protocol == "messages":
        headers["anthropic-version"] = "2023-06-01"
    return headers


def request_body(protocol: str, model: str, stream: bool) -> dict[str, Any]:
    """A minimal request for *protocol*."""
    if protocol == "responses":
        return {"model": model, "input": "entry split probe", "stream": stream}
    body: dict[str, Any] = {
        "model": model,
        "stream": stream,
        "messages": [{"role": "user", "content": "entry split probe"}],
    }
    if protocol == "messages":
        body["max_tokens"] = 64
    elif stream:
        body["stream_options"] = {"include_usage": True}
    return body


def simple_request(
    target: Target,
    method: str,
    path: str,
    *,
    api_key: str | None = None,
    body: bytes | None = None,
    timeout: float = 30.0,
) -> tuple[int, dict[str, str], bytes]:
    """One request, read whole. Headers come back lower-cased."""
    connection = target.connect(timeout)
    try:
        connection.request(method, target.prefix + path, body=body, headers=_headers(api_key))
        response = connection.getresponse()
        return response.status, {k.lower(): v for k, v in response.getheaders()}, response.read()
    finally:
        connection.close()


def run_stream(
    target: Target,
    api_key: str,
    protocol: str,
    model: str,
    *,
    expected_frames: int | None = None,
    interval_ms: float | None = None,
    tolerance_ms: float = 50.0,
    read_timeout: float = 120.0,
) -> StreamReport:
    """Send one streaming request and follow it to its end."""
    path = ENDPOINTS[protocol]
    report = StreamReport(protocol=protocol, path=path, started_at=time.time())
    check = StreamCheck(
        protocol,
        report,
        expected_frames=expected_frames,
        interval_ms=interval_ms,
        tolerance_ms=tolerance_ms,
    )
    started = time.monotonic()
    connection = target.connect(read_timeout)
    error: str | None = None
    try:
        body = json.dumps(request_body(protocol, model, True)).encode()
        connection.request(
            "POST", target.prefix + path, body=body, headers=_headers(api_key, protocol)
        )
        response = connection.getresponse()
        report.status = response.status
        if response.status != 200:
            detail = response.read()[:_SNIPPET].decode("utf-8", "replace")
            report.errors.append(f"status {response.status}: {detail}")
            report.duration_s = round(time.monotonic() - started, 3)
            return report
        while True:
            chunk = response.read1(65536)
            now = time.monotonic() - started
            if not chunk:
                break
            if report.first_byte_s is None:
                report.first_byte_s = round(now, 4)
            check.feed(chunk, now)
    except (OSError, http.client.HTTPException) as exc:
        error = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
    finally:
        connection.close()
    report.duration_s = round(time.monotonic() - started, 3)
    if report.first_text_s is not None:
        report.first_text_s = round(report.first_text_s, 4)
    return check.finish(error)


def run_short(target: Target, api_key: str, protocol: str, model: str, stream: bool) -> dict:
    """One short request; the outcome as a report row."""
    if stream:
        report = run_stream(target, api_key, protocol, model)
        return {
            "protocol": protocol,
            "stream": True,
            "status": report.status,
            "duration_s": report.duration_s,
            "errors": report.errors,
        }
    started = time.monotonic()
    errors: list[str]
    status: int | None = None
    try:
        body = json.dumps(request_body(protocol, model, False)).encode()
        connection = target.connect(120.0)
        try:
            connection.request(
                "POST",
                target.prefix + ENDPOINTS[protocol],
                body=body,
                headers=_headers(api_key, protocol),
            )
            response = connection.getresponse()
            status = response.status
            errors = check_completion(protocol, status, response.read())
        finally:
            connection.close()
    except (OSError, http.client.HTTPException) as exc:
        errors = [f"{type(exc).__name__}: {exc}"]
    return {
        "protocol": protocol,
        "stream": False,
        "status": status,
        "duration_s": round(time.monotonic() - started, 3),
        "errors": errors,
    }


def check_boundary(target: Target, api_key: str, entries: list[dict[str, Any]]) -> list[dict]:
    """The section 5.1 checks a client can make. Each row has ``ok``."""
    rows: list[dict] = []

    def row(name: str, problems: list[str], **detail: Any) -> None:
        rows.append({"check": name, "ok": not problems, "errors": problems, **detail})

    def attempt(name: str, method: str, path: str, **kwargs: Any):
        try:
            return simple_request(target, method, path, **kwargs)
        except (OSError, http.client.HTTPException) as exc:
            row(name, [f"{type(exc).__name__}: {exc}"], path=path)
            return None

    for entry in entries:
        if not entry["entry_split"]:
            continue
        path = f"{entry['path']}/entry-split-probe-not-found"
        name = f"{entry['path']} reaches the gateway"
        if (answer := attempt(name, "GET", path, api_key=api_key)) is None:
            continue
        status, headers, _ = answer
        problems = []
        if "x-request-id" not in headers:
            problems.append("no X-Request-ID: the gateway did not answer")
        if headers.get("x-content-type-options", "").split(",")[0].strip().lower() != "nosniff":
            problems.append("no X-Content-Type-Options: nosniff")
        row(name, problems, path=path, status=status)

    for method, path, bare in (
        ("GET", "/v1/models/", "/v1/models"),
        ("POST", "/anthropic/v1/messages/", "/anthropic/v1/messages"),
    ):
        name = f"trailing slash on {path}"
        body = b"{}" if method == "POST" else None
        if (answer := attempt(name, method, path, api_key=api_key, body=body)) is None:
            continue
        status, headers, _ = answer
        location = headers.get("location", "")
        resolved = urljoin(f"{target.origin}{target.prefix}{path}", location)
        problems = []
        if status not in (307, 308):
            problems.append(f"status {status}, not a method-preserving redirect")
        if resolved != f"{target.origin}{target.prefix}{bare}":
            problems.append(f"Location {location!r} leads to {resolved}")
        row(name, problems, path=path, status=status, location=location)

    for path in boundary_paths(entries):
        name = f"{path} is not published"
        if covered(path, entries):
            row(name, ["the manifest publishes this path; the probe's list is wrong"], path=path)
            continue
        if (answer := attempt(name, "GET", path)) is None:
            continue
        status, headers, body = answer
        reason = leak(status, headers, body)
        row(name, [reason] if reason else [], path=path, status=status)

    request = json.dumps(request_body("chat", "entry-split-probe", False)).encode()
    for name, key in (("no API key gets 401", None), ("a wrong API key gets 401", INVALID_KEY)):
        if (answer := attempt(name, "POST", ENDPOINTS["chat"], api_key=key, body=request)) is None:
            continue
        status, _, body = answer
        problems = [] if status == 401 else [f"status {status}"]
        row(name, problems, status=status, body=body[:_SNIPPET].decode("utf-8", "replace"))
    return rows


# --- the run -------------------------------------------------------------------


class Tally:
    """Short-request outcomes, from many threads."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.counts: dict[str, dict[str, int]] = {}
        self.failures: list[dict] = []
        self.not_sent = 0

    def add(self, row: dict) -> None:
        """Count one finished request."""
        key = f"{row['protocol']}:{'stream' if row['stream'] else 'json'}"
        with self._lock:
            count = self.counts.setdefault(key, {"sent": 0, "failed": 0})
            count["sent"] += 1
            if row["errors"]:
                count["failed"] += 1
                self.failures.append(row)

    def skip(self) -> None:
        """Count a request the schedule could not start."""
        with self._lock:
            self.not_sent += 1

    def summary(self) -> dict:
        """Totals and every failure."""
        with self._lock:
            sent = sum(c["sent"] for c in self.counts.values())
            failed = sum(c["failed"] for c in self.counts.values())
            return {
                "sent": sent,
                "failed": failed,
                "not_sent": self.not_sent,
                "by_kind": dict(self.counts),
                "failures": list(self.failures),
            }


def run(args: argparse.Namespace, api_key: str) -> dict:
    """Run every part the arguments ask for and return the report."""
    target = Target.parse(args.base_url)
    entries = load_manifest(args.manifest)
    report: dict[str, Any] = {
        "base_url": target.origin + target.prefix,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "settings": {
            "model": args.model,
            "short_model": args.short_model or args.model,
            "long_streams": args.long_streams,
            "expect_frames": args.expect_frames,
            "frame_interval_ms": args.frame_interval_ms,
            "interval_tolerance_ms": args.interval_tolerance_ms,
            "short_rate": args.short_rate,
        },
    }
    if not args.skip_boundary:
        report["boundary"] = check_boundary(target, api_key, entries)

    long_reports: list[StreamReport] = []
    lock = threading.Lock()

    def long_stream(protocol: str) -> None:
        result = run_stream(
            target,
            api_key,
            protocol,
            args.model,
            expected_frames=args.expect_frames,
            interval_ms=args.frame_interval_ms,
            tolerance_ms=args.interval_tolerance_ms,
            read_timeout=args.read_timeout,
        )
        with lock:
            long_reports.append(result)

    threads = [
        threading.Thread(target=long_stream, args=(PROTOCOLS[i % len(PROTOCOLS)],), daemon=True)
        for i in range(args.long_streams)
    ]
    for thread in threads:
        thread.start()

    tally = Tally()
    health: list[dict] = []
    in_flight = threading.BoundedSemaphore(args.max_in_flight)
    kinds = [(protocol, stream) for protocol in PROTOCOLS for stream in (True, False)]
    started = time.monotonic()
    deadline = started + args.duration if args.duration else None
    next_health = started
    tick = 0
    workers: list[threading.Thread] = []

    def short(protocol: str, stream: bool) -> None:
        try:
            tally.add(run_short(target, api_key, protocol, args.short_model or args.model, stream))
        finally:
            in_flight.release()

    while True:
        now = time.monotonic()
        long_running = any(thread.is_alive() for thread in threads)
        if deadline is not None and now >= deadline and not long_running:
            break
        if deadline is None and not long_running:
            break
        if now >= next_health:
            next_health += args.health_every
            try:
                t0 = time.monotonic()
                status, _, _ = simple_request(target, "GET", "/health", timeout=10.0)
                health.append(
                    {
                        "at_s": round(now - started, 1),
                        "status": status,
                        "ms": round((time.monotonic() - t0) * 1000),
                    }
                )
            except (OSError, http.client.HTTPException) as exc:
                health.append({"at_s": round(now - started, 1), "error": type(exc).__name__})
        if args.short_rate > 0 and (deadline is None or now < deadline):
            due = started + tick / args.short_rate
            if now >= due:
                protocol, stream = kinds[tick % len(kinds)]
                tick += 1
                if in_flight.acquire(blocking=False):
                    worker = threading.Thread(target=short, args=(protocol, stream), daemon=True)
                    worker.start()
                    workers.append(worker)
                else:
                    tally.skip()
                continue
        time.sleep(0.01)

    for worker in workers:
        worker.join(timeout=args.read_timeout)
    for thread in threads:
        thread.join(timeout=args.read_timeout)

    report["long_streams"] = [
        {**asdict(r), "ok": r.ok} for r in sorted(long_reports, key=lambda r: r.started_at)
    ]
    report["short_requests"] = tally.summary()
    report["health"] = {"note": "auxiliary only; not counted", "samples": health}
    failures = sum(1 for r in long_reports if not r.ok) + (args.long_streams - len(long_reports))
    failures += report["short_requests"]["failed"] + report["short_requests"]["not_sent"]
    failures += sum(1 for row in report.get("boundary", []) if not row["ok"])
    report["failures"] = failures
    report["ok"] = failures == 0
    report["duration_s"] = round(time.monotonic() - started, 1)
    return report


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """The command line."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--base-url", required=True, help="the deployment's public base URL")
    parser.add_argument("--model", required=True, help="model for the long streams")
    parser.add_argument("--short-model", help="model for the short requests (default: --model)")
    parser.add_argument(
        "--api-key-env", default="PROBE_API_KEY", help="environment variable holding the API key"
    )
    parser.add_argument(
        "--manifest", type=Path, default=DEFAULT_MANIFEST, help="the public path manifest"
    )
    parser.add_argument(
        "--long-streams",
        type=int,
        default=3,
        help="concurrent long streams, spread over the protocols",
    )
    parser.add_argument(
        "--expect-frames", type=int, help="numbered frames the synthetic upstream sends per stream"
    )
    parser.add_argument(
        "--frame-interval-ms", type=float, help="the upstream's gap between numbered frames"
    )
    parser.add_argument(
        "--interval-tolerance-ms", type=float, default=50.0, help="allowed deviation of each gap"
    )
    parser.add_argument(
        "--short-rate", type=float, default=1.0, help="short requests per second (0: none)"
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=0.0,
        help="seconds of short requests (0: while the long streams run)",
    )
    parser.add_argument(
        "--max-in-flight",
        type=int,
        default=32,
        help="short requests open at once; past it a tick counts as not sent",
    )
    parser.add_argument(
        "--health-every", type=float, default=5.0, help="seconds between /health samples"
    )
    parser.add_argument(
        "--read-timeout",
        type=float,
        default=120.0,
        help="seconds of silence before a request fails",
    )
    parser.add_argument("--skip-boundary", action="store_true", help="skip the boundary checks")
    parser.add_argument("--output", type=Path, help="write the JSON report here instead of stdout")
    args = parser.parse_args(argv)
    if (
        args.long_streams < 0
        or args.max_in_flight < 1
        or args.short_rate < 0
        or args.health_every <= 0
    ):
        parser.error("counts and rates must not be negative")
    if args.frame_interval_ms is not None and args.expect_frames is None:
        parser.error("--frame-interval-ms requires --expect-frames")
    if args.long_streams == 0 and not args.duration and args.short_rate:
        args.duration = 10.0
    return args


def main(argv: list[str] | None = None) -> int:
    """Run the probe; 0 when nothing failed."""
    args = parse_args(argv)
    api_key = os.environ.get(args.api_key_env, "")
    if not api_key:
        print(f"set the API key in ${args.api_key_env}", file=sys.stderr)
        return 2
    try:
        Target.parse(args.base_url)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    report = run(args, api_key)
    text = json.dumps(report, indent=2)
    if args.output:
        args.output.write_text(text + "\n")
        print(
            f"{'OK' if report['ok'] else 'FAILED'}: {report['failures']} failure(s); report in {args.output}"
        )
    else:
        print(text)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
