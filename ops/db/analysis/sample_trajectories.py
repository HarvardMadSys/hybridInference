r"""Sample the longest agent trajectories per (model, harness) from ``api_logs``.

A *trajectory* is one agent session: a time-ordered run of requests by a single
user against a single (model, agent) pair. Because ``session_id`` is only
populated for a minority of clients, trajectories are inferred by
**gap-sessionization** -- a new trajectory starts whenever a user is idle on a
(model, agent) pair for longer than ``--gap-min`` minutes.

For each (model, harness) pair with at least ``--min-trajs`` qualifying
trajectories (each at least ``--min-len`` requests), the ``--top-n`` longest are
written to ``<out-dir>/<model>__<agent>.json`` with per-request content
truncated so the files stay analyzable. ``_index.json`` and ``_summary.json``
hold the cross-pair manifest and quantitative summary.

Read-only. Connects to PostgreSQL via ``.env`` / ``DB_*`` env vars, like the
other live-DB analysis scripts in this directory.

Example::

    uv run python ops/db/analysis/sample_trajectories.py \\
        --top-n 20 --out-dir data/trajectories

The harness label is the prompt-derived ``metadata.agent`` (lowercased), e.g.
``opencode``, ``kilo``, ``pi``, ``zcode``, ``claude``, ``codex``, ``cline``,
``hermes``, ``openclaw``. Pass ``--agents`` to restrict to a subset.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics as st
from pathlib import Path
from typing import Any

import asyncpg
import dotenv

# Truncation caps (characters) so trajectory files stay readable.
CAP_LAST = 600
CAP_ASSIST = 1000
CAP_TOOLIN = 300
CAP_SYS = 2000


def _load_env(env_path: str | None) -> None:
    """Load DB_* settings from the first .env found (explicit path wins)."""
    for p in (
        env_path,
        os.environ.get("ENV_FILE"),
        "/srv/hybridInference/.env",
        str(Path(__file__).resolve().parents[3] / ".env"),
    ):
        if p and Path(p).exists():
            dotenv.load_dotenv(p, override=False)
            return


def _dsn() -> str:
    """Build a PostgreSQL DSN from DB_* environment variables."""
    return (
        f"postgresql://{os.environ['DB_USER']}:{os.environ.get('DB_PASSWORD', '')}"
        f"@{os.environ.get('DB_HOST', 'localhost')}:{os.environ.get('DB_PORT', '5432')}"
        f"/{os.environ.get('DB_NAME', 'freeinference_db')}"
    )


def _loads(x: Any) -> Any:
    """Best-effort JSON decode; pass through dict/list, return None on failure."""
    if isinstance(x, (dict, list)):
        return x
    if isinstance(x, str):
        try:
            return json.loads(x)
        except (json.JSONDecodeError, ValueError):
            return None
    return None


def _summarize_content(content: Any, cap: int) -> str:
    """Render Anthropic/OpenAI message content to a short text summary."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content[:cap]
    parts: list[str] = []
    if isinstance(content, list):
        for b in content:
            if isinstance(b, str):
                parts.append(b)
                continue
            if not isinstance(b, dict):
                continue
            t = b.get("type")
            if t == "text" or (t is None and "text" in b):
                parts.append(str(b.get("text") or ""))
            elif t == "tool_result":
                inner = b.get("content")
                txt = inner if isinstance(inner, str) else _summarize_content(inner, cap)
                parts.append(f"[tool_result{' err' if b.get('is_error') else ''}: {txt[:200]}]")
            elif t == "tool_use":
                parts.append(
                    f"[tool_use {b.get('name')}({json.dumps(b.get('input') or {})[:120]})]"
                )
            elif t in ("image", "image_url", "input_image"):
                parts.append("[image]")
            elif t == "thinking":
                parts.append(f"[thinking: {str(b.get('thinking') or '')[:120]}]")
    return (" ".join(p for p in parts if p))[:cap]


