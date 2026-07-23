"""PostgreSQL implementation of LogStore (api_logs + api_stats_hourly)."""

from __future__ import annotations

import datetime as dt
import json
from typing import TYPE_CHECKING, Any, Literal

from serving.analytics.automation_score import score_users_from_logs
from serving.storage.base import LogStore, Row
from serving.storage.log_schema import ensure_api_logs_schema
from serving.storage.utils import (
    agent_name_from_prompt,
    calculate_cost,
    conversation_shape,
    json_safe,
    strip_null_bytes,
    user_message_stats,
)
from serving.utils.logging import get_logger
from serving.utils.token_utils import normalize_usage

if TYPE_CHECKING:
    import asyncpg

logger = get_logger(__name__)

# Tool names (lowercased) that let the model ask the end user a clarifying
# question. Matched case-insensitively against each request's ``tools`` list; a
# tool name lives at ``function.name`` (OpenAI shape) or ``name`` (Anthropic
# shape). Deliberately broad — it spans the coding-agent clients we serve
# (e.g. Claude Code's ``AskUserQuestion``, plus ``question`` /
# ``ask_followup_question`` / ``vscode_askquestions`` and the ``ask_user_*``
# variants).
ASK_QUESTION_TOOL_NAMES: tuple[str, ...] = (
    "question",
    "askuserquestion",
    "ask_user_question",
    "ask_user_questions",
    "ask_followup_question",
    "vscode_askquestions",
)

# Ask-question adoption is measured over each user's *early-conversation*
# requests only — those with ``num_turns <= ASK_QUESTION_TURN_CAP``. Agentic
# clients re-send the same tool manifest on every turn, so a single long
# conversation would otherwise dominate the per-request count; capping to the
# opening turns approximates one observation per conversation. The manifest is
# fixed within a conversation (an ask tool present at turn 1 is present
# throughout), so the opening turns are representative of the whole trajectory.
ASK_QUESTION_TURN_CAP = 5


