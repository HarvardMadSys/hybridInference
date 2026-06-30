"""Dump a user's recent api_logs rows to inspect duplicate / repeated LLM calls.

For a given user this prints the raw request timeline -- timestamp, request_id,
status, model, token counts (prompt / completion / total), client, and a short
fingerprint of the request payload (first user message + message count). It is a
debugging tool for questions like "why are there several LLM calls with the same
input and output token counts?": grouping by (prompt_tokens, completion_tokens)
reveals whether they are retries, agent tool-call rounds, streaming re-sends, or
genuinely distinct turns.

Usage:
    python ops/db/analysis/request_log_tail.py artemorlov0605@gmail.com --limit 40
    python ops/db/analysis/request_log_tail.py a@x.com --model minimax-m3 --dupes
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
from typing import Any

import asyncpg
import dotenv


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


def _payload_fingerprint(payload: Any) -> tuple[int, str, str]:
    """Return (message_count, first-user-text preview, sha1 of full payload text)."""
    raw = payload if isinstance(payload, str) else json.dumps(payload, default=str, sort_keys=True)
    digest = hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()[:10]
    p = payload
    if isinstance(p, str):
        try:
            p = json.loads(p)
        except json.JSONDecodeError:
            return (0, raw[:60], digest)
    if not isinstance(p, dict):
        return (0, "", digest)
    msgs = p.get("messages", []) or []
    first_user = ""
    for m in msgs:
        if isinstance(m, dict) and m.get("role") == "user":
            c = m.get("content")
            if isinstance(c, str):
                first_user = c
            elif isinstance(c, list):
                first_user = " ".join(b.get("text", "") for b in c if isinstance(b, dict))
            break
    return (len(msgs), first_user.strip()[:60], digest)


async def _tail(
    conn: asyncpg.Connection, email: str, model: str | None, limit: int
) -> dict[str, Any]:
    user = await conn.fetchrow(
        "SELECT id FROM users WHERE lower(trim(email)) = $1", email.strip().lower()
    )
    if user is None:
        return {"email": email, "found": False}
    uid = user["id"]
    clause = "AND model_id ILIKE '%' || $3 || '%'" if model else ""
    params: list[Any] = [uid, limit] + ([model] if model else [])
    rows = await conn.fetch(
        f"""
        SELECT timestamp, request_id, status_code, model_id,
               prompt_tokens, completion_tokens, total_tokens,
               metadata->>'user_agent' AS ua, request_payload
        FROM api_logs
        WHERE user_id = $1 {clause}
        ORDER BY timestamp DESC
        LIMIT $2
        """,
        *params,
    )
    return {"email": email, "found": True, "rows": rows}


async def main(email: str, model: str | None, limit: int, dupes_only: bool) -> int:
    """Print a user's recent request timeline with token counts and payload fingerprints."""
    pool = await asyncpg.create_pool(_dsn(), min_size=1, max_size=2)
    try:
        async with pool.acquire() as conn:
            res = await _tail(conn, email, model, limit)
    finally:
        await pool.close()

    if not res["found"]:
        print(f"USER: {email}  —  NOT FOUND")
        return 0

    rows = list(res["rows"])
    # Tag each row with its payload fingerprint.
    enriched = []
    for r in rows:
        nmsg, first_user, digest = _payload_fingerprint(r["request_payload"])
        enriched.append((r, nmsg, first_user, digest))

    # Group by (prompt_tokens, completion_tokens) to surface identical-token clusters.
    from collections import defaultdict

    groups: dict[tuple[Any, Any], list[Any]] = defaultdict(list)
    for r, *_ in enriched:
        groups[(r["prompt_tokens"], r["completion_tokens"])].append(r)
    dup_keys = {k for k, v in groups.items() if len(v) > 1}

    print(f"USER: {email}{f'  model~{model!r}' if model else ''}  —  {len(rows)} recent rows")
    print(
        f"identical (prompt_tokens, completion_tokens) clusters: "
        f"{len(dup_keys)} (covering {sum(len(groups[k]) for k in dup_keys)} rows)\n"
    )
    print(
        f"{'timestamp':<20} {'http':>4} {'prompt':>8} {'compl':>6} {'msgs':>4}  {'payload':>10}  first_user"
    )
    for r, nmsg, first_user, digest in enriched:
        key = (r["prompt_tokens"], r["completion_tokens"])
        if dupes_only and key not in dup_keys:
            continue
        mark = "DUP" if key in dup_keys else "   "
        ts = r["timestamp"].isoformat(sep=" ", timespec="seconds")[:19]
        print(
            f"{ts:<20} {r['status_code'] or 0:>4} {r['prompt_tokens'] or 0:>8} "
            f"{r['completion_tokens'] or 0:>6} {nmsg:>4}  {digest:>10} {mark} {first_user!r}"
        )
    return 0


def cli() -> None:
    """Parse CLI args, load env, and run the tail."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("email", help="User email to inspect")
    parser.add_argument("-m", "--model", default=None, help="Only this model_id (substring)")
    parser.add_argument(
        "-l", "--limit", type=int, default=40, help="How many recent rows (default: 40)"
    )
    parser.add_argument(
        "--dupes", action="store_true", help="Show only rows in an identical-token cluster"
    )
    parser.add_argument("--env-file", default=None, help="Path to .env (default: auto-detect)")
    args = parser.parse_args()
    _load_env(args.env_file)
    raise SystemExit(asyncio.run(main(args.email, args.model, args.limit, args.dupes)))


if __name__ == "__main__":
    cli()
