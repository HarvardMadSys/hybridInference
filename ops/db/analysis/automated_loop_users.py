"""Find users running unattended / automated agent loops (vs. interactive use).

Scans ``api_logs`` request payloads over a window for the textual fingerprints of
automation -- scheduled cron jobs, "auto-mode" harnesses, self-described automated
loops, continuous-monitoring swarms, and tool-injected (non-user) check turns --
then groups the hits by user, resolves each email, and reports which signature(s)
matched plus a sample. The point is to separate "a human is driving this" from
"this is a robot looping on its own", which matters for capacity and abuse triage.

Signature matching is a case-insensitive substring test against the JSON request
payload. Signatures are deliberately specific phrases that interactive coding
agents do not normally emit, to keep false positives low.

Usage:
    python ops/db/analysis/automated_loop_users.py --days 7
    python ops/db/analysis/automated_loop_users.py --days 1 --json
    python ops/db/analysis/automated_loop_users.py --days 7 --min-hits 5
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

# Each entry: (label, signature substring matched case-insensitively against request_payload::text).
# Phrases chosen to fire on unattended/looping harnesses, not ordinary interactive agent traffic.
SIGNATURES: list[tuple[str, str]] = [
    ("scheduled-cron", "running as a scheduled cron job"),
    ("cron-job", "scheduled cron job"),
    ("automated-loop", "agent in an automated loop"),
    ("automated-loop2", "in an automated loop"),
    ("auto-mode", "auto-mode"),
    ("gsd-auto-mode", "executing gsd auto-mode"),
    ("continuous-monitor", "continuously monitor"),
    ("continuous-monitor2", "continuous monitoring"),
    ("pi-lens-auto", "automated check — not a user request"),
    ("pi-lens-auto2", "automated checks run on your edits"),
    ("swarm-directive", "research directive"),
    ("mission-anchor", "mission anchor"),
    ("unattended", "unattended"),
]


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


async def _scan(conn: asyncpg.Connection, days: int) -> list[dict[str, Any]]:
    """Per user, count how many requests match each automation signature."""
    # Build one aggregate query: for each signature, a FILTER count over the window.
    filters = ",\n".join(
        f"count(*) FILTER (WHERE l.request_payload::text ILIKE '%' || ${i + 2} || '%') AS sig_{i}"
        for i in range(len(SIGNATURES))
    )
    params = [days] + [sig for _, sig in SIGNATURES]
    rows = await conn.fetch(
        f"""
        WITH hits AS (
            SELECT l.user_id,
                   count(*) AS total_reqs,
                   {filters}
            FROM api_logs l
            WHERE l.timestamp > now() - make_interval(days => $1)
              AND l.user_id IS NOT NULL
            GROUP BY l.user_id
        )
        SELECT h.*, u.email, u.role, u.status, u.created_at
        FROM hits h
        LEFT JOIN users u ON u.id = h.user_id
        """,
        *params,
    )
    out: list[dict[str, Any]] = []
    for r in rows:
        matched = {}
        for i, (label, _) in enumerate(SIGNATURES):
            n = r[f"sig_{i}"]
            if n:
                matched[label] = n
        if matched:
            out.append(
                {
                    "user_id": r["user_id"],
                    "email": r["email"],
                    "role": r["role"],
                    "status": r["status"],
                    "created_at": r["created_at"],
                    "total_reqs": r["total_reqs"],
                    "matched": matched,
                    "loop_hits": sum(matched.values()),
                }
            )
    out.sort(key=lambda e: e["loop_hits"], reverse=True)
    return out


def _fmt_dt(v: Any) -> str:
    return v.isoformat(sep=" ", timespec="minutes") if v else "—"


def _print_report(rows: list[dict[str, Any]], days: int, min_hits: int) -> None:
    shown = [r for r in rows if r["loop_hits"] >= min_hits]
    print(
        f"Users showing automated-loop signatures over the last {days} day(s): "
        f"{len(shown)} (min_hits={min_hits})\n"
    )
    for r in shown:
        email = r["email"] or "<no account>"
        sigs = ", ".join(f"{k}x{v}" for k, v in sorted(r["matched"].items(), key=lambda kv: -kv[1]))
        print(f"  {email}  [{r['role'] or '?'}/{r['status'] or '?'}]")
        print(
            f"    {r['loop_hits']} loop-hits of {r['total_reqs']} reqs   created={_fmt_dt(r['created_at'])}"
        )
        print(f"    signatures: {sigs}")
        print(f"    (id={r['user_id']})")
        print()


async def main(days: int, min_hits: int, as_json: bool) -> int:
    """Scan for automated-loop signatures and print or emit the result."""
    pool = await asyncpg.create_pool(_dsn(), min_size=1, max_size=2)
    try:
        async with pool.acquire() as conn:
            rows = await _scan(conn, days)
    finally:
        await pool.close()

    if as_json:
        print(
            json.dumps({"days": days, "min_hits": min_hits, "users": rows}, indent=2, default=str)
        )
    else:
        _print_report(rows, days, min_hits)
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
        "--min-hits",
        type=int,
        default=1,
        help="Only show users with at least this many hits (default: 1)",
    )
    parser.add_argument(
        "--json", action="store_true", help="Emit JSON instead of a human-readable report"
    )
    parser.add_argument("--env-file", default=None, help="Path to .env (default: auto-detect)")
    args = parser.parse_args()
    _load_env(args.env_file)
    raise SystemExit(asyncio.run(main(args.days, args.min_hits, args.json)))


if __name__ == "__main__":
    cli()
