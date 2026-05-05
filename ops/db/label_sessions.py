from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import asyncpg


@dataclass(frozen=True)
class InferredSession:
    row_id: int
    session_id: str
    user_id: str
    user_agent: str
    model_id: str
    timestamp: datetime


def _parse_ts(val: str | datetime) -> datetime:
    if isinstance(val, datetime):
        return val if val.tzinfo else val.replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(val.replace("Z", "+00:00"))


def _group_hash(user_id: str, user_agent: str) -> str:
    raw = f"{user_id}\0{user_agent}"
    return hashlib.sha256(raw.encode()).hexdigest()[:8]


def assign_sessions(
    rows: list[dict[str, Any]],
    gap_minutes: float = 10.0,
) -> list[InferredSession]:
    if not rows:
        return []

    gap_threshold = timedelta(minutes=gap_minutes)
    parsed: list[tuple[datetime, dict[str, Any]]] = []
    for r in rows:
        parsed.append((_parse_ts(r["timestamp"]), r))

    parsed.sort(
        key=lambda p: (p[1].get("user_id", ""), p[1].get("user_agent", ""), p[0])
    )

    results: list[InferredSession] = []
    counters: dict[str, int] = {}
    prev_key: tuple[str, str] | None = None
    prev_ts: datetime | None = None

    for ts, row in parsed:
        uid = row.get("user_id") or ""
        ua = row.get("user_agent") or ""
        key = (uid, ua)
        gh = _group_hash(uid, ua)

        if key != prev_key or prev_ts is None or (ts - prev_ts) >= gap_threshold:
            counters[gh] = counters.get(gh, 0) + 1
            prev_ts = ts

        prev_key = key
        prev_ts = ts

        seq = counters.get(gh, 1)
        sid = f"sess_{gh}_{seq:03d}"

        results.append(
            InferredSession(
                row_id=row["id"],
                session_id=sid,
                user_id=uid,
                user_agent=ua,
                model_id=row.get("model_id", ""),
                timestamp=ts,
            )
        )

    return results


async def _fetch_logs(pool: asyncpg.Pool) -> list[dict[str, Any]]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                id, timestamp, user_id, model_id, provider,
                metadata->>'user_agent' AS user_agent,
                metadata->>'surface' AS surface,
                metadata->>'alias_input' AS alias_input,
                status_code, latency_ms, ttft_ms,
                prompt_tokens, completion_tokens, reasoning_tokens,
                total_tokens, cost_usd, stream, error
            FROM api_logs
            ORDER BY id
            """
        )
    return [dict(r) for r in rows]


def _ts_iso(val: Any) -> str:
    if isinstance(val, datetime):
        return val.isoformat()
    return str(val)


def _export_jsonl(
    rows: list[dict[str, Any]],
    sessions: list[InferredSession],
    output_path: str,
) -> int:
    session_map: dict[int, str] = {s.row_id: s.session_id for s in sessions}
    count = 0
    with open(output_path, "w") as f:
        for row in rows:
            row["session_id"] = session_map.get(row["id"])
            row["timestamp"] = _ts_iso(row.get("timestamp"))
            for k, v in row.items():
                if isinstance(v, datetime):
                    row[k] = v.isoformat()
                elif hasattr(v, "__float__") and not isinstance(v, (int, float, str, type(None))):
                    row[k] = float(v)
            f.write(json.dumps(row, default=str) + "\n")
            count += 1
    return count


def _print_summary(sessions: list[InferredSession]) -> None:
    total = len(sessions)
    unique_sessions = len({s.session_id for s in sessions})
    unique_users = len({s.user_id for s in sessions})
    unique_agents = len({s.user_agent for s in sessions})

    sess_counts: dict[str, int] = {}
    for s in sessions:
        sess_counts[s.session_id] = sess_counts.get(s.session_id, 0) + 1

    sizes = list(sess_counts.values())

    print(f"\n{'=' * 60}")
    print(f"Session labeling summary")
    print(f"{'=' * 60}")
    print(f"  Total requests:      {total}")
    print(f"  Unique sessions:     {unique_sessions}")
    print(f"  Unique users:        {unique_users}")
    print(f"  Unique user agents:  {unique_agents}")
    if sizes:
        print(f"  Session size min:    {min(sizes)}")
        print(f"  Session size max:    {max(sizes)}")
        print(f"  Session size avg:    {sum(sizes) / len(sizes):.1f}")
    print(f"{'=' * 60}\n")


async def main(
    output_path: str = "session_labeled_requests.jsonl",
    gap_minutes: float = 10.0,
) -> None:
    dsn = (
        f"postgresql://{os.environ.get('DB_USER', 'postgres')}"
        f":{os.environ.get('DB_PASSWORD', '')}"
        f"@{os.environ.get('DB_HOST', 'localhost')}"
        f":{os.environ.get('DB_PORT', '5432')}"
        f"/{os.environ.get('DB_NAME', 'freeinference_db')}"
    )
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
    try:
        rows = await _fetch_logs(pool)
        if not rows:
            print("No rows found in api_logs.")
            return

        sessions = assign_sessions(rows, gap_minutes=gap_minutes)
        _print_summary(sessions)

        count = _export_jsonl(rows, sessions, output_path)
        print(f"Exported {count} labeled requests to {output_path}")
    finally:
        await pool.close()


def cli() -> None:
    parser = argparse.ArgumentParser(
        description="Label api_logs rows with inferred session IDs and export to JSONL"
    )
    parser.add_argument(
        "-o",
        "--output",
        default="session_labeled_requests.jsonl",
        help="Output JSONL file path (default: session_labeled_requests.jsonl)",
    )
    parser.add_argument(
        "--gap-minutes",
        type=float,
        default=10.0,
        help="Inactivity gap in minutes to split sessions (default: 10)",
    )
    args = parser.parse_args()
    asyncio.run(main(output_path=args.output, gap_minutes=args.gap_minutes))


if __name__ == "__main__":
    cli()
