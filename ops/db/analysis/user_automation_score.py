"""Score how script-driven vs. human-driven each user's API usage is (CLI).

Produces a per-user ``automation_score`` in ``[0, 1]`` where **HIGH means the
traffic is mostly driven by automatic scripts / batch jobs / cron** and **LOW
means a human is using the service interactively** (a chat UI, or a human-driven
coding agent such as Claude Code).

This is a thin command-line wrapper: the scoring methodology and the SQL that
gathers per-user ``api_logs`` aggregates live in
``serving.analytics.automation_score`` (the same code the admin dashboard uses),
so the CLI and the dashboard can never drift apart.

Usage::

    # rank every user with >= 20 requests in the last 30 days, most script-like first
    python ops/db/analysis/user_automation_score.py --min-requests 20

    # profile one user with a full per-signal breakdown
    python ops/db/analysis/user_automation_score.py --email a@x.com

    # machine-readable
    python ops/db/analysis/user_automation_score.py --min-requests 20 --json
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

# The scoring methodology lives in the serving package so the CLI and the admin
# dashboard share one implementation. Make ``apps/backend`` importable when this
# script is run directly (python ops/db/analysis/user_automation_score.py).
_BACKEND = Path(__file__).resolve().parents[3] / "apps" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from serving.analytics.automation_score import (
    MIN_REQUESTS,
    SIGNAL_WEIGHTS,
    score_users_from_logs,
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


async def _gather(
    conn: asyncpg.Connection, days: int, user_id: str | None = None
) -> list[dict[str, Any]]:
    """Score users and enrich each record with display fields from ``users``."""
    user_ids = [user_id] if user_id else None
    records = await score_users_from_logs(conn, days=days, user_ids=user_ids)
    if not records:
        return []

    user_rows = await conn.fetch(
        "SELECT id, email, user_name, role, status FROM users WHERE id = ANY($1::text[])",
        [r["user_id"] for r in records],
    )
    users = {row["id"]: dict(row) for row in user_rows}
    for r in records:
        user = users.get(r["user_id"], {})
        r["email"] = user.get("email")
        r["user_name"] = user.get("user_name")
        r["role"] = user.get("role")
        r["status"] = user.get("status")
    return records


# --- reporting ---------------------------------------------------------------


def _fmt_dt(value: Any) -> str:
    return value.isoformat(sep=" ", timespec="seconds") if value else "—"


def _sub_cell(signals: dict[str, Any], name: str) -> str:
    """Format a signal's sub-score for the ranked table (or ``—`` when dropped)."""
    value = signals[name]["sub"]
    return f"{value:>5.2f}" if value is not None else f"{'—':>5}"


def _print_table(records: list[dict[str, Any]], days: int) -> None:
    print("=" * 96)
    print(f"AUTOMATION SCORE  (last {days}d)   HIGH = script/batch/cron, LOW = interactive human")
    print("-" * 96)
    print(
        f"{'score':>6} {'conf':>5} {'band':<18} {'reqs':>7}  "
        f"{'turns':>5} {'size':>5} {'ua':>5} {'daily':>5}  email"
    )
    print("-" * 96)
    for r in records:
        sig = r["signals"]
        flag = "*" if r["insufficient_data"] else " "
        email = r["email"] or f"(id {r['user_id']})"
        role = f" [{r['role']}]" if r["role"] and r["role"] != "free" else ""
        print(
            f"{r['score']:>6.2f}{flag}{r['confidence']:>5.2f} {r['band']:<18} {r['n_req']:>7,}  "
            f"{_sub_cell(sig, 'turn_pattern')} {_sub_cell(sig, 'prompt_size_dispersion')} "
            f"{_sub_cell(sig, 'client_tool_prior')} {_sub_cell(sig, 'daily_activity_shape')}  "
            f"{email}{role}"
        )
    print("-" * 96)
    print(f"* = insufficient_data (fewer than {MIN_REQUESTS} requests; score shrunk to prior)")


