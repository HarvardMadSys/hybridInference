"""Compare two (or more) users side by side to assess whether they are the same person.

Given a set of emails, this resolves each to its ``users`` row and reports, per user:
account metadata (role, status, signup reason, created/last-login), API-key activity,
and aggregate ``api_logs`` usage. It then computes cross-user *overlap* signals --
shared login IPs and user agents from ``login_events`` -- which are the strongest
evidence that two accounts belong to the same operator.

Usage:
    python ops/db/analysis/compare_users.py shark.pd@gmail.com shark3d.pd@gmail.com
    python ops/db/analysis/compare_users.py a@x.com b@x.com --json
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


async def _profile(conn: asyncpg.Connection, email: str) -> dict[str, Any]:
    """Build a single user's profile across users / api_keys / login_events / api_logs."""
    norm = email.strip().lower()
    user = await conn.fetchrow("SELECT * FROM users WHERE lower(trim(email)) = $1", norm)
    if user is None:
        return {"email": email, "found": False}

    uid = user["id"]

    keys = await conn.fetch(
        "SELECT key_prefix, status, created_at, last_used_at, "
        "quota_daily_cost_usd, quota_monthly_cost_usd "
        "FROM api_keys WHERE user_id = $1 ORDER BY created_at",
        uid,
    )

    logins = await conn.fetchrow(
        "SELECT count(*) AS n, "
        "count(*) FILTER (WHERE outcome = 'success') AS ok, "
        "count(*) FILTER (WHERE outcome = 'failure') AS fail, "
        "min(created_at) AS first, max(created_at) AS last, "
        "count(DISTINCT ip) AS distinct_ips, "
        "count(DISTINCT user_agent) AS distinct_uas "
        "FROM login_events WHERE user_id = $1",
        uid,
    )

    ips = await conn.fetch(
        "SELECT ip, count(*) AS n, max(created_at) AS last "
        "FROM login_events WHERE user_id = $1 AND ip IS NOT NULL "
        "GROUP BY ip ORDER BY n DESC",
        uid,
    )

    usage = await conn.fetchrow(
        "SELECT count(*) AS requests, "
        "count(DISTINCT model_id) AS distinct_models, "
        "min(timestamp) AS first, max(timestamp) AS last, "
        "sum(total_tokens) AS total_tokens, "
        "count(*) FILTER (WHERE status_code >= 400) AS errors "
        "FROM api_logs WHERE user_id = $1",
        uid,
    )

    top_models = await conn.fetch(
        "SELECT model_id, count(*) AS n FROM api_logs WHERE user_id = $1 "
        "GROUP BY model_id ORDER BY n DESC LIMIT 5",
        uid,
    )

    return {
        "email": email,
        "found": True,
        "user": dict(user),
        "api_keys": [dict(k) for k in keys],
        "logins": dict(logins) if logins else {},
        "ips": [dict(r) for r in ips],
        "usage": dict(usage) if usage else {},
        "top_models": [dict(m) for m in top_models],
    }


def _ip_set(profile: dict[str, Any]) -> set[str]:
    return {r["ip"] for r in profile.get("ips", []) if r.get("ip")}


async def _ua_set(conn: asyncpg.Connection, uid: str) -> set[str]:
    rows = await conn.fetch(
        "SELECT DISTINCT user_agent FROM login_events "
        "WHERE user_id = $1 AND user_agent IS NOT NULL",
        uid,
    )
    return {r["user_agent"] for r in rows}


def _fmt_dt(v: Any) -> str:
    return v.isoformat(sep=" ", timespec="seconds") if v else "—"


