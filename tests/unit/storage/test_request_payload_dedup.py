"""Tests for dropping already-columned content from ``api_logs.request_payload``.

``messages`` and ``tools`` are written to the dedicated ``prompt`` and ``tools``
columns of the same row, so storing them again inside the raw body was pure
duplication — the reason ``request_payload`` grew to roughly half of a 517 GB
table.

The contract these tests pin is that the dedup is *verified*, never assumed: a
key is dropped only when this row demonstrably stores the same value elsewhere.
Two real cases would otherwise lose data outright:

* the ``prompt`` column on the OpenAI-compat surface is a pydantic
  re-serialization (``ChatMessage`` is ``extra="ignore"``), so per-message
  extras a client sent — ``cache_control``, ``prefix`` — exist only in the body;
* early-error rows log ``early_params``, which carries no ``tools``, so the
  ``tools`` column is NULL and the body holds the only copy.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from serving.storage.database import DatabaseLogger
from serving.storage.payload_dedup import MESSAGE_RESIDUAL_KEY, strip_duplicated_payload_keys
from tests.unit.storage.test_served_model_logging import _log, _store_with_capture

# conn.execute is called as (sql, $1, $2, ...), so placeholder $N maps to args[N].
PROMPT_ARG = 18
REQUEST_PAYLOAD_ARG = 20
METADATA_ARG = 25
TOOLS_ARG = 26

SAMPLE_MESSAGES = [{"role": "user", "content": "hi"}]
SAMPLE_TOOLS = [{"name": "Bash"}]


def _db_logger_with_capture() -> tuple[DatabaseLogger, MagicMock]:
    """Return a DatabaseLogger wired to a mock pool, plus the captured conn."""
    conn = MagicMock()
    conn.execute = AsyncMock(return_value="INSERT 0 1")
    acquire_cm = MagicMock()
    acquire_cm.__aenter__ = AsyncMock(return_value=conn)
    acquire_cm.__aexit__ = AsyncMock(return_value=None)
    pool = MagicMock()
    pool.acquire.return_value = acquire_cm
    db = DatabaseLogger({}, store_full_prompts=True)
    db.pool = pool
    return db, conn


def _sample_body() -> dict:
    return {
        "model": "glm-5.1",
        "messages": [dict(m) for m in SAMPLE_MESSAGES],
        "tools": [dict(t) for t in SAMPLE_TOOLS],
        "system": "You are Claude Code, an agent.",
        "stream": True,
        "temperature": 0.4,
        "max_tokens": 256,
        "tool_choice": "auto",
        "response_format": {"type": "json_object"},
        "thinking": {"type": "disabled"},
    }


def _stripped(payload, **kwargs):
    """Strip against the sample columns unless a test overrides them."""
    kwargs.setdefault("stored_messages", SAMPLE_MESSAGES)
    kwargs.setdefault("stored_tools", SAMPLE_TOOLS)
    return strip_duplicated_payload_keys(payload, **kwargs)


# -- helper: what gets dropped ------------------------------------------------


def test_drops_messages_and_tools_the_columns_already_hold() -> None:
    stripped = _stripped(_sample_body())
    assert "messages" not in stripped
    assert "tools" not in stripped
    assert MESSAGE_RESIDUAL_KEY not in stripped
    assert stripped == {
        "model": "glm-5.1",
        "system": "You are Claude Code, an agent.",
        "stream": True,
        "temperature": 0.4,
        "max_tokens": 256,
        "tool_choice": "auto",
        "response_format": {"type": "json_object"},
        "thinking": {"type": "disabled"},
    }


def test_does_not_mutate_the_caller_dict() -> None:
    """Callers keep reading the body after logging — it must survive intact."""
    body = _sample_body()
    _stripped(body)
    assert body["messages"] == SAMPLE_MESSAGES
    assert body["tools"] == SAMPLE_TOOLS


def test_passes_through_shapes_with_nothing_to_strip() -> None:
    assert _stripped(None) is None
    payload = {"model": "glm-5.1", "input": "embed me"}
    assert _stripped(payload) is payload
    assert _stripped("not json at all") == "not json at all"
    assert _stripped("[1, 2, 3]") == "[1, 2, 3]"
    assert _stripped(123) == 123


def test_strips_inside_a_json_string_and_keeps_it_a_string() -> None:
    stripped = _stripped(json.dumps(_sample_body()))
    assert isinstance(stripped, str)
    decoded = json.loads(stripped)
    assert "messages" not in decoded
    assert "tools" not in decoded
    assert decoded["system"] == "You are Claude Code, an agent."


# -- helper: what is deliberately KEPT ----------------------------------------


def test_keeps_tools_when_the_tools_column_is_empty() -> None:
    """Early-error rows log ``early_params``, which carries no tools.

    The tools column is NULL there, so the body is the only copy — dropping it
    would silently destroy the tool definitions on every 404/400 row.
    """
    stripped = _stripped(_sample_body(), stored_tools=None)
    assert stripped["tools"] == SAMPLE_TOOLS
    assert "messages" not in stripped


def test_keeps_tools_when_the_column_holds_something_different() -> None:
    stripped = _stripped(_sample_body(), stored_tools=[{"name": "Read"}])
    assert stripped["tools"] == SAMPLE_TOOLS


def test_preserves_per_message_fields_the_prompt_column_dropped() -> None:
    """``ChatMessage`` is ``extra="ignore"``, so cache_control never reaches prompt."""
    body = _sample_body()
    body["messages"] = [
        {"role": "user", "content": "hi", "cache_control": {"type": "ephemeral"}},
        {"role": "assistant", "content": "partial", "prefix": True},
    ]
    # What the prompt column actually stores: pydantic-normalized, extras gone.
    stored = [
        {"role": "user", "content": "hi", "tool_call_id": None},
        {"role": "assistant", "content": "partial", "tool_call_id": None},
    ]

    stripped = _stripped(body, stored_messages=stored)

    # The bulk (content) is gone, but nothing the column dropped is lost.
    assert "messages" not in stripped
    assert stripped[MESSAGE_RESIDUAL_KEY] == [
        {"cache_control": {"type": "ephemeral"}},
        {"prefix": True},
    ]


def test_keeps_messages_when_they_cannot_be_aligned() -> None:
    """Different length or non-dict entries: cannot verify, so keep everything."""
    body = _sample_body()
    assert _stripped(body, stored_messages=[])["messages"] == SAMPLE_MESSAGES
    assert _stripped(body, stored_messages=None)["messages"] == SAMPLE_MESSAGES
    assert _stripped(body, stored_messages="not a list")["messages"] == SAMPLE_MESSAGES


def test_keeps_messages_whose_content_the_column_did_not_store() -> None:
    """A truncated/redacted prompt column must not license dropping the body."""
    body = _sample_body()
    stored = [{"role": "user", "content": "[redacted]"}]
    stripped = _stripped(body, stored_messages=stored)
    assert stripped[MESSAGE_RESIDUAL_KEY] == [{"content": "hi"}]


# -- PostgresLogStore write path ---------------------------------------------


@pytest.mark.asyncio
async def test_postgres_store_strips_duplicated_keys_but_keeps_the_rest() -> None:
    store, conn = _store_with_capture()
    body = _sample_body()
    await _log(store, params={"tools": body["tools"]}, request_payload=body)

    args = conn.execute.await_args.args
    stored = json.loads(args[REQUEST_PAYLOAD_ARG])
    assert "messages" not in stored
    assert "tools" not in stored
    # Everything unique to the body survives, ``system`` above all: it is the
    # only stored copy of the Anthropic-surface system prompt.
    assert stored["model"] == "glm-5.1"
    assert stored["system"] == "You are Claude Code, an agent."
    assert stored["stream"] is True
    assert stored["temperature"] == 0.4
    assert stored["response_format"] == {"type": "json_object"}

    # The stripped content is not lost — it lives in its own columns.
    assert json.loads(args[PROMPT_ARG])[0]["content"] == "hi"
    assert json.loads(args[TOOLS_ARG])[0]["name"] == "Bash"


@pytest.mark.asyncio
async def test_postgres_store_keeps_tools_on_an_early_error_row() -> None:
    """Reproduces the 404 path: params=early_params, which has no tools."""
    store, conn = _store_with_capture()
    body = _sample_body()
    await _log(
        store,
        status_code=404,
        params={"stream": True, "session_id": "s1"},
        request_payload=body,
    )

    args = conn.execute.await_args.args
    assert args[TOOLS_ARG] is None  # the column really is empty here
    stored = json.loads(args[REQUEST_PAYLOAD_ARG])
    assert stored["tools"] == SAMPLE_TOOLS  # ...so the body keeps the only copy


@pytest.mark.asyncio
async def test_postgres_store_still_derives_agent_from_the_kept_system_field() -> None:
    """``metadata.agent`` reads request_payload["system"], so it must survive."""
    store, conn = _store_with_capture()
    await _log(store, request_payload=_sample_body())
    metadata = json.loads(conn.execute.await_args.args[METADATA_ARG])
    assert metadata["agent"] == "Claude"


@pytest.mark.asyncio
async def test_postgres_store_does_not_mutate_the_caller_payload() -> None:
    store, _conn = _store_with_capture()
    body = _sample_body()
    await _log(store, params={"tools": body["tools"]}, request_payload=body)
    assert body == _sample_body()


@pytest.mark.asyncio
async def test_postgres_store_handles_json_string_and_none_payloads() -> None:
    store, conn = _store_with_capture()
    body = _sample_body()
    await _log(store, params={"tools": body["tools"]}, request_payload=json.dumps(body))
    # A string payload is serialized as a JSON string (unchanged behaviour);
    # only its contents are deduplicated.
    inner = json.loads(conn.execute.await_args.args[REQUEST_PAYLOAD_ARG])
    assert isinstance(inner, str)
    decoded = json.loads(inner)
    assert "messages" not in decoded
    assert "tools" not in decoded
    assert decoded["system"] == "You are Claude Code, an agent."

    store2, conn2 = _store_with_capture()
    await _log(store2, request_payload=None)
    assert conn2.execute.await_args.args[REQUEST_PAYLOAD_ARG] is None


@pytest.mark.asyncio
async def test_postgres_store_privacy_mode_still_stores_no_payload() -> None:
    store, conn = _store_with_capture()
    await _log(store, store_full_content=False, request_payload=_sample_body())
    assert conn.execute.await_args.args[REQUEST_PAYLOAD_ARG] is None


# -- DatabaseLogger write path -----------------------------------------------


@pytest.mark.asyncio
async def test_database_logger_strips_duplicated_keys_but_keeps_the_rest() -> None:
    db, conn = _db_logger_with_capture()
    body = _sample_body()
    await db.log_request(
        request_id="req-1",
        model_id="glm-5.1",
        provider="zhipu",
        prompt=body["messages"],
        response={"message": {"content": "ok"}},
        usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        latency_ms=10,
        status_code=200,
        params={"tools": body["tools"]},
        request_payload=body,
    )

    args = conn.execute.await_args.args
    stored = json.loads(args[REQUEST_PAYLOAD_ARG])
    assert "messages" not in stored
    assert "tools" not in stored
    assert stored["model"] == "glm-5.1"
    assert stored["system"] == "You are Claude Code, an agent."
    assert json.loads(args[PROMPT_ARG])[0]["content"] == "hi"
    assert json.loads(args[TOOLS_ARG])[0]["name"] == "Bash"
    # The caller's body is untouched.
    assert body == _sample_body()


@pytest.mark.asyncio
async def test_database_logger_keeps_tools_on_an_early_error_row() -> None:
    db, conn = _db_logger_with_capture()
    body = _sample_body()
    await db.log_request(
        request_id="req-1",
        model_id="glm-5.1",
        provider="zhipu",
        prompt=body["messages"],
        response=None,
        usage=None,
        latency_ms=3,
        status_code=404,
        params={"stream": False},
        request_payload=body,
    )

    args = conn.execute.await_args.args
    assert args[TOOLS_ARG] is None
    assert json.loads(args[REQUEST_PAYLOAD_ARG])["tools"] == SAMPLE_TOOLS


@pytest.mark.asyncio
async def test_database_logger_handles_json_string_and_none_payloads() -> None:
    db, conn = _db_logger_with_capture()
    base = {
        "request_id": "req-1",
        "model_id": "glm-5.1",
        "provider": "zhipu",
        "prompt": [{"role": "user", "content": "hi"}],
        "response": {"message": {"content": "ok"}},
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        "latency_ms": 10,
        "status_code": 200,
    }
    await db.log_request(**base, request_payload=json.dumps(_sample_body()))
    inner = json.loads(conn.execute.await_args.args[REQUEST_PAYLOAD_ARG])
    assert isinstance(inner, str)
    assert "messages" not in json.loads(inner)

    db2, conn2 = _db_logger_with_capture()
    await db2.log_request(**base, request_payload=None)
    assert conn2.execute.await_args.args[REQUEST_PAYLOAD_ARG] is None