def _print_detail(record: dict[str, Any], days: int) -> None:
    r = record
    print("=" * 78)
    label = r["email"] or f"(id {r['user_id']})"
    print(f"USER: {label}   role={r['role']}  status={r['status']}")
    print(f"  window: last {days}d   requests={r['n_req']:,}   total_tokens={r['total_tokens']:,}")
    print(f"  span: {_fmt_dt(r['first_seen'])} → {_fmt_dt(r['last_seen'])}")
    print()
    print(f"  AUTOMATION SCORE : {r['score']:.3f}   ({r['band']})")
    print(f"  confidence       : {r['confidence']:.3f}")
    if r["insufficient_data"]:
        print(f"  NOTE: insufficient_data (< {MIN_REQUESTS} requests) — score shrunk toward 0.5")

    print("\n  --- signals (sub-score in [0,1]; HIGH = automated) ---")
    for name, weight in SIGNAL_WEIGHTS.items():
        sig = r["signals"][name]
        if sig["available"]:
            bar = "#" * round(sig["sub"] * 20)
            print(f"    {name:<24} w={weight:<4} {sig['sub']:.2f}  {bar}")
        else:
            print(f"    {name:<24} w={weight:<4}  —    (unavailable — dropped)")

    d = r["detail"]
    print("\n  --- supporting metrics ---")
    keys = [
        ("one_shot_fraction", "one-shot chat fraction"),
        ("p90_user_turns", "p90 user-turn depth"),
        ("prompt_token_rcv", "prompt-size IQR/median"),
        ("ua_base", "UA-class base value"),
        ("agent_share", "coding-agent opener share"),
        ("hour_coverage", "active-hour coverage"),
        ("hour_entropy_norm", "hour entropy (norm)"),
        ("max_quiet_gap_hours", "longest quiet gap (h)"),
        ("interarrival_rcv", "inter-arrival IQR/median"),
        ("toolcall_share", "tool-call request share"),
    ]
    for key, desc in keys:
        if key in d and d[key] is not None:
            value = d[key]
            shown = f"{value:.3f}" if isinstance(value, float) else str(value)
            print(f"    {desc:<28} {shown}")


async def main(email: str | None, days: int, min_requests: int, top: int, as_json: bool) -> int:
    """Gather, score, and print the automation report."""
    pool = await asyncpg.create_pool(_dsn(), min_size=1, max_size=2)
    try:
        async with pool.acquire() as conn:
            if email is not None:
                uid = await conn.fetchval(
                    "SELECT id FROM users WHERE lower(trim(email)) = $1",
                    email.strip().lower(),
                )
                if uid is None:
                    print(f"USER: {email}  —  NOT FOUND")
                    return 1
                records = await _gather(conn, days, user_id=uid)
            else:
                records = await _gather(conn, days)
    finally:
        await pool.close()

    if email is None:
        records = [r for r in records if r["n_req"] >= min_requests]
        if top > 0:
            records = records[:top]

    if as_json:
        print(json.dumps(records, indent=2, default=str))
    elif email is not None:
        if not records:
            print(f"USER: {email}  —  no requests in the last {days}d")
        else:
            _print_detail(records[0], days)
    elif not records:
        print(f"No users with >= {min_requests} requests in the last {days}d.")
    else:
        _print_table(records, days)
    return 0


def cli() -> None:
    """Parse CLI args, load env, and run the report."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--email", default=None, help="Profile a single user (full per-signal breakdown)"
    )
    parser.add_argument(
        "-n", "--days", type=int, default=30, help="Trailing window in days (default: 30)"
    )
    parser.add_argument(
        "--min-requests",
        type=int,
        default=20,
        help="Only rank users with at least this many requests (default: 20)",
    )
    parser.add_argument(
        "--top", type=int, default=40, help="Show the top-N most script-like users (default: 40)"
    )
    parser.add_argument(
        "--json", action="store_true", help="Emit JSON instead of a human-readable report"
    )
    parser.add_argument("--env-file", default=None, help="Path to .env (default: auto-detect)")
    args = parser.parse_args()
    _load_env(args.env_file)
    raise SystemExit(
        asyncio.run(main(args.email, args.days, args.min_requests, args.top, args.json))
    )


if __name__ == "__main__":
    cli()