def _last_message(prompt: Any) -> tuple[str | None, str, int]:
    """Return (role, truncated text, message count) for the prompt's last turn."""
    msgs = _loads(prompt)
    if not isinstance(msgs, list) or not msgs:
        return None, "", 0
    last = msgs[-1]
    if not isinstance(last, dict):
        return None, str(last)[:CAP_LAST], len(msgs)
    return last.get("role"), _summarize_content(last.get("content"), CAP_LAST), len(msgs)


def _assistant_from_response(response: Any) -> tuple[str, list[dict[str, Any]], Any]:
    """Extract (text, tool_calls, stop_reason) from an Anthropic or OpenAI response."""
    r = _loads(response)
    if not isinstance(r, dict):
        return "", [], None
    text, tools = "", []
    stop = r.get("stop_reason") or r.get("finish_reason")
    if isinstance(r.get("content"), list):  # Anthropic shape
        chunks = []
        for b in r["content"]:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text":
                chunks.append(str(b.get("text") or ""))
            elif b.get("type") == "thinking":
                chunks.append(f"[thinking]{str(b.get('thinking') or '')[:200]}")
            elif b.get("type") == "tool_use":
                tools.append(
                    {"name": b.get("name"), "input": json.dumps(b.get("input") or {})[:CAP_TOOLIN]}
                )
        text = " ".join(chunks)[:CAP_ASSIST]
    elif isinstance(r.get("choices"), list) and r["choices"]:  # OpenAI shape
        choice = r["choices"][0] or {}
        msg = choice.get("message") or {}
        stop = choice.get("finish_reason") or stop
        text = str(msg.get("content") or "")[:CAP_ASSIST]
        for tc in msg.get("tool_calls") or []:
            fn = (tc or {}).get("function") or {}
            tools.append(
                {"name": fn.get("name"), "input": str(fn.get("arguments") or "")[:CAP_TOOLIN]}
            )
    return text, tools, stop


def _first_context(prompt: Any, request_payload: Any) -> tuple[str, list[str]]:
    """Return (system-prompt excerpt, offered tool names) from the first request."""
    rp = _loads(request_payload) or {}
    sys = rp.get("system")
    if sys is None:  # OpenAI surface keeps system as the first message
        for m in _loads(prompt) or []:
            if isinstance(m, dict) and m.get("role") == "system":
                sys = m.get("content")
                break
    names: list[str] = []
    for t in rp.get("tools") or []:
        if isinstance(t, dict):
            n = t.get("name") or ((t.get("function") or {}).get("name"))
            if n:
                names.append(n)
    return _summarize_content(sys, CAP_SYS), names[:80]


# SQL that gap-sessionizes api_logs and ranks trajectories within each (model,
# agent) pair. $1=agents filter (NULL = all), $2=gap minutes, $3=min length,
# $4=min trajectories/pair, $5=top-n per pair.
_SELECT_SQL = """
WITH base AS (
    SELECT timestamp, model_id, lower(metadata->>'agent') AS agent,
           metadata->>'user_id' AS uid
    FROM api_logs
    WHERE metadata->>'agent' IS NOT NULL
      AND metadata->>'user_id' IS NOT NULL
      AND model_id <> ''
      AND ($1::text[] IS NULL OR lower(metadata->>'agent') = ANY($1::text[]))
),
flagged AS (
    SELECT *, CASE
        WHEN timestamp - lag(timestamp) OVER w > make_interval(mins => $2)
             OR lag(timestamp) OVER w IS NULL THEN 1 ELSE 0 END AS nz
    FROM base
    WINDOW w AS (PARTITION BY uid, model_id, agent ORDER BY timestamp)
),
numbered AS (
    SELECT *, sum(nz) OVER (PARTITION BY uid, model_id, agent ORDER BY timestamp) AS sn
    FROM flagged
),
sess AS (
    SELECT model_id, agent, uid, sn, count(*) AS n,
           min(timestamp) AS t0, max(timestamp) AS t1
    FROM numbered GROUP BY model_id, agent, uid, sn
    HAVING count(*) >= $3
),
pair AS (SELECT model_id, agent, count(*) AS pair_trajs FROM sess GROUP BY model_id, agent),
ranked AS (
    SELECT s.*, p.pair_trajs,
           row_number() OVER (PARTITION BY s.model_id, s.agent ORDER BY s.n DESC, s.t0) AS rnk
    FROM sess s JOIN pair p USING (model_id, agent)
)
SELECT model_id, agent, uid, n, t0, t1, pair_trajs
FROM ranked WHERE pair_trajs >= $4 AND rnk <= $5
ORDER BY model_id, agent, n DESC
"""


