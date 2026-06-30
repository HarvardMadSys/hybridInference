r"""Sample the longest agent trajectories per (model, harness) from ``api_logs``.

A *trajectory* is one agent session: a run of a user's requests against a single
(model, agent) pair that share conversation context. ``session_id`` is only set
by a minority of clients, so trajectories are inferred by **context overlap**:
each request is fingerprinted by the content hashes of its first two and last
five messages, and a request joins a recent open session when its fingerprint
shares any hash with that session's recent requests. The anchor (first messages)
links ordinary turns; the retained tail links across **compaction** -- when an
agent replaces history with a summary it keeps the most recent messages, so the
overlap (and thus the session) survives. A short-gap shrinking-context bridge
covers summary-only compaction that retains nothing verbatim.

The script prints (live) and saves per-(model, agent) statistics over *all*
detected trajectories (counts, length/duration/token distributions, error and
tool-call rates), then writes the ``--top-n`` longest trajectories per
qualifying pair to ``<out-dir>/<model>__<agent>.json``. The full conversation
history is kept compactly as per-request message deltas (only the messages added
since the previous request), so compaction stays visible without storing the
repeated prefix on every request.

Read-only. Connects to PostgreSQL via ``.env`` / ``DB_*`` env vars, like the
other live-DB analysis scripts in this directory.

Example::

    uv run python ops/db/analysis/sample_trajectories.py --top-n 20

Harness = the prompt-derived ``metadata.agent`` (lowercased), e.g. ``opencode``,
``kilo``, ``pi``, ``zcode``, ``claude``, ``codex``, ``cline``, ``hermes``,
``openclaw``. Pass ``--agents`` to restrict to a subset.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import statistics as st
import sys
from collections import defaultdict, deque
from contextlib import suppress
from datetime import timedelta
from pathlib import Path
from typing import Any

import asyncpg
import dotenv

# Truncation caps (characters) so trajectory files stay readable.
CAP_HIST = 500
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


def _messages_and_hashes(prompt: Any) -> tuple[list[Any], list[str]]:
    """Return (messages, per-message content hashes) for a prompt."""
    msgs = _loads(prompt)
    if not isinstance(msgs, list):
        return [], []
    hashes = [
        hashlib.md5(json.dumps(m, sort_keys=True, default=str).encode()).hexdigest() for m in msgs
    ]
    return msgs, hashes


def _common_prefix_len(a: list[str], b: list[str]) -> int:
    """Length of the shared leading run between two hash lists."""
    n = 0
    for x, y in zip(a, b, strict=False):
        if x != y:
            break
        n += 1
    return n


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


# Per-request fingerprint + telemetry, computed server-side so only small values
# transfer. fp = content hashes of the first two and last five messages (the
# anchor + recent tail). $1 = agents filter (NULL = all agents).
_FP_SQL = """
WITH p AS (
    SELECT request_id, timestamp, model_id, lower(metadata->>'agent') AS agent,
           metadata->>'user_id' AS uid, status_code, ttft_ms,
           prompt_tokens, completion_tokens,
           (response LIKE '%"tool_use"%' OR response LIKE '%"tool_calls"%') AS has_tool,
           prompt::jsonb AS pj
    FROM api_logs
    WHERE metadata->>'agent' IS NOT NULL AND metadata->>'user_id' IS NOT NULL
      AND model_id <> '' AND prompt IS NOT NULL
      AND ($1::text[] IS NULL OR lower(metadata->>'agent') = ANY($1::text[]))
)
SELECT request_id, timestamp, model_id, agent, uid, status_code, ttft_ms,
       prompt_tokens, completion_tokens, has_tool,
       CASE WHEN jsonb_typeof(pj) = 'array' THEN jsonb_array_length(pj) ELSE 0 END AS nmsg,
       array_remove(ARRAY[
           md5((pj -> 0)::text), md5((pj -> 1)::text), md5((pj -> -1)::text),
           md5((pj -> -2)::text), md5((pj -> -3)::text), md5((pj -> -4)::text),
           md5((pj -> -5)::text)], NULL) AS fp