def _print_report(profiles: list[dict[str, Any]], overlaps: dict[str, Any]) -> None:
    for p in profiles:
        print("=" * 78)
        if not p["found"]:
            print(f"USER: {p['email']}  —  NOT FOUND")
            print()
            continue
        u = p["user"]
        lg = p["logins"]
        us = p["usage"]
        print(f"USER: {p['email']}   (id={u['id']})")
        print(
            f"  name={u.get('user_name')!r}  role={u['role']}  status={u['status']}  "
            f"email_verified={u['email_verified']}"
        )
        print(
            f"  created_at={_fmt_dt(u['created_at'])}  last_login_at={_fmt_dt(u.get('last_login_at'))}"
        )
        if u.get("signup_reason"):
            print(f"  signup_reason={u['signup_reason']!r}")
        if u.get("max_concurrent_requests") is not None:
            print(f"  max_concurrent_requests={u['max_concurrent_requests']}")
        print(
            f"  api_keys: {len(p['api_keys'])} "
            + ", ".join(f"{k['key_prefix']}…({k['status']})" for k in p["api_keys"])
        )
        print(
            f"  logins: {lg.get('n', 0)} total "
            f"({lg.get('ok', 0)} ok / {lg.get('fail', 0)} fail), "
            f"{lg.get('distinct_ips', 0)} distinct IPs, {lg.get('distinct_uas', 0)} distinct UAs, "
            f"first={_fmt_dt(lg.get('first'))} last={_fmt_dt(lg.get('last'))}"
        )
        print(
            f"  api_logs: {us.get('requests', 0)} requests, "
            f"{us.get('distinct_models', 0)} models, "
            f"{us.get('total_tokens') or 0} tokens, {us.get('errors', 0)} errors, "
            f"first={_fmt_dt(us.get('first'))} last={_fmt_dt(us.get('last'))}"
        )
        if p["top_models"]:
            print(
                "  top models: " + ", ".join(f"{m['model_id']}({m['n']})" for m in p["top_models"])
            )
        if p["ips"]:
            print("  login IPs: " + ", ".join(f"{r['ip']}({r['n']})" for r in p["ips"][:8]))
        print()

    print("=" * 78)
    print("OVERLAP ANALYSIS")
    print("=" * 78)
    found = [p for p in profiles if p["found"]]
    if len(found) < 2:
        print("  Need at least two existing users to compare.")
        return
    print(
        f"  Shared login IPs ({len(overlaps['shared_ips'])}): "
        + (", ".join(sorted(overlaps["shared_ips"])) or "none")
    )
    print(f"  Shared user agents ({len(overlaps['shared_uas'])}):")
    for ua in sorted(overlaps["shared_uas"]):
        print(f"    - {ua}")
    if not overlaps["shared_uas"]:
        print("    none")
    print()
    print(f"  VERDICT: {overlaps['verdict']}")


async def main(emails: list[str], as_json: bool) -> int:
    """Profile each user and print or emit the comparison."""
    pool = await asyncpg.create_pool(_dsn(), min_size=1, max_size=2)
    try:
        async with pool.acquire() as conn:
            profiles = [await _profile(conn, e) for e in emails]
            ua_by_email = {}
            for p in profiles:
                if p["found"]:
                    ua_by_email[p["email"]] = await _ua_set(conn, p["user"]["id"])
    finally:
        await pool.close()

    found = [p for p in profiles if p["found"]]
    shared_ips: set[str] = set()
    shared_uas: set[str] = set()
    if len(found) >= 2:
        ip_sets = [_ip_set(p) for p in found]
        shared_ips = set.intersection(*ip_sets) if ip_sets else set()
        ua_sets = [ua_by_email[p["email"]] for p in found]
        shared_uas = set.intersection(*ua_sets) if ua_sets else set()

    if shared_ips and shared_uas:
        verdict = "STRONG — shared IP(s) AND shared user agent(s)."
    elif shared_ips:
        verdict = "MODERATE — shared login IP(s); user agents differ."
    elif shared_uas:
        verdict = "WEAK — shared user agent(s) only (common for default clients)."
    elif len(found) >= 2:
        verdict = "NONE — no shared IPs or user agents in login history."
    else:
        verdict = "N/A — fewer than two existing accounts."

    overlaps = {"shared_ips": shared_ips, "shared_uas": shared_uas, "verdict": verdict}

    if as_json:
        print(
            json.dumps(
                {
                    "profiles": profiles,
                    "overlap": {
                        "shared_ips": sorted(shared_ips),
                        "shared_uas": sorted(shared_uas),
                        "verdict": verdict,
                    },
                },
                indent=2,
                default=str,
            )
        )
    else:
        _print_report(profiles, overlaps)
    return 0


def cli() -> None:
    """Parse CLI args, load env, and run the comparison."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("emails", nargs="+", help="Two or more emails to compare")
    parser.add_argument(
        "--json", action="store_true", help="Emit JSON instead of a human-readable report"
    )
    parser.add_argument("--env-file", default=None, help="Path to .env (default: auto-detect)")
    args = parser.parse_args()
    _load_env(args.env_file)
    raise SystemExit(asyncio.run(main(args.emails, args.json)))


if __name__ == "__main__":
    cli()
