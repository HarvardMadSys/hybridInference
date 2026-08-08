"""Sample a user's stored request payloads to see *what* they are actually prompting.

Resolves an email to its ``users`` row and pulls a sample of recent ``api_logs``
rows (optionally filtered to a single model substring). For each sampled request
it surfaces the client/user-agent, the opening of the system prompt (which usually
identifies the calling agent -- Kilo Code, opencode, Claude Code, etc.), and the
user-turn text. This answers "what is this person building?" rather than just how
much traffic they generate (see ``user_usage_pattern.py`` for the latter).

Handles both OpenAI-shape (``messages``) and Anthropic-shape (``system`` + content
blocks) requests, and both storage eras: the conversation turns are read from the
dedicated ``prompt`` column, falling back to the ``messages`` copy that older rows
still keep inside ``request_payload``.

Usage:
    python ops/db/analysis/user_prompt_sample.py user@example.com --model minimax
    python ops/db/analysis/user_prompt_sample.py a@x.com --limit 20 --max-chars 800
    python ops/db/analysis/user_prompt_sample.py a@x.com --json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import asyncpg
import dotenv

# Make the ``serving`` package importable when running from a source checkout
# without an editable install (apps/backend is the package root).
_BACKEND_ROOT = Path(__file__).resolve().parents[3] / "apps" / "backend"
if _BACKEND_ROOT.is_dir() and str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from serving.utils.prompt_sampling import (
    as_payload_dict,
    system_opener,
    user_agent_from_metadata,
    user_messages,
)


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


async def _sample(
    conn: asyncpg.Connection, email: str, model: str | None, limit: int
) -> dict[str, Any]:
    norm = email.strip().lower()
    user = await conn.fetchrow("SELECT id FROM users WHERE lower(trim(email)) = $1", norm)
    if user is None:
        return {"email": email, "found": False}
    uid = user["id"]

    # ``prompt`` holds the conversation turns; ``request_payload`` keeps only a
    # historical copy, so both are needed to cover old and new rows alike.
    cols = (
        "SELECT timestamp, request_id, model_id, provider, status_code, metadata, "
        "request_payload, prompt "
    )
    if model:
        rows = await conn.fetch(
            cols + "FROM api_logs WHERE user_id = $1 AND model_id ILIKE '%' || $2 || '%' "
            "ORDER BY timestamp DESC LIMIT $3",
            uid,
            model,
            limit,
        )
    else:
        rows = await conn.fetch(
            cols + "FROM api_logs WHERE user_id = $1 ORDER BY timestamp DESC LIMIT $2",
            uid,
            limit,
        )
    return {"email": email, "found": True, "rows": rows}


async def main(email: str, model: str | None, limit: int, max_chars: int, as_json: bool) -> int:
    """Sample a user's request payloads and print or emit them."""
    pool = await asyncpg.create_pool(_dsn(), min_size=1, max_size=2)
    try:
        async with pool.acquire() as conn:
            res = await _sample(conn, email, model, limit)
    finally:
        await pool.close()

    if not res["found"]:
        print(f"USER: {email}  —  NOT FOUND")
        return 0

    samples = []
    for r in res["rows"]:
        payload = as_payload_dict(r["request_payload"])
        turns = r["prompt"]
        samples.append(
            {
                "timestamp": r["timestamp"].isoformat(),
                "request_id": r["request_id"],
                "model_id": r["model_id"],
                "provider": r["provider"],
                "status_code": r["status_code"],
                "user_agent": user_agent_from_metadata(r["metadata"]),
                "system_opener": system_opener(payload, max_chars, turns),
                "user_messages": user_messages(payload, max_chars, turns),
            }
        )

    if as_json:
        print(
            json.dumps({"email": email, "model": model, "samples": samples}, indent=2, default=str)
        )
        return 0

    scope = f" model~{model!r}" if model else ""
    print(f"USER: {email}{scope}  —  {len(samples)} sampled request(s)\n")
    for s in samples:
        print("=" * 78)
        print(
            f"{s['timestamp']}  {s['model_id']} ({s['provider']})  http={s['status_code']}  ua={s['user_agent']}"
        )
        print(f"  request_id={s['request_id']}")
        if s["system_opener"]:
            print(f"  system: {s['system_opener']!r}")
        if s["user_messages"]:
            for i, m in enumerate(s["user_messages"]):
                label = "  user:" if i == 0 else "       "
                print(f"{label} {m!r}")
        else:
            print("  user: <none captured>")
        print()
    return 0


def cli() -> None:
    """Parse CLI args, load env, and run the sampler."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("email", help="User email to sample")
    parser.add_argument(
        "-m",
        "--model",
        default=None,
        help="Only sample requests whose model_id matches this substring",
    )
    parser.add_argument(
        "-l",
        "--limit",
        type=int,
        default=15,
        help="Number of recent requests to sample (default: 15)",
    )
    parser.add_argument(
        "--max-chars", type=int, default=1200, help="Truncate each message (default: 1200)"
    )
    parser.add_argument(
        "--json", action="store_true", help="Emit JSON instead of a human-readable report"
    )
    parser.add_argument("--env-file", default=None, help="Path to .env (default: auto-detect)")
    args = parser.parse_args()
    _load_env(args.env_file)
    raise SystemExit(
        asyncio.run(main(args.email, args.model, args.limit, args.max_chars, args.json))
    )


if __name__ == "__main__":
    cli()