FROM p
ORDER BY uid, model_id, agent, timestamp
"""


def _sessionize(
    rows: list[asyncpg.Record], roll: int, bridge_min: int, close_hours: int
) -> tuple[list[dict[str, Any]], int]:
    """Group fingerprinted requests into context-overlapping trajectories.

    Returns (trajectories, n_compaction_bridges). Each trajectory is a dict with
    ``model``/``agent``/``uid`` and an ordered ``reqs`` list of the source rows.
    """
    bridge = timedelta(minutes=bridge_min)
    close = timedelta(hours=close_hours)
    open_by_key: dict[tuple, list[dict[str, Any]]] = defaultdict(list)
    trajectories: list[dict[str, Any]] = []
    n_bridge = 0

    for row in rows:
        key = (row["uid"], row["model_id"], row["agent"])
        fp = set(row["fp"] or [])
        ts, nmsg = row["timestamp"], row["nmsg"]
        opens = [s for s in open_by_key[key] if ts - s["last_ts"] <= close]
        open_by_key[key] = opens

        best, best_score = None, 0
        for s in opens:
            score = len(fp & s["recent_union"])
            if score > best_score:
                best, best_score = s, score

        if best is not None and best_score >= 1:
            sess = best
        elif (
            best is not None
            and ts - best["last_ts"] <= bridge
            and best["last_nmsg"]
            and nmsg < best["last_nmsg"]
        ):
            sess = best  # summary-only compaction: context shrank within a short gap
            n_bridge += 1
        else:
            sess = {
                "model": row["model_id"],
                "agent": row["agent"],
                "uid": row["uid"],
                "reqs": [],
                "recent_fps": deque(maxlen=roll),
                "recent_union": set(),
                "last_ts": ts,
                "last_nmsg": nmsg,
            }
            opens.append(sess)
            trajectories.append(sess)

        sess["reqs"].append(row)
        sess["recent_fps"].append(fp)
        sess["recent_union"] = set().union(*sess["recent_fps"])
        sess["last_ts"] = ts
        sess["last_nmsg"] = nmsg

    return trajectories, n_bridge


def _pair_stats(trajectories: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Compute per-(model, agent) statistics over all detected trajectories."""
    bypair: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for t in trajectories:
        bypair[(t["model"], t["agent"])].append(t)

    out = []
    for (model, agent), trajs in bypair.items():
        lens = [len(t["reqs"]) for t in trajs]
        reqs = [r for t in trajs for r in t["reqs"]]
        n = len(reqs)
        durs = [
            (t["reqs"][-1]["timestamp"] - t["reqs"][0]["timestamp"]).total_seconds() / 60
            for t in trajs
        ]
        ttfts = [r["ttft_ms"] for r in reqs if r["ttft_ms"] is not None]
        out.append(
            {
                "model": model,
                "harness": agent,
                "n_trajectories": len(trajs),
                "n_requests": n,
                "n_users": len({t["uid"] for t in trajs}),
                "len_p50": int(st.median(lens)),
                "len_p90": sorted(lens)[int(0.9 * (len(lens) - 1))],
                "len_max": max(lens),
                "dur_med_min": round(st.median(durs), 1),
                "avg_prompt_tok": round(st.mean([r["prompt_tokens"] or 0 for r in reqs])),
                "avg_compl_tok": round(st.mean([r["completion_tokens"] or 0 for r in reqs])),
                "err_pct": round(
                    100 * sum(1 for r in reqs if (r["status_code"] or 0) >= 400) / n, 1
                ),
                "toolcall_pct": round(100 * sum(1 for r in reqs if r["has_tool"]) / n, 1),
                "median_ttft_ms": int(st.median(ttfts)) if ttfts else None,
            }
        )
    return sorted(out, key=lambda r: -r["n_trajectories"])


