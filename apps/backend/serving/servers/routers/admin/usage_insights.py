"""Admin Usage Insights endpoint — LLM-powered analysis of how users prompt.

The admin analytics tab answers *how much* traffic flows through the gateway.
This endpoint answers *what for*: it randomly samples stored ``api_logs``
request payloads (system-prompt openers + user turns + client user-agents) and
asks an LLM to summarize which harnesses/agents people run, what kinds of tasks
they work on, and any notable usage patterns. Sampling at random (rather than
taking the latest N) keeps the report from being dominated by whatever a user
happened to be doing in their most recent session.

The analysis provider is an OpenAI-compatible API — freeinference.org itself by
default. The API key and model are configured once in Admin → Settings (stored
in ``site_settings`` and read server-side), not supplied per request. The
analyze action runs site-wide from the Usage Insights tab or scoped to one user
from that user's admin detail panel.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import aiohttp
from fastapi import APIRouter, Depends, HTTPException, Request

from serving.http import AsyncHTTPClient
from serving.schemas_admin import (
    UsageInsightsRequest,
    UsageInsightsResponse,
    UsageInsightsSettings,
    UsageInsightsSettingsUpdate,
)
from serving.servers.auth import log_admin_action
from serving.servers.deps import (
    get_db_logger,
    get_operational_store,
    verify_admin_access,
)
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

# site_settings keys for the analysis provider config.
_SETTING_API_KEY = "usage_insights_api_key"
_SETTING_MODEL = "usage_insights_model"

# Default analysis model. Must be a chat model that freeinference.org actually
# serves (see GET https://freeinference.org/v1/models) — an unserved id makes the
# upstream return "model not found" and the analysis fails.
_DEFAULT_MODEL = "glm-5.1"

# The analysis call always targets the gateway's own OpenAI-compatible API. It is
# a fixed, trusted host (not user-supplied) so the admin's stored key and the
# sampled prompt content can't be redirected to an arbitrary/internal endpoint.
_BASE_URL = "https://freeinference.org/v1"

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

# Size of the candidate window the sample is drawn from. We randomly sample the
# requested number of rows out of (at most) this many of the user's most recent
# requests, rather than just taking the latest N. Sampling at random gives a
# more representative picture of how someone uses the gateway over time — the
# absolute-latest requests are often one in-progress session and overweight
# whatever that user happened to be doing in the last few minutes. We cap the
# pool instead of randomizing over all of api_logs because an unbounded
# ``ORDER BY random()`` forces a full table scan + sort that gets slow as the
# log grows; for a user with fewer requests than the cap this samples their
# entire history.
_SAMPLE_POOL = 5_000

# Bound the analysis report length. Unbounded generation is the dominant source
# of latency and is what made a slow report exceed the edge proxy timeout (the
# browser then sees an HTML 5xx as a generic "Network error"). A concise report
# fits comfortably under this.
_MAX_OUTPUT_TOKENS = 8192

# Keep the upstream call well under the edge/proxy timeout (Cloudflare ~100s,
# some reverse proxies 60s) so a slow model yields a clean JSON 504 from us
# rather than an edge HTML error page surfaced to the client as "Network error".
_ANALYSIS_TIMEOUT_SEC = 55


def _mask_key(key: str) -> str:
    """Return a non-reversible tail hint of an API key for UI recognition."""
    tail = key[-4:] if len(key) >= 4 else key
    return f"…{tail}"


async def _read_setting(op_store, key: str) -> str | None:
    """Return a non-empty string site_settings value, or None when unset."""
    row = await op_store.get_setting(key)
    if not row:
        return None
    value = row.get("value")
    return value if isinstance(value, str) and value != "" else None


async def _load_provider(op_store) -> tuple[str | None, str]:
    """Resolve the configured (api_key, model). model falls back to the default."""
    api_key = await _read_setting(op_store, _SETTING_API_KEY)
    model = await _read_setting(op_store, _SETTING_MODEL) or _DEFAULT_MODEL
    return api_key, model


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
    """Randomly sample api_logs rows (optionally scoped to one user) with payloads.

    Draws ``payload.limit`` rows uniformly at random from the user's most recent
    ``_SAMPLE_POOL`` requests instead of taking the latest N, so the report
    reflects varied behavior across many sessions rather than just the last one.
    """
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
    # Two-stage: bound a recent candidate window (the inner query, cheap thanks
    # to the timestamp index), then draw the random sample from it.
    if user_id:
        rows = await conn.fetch(
            f"""
            SELECT timestamp, model_id, provider, metadata, request_payload
            FROM (
                SELECT timestamp, model_id, provider, metadata, request_payload
                FROM api_logs
                WHERE user_id = $1 AND {_content_filter}
                ORDER BY timestamp DESC
                LIMIT $2
            ) pool
            ORDER BY random()
            LIMIT $3
            """,
            user_id,
            _SAMPLE_POOL,
            payload.limit,
        )
    else:
        rows = await conn.fetch(
            f"""
            SELECT timestamp, model_id, provider, metadata, request_payload
            FROM (
                SELECT timestamp, model_id, provider, metadata, request_payload
                FROM api_logs
                WHERE user_id IS NOT NULL AND {_content_filter}
                ORDER BY timestamp DESC
                LIMIT $1
            ) pool
            ORDER BY random()
            LIMIT $2
            """,
            _SAMPLE_POOL,
            payload.limit,
        )
    # The random draw returns rows in arbitrary order; sort newest-first so the
    # rendered "Request N" blocks and their timestamps read chronologically.
    dated = [r for r in rows if r["timestamp"] is not None]
    undated = [r for r in rows if r["timestamp"] is None]
    dated.sort(key=lambda r: r["timestamp"], reverse=True)
    rows = dated + undated
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


def _message_text(message: dict) -> str:
    """Extract assistant text from an OpenAI-shape message.

    Handles plain string ``content``, a list of content blocks, and reasoning
    models that leave ``content`` empty but populate ``reasoning_content``.
    """
    content = message.get("content")
    if isinstance(content, str) and content.strip():
        return content
    if isinstance(content, list):
        parts = [
            b.get("text", "")
            for b in content
            if isinstance(b, dict) and b.get("type") in (None, "text")
        ]
        joined = "\n".join(p for p in parts if p)
        if joined.strip():
            return joined
    reasoning = message.get("reasoning_content")
    if isinstance(reasoning, str) and reasoning.strip():
        return reasoning
    return ""


async def _call_analysis_model(api_key: str, model: str, content: str) -> str:
    """Call the OpenAI-compatible chat-completions endpoint and return the text."""
    url = _BASE_URL.rstrip("/") + "/chat/completions"
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": content},
        ],
        "temperature": 0.3,
        "max_tokens": _MAX_OUTPUT_TOKENS,
        "stream": False,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        # The analysis call is handled by the gateway itself. Marking it a
        # synthetic probe keeps it out of api_logs (so a later report can't
        # re-sample it) and off the admin's quota.
        "X-Probe": "synthetic",
        "User-Agent": "freeinference-usage-insights/1.0",
    }
    try:
        data = await AsyncHTTPClient.shared().json_post(
            url,
            json=body,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=_ANALYSIS_TIMEOUT_SEC),
        )
    except aiohttp.ClientResponseError as e:
        # The upstream is the gateway's own trusted domain; surface a bounded
        # snippet of its error to help the admin diagnose (e.g. bad key, unknown
        # model). The api_key is a request header, not echoed in the body.
        detail = getattr(e, "error_body", "") or e.message
        snippet = str(detail)[:500]
        logger.warning("usage-insights analysis upstream error: status=%s", e.status)
        raise HTTPException(
            502, f"Analysis model returned an error (HTTP {e.status}): {snippet}"
        ) from e
    except (TimeoutError, asyncio.TimeoutError) as e:
        # aiohttp raises asyncio.TimeoutError, which is the builtin TimeoutError
        # only on Python 3.11+ — catch both so this works on 3.10 too. Return a
        # clean JSON 504 before the edge proxy would cut the request with a
        # non-JSON 5xx (which the client shows as a generic "Network error").
        logger.warning("usage-insights analysis timed out after %ss", _ANALYSIS_TIMEOUT_SEC)
        raise HTTPException(
            504,
            "The analysis model took too long to respond. Try a smaller sample "
            "size, or pick a faster model in Settings → Usage Insights.",
        ) from e
    except (aiohttp.ClientError, json.JSONDecodeError) as e:
        logger.warning("usage-insights analysis call failed: %s", e)
        raise HTTPException(502, "Could not reach the analysis model.") from e

    try:
        message = data["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as e:
        raise HTTPException(502, "Analysis model returned an unexpected response shape.") from e
    text = _message_text(message) if isinstance(message, dict) else ""
    if not text.strip():
        raise HTTPException(502, "Analysis model returned an empty response.")
    return text.strip()


@router.get("/usage-insights/settings", response_model=UsageInsightsSettings)
async def get_usage_insights_settings(
    _admin_id: str = Depends(verify_admin_access),
    op_store=Depends(get_operational_store),
) -> UsageInsightsSettings:
    """Return the stored analysis-provider config (never the raw API key)."""
    if not op_store:
        raise HTTPException(500, "Database not configured")
    api_key, model = await _load_provider(op_store)
    return UsageInsightsSettings(
        configured=bool(api_key),
        api_key_hint=_mask_key(api_key) if api_key else None,
        model=model,
    )


@router.put("/usage-insights/settings", response_model=UsageInsightsSettings)
async def update_usage_insights_settings(
    request: Request,
    payload: UsageInsightsSettingsUpdate,
    admin_id: str = Depends(verify_admin_access),
    op_store=Depends(get_operational_store),
) -> UsageInsightsSettings:
    """Set or clear the analysis API key and/or model."""
    if not op_store:
        raise HTTPException(500, "Database not configured")

    # Validate before any writes so a rejected model can't leave the api_key
    # half-applied. A whitespace-only model passes Field(min_length=1) but
    # strips to "", which we must not persist.
    model_value: str | None = None
    if payload.model is not None:
        model_value = payload.model.strip()
        if not model_value:
            raise HTTPException(400, "Model name cannot be empty or whitespace only.")

    if payload.api_key is not None:
        key = payload.api_key.strip()
        if key:
            await op_store.set_setting(_SETTING_API_KEY, key, "str", admin_id)
        else:
            await op_store.delete_setting(_SETTING_API_KEY)
    if model_value is not None:
        await op_store.set_setting(_SETTING_MODEL, model_value, "str", admin_id)

    await log_admin_action(
        op_store,
        get_client_ip(request),
        "usage_insights_settings_update",
        details={
            "api_key_changed": payload.api_key is not None,
            "model_changed": payload.model is not None,
        },
    )

    api_key, model = await _load_provider(op_store)
    return UsageInsightsSettings(
        configured=bool(api_key),
        api_key_hint=_mask_key(api_key) if api_key else None,
        model=model,
    )


@router.post("/usage-insights/analyze", response_model=UsageInsightsResponse)
async def admin_analyze_usage(
    request: Request,
    payload: UsageInsightsRequest,
    _admin_id: str = Depends(verify_admin_access),
    db_logger=Depends(get_db_logger),
    op_store=Depends(get_operational_store),
) -> UsageInsightsResponse:
    """Sample stored request payloads and summarize how users use the gateway."""
    if not db_logger or not db_logger.pool:
        raise HTTPException(500, "Database not configured")
    if not op_store:
        raise HTTPException(500, "Database not configured")

    api_key, model = await _load_provider(op_store)
    if not api_key:
        raise HTTPException(
            400,
            "No analysis API key configured. Add a freeinference.org API key in "
            "Admin → Settings → Usage Insights.",
        )

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

    analysis = await _call_analysis_model(api_key, model, content)

    await log_admin_action(
        db_logger,
        get_client_ip(request),
        "usage_insights_analyze",
        target_user_id=user_id,
        details={"sampled_requests": used, "model": model, "scope": scope},
    )

    return UsageInsightsResponse(
        analysis=analysis,
        model=model,
        sampled_requests=used,
        scope=scope,
        generated_at=datetime.now(timezone.utc),
    )
