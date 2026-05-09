#!/usr/bin/env python3
"""Convert exported api_logs prompt fields into token JSONL rows."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any, Protocol, TextIO

from transformers import AutoTokenizer

DEFAULT_TOKENIZER = "zai-org/GLM-5.1"
DEFAULT_WORKERS = min(4, os.cpu_count() or 1)
_PROCESS_TOKENIZER: ChatTokenizer | None = None


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
    if isinstance(arguments, str):
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


def parse_tools(tools: Any) -> list[dict[str, Any]] | None:
    """Decode the exported tools field when tool definitions are present."""
    parsed = parse_prompt(tools)
    if not isinstance(parsed, list) or not parsed:
        return None
    tool_defs: list[dict[str, Any]] = []
    for item in parsed:
        if not isinstance(item, dict):
            return None
        tool_defs.append(item)
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


def hash_token_ids(token_ids: list[int], hash_n: int) -> list[str]:
    """Return chained SHA-256 hashes for consecutive token-id chunks."""
    if hash_n <= 0:
        raise ValueError("hash_n must be > 0")
    previous = ""
    hashes: list[str] = []
    for offset in range(0, len(token_ids), hash_n):
        chunk = token_ids[offset : offset + hash_n]
        payload = json.dumps(
            {"previous": previous, "token_ids": chunk},
            separators=(",", ":"),
        ).encode("utf-8")
        previous = hashlib.sha256(payload).hexdigest()
        hashes.append(previous)
    return hashes


def convert_record(
    record: dict[str, Any],
    *,
    tokenizer: ChatTokenizer,
    line_number: int,
    add_generation_prompt: bool,
    include_token_ids: bool,
    include_text: bool,
    hash_n: int | None = None,
) -> dict[str, Any]:
    """Convert one api_logs export row into a tokenized prompt row."""
    token_ids, prompt_text = tokenize_prompt(
        tokenizer,
        record.get("prompt"),
        tools=record.get("tools"),
        add_generation_prompt=add_generation_prompt,
    )
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
            output["token_id_hash_algorithm"] = "sha256-chained"
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
    workers: int,
) -> tuple[list[dict[str, Any]], int]:
    skipped = 0
    records: list[tuple[int, dict[str, Any]]] = []
    with input_path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            text = line.strip()
            if not text:
                continue
            try:
                record = json.loads(text)
            except json.JSONDecodeError as exc:
                print(f"WARNING: skipped line {line_number}: invalid JSON ({exc})", file=sys.stderr)
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
            records.append((line_number, record))

    if workers == 1 or len(records) <= 1:
        tokenizer = load_tokenizer(tokenizer_name_or_path, trust_remote_code=trust_remote_code)
        rows = [
            convert_record(
                record,
                tokenizer=tokenizer,
                line_number=line_number,
                add_generation_prompt=add_generation_prompt,
                include_token_ids=include_token_ids,
                include_text=include_text,
                hash_n=hash_n,
            )
            for line_number, record in records
        ]
    else:
        tasks = [
            (
                line_number,
                record,
                add_generation_prompt,
                include_token_ids,
                include_text,
                hash_n,
            )
            for line_number, record in records
        ]
        with ProcessPoolExecutor(
            max_workers=workers,
            initializer=_init_process_tokenizer,
            initargs=(tokenizer_name_or_path, trust_remote_code),
        ) as executor:
            rows = list(executor.map(_convert_record_in_process, tasks))

    for row in rows:
        output.write(json.dumps(row, ensure_ascii=False))
        output.write("\n")

    return rows, skipped


def prompt_delta_stats(rows: list[dict[str, Any]]) -> dict[str, int | float] | None:
    """Return summary stats for rows with numeric prompt_tokens_delta values."""
    deltas = [
        int(row["prompt_tokens_delta"])
        for row in rows
        if isinstance(row.get("prompt_tokens_delta"), int)
    ]
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


def _print_prompt_delta_stats(rows: list[dict[str, Any]]) -> None:
    stats = prompt_delta_stats(rows)
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
    global _PROCESS_TOKENIZER
    _PROCESS_TOKENIZER = load_tokenizer(tokenizer_name_or_path, trust_remote_code=trust_remote_code)


def _convert_record_in_process(
    task: tuple[int, dict[str, Any], bool, bool, bool, int | None],
) -> dict[str, Any]:
    line_number, record, add_generation_prompt, include_token_ids, include_text, hash_n = task
    if _PROCESS_TOKENIZER is None:
        raise RuntimeError("process tokenizer is not initialized")
    return convert_record(
        record,
        tokenizer=_PROCESS_TOKENIZER,
        line_number=line_number,
        add_generation_prompt=add_generation_prompt,
        include_token_ids=include_token_ids,
        include_text=include_text,
        hash_n=hash_n,
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
        help="Replace each N token IDs in prompt_token_ids with a chained SHA-256 hash",
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
    if args.workers <= 0:
        print("ERROR: --workers must be > 0", file=sys.stderr)
        return 1

    id_filter = set(args.id) if args.id is not None else None
    try:
        if args.output is None:
            rows, skipped = _write_converted_rows(
                args.input,
                sys.stdout,
                tokenizer_name_or_path=args.tokenizer,
                trust_remote_code=args.trust_remote_code,
                add_generation_prompt=not args.no_add_generation_prompt,
                include_token_ids=not args.count_only,
                include_text=args.include_text,
                id_filter=id_filter,
                hash_n=args.hash_n,
                workers=args.workers,
            )
        else:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with args.output.open("w", encoding="utf-8") as output:
                rows, skipped = _write_converted_rows(
                    args.input,
                    output,
                    tokenizer_name_or_path=args.tokenizer,
                    trust_remote_code=args.trust_remote_code,
                    add_generation_prompt=not args.no_add_generation_prompt,
                    include_token_ids=not args.count_only,
                    include_text=args.include_text,
                    id_filter=id_filter,
                    hash_n=args.hash_n,
                    workers=args.workers,
                )
    except (OSError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    destination = str(args.output) if args.output is not None else "stdout"
    print(f"Wrote {len(rows)} tokenized prompts to {destination}", file=sys.stderr)
    _print_prompt_delta_stats(rows)
    if skipped:
        print(f"Skipped {skipped} invalid rows", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