def _print_stats(stats: list[dict[str, Any]], n_bridge: int) -> str:
    """Print the stats table to stdout and return it as a markdown string."""
    cols = [
        "model",
        "harness",
        "n_trajectories",
        "n_requests",
        "n_users",
        "len_p50",
        "len_p90",
        "len_max",
        "dur_med_min",
        "avg_prompt_tok",
        "avg_compl_tok",
        "err_pct",
        "toolcall_pct",
        "median_ttft_ms",
    ]
    w = {c: max(len(c), *(len(str(r[c])) for r in stats)) for c in cols} if stats else {}
    print(
        f"\n=== per (model, harness) trajectory statistics "
        f"({len(stats)} pairs; {n_bridge} compaction bridges) ==="
    )
    header = " | ".join(c.ljust(w[c]) for c in cols)
    print(header)
    print("-+-".join("-" * w[c] for c in cols))
    for r in stats:
        print(" | ".join(str(r[c]).ljust(w[c]) for c in cols))
    md = ["| " + " | ".join(cols) + " |", "|" + "|".join("---" for _ in cols) + "|"]
    md += ["| " + " | ".join(str(r[c]) for c in cols) + " |" for r in stats]
    return "\n".join(md) + "\n"


async def _build_pair_file(
    conn: asyncpg.Connection, model: str, agent: str, trajs: list[dict[str, Any]], out: Path
) -> list[int]:
    """Fetch the chosen trajectories and write the pair file, keeping full history.

    History is kept compactly as per-request message *deltas*: the first request
    stores its whole message list, and each later request stores only the
    messages added since the previous one (assistant turns excluded -- they are
    captured in ``assistant_text``/``tool_calls``). When the leading messages
    diverge (history compaction), ``reset`` is set and the delta carries the new
    state (summary + retained tail), so the conversation stays reconstructable.
    """
    out_trajs = []
    for t in trajs:
        ids = [r["request_id"] for r in t["reqs"]]
        rows = await conn.fetch(
            "SELECT request_id, timestamp, status_code, ttft_ms, prompt_tokens, completion_tokens, "
            "prompt, response, request_payload, metadata->>'user_agent' ua "
            "FROM api_logs WHERE request_id = ANY($1::text[]) ORDER BY timestamp ASC",
            ids,
        )
        if not rows:
            continue
        sys_txt, tool_names = _first_context(rows[0]["prompt"], rows[0]["request_payload"])
        recs, tools_used, prev_hashes = [], set(), []
        for i, r in enumerate(rows):
            msgs, hashes = _messages_and_hashes(r["prompt"])
            shared = _common_prefix_len(prev_hashes, hashes)
            reset = shared < len(prev_hashes)  # leading messages diverged -> compaction
            new_messages = [
                {
                    "role": m.get("role") if isinstance(m, dict) else None,
                    "content": _summarize_content(
                        m.get("content") if isinstance(m, dict) else m, CAP_HIST
                    ),
                }
                for m in msgs[shared:]
                if not (isinstance(m, dict) and m.get("role") == "assistant")
            ]
            atext, tcalls, stop = _assistant_from_response(r["response"])
            for tc in tcalls:
                if tc.get("name"):
                    tools_used.add(tc["name"])
            recs.append(
                {
                    "seq": i,
                    "ts": r["timestamp"].isoformat(),
                    "status": r["status_code"] or 0,
                    "ttft_ms": r["ttft_ms"],
                    "prompt_tokens": r["prompt_tokens"],
                    "completion_tokens": r["completion_tokens"],
                    "n_messages": len(msgs),
                    "reset": reset,
                    "new_messages": new_messages,
                    "assistant_text": atext,
                    "tool_calls": tcalls,
                    "stop": stop,
                }
            )
            prev_hashes = hashes
        dur = (rows[-1]["timestamp"] - rows[0]["timestamp"]).total_seconds()
        out_trajs.append(
            {
                "user_id": t["uid"],
                "n_requests": len(rows),
                "user_agent": rows[0]["ua"],
                "start": rows[0]["timestamp"].isoformat(),
                "duration_s": round(dur, 1),
                "system_excerpt": sys_txt,
                "tools_offered": tool_names,
                "distinct_tools_used": sorted(tools_used),
                "requests": recs,
            }
        )
    fname = f"{model}__{agent}.json".replace("/", "_")
    (out / fname).write_text(
        json.dumps(
            {
                "model": model,
                "harness": agent,
                "method": "context-overlap",
                "n_trajectories": len(out_trajs),
                "trajectories": out_trajs,
            },
            indent=1,
        )
    )
    return [t["n_requests"] for t in out_trajs]


