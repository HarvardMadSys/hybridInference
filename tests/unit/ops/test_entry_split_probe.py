"""The entry-split probe's parsing and accounting, without a deployment.

Everything but the last two tests is pure: events in, verdict out. Those two
serve a stream from a loopback socket, so the reading loop itself is covered:
a stream the server cuts off must come back as truncated, never as whole.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from ops.dev import entry_split_probe as probe


def _chat(content: str | None = None, *, usage: dict | None = None, finish: bool = False) -> bytes:
    delta = {} if content is None else {"content": content}
    event: dict = {
        "choices": [{"index": 0, "delta": delta, "finish_reason": "stop" if finish else None}]
    }
    if usage is not None:
        event = {"choices": [], "usage": usage}
    return b"data: " + json.dumps(event).encode() + b"\n\n"


_CHAT_USAGE = {"prompt_tokens": 4, "completion_tokens": 3, "total_tokens": 7}


def _whole_chat_stream(numbers: list[int]) -> list[bytes]:
    frames = [_chat("")]
    frames += [_chat(f"[{n:06d}]") for n in numbers]
    return [*frames, _chat(finish=True), _chat(usage=_CHAT_USAGE), b"data: [DONE]\n\n"]


def _check(protocol: str = "chat", **kwargs) -> probe.StreamCheck:
    report = probe.StreamReport(protocol=protocol, path=probe.ENDPOINTS[protocol])
    return probe.StreamCheck(protocol, report, **kwargs)


def _feed(check: probe.StreamCheck, frames: list[bytes], gap_s: float = 0.1) -> None:
    for index, frame in enumerate(frames):
        check.feed(frame, index * gap_s)


# --- the event stream --------------------------------------------------------


def test_the_parser_reassembles_events_split_anywhere() -> None:
    stream = b'event: message_stop\r\ndata: {"type":\r\ndata: "x"}\r\n\r\n: keepalive\n\ndata: [DONE]\n\n'
    for size in (1, 2, 5, 7, len(stream)):
        parser = probe.SSEParser()
        events = []
        for i in range(0, len(stream), size):
            events += parser.feed(stream[i : i + size])
        assert events == [
            probe.SSEEvent("message_stop", '{"type":\n"x"}'),
            probe.SSEEvent(None, "keepalive", comment=True),
            probe.SSEEvent(None, "[DONE]"),
        ], size
        assert not parser.pending


def test_the_parser_knows_when_it_holds_half_an_event() -> None:
    parser = probe.SSEParser()
    parser.feed(b'data: {"choices": [')
    assert parser.pending


# --- one stream ----------------------------------------------------------------


def test_a_whole_numbered_chat_stream_passes() -> None:
    check = _check(expected_frames=3, interval_ms=100.0)
    _feed(check, _whole_chat_stream([0, 1, 2]))

    report = check.finish()

    assert report.ok, report.errors
    assert report.terminal and report.sequence_checked
    assert report.frames == 3
    assert report.gaps_ms == {"min": 100.0, "median": 100.0, "max": 100.0, "expected": 100.0}


def test_a_stream_without_its_end_marker_is_truncated() -> None:
    check = _check(expected_frames=3)
    _feed(check, _whole_chat_stream([0, 1, 2])[:-1])

    report = check.finish()

    assert "truncated: the stream ended without [DONE]" in report.errors


def test_a_request_that_got_no_response_says_so() -> None:
    check = _check(expected_frames=3)

    report = check.finish("ConnectionRefusedError: [Errno 111] Connection refused")

    assert report.errors == ["no response: ConnectionRefusedError: [Errno 111] Connection refused"]


def test_a_stream_the_connection_dropped_is_truncated() -> None:
    check = _check(expected_frames=3)
    check.report.status = 200
    _feed(check, _whole_chat_stream([0, 1])[:3])

    report = check.finish("IncompleteRead: IncompleteRead(0 bytes read)")

    assert report.errors[0] == "truncated: IncompleteRead: IncompleteRead(0 bytes read)"
    assert "missing frames: 2" in report.errors


def test_a_stream_cut_inside_an_event_is_truncated() -> None:
    check = _check()
    check.feed(_chat("[000000]") + b'data: {"choi', 0.0)

    assert "truncated: the stream ended inside an event" in check.finish().errors


@pytest.mark.parametrize(
    ("seen", "problem"),
    [
        ([0, 1, 3], "missing frames: 2"),
        ([0, 1, 1, 2], "repeated frames: 1"),
        ([0, 2, 1], "frames out of order"),
        ([0, 1, 2, 7], "frames never sent: 7"),
        ([], "missing frames: 0, 1, 2"),
    ],
)
def test_a_wrong_sequence_is_named(seen: list[int], problem: str) -> None:
    assert problem in probe.check_sequence(seen, 3)
    check = _check(expected_frames=3)
    _feed(check, _whole_chat_stream(seen))
    assert problem in check.finish().errors


def test_a_whole_sequence_has_no_problem() -> None:
    assert probe.check_sequence(list(range(5)), 5) == []


def test_a_frame_number_split_over_two_deltas_counts_once() -> None:
    check = _check(expected_frames=2)
    _feed(check, [_chat("[0000"), _chat("00][00"), _chat("0001]"), _chat(finish=True)])
    _feed(check, [_chat(usage=_CHAT_USAGE), b"data: [DONE]\n\n"])

    report = check.finish()

    assert check.sequence == [0, 1]
    assert report.ok, report.errors


def test_frames_that_arrive_in_a_burst_fail_the_timing_check() -> None:
    """What a buffering entry looks like: nothing for a while, then everything."""
    check = _check(expected_frames=4, interval_ms=100.0, tolerance_ms=50.0)
    frames = _whole_chat_stream([0, 1, 2, 3])
    for frame, at in zip(frames, [0.0, 0.0, 0.0, 0.0, 0.3, 0.3, 0.3, 0.3], strict=True):
        check.feed(frame, at)

    report = check.finish()

    assert "3 of 3 frame gaps off 100.0 ms by more than 50.0 ms" in report.errors
    assert report.off_gaps == [[1, 0.0], [2, 0.0], [3, 300.0]]


def test_a_stream_without_usage_fails() -> None:
    check = _check()
    _feed(check, [_chat("hi"), _chat(finish=True), b"data: [DONE]\n\n"])
    assert check.finish().errors == ["usage missing or incomplete"]


def test_an_error_event_fails_the_stream() -> None:
    check = _check()
    _feed(check, [b'data: {"error": {"message": "upstream failed"}}\n\n', b"data: [DONE]\n\n"])
    assert any(error.startswith("error event:") for error in check.finish().errors)


def test_keepalives_are_counted_and_ignored() -> None:
    check = _check(expected_frames=1)
    _feed(check, [b": keepalive\n\n", *_whole_chat_stream([0])])

    report = check.finish()

    assert report.keepalives == 1
    assert report.ok, report.errors


def test_a_responses_stream_ends_at_response_completed_with_its_usage() -> None:
    def event(kind: str, **fields) -> bytes:
        return f"event: {kind}\ndata: {json.dumps({'type': kind, **fields})}\n\n".encode()

    usage = {"input_tokens": 4, "output_tokens": 2, "total_tokens": 6}
    whole = [
        event("response.created", response={}),
        event("response.output_text.delta", delta="[000000]"),
        event("response.output_text.delta", delta="[000001]"),
        event("response.completed", response={"usage": usage}),
    ]
    check = _check("responses", expected_frames=2)
    _feed(check, whole)
    assert check.finish().ok

    cut = _check("responses", expected_frames=2)
    _feed(cut, whole[:-1])
    assert "truncated: the stream ended without response.completed" in cut.finish().errors

    failed = _check("responses")
    _feed(failed, [event("response.failed", response={"error": {"message": "x"}})])
    assert any(error.startswith("error event:") for error in failed.finish().errors)


def test_a_messages_stream_ends_at_message_stop_and_reports_usage() -> None:
    def event(kind: str, **fields) -> bytes:
        return f"event: {kind}\ndata: {json.dumps({'type': kind, **fields})}\n\n".encode()

    whole = [
        event("message_start", message={"usage": {"input_tokens": 4, "output_tokens": 0}}),
        event("content_block_delta", index=0, delta={"type": "text_delta", "text": "[000000]"}),
        event("message_delta", delta={}, usage={"input_tokens": 4, "output_tokens": 1}),
        event("message_stop"),
    ]
    check = _check("messages", expected_frames=1)
    _feed(check, whole)
    assert check.finish().ok

    no_usage = _check("messages")
    _feed(no_usage, [whole[1], whole[3]])
    assert no_usage.finish().errors == ["usage missing or incomplete"]

    cut = _check("messages")
    _feed(cut, whole[:2])
    assert "truncated: the stream ended without message_stop" in cut.finish().errors


def test_content_after_the_end_marker_fails() -> None:
    check = _check()
    _feed(check, [*_whole_chat_stream([0]), _chat("late")])
    assert "content after [DONE]" in check.finish().errors


# --- non-streaming answers ------------------------------------------------------


@pytest.mark.parametrize(
    ("protocol", "answer"),
    [
        ("chat", {"choices": [{"message": {"content": "ok"}}], "usage": _CHAT_USAGE}),
        (
            "responses",
            {
                "status": "completed",
                "output": [{"content": [{"type": "output_text", "text": "ok"}]}],
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        ),
        (
            "messages",
            {
                "content": [{"type": "text", "text": "ok"}],
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        ),
    ],
)
def test_a_complete_answer_passes_and_a_broken_one_does_not(protocol: str, answer: dict) -> None:
    assert probe.check_completion(protocol, 200, json.dumps(answer).encode()) == []
    assert probe.check_completion(protocol, 200, json.dumps({**answer, "usage": {}}).encode()) == [
        "usage missing or incomplete"
    ]
    assert probe.check_completion(protocol, 502, b'{"error":{}}')[0].startswith("status 502")
    assert probe.check_completion(protocol, 200, b"<html>")[0].startswith("unparseable body")


# --- the boundary ------------------------------------------------------------------


def test_the_boundary_list_names_nothing_the_manifest_publishes() -> None:
    entries = probe.load_manifest(probe.DEFAULT_MANIFEST)
    paths = probe.boundary_paths(entries)

    assert "/v1/%2e%2e/openapi.json" in paths
    assert "/anthropic/../openapi.json" in paths
    assert "//v1" in paths
    assert [path for path in paths if probe.covered(path, entries)] == []


def test_the_manifest_coverage_follows_its_match_rules() -> None:
    entries = probe.load_manifest(probe.DEFAULT_MANIFEST)
    assert probe.covered("/v1", entries)
    assert probe.covered("/v1/chat/completions", entries)
    assert not probe.covered("/v10", entries)
    assert probe.covered("/internal/users/u-1/status", entries)
    assert not probe.covered("/internal/users/u-1", entries)
    assert not probe.covered("/health/deep", entries)
    assert not probe.covered("/docs", entries)
    # Judged as an entry must judge it (E1): after the dot segments resolve.
    assert not probe.covered("/v1/%2e%2e/openapi.json", entries)
    assert not probe.covered("/v1/..%2fhealth/deep", entries)
    assert probe.covered("/v1/x/%2E%2E/models", entries)
    assert not probe.covered("//v1", entries)


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/v1/%2e%2e/openapi.json", "/openapi.json"),
        ("/v1/%2E%2E/health/deep", "/health/deep"),
        ("/v1/..%2fopenapi.json", "/openapi.json"),
        ("/v1/./models", "/v1/models"),
        ("/v1/a/b/../../..", "/"),
        ("/v1/..", "/"),
        ("//v1", "//v1"),
    ],
)
def test_a_path_is_resolved_as_an_entry_resolves_it(path: str, expected: str) -> None:
    assert probe.resolved(path) == expected


@pytest.mark.parametrize(
    ("status", "headers", "body", "leaked"),
    [
        (200, {}, b'{"openapi": "3.1.0", "paths": {}}', True),
        (200, {}, b"<script src='https://cdn/swagger-ui-dist@5/swagger-ui-bundle.js'>", True),
        (200, {}, b"<script src='https://cdn/redoc@2/bundles/redoc.standalone.js'>", True),
        # A console error page that names the path it was asked for.
        (500, {}, b'<html><script>self.__next_f.push(["/redoc"])</script>', False),
        (200, {}, b'{"openapi": "not this"}', False),
        (200, {"x-request-id": "r"}, b'{"status": "ok"}', True),
        (401, {"x-request-id": "r"}, b"{}", True),
        (404, {"x-request-id": "r"}, b'{"detail": "Not Found"}', False),
        (308, {"x-request-id": "r"}, b"", False),
        (404, {}, b"<html>console not found</html>", False),
        (200, {}, b"<html>a console page</html>", False),
    ],
)
def test_an_answer_leaks_only_when_it_shows_the_gateway_served_the_path(
    status: int, headers: dict, body: bytes, leaked: bool
) -> None:
    assert (probe.leak(status, headers, body) is not None) is leaked


# --- accounting ------------------------------------------------------------------


def test_the_tally_counts_every_failure() -> None:
    tally = probe.Tally()
    tally.add({"protocol": "chat", "stream": True, "errors": []})
    tally.add({"protocol": "chat", "stream": True, "errors": ["truncated: x"]})
    tally.add({"protocol": "messages", "stream": False, "errors": ["status 502"]})
    tally.skip()

    summary = tally.summary()

    assert summary["sent"] == 3
    assert summary["failed"] == 2
    assert summary["not_sent"] == 1
    assert summary["by_kind"] == {
        "chat:stream": {"sent": 2, "failed": 1},
        "messages:json": {"sent": 1, "failed": 1},
    }
    assert [row["errors"] for row in summary["failures"]] == [["truncated: x"], ["status 502"]]


def test_the_base_url_is_read_whole() -> None:
    target = probe.Target.parse("https://api.example.org/base/")
    assert (target.scheme, target.host, target.port, target.prefix) == (
        "https",
        "api.example.org",
        443,
        "/base",
    )
    assert target.origin == "https://api.example.org"
    with pytest.raises(ValueError):
        probe.Target.parse("ftp://example.org")


def test_it_refuses_to_run_without_a_key(monkeypatch, capsys) -> None:
    monkeypatch.delenv("PROBE_API_KEY", raising=False)
    assert probe.main(["--base-url", "http://127.0.0.1:9", "--model", "m"]) == 2
    assert "PROBE_API_KEY" in capsys.readouterr().err


# --- the reading loop, over a loopback socket ---------------------------------------


class _StreamServer(ThreadingHTTPServer):
    frames: list[bytes]


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:
        pass

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        for frame in self.server.frames:
            self.wfile.write(f"{len(frame):x}\r\n".encode() + frame + b"\r\n")
            self.wfile.flush()
        if self.server.frames is not _CUT:
            self.wfile.write(b"0\r\n\r\n")
        self.close_connection = True


_WHOLE = _whole_chat_stream([0, 1, 2])
_CUT = _WHOLE[:3]


@pytest.fixture
def stream_server():
    server = _StreamServer(("127.0.0.1", 0), _Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()
    server.server_close()


@pytest.mark.parametrize(("frames", "ok"), [(_WHOLE, True), (_CUT, False)])
def test_a_served_stream_is_judged_by_how_it_ended(stream_server, frames, ok) -> None:
    stream_server.frames = frames
    target = probe.Target.parse(f"http://127.0.0.1:{stream_server.server_port}")

    report = probe.run_stream(target, "test-key", "chat", "m", expected_frames=3, read_timeout=10)

    assert report.ok is ok, report.errors
    if not ok:
        assert report.errors[0].startswith("truncated: IncompleteRead")
        assert "missing frames: 2" in report.errors
