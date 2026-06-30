"""Rank the heaviest users over a window and summarize each at a glance.

Aggregates ``api_logs`` over the last N days, ranks users by total tokens (or by
request count with ``--by requests``), resolves each to its account email/role,
and prints a leaderboard with the signal needed to triage usage: volume, error
rate, top model, the dominant client user-agent, and account age. This is the
entry point for "who are our biggest users and what are they running?"; drill
into any one with ``user_usage_pattern.py`` / ``user_prompt_sample.py``.

Usage:
    python ops/db/analysis/top_users.py --days 7 --limit 25
    python ops/db/analysis/top_users.py --days 1 --by requests
    python ops/db/analysis/top_users.py --days 7 --json
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


async def _rank(
    conn: asyncpg.Connection, days: int, order_by: str, limit: int
) -> list[dict[str, Any]]:
    order_col = "total_tokens" if order_by == "tokens" else "requests"
    rows = await conn.fetch(
        f"""
        WITH agg AS (
            SELECT
                user_id,
                count(*) AS requests,
                sum(total_tokens) AS total_tokens,
                count(*) FILTER (WHERE status_code >= 400) AS errors,
                count(DISTINCT model_id) AS distinct_models,
                min(timestamp) AS first_seen,
                max(timestamp) AS last_seen
            FROM api_logs
            WHERE timestamp > now() - make_interval(days => $1)
              AND user_id IS NOT NULL
            GROUP BY user_id
        ),
        top_model AS (
            SELECT DISTINCT ON (user_id) user_id, model_id, count(*) AS n
            FROM api_logs
            WHERE timestamp > now() - make_interval(days => $1) AND user_id IS NOT NULL
            GROUP BY user_id, model_id
            ORDER BY user_id, n DESC
        ),
        top_ua AS (
            SELECT DISTINCT ON (user_id) user_id, metadata->>'user_agent' AS ua, count(*) AS n
            FROM api_logs
            WHERE timestamp > now() - make_interval(days => $1) AND user_id IS NOT NULL
            GROUP BY user_id, metadata->>'user_agent'
            ORDER BY user_id, n DESC
        )
        SELECT
            a.*,
            u.email, u.role, u.status, u.created_at,
            tm.model_id AS top_model,
            ua.ua AS top_ua
        FROM agg a
        LEFT JOIN users u ON u.id = a.user_id
        LEFT JOIN top_model tm ON tm.user_id = a.user_id
        LEFT JOIN top_ua ua ON ua.user_id = a.user_id
        ORDER BY a.{order_col} DESC NULLS LAST
        LIMIT $2
        """,
        days,
        limit,
    )
    return [dict(r) for r in rows]


def _fmt_dt(v: Any) -> str:
    return v.isoformat(sep=" ", timespec="minutes") if v else "—"


def _print_report(rows: list[dict[str, Any]], days: int, order_by: str) -> None:
    print(f"Top {len(rows)} users by {order_by} over the last {days} day(s)\n")
    for i, r in enumerate(rows, 1):
        email = r.get("email") or "<no account>"
        toks = r.get("total_tokens") or 0
        err = r.get("errors") or 0
        reqs = r.get("requests") or 0
        err_pct = (err / reqs * 100) if reqs else 0
        print(f"{i:>3}. {email}  [{r.get('role') or '?'}/{r.get('status') or '?'}]")
        print(
            f"     {reqs:,} reqs   {toks:,} tok   {err} err ({err_pct:.1f}%)   "
            f"{r.get('distinct_models')} models"
        )
        print(f"     top_model={r.get('top_model')}   ua={r.get('top_ua')}")
        print(
            f"     created={_fmt_dt(r.get('created_at'))}   "
            f"active={_fmt_dt(r.get('first_seen'))} → {_fmt_dt(r.get('last_seen'))}   "
            f"(id={r.get('user_id')})"
        )
        print()


async def main(days: int, order_by: str, limit: int, as_json: bool) -> int:
    """Rank top users and print or emit the leaderboard."""
    pool = await asyncpg.create_pool(_dsn(), min_size=1, max_size=2)
    try:
        async with pool.acquire() as conn:
            rows = await _rank(conn, days, order_by, limit)
    finally:
        await pool.close()

    if as_json:
        print(json.dumps({"days": days, "by": order_by, "users": rows}, indent=2, default=str))
    else:
        _print_report(rows, days, order_by)
    return 0


def cli() -> None:
    """Parse CLI args, load env, and run the leaderboard."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "-n", "--days", type=int, default=7, help="Look back this many days (default: 7)"
    )
    parser.add_argument(
        "--by",
        choices=["tokens", "requests"],
        default="tokens",
        help="Ranking metric (default: tokens)",
    )
    parser.add_argument(
        "-l", "--limit", type=int, default=25, help="How many users to show (default: 25)"
    )
    parser.add_argument(
        "--json", action="store_true", help="Emit JSON instead of a human-readable report"
    )
    parser.add_argument("--env-file", default=None, help="Path to .env (default: auto-detect)")
    args = parser.parse_args()
    _load_env(args.env_file)
    raise SystemExit(asyncio.run(main(args.days, args.by, args.limit, args.json)))


if __name__ == "__main__":
    cli()
