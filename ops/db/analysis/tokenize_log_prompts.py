#!/usr/bin/env python3
"""Convert exported api_logs prompt fields into token JSONL rows."""

from __future__ import annotations

import argparse
import binascii
import json
import os
import sys
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, TextIO

if TYPE_CHECKING:
    from collections.abc import Iterator

DEFAULT_TOKENIZER = "zai-org/GLM-5.1"
DEFAULT_WORKERS = min(4, os.cpu_count() or 1)
HASH_SEED = 0x4B1D5EED
QWEN_TRACE_PARENT_HASH_OVERLAP_THRESHOLD = 0.8
_PROCESS_TOKENIZER: ChatTokenizer | None = None
_PROCESS_TOKENIZER_NAME_OR_PATH: str | None = None
_PROCESS_TRUST_REMOTE_CODE = False


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


class ChatTokenizer(Protocol):
    name_or_path: str

    def apply_chat_template(
        self,
        conversation: list[dict[str, Any]],
        *,
        tokenize: bool,
        add_generation_prompt: bool,
        tools: list[dict[str, Any]] | None = None,
    ) -> Any: ...

    def encode(self, text: str, *, add_special_tokens: bool = False) -> list[int]: ...


def parse_prompt(prompt: Any) -> Any:
    """Decode the exported prompt field when it contains JSON text."""
    if not isinstance(prompt, str):
        return prompt
    stripped = prompt.strip()
    if not stripped:
        return ""
    if stripped[0] not in "[{":
        return stripped
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return stripped


def prompt_to_messages(prompt: Any) -> list[dict[str, Any]] | None:
    """Return OpenAI-style chat messages when the prompt has that shape."""
    parsed = parse_prompt(prompt)
    if not isinstance(parsed, list) or not parsed:
        return None
    messages: list[dict[str, Any]] = []
    for item in parsed:
        if not isinstance(item, dict):
            return None
        messages.append(_normalize_message(item))
    _repair_tool_message_sequence(messages)
    return messages


def _normalize_tool_call(tool_call: Any) -> Any:
    if not isinstance(tool_call, dict):
        return tool_call
    normalized = dict(tool_call)
    function = normalized.get("function")
    if not isinstance(function, dict):
        return normalized
    normalized_function = dict(function)
    arguments = normalized_function.get("arguments")
    if arguments is None:
        normalized_function["arguments"] = {}
    elif isinstance(arguments, str):
        try:
            decoded_arguments = json.loads(arguments)
        except json.JSONDecodeError:
            decoded_arguments = {"arguments": arguments}
        normalized_function["arguments"] = (
            decoded_arguments if isinstance(decoded_arguments, dict) else {}
        )
    normalized["function"] = normalized_function
    return normalized


