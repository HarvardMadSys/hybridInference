"""PostgreSQL-backed request/metrics logger using asyncpg.

This module provides a simple database logger that writes API requests,
responses, and usage metrics into PostgreSQL tables. It is intended for
production or staging environments where PostgreSQL is available.

Pure utility functions (``calculate_cost``, etc.) have been moved to
``serving.storage.utils`` so they can be imported without pulling in asyncpg.
"""

import json
from typing import Any

import asyncpg

from serving.storage.erasure_fence import (
    protect_api_logs_insert,
    validate_or_init_fingerprint,
)
from serving.storage.payload_dedup import strip_duplicated_payload_keys
from serving.storage.pools import (
    COMMAND_TIMEOUT_SEC,
    GENERAL_POOL_MAX_SIZE,
    GENERAL_POOL_MIN_SIZE,
    REQUEST_POOL_MAX_SIZE,
    REQUEST_POOL_MIN_SIZE,
)
from serving.storage.utils import (
    calculate_cost,
    conversation_shape,
    json_safe,
    strip_null_bytes,
    user_message_stats,
)
from serving.utils.logging import get_logger
from serving.utils.token_utils import normalize_usage

logger = get_logger(__name__)


def _string_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _parse_command_tag_count(command_tag: str) -> int:
    """Extract the affected row count from an asyncpg command tag."""
    parts = command_tag.split()
    if not parts:
        return 0
    try:
        return int(parts[-1])
    except ValueError:
        return 0


