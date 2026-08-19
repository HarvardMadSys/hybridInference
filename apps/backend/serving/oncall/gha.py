r"""Workflow-side entrypoints for the ``codex-oncall`` GitHub Actions job.

Runs inside ``.github/workflows/codex-oncall.yml`` with
``PYTHONPATH=apps/backend`` and only ``pydantic`` + ``httpx`` installed:

    python -m serving.oncall.gha render --payload payload.json \
        --prompt-out prompt.txt --schema-out schema.json
    python -m serving.oncall.gha post --payload payload.json \
        --analysis analysis.json --codex-log codex-stdout.jsonl
    python -m serving.oncall.gha post --payload payload.json \
        --failed --run-url "$RUN_URL"

``render`` turns the dispatched alert into the Codex prompt and output
schema; ``post`` delivers the structured analysis (or a failure notice) as a
reply in the original Slack alert thread. The Slack bot token is read from
the ``SLACK_BOT_TOKEN`` environment variable so it never appears in argv.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

# Shared with the cloud-agent backend: the prompt a model sees and the parser
# that accepts exactly one schema-valid analysis must not fork between the two
# dispatch paths. Re-exported here so the workflow-side surface (and its
# tests) keeps one import root.
from serving.oncall.analysis import parse_analysis_output, render_prompt, render_schema
from serving.oncall.service import format_analysis
from serving.oncall.slack import SlackClient

__all__ = [
    "main",
    "parse_analysis_output",
    "parse_thread_id",
    "render_prompt",
    "validate_codex_log",
]


def parse_thread_id(json_lines: str) -> str | None:
    """Extract the first Codex ``thread.started`` identifier from JSONL."""
    for line in json_lines.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "thread.started" and event.get("thread_id"):
            return str(event["thread_id"])
    return None


def validate_codex_log(json_lines: str) -> None:
    """Reject analyses that were not grounded by a successful shell command."""
    has_successful_command = False
    has_completed_turn = False
    for line in json_lines.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        event_type = event.get("type")
        if event_type == "turn.failed":
            raise ValueError("Codex turn failed")
        if event_type == "turn.completed":
            has_completed_turn = True
            continue
        if event_type != "item.completed":
            continue
        item = event.get("item")
        if (
            isinstance(item, dict)
            and item.get("type") == "command_execution"
            and item.get("exit_code") == 0
        ):
            has_successful_command = True

    if not has_successful_command:
        raise ValueError("Codex log has no successful command_execution")
    if not has_completed_turn:
        raise ValueError("Codex turn did not complete")


def _load_payload(path: str) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "alert" not in data:
        raise SystemExit("payload file does not look like a oncall dispatch payload")
    return data


def _cmd_render(args: argparse.Namespace) -> int:
    payload = _load_payload(args.payload)
    schema = render_schema()
    prompt = render_prompt(payload["alert"], schema)
    Path(args.schema_out).write_text(schema, encoding="utf-8")
    Path(args.prompt_out).write_text(prompt, encoding="utf-8")
    print(f"rendered prompt ({len(prompt)} bytes) and schema ({len(schema)} bytes)")
    return 0


def _cmd_post(args: argparse.Namespace) -> int:
    payload = _load_payload(args.payload)
    token = os.environ.get("SLACK_BOT_TOKEN", "").strip()
    if not token:
        print("SLACK_BOT_TOKEN is not set", file=sys.stderr)
        return 2
    channel = str(payload.get("slack_channel_id", "")).strip()
    thread_ts = str(payload.get("slack_thread_ts", "")).strip()
    if not channel or not thread_ts:
        print("payload is missing slack_channel_id / slack_thread_ts", file=sys.stderr)
        return 2

    if args.failed:
        text = (
            "*Codex on-call unavailable*\n"
            "The analysis workflow failed; the original alert above still stands."
        )
        if args.run_url:
            text += f"\nRun logs: {args.run_url}"
    else:
        if not args.codex_log:
            print("codex log is required before publishing an analysis", file=sys.stderr)
            return 2
        try:
            codex_log = Path(args.codex_log).read_text(encoding="utf-8")
            validate_codex_log(codex_log)
        except (OSError, ValueError) as exc:
            print(f"refusing to publish ungrounded Codex analysis: {exc}", file=sys.stderr)
            return 2
        analysis = parse_analysis_output(Path(args.analysis).read_text(encoding="utf-8"))
        thread_id = parse_thread_id(codex_log)
        text = format_analysis(analysis, thread_id)

    asyncio.run(SlackClient(token, channel).post(text, thread_ts=thread_ts))
    print("posted to Slack thread", thread_ts)
    return 0


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and run the requested workflow step."""
    parser = argparse.ArgumentParser(prog="serving.oncall.gha")
    sub = parser.add_subparsers(dest="command", required=True)

    render = sub.add_parser("render", help="write the Codex prompt and output schema")
    render.add_argument("--payload", required=True)
    render.add_argument("--prompt-out", required=True)
    render.add_argument("--schema-out", required=True)
    render.set_defaults(func=_cmd_render)

    post = sub.add_parser("post", help="post the analysis or a failure notice to Slack")
    post.add_argument("--payload", required=True)
    post.add_argument("--analysis", help="path to the structured analysis JSON")
    post.add_argument("--codex-log", help="codex --json stdout, for the thread id")
    post.add_argument("--failed", action="store_true", help="post a failure notice instead")
    post.add_argument("--run-url", help="Actions run URL to include in the failure notice")
    post.set_defaults(func=_cmd_post)

    args = parser.parse_args(argv)
    if args.command == "post" and not args.failed and not args.analysis:
        parser.error("post requires --analysis unless --failed is given")
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
