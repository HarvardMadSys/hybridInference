"""Score how script-driven vs. human-driven each user's API usage is.

Produces a per-user ``automation_score`` in ``[0, 1]`` where **HIGH means the
traffic is mostly driven by automatic scripts / batch jobs / cron** and **LOW
means a human is using the service interactively** (a chat UI, or a
human-driven coding agent such as Claude Code). The score blends the four
signals the request named -- user turns, length of user turn, user-agent, and
daily activity -- plus two small supporting signals that disambiguate the main
confounder (a high-volume but human-driven coding agent):

* ``turn_pattern`` (user turns) -- a script firing independent one-shot chat
  completions emits ``num_user_turns = 1`` on essentially every request, while an
  interactive session resends a growing history (1, 2, 3, ...). Scores the
  fraction of one-shot chat requests, dampened when the user has demonstrably
  held deep multi-turn threads.
* ``prompt_size_dispersion`` (length of user turn) -- templated automation sends
  near-constant prompt sizes; humans vary wildly. Scores the robust relative
  dispersion (IQR / median) of ``prompt_tokens``.
* ``client_tool_prior`` (user-agent) -- a soft, low-weight prior from the client
  class (interactive client vs. raw HTTP library vs. ambiguous SDK), overridable
  toward human by the ``metadata->>'agent'`` coding-agent opener.
* ``daily_activity_shape`` (daily activity) -- humans are diurnal and bursty;
  cron is 24/7 and/or metronomic. Fuses hour coverage, hour entropy, the longest
  nightly quiet gap, and inter-arrival regularity.
* ``tool_call_human_tell`` and ``agent_opener_override`` -- one-directional human
  tells so a human-driven coding agent is never branded a script on volume alone.

Signals lacking enough data for a user are dropped and the remaining weights
re-normalized (never imputed as 0). The blended score is then shrunk toward a
neutral 0.5 prior for users with few requests, and a ``confidence`` is reported
alongside so sparse verdicts are not trusted blindly.

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
import math
import os
import re
from pathlib import Path
from typing import Any

import asyncpg
import dotenv

# --- scoring constants -------------------------------------------------------

# Trailing window and the minimum requests below which a user is flagged
# ``insufficient_data`` (still scored, but heavily shrunk toward the prior).
MIN_REQUESTS = 5
NEUTRAL_PRIOR = 0.5
# Shrinkage strength: alpha = N / (N + SHRINK_K); data outweighs the prior at
# N == SHRINK_K.
SHRINK_K = 30

# Default relative weights per signal (re-normalized over the signals that are
# actually available for a given user). They sum to 1.0.
SIGNAL_WEIGHTS: dict[str, float] = {
    "turn_pattern": 0.24,
    "prompt_size_dispersion": 0.17,
    "client_tool_prior": 0.16,
    "daily_activity_shape": 0.27,
    "tool_call_human_tell": 0.08,
    "agent_opener_override": 0.08,
}

# Classification bands over the final score: (label, lower_inclusive).
SCORE_BANDS: tuple[tuple[str, float], ...] = (
    ("scripted_batch", 0.8),
    ("likely_automated", 0.6),
    ("mixed_or_uncertain", 0.35),
    ("likely_human", 0.0),
)

# --- user-agent classification (Python port of the frontend parseClientTool) -

# Human-in-the-loop clients: interactive coding agents and real browsers.
INTERACTIVE_CLIENTS = frozenset(
    {
        "claude-code",
        "cline",
        "kilo-code",
        "roo-code",
        "cursor",
        "aider",
        "continue",
        "codex",
        "browser",
    }
)
# Official SDKs -- ambiguous, because a human chat UI can sit on top of them.
SDK_CLIENTS = frozenset({"openai-python", "openai-node", "anthropic-python", "anthropic-sdk"})
# Raw HTTP libraries / API tools -- overwhelmingly scripts.
SCRIPT_CLIENTS = frozenset(
    {
        "python-requests",
        "aiohttp",
        "httpx",
        "node-fetch",
        "axios",
        "go-http",
        "okhttp",
        "curl",
        "wget",
        "postman",
        "insomnia",
        "httpie",
    }
)

# Per-request automation value by client class. Constants chosen so the
# user-agent (the most spoofable signal) is influential but never decisive:
# raw-HTTP libs lean script (0.85, not 1.0), SDKs stay neutral (0.5), unknown
# leading-token labels lean mild (0.6), and an absent UA leans mild (0.7).
UA_VALUE_INTERACTIVE = 0.1
UA_VALUE_SDK = 0.5
UA_VALUE_SCRIPT = 0.85
UA_VALUE_UNKNOWN = 0.6
UA_VALUE_ABSENT = 0.7

# Ordered (regex, label) table mirroring apps/frontend RequestsTab.parseClientTool.
_CLIENT_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"claude-cli/|claude-code/", re.I), "claude-code"),
    (re.compile(r"kilo[-_ ]?code/", re.I), "kilo-code"),
    (re.compile(r"roo[-_ ]?code/", re.I), "roo-code"),
    (re.compile(r"cline/", re.I), "cline"),
    (re.compile(r"cursor[-_ ]?(?:ide|agent|cli)?/", re.I), "cursor"),
    (re.compile(r"aider/", re.I), "aider"),
    (re.compile(r"continue/", re.I), "continue"),
    (re.compile(r"codex[-_ ]?cli/", re.I), "codex"),
    (re.compile(r"openai[-_ ]?python/|openai/python", re.I), "openai-python"),
    (re.compile(r"openai[-_ ]?node/|openai/(?:javascript|js)\b", re.I), "openai-node"),
    (re.compile(r"anthropic[-_ ]?python/", re.I), "anthropic-python"),
    (re.compile(r"anthropic[-_ ]?(?:sdk|ts|js)/", re.I), "anthropic-sdk"),
    (re.compile(r"postmanruntime/", re.I), "postman"),
    (re.compile(r"insomnia/", re.I), "insomnia"),
    (re.compile(r"httpie/", re.I), "httpie"),
    (re.compile(r"curl/", re.I), "curl"),
    (re.compile(r"wget/", re.I), "wget"),
    (re.compile(r"python-requests/", re.I), "python-requests"),
    (re.compile(r"aiohttp/", re.I), "aiohttp"),
    (re.compile(r"httpx/", re.I), "httpx"),
    (re.compile(r"node-fetch/", re.I), "node-fetch"),
    (re.compile(r"axios/", re.I), "axios"),
    (re.compile(r"go-http-client/", re.I), "go-http"),
    (re.compile(r"okhttp/", re.I), "okhttp"),
)
_BROWSER_RE = re.compile(r"mozilla/|chrome/|safari/|firefox/|edg/", re.I)
_LEADING_TOKEN_RE = re.compile(r"^([A-Za-z][\w.-]{1,32})/")


def classify_client(ua: str | None) -> str | None:
    """Return a canonical client label for a User-Agent, mirroring parseClientTool.

    Returns ``None`` for an empty/absent User-Agent or one with no recognizable
    leading token, a known label (e.g. ``"claude-code"``, ``"curl"``,
    ``"browser"``) for a matched client, or the lowercased leading token for an
    unrecognized but slash-delimited agent.
    """
    if not ua:
        return None
    s = ua.strip()
    if not s:
        return None
    for pattern, label in _CLIENT_PATTERNS:
        if pattern.search(s):
            return label
    if _BROWSER_RE.search(s):
        return "browser"
    match = _LEADING_TOKEN_RE.match(s)
    if match:
        return match.group(1).lower()
    return None


def ua_automation_value(ua: str | None) -> float:
    """Map a single User-Agent to its per-request automation value in ``[0, 1]``."""
    label = classify_client(ua)
    if label is None:
        return UA_VALUE_ABSENT
    if label in INTERACTIVE_CLIENTS:
        return UA_VALUE_INTERACTIVE
    if label in SDK_CLIENTS:
        return UA_VALUE_SDK
    if label in SCRIPT_CLIENTS:
        return UA_VALUE_SCRIPT
    return UA_VALUE_UNKNOWN


def ua_base_from_breakdown(rows: list[tuple[str | None, int]]) -> float | None:
    """Return the request-weighted mean automation value over a user's UAs.

    ``rows`` is ``[(user_agent, request_count), ...]``. Returns ``None`` only when
    the user has no requests (so the signal is dropped rather than imputed).
    """
    total = sum(n for _, n in rows)
    if total <= 0:
        return None
    acc = sum(ua_automation_value(ua) * n for ua, n in rows)
    return acc / total


# --- pure scoring helpers ----------------------------------------------------


def clamp01(value: float) -> float:
    """Clamp a float to the ``[0, 1]`` interval."""
    if value < 0.0:
        return 0.0
    if value > 1.0:
        return 1.0
    return value


def hour_shape(hist: list[int]) -> tuple[float, float, float]:
    """Return ``(coverage, entropy_norm, max_quiet_gap_hours)`` for an hour histogram.

    ``hist`` has 24 buckets (UTC hour-of-day request counts). ``coverage`` is the
    fraction of distinct active hours; ``entropy_norm`` is the Shannon entropy of
    the hour distribution normalized to ``[0, 1]`` (1.0 == perfectly uniform,
    24/7); ``max_quiet_gap_hours`` is the longest circular run of inactive hours
    (a human's nightly rest gap; ~0 for round-the-clock automation).
    """
    total = sum(hist)
    if total <= 0:
        return 0.0, 0.0, 0.0
    active = [h for h in range(24) if hist[h] > 0]
    coverage = len(active) / 24.0

    entropy = 0.0
    for count in hist:
        if count > 0:
            p = count / total
            entropy -= p * math.log2(p)
    entropy_norm = entropy / math.log2(24)

    # Longest circular run of inactive hours = (largest gap between consecutive
    # active hours, wrapping past midnight) - 1.
    augmented = [*active, active[0] + 24]
    max_gap = max(augmented[i + 1] - augmented[i] for i in range(len(active))) - 1
    return coverage, entropy_norm, float(max_gap)


def score_user(stats: dict[str, Any]) -> dict[str, Any]:
    """Compute the automation score and per-signal breakdown for one user.

    ``stats`` carries the per-user aggregates gathered from ``api_logs``. Returns
    ``{score, confidence, band, insufficient_data, n_req, agent_share, signals,
    detail}`` where ``signals`` maps each signal name to
    ``{sub, weight, available}`` and ``detail`` exposes the raw metrics behind
    the sub-scores so a verdict is auditable.
    """
    n_req = stats["n_req"]
    n_chat = stats["n_chat"]
    agent_share = stats["agent_share"]
    signals: dict[str, dict[str, Any]] = {}
    detail: dict[str, Any] = {"agent_share": agent_share}

    def record(name: str, sub: float | None, available: bool) -> None:
        signals[name] = {
            "sub": sub,
            "weight": SIGNAL_WEIGHTS[name],
            "available": available,
        }

    # 1. turn_pattern -- fraction of one-shot chat requests, dampened when the
    #    user has held genuinely deep (p90 >= 3) multi-turn threads.
    if n_chat >= 5:
        one_shot_fraction = stats["n_oneshot"] / n_chat
        p90_depth = stats["p90_depth"]
        depth_factor = 0.5 if (p90_depth is not None and p90_depth >= 3) else 1.0
        record("turn_pattern", clamp01(one_shot_fraction * depth_factor), True)
        detail["one_shot_fraction"] = one_shot_fraction
        detail["p90_user_turns"] = p90_depth
    else:
        record("turn_pattern", None, False)

    # 2. prompt_size_dispersion -- robust relative dispersion of prompt sizes;
    #    low dispersion (templated) -> automated.
    median_sz = stats["p50_sz"]
    if stats["n_sz"] >= 8 and median_sz and median_sz > 0:
        rcv = (stats["p75_sz"] - stats["p25_sz"]) / median_sz
        record("prompt_size_dispersion", clamp01(1.0 - rcv / 0.5), True)
        detail["prompt_token_rcv"] = rcv
    else:
        record("prompt_size_dispersion", None, False)

    # 3. client_tool_prior -- soft UA-class prior, pulled toward human by the
    #    coding-agent opener share. Available whenever the user has any requests.
    ua_base = stats["ua_base"]
    if ua_base is not None:
        sub_ua = clamp01(ua_base * (1.0 - 0.85 * min(agent_share, 1.0)))
        record("client_tool_prior", sub_ua, True)
        detail["ua_base"] = ua_base
    else:
        record("client_tool_prior", None, False)

    # 4. daily_activity_shape -- fuse the available timing parts (mostly
    #    timezone-invariant) and re-normalize over whichever pass their floors.
    restgap_score: float | None = None
    parts: list[tuple[float, float]] = []
    coverage, entropy_norm, max_gap = hour_shape(stats["hour_hist"])
    if n_req >= 10 and sum(stats["hour_hist"]) > 0:
        parts.append((clamp01((coverage - 0.5) / 0.5), 0.20))
        parts.append((clamp01((entropy_norm - 0.5) / (0.92 - 0.5)), 0.20))
        restgap_score = clamp01(1.0 - max_gap / 6.0)
        parts.append((restgap_score, 0.30))
        detail["hour_coverage"] = coverage
        detail["hour_entropy_norm"] = entropy_norm
        detail["max_quiet_gap_hours"] = max_gap
    gap_median = stats["gap_p50"]
    if stats["n_gap"] >= 3 and gap_median and gap_median > 0:
        gap_rcv = (stats["gap_p75"] - stats["gap_p25"]) / gap_median
        parts.append((clamp01(1.0 - gap_rcv / 1.0), 0.30))
        detail["interarrival_rcv"] = gap_rcv
    if parts:
        part_wsum = sum(w for _, w in parts)
        record("daily_activity_shape", sum(s * w for s, w in parts) / part_wsum, True)
    else:
        record("daily_activity_shape", None, False)

    # 5. tool_call_human_tell -- agentic tool use is a human (coding-agent) tell.
    #    One-directional: it is dropped entirely when there are no tool calls
    #    (absence of tool use is not evidence of automation) and otherwise maps
    #    more tool use to a lower sub-score, capped at the neutral 0.5 so it can
    #    only ever pull a user toward human, never raise the score.
    if n_chat >= 5 and stats["n_toolpos"] > 0:
        toolcall_share = stats["n_toolpos"] / stats["n_toolrows"]
        record("tool_call_human_tell", clamp01(0.5 - toolcall_share), True)
        detail["toolcall_share"] = toolcall_share
    else:
        record("tool_call_human_tell", None, False)

    # 6. agent_opener_override -- a coding-agent opener is content-derived proof
    #    of human-in-the-loop use; its absence is uninformative (dropped).
    if agent_share >= 0.05:
        record("agent_opener_override", clamp01(0.15 - agent_share), True)
    else:
        record("agent_opener_override", None, False)

    available = {
        name: (sig["sub"], sig["weight"]) for name, sig in signals.items() if sig["available"]
    }
    if not available:
        raw, confidence = NEUTRAL_PRIOR, 0.0
    else:
        weight_sum = sum(w for _, w in available.values())
        raw = sum(sub * w for sub, w in available.values()) / weight_sum
        # Hard human clamp: a high-volume coding-agent user with a real nightly
        # rest gap can never be branded above "mixed" on volume alone.
        if agent_share >= 0.3 and restgap_score is not None and restgap_score < 0.5:
            raw = min(raw, 0.5)
        # Coverage = share of total weight that was actually available.
        confidence = weight_sum

    alpha = n_req / (n_req + SHRINK_K)
    final = clamp01(alpha * raw + (1.0 - alpha) * NEUTRAL_PRIOR)
    confidence = alpha * confidence

    return {
        "score": final,
        "confidence": confidence,
        "band": band_for(final),
        "insufficient_data": n_req < MIN_REQUESTS,
        "n_req": n_req,
        "agent_share": agent_share,
        "signals": signals,
        "detail": detail,
    }


def band_for(score: float) -> str:
    """Return the classification band label for a final score."""
    for label, lower in SCORE_BANDS:
        if score >= lower:
            return label
    return SCORE_BANDS[-1][0]


# --- environment / connection (mirrors user_usage_pattern.py) ----------------


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


# --- gathering ---------------------------------------------------------------


def _scope(user_id: str | None) -> tuple[str, tuple[Any, ...]]:
    """Return the shared WHERE clause and params for the window (+ optional user)."""
    # Cast the day count explicitly so the make_interval(days => ...) argument
    # binds as int4 regardless of how the driver infers the parameter type.
    clause = "timestamp >= now() - make_interval(days => $1::int) AND user_id IS NOT NULL"
    params: tuple[Any, ...] = ()
    if user_id is not None:
        clause += " AND user_id = $2"
        params = (user_id,)
    return clause, params


async def _gather(
    conn: asyncpg.Connection, days: int, user_id: str | None = None
) -> list[dict[str, Any]]:
    """Aggregate ``api_logs`` per user and return a scored record for each."""
    scope, extra = _scope(user_id)
    args = (days, *extra)

    base = await conn.fetch(
        f"""
        SELECT user_id,
               count(*) AS n_req,
               count(*) FILTER (WHERE num_user_turns IS NOT NULL) AS n_chat,
               count(*) FILTER (WHERE num_user_turns = 1) AS n_oneshot,
               percentile_disc(0.9) WITHIN GROUP (ORDER BY num_user_turns) AS p90_depth,
               count(*) FILTER (WHERE num_tool_calls IS NOT NULL) AS n_toolrows,
               count(*) FILTER (WHERE num_tool_calls > 0) AS n_toolpos,
               count(*) FILTER (WHERE prompt_tokens > 0) AS n_sz,
               percentile_cont(0.25) WITHIN GROUP (ORDER BY nullif(prompt_tokens, 0)) AS p25_sz,
               percentile_cont(0.5)  WITHIN GROUP (ORDER BY nullif(prompt_tokens, 0)) AS p50_sz,
               percentile_cont(0.75) WITHIN GROUP (ORDER BY nullif(prompt_tokens, 0)) AS p75_sz,
               count(*) FILTER (WHERE metadata->>'agent' IS NOT NULL) AS n_agent,
               min(timestamp) AS first_seen,
               max(timestamp) AS last_seen,
               sum(total_tokens) AS total_tokens
        FROM api_logs
        WHERE {scope}
        GROUP BY user_id
        """,
        *args,
    )
    if not base:
        return []

    hour_rows = await conn.fetch(
        f"""
        SELECT user_id, extract(hour FROM timestamp)::int AS hour, count(*) AS n
        FROM api_logs WHERE {scope}
        GROUP BY user_id, hour
        """,
        *args,
    )
    hist_by_uid: dict[str, list[int]] = {}
    for row in hour_rows:
        hist = hist_by_uid.setdefault(row["user_id"], [0] * 24)
        hist[row["hour"]] = row["n"]

    gap_rows = await conn.fetch(
        f"""
        WITH gaps AS (
            SELECT user_id,
                   extract(epoch FROM timestamp)
                     - lag(extract(epoch FROM timestamp))
                         OVER (PARTITION BY user_id ORDER BY timestamp) AS gap
            FROM api_logs WHERE {scope}
        )
        SELECT user_id,
               percentile_cont(0.25) WITHIN GROUP (ORDER BY gap) AS gp25,
               percentile_cont(0.5)  WITHIN GROUP (ORDER BY gap) AS gp50,
               percentile_cont(0.75) WITHIN GROUP (ORDER BY gap) AS gp75,
               count(gap) AS n_gap
        FROM gaps WHERE gap > 0
        GROUP BY user_id
        """,
        *args,
    )
    gap_by_uid = {row["user_id"]: row for row in gap_rows}

    ua_rows = await conn.fetch(
        f"""
        SELECT user_id, metadata->>'user_agent' AS ua, count(*) AS n
        FROM api_logs WHERE {scope}
        GROUP BY user_id, ua
        """,
        *args,
    )
    ua_by_uid: dict[str, list[tuple[str | None, int]]] = {}
    for row in ua_rows:
        ua_by_uid.setdefault(row["user_id"], []).append((row["ua"], row["n"]))

    uids = [row["user_id"] for row in base]
    user_rows = await conn.fetch(
        "SELECT id, email, user_name, role, status, created_at, last_login_at "
        "FROM users WHERE id = ANY($1::text[])",
        uids,
    )
    users = {row["id"]: dict(row) for row in user_rows}

    records: list[dict[str, Any]] = []
    for row in base:
        uid = row["user_id"]
        n_req = row["n_req"]
        gap = gap_by_uid.get(uid)
        stats = {
            "n_req": n_req,
            "n_chat": row["n_chat"],
            "n_oneshot": row["n_oneshot"],
            "p90_depth": row["p90_depth"],
            "n_toolrows": row["n_toolrows"],
            "n_toolpos": row["n_toolpos"],
            "n_sz": row["n_sz"],
            "p25_sz": row["p25_sz"],
            "p50_sz": row["p50_sz"],
            "p75_sz": row["p75_sz"],
            "hour_hist": hist_by_uid.get(uid, [0] * 24),
            "gap_p25": gap["gp25"] if gap else None,
            "gap_p50": gap["gp50"] if gap else None,
            "gap_p75": gap["gp75"] if gap else None,
            "n_gap": gap["n_gap"] if gap else 0,
            "ua_base": ua_base_from_breakdown(ua_by_uid.get(uid, [])),
            "agent_share": row["n_agent"] / n_req if n_req else 0.0,
        }
        scored = score_user(stats)
        user = users.get(uid, {})
        records.append(
            {
                "user_id": uid,
                "email": user.get("email"),
                "user_name": user.get("user_name"),
                "role": user.get("role"),
                "status": user.get("status"),
                "first_seen": row["first_seen"],
                "last_seen": row["last_seen"],
                "total_tokens": int(row["total_tokens"]) if row["total_tokens"] else 0,
                **scored,
            }
        )
    records.sort(key=lambda r: r["score"], reverse=True)
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
    print(f"  window: last {days}d   requests={r['n_req']:,}   tokens={r['total_tokens']:,}")
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
