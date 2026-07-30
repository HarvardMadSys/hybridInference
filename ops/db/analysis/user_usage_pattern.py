"""Characterize a single user's API usage pattern over time.

Resolves an email to its ``users`` row and reports the *shape* of that account's
traffic -- not just totals, but how it is distributed. For the user this prints:
per-day request/token/error counts, an hour-of-day histogram, a full per-model
breakdown, the client surfaces / user agents seen, the error mix by status code,
and request-size statistics.

This complements ``compare_users.py`` (which focuses on cross-account overlap):
here the question is "how does this one person use the service?".

Usage:
    python ops/db/analysis/user_usage_pattern.py a@x.com
    python ops/db/analysis/user_usage_pattern.py a@x.com --days 60
    python ops/db/analysis/user_usage_pattern.py a@x.com --json
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


async def _gather(conn: asyncpg.Connection, email: str, days: int) -> dict[str, Any]:
    """Collect the usage-pattern slices for one user."""
    norm = email.strip().lower()
    user = await conn.fetchrow("SELECT * FROM users WHERE lower(trim(email)) = $1", norm)
    if user is None:
        return {"email": email, "found": False}
    uid = user["id"]

    totals = await conn.fetchrow(
        "SELECT count(*) AS requests, count(DISTINCT model_id) AS distinct_models, "
        "min(timestamp) AS first, max(timestamp) AS last, "
        "sum(total_tokens) AS total_tokens, "
        "count(*) FILTER (WHERE status_code >= 400) AS errors "
        "FROM api_logs WHERE user_id = $1",
        uid,
    )

    per_day = await conn.fetch(
        "SELECT date_trunc('day', timestamp)::date AS day, count(*) AS n, "
        "sum(total_tokens) AS toks, count(*) FILTER (WHERE status_code >= 400) AS err "
        "FROM api_logs WHERE user_id = $1 AND timestamp > now() - make_interval(days => $2) "
        "GROUP BY day ORDER BY day",
        uid,
        days,
    )

    by_hour = await conn.fetch(
        "SELECT extract(hour FROM timestamp)::int AS hour, count(*) AS n "
        "FROM api_logs WHERE user_id = $1 GROUP BY hour ORDER BY hour",
        uid,
    )

    by_model = await conn.fetch(
        "SELECT model_id, count(*) AS n, sum(total_tokens) AS toks, "
        "count(*) FILTER (WHERE status_code >= 400) AS err "
        "FROM api_logs WHERE user_id = $1 GROUP BY model_id ORDER BY n DESC",
        uid,
    )

    by_surface = await conn.fetch(
        "SELECT metadata->>'surface' AS surface, metadata->>'user_agent' AS user_agent, "
        "count(*) AS n FROM api_logs WHERE user_id = $1 "
        "GROUP BY surface, user_agent ORDER BY n DESC LIMIT 15",
        uid,
    )

    by_status = await conn.fetch(
        "SELECT status_code, count(*) AS n FROM api_logs "
        "WHERE user_id = $1 AND status_code >= 400 GROUP BY status_code ORDER BY n DESC",
        uid,
    )

    sizes = await conn.fetchrow(
        "SELECT avg(total_tokens)::int AS avg_tok, max(total_tokens) AS max_tok, "
        "percentile_cont(0.5) WITHIN GROUP (ORDER BY total_tokens)::int AS med_tok, "
        "avg(prompt_tokens)::int AS avg_prompt, avg(completion_tokens)::int AS avg_completion "
        "FROM api_logs WHERE user_id = $1 AND total_tokens IS NOT NULL",
        uid,
    )

    return {
        "email": email,
        "found": True,
        "days": days,
        "user": dict(user),
        "totals": dict(totals) if totals else {},
        "per_day": [dict(r) for r in per_day],
        "by_hour": [dict(r) for r in by_hour],
        "by_model": [dict(r) for r in by_model],
        "by_surface": [dict(r) for r in by_surface],
        "by_status": [dict(r) for r in by_status],
        "sizes": dict(sizes) if sizes else {},
    }


def _fmt_dt(v: Any) -> str:
    return v.isoformat(sep=" ", timespec="seconds") if v else "—"


def _print_report(p: dict[str, Any]) -> None:
    if not p["found"]:
        print(f"USER: {p['email']}  —  NOT FOUND")
        return

    u = p["user"]
    t = p["totals"]
    print("=" * 78)
    print(f"USER: {p['email']}   (id={u['id']})")
    print(
        f"  role={u['role']}  status={u['status']}  email_verified={u['email_verified']}  "
        f"max_concurrent_requests={u.get('max_concurrent_requests')}"
    )
    print(
        f"  created_at={_fmt_dt(u['created_at'])}  last_login_at={_fmt_dt(u.get('last_login_at'))}"
    )
    print(
        f"  api_logs: {t.get('requests', 0):,} requests, {t.get('distinct_models', 0)} models, "
        f"{t.get('total_tokens') or 0:,} tokens, {t.get('errors', 0)} errors"
    )
    print(f"  span: {_fmt_dt(t.get('first'))} → {_fmt_dt(t.get('last'))}")

    print(f"\n--- Requests per day (last {p['days']}d) ---")
    for r in p["per_day"]:
        print(f"  {r['day']}  {r['n']:>6} reqs  {(r['toks'] or 0):>15,} tok  {r['err']:>4} err")
    if not p["per_day"]:
        print("  (none in window)")

    print("\n--- Hour-of-day distribution (UTC, all time) ---")
    peak = max((r["n"] for r in p["by_hour"]), default=1)
    for r in p["by_hour"]:
        bar = "#" * max(1, round(r["n"] / peak * 40)) if r["n"] else ""
        print(f"  {r['hour']:02d}h {r['n']:>6}  {bar}")

    print("\n--- Models ---")
    for r in p["by_model"]:
        print(
            f"  {r['model_id'] or '<none>':<30} {r['n']:>6} reqs  "
            f"{(r['toks'] or 0):>15,} tok  {r['err']:>4} err"
        )

    print("\n--- Surface / client ---")
    for r in p["by_surface"]:
        print(f"  {r['n']:>6}  surface={r['surface']}  ua={r['user_agent']}")

    print("\n--- Errors by status code ---")
    for r in p["by_status"]:
        print(f"  {r['status_code']}  {r['n']}")
    if not p["by_status"]:
        print("  (no errors)")

    s = p["sizes"]
    if s.get("avg_tok") is not None:
        print("\n--- Request size (total_tokens) ---")
        print(
            f"  avg={s['avg_tok']:,}  median={s['med_tok']:,}  max={s['max_tok']:,}  "
            f"| avg_prompt={s['avg_prompt']}  avg_completion={s['avg_completion']}"
        )


async def main(email: str, days: int, as_json: bool) -> int:
    """Gather and print one user's usage pattern."""
    pool = await asyncpg.create_pool(_dsn(), min_size=1, max_size=2)
    try:
        async with pool.acquire() as conn:
            profile = await _gather(conn, email, days)
    finally:
        await pool.close()

    if as_json:
        print(json.dumps(profile, indent=2, default=str))
    else:
        _print_report(profile)
    return 0


def cli() -> None:
    """Parse CLI args, load env, and run the report."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("email", help="User email to profile")
    parser.add_argument(
        "-n", "--days", type=int, default=30, help="Per-day window in days (default: 30)"
    )
    parser.add_argument(
        "--json", action="store_true", help="Emit JSON instead of a human-readable report"
    )
    parser.add_argument("--env-file", default=None, help="Path to .env (default: auto-detect)")
    args = parser.parse_args()
    _load_env(args.env_file)
    raise SystemExit(asyncio.run(main(args.email, args.days, args.json)))


if __name__ == "__main__":
    cli()
