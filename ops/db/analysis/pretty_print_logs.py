#!/usr/bin/env python3
"""Pretty-print JSONL api_logs files for human inspection.

Usage:
    python pretty_print_logs.py api_logs_export.jsonl
    python pretty_print_logs.py api_logs_export.jsonl --truncate 500
    python pretty_print_logs.py api_logs_export.jsonl --no-prompt
    python pretty_print_logs.py api_logs_export.jsonl --id 12
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SEP = "=" * 90
THIN = "-" * 90
ROLE_COLORS = {
    "system": "\033[36m",
    "user": "\033[33m",
    "assistant": "\033[32m",
    "tool": "\033[35m",
}
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"


def _color(role: str) -> str:
    return ROLE_COLORS.get(role, "")


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n{DIM}... ({len(text)} chars total){RESET}"


def _print_messages(messages: list[dict], truncate: int) -> None:
    for msg in messages:
        role = msg.get("role", "?")
        content = msg.get("content", "")
        tool_name = msg.get("name", "")
        tool_calls = msg.get("tool_calls")
        color = _color(role)

        if tool_name:
            print(f"\n{color}{BOLD}[{role}]{RESET} (tool: {tool_name})")
            if isinstance(content, str):
                print(_truncate(content, truncate))
            else:
                print(_truncate(json.dumps(content, indent=2), truncate))
        elif tool_calls:
            print(f"\n{color}{BOLD}[{role} tool_calls]{RESET}")
            for tc in tool_calls:
                fn = tc.get("function", {})
                name = fn.get("name", "?")
                args_raw = fn.get("arguments", "")
                try:
                    args = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
                    args_str = json.dumps(args, indent=2)
                except (json.JSONDecodeError, TypeError):
                    args_str = args_raw
                print(f"  {BOLD}-> {name}{RESET}")
                print(_truncate(args_str, truncate))
        else:
            print(f"\n{color}{BOLD}[{role}]{RESET}")
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        print(_truncate(part.get("text", ""), truncate))
                    else:
                        print(_truncate(json.dumps(part, indent=2), truncate))
            else:
                print(_truncate(str(content), truncate))


def _print_response(raw: str, truncate: int) -> None:
    try:
        resp = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        print(_truncate(str(raw), truncate))
        return
    if not isinstance(resp, dict):
        print(_truncate(json.dumps(resp), truncate))
        return

    for choice in resp.get("choices", []):
        msg = choice.get("message", {})
        content = msg.get("content", "")
        tool_calls = msg.get("tool_calls")
        finish = choice.get("finish_reason", "")
        color = _color("assistant")

        if content:
            print(f"\n{color}{BOLD}[assistant]{RESET} (finish: {finish})")
            print(_truncate(content, truncate))
        if tool_calls:
            print(f"\n{color}{BOLD}[assistant tool_calls]{RESET} (finish: {finish})")
            for tc in tool_calls:
                fn = tc.get("function", {})
                name = fn.get("name", "?")
                args_raw = fn.get("arguments", "")
                try:
                    args = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
                    args_str = json.dumps(args, indent=2)
                except (json.JSONDecodeError, TypeError):
                    args_str = args_raw
                print(f"  {BOLD}-> {name}{RESET}")
                print(_truncate(args_str, truncate))

    usage = resp.get("usage", {})
    if usage:
        print(
            f"\n{DIM}usage: prompt={usage.get('prompt_tokens')}, "
            f"completion={usage.get('completion_tokens')}, "
            f"total={usage.get('total_tokens')}{RESET}"
        )


def pretty_print_record(
    record: dict, truncate: int, show_prompt: bool, show_response: bool
) -> None:
    """Pretty-print a single api_logs record to stdout."""
    rid = record.get("id", "?")
    ts = record.get("timestamp", "?")
    model = record.get("model_id", "?")
    provider = record.get("provider", "?")
    ttft = record.get("ttft_ms")
    latency = record.get("latency_ms")
    prompt_tokens = record.get("prompt_tokens")
    completion_tokens = record.get("completion_tokens")
    status = record.get("status_code", "?")
    cost = record.get("cost_usd", "")
    user = record.get("user_id", "")

    print(f"\n{SEP}")
    print(f"{BOLD}RECORD {rid}{RESET}  {ts}  model={model}  provider={provider}  status={status}")
    print(
        f"  ttft={ttft}ms  latency={latency}ms  "
        f"tokens={prompt_tokens}+{completion_tokens}  "
        f"cost=${cost}  user={user}"
    )
    print(SEP)

    if show_prompt:
        print(f"\n{BOLD}--- PROMPT ---{RESET}")
        raw_prompt = record.get("prompt") or "[]"
        try:
            messages = json.loads(raw_prompt) if isinstance(raw_prompt, str) else raw_prompt
            if isinstance(messages, list) and all(isinstance(msg, dict) for msg in messages):
                _print_messages(messages, truncate)
            else:
                print(_truncate(json.dumps(messages), truncate))
        except (json.JSONDecodeError, TypeError):
            print(_truncate(str(raw_prompt), truncate))

    if show_response:
        print(f"\n{BOLD}--- RESPONSE ---{RESET}")
        raw_resp = record.get("response") or "{}"
        _print_response(raw_resp if isinstance(raw_resp, str) else json.dumps(raw_resp), truncate)

    print()


def main() -> None:
    """Parse CLI args and pretty-print the given JSONL api_logs file."""
    parser = argparse.ArgumentParser(description="Pretty-print JSONL api_logs files")
    parser.add_argument("file", help="Path to JSONL file")
    parser.add_argument(
        "--truncate",
        "-t",
        type=int,
        default=800,
        help="Max chars per content block (0=unlimited, default: 800)",
    )
    parser.add_argument("--no-prompt", action="store_true", help="Hide prompts")
    parser.add_argument("--no-response", action="store_true", help="Hide responses")
    parser.add_argument("--no-color", action="store_true", help="Disable colors")
    parser.add_argument("--id", type=int, nargs="*", help="Only show records with these IDs")
    parser.add_argument(
        "--summary", action="store_true", help="Only print a summary table (no prompt/response)"
    )
    args = parser.parse_args()

    if args.no_color:
        global ROLE_COLORS, RESET, BOLD, DIM
        ROLE_COLORS = dict.fromkeys(ROLE_COLORS, "")
        RESET = ""
        BOLD = ""
        DIM = ""

    truncate = args.truncate if args.truncate > 0 else sys.maxsize
    path = Path(args.file)
    if not path.is_file():
        print(f"ERROR: {args.file} not found")
        sys.exit(1)

    records: list[dict] = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))

    if args.id is not None:
        id_set = set(args.id)
        records = [r for r in records if r.get("id") in id_set]

    if args.summary:
        print(
            f"{'ID':>5}  {'Timestamp':<28}  {'Model':<16}  {'TTFT':>6}  {'Latency':>7}  "
            f"{'PromptT':>8}  {'ComplT':>7}  {'Cost':>10}  {'Status':>6}"
        )
        print(THIN)
        for r in records:
            print(
                f"{r.get('id', '?'):>5}  "
                f"{str(r.get('timestamp', '?'))[:28]:<28}  "
                f"{r.get('model_id', '?'):<16}  "
                f"{r.get('ttft_ms', '?'):>6}  "
                f"{r.get('latency_ms', '?'):>7}  "
                f"{r.get('prompt_tokens', '?'):>8}  "
                f"{r.get('completion_tokens', '?'):>7}  "
                f"${r.get('cost_usd', ''):>9}  "
                f"{r.get('status_code', '?'):>6}"
            )
        print(f"\n{len(records)} records total")
        return

    show_prompt = not args.no_prompt
    show_response = not args.no_response
    for record in records:
        pretty_print_record(record, truncate, show_prompt, show_response)


if __name__ == "__main__":
    main()