def _normalize_message(message: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(message)
    tool_calls = normalized.get("tool_calls")
    if isinstance(tool_calls, list):
        normalized["tool_calls"] = [_normalize_tool_call(tool_call) for tool_call in tool_calls]
    return normalized


def _repair_tool_message_sequence(messages: list[dict[str, Any]]) -> None:
    pending_tool_calls: list[dict[str, Any]] = []
    tool_calls_by_id: dict[str, dict[str, Any]] = {}
    generated_tool_call_index = 0

    for message in messages:
        role = message.get("role")
        if role == "assistant":
            tool_calls = message.get("tool_calls")
            if not isinstance(tool_calls, list):
                pending_tool_calls = []
                continue
            repaired_calls: list[dict[str, Any]] = []
            for tool_call in tool_calls:
                if not isinstance(tool_call, dict):
                    continue
                function = tool_call.get("function")
                if not isinstance(function, dict):
                    repaired_calls.append(tool_call)
                    continue
                normalized_call = dict(tool_call)
                normalized_function = dict(function)
                had_missing_fields = False
                if not normalized_function.get("name"):
                    had_missing_fields = True
                    normalized_function["name"] = _infer_tool_name_from_arguments(
                        normalized_function.get("arguments")
                    )
                if not normalized_call.get("id") and had_missing_fields:
                    generated_tool_call_index += 1
                    normalized_call["id"] = f"tool_call_{generated_tool_call_index}"
                normalized_call["function"] = normalized_function
                call_id = normalized_call.get("id")
                if isinstance(call_id, str):
                    tool_calls_by_id[call_id] = normalized_call
                repaired_calls.append(normalized_call)
            message["tool_calls"] = repaired_calls
            pending_tool_calls = repaired_calls
            continue

        if role == "tool":
            tool_call_id = message.get("tool_call_id")
            matched_call: dict[str, Any] | None = None
            if tool_call_id and isinstance(tool_call_id, str):
                matched_call = tool_calls_by_id.get(tool_call_id)
            if matched_call is None:
                for candidate in pending_tool_calls:
                    candidate_id = candidate.get("id")
                    if candidate_id and all(
                        existing.get("tool_call_id") != candidate_id
                        for existing in messages
                        if existing is not message and existing.get("role") == "tool"
                    ):
                        matched_call = candidate
                        break
            if matched_call is None:
                continue
            if not message.get("tool_call_id"):
                message["tool_call_id"] = matched_call.get("id")
            if not message.get("name"):
                function = matched_call.get("function")
                if isinstance(function, dict):
                    message["name"] = function.get("name")


def _infer_tool_name_from_arguments(arguments: Any) -> str:
    if not isinstance(arguments, dict):
        return "tool"
    if "command" in arguments:
        return "bash"
    if "path" in arguments and "content" in arguments:
        return "write_file"
    if "path" in arguments:
        return "read_file"
    if "question" in arguments:
        return "ask_user"
    if "task" in arguments:
        return "spawn_agent"
    return "tool"


def parse_tools(tools: Any) -> list[dict[str, Any]] | None:
    """Decode the exported tools field when tool definitions are present."""
    parsed = parse_prompt(tools)
    if not isinstance(parsed, list) or not parsed:
        return None
    tool_defs: list[dict[str, Any]] = []
    for item in parsed:
        if not isinstance(item, dict):
            return None
        function = item.get("function")
        if item.get("type") == "function" and isinstance(function, dict):
            tool_defs.append(dict(function))
            continue
        tool_defs.append(dict(item))
    return tool_defs


def tokenize_prompt(
    tokenizer: ChatTokenizer,
    prompt: Any,
    *,
    tools: Any = None,
    add_generation_prompt: bool,
) -> tuple[list[int], str]:
    """Tokenize an exported prompt with a tokenizer chat template."""
    messages = prompt_to_messages(prompt)
    if messages is None:
        text = parse_prompt(prompt)
        rendered = text if isinstance(text, str) else _stable_json(text)
        return tokenizer.encode(rendered, add_special_tokens=False), rendered

    tool_defs = parse_tools(tools)
    token_ids = tokenizer.apply_chat_template(
        messages,
        tokenize=True,
        add_generation_prompt=add_generation_prompt,
        tools=tool_defs,
    )
    rendered_prompt = tokenizer.apply_chat_template(
        messages,
        tokenize=False,
        add_generation_prompt=add_generation_prompt,
        tools=tool_defs,
    )
    return _extract_token_ids(token_ids), str(rendered_prompt)


def _extract_token_ids(template_result: Any) -> list[int]:
    if hasattr(template_result, "get") and "input_ids" in template_result:
        template_result = template_result["input_ids"]
    if hasattr(template_result, "tolist"):
        template_result = template_result.tolist()
    if (
        isinstance(template_result, list)
        and template_result
        and isinstance(template_result[0], list)
    ):
        template_result = template_result[0]
    return list(template_result)


def hash_token_ids(token_ids: list[int], hash_n: int) -> list[int]:
    """Return chained deterministic integer hashes for consecutive token-id chunks."""
    if hash_n <= 0:
        raise ValueError("hash_n must be > 0")
    previous = HASH_SEED
    hashes: list[int] = []
    for offset in range(0, len(token_ids), hash_n):
        chunk = token_ids[offset : offset + hash_n]
        payload = json.dumps(
            {"previous": previous, "token_ids": chunk},
            separators=(",", ":"),
        ).encode("utf-8")
        previous = binascii.crc32(payload, HASH_SEED) & 0xFFFFFFFF
        hashes.append(previous)
    return hashes


def is_qwen_trace_record(record: dict[str, Any]) -> bool:
    """Return True when a row already contains qwen trace hash IDs."""
    return isinstance(record.get("hash_ids"), list)


def convert_qwen_trace_record(
    record: dict[str, Any],
    *,
    line_number: int,
    include_token_ids: bool,
) -> dict[str, Any]:
    """Convert one qwen trace row into the common tokenized prompt schema."""
    hash_ids = [item for item in record.get("hash_ids", []) if isinstance(item, int)]
    output: dict[str, Any] = {
        "line_number": line_number,
        "id": record.get("chat_id"),
        "request_id": None,
        "timestamp": record.get("timestamp"),
        "model_id": None,
        "provider": "qwen-trace",
        "tokenizer": "qwen-trace-format",
        "tool_count": 0,
        "logged_prompt_tokens": record.get("input_length"),
        "computed_prompt_tokens": len(hash_ids),
        "chat_id": record.get("chat_id"),
        "parent_chat_id": record.get("parent_chat_id"),
        "type": record.get("type"),
        "turn": record.get("turn"),
        "input_length": record.get("input_length"),
        "output_length": record.get("output_length"),
    }
    if include_token_ids:
        output["prompt_token_ids"] = hash_ids
        output["token_id_hash_algorithm"] = "qwen-trace-hash-ids"
    return output


def convert_record_to_qwen_trace(
    record: dict[str, Any],
    *,
    tokenizer: ChatTokenizer,
    hash_n: int,
    add_generation_prompt: bool,
    debug_failing_tools: bool = False,
    line_number: int | None = None,
) -> dict[str, Any]:
    """Convert one api_logs export row into qwen trace JSONL shape."""
    try:
        token_ids, _prompt_text = tokenize_prompt(
            tokenizer,
            record.get("prompt"),
            tools=record.get("tools"),
            add_generation_prompt=add_generation_prompt,
        )
    except Exception:
        if debug_failing_tools:
            _print_failing_tools_debug(record, line_number=line_number)
        raise
    return {
        "chat_id": record.get("id"),
        "parent_chat_id": -1,
        "timestamp": record.get("timestamp"),
        "input_length": len(token_ids),
        "output_length": _qwen_trace_output_length(record),
        "type": record.get("type") if isinstance(record.get("type"), str) else "text",
        "turn": record.get("turn") if isinstance(record.get("turn"), int) else 1,
        "hash_ids": hash_token_ids(token_ids, hash_n),
    }


def _qwen_trace_output_length(record: dict[str, Any]) -> int:
    for key in ("completion_tokens", "output_tokens", "response_tokens"):
        value = record.get(key)
        if isinstance(value, int):
            return value
        if value is not None:
            try:
                return int(value)
            except (TypeError, ValueError):
                continue
    return 0


def _parse_timestamp_seconds(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value).timestamp()
        except ValueError:
            return None
    return None


def _coerce_hash_ids(value: Any) -> tuple[int, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, int) and not isinstance(item, bool))