async def main(args: argparse.Namespace) -> int:
    """Run sessionization, emit statistics, and write sampled trajectory files."""
    # Print progress/stats live even when stdout is piped (block-buffered).
    with suppress(Exception):
        sys.stdout.reconfigure(line_buffering=True)
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    agents = [a.strip().lower() for a in args.agents.split(",")] if args.agents else None

    conn = await asyncpg.connect(_dsn())
    try:
        print("Fetching request fingerprints (server-side)...")
        rows = await conn.fetch(_FP_SQL, agents)
        print(f"  {len(rows)} agent-labeled requests")

        trajectories, n_bridge = _sessionize(rows, args.roll, args.bridge_min, args.close_hours)
        trajectories = [t for t in trajectories if len(t["reqs"]) >= args.min_len]
        print(f"  {len(trajectories)} trajectories (>= {args.min_len} requests)")

        stats = _pair_stats(trajectories)
        md = _print_stats(stats, n_bridge)
        (out / "_stats.json").write_text(json.dumps(stats, indent=1))
        (out / "_stats.md").write_text(md)

        bypair: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for t in trajectories:
            bypair[(t["model"], t["agent"])].append(t)

        index = []
        print("\nWriting sampled trajectory files...")
        for (model, agent), trajs in sorted(bypair.items(), key=lambda kv: -len(kv[1])):
            if len(trajs) < args.min_trajs:
                continue
            chosen = sorted(trajs, key=lambda t: -len(t["reqs"]))[: args.top_n]
            lens = await _build_pair_file(conn, model, agent, chosen, out)
            index.append(
                {
                    "model": model,
                    "harness": agent,
                    "file": f"{model}__{agent}.json".replace("/", "_"),
                    "pair_trajs": len(trajs),
                    "n_sampled": len(lens),
                    "lengths": lens,
                }
            )
            print(f"  {model}__{agent}.json: {len(lens)}/{len(trajs)} sampled, lengths={lens}")

        (out / "_index.json").write_text(json.dumps(index, indent=1))
        print(
            f"\nWrote stats for {len(stats)} pairs; sampled {len(index)} pairs "
            f"({sum(x['n_sampled'] for x in index)} trajectories) to {out}"
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
    ap.add_argument("--top-n", type=int, default=20, help="Longest trajectories sampled per pair.")
    ap.add_argument("--min-len", type=int, default=5, help="Min requests for a trajectory.")
    ap.add_argument(
        "--min-trajs",
        type=int,
        default=5,
        help="Min trajectories for a pair to be sampled to file.",
    )
    ap.add_argument(
        "--roll",
        type=int,
        default=3,
        help="Recent requests whose fingerprints define a session's overlap window.",
    )
    ap.add_argument(
        "--bridge-min",
        type=int,
        default=3,
        help="Max idle minutes for the summary-only compaction bridge.",
    )
    ap.add_argument(
        "--close-hours",
        type=int,
        default=3,
        help="Idle hours after which an open session is closed.",
    )
    ap.add_argument("--agents", default=None, help="Comma-separated agent labels (default: all).")
    ap.add_argument("--env-file", default=None, help="Path to .env (default: auto-detect).")
    args = ap.parse_args()
    _load_env(args.env_file)
    if not os.environ.get("DB_USER"):
        raise SystemExit("ERROR: DB_USER not set; load the .env file or set DB_* env vars.")
    raise SystemExit(asyncio.run(main(args)))


if __name__ == "__main__":
    cli()
