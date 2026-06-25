"""Admin Usage Insights endpoint — LLM-powered analysis of how users prompt.

The admin analytics tab answers *how much* traffic flows through the gateway.
This endpoint answers *what for*: it samples stored ``api_logs`` request
payloads (system-prompt openers + user turns + client user-agents) and asks an
LLM to summarize which harnesses/agents people run, what kinds of tasks they
work on, and any notable usage patterns.

The analysis model is reached over an OpenAI-compatible API. By default that is
freeinference.org itself, with an API key the admin supplies in the request
(never stored server-side).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import aiohttp
from fastapi import APIRouter, Depends, HTTPException, Request

from serving.http import AsyncHTTPClient
from serving.schemas_admin import UsageInsightsRequest, UsageInsightsResponse
from serving.servers.auth import log_admin_action
from serving.servers.deps import get_db_logger, verify_admin_access
from serving.utils.logging import get_logger
from serving.utils.prompt_sampling import (
    as_payload_dict,
    system_opener,
    user_agent_from_metadata,
    user_messages,
)
from serving.utils.request_ip import get_client_ip

logger = get_logger(__name__)

router = APIRouter(prefix="/admin")

_SYSTEM_PROMPT = (
    "You are a product analyst for an LLM inference gateway. You are given a "
    "sample of real API requests that users sent through the gateway. Each "
    "sample includes the calling client's user-agent, the opening of the system "
    "prompt (which usually identifies the coding agent or harness, e.g. Claude "
    "Code, Kilo Code, Cline, Aider, opencode), and the user's message(s).\n\n"
    "Analyze how people are actually using the gateway and write a concise "
    "Markdown report with these sections:\n"
    "1. **Client tools & harnesses** — which agents/SDKs dominate.\n"
    "2. **Use cases** — what kinds of tasks (coding, writing, data, agents, "
    "roleplay, etc.) and, where visible, what projects/domains.\n"
    "3. **Usage patterns** — conversation style, prompt length, multi-turn vs "
    "one-shot, tool use, anything notable.\n"
    "4. **Notable or anomalous behavior** — heavy users, abuse signals, or "
    "surprising requests, if any.\n"
    "5. **Takeaways** — 2-4 short bullets an operator can act on.\n\n"
    "Be specific and ground claims in the samples. Do not invent data. If the "
    "sample is small or skewed, say so. Never reproduce secrets or full "
    "personal data verbatim."
)

# Cap how much text we ship to the analysis model regardless of per-message
# truncation, so a handful of giant prompts can't blow the context window.
_MAX_PROMPT_CHARS = 60_000


async def _resolve_user_id(conn, payload: UsageInsightsRequest) -> str | None:
    """Resolve the optional user scope to a user_id (raises 404 if not found)."""
    if payload.user_id:
        return payload.user_id
    if payload.user_email:
        row = await conn.fetchrow(
            "SELECT id FROM users WHERE lower(trim(email)) = lower(trim($1))",
            payload.user_email,
        )
        if row is None:
            raise HTTPException(404, f"No user found for email {payload.user_email!r}")
        return str(row["id"])
    return None


async def _fetch_samples(conn, user_id: str | None, payload: UsageInsightsRequest) -> list[dict]:
    """Pull recent api_logs rows (optionally scoped to one user) with payloads."""
    # Exclude rows that carry no chat content or that would pollute the report:
    #  - embeddings (metadata.request_type = "embedding") have a payload but no
    #    messages/system, so they render as "<none captured>" and can crowd out
    #    real prompts (same exclusion the request-metrics/export queries use).
    #  - this feature's own analysis calls, which we mark as synthetic probes so
    #    the gateway skips logging them; the metadata.synthetic_probe filter is a
    #    belt-and-braces guard for when log_synthetic_probes is enabled.
    _content_filter = (
        "request_payload IS NOT NULL"
        " AND (metadata->>'request_type') IS DISTINCT FROM 'embedding'"
        " AND (metadata->>'synthetic_probe') IS DISTINCT FROM 'true'"
    )
    if user_id:
        rows = await conn.fetch(
            f"""
            SELECT timestamp, model_id, provider, metadata, request_payload
            FROM api_logs
            WHERE user_id = $1 AND {_content_filter}
            ORDER BY timestamp DESC
            LIMIT $2
            """,
            user_id,
            payload.limit,
        )
    else:
        rows = await conn.fetch(
            f"""
            SELECT timestamp, model_id, provider, metadata, request_payload
            FROM api_logs
            WHERE user_id IS NOT NULL AND {_content_filter}
            ORDER BY timestamp DESC
            LIMIT $1
            """,
            payload.limit,
        )
    samples: list[dict] = []
    for r in rows:
        body = as_payload_dict(r["request_payload"])
        samples.append(
            {
                "timestamp": r["timestamp"].isoformat() if r["timestamp"] else None,
                "model_id": r["model_id"],
                "provider": r["provider"],
                "user_agent": user_agent_from_metadata(r["metadata"]),
                "system_opener": system_opener(body, payload.max_chars),
                "user_messages": user_messages(body, payload.max_chars),
            }
        )
    return samples


def _render_samples(samples: list[dict]) -> tuple[str, int]:
    """Format sampled requests into a compact, bounded text block for the LLM.

    Returns the rendered text and the number of requests that fit the budget.
    """
    blocks: list[str] = []
    total = 0
    used = 0
    for i, s in enumerate(samples, 1):
        lines = [
            f"### Request {i}",
            f"- time: {s['timestamp']}",
            f"- model: {s['model_id']} ({s['provider']})",
            f"- user_agent: {s['user_agent'] or 'unknown'}",
        ]
        if s["system_opener"]:
            lines.append(f"- system prompt opener: {s['system_opener']}")
        if s["user_messages"]:
            joined = "\n".join(f"  - {m}" for m in s["user_messages"])
            lines.append(f"- user message(s):\n{joined}")
        else:
            lines.append("- user message(s): <none captured>")
        block = "\n".join(lines)
        if total + len(block) > _MAX_PROMPT_CHARS:
            # Always include at least the first sample, truncated to the budget,
            # so a single oversized payload still yields a usable analysis.
            if not blocks:
                blocks.append(block[:_MAX_PROMPT_CHARS])
                used += 1
            break
        blocks.append(block)
        total += len(block)
        used += 1
    return "\n\n".join(blocks), used


async def _call_analysis_model(payload: UsageInsightsRequest, content: str) -> str:
    """Call the OpenAI-compatible chat-completions endpoint and return the text."""
    url = payload.base_url.rstrip("/") + "/chat/completions"
    body = {
        "model": payload.model,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": content},
        ],
        "temperature": 0.3,
        "stream": False,
    }
    headers = {
        "Authorization": f"Bearer {payload.api_key}",
        "Content-Type": "application/json",
        # When base_url is the gateway itself (the default), this marks the call
        # as a synthetic probe so it is not persisted to api_logs (and not
        # re-sampled by a later report) or counted against the admin's quota.
        "X-Probe": "synthetic",
        "User-Agent": "freeinference-usage-insights/1.0",
    }
    try:
        data = await AsyncHTTPClient.shared().json_post(
            url,
            json=body,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=120),
        )
    except aiohttp.ClientResponseError as e:
        # base_url is constrained to freeinference.org (see UsageInsightsRequest),
        # so the upstream is trusted; surface a bounded snippet of its error to
        # help the admin diagnose (e.g. bad key, unknown model). The api_key is a
        # request header, not part of the response body, so it is not echoed here.
        detail = getattr(e, "error_body", "") or e.message
        snippet = str(detail)[:500]
        logger.warning("usage-insights analysis upstream error: status=%s", e.status)
        raise HTTPException(
            502, f"Analysis model returned an error (HTTP {e.status}): {snippet}"
        ) from e
    except (aiohttp.ClientError, TimeoutError, json.JSONDecodeError) as e:
        logger.warning("usage-insights analysis call failed: %s", e)
        raise HTTPException(502, "Could not reach the analysis model.") from e

    try:
        text = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as e:
        raise HTTPException(502, "Analysis model returned an unexpected response shape.") from e
    if not isinstance(text, str) or not text.strip():
        raise HTTPException(502, "Analysis model returned an empty response.")
    return text.strip()


@router.post("/usage-insights/analyze", response_model=UsageInsightsResponse)
async def admin_analyze_usage(
    request: Request,
    payload: UsageInsightsRequest,
    _admin_id: str = Depends(verify_admin_access),
    db_logger=Depends(get_db_logger),
) -> UsageInsightsResponse:
    """Sample stored request payloads and summarize how users use the gateway."""
    if not db_logger or not db_logger.pool:
        raise HTTPException(500, "Database not configured")

    async with db_logger.pool.acquire() as conn:
        user_id = await _resolve_user_id(conn, payload)
        samples = await _fetch_samples(conn, user_id, payload)

    if not samples:
        raise HTTPException(404, "No requests with stored payloads found for the chosen scope.")

    rendered, used = _render_samples(samples)
    scope = payload.user_email or user_id or "all users"
    content = (
        f"Here is a sample of {used} request(s) sent through the gateway "
        f"(scope: {scope}). Analyze how the gateway is being used.\n\n{rendered}"
    )

    analysis = await _call_analysis_model(payload, content)

    await log_admin_action(
        db_logger,
        get_client_ip(request),
        "usage_insights_analyze",
        target_user_id=user_id,
        details={"sampled_requests": used, "model": payload.model, "scope": scope},
    )

    return UsageInsightsResponse(
        analysis=analysis,
        model=payload.model,
        sampled_requests=used,
        scope=scope,
        generated_at=datetime.now(timezone.utc),
    )