def _hash_id_overlap(left: tuple[int, ...], right: tuple[int, ...]) -> float:
    if not left or not right:
        return 0.0
    overlap_count = sum((Counter(left) & Counter(right)).values())
    return overlap_count / max(len(left), len(right))


def _coerce_int_id(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    return value if isinstance(value, int) else None


def _coerce_turn(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value


def _assign_qwen_trace_session(
    row: dict[str, Any],
    session_last_rows: list[dict[str, Any]],
) -> None:
    current_hash_ids = _coerce_hash_ids(row.get("hash_ids"))
    for index in range(len(session_last_rows) - 1, -1, -1):
        previous = session_last_rows[index]
        if (
            _hash_id_overlap(current_hash_ids, _coerce_hash_ids(previous.get("hash_ids")))
            >= QWEN_TRACE_PARENT_HASH_OVERLAP_THRESHOLD
        ):
            parent_chat_id = _coerce_int_id(previous.get("chat_id"))
            row["parent_chat_id"] = parent_chat_id if parent_chat_id is not None else -1
            row["session_id"] = previous["session_id"]
            row["turn"] = _coerce_turn(previous.get("turn")) + 1
            session_last_rows.pop(index)
            session_last_rows.append(row)
            return
    row["parent_chat_id"] = -1
    row["session_id"] = len(session_last_rows) + 1
    row["turn"] = 1
    session_last_rows.append(row)


def _ordered_qwen_trace_row(row: dict[str, Any]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key, value in row.items():
        if key == "session_id":
            continue
        if key == "hash_ids" and "session_id" in row:
            output["session_id"] = row["session_id"]
        output[key] = value
    if "session_id" in row and "session_id" not in output:
        output["session_id"] = row["session_id"]
    return output


def convert_record(
    record: dict[str, Any],
    *,
    tokenizer: ChatTokenizer | None,
    line_number: int,
    add_generation_prompt: bool,
    include_token_ids: bool,
    include_text: bool,
    hash_n: int | None = None,
    debug_failing_tools: bool = False,
) -> dict[str, Any]:
    """Convert one api_logs export row into a tokenized prompt row."""
    if is_qwen_trace_record(record):
        return convert_qwen_trace_record(
            record,
            line_number=line_number,
            include_token_ids=include_token_ids,
        )
    if tokenizer is None:
        raise RuntimeError("tokenizer is required for api_logs prompt rows")
    try:
        token_ids, prompt_text = tokenize_prompt(
            tokenizer,
            record.get("prompt"),
            tools=record.get("tools"),
            add_generation_prompt=add_generation_prompt,
        )
    except Exception:
        if debug_failing_tools:
            _print_failing_tools_debug(record, line_number=line_number)
        raise
    tools = parse_tools(record.get("tools"))
    output: dict[str, Any] = {
        "line_number": line_number,
        "id": record.get("id"),
        "request_id": record.get("request_id"),
        "timestamp": record.get("timestamp"),
        "model_id": record.get("model_id"),
        "provider": record.get("provider"),
        "tokenizer": tokenizer.name_or_path,
        "tool_count": len(tools or []),
        "logged_prompt_tokens": record.get("prompt_tokens"),
        "computed_prompt_tokens": len(token_ids),
    }
    if record.get("prompt_tokens") is not None:
        try:
            output["prompt_tokens_delta"] = len(token_ids) - int(record["prompt_tokens"])
        except (TypeError, ValueError):
            output["prompt_tokens_delta"] = None
    if include_text:
        output["prompt_text"] = prompt_text
    if include_token_ids:
        if hash_n is None:
            output["prompt_token_ids"] = token_ids
        else:
            output["prompt_token_ids"] = hash_token_ids(token_ids, hash_n)
            output["token_id_hash_n"] = hash_n
            output["token_id_hash_algorithm"] = "seeded-int-chained"
    return output


def _write_converted_rows(
    input_path: Path,
    output: TextIO,
    *,
    tokenizer_name_or_path: str,
    trust_remote_code: bool,
    add_generation_prompt: bool,
    include_token_ids: bool,
    include_text: bool,
    id_filter: set[int] | None,
    hash_n: int | None,
    qwen_trace_format: bool,
    debug_failing_tools: bool,
    workers: int,
) -> tuple[int, list[int], int]:
    skipped = 0

    def iter_records() -> Iterator[tuple[int, dict[str, Any]]]:
        nonlocal skipped
        with input_path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                text = line.strip()
                if not text:
                    continue
                try:
                    record = json.loads(text)
                except json.JSONDecodeError as exc:
                    print(
                        f"WARNING: skipped line {line_number}: invalid JSON ({exc})",
                        file=sys.stderr,
                    )
                    skipped += 1
                    continue
                if not isinstance(record, dict):
                    print(
                        f"WARNING: skipped line {line_number}: JSON value is not an object",
                        file=sys.stderr,
                    )
                    skipped += 1
                    continue
                if id_filter is not None and record.get("id") not in id_filter:
                    continue
                yield line_number, record

    row_count = 0
    prompt_deltas: list[int] = []
    tokenizer: ChatTokenizer | None = None
    qwen_trace_start_seconds: float | None = None
    qwen_trace_session_last_rows: list[dict[str, Any]] = []

    def write_row(row: dict[str, Any]) -> None:
        nonlocal row_count, qwen_trace_start_seconds
        if qwen_trace_format and "chat_id" in row:
            current_seconds = _parse_timestamp_seconds(row.get("timestamp"))
            if current_seconds is None:
                raise RuntimeError(f"invalid qwen trace timestamp: {row.get('timestamp')!r}")
            if qwen_trace_start_seconds is None:
                qwen_trace_start_seconds = current_seconds
            row = dict(row)
            row["timestamp"] = round(current_seconds - qwen_trace_start_seconds, 3)
            _assign_qwen_trace_session(
                row,
                qwen_trace_session_last_rows,
            )
            row = _ordered_qwen_trace_row(row)
        output.write(json.dumps(row, ensure_ascii=False))
        output.write("\n")
        row_count += 1
        prompt_tokens_delta = row.get("prompt_tokens_delta")
        if isinstance(prompt_tokens_delta, int):
            prompt_deltas.append(int(prompt_tokens_delta))

    if workers == 1:
        for line_number, record in iter_records():
            if tokenizer is None and not is_qwen_trace_record(record):
                tokenizer = load_tokenizer(
                    tokenizer_name_or_path, trust_remote_code=trust_remote_code
                )
            if qwen_trace_format and not is_qwen_trace_record(record):
                if tokenizer is None:
                    raise RuntimeError("tokenizer is required for qwen trace conversion")
                write_row(
                    convert_record_to_qwen_trace(
                        record,
                        tokenizer=tokenizer,
                        hash_n=hash_n if hash_n is not None else 0,
                        add_generation_prompt=add_generation_prompt,
                        debug_failing_tools=debug_failing_tools,
                        line_number=line_number,
                    )
                )
                continue
            write_row(
                convert_record(
                    record,
                    tokenizer=tokenizer,
                    line_number=line_number,
                    add_generation_prompt=add_generation_prompt,
                    include_token_ids=include_token_ids,
                    include_text=include_text,
                    hash_n=hash_n,
                    debug_failing_tools=debug_failing_tools,
                )
            )
    else:
        tasks = (
            (
                line_number,
                record,
                add_generation_prompt,
                include_token_ids,
                include_text,
                hash_n,
                qwen_trace_format,
                debug_failing_tools,
            )
            for line_number, record in iter_records()
        )
        with ProcessPoolExecutor(
            max_workers=workers,
            initializer=_init_process_tokenizer,
            initargs=(tokenizer_name_or_path, trust_remote_code),
        ) as executor:
            for row in executor.map(_convert_record_in_process, tasks):
                write_row(row)

    return row_count, prompt_deltas, skipped


def prompt_delta_stats(rows: list[dict[str, Any]]) -> dict[str, int | float] | None:
    """Return summary stats for rows with numeric prompt_tokens_delta values."""
    deltas = [
        int(row["prompt_tokens_delta"])
        for row in rows
        if isinstance(row.get("prompt_tokens_delta"), int)
    ]
    return _prompt_delta_stats_from_deltas(deltas)


def _prompt_delta_stats_from_deltas(deltas: list[int]) -> dict[str, int | float] | None:
    if not deltas:
        return None
    ordered = sorted(deltas)
    return {
        "count": len(ordered),
        "min": ordered[0],
        "p50": _percentile(ordered, 0.50),
        "p90": _percentile(ordered, 0.90),
        "p95": _percentile(ordered, 0.95),
        "p99": _percentile(ordered, 0.99),
        "max": ordered[-1],
        "mean": sum(ordered) / len(ordered),
    }


def _percentile(ordered_values: list[int], percentile: float) -> int:
    index = round((len(ordered_values) - 1) * percentile)
    return ordered_values[index]


def _print_prompt_delta_stats(deltas: list[int]) -> None:
    stats = _prompt_delta_stats_from_deltas(deltas)
    if stats is None:
        print("prompt_tokens_delta stats: no numeric deltas", file=sys.stderr)
        return
    print(
        "prompt_tokens_delta stats: "
        f"count={stats['count']} "
        f"min={stats['min']} "
        f"p50={stats['p50']} "
        f"p90={stats['p90']} "
        f"p95={stats['p95']} "
        f"p99={stats['p99']} "
        f"max={stats['max']} "
        f"mean={stats['mean']:.2f}",
        file=sys.stderr,
    )


def _init_process_tokenizer(tokenizer_name_or_path: str, trust_remote_code: bool) -> None:
    global _PROCESS_TOKENIZER_NAME_OR_PATH, _PROCESS_TRUST_REMOTE_CODE
    _PROCESS_TOKENIZER_NAME_OR_PATH = tokenizer_name_or_path
    _PROCESS_TRUST_REMOTE_CODE = trust_remote_code


def _print_failing_tools_debug(record: dict[str, Any], *, line_number: int | None) -> None:
    prompt = parse_prompt(record.get("prompt"))
    debug_info = {
        "line_number": line_number,
        "record_id": record.get("id"),
        "chat_id": record.get("chat_id"),
        "request_id": record.get("request_id"),
        "model_id": record.get("model_id"),
        "provider": record.get("provider"),
        "tools": record.get("tools"),
        "parsed_tools": parse_tools(record.get("tools")),
        "prompt_summary": _debug_prompt_summary(prompt),
        "tool_related_messages": _debug_tool_related_messages(prompt),
    }
    print(
        f"DEBUG failing tools: {json.dumps(debug_info, ensure_ascii=False, default=str)}",
        file=sys.stderr,
    )


def _debug_prompt_summary(prompt: Any) -> dict[str, Any]:
    if not isinstance(prompt, list):
        return {"prompt_type": type(prompt).__name__}
    summary: dict[str, Any] = {
        "prompt_type": "list",
        "message_count": len(prompt),
        "roles": [],
    }
    roles: list[Any] = []
    content_kinds: list[dict[str, Any]] = []
    for msg in prompt[:12]:
        if not isinstance(msg, dict):
            roles.append(type(msg).__name__)
            continue
        roles.append(msg.get("role"))
        content = msg.get("content")
        content_kinds.append(
            {
                "role": msg.get("role"),
                "content_type": type(content).__name__,
                "has_tool_calls": isinstance(msg.get("tool_calls"), list),
                "has_name": "name" in msg,
            }
        )
    summary["roles"] = roles
    summary["content_kinds"] = content_kinds
    return summary


def _debug_tool_related_messages(prompt: Any) -> list[dict[str, Any]]:
    if not isinstance(prompt, list):
        return []
    results: list[dict[str, Any]] = []
    for index, msg in enumerate(prompt):
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role not in {"assistant", "tool", "function"} and not isinstance(
            msg.get("tool_calls"), list
        ):
            continue
        item: dict[str, Any] = {
            "index": index,
            "role": role,
            "keys": sorted(msg.keys()),
        }
        if "name" in msg:
            item["name"] = msg.get("name")
        if "tool_call_id" in msg:
            item["tool_call_id"] = msg.get("tool_call_id")
        content = msg.get("content")
        item["content_type"] = type(content).__name__
        if isinstance(content, list) and content:
            first = content[0]
            item["content0_keys"] = (
                sorted(first.keys()) if isinstance(first, dict) else type(first).__name__
            )
        if isinstance(msg.get("tool_calls"), list):
            item["tool_calls"] = msg.get("tool_calls")
        results.append(item)
        if len(results) >= 8:
            break
    return results


def _convert_record_in_process(
    task: tuple[int, dict[str, Any], bool, bool, bool, int | None, bool, bool],
) -> dict[str, Any]:
    (
        line_number,
        record,
        add_generation_prompt,
        include_token_ids,
        include_text,
        hash_n,
        qwen_trace_format,
        debug_failing_tools,
    ) = task
    if is_qwen_trace_record(record):
        return convert_record(
            record,
            tokenizer=None,
            line_number=line_number,
            add_generation_prompt=add_generation_prompt,
            include_token_ids=include_token_ids,
            include_text=include_text,
            hash_n=hash_n,
            debug_failing_tools=debug_failing_tools,
        )
    global _PROCESS_TOKENIZER
    if _PROCESS_TOKENIZER is None:
        if _PROCESS_TOKENIZER_NAME_OR_PATH is None:
            raise RuntimeError("process tokenizer is not initialized")
        _PROCESS_TOKENIZER = load_tokenizer(
            _PROCESS_TOKENIZER_NAME_OR_PATH,
            trust_remote_code=_PROCESS_TRUST_REMOTE_CODE,
        )
    if qwen_trace_format:
        if hash_n is None:
            raise RuntimeError("hash_n is required for qwen trace conversion")
        return convert_record_to_qwen_trace(
            record,
            tokenizer=_PROCESS_TOKENIZER,
            hash_n=hash_n,
            add_generation_prompt=add_generation_prompt,
            debug_failing_tools=debug_failing_tools,
            line_number=line_number,
        )
    return convert_record(
        record,
        tokenizer=_PROCESS_TOKENIZER,
        line_number=line_number,
        add_generation_prompt=add_generation_prompt,
        include_token_ids=include_token_ids,
        include_text=include_text,
        hash_n=hash_n,
        debug_failing_tools=debug_failing_tools,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Tokenize prompt fields from an ops/db/export_logs.py JSONL export."
    )
    parser.add_argument("input", type=Path, help="Path to api_logs_export.jsonl")
    parser.add_argument(
        "--tokenizer",
        default=DEFAULT_TOKENIZER,
        help=(
            "Hugging Face tokenizer name or local path to use with apply_chat_template "
            f"(default: {DEFAULT_TOKENIZER})"
        ),
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="Output JSONL file path (default: stdout)",
    )
    parser.add_argument(
        "--trust-remote-code",
        action="store_true",
        help="Pass trust_remote_code=True to AutoTokenizer.from_pretrained",
    )
    parser.add_argument(
        "--no-add-generation-prompt",
        action="store_true",
        help="Do not append the assistant generation marker in the chat template",
    )
    parser.add_argument(
        "--count-only",
        action="store_true",
        help="Only write token counts; omit prompt_token_ids",
    )
    parser.add_argument(
        "--include-text",
        action="store_true",
        help="Also include the normalized prompt text that was tokenized",
    )
    parser.add_argument(
        "--hash-n",
        type=int,
        default=None,
        help="Replace each N token IDs in prompt_token_ids with a deterministic chained integer hash",
    )
    parser.add_argument(
        "--qwen-trace-format",
        action="store_true",
        help="When used with --hash-n, emit qwen trace JSONL rows with integer hash_ids",
    )
    parser.add_argument(
        "--debug-failing-tools",
        action="store_true",
        help="Print row identifiers and raw/parsed tools payloads before re-raising tokenization failures",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        help=f"Number of log rows to tokenize in parallel (default: {DEFAULT_WORKERS})",
    )
    parser.add_argument("--id", type=int, nargs="*", help="Only convert records with these IDs")
    return parser


def load_tokenizer(tokenizer_name_or_path: str, *, trust_remote_code: bool) -> ChatTokenizer:
    """Load a Hugging Face tokenizer lazily so tests do not need transformers."""
    from transformers import AutoTokenizer

    return AutoTokenizer.from_pretrained(
        tokenizer_name_or_path,
        trust_remote_code=trust_remote_code,
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.input.is_file():
        print(f"ERROR: input file does not exist: {args.input}", file=sys.stderr)
        return 1
    if args.hash_n is not None and args.hash_n <= 0:
        print("ERROR: --hash-n must be > 0", file=sys.stderr)
        return 1
    if args.qwen_trace_format and args.hash_n is None:
        print("ERROR: --qwen-trace-format requires --hash-n", file=sys.stderr)
        return 1
    if args.qwen_trace_format and args.count_only:
        print("ERROR: --qwen-trace-format cannot be combined with --count-only", file=sys.stderr)
        return 1
    if args.workers <= 0:
        print("ERROR: --workers must be > 0", file=sys.stderr)
        return 1

    id_filter = set(args.id) if args.id is not None else None
    try:
        if args.output is None:
            row_count, prompt_deltas, skipped = _write_converted_rows(
                args.input,
                sys.stdout,
                tokenizer_name_or_path=args.tokenizer,
                trust_remote_code=args.trust_remote_code,
                add_generation_prompt=not args.no_add_generation_prompt,
                include_token_ids=not args.count_only,
                include_text=args.include_text,
                id_filter=id_filter,
                hash_n=args.hash_n,
                qwen_trace_format=args.qwen_trace_format,
                debug_failing_tools=args.debug_failing_tools,
                workers=args.workers,
            )
        else:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with args.output.open("w", encoding="utf-8") as output:
                row_count, prompt_deltas, skipped = _write_converted_rows(
                    args.input,
                    output,
                    tokenizer_name_or_path=args.tokenizer,
                    trust_remote_code=args.trust_remote_code,
                    add_generation_prompt=not args.no_add_generation_prompt,
                    include_token_ids=not args.count_only,
                    include_text=args.include_text,
                    id_filter=id_filter,
                    hash_n=args.hash_n,
                    qwen_trace_format=args.qwen_trace_format,
                    debug_failing_tools=args.debug_failing_tools,
                    workers=args.workers,
                )
    except (OSError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    destination = str(args.output) if args.output is not None else "stdout"
    print(f"Wrote {row_count} tokenized prompts to {destination}", file=sys.stderr)
    _print_prompt_delta_stats(prompt_deltas)
    if skipped:
        print(f"Skipped {skipped} invalid rows", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
