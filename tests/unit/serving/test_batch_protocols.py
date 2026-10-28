"""Protocol pieces of batch inference, without a database or a network.

Result lines are byte-stable (a virtual file renders the same bytes every
time) and share one shape for every target, every batch shows the same status
words, Anthropic version profiles refuse what their version refuses, target
admission refuses what a target would silently drop, webhook targets are
checked against forgery and signed as Standard Webhooks, and the multipart
reader handles uploads cut anywhere.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import json
import socket
from types import SimpleNamespace
from typing import Any

import pytest

from routing.routers import FixedRouter
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


def test_result_lines_are_exact_and_stable():
    done = _item("completed", response={"id": "x", "b": 2, "a": "é"})
    line = renderers.result_line(done)
    assert (
        line
        == (
            '{"id":"batch_req_42","custom_id":"c-1","response":{"status_code":200,'
            '"request_id":"req-9","body":{"id":"x","b":2,"a":"é"}},"error":null}\n'
        ).encode()
    )
    assert renderers.result_line(done) == line

    http_fail = _item(
        "failed",
        status_code=400,
        error={"code": "invalid_request", "message": "bad", "status": 400},
    )
    body = json.loads(renderers.result_line(http_fail))
    assert body["response"]["status_code"] == 400
    assert body["response"]["body"]["error"] == {
        "message": "bad",
        "type": "invalid_request_error",
        "param": None,
        "code": "invalid_request",
    }
    assert body["error"] is None

    expired = json.loads(renderers.result_line(_item("expired")))
    assert expired["response"] is None
    assert expired["error"]["code"] == "batch_expired"
    cancelled = json.loads(renderers.result_line(_item("cancelled")))
    assert cancelled["error"]["code"] == "batch_cancelled"


def test_a_messages_result_is_the_same_line_its_body_the_messages_response():
    """Every target shares one line shape; a Messages error is an OpenAI error body."""
    message = {"id": "msg_1", "type": "message", "role": "assistant", "content": []}
    done = json.loads(renderers.result_line(_item("completed", response=message)))
    assert done == {
        "id": "batch_req_42",
        "custom_id": "c-1",
        "response": {"status_code": 200, "request_id": "req-9", "body": message},
        "error": None,
    }
    failed = _item(
        "failed",
        status_code=429,
        error={
            "code": "rate_limited",
            "message": "slow",
            "status": 429,
            "anthropic": {"type": "rate_limit_error", "message": "slow"},
        },
    )
    line = json.loads(renderers.result_line(failed))
    assert line["response"]["body"]["error"]["type"] == "rate_limit_error"
    assert line["response"]["body"]["error"]["code"] == "rate_limited"
    assert not hasattr(renderers, "anthropic_line")


def test_an_unknown_renderer_version_is_refused():
    with pytest.raises(ValueError):
        renderers.result_line(_item("completed"), version=99)


def test_display_status_is_projected_from_phase():
    file_row = {"api_surface": "openai_file"}
    assert (
        renderers.display_status({**file_row, "status": "validating", "phase": "validating"})
        == "validating"
    )
    assert (
        renderers.display_status(
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
        renderers.display_status(
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
        renderers.display_status(
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
        renderers.display_status({**file_row, "status": "expired", "phase": "runnable"})
        == "in_progress"
    )
    assert (
        renderers.display_status({**file_row, "status": "completed", "phase": "sealed"})
        == "completed"
    )
    # An inline batch keeps its status as it always did.
    assert (
        renderers.display_status(
            {"api_surface": "openai_inline", "status": "expired", "phase": "runnable"}
        )
        == "expired"
    )
    # So does one the retired Message Batches API stored: the words are those
    # of /v1/batches, never "ended" or "canceling".
    for status in ("in_progress", "cancelling", "completed", "cancelled", "expired", "failed"):
        legacy = {"api_surface": "anthropic", "status": status, "phase": "sealed"}
        assert renderers.display_status(legacy) == status
    # A Messages file batch is projected like any other file batch.
    assert (
        renderers.display_status(
            {
                "api_surface": "openai_file",
                "target": "messages",
                "status": "in_progress",
                "phase": "sealing",
                "pending_terminal_status": "completed",
            }
        )
        == "finalizing"
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
        # The version is the batch's; a request cannot name its own, or betas.
        {"anthropic_version": "2023-06-01"},
        {"betas": ["x"]},
    ],
)
def test_messages_batch_requests_refuse_what_is_not_accepted_yet(body):
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


@pytest.mark.parametrize(
    "url",
    [
        "https://[broken/h",
        "https://example.com:bad/hook",
        "https://example.com:99999/hook",
        # A 64-character label is not valid IDNA.
        "https://" + "a" * 64 + ".example.com/hook",
        "https://a..example.com/hook",
    ],
)
async def test_a_malformed_webhook_url_is_refused_before_any_lookup(monkeypatch, url):
    loop = asyncio.get_running_loop()

    async def no_lookup(*args, **kwargs):
        raise AssertionError("a malformed URL must not be resolved")

    monkeypatch.setattr(loop, "getaddrinfo", no_lookup)
    with pytest.raises(webhooks.WebhookTargetRefused):
        webhooks.check_url(url, allow_private=False)
    normalized, problem = webhooks.url_problem(url, allow_private=False)
    assert normalized is None and problem == "The webhook URL is malformed."
    with pytest.raises(webhooks.WebhookTargetRefused):
        await webhooks.check_resolves_public(url, allow_private=False)


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


# -- malformed bodies are refused, never raised -----------------------------------------

_VALID_BODIES: dict[str, dict[str, Any]] = {
    "chat": {
        "model": "m",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 5,
        "tools": [{"type": "function", "function": {"name": "f", "parameters": {}}}],
    },
    "embeddings": {"model": "e", "input": ["a", "b"]},
    "messages": {
        "model": "m",
        "max_tokens": 5,
        "system": [{"type": "text", "text": "s"}],
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "hi"},
                    {
                        "type": "tool_result",
                        "tool_use_id": "t",
                        "content": [{"type": "text", "text": "r"}],
                    },
                ],
            }
        ],
        "tools": [{"name": "f", "input_schema": {"type": "object"}}],
        "tool_choice": {"type": "auto"},
        "metadata": {"user_id": "u"},
        "output_config": {"effort": "low"},
    },
    "responses": {
        "model": "m",
        "instructions": "i",
        "input": [
            {"role": "user", "content": [{"type": "input_text", "text": "hi"}]},
            {"type": "function_call_output", "call_id": "c", "output": "x"},
        ],
        "tools": [{"type": "function", "name": "f"}],
        "text": {"format": {"type": "json_object"}},
        "reasoning": {"effort": "low"},
    },
}
_MALFORMED = [[], {}, 1, 1.5, True, None, "x", [[]], [{}], {"type": []}, {"type": {}}]


def _paths(value: Any, path: tuple = ()) -> Any:
    yield path
    if isinstance(value, dict):
        for key, child in value.items():
            yield from _paths(child, (*path, key))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _paths(child, (*path, index))


def _replaced(value: Any, path: tuple, replacement: Any) -> Any:
    if not path:
        return replacement
    copied = json.loads(json.dumps(value))
    node = copied
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = json.loads(json.dumps(replacement))
    return copied


def _admission_context(profile: Any) -> targets.AdmissionContext:
    embedder = SimpleNamespace(config=SimpleNamespace(id="e"))
    # Registered as the registry registers an embedding model: adapter and gate.
    router = FixedRouter()
    router.register_embedding_route("e", [(embedder, 1.0)], required_role="free")
    return targets.AdmissionContext(
        user_ctx={},
        routes={},
        services=None,
        visibility_resolver=None,
        embedding_adapters={"e": embedder},
        profile=profile,
        embedding_routes=router.embedding_routes,
    )


@pytest.mark.parametrize("target", sorted(_VALID_BODIES))
async def test_every_malformed_value_anywhere_is_refused_not_raised(monkeypatch, target):
    """Replace each value of a valid body by each wrong JSON type: ItemRejected or success only."""
    openai = _openai_adapter(supported_params=["reasoning_effort"], reasoning_efforts=["low"])
    route = SimpleNamespace(adapters=[(openai, 1.0), (_NATIVE, 1.0)])

    async def admitted_model(model: Any, ctx: Any, target: str, resolver: Any = None) -> Any:
        if not isinstance(model, str):
            raise targets.ItemRejected("invalid_request", "model: a model name is required")
        return model, route

    monkeypatch.setattr(targets, "_cached_route_model", admitted_model)
    body = _VALID_BODIES[target]
    for profile in profiles.PROFILES.values():
        ctx = _admission_context(profile)
        assert await targets.admit_item(target, json.loads(json.dumps(body)), ctx)
        for path in list(_paths(body)):
            for replacement in _MALFORMED:
                with contextlib.suppress(targets.ItemRejected):
                    await targets.admit_item(target, _replaced(body, path, replacement), ctx)


@pytest.mark.parametrize(
    "content",
    [
        [{"type": []}],
        [{"type": {}}],
        [{"type": 3}],
        [{"type": "text", "text": "a"}, {"type": None}],
    ],
)
def test_a_content_block_whose_type_is_not_a_string_is_a_problem(content):
    body = {"model": "m", "max_tokens": 5, "messages": [{"role": "user", "content": content}]}
    problem = targets.native_messages_problem(body)
    assert problem is not None and "content block type" in problem


@pytest.mark.parametrize(
    "body",
    [
        {"input": [{"type": {}}]},
        {"input": [{"type": ["message"]}]},
        {"input": [{"role": "user", "content": [{"type": []}]}]},
        {"input": "x", "tools": [{"type": []}]},
        {"input": "x", "tools": ["function"]},
        {"input": "x", "tools": {"type": "function"}},
        {"input": "x", "tools": [{"type": "function"}]},
    ],
)
async def test_responses_with_malformed_types_or_tools_are_refused(monkeypatch, body):
    async def admitted_model(*args: Any, **kwargs: Any) -> Any:
        return "m", SimpleNamespace(adapters=[])

    # The model is admitted, so a refusal can only come from the body's shape.
    monkeypatch.setattr(targets, "_cached_route_model", admitted_model)
    with pytest.raises(targets.ItemRejected) as refused:
        await _admit_responses(body)
    assert refused.value.param in ("input", "tools")


async def test_a_messages_request_without_the_batchs_version_is_refused(monkeypatch):
    """Admission never runs a Messages request under no version's rules."""

    async def admitted_model(*args: Any, **kwargs: Any) -> Any:
        return "m", SimpleNamespace(adapters=[(_NATIVE, 1.0)])

    monkeypatch.setattr(targets, "_cached_route_model", admitted_model)
    body = json.loads(json.dumps(_VALID_BODIES["messages"]))
    with pytest.raises(targets.ItemRejected) as refused:
        await targets.admit_item("messages", body, _admission_context(None))
    assert "anthropic_version" in refused.value.message
    assert await targets.admit_item(
        "messages", body, _admission_context(profiles.PROFILES["2023-06-01"])
    )
    # The versions differ where the migration guide says they do.
    with pytest.raises(targets.ItemRejected):
        await targets.admit_item(
            "messages",
            {**body, "temperature": 0.5},
            _admission_context(profiles.PROFILES["2026-10-21"]),
        )


def test_messages_is_a_target_of_v1_batches():
    assert targets.TARGET_URLS == {
        "/v1/chat/completions": "chat",
        "/v1/embeddings": "embeddings",
        "/v1/messages": "messages",
        "/v1/responses": "responses",
    }
