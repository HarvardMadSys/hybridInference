"""Protocol pieces of batch inference, without a database or a network.

Result lines are byte-stable (a virtual file renders the same bytes every
time), Anthropic version profiles refuse what their version refuses, target
admission refuses what a target would silently drop, webhook targets are
checked against forgery and signed as Standard Webhooks, and the multipart
reader handles uploads cut anywhere.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import socket
from types import SimpleNamespace
from typing import Any

import pytest

from serving.batch import multipart, profiles, renderers, targets, webhooks
from serving.batch.config import KNOWN_ANTHROPIC_VERSIONS

# -- renderers ---------------------------------------------------------------------


def _item(status: str, **extra: Any) -> dict[str, Any]:
    return {
        "id": 42,
        "custom_id": "c-1",
        "status": status,
        "status_code": extra.pop("status_code", 200 if status == "completed" else None),
        "response": extra.pop("response", None),
        "error": extra.pop("error", None),
        "request_id": "req-9",
        **extra,
    }


def test_openai_lines_are_exact_and_stable():
    done = _item("completed", response={"id": "x", "b": 2, "a": "é"})
    line = renderers.openai_line(done)
    assert (
        line
        == (
            '{"id":"batch_req_42","custom_id":"c-1","response":{"status_code":200,'
            '"request_id":"req-9","body":{"id":"x","b":2,"a":"é"}},"error":null}\n'
        ).encode()
    )
    assert renderers.openai_line(done) == line

    http_fail = _item(
        "failed",
        status_code=400,
        error={"code": "invalid_request", "message": "bad", "status": 400},
    )
    body = json.loads(renderers.openai_line(http_fail))
    assert body["response"]["status_code"] == 400
    assert body["response"]["body"]["error"] == {
        "message": "bad",
        "type": "invalid_request_error",
        "param": None,
        "code": "invalid_request",
    }
    assert body["error"] is None

    expired = json.loads(renderers.openai_line(_item("expired")))
    assert expired["response"] is None
    assert expired["error"]["code"] == "batch_expired"
    cancelled = json.loads(renderers.openai_line(_item("cancelled")))
    assert cancelled["error"]["code"] == "batch_cancelled"


def test_anthropic_lines_map_every_outcome():
    succeeded = json.loads(
        renderers.anthropic_line(_item("completed", response={"type": "message"}))
    )
    assert succeeded == {
        "custom_id": "c-1",
        "result": {"type": "succeeded", "message": {"type": "message"}},
    }
    native = _item(
        "failed",
        error={
            "code": "rate_limited",
            "message": "slow",
            "anthropic": {"type": "rate_limit_error", "message": "slow"},
        },
    )
    errored = json.loads(renderers.anthropic_line(native))["result"]
    assert errored == {
        "type": "errored",
        "error": {
            "type": "error",
            "error": {"type": "rate_limit_error", "message": "slow"},
            "request_id": "req-9",
        },
    }
    hidden = _item("failed", error={"code": "model_not_found", "message": "Model 'x' not found"})
    assert json.loads(renderers.anthropic_line(hidden))["result"]["error"]["error"]["type"] == (
        "not_found_error"
    )
    assert json.loads(renderers.anthropic_line(_item("cancelled")))["result"] == {
        "type": "canceled"
    }
    assert json.loads(renderers.anthropic_line(_item("expired")))["result"] == {"type": "expired"}


def test_an_unknown_renderer_version_is_refused():
    with pytest.raises(ValueError):
        renderers.openai_line(_item("completed"), version=99)


def test_anthropic_counts_hold_everything_as_processing_until_the_end():
    running = {"phase": "runnable", "request_count": 5, "completed_count": 2, "failed_count": 1}
    assert renderers.anthropic_request_counts(running) == {
        "processing": 5,
        "succeeded": 0,
        "errored": 0,
        "canceled": 0,
        "expired": 0,
    }
    ended = {
        "phase": "sealed",
        "request_count": 6,
        "completed_count": 2,
        "failed_count": 3,
        "expired_count": 1,
        "cancelled_count": 1,
    }
    counts = renderers.anthropic_request_counts(ended)
    assert counts == {"processing": 0, "succeeded": 2, "errored": 2, "canceled": 1, "expired": 1}
    assert sum(counts.values()) == ended["request_count"]


def test_openai_status_is_projected_from_phase():
    file_row = {"api_surface": "openai_file"}
    assert (
        renderers.openai_status({**file_row, "status": "validating", "phase": "validating"})
        == "validating"
    )
    assert (
        renderers.openai_status(
            {
                **file_row,
                "status": "in_progress",
                "phase": "sealing",
                "pending_terminal_status": "completed",
            }
        )
        == "finalizing"
    )
    assert (
        renderers.openai_status(
            {
                **file_row,
                "status": "expired",
                "phase": "sealing",
                "pending_terminal_status": "expired",
            }
        )
        == "finalizing"
    )
    assert (
        renderers.openai_status(
            {
                **file_row,
                "status": "cancelling",
                "phase": "sealing",
                "pending_terminal_status": "cancelled",
            }
        )
        == "cancelling"
    )
    assert (
        renderers.openai_status({**file_row, "status": "expired", "phase": "runnable"})
        == "in_progress"
    )
    assert (
        renderers.openai_status({**file_row, "status": "completed", "phase": "sealed"})
        == "completed"
    )
    # An inline batch keeps its status as it always did.
    assert (
        renderers.openai_status(
            {"api_surface": "openai_inline", "status": "expired", "phase": "runnable"}
        )
        == "expired"
    )


# -- Anthropic versions ------------------------------------------------------------


def test_every_known_version_has_a_profile_and_the_adapter_accepts_it():
    from serving.adapters.anthropic import _KNOWN_BATCH_VERSIONS

    assert set(profiles.PROFILES) == set(KNOWN_ANTHROPIC_VERSIONS) == set(_KNOWN_BATCH_VERSIONS)


@pytest.mark.parametrize(
    "params",
    [
        {"temperature": 1.0},
        {"top_p": 0.9},
        {"top_k": 5},
        {"messages": [{"role": "assistant", "content": [{"type": "document", "source": {}}]}]},
        {
            "messages": [
                {
                    "role": "user",
                    "content": [{"type": "text", "text": "q", "citations": [{"x": 1}]}],
                }
            ]
        },
        {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "search_result",
                            "content": [{"type": "text", "text": "q", "citations": [{"x": 1}]}],
                        }
                    ],
                }
            ]
        },
    ],
)
def test_2026_10_21_refuses_what_its_migration_guide_lists(params):
    body = {"messages": [{"role": "user", "content": "hi"}], **params}
    assert profiles.profile_violation(profiles.PROFILES["2026-10-21"], body)
    assert profiles.profile_violation(profiles.PROFILES["2023-06-01"], body) is None


def test_2026_10_21_still_accepts_citations_where_the_guide_keeps_them():
    body = {
        "messages": [
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "a", "citations": [{"x": 1}]}],
            },
            {
                "role": "user",
                "content": [{"type": "document", "source": {}, "citations": {"enabled": True}}],
            },
        ]
    }
    assert profiles.profile_violation(profiles.PROFILES["2026-10-21"], body) is None


# -- target capabilities -----------------------------------------------------------


def _openai_adapter(**config: Any) -> Any:
    settings = {"supports_tools": True, "supported_params": [], "reasoning_efforts": []}
    settings.update(config)
    return SimpleNamespace(native_format="openai", config=SimpleNamespace(**settings))


_NATIVE = SimpleNamespace(native_format="anthropic", config=SimpleNamespace())


@pytest.mark.parametrize(
    "extra",
    [
        {"thinking": {"type": "enabled", "budget_tokens": 1000}},
        {"top_k": 3},
        {"output_config": {"format": {"type": "json_schema"}}},
        {"system": [{"type": "text", "text": "s", "cache_control": {"type": "ephemeral"}}]},
        {"tool_choice": {"type": "none"}},
        {"tool_choice": {"type": "auto", "disable_parallel_tool_use": True}},
        {
            "messages": [
                {
                    "role": "user",
                    "content": [{"type": "document", "source": {"type": "text", "data": "d"}}],
                }
            ]
        },
    ],
)
def test_openai_routes_refuse_what_they_would_drop_and_native_ones_take_it(extra):
    body = {
        "model": "m",
        "max_tokens": 10,
        "messages": [{"role": "user", "content": "hi"}],
        **extra,
    }
    assert targets.adapter_problem(_openai_adapter(), body) is not None
    assert targets.native_messages_problem(body) is None
    assert targets.adapter_problem(_NATIVE, body) is None


def test_reasoning_effort_is_translated_only_where_the_model_declares_it():
    body = {
        "model": "m",
        "max_tokens": 10,
        "messages": [{"role": "user", "content": "hi"}],
        "output_config": {"effort": "high"},
    }
    plain = _openai_adapter()
    able = _openai_adapter(supported_params=["reasoning_effort"], reasoning_efforts=["low", "high"])
    assert targets.adapter_problem(plain, body) is not None
    assert targets.adapter_problem(able, body) is None


@pytest.mark.parametrize(
    "body",
    [
        {"tools": [{"type": "web_search_20250305", "name": "web_search"}]},
        {"mcp_servers": [{"url": "https://x"}]},
        {"container": "c"},
        {"stream": True},
        {
            "messages": [
                {
                    "role": "user",
                    "content": [{"type": "image", "source": {"type": "file", "file_id": "f"}}],
                }
            ]
        },
        {"metadata": {"user_id": "u", "other": "x"}},
        {"messages": [{"role": "system", "content": "s"}]},
    ],
)
def test_message_batches_refuse_what_is_not_accepted_yet(body):
    full = {"model": "m", "max_tokens": 10, "messages": [{"role": "user", "content": "hi"}], **body}
    assert targets.native_messages_problem(full) is not None


async def _admit_responses(body: dict[str, Any]) -> None:
    ctx = SimpleNamespace(
        _models={}, routes={}, services=None, user_ctx={}, visibility_resolver=None
    )
    await targets._admit_responses({"model": "m", "input": "hi", **body}, ctx)


@pytest.mark.parametrize(
    "body",
    [
        {"store": True},
        {"previous_response_id": "resp_1"},
        {"conversation": "conv_1"},
        {"background": True},
        {"stream": True},
        {"parallel_tool_calls": False},
        {"tools": [{"type": "web_search"}]},
        {"include": ["reasoning.encrypted_content"]},
        {"input": [{"type": "item_reference", "id": "x"}]},
        {"reasoning": {"effort": "low", "summary": "auto"}},
    ],
)
async def test_responses_batches_refuse_stateful_and_untranslatable_requests(body):
    with pytest.raises(targets.ItemRejected):
        await _admit_responses(body)


# -- webhooks ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/hook",
        "https://127.0.0.1/hook",
        "https://10.0.0.5/hook",
        "https://[::1]/hook",
        "https://169.254.169.254/latest",
        "https://metadata.google.internal/x",
        "https://localhost/x",
        "https://user:pw@example.com/x",
        "https://192.168.1.10/x",
        "https://[::ffff:10.0.0.1]/x",
    ],
)
def test_webhook_urls_that_reach_inside_are_refused(url):
    with pytest.raises(webhooks.WebhookTargetRefused):
        webhooks.check_url(url, allow_private=False)


def test_private_targets_can_be_allowed_for_tests_but_never_metadata():
    assert webhooks.check_url("http://127.0.0.1:9000/h", allow_private=True)
    with pytest.raises(webhooks.WebhookTargetRefused):
        webhooks.check_url("http://169.254.169.254/x", allow_private=True)


def test_the_socket_guard_refuses_private_addresses_at_connect_time():
    guard = webhooks.guarded_socket(allow_private=False)
    for address in ("10.1.2.3", "127.0.0.1", "169.254.169.254", "::1"):
        family = socket.AF_INET6 if ":" in address else socket.AF_INET
        with pytest.raises(OSError):
            guard((family, socket.SOCK_STREAM, 6, "", (address, 443)))
    sock = guard((socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)))
    sock.close()


async def test_a_name_resolving_inside_is_refused_at_save_time(monkeypatch):
    loop = asyncio.get_running_loop()

    async def resolve(host, port, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.7", port))]

    monkeypatch.setattr(loop, "getaddrinfo", resolve)
    with pytest.raises(webhooks.WebhookTargetRefused):
        await webhooks.check_resolves_public("https://inside.example.com/h", allow_private=False)


def test_signatures_follow_standard_webhooks():
    secret = webhooks.new_secret()
    body = b'{"id":"evt_1"}'
    signature = webhooks.sign(secret, "evt_1", 1700000000, body)
    key = base64.b64decode(secret.removeprefix("whsec_"))
    expected = base64.b64encode(
        hmac.new(key, b"evt_1.1700000000." + body, hashlib.sha256).digest()
    ).decode()
    assert signature == f"v1,{expected}"


class _Response:
    def __init__(self, status: int, location: str | None = None) -> None:
        self.status = status
        self.headers = {"location": location} if location else {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Session:
    def __init__(self, answers: list[_Response], sent: list[str]) -> None:
        self._answers = answers
        self._sent = sent

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def post(self, url, *, data, headers, allow_redirects):
        assert allow_redirects is False
        self._sent.append(url)
        return self._answers.pop(0)


async def test_a_redirect_into_the_network_is_not_followed():
    sent: list[str] = []
    answers = [_Response(302, "http://10.0.0.1/steal")]
    delivered, status, error = await webhooks.deliver(
        url="https://hooks.example.com/h",
        secret=webhooks.new_secret(),
        event_id="evt_1",
        payload={"id": "evt_1"},
        allow_private=False,
        session_factory=lambda **kwargs: _Session(answers, sent),
    )
    assert (delivered, status) == (False, 302)
    assert "redirect refused" in error
    assert sent == ["https://hooks.example.com/h"]


async def test_a_public_redirect_is_followed_and_signed_again():
    sent: list[str] = []
    answers = [_Response(307, "https://other.example.com/h2"), _Response(204)]
    delivered, status, _error = await webhooks.deliver(
        url="https://hooks.example.com/h",
        secret=webhooks.new_secret(),
        event_id="evt_1",
        payload={"id": "evt_1"},
        allow_private=False,
        session_factory=lambda **kwargs: _Session(answers, sent),
    )
    assert (delivered, status) == (True, 204)
    assert sent == ["https://hooks.example.com/h", "https://other.example.com/h2"]


def test_retries_back_off_and_end():
    assert webhooks.next_attempt_at(1) is not None
    assert webhooks.next_attempt_at(webhooks.MAX_ATTEMPTS) is None


# -- multipart ---------------------------------------------------------------------


def _form(boundary: str, *, purpose: str = "batch", data: bytes = b'{"a":1}\n') -> bytes:
    return (
        (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="purpose"\r\n\r\n'
            f"{purpose}\r\n"
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="file"; filename="in.jsonl"\r\n'
            "Content-Type: application/jsonl\r\n\r\n"
        ).encode()
        + data
        + f"\r\n--{boundary}--\r\n".encode()
    )


async def _read(body: bytes, boundary: str, step: int) -> tuple[dict[str, str], bytes, str]:
    async def stream():
        for start in range(0, len(body), step):
            yield body[start : start + step]

    fields: dict[str, str] = {}
    data = bytearray()
    names: list[str] = []

    async def on_field(name, value):
        fields[name] = value

    async def on_start(name, filename):
        names.append(filename)

    async def on_data(chunk):
        data.extend(chunk)

    async def on_end():
        pass

    await multipart.read_multipart(
        stream(),
        boundary.encode(),
        on_field=on_field,
        on_file_start=on_start,
        on_file_data=on_data,
        on_file_end=on_end,
    )
    return fields, bytes(data), names[0]


@pytest.mark.parametrize("step", [1, 2, 7, 64, 100000])
async def test_the_multipart_reader_reassembles_a_body_cut_anywhere(step):
    payload = b"".join(f'{{"i":{i},"x":"\\r\\n--almost"}}\n'.encode() for i in range(50))
    fields, data, filename = await _read(_form("XyZ", data=payload), "XyZ", step)
    assert fields == {"purpose": "batch"}
    assert data == payload
    assert filename == "in.jsonl"


async def test_a_body_without_its_closing_boundary_is_refused():
    body = _form("b1")[:-8]
    with pytest.raises(multipart.MultipartError):
        await _read(body, "b1", 10)


def test_the_boundary_comes_from_the_content_type():
    assert multipart.boundary_of('multipart/form-data; boundary="abc"') == b"abc"
    with pytest.raises(multipart.MultipartError):
        multipart.boundary_of("application/json")
