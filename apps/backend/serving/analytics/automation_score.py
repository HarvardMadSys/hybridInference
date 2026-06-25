"""Score how script-driven vs. human-driven each user's API usage is.

Produces a per-user ``automation_score`` in ``[0, 1]`` where **HIGH means the
traffic is mostly driven by automatic scripts / batch jobs / cron** and **LOW
means a human is using the service interactively** (a chat UI, or a human-driven
coding agent such as Claude Code). The score blends the four signals the
admin dashboard surfaces -- user turns, length of user turn, user-agent, and
daily activity -- plus two small supporting signals that disambiguate the main
confounder (a high-volume but human-driven coding agent):

* ``turn_pattern`` (user turns) -- fraction of one-shot (``num_user_turns = 1``)
  chat requests, dampened when the user has held deep multi-turn threads.
* ``prompt_size_dispersion`` (length of user turn) -- robust relative dispersion
  (IQR / median) of ``prompt_tokens``; templated automation is near-constant.
* ``client_tool_prior`` (user-agent) -- a soft client-class prior, overridable
  toward human by the ``metadata->>'agent'`` coding-agent opener.
* ``daily_activity_shape`` (daily activity) -- hour coverage, hour entropy, the
  longest nightly quiet gap, and inter-arrival regularity.
* ``tool_call_human_tell`` and ``agent_opener_override`` -- one-directional human
  tells so a human-driven coding agent is never branded a script on volume alone.

Signals lacking enough data for a user are dropped and the remaining weights
re-normalized (never imputed as 0). The blended score is then shrunk toward a
neutral 0.5 prior for users with few requests, and a ``confidence`` is reported
alongside so sparse verdicts are not trusted blindly.

The pure scoring functions have no database dependency and are unit-tested.
:func:`score_users_from_logs` is the single shared entry point that gathers the
per-user aggregates from ``api_logs`` over an asyncpg connection and applies the
scoring -- used by both the admin endpoints and the ``user_automation_score``
CLI so the methodology lives in exactly one place.
"""

from __future__ import annotations

import math
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    import asyncpg

# --- scoring constants -------------------------------------------------------

# Minimum requests below which a user is flagged ``insufficient_data`` (still
# scored, but heavily shrunk toward the prior).
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


def ua_base_from_breakdown(rows: Iterable[tuple[str | None, int]]) -> float | None:
    """Return the request-weighted mean automation value over a user's UAs.

    ``rows`` is an iterable of ``(user_agent, request_count)``. Returns ``None``
    only when the user has no requests (so the signal is dropped, not imputed).
    """
    rows = list(rows)
    total = sum(n for _, n in rows)
    if total <= 0:
        return None
    acc = sum(ua_automation_value(ua) * n for ua, n in rows)
    return acc / total


def clamp01(value: float) -> float:
    """Clamp a float to the ``[0, 1]`` interval."""
    if value < 0.0:
        return 0.0
    if value > 1.0:
        return 1.0
    return value


def hour_shape(hist: Sequence[int]) -> tuple[float, float, float]:
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


# --- DB gather + scoring (shared by the admin endpoints and the CLI) ---------


def _scope(user_ids: Sequence[str] | None) -> tuple[str, tuple[Any, ...]]:
    """Return the shared WHERE clause and params for the window (+ optional users).

    The day count is cast to ``int`` so the ``make_interval(days => ...)``
    argument binds as int4 regardless of how the driver infers the type.
    """
    clause = "timestamp >= now() - make_interval(days => $1::int) AND user_id IS NOT NULL"
    if user_ids is None:
        return clause, ()
    return clause + " AND user_id = ANY($2::text[])", (list(user_ids),)


async def score_users_from_logs(
    conn: asyncpg.Connection,
    *,
    days: int = 30,
    user_ids: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    """Aggregate ``api_logs`` per user over ``days`` and return a scored record each.

    When ``user_ids`` is given, only those users are scored (empty list -> no
    rows); otherwise every non-anonymous user with traffic in the window is
    scored. Each record is ``score_user(...)`` plus ``user_id``, ``days``,
    ``first_seen``, ``last_seen`` and ``total_tokens``, sorted most script-like
    first. The SQL lives here so the admin endpoints and the CLI share one copy.
    """
    if user_ids is not None and not list(user_ids):
        return []
    scope, extra = _scope(user_ids)
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
        record = {
            "user_id": uid,
            "days": days,
            "first_seen": row["first_seen"],
            "last_seen": row["last_seen"],
            "total_tokens": int(row["total_tokens"]) if row["total_tokens"] else 0,
            **score_user(stats),
        }
        records.append(record)
    records.sort(key=lambda r: r["score"], reverse=True)
    return records
