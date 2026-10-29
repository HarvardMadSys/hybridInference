"""The console scanner's strict rules, line by line and field by field."""

from __future__ import annotations

import pytest

from serving.batch import console_prepare as cp
from serving.schemas import ChatCompletionRequest, EmbeddingRequest


def _problem(raw: bytes, line_no: int = 2) -> tuple[str, str]:
    with pytest.raises(cp._LineProblem) as caught:
        cp.parse_line(raw, line_no)
    return caught.value.code, caught.value.message


def test_a_bom_is_refused_only_where_a_file_starts():
    assert _problem(b'\xef\xbb\xbf{"a":1}', 1)[0] == "utf8_bom"
    # Elsewhere it is simply not JSON.
    assert _problem(b'\xef\xbb\xbf{"a":1}', 2)[0] == "invalid_json_line"


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        (b"", "empty_line"),
        (b"   \r", "empty_line"),
        (b"[1, 2]", "invalid_json_line"),
        (b'{"a": NaN}', "invalid_json_line"),
        (b'{"a": -Infinity}', "invalid_json_line"),
        (b'{"a": 1e400}', "invalid_json_line"),
        (b'{"a": "\\ud800"}', "invalid_json_line"),
        (b"\xff\xfe", "invalid_json_line"),
        (b'{"a": 1, "a": 2}', "duplicate_key"),
        (b'{"body": {"messages": [{"role": "user", "role": "user"}]}}', "duplicate_key"),
    ],
)
def test_lines_are_parsed_strictly(raw, code):
    assert _problem(raw)[0] == code


def test_problems_never_quote_the_line():
    secret = b"prompt-text-that-must-not-leak"
    for raw in (b'{"x": "' + secret + b'", ', b'{"' + secret + b'": 1, "z": NaN}'):
        _code, message = _problem(raw)
        assert secret.decode() not in message


def test_a_crlf_line_is_one_object():
    assert cp.parse_line(b'{"custom_id": "a"}\r', 3) == {"custom_id": "a"}


def test_only_undeclared_fields_are_unsupported_and_free_form_json_is_not_entered():
    body = {
        "model": "m",
        "messages": [
            {"role": "user", "content": {"anything": {"goes": 1}}},
            {"role": "assistant", "content": "x", "audio": {"id": "a"}, "refusal": None},
        ],
        "tools": [{"type": "function", "function": {"name": "f", "x-extra": 1}}],
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "s", "schema": {"properties": {"q": {}}}},
            "strict": True,
        },
        "thinking": {"type": "enabled", "budget": 1},
        "max_completion_tokens": 5,
        "n": 2,
    }
    paths = sorted(cp.unsupported_fields(body, ChatCompletionRequest, "body"))
    assert paths == [
        "body.max_completion_tokens",
        "body.messages[1].audio",
        "body.messages[1].refusal",
        "body.n",
        "body.response_format.strict",
    ]


def test_an_alias_is_the_declared_name():
    # ResponseFormat declares ``schema`` through an alias.
    body = {"model": "m", "messages": [], "response_format": {"type": "x", "schema": {}}}
    assert list(cp.unsupported_fields(body, ChatCompletionRequest, "body")) == []
    body["response_format"] = {"schema_": {}}
    assert list(cp.unsupported_fields(body, ChatCompletionRequest, "body")) == [
        "body.response_format.schema_"
    ]


def test_an_embeddings_body_takes_no_chat_fields():
    body = {"model": "m", "input": "x", "messages": [], "user": "u"}
    assert sorted(cp.unsupported_fields(body, EmbeddingRequest, "body")) == [
        "body.messages",
        "body.user",
    ]


@pytest.mark.parametrize(
    ("body", "shape"),
    [
        ({"messages": []}, "chat"),
        ({"input": "x"}, "embeddings"),
        ({"messages": [], "input": "x"}, None),
        ({"model": "m"}, None),
    ],
)
def test_a_body_shape_names_a_type_only_when_unambiguous(body, shape):
    assert cp.body_shape(body) == shape


def test_the_console_creates_chat_and_embeddings_only():
    assert set(cp.CONSOLE_TARGETS) == {"/v1/chat/completions", "/v1/embeddings"}


def test_a_long_request_is_cut_for_the_preview_and_says_so():
    request = {
        "custom_id": "a",
        "method": "POST",
        "url": "/v1/chat/completions",
        "body": {"model": "m", "messages": [{"role": "user", "content": "x" * 50_000}]},
    }
    entry = cp.preview_entry(4, request, ["url"])
    assert entry["truncated"] is True
    assert len(str(entry["request"])) < cp.PREVIEW_BYTES + 1024
    short = cp.preview_entry(1, {**request, "body": {"model": "m", "messages": []}}, [])
    assert short["truncated"] is False


def test_scans_are_limited_per_process_and_per_user():
    with cp.scan_slot("a"):
        with pytest.raises(cp.ScanBusy), cp.scan_slot("a"):
            pass
        with cp.scan_slot("b"), pytest.raises(cp.ScanBusy), cp.scan_slot("c"):
            pass
    with cp.scan_slot("a"):
        pass  # released


def test_an_integer_past_pythons_conversion_limit_is_a_line_problem():
    raw = b'{"custom_id": "a", "body": {"max_tokens": ' + b"9" * 5000 + b"}}"
    code, message = _problem(raw)
    assert code == "invalid_json_line"
    assert "9999" not in message
    # The limit itself stays on for everything else.
    import sys

    assert sys.get_int_max_str_digits() > 0