class DatabaseLogger:
    """Asynchronous PostgreSQL logger, and the owner of the gateway's two pools.

    ``pool`` is the general pool everything uses; ``request_pool`` is reserved
    for the lookups a request makes before it is served (see
    :mod:`serving.storage.pools`).
    """

    def __init__(
        self,
        db_config: dict[str, str],
        store_full_prompts: bool = True,
        fence_secret: str | None = None,
    ):
        """Initialize the logger with a DSN/config mapping.

        Args:
            db_config: Mapping with asyncpg pool connection arguments.
            store_full_prompts: If False, prompt and response fields will be NULL.
            fence_secret: Server secret used to derive non-reversible
                erasure-fence digests. Resolved at startup by
                :func:`serving.storage.erasure_fence.resolve_fence_secret`,
                which fails closed if no stable secret is configured.
        """
        self.db_config = db_config
        self.store_full_prompts = store_full_prompts
        self.fence_secret = fence_secret
        # Use Any to avoid mypy issues when asyncpg types are unavailable.
        self.pool: Any | None = None
        self.request_pool: Any | None = None

    async def initialize(self) -> None:
        """Create the connection pools and check the erasure-fence secret.

        The tables come from the schema migrations. With a fence secret, its
        fingerprint is checked against the pinned one (pinned on first use)
        before anything can write a log row.
        """
        self.pool = await asyncpg.create_pool(
            **self.db_config,
            min_size=GENERAL_POOL_MIN_SIZE,
            max_size=GENERAL_POOL_MAX_SIZE,
            command_timeout=COMMAND_TIMEOUT_SEC,
        )
        try:
            self.request_pool = await asyncpg.create_pool(
                **self.db_config,
                min_size=REQUEST_POOL_MIN_SIZE,
                max_size=REQUEST_POOL_MAX_SIZE,
                command_timeout=COMMAND_TIMEOUT_SEC,
            )
            if self.fence_secret:
                async with self.pool.acquire() as conn:
                    await validate_or_init_fingerprint(conn, secret=self.fence_secret)
        except Exception:
            # Closed whatever went wrong. A fingerprint mismatch is a durable
            # privacy boundary, not a transient database startup error, and
            # bootstrap must not retry it while retaining an open pool; any
            # other failure is retried by bootstrap with a fresh call, which
            # would otherwise leave this one's pools open behind it.
            await self.cleanup()
            raise

    async def log_request(
        self,
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

        Args:
            request_id: Unique request identifier.
            model_id: Logical model identifier.
            provider: Provider name for the request.
            prompt: Request messages payload (list of dicts or string).
            response: Provider response payload (dict, string, or None).
            usage: Token usage breakdown.
            latency_ms: End-to-end latency in milliseconds.
            status_code: HTTP status code returned to client.
            error: Optional error message.
            params: Request parameters (temperature, max_tokens, stream, etc.).
            metadata: Additional metadata (user_id, session_id, etc.).
            ttft_ms: Time to first token in milliseconds.
            store_full_content: Override instance default for storing full prompt/response.
                If None, uses self.store_full_prompts. Set False for privacy mode.
            pricing: Model pricing config for cost calculation (per 1M tokens).
            upstream_cost_usd: OpenRouter-reported per-request upstream cost (USD).
                Internal accounting only — orthogonal to user-billed `cost_usd`.
                None for non-OpenRouter routes.
            request_payload: Raw incoming request body (dict). Stored as JSONB
                in the ``request_payload`` column when full-content logging
                is enabled; nulled in privacy mode. Its ``messages``/``tools``
                keys are dropped before the insert — they already have their own
                ``prompt``/``tools`` columns (see
                :mod:`serving.storage.payload_dedup`).
            served_model_id: Model that actually served the request when it
                diverges from the client-requested ``model_id``. Defaults to
                ``model_id``. Served endpoint is recovered from ``metadata``.
        """
        if not self.pool:
            raise RuntimeError("DatabaseLogger not initialized")

        # Same explicit-None rule as the router paths: an all-invalid usage dict
        # must not be restored to its raw form.
        normalized = normalize_usage(usage)
        usage = normalized if normalized is not None else usage

        # Privacy control: use per-request override or instance default
        should_store_full = (
            store_full_content if store_full_content is not None else self.store_full_prompts
        )
        sanitized_error = strip_null_bytes(error)
        sanitized_metadata = strip_null_bytes(metadata)
        sanitized_tools = strip_null_bytes((params or {}).get("tools"))

        if should_store_full:
            # Store full prompt and response text
            sanitized_prompt = strip_null_bytes(prompt)
            sanitized_response = strip_null_bytes(response)
            # Drop from the stored body whatever this same row already writes to
            # the dedicated ``prompt``/``tools`` columns — the duplication that
            # grew request_payload to roughly half of api_logs on disk. The
            # sanitized column values are passed in so the dedup is verified
            # rather than assumed: anything the columns did not capture (a
            # client's per-message cache_control, or tools on an early-error row
            # whose params carried none) is kept. Strip *after* strip_null_bytes
            # so a key obfuscated with null bytes ("mess\x00ages") is caught too,
            # and so both sides of the comparison are sanitized alike.
            sanitized_request_payload = strip_duplicated_payload_keys(
                strip_null_bytes(request_payload),
                stored_messages=sanitized_prompt,
                stored_tools=sanitized_tools,
            )
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
            # Privacy mode: do not persist request/response content.
            prompt_str = None
            response_str = None
            request_payload_str = None

        # Calculate cost based on usage and pricing
        cost_usd = calculate_cost(usage, pricing)
        # Conversation shape is derived metadata (like token counts), so it is
        # always recorded — independent of full-content storage — letting the
        # admin list query read cheap integer columns instead of the payload.
        num_turns, num_user_turns, num_tool_calls = conversation_shape(prompt)
        # Size / entropy / repetition fingerprint of the newest user message,
        # derived from the inbound prompt like conversation shape so it is
        # recorded independent of full-content storage.
        last_user_msg_chars, last_user_msg_entropy, last_user_msg_hash = user_message_stats(prompt)

        served_model = served_model_id or model_id
        served_md = sanitized_metadata if isinstance(sanitized_metadata, dict) else {}
        routewise_md = served_md.get("routewise")
        served_endpoint = (
            _string_or_none(served_md.get("endpoint_id"))
            or (
                _string_or_none(routewise_md.get("primary_provider"))
                if isinstance(routewise_md, dict)
                else None
            )
            or _string_or_none(served_md.get("base_url"))
            or _string_or_none(provider)
        )

        # Determine the two account identifiers this row could carry. Either
        # would identify the deleted account in api_logs after a hard-delete,
        # so both must be checked against the erasure fence (issue #1421).
        _row_user_id = (sanitized_metadata or {}).get("user_id")
        _row_credential_owner = (sanitized_metadata or {}).get("credential_owner_id")

        async with self.pool.acquire() as conn, conn.transaction():
            # Erasure fence check (issue #1421): if any account this row would
            # identify has been hard-deleted, do not create an identifying row.
            _row_user_id = (sanitized_metadata or {}).get("user_id")
            _row_credential_owner = (sanitized_metadata or {}).get("credential_owner_id")
            if await protect_api_logs_insert(
                conn,
                fence_secret=self.fence_secret,
                user_id=_row_user_id,
                credential_owner_id=_row_credential_owner,
            ):
                logger.info(
                    "log_request suppressed: erasure fence hit for request %s",
                    request_id,
                )
                return

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
                    served_model_id, served_endpoint_id, agent_job_id, batch_job_id
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
                    $34, $35, $36, $37
                )
                ON CONFLICT (request_id) DO NOTHING
                """,
                request_id,
                model_id,
                provider,
                # Request parameters
                (params or {}).get("temperature"),
                (params or {}).get("top_p"),
                (params or {}).get("max_tokens"),
                (params or {}).get("seed"),
                (params or {}).get("stream"),
                # Performance metrics
                ttft_ms,
                latency_ms,
                # Token usage
                (usage or {}).get("prompt_tokens"),
                (usage or {}).get("completion_tokens"),
                (usage or {}).get("reasoning_tokens"),
                (usage or {}).get("total_tokens"),
                # Cache and cost
                (usage or {}).get("cache_read_tokens"),
                (usage or {}).get("cache_write_tokens"),
                cost_usd,
                # Content
                prompt_str,
                response_str,
                request_payload_str,
                # Metadata
                status_code,
                sanitized_error,
                (sanitized_metadata or {}).get("user_id"),
                (sanitized_metadata or {}).get("session_id"),
                json.dumps(json_safe(sanitized_metadata)) if sanitized_metadata else None,
                json.dumps(json_safe(sanitized_tools)) if sanitized_tools else None,
                upstream_cost_usd,
                # Conversation shape
                num_turns,
                num_user_turns,
                num_tool_calls,
                # Newest user message fingerprint
                last_user_msg_chars,
                last_user_msg_entropy,
                last_user_msg_hash,
                # Served model/endpoint (queryable routing identity)
                served_model,
                served_endpoint,
                # Cloud agent sandbox job attribution (issue #1041)
                (sanitized_metadata or {}).get("agent_job_id"),
                # Batch inference attribution (/v1/batches)
                (sanitized_metadata or {}).get("batch_job_id"),
            )

    async def get_model_activity(self, window_minutes: int = 10) -> dict[str, Any]:
        """Aggregate recent real-user traffic per (model_id, provider).

        Returns a dict keyed by ``"model_id::provider"`` with per-route stats.
        Synthetic probes (``user_id IS NULL``) are excluded.
        """
        if not self.pool:
            return {}
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
                  AND provider NOT IN ('', 'router')
                GROUP BY model_id, provider
                """,
                str(window_minutes),
            )
        result: dict[str, Any] = {}
        for row in rows:
            provider = row["provider"]
            if provider in ("", "router"):
                continue
            key = f"{row['model_id']}::{row['provider']}"
            last_req = row["last_request_at"]

            def _iso(ts: object) -> str | None:
                if ts is None:
                    return None
                return ts.isoformat().replace("+00:00", "Z")  # type: ignore[union-attr]

            def _round_or_none(val: object) -> float | None:
                if val is None:
                    return None
                return round(float(val), 1)

            result[key] = {
                "request_count": row["request_count"],
                "success_count": row["success_count"],
                "last_request_at": _iso(last_req),
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

    async def get_stats(
        self, model_id: str | None = None, provider: str | None = None, hours: int = 24
    ) -> list[dict[str, Any]]:
        """Fetch aggregated hourly stats for the given time window."""
        if not self.pool:
            return []
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

    async def cleanup(self) -> None:
        """Close the connection pools that were opened."""
        pool, request_pool = self.pool, self.request_pool
        self.pool = self.request_pool = None
        try:
            if request_pool:
                await request_pool.close()
        finally:
            if pool:
                await pool.close()