def _metadata_dict(value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value:
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return {}


def _string_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


class PostgresLogStore(LogStore):
    """LogStore backed by an asyncpg connection pool."""

    def __init__(
        self,
        pool: asyncpg.Pool,
        *,
        store_full_prompts: bool = True,
    ) -> None:
        """Initialize with an existing asyncpg pool.

        Args:
            pool: Shared asyncpg connection pool.
            store_full_prompts: If False, prompt and response fields will be NULL.
        """
        self.pool = pool
        self.store_full_prompts = store_full_prompts

    # -- lifecycle -----------------------------------------------------------

    async def initialize(self) -> None:
        """Create api_logs and api_stats_hourly tables with indexes.

        Delegates to :func:`serving.storage.log_schema.ensure_api_logs_schema`,
        the single source of truth shared with ``DatabaseLogger._create_tables``
        so the two initializers cannot drift.
        """
        async with self.pool.acquire() as conn:
            await ensure_api_logs_schema(conn)

    async def cleanup(self) -> None:
        """No-op — pool lifecycle is managed externally."""

    async def health_check(self) -> bool:
        """Run ``SELECT 1`` to verify connectivity."""
        try:
            async with self.pool.acquire() as conn:
                await conn.fetchval("SELECT 1")
            return True
        except Exception:
            return False

    async def query_provider_hourly_spend(self, hour_iso: str) -> dict[str, float]:
        """Return per-provider total cost (USD) for the given UTC hour bucket.

        ``hour_iso`` is an ISO-8601 timestamp truncated to the hour (e.g.
        ``2026-05-03T12:00:00+00:00``). Used by ``ProviderHourlySpendJob``.
        """
        sql = """
            SELECT provider, COALESCE(SUM(cost_usd), 0) AS total
            FROM api_logs
            WHERE date_trunc('hour', timestamp) = $1
            GROUP BY provider
        """
        hour_dt = dt.datetime.fromisoformat(hour_iso)
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(sql, hour_dt)
        return {r["provider"]: float(r["total"]) for r in rows if r["provider"]}

    # -- request logging -----------------------------------------------------

    async def log_request(
        self,
        *,
        request_id: str,
        model_id: str,
        provider: str,
        prompt: list[dict[str, Any]] | str,
        response: dict[str, Any] | str | None,
        usage: dict[str, Any] | None,
        latency_ms: int,
        status_code: int,
        error: str | None = None,
        params: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        ttft_ms: int | None = None,
        store_full_content: bool | None = None,
        pricing: dict[str, str] | None = None,
        upstream_cost_usd: float | None = None,
        request_payload: dict[str, Any] | None = None,
        served_model_id: str | None = None,
    ) -> None:
        """Insert a single request log row.

        upstream_cost_usd: OpenRouter-reported per-request upstream cost (USD),
        or None for non-OpenRouter routes.

        served_model_id: the model that actually served the request, when it
        diverges from the client-requested ``model_id`` (aliasing / rerouting).
        Defaults to ``model_id``. The served endpoint is recovered from the
        routing ``metadata`` and stored alongside it.
        """
        usage = normalize_usage(usage) or usage

        should_store_full = (
            store_full_content if store_full_content is not None else self.store_full_prompts
        )
        sanitized_error = strip_null_bytes(error)
        sanitized_metadata = strip_null_bytes(metadata)
        sanitized_tools = strip_null_bytes((params or {}).get("tools"))
        if should_store_full:
            sanitized_prompt = strip_null_bytes(prompt)
            sanitized_response = strip_null_bytes(response)
            sanitized_request_payload = strip_null_bytes(request_payload)
            prompt_str = (
                json.dumps(sanitized_prompt)
                if isinstance(sanitized_prompt, list)
                else str(sanitized_prompt)
            )
            response_str = (
                json.dumps(json_safe(sanitized_response))
                if isinstance(sanitized_response, dict)
                else str(sanitized_response)
                if sanitized_response is not None
                else None
            )
            request_payload_str = (
                json.dumps(json_safe(sanitized_request_payload))
                if sanitized_request_payload is not None
                else None
            )
        else:
            prompt_str = None
            response_str = None
            request_payload_str = None

        cost_usd = calculate_cost(usage, pricing)
        # Conversation shape is derived metadata (like token counts), so it is
        # always recorded — independent of full-content storage — letting the
        # admin list query read cheap integer columns instead of the payload.
        num_turns, num_user_turns, num_tool_calls = conversation_shape(prompt)
        # Size / entropy / repetition fingerprint of the newest user message,
        # derived from the inbound prompt like conversation shape so it is
        # recorded independent of full-content storage and lets the automation
        # score read cheap columns instead of de-TOASTing the payload per row.
        last_user_msg_chars, last_user_msg_entropy, last_user_msg_hash = user_message_stats(prompt)
        # Agent identity declared in the system prompt (e.g. "You are Claude
        # Code, ..."). Stored in metadata so the admin list query can label the
        # client by its declared name, falling back to User-Agent parsing when
        # absent. Derived from the original inbound prompt, like conversation
        # shape, so it is recorded independent of full-content storage. The
        # Anthropic /v1/messages surface carries the system prompt as a
        # top-level ``system`` field (outside ``messages``), preserved in
        # request_payload — pass it so that surface is covered too.
        system_field = request_payload.get("system") if isinstance(request_payload, dict) else None
        agent = agent_name_from_prompt(prompt, system=system_field)
        if agent is not None:
            sanitized_metadata = {**(sanitized_metadata or {}), "agent": agent}

        # Promote the served model/endpoint into queryable columns. served_model
        # defaults to model_id (already the resolved model on the Anthropic
        # surface; requested == served on completions); callers pass it
        # explicitly when the served model diverges. served_endpoint is recovered
        # from the routing metadata using the same fallback chain as the admin
        # routing view (endpoint_id -> routewise primary -> base_url -> provider).
        served_model = served_model_id or model_id
        _served_md = _metadata_dict(sanitized_metadata)
        _routewise_md = _served_md.get("routewise")
        served_endpoint = (
            _string_or_none(_served_md.get("endpoint_id"))
            or (
                _string_or_none(_routewise_md.get("primary_provider"))
                if isinstance(_routewise_md, dict)
                else None
            )
            or _string_or_none(_served_md.get("base_url"))
            or _string_or_none(provider)
        )

        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO api_logs (
                    request_id, model_id, provider,
                    temperature, top_p, max_tokens, seed, stream,
                    ttft_ms, latency_ms,
                    prompt_tokens, completion_tokens, reasoning_tokens, total_tokens,
                    cache_read_tokens, cache_write_tokens, cost_usd,
                    prompt, response, request_payload,
                    status_code, error, user_id, session_id, metadata,
                    tools, upstream_cost_usd,
                    num_turns, num_user_turns, num_tool_calls,
                    last_user_msg_chars, last_user_msg_entropy, last_user_msg_hash,
                    served_model_id, served_endpoint_id
                )
                VALUES (
                    $1, $2, $3,
                    $4, $5, $6, $7, $8,
                    $9, $10,
                    $11, $12, $13, $14,
                    $15, $16, $17,
                    $18, $19, $20::jsonb,
                    $21, $22, $23, $24, $25::jsonb,
                    $26::jsonb, $27,
                    $28, $29, $30,
                    $31, $32, $33,
                    $34, $35
                )
                ON CONFLICT (request_id) DO NOTHING
                """,
                request_id,
                model_id,
                provider,
                (params or {}).get("temperature"),
                (params or {}).get("top_p"),
                (params or {}).get("max_tokens"),
                (params or {}).get("seed"),
                (params or {}).get("stream"),
                ttft_ms,
                latency_ms,
                (usage or {}).get("prompt_tokens"),
                (usage or {}).get("completion_tokens"),
                (usage or {}).get("reasoning_tokens"),
                (usage or {}).get("total_tokens"),
                (usage or {}).get("cache_read_tokens"),
                (usage or {}).get("cache_write_tokens"),
                cost_usd,
                prompt_str,
                response_str,
                request_payload_str,
                status_code,
                sanitized_error,
                (sanitized_metadata or {}).get("user_id"),
                (sanitized_metadata or {}).get("session_id"),
                json.dumps(json_safe(sanitized_metadata)) if sanitized_metadata else None,
                json.dumps(json_safe(sanitized_tools)) if sanitized_tools else None,
                upstream_cost_usd,
                num_turns,
                num_user_turns,
                num_tool_calls,
                last_user_msg_chars,
                last_user_msg_entropy,
                last_user_msg_hash,
                served_model,
                served_endpoint,
            )

    # -- usage / cost queries ------------------------------------------------

    async def get_user_cost_today(self, user_id: str) -> float:
        """Return total cost_usd since UTC midnight."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT COALESCE(SUM(cost_usd), 0) AS cost_spent
                FROM api_logs
                WHERE user_id = $1
                  AND timestamp >= date_trunc('day', NOW() AT TIME ZONE 'UTC')
                """,
                user_id,
            )
        return float(row["cost_spent"]) if row else 0.0

    async def get_user_cost_period(
        self,
        user_id: str,
        period: Literal["today", "month"],
    ) -> float:
        """Return total cost_usd within the given period."""
        trunc = "day" if period == "today" else "month"
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                SELECT COALESCE(SUM(cost_usd), 0) AS cost_spent
                FROM api_logs
                WHERE user_id = $1
                  AND timestamp >= date_trunc('{trunc}', NOW() AT TIME ZONE 'UTC')
                """,
                user_id,
            )
        return float(row["cost_spent"]) if row else 0.0

    async def get_user_usage_detail(self, user_id: str) -> dict[str, Any]:
        """Return detailed usage stats for the user dashboard."""
        async with self.pool.acquire() as conn:
            today = await conn.fetchrow(
                """
                SELECT COALESCE(SUM(cost_usd), 0) AS cost,
                       COUNT(*) AS reqs,
                       COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                       COALESCE(SUM(completion_tokens), 0) AS completion_tokens
                FROM api_logs
                WHERE user_id = $1
                  AND timestamp >= date_trunc('day', NOW() AT TIME ZONE 'UTC')
                """,
                user_id,
            )
            week = await conn.fetchrow(
                """
                SELECT COALESCE(SUM(cost_usd), 0) AS cost,
                       COUNT(*) AS reqs,
                       COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                       COALESCE(SUM(completion_tokens), 0) AS completion_tokens
                FROM api_logs
                WHERE user_id = $1
                  AND timestamp >= NOW() - INTERVAL '7 days'
                """,
                user_id,
            )
            month = await conn.fetchrow(
                """
                SELECT COALESCE(SUM(cost_usd), 0) AS cost,
                       COUNT(*) AS reqs,
                       COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                       COALESCE(SUM(completion_tokens), 0) AS completion_tokens
                FROM api_logs
                WHERE user_id = $1
                  AND timestamp >= date_trunc('month', NOW() AT TIME ZONE 'UTC')
                """,
                user_id,
            )
            alltime = await conn.fetchrow(
                """
                SELECT COALESCE(SUM(cost_usd), 0) AS cost,
                       COUNT(*) AS reqs,
                       COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                       COALESCE(SUM(completion_tokens), 0) AS completion_tokens
                FROM api_logs
                WHERE user_id = $1
                """,
                user_id,
            )

        def _extract(row: Any) -> dict[str, Any]:
            if not row:
                return {
                    "cost_usd": 0.0,
                    "requests": 0,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                }
            return {
                "cost_usd": float(row["cost"]),
                "requests": int(row["reqs"]),
                "prompt_tokens": int(row["prompt_tokens"]),
                "completion_tokens": int(row["completion_tokens"]),
            }

        return {
            "today": _extract(today),
            "week": _extract(week),
            "month": _extract(month),
            "alltime": _extract(alltime),
        }

    async def get_batch_usage(
        self,
        user_ids: list[str],
        period: Literal["today", "month"],
    ) -> dict[str, float]:
        """Return ``{user_id: cost_usd}`` for a batch of users."""
        if not user_ids:
            return {}
        trunc = "day" if period == "today" else "month"
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT user_id, COALESCE(SUM(cost_usd), 0) AS cost
                FROM api_logs
                WHERE user_id = ANY($1::text[])
                  AND timestamp >= date_trunc('{trunc}', NOW() AT TIME ZONE 'UTC')
                GROUP BY user_id
                """,
                user_ids,
            )
        return {row["user_id"]: float(row["cost"]) for row in rows}

    async def get_user_detail_usage(
        self, user_id: str, *, include_activity_stats: bool = False
    ) -> dict[str, Any]:
        """Return usage detail for admin user-detail view.

        The all-time turn averages (``avg_turns``, ``avg_user_turns``) require
        full-history scans of the user's ``api_logs`` rows (no time bound), so
        for heavy users they dominate the cost of this call; ``ask_question_fraction``
        is measured over just the early-conversation requests (first few turns,
        see ``ASK_QUESTION_TURN_CAP``). These three are computed only when
        ``include_activity_stats`` is set; otherwise those keys are ``None`` and
        the admin UI loads them on demand behind a button. The remaining fields
        are time-bounded (or an indexed ``MAX``) and are always computed.
        """
        avg_turns: float | None = None
        avg_user_turns: float | None = None
        ask_question_fraction: float | None = None
        async with self.pool.acquire() as conn:
            today = await conn.fetchrow(
                """
                SELECT COALESCE(SUM(cost_usd), 0) AS cost, COUNT(*) AS reqs
                FROM api_logs
                WHERE user_id = $1
                  AND timestamp >= date_trunc('day', NOW() AT TIME ZONE 'UTC')
                """,
                user_id,
            )
            month = await conn.fetchrow(
                """
                SELECT COALESCE(SUM(cost_usd), 0) AS cost, COUNT(*) AS reqs
                FROM api_logs
                WHERE user_id = $1
                  AND timestamp >= date_trunc('month', NOW() AT TIME ZONE 'UTC')
                """,
                user_id,
            )
            models_rows = await conn.fetch(
                """
                SELECT DISTINCT model_id FROM api_logs
                WHERE user_id = $1 AND timestamp >= NOW() - INTERVAL '30 days'
                ORDER BY model_id
                """,
                user_id,
            )
            last_req = await conn.fetchrow(
                "SELECT MAX(timestamp) AS ts FROM api_logs WHERE user_id = $1",
                user_id,
            )
            if include_activity_stats:
                # AVG ignores the NULL num_turns / num_user_turns of non-chat
                # requests, so these average over chat-style requests only.
                turns = await conn.fetchrow(
                    """
                    SELECT AVG(num_turns) AS avg_turns,
                           AVG(num_user_turns) AS avg_user_turns
                    FROM api_logs
                    WHERE user_id = $1
                    """,
                    user_id,
                )
                avg_turns = (
                    float(turns["avg_turns"]) if turns and turns["avg_turns"] is not None else None
                )
                avg_user_turns = (
                    float(turns["avg_user_turns"])
                    if turns and turns["avg_user_turns"] is not None
                    else None
                )
                # Share of the user's early-conversation requests (first
                # ``ASK_QUESTION_TURN_CAP`` turns) whose available ``tools`` offer
                # an ask-the-user clarifying tool (see ``ASK_QUESTION_TOOL_NAMES``).
                ask = await conn.fetchrow(
                    """
                    SELECT COUNT(*) AS n_requests,
                           COUNT(*) FILTER (
                               WHERE jsonb_typeof(tools) = 'array' AND EXISTS (
                                   SELECT 1 FROM jsonb_array_elements(tools) AS t
                                   WHERE lower(
                                           COALESCE(t->'function'->>'name', t->>'name')
                                         ) = ANY($2)
                               )
                           ) AS n_ask_requests
                    FROM api_logs
                    WHERE user_id = $1
                      AND num_turns <= $3
                    """,
                    user_id,
                    list(ASK_QUESTION_TOOL_NAMES),
                    ASK_QUESTION_TURN_CAP,
                )
                ask_question_fraction = (
                    int(ask["n_ask_requests"]) / int(ask["n_requests"])
                    if ask and ask["n_requests"]
                    else None
                )

        return {
            "usage_today_usd": float(today["cost"]) if today else 0.0,
            "usage_today_requests": int(today["reqs"]) if today else 0,
            "usage_month_usd": float(month["cost"]) if month else 0.0,
            "usage_month_requests": int(month["reqs"]) if month else 0,
            "models_used": [r["model_id"] for r in models_rows],
            "last_request_at": last_req["ts"] if last_req and last_req["ts"] else None,
            "avg_turns": avg_turns,
            "avg_user_turns": avg_user_turns,
            "ask_question_fraction": ask_question_fraction,
        }

    async def get_bulk_user_turn_averages(
        self, user_ids: list[str]
    ) -> dict[str, dict[str, float | None]]:
        """Return per-user all-time average turn counts for many users.

        Maps ``user_id`` → ``{"avg_turns": float|None, "avg_user_turns": float|None}``
        for users with chat-style requests; users with no chat logs are omitted
        (the caller fills a default). AVG ignores the NULL turn counts of
        non-chat requests.
        """
        if not user_ids:
            return {}
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT user_id,
                       AVG(num_turns) AS avg_turns,
                       AVG(num_user_turns) AS avg_user_turns
                FROM api_logs
                WHERE user_id = ANY($1)
                GROUP BY user_id
                """,
                user_ids,
            )
        return {
            r["user_id"]: {
                "avg_turns": float(r["avg_turns"]) if r["avg_turns"] is not None else None,
                "avg_user_turns": float(r["avg_user_turns"])
                if r["avg_user_turns"] is not None
                else None,
            }
            for r in rows
        }

    async def get_bulk_user_ask_question_fractions(
        self, user_ids: list[str]
    ) -> dict[str, dict[str, float | int | None]]:
        """Return per-user share of early-conversation requests offering an ask tool.

        Maps ``user_id`` → ``{"ask_question_fraction", "n_requests",
        "n_ask_requests"}`` over the user's early-conversation ``api_logs`` rows
        (those with ``num_turns <= ASK_QUESTION_TURN_CAP``). A request counts
        toward ``n_ask_requests`` when its ``tools`` list offers a tool from
        :data:`ASK_QUESTION_TOOL_NAMES` (matched case-insensitively across the
        OpenAI and Anthropic tool shapes); ``ask_question_fraction`` is
        ``n_ask_requests / n_requests``. Capping to opening turns keeps a long
        agentic conversation (which re-sends the same manifest every turn) from
        dominating the count. Users with no early-conversation requests are
        omitted (the caller fills a default). Bounded to the page's ``user_ids``
        so it stays an indexed lookup.
        """
        if not user_ids:
            return {}
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT user_id,
                       COUNT(*) AS n_requests,
                       COUNT(*) FILTER (
                           WHERE jsonb_typeof(tools) = 'array' AND EXISTS (
                               SELECT 1 FROM jsonb_array_elements(tools) AS t
                               WHERE lower(
                                       COALESCE(t->'function'->>'name', t->>'name')
                                     ) = ANY($2)
                           )
                       ) AS n_ask_requests
                FROM api_logs
                WHERE user_id = ANY($1)
                  AND num_turns <= $3
                GROUP BY user_id
                """,
                user_ids,
                list(ASK_QUESTION_TOOL_NAMES),
                ASK_QUESTION_TURN_CAP,
            )
        result: dict[str, dict[str, float | int | None]] = {}
        for r in rows:
            n_requests = int(r["n_requests"])
            n_ask = int(r["n_ask_requests"])
            result[r["user_id"]] = {
                "ask_question_fraction": (n_ask / n_requests) if n_requests else None,
                "n_requests": n_requests,
                "n_ask_requests": n_ask,
            }
        return result

    async def get_user_automation_score(
        self, user_id: str, *, days: int = 30
    ) -> dict[str, Any] | None:
        """Return the automation score for one user, or ``None`` with no traffic.

        See :mod:`serving.analytics.automation_score`: HIGH (→1) means the user's
        ``api_logs`` over the trailing ``days`` look script/batch/cron-driven, LOW
        (→0) interactive-human (incl. human-driven coding agents).
        """
        async with self.pool.acquire() as conn:
            records = await score_users_from_logs(conn, days=days, user_ids=[user_id])
        return records[0] if records else None

    async def get_bulk_user_automation_scores(
        self, user_ids: list[str], *, days: int = 30
    ) -> dict[str, dict[str, Any]]:
        """Return ``{user_id: automation-score record}`` for many users.

        One round-trip scores every requested user over the trailing ``days``
        window. Users with no traffic in the window are omitted (the caller
        renders a placeholder). See :mod:`serving.analytics.automation_score`.
        """
        if not user_ids:
            return {}
        async with self.pool.acquire() as conn:
            records = await score_users_from_logs(conn, days=days, user_ids=user_ids)
        return {r["user_id"]: r for r in records}

    async def get_key_detail_usage(self, user_id: str) -> dict[str, Any]:
        """Return usage detail for admin key-detail view."""
        async with self.pool.acquire() as conn:
            today = await conn.fetchrow(
                """
                SELECT COALESCE(SUM(cost_usd), 0) AS cost, COUNT(*) AS reqs
                FROM api_logs
                WHERE user_id = $1
                  AND timestamp >= date_trunc('day', NOW() AT TIME ZONE 'UTC')
                """,
                user_id,
            )
            month = await conn.fetchrow(
                """
                SELECT COALESCE(SUM(cost_usd), 0) AS cost, COUNT(*) AS reqs
                FROM api_logs
                WHERE user_id = $1
                  AND timestamp >= date_trunc('month', NOW() AT TIME ZONE 'UTC')
                """,
                user_id,
            )
            models_rows = await conn.fetch(
                """
                SELECT DISTINCT model_id FROM api_logs
                WHERE user_id = $1 AND timestamp >= NOW() - INTERVAL '30 days'
                ORDER BY model_id
                """,
                user_id,
            )
            last_req = await conn.fetchrow(
                "SELECT MAX(timestamp) AS ts FROM api_logs WHERE user_id = $1",
                user_id,
            )

        return {
            "today": {
                "cost_usd": float(today["cost"]) if today else 0.0,
                "requests": int(today["reqs"]) if today else 0,
            },
            "this_month": {
                "cost_usd": float(month["cost"]) if month else 0.0,
                "requests": int(month["reqs"]) if month else 0,
            },
            "models_used": [r["model_id"] for r in models_rows],
            "last_request_at": last_req["ts"] if last_req and last_req["ts"] else None,
        }

    # -- analytics -----------------------------------------------------------

    async def get_model_activity(self, window_minutes: int = 10) -> dict[str, Any]:
        """Aggregate recent real-user traffic per (model_id, provider).

        Synthetic probe rows (``metadata.synthetic_probe``, persisted when
        ``log_synthetic_probes`` is on) are excluded so probe traffic can't be
        mistaken for real activity — which feeds ``/health/model-activity`` and
        would otherwise let a probe suppress subsequent probes.
        """
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT model_id, provider,
                       COUNT(*)                                           AS request_count,
                       COUNT(*) FILTER (WHERE status_code < 400)          AS success_count,
                       MAX(timestamp)                                     AS last_request_at,
                       AVG(latency_ms) FILTER (WHERE status_code < 400)   AS avg_latency_ms,
                       COUNT(*) FILTER (WHERE stream = TRUE)              AS stream_count,
                       COUNT(*) FILTER (WHERE stream IS NOT TRUE)         AS non_stream_count,
                       COUNT(*) FILTER (WHERE stream = TRUE AND status_code < 400)
                           AS stream_success_count,
                       COUNT(*) FILTER (WHERE stream IS NOT TRUE AND status_code < 400)
                           AS non_stream_success_count,
                       MAX(timestamp) FILTER (WHERE stream = TRUE AND status_code < 400)
                           AS stream_last_success_at,
                       MAX(timestamp) FILTER (WHERE stream IS NOT TRUE AND status_code < 400)
                           AS non_stream_last_success_at,
                       AVG(ttft_ms) FILTER (WHERE stream = TRUE AND status_code < 400 AND ttft_ms IS NOT NULL)
                           AS stream_avg_ttft_ms,
                       AVG(completion_tokens) FILTER (WHERE status_code < 400 AND completion_tokens IS NOT NULL)
                           AS avg_completion_tokens,
                       AVG(completion_tokens) FILTER (WHERE stream = TRUE AND status_code < 400 AND completion_tokens IS NOT NULL)
                           AS stream_avg_completion_tokens,
                       AVG(completion_tokens) FILTER (WHERE stream IS NOT TRUE AND status_code < 400 AND completion_tokens IS NOT NULL)
                           AS non_stream_avg_completion_tokens
                FROM api_logs
                WHERE timestamp >= NOW() - ($1 || ' minutes')::interval
                  AND user_id IS NOT NULL
                  AND (metadata->>'synthetic_probe') IS DISTINCT FROM 'true'
                  AND provider NOT IN ('', 'router')
                GROUP BY model_id, provider
                """,
                str(window_minutes),
            )

        def _iso(ts: object) -> str | None:
            if ts is None:
                return None
            return ts.isoformat().replace("+00:00", "Z")  # type: ignore[union-attr]

        def _round_or_none(val: object) -> float | None:
            if val is None:
                return None
            return round(float(val), 1)

        result: dict[str, Any] = {}
        for row in rows:
            provider = row["provider"]
            if provider in ("", "router"):
                continue
            key = f"{row['model_id']}::{row['provider']}"
            result[key] = {
                "request_count": row["request_count"],
                "success_count": row["success_count"],
                "last_request_at": _iso(row["last_request_at"]),
                "avg_latency_ms": _round_or_none(row["avg_latency_ms"]),
                "stream_count": row["stream_count"],
                "non_stream_count": row["non_stream_count"],
                "stream_success_count": row["stream_success_count"],
                "non_stream_success_count": row["non_stream_success_count"],
                "stream_last_success_at": _iso(row["stream_last_success_at"]),
                "non_stream_last_success_at": _iso(row["non_stream_last_success_at"]),
                "stream_avg_ttft_ms": _round_or_none(row["stream_avg_ttft_ms"]),
                "avg_completion_tokens": _round_or_none(row["avg_completion_tokens"]),
                "stream_avg_completion_tokens": _round_or_none(row["stream_avg_completion_tokens"]),
                "non_stream_avg_completion_tokens": _round_or_none(
                    row["non_stream_avg_completion_tokens"]
                ),
            }
        return result

    async def get_routewise_bootstrap_rows(
        self,
        *,
        model_ids: list[str],
        since: dt.datetime,
        limit: int | None = None,
    ) -> list[Row]:
        """Fetch recent api_logs rows for RouteWise startup bootstrap.

        Synthetic probe rows (``metadata.synthetic_probe``) are excluded so that
        probe traffic logged via ``log_synthetic_probes`` does not skew the
        replayed latency profiles or cost envelope, matching the live path which
        never records routing observations for probes.
        """
        if not model_ids or (limit is not None and limit <= 0):
            return []
        async with self.pool.acquire() as conn:
            if limit is None:
                rows = await conn.fetch(
                    """
                    SELECT timestamp, model_id, provider, ttft_ms, latency_ms,
                           status_code, error, prompt_tokens, completion_tokens,
                           metadata
                    FROM api_logs
                    WHERE timestamp >= $1
                      AND model_id = ANY($2::text[])
                      AND (metadata->>'synthetic_probe') IS DISTINCT FROM 'true'
                    ORDER BY timestamp ASC
                    """,
                    since,
                    model_ids,
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT *
                    FROM (
                        SELECT timestamp, model_id, provider, ttft_ms, latency_ms,
                               status_code, error, prompt_tokens, completion_tokens,
                               metadata
                        FROM api_logs
                        WHERE timestamp >= $1
                          AND model_id = ANY($2::text[])
                          AND (metadata->>'synthetic_probe') IS DISTINCT FROM 'true'
                        ORDER BY timestamp DESC
                        LIMIT $3
                    ) recent
                    ORDER BY timestamp ASC
                    """,
                    since,
                    model_ids,
                    int(limit),
                )

        normalized: list[Row] = []
        for raw_row in rows:
            row = dict(raw_row)
            metadata = _metadata_dict(row.get("metadata"))
            routewise = metadata.get("routewise")
            endpoint_id = (
                _string_or_none(metadata.get("endpoint_id"))
                or (
                    _string_or_none(routewise.get("primary_provider"))
                    if isinstance(routewise, dict)
                    else None
                )
                or _string_or_none(metadata.get("base_url"))
                or _string_or_none(row.get("provider"))
            )
            failed_attempts = metadata.get("failed_attempts")
            normalized.append(
                {
                    "timestamp": row.get("timestamp"),
                    "model_id": row.get("model_id"),
                    "provider": row.get("provider"),
                    "endpoint_id": endpoint_id,
                    "ttft_ms": row.get("ttft_ms"),
                    "latency_ms": row.get("latency_ms"),
                    "status_code": row.get("status_code"),
                    "error": row.get("error"),
                    "prompt_tokens": row.get("prompt_tokens"),
                    "completion_tokens": row.get("completion_tokens"),
                    "failed_attempts": tuple(
                        item for item in failed_attempts if isinstance(item, dict)
                    )
                    if isinstance(failed_attempts, list)
                    else (),
                }
            )
        return normalized

    async def get_stats(
        self,
        *,
        model_id: str | None = None,
        provider: str | None = None,
        hours: int = 24,
    ) -> list[Row]:
        """Fetch aggregated hourly stats."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT * FROM api_stats_hourly
                WHERE hour >= NOW() - ($1 || ' hours')::interval
                AND ($2::text IS NULL OR model_id = $2)
                AND ($3::text IS NULL OR provider = $3)
                ORDER BY hour DESC
                LIMIT 1000
                """,
                str(hours),
                model_id,
                provider,
            )
        return [dict(r) for r in rows]

    # -- admin: bulk delete --------------------------------------------------

    async def delete_recent_error_requests(self, *, hours: int = 1) -> int:
        """Hard-delete error requests from ``api_logs`` in the last *hours*.

        The error predicate mirrors the admin Recent Requests "errors only"
        filter (``admin_list_recent_requests``) so this clears exactly the
        rows that filter surfaces. Returns the deleted row count parsed from
        the asyncpg command tag.
        """
        hours = max(1, hours)
        async with self.pool.acquire() as conn:
            status = await conn.execute(
                """
                DELETE FROM api_logs
                WHERE timestamp >= NOW() - make_interval(hours => $1::int)
                  AND (error IS NOT NULL OR status_code IS NULL
                       OR status_code < 200 OR status_code >= 400)
                """,
                hours,
            )
        try:
            return int(status.rsplit(" ", 1)[-1])
        except (ValueError, IndexError):
            return 0

    # -- admin: hard-delete user-owned rows ---------------------------------

    async def hard_delete_user_data(self, user_id: str) -> dict[str, int]:
        """Wipe ``api_logs`` and ``email_broadcast_recipients`` for *user_id*.

        Both DELETEs run in a single transaction.  Returns row counts parsed
        from asyncpg command tags.  ``email_broadcast_recipients`` is created
        by ``DatabaseLogger.initialize`` (``database.py``) — when the
        broadcast subsystem hasn't been provisioned, that DELETE is skipped
        via a savepoint and the key is omitted from the result.
        """
        import asyncpg as _asyncpg

        def _row_count(status: str) -> int:
            try:
                return int(status.rsplit(" ", 1)[-1])
            except (ValueError, IndexError):
                return 0

        async with self.pool.acquire() as conn, conn.transaction():
            logs_status = await conn.execute("DELETE FROM api_logs WHERE user_id = $1", user_id)
            recipients_count: int | None
            try:
                # email_broadcast_recipients is created lazily by the broadcast
                # subsystem — savepoint so a missing table doesn't abort the
                # outer transaction.
                async with conn.transaction():
                    recipients_status = await conn.execute(
                        "DELETE FROM email_broadcast_recipients WHERE user_id = $1",
                        user_id,
                    )
                recipients_count = _row_count(recipients_status)
            except _asyncpg.UndefinedTableError as exc:
                logger.debug(
                    "email_broadcast_recipients delete skipped for user %s (table absent): %s",
                    user_id,
                    exc,
                )
                recipients_count = None

        counts = {"api_logs": _row_count(logs_status)}
        if recipients_count is not None:
            counts["email_broadcast_recipients"] = recipients_count
        return counts
