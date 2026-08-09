"""Identify users who hit the Kimi coding-plan "non-coding agent" 403 and the messages they sent.

The Kimi coding plan (provider ``kimi_coding`` -> api.kimi.com/coding) rejects any
request that does not carry a recognized coding-agent identity with HTTP 403:

    "For Coding is currently only available for Coding Agents such as CLI, Code,
     Roo Code, Kilo Code, etc."

This script scans ``api_logs`` for those rejections over the past N days, resolves
each caller's email, and prints the user message(s) that triggered them.

Usage:
    python ops/db/analysis/kimi_non_coding_users.py --days 7
    python ops/db/analysis/kimi_non_coding_users.py --days 30 --json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from typing import Any

import asyncpg
import dotenv

# Substring of the upstream Kimi coding-plan rejection. Matched case-insensitively.
NON_CODING_SIGNATURE = "only available for Coding Agents"


def _load_env(env_path: str | None = None) -> None:
    candidates = [
        env_path,
        os.environ.get("ENV_FILE"),
        "/srv/hybridInference/.env",
        str(Path(__file__).resolve().parents[3] / ".env"),
    ]
    for p in candidates:
        if p and Path(p).is_file():
            dotenv.load_dotenv(p, override=False)
            return


def _dsn() -> str:
    user = os.environ.get("DB_USER")
    if not user:
        raise SystemExit(
            "ERROR: DB_USER is not set. Load the .env file or set the environment variable."
        )
    return (
        f"postgresql://{user}:{os.environ.get('DB_PASSWORD', '')}"
        f"@{os.environ.get('DB_HOST', 'localhost')}:{os.environ.get('DB_PORT', '5432')}"
        f"/{os.environ.get('DB_NAME', 'freeinference_db')}"
    )


def _message_list(prompt: Any) -> list[Any]:
    """Decode the ``api_logs.prompt`` column (TEXT holding a JSON message list)."""
    p = prompt
    if isinstance(p, str):
        try:
            p = json.loads(p)
        except json.JSONDecodeError:
            return []
    return p if isinstance(p, list) else []


def _extract_user_messages(request_payload: Any, max_chars: int, prompt: Any = None) -> list[str]:
    """Pull user-turn text out of a stored request (OpenAI or Anthropic shape).

    The turns are stored in the dedicated ``prompt`` column; ``request_payload``
    carries a ``messages`` copy only on rows logged before that de-duplication,
    so prefer the column and fall back to the payload for historical rows.
    """
    messages = _message_list(prompt)
    if not messages:
        payload = request_payload
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                return [payload[:max_chars]]
        if not isinstance(payload, dict):
            return []
        raw = payload.get("messages") or []
        messages = raw if isinstance(raw, list) else []

    out: list[str] = []
    for msg in messages:
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            # Anthropic / multimodal content blocks
            parts = [
                b.get("text", "")
                for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            ]
            text = "\n".join(p for p in parts if p)
        else:
            text = json.dumps(content, default=str)
        text = (text or "").strip()
        if text:
            out.append(text[:max_chars])
    return out


async def _fetch(pool: asyncpg.Pool, days: int) -> list[asyncpg.Record]:
    query = """
        SELECT
            l.timestamp,
            l.request_id,
            l.model_id,
            l.provider,
            l.user_id,
            l.metadata,
            l.request_payload,
            l.prompt,
            l.error,
            e.email
        FROM api_logs l
        LEFT JOIN LATERAL (
            SELECT email
            FROM login_events le
            WHERE le.user_id = l.user_id AND le.email IS NOT NULL
            ORDER BY le.created_at DESC
            LIMIT 1
        ) e ON TRUE
        WHERE l.status_code = 403
          AND l.error ILIKE '%' || $1 || '%'
          AND l.timestamp > now() - make_interval(days => $2)
        ORDER BY l.timestamp DESC
    """
    async with pool.acquire() as conn:
        return await conn.fetch(query, NON_CODING_SIGNATURE, days)


def _surface_meta(metadata: Any) -> tuple[str | None, str | None]:
    md = metadata
    if isinstance(md, str):
        try:
            md = json.loads(md)
        except json.JSONDecodeError:
            md = {}
    if not isinstance(md, dict):
        md = {}
    return md.get("surface"), md.get("user_agent")


async def main(days: int, as_json: bool, max_chars: int) -> int:
    """Scan for Kimi non-coding 403s and print or emit them."""
    pool = await asyncpg.create_pool(_dsn(), min_size=1, max_size=2)
    try:
        rows = await _fetch(pool, days)
    finally:
        await pool.close()

    # Group by user
    by_user: dict[str, dict[str, Any]] = {}
    for r in rows:
        uid = r["user_id"] or "<anonymous>"
        surface, user_agent = _surface_meta(r["metadata"])
        entry = by_user.setdefault(
            uid,
            {"user_id": r["user_id"], "email": r["email"], "count": 0, "requests": []},
        )
        if r["email"] and not entry["email"]:
            entry["email"] = r["email"]
        entry["count"] += 1
        entry["requests"].append(
            {
                "timestamp": r["timestamp"].isoformat(),
                "request_id": r["request_id"],
                "model_id": r["model_id"],
                "provider": r["provider"],
                "surface": surface,
                "user_agent": user_agent,
                "messages": _extract_user_messages(r["request_payload"], max_chars, r["prompt"]),
            }
        )

    if as_json:
        print(json.dumps(list(by_user.values()), indent=2, default=str))
        return 0

    if not rows:
        print(f"No Kimi non-coding-plan 403s in the past {days} day(s).")
        return 0

    print(
        f"Kimi non-coding-plan 403s in the past {days} day(s): "
        f"{len(rows)} request(s) from {len(by_user)} user(s)\n"
    )
    for entry in sorted(by_user.values(), key=lambda e: e["count"], reverse=True):
        who = entry["email"] or "<unknown email>"
        print("=" * 78)
        print(f"USER: {who}  (user_id={entry['user_id']})  —  {entry['count']} rejection(s)")
        for req in entry["requests"]:
            ua = req["user_agent"] if req["user_agent"] is not None else "<none>"
            print(f"\n  • {req['timestamp']}  [{req['surface']}]  UA={ua}")
            print(f"    request_id={req['request_id']}  model={req['model_id']}")
            if req["messages"]:
                for i, m in enumerate(req["messages"]):
                    label = "    user msg:" if i == 0 else "             "
                    print(f"{label} {m!r}")
            else:
                print("    user msg: <none captured>")
        print()
    return 0


def cli() -> None:
    """Parse CLI args, load env, and run the scan."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "-n", "--days", type=int, default=7, help="Look back this many days (default: 7)"
    )
    parser.add_argument(
        "--json", action="store_true", help="Emit JSON instead of a human-readable report"
    )
    parser.add_argument(
        "--max-chars", type=int, default=2000, help="Truncate each captured message (default: 2000)"
    )
    parser.add_argument(
        "--env-file",
        default=None,
        help="Path to .env (default: auto-detect /srv/hybridInference/.env)",
    )
    args = parser.parse_args()
    _load_env(args.env_file)
    raise SystemExit(asyncio.run(main(days=args.days, as_json=args.json, max_chars=args.max_chars)))


if __name__ == "__main__":
    cli()
