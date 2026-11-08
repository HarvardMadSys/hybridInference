"""A refused request's api_logs row keeps a bounded summary (serving/storage/bounded_log.py)."""

from __future__ import annotations

import copy
import re
import time
from typing import Any

import pytest

from serving.storage.bounded_log import (
    LOGGED_CONTENT_MAX_CHARS,
    LOGGED_ERROR_MAX_CHARS,
    LOGGED_FIELDS_MAX_CHARS,
    LOGGED_MODEL_ID_MAX_CHARS,
    REJECTED_LOG_MAX_CHARS,
    bound_log_payload,
    bound_payload_leftovers,
    bound_rejected_log_payload,
    bounded_json,
    elide_middle,
    is_client_rejection,
    json_size_within,
)

#: The failed-request alerter's exclusion for not-found rows
#: (``error NOT ILIKE 'Model ''%'' not found'``), as a regex.
_NOT_FOUND = re.compile(r"Model '.*' not found", re.DOTALL | re.IGNORECASE)


def _messages(count: int, size: int) -> list[dict[str, Any]]:
    return [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"{i}:" + "x" * size}
        for i in range(count)
    ]


def test_a_small_value_is_copied_whole() -> None:
    body = {"model": "m", "messages": _messages(3, 10), "stream": True, "temperature": 0.5}
    copied, cut = bounded_json(body)
    assert copied == body
    assert copied is not body
    assert cut is False


def test_long_text_keeps_its_head() -> None:
    copied, cut = bounded_json("a" * 100 + "b" * 100, 50)
    assert cut is True
    assert copied.startswith("a" * 40)
    assert copied.endswith("…")
    assert len(copied) <= 50


def test_a_long_conversation_keeps_its_first_and_last_turns() -> None:
    messages = _messages(1000, 200)
    copied, cut = bounded_json(messages, 4096)
    assert cut is True
    assert copied[0] == messages[0]
    assert copied[-1] == messages[-1]
    assert len(copied) < 100


def test_small_fields_survive_a_large_leading_field() -> None:
    body = {"system": "s" * 100_000, "messages": _messages(50, 1000), "model": "m", "stream": True}
    copied, cut = bounded_json(body, 2048)
    assert cut is True
    assert copied["model"] == "m"
    assert copied["stream"] is True


def test_skipped_keys_are_left_out_without_counting_as_a_cut() -> None:
    copied, cut = bounded_json({"model": "m", "messages": _messages(2, 5)}, skip_keys=("messages",))
    assert copied == {"model": "m"}
    assert cut is False


def test_nesting_is_followed_only_so_deep() -> None:
    deep: Any = "leaf"
    for _ in range(200):
        deep = [deep]
    copied, cut = bounded_json(deep)
    assert cut is True
    depth = 0
    while isinstance(copied, list):
        copied = copied[0]
        depth += 1
    assert 16 <= depth <= 40
    assert copied == "…"


def test_values_that_are_not_json_are_never_stringified() -> None:
    class Huge:
        def __str__(self) -> str:  # pragma: no cover - must not be called
            raise AssertionError("stringified")

    copied, cut = bounded_json({"blob": b"\x00" * 10, "obj": Huge()})
    assert copied == {"blob": "<bytes>", "obj": "<Huge>"}
    assert cut is True


@pytest.mark.parametrize(
    "value",
    [
        [{}] * 2_000_000,
        ["a"] * 2_000_000,
        {f"k{i}": i for i in range(500_000)},
        [[[["x" * 10] * 20] * 20] * 20] * 50,
    ],
    ids=["many-dicts", "many-strings", "wide-dict", "nested"],
)
def test_the_work_is_bounded_by_the_budget_not_the_input(value: Any) -> None:
    started = time.perf_counter()
    copied, cut = bounded_json(value)
    elapsed = time.perf_counter() - started
    assert cut is True
    # A full walk of these inputs takes seconds; the summary stops at its budget.
    assert elapsed < 0.5
    assert len(repr(copied)) < 4 * REJECTED_LOG_MAX_CHARS


def test_elision_keeps_both_ends() -> None:
    error = "Model '" + "z" * 100_000 + "' not found"
    elided = elide_middle(error, LOGGED_ERROR_MAX_CHARS)
    assert len(elided) == LOGGED_ERROR_MAX_CHARS
    assert elided.startswith("Model 'zzz")
    assert elided.endswith("zzz' not found")
    assert _NOT_FOUND.fullmatch(elided)
    assert elide_middle("short", 10) == "short"