async def _fetch_trajectory(conn: asyncpg.Connection, t: asyncpg.Record) -> dict[str, Any]:
    """Fetch and shape one trajectory's requests (truncated) plus its stats."""
    rows = await conn.fetch(
        "SELECT timestamp, status_code, latency_ms, ttft_ms, prompt_tokens, completion_tokens, "
        "error, prompt, response, request_payload, metadata->>'user_agent' ua "
        "FROM api_logs WHERE metadata->>'user_id'=$1 AND model_id=$2 "
        "AND lower(metadata->>'agent')=$3 AND timestamp BETWEEN $4 AND $5 ORDER BY timestamp ASC",
        t["uid"],
        t["model_id"],
        t["agent"],
        t["t0"],
        t["t1"],
    )
    sys_txt, tool_names = _first_context(rows[0]["prompt"], rows[0]["request_payload"])
    recs, tools_used, ttfts, n_err, n_5xx = [], set(), [], 0, 0
    for i, r in enumerate(rows):
        role, lastmsg, nmsg = _last_message(r["prompt"])
        atext, tcalls, stop = _assistant_from_response(r["response"])
        for tc in tcalls:
            if tc.get("name"):
                tools_used.add(tc["name"])
        sc = r["status_code"] or 0
        n_err += sc >= 400
        n_5xx += sc >= 500
        if r["ttft_ms"] is not None:
            ttfts.append(r["ttft_ms"])
        recs.append(
            {
                "seq": i,
                "ts": r["timestamp"].isoformat(),
                "status": sc,
                "latency_ms": r["latency_ms"],
                "ttft_ms": r["ttft_ms"],
                "prompt_tokens": r["prompt_tokens"],
                "completion_tokens": r["completion_tokens"],
                "n_messages": nmsg,
                "last_role": role,
                "last_msg": lastmsg,
                "assistant_text": atext,
                "tool_calls": tcalls,
                "stop": stop,
                "error": (r["error"] or "")[:200] or None,
            }
        )
    dur = (rows[-1]["timestamp"] - rows[0]["timestamp"]).total_seconds()
    return {
        "user_id": t["uid"],
        "n_requests": len(rows),
        "user_agent": rows[0]["ua"],
        "start": rows[0]["timestamp"].isoformat(),
        "duration_s": round(dur, 1),
        "system_excerpt": sys_txt,
        "tools_offered": tool_names,
        "stats": {
            "total_prompt_tokens": sum((r["prompt_tokens"] or 0) for r in rows),
            "total_completion_tokens": sum((r["completion_tokens"] or 0) for r in rows),
            "n_errors": n_err,
            "n_5xx": n_5xx,
            "max_n_messages": max((rec["n_messages"] for rec in recs), default=0),
            "distinct_tools_used": sorted(tools_used),
            "median_ttft_ms": (sorted(ttfts)[len(ttfts) // 2] if ttfts else None),
        },
        "requests": recs,
    }


async def main(args: argparse.Namespace) -> int:
    """Run the extraction and write per-pair trajectory files plus a summary."""
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    agents = [a.strip().lower() for a in args.agents.split(",")] if args.agents else None

    conn = await asyncpg.connect(_dsn())
    try:
        selected = await conn.fetch(
            _SELECT_SQL, agents, args.gap_min, args.min_len, args.min_trajs, args.top_n
        )
        bypair: dict[tuple[str, str], list[asyncpg.Record]] = {}
        for r in selected:
            bypair.setdefault((r["model_id"], r["agent"]), []).append(r)

        index, summary = [], []
        for (model, agent), trows in sorted(bypair.items(), key=lambda kv: -len(kv[1])):
            trajs = [await _fetch_trajectory(conn, t) for t in trows]
            fname = f"{model}__{agent}.json".replace("/", "_")
            (out / fname).write_text(
                json.dumps(
                    {
                        "model": model,
                        "harness": agent,
                        "method": f"gap-sessionized@{args.gap_min}min",
                        "n_trajectories": len(trajs),
                        "trajectories": trajs,
                    },
                    indent=1,
                )
            )
            lens = [t["n_requests"] for t in trajs]
            reqs = [r for t in trajs for r in t["requests"]]
            n = len(reqs)
            index.append(
                {
                    "model": model,
                    "harness": agent,
                    "file": fname,
                    "n_trajectories": len(trajs),
                    "pair_trajs": trows[0]["pair_trajs"],
                    "lengths": lens,
                }
            )
            summary.append(
                {
                    "pair": f"{model} / {agent}",
                    "n_traj": len(trajs),
                    "req_min_med_max": f"{min(lens)}/{int(st.median(lens))}/{max(lens)}",
                    "avg_prompt_tok": round(st.mean([r["prompt_tokens"] or 0 for r in reqs]))
                    if n
                    else 0,
                    "avg_compl_tok": round(st.mean([r["completion_tokens"] or 0 for r in reqs]))
                    if n
                    else 0,
                    "err_pct": round(100 * sum(1 for r in reqs if r["status"] >= 400) / n, 1)
                    if n
                    else 0,
                    "toolcall_pct": round(100 * sum(1 for r in reqs if r["tool_calls"]) / n, 1)
                    if n
                    else 0,
                }
            )
            print(
                f"  {fname}: {len(trajs)} trajectories (pair has {trows[0]['pair_trajs']}), "
                f"lengths={lens}"
            )

        (out / "_index.json").write_text(json.dumps(index, indent=1))
        (out / "_summary.json").write_text(json.dumps(summary, indent=1))
        print(
            f"\nWrote {len(index)} (model, harness) files, "
            f"{sum(x['n_trajectories'] for x in index)} trajectories to {out}"
        )
    finally:
        await conn.close()
    return 0


def cli() -> None:
    """Parse arguments and run the trajectory sampler."""
    ap = argparse.ArgumentParser(
        description="Sample longest agent trajectories per (model, harness)."
    )
    ap.add_argument("--out-dir", default="data/trajectories", help="Output directory.")
    ap.add_argument("--top-n", type=int, default=20, help="Longest trajectories per pair.")
    ap.add_argument("--min-len", type=int, default=5, help="Min requests for a trajectory.")
    ap.add_argument(
        "--min-trajs",
        type=int,
        default=5,
        help="Min qualifying trajectories for a pair to be included.",
    )
    ap.add_argument(
        "--gap-min", type=int, default=30, help="Idle minutes that start a new trajectory."
    )
    ap.add_argument(
        "--agents", default=None, help="Comma-separated agent labels to restrict to (default: all)."
    )
    ap.add_argument("--env-file", default=None, help="Path to .env (default: auto-detect).")
    args = ap.parse_args()
    _load_env(args.env_file)
    if not os.environ.get("DB_USER"):
        raise SystemExit("ERROR: DB_USER not set; load the .env file or set DB_* env vars.")
    raise SystemExit(asyncio.run(main(args)))


if __name__ == "__main__":
    cli()