@pytest.mark.parametrize(
    ("status", "bounded"),
    [(200, False), (400, True), (404, True), (413, True), (429, True), (499, False), (502, False)],
)
def test_which_statuses_are_summarized(status: int, bounded: bool) -> None:
    assert is_client_rejection(status) is bounded


def _row(status: int) -> dict[str, Any]:
    model = "m" * 50_000
    messages = _messages(400, 2000)
    return {
        "request_id": "r1",
        "model_id": model,
        "provider": "router",
        "prompt": messages,
        "response": None,
        "usage": None,
        "latency_ms": 1,
        "status_code": status,
        "error": f"Model '{model}' not found",
        "params": {"stream": False, "tools": [{"name": "t", "description": "d" * 200_000}]},
        "metadata": {"user_id": "u1"},
        "pricing": None,
        "request_payload": {"model": model, "messages": messages, "max_tokens": 10},
    }


def test_a_refused_row_is_stored_as_a_bounded_summary() -> None:
    row = _row(404)
    original = copy.deepcopy(row)

    stored = bound_rejected_log_payload(row)

    assert stored["prompt"][0] == row["prompt"][0]
    assert stored["prompt"][-1]["content"].startswith(row["prompt"][-1]["content"][:100])
    assert len(repr(stored["prompt"])) < 2 * REJECTED_LOG_MAX_CHARS
    # The prompt column holds the messages, so the body leaves them out.
    assert "messages" not in stored["request_payload"]
    assert stored["request_payload"]["max_tokens"] == 10
    assert len(repr(stored["params"]["tools"])) < 2 * REJECTED_LOG_MAX_CHARS
    assert stored["params"]["stream"] is False
    assert len(stored["model_id"]) == LOGGED_MODEL_ID_MAX_CHARS
    assert len(stored["error"]) == LOGGED_ERROR_MAX_CHARS
    # Still excluded from failed-request alerts as a not-found row.
    assert _NOT_FOUND.fullmatch(stored["error"])
    assert stored["metadata"] == {"user_id": "u1", "payload_truncated": True}
    # The caller's dicts are untouched.
    assert row == original
    assert row["metadata"] == {"user_id": "u1"}


def test_a_small_refused_row_is_stored_as_it_was_sent() -> None:
    row = {
        "request_id": "r1",
        "model_id": "nope",
        "prompt": [{"role": "user", "content": "hi"}],
        "status_code": 404,
        "error": "Model 'nope' not found",
        "params": {"stream": False},
        "metadata": {"user_id": "u1"},
        "request_payload": {"model": "nope", "messages": [{"role": "user", "content": "hi"}]},
    }
    stored = bound_rejected_log_payload(row)
    assert stored["prompt"] == row["prompt"]
    assert stored["request_payload"] == {"model": "nope"}
    assert stored["metadata"] == {"user_id": "u1"}


@pytest.mark.parametrize("status", [200, 499, 500])
def test_rows_that_are_not_refusals_are_stored_whole(status: int) -> None:
    row = _row(status)
    assert bound_rejected_log_payload(row) is row


# --- every other row: the conversation kept, the rest bounded -----------------
#
# A served request is billed for its conversation, which is what the stored
# prompts are read for. Any other field it sent is not: the gateway ignores a
# field no request schema declares, so a cheap request could carry an ignored
# 32 MiB field into its row.


def _served(status: int = 200, **payload: Any) -> dict[str, Any]:
    messages = [{"role": "user", "content": "hi"}]
    return {
        "request_id": "r1",
        "model_id": "m",
        "provider": "p",
        "prompt": messages,
        "response": {"id": "x"},
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        "latency_ms": 1,
        "status_code": status,
        "params": {"stream": False},
        "metadata": {"user_id": "u1"},
        "pricing": None,
        "request_payload": {"model": "m", "messages": messages, "max_tokens": 1, **payload},
    }


@pytest.mark.parametrize("status", [200, 499, 500, 504])
def test_an_ignored_large_field_is_not_stored(status: int) -> None:
    row = _served(status, pad="p" * (32 * 1024 * 1024), user="alice", n=1)
    original_payload = dict(row["request_payload"])

    stored = bound_log_payload(row)

    body = stored["request_payload"]
    assert "pad" not in body
    # Every other field is kept whole, in the order it was sent.
    assert list(body) == ["model", "messages", "max_tokens", "user", "n"]
    assert body["messages"] is row["request_payload"]["messages"]
    assert stored["prompt"] is row["prompt"]
    assert stored["metadata"] == {"user_id": "u1", "payload_truncated": True}
    assert row["request_payload"] == original_payload
    assert row["metadata"] == {"user_id": "u1"}


def test_fields_outside_the_conversation_share_one_budget_smallest_first() -> None:
    third = LOGGED_FIELDS_MAX_CHARS // 3
    row = _served(
        a="a" * third,
        b="b" * third,
        big="z" * LOGGED_FIELDS_MAX_CHARS,
        c="c" * third,
        tiny=True,
    )
    body = bound_log_payload(row)["request_payload"]
    # The small fields and the first two thirds fit; the third does not, and
    # a field is kept whole or not at all.
    assert set(body) == {"model", "messages", "max_tokens", "a", "b", "tiny"}
    assert body["a"] == row["request_payload"]["a"]


def test_a_real_conversation_is_stored_whole() -> None:
    # About 3.5 MiB of text, tools and a system prompt: under the bound.
    messages = _messages(700, 5000)
    tools = [{"name": f"t{i}", "description": "d" * 2000} for i in range(50)]
    row = _served(system="s" * 20_000, tools=tools)
    row["prompt"] = messages
    row["request_payload"]["messages"] = messages
    row["params"] = {"tools": tools}
    assert bound_log_payload(row) is row


def test_a_conversation_over_its_bound_is_summarized() -> None:
    messages = _messages(3, LOGGED_CONTENT_MAX_CHARS // 2)
    row = _served()
    row["prompt"] = messages
    row["request_payload"]["messages"] = messages

    stored = bound_log_payload(row)

    assert len(repr(stored["prompt"])) < 1.1 * LOGGED_CONTENT_MAX_CHARS
    assert stored["prompt"][0]["content"].startswith("0:")
    assert stored["prompt"][-1]["content"].startswith("2:")
    assert len(repr(stored["request_payload"])) < 1.1 * LOGGED_CONTENT_MAX_CHARS
    assert stored["request_payload"]["max_tokens"] == 1
    assert stored["metadata"]["payload_truncated"] is True


def test_a_large_ignored_key_is_dropped_too() -> None:
    row = _served()
    row["request_payload"]["k" * 100_000] = 1
    assert list(bound_log_payload(row)["request_payload"]) == ["model", "messages", "max_tokens"]


def test_a_body_with_countless_fields_keeps_the_first_ones() -> None:
    row = _served(**{f"f{i}": i for i in range(100_000)})
    started = time.perf_counter()
    body = bound_log_payload(row)["request_payload"]
    assert time.perf_counter() - started < 0.5
    assert body["model"] == "m"
    assert 0 < len(body) < 1000


@pytest.mark.parametrize(
    "value",
    [
        ["x"] * 5_000_000,
        {f"k{i}": i for i in range(1_000_000)},
        "s" * 50_000_000,
    ],
    ids=["long-list", "wide-dict", "long-text"],
)
def test_measuring_a_value_stops_at_the_budget(value: Any) -> None:
    started = time.perf_counter()
    assert json_size_within(value, LOGGED_FIELDS_MAX_CHARS) is None
    assert time.perf_counter() - started < 0.1


def test_measuring_refuses_nesting_past_what_a_summary_follows() -> None:
    deep: Any = "leaf"
    for _ in range(5000):
        deep = [deep]
    assert json_size_within(deep, LOGGED_CONTENT_MAX_CHARS) is None
    assert json_size_within([[["x"]]], 100) == 2 + 1 + 2 + 1 + 2 + 1 + 3


def test_the_old_name_is_the_same_function() -> None:
    assert bound_rejected_log_payload is bound_log_payload


# --- what a stored body repeats of the prompt column -------------------------


def test_ignored_per_message_fields_are_bounded_once_deduplicated() -> None:
    residuals = [{"pad": "q" * 1_000_000}, {}]
    body, cut = bound_payload_leftovers(
        {"model": "m", "messages_extra": residuals}, prompt_stored=True
    )
    assert cut is True
    assert len(repr(body["messages_extra"])) < 1.1 * LOGGED_FIELDS_MAX_CHARS
    assert body["model"] == "m"


def test_messages_the_dedup_left_are_bounded_only_when_the_prompt_holds_them() -> None:
    messages = _messages(10, 20_000)
    bounded, cut = bound_payload_leftovers({"messages": messages}, prompt_stored=True)
    assert cut is True
    assert len(repr(bounded["messages"])) < 1.1 * LOGGED_FIELDS_MAX_CHARS

    kept, kept_cut = bound_payload_leftovers({"messages": messages}, prompt_stored=False)
    assert kept_cut is False
    assert kept["messages"] is messages


def test_small_leftovers_are_left_alone() -> None:
    body = {"model": "m", "messages_extra": [{"cache_control": {"type": "ephemeral"}}, {}]}
    assert bound_payload_leftovers(body, prompt_stored=True) == (body, False)
