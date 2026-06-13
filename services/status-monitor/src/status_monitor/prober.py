"""Probe logic.

A probe sends one small synthetic chat completion to the gateway for a single
model and records whether it succeeded along with latency metrics.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import httpx

    from status_monitor.config import GatewayConfig, Settings


class StreamingProbeError(RuntimeError):
    """Raised when a streaming response carries an in-band error chunk.

    The gateway returns HTTP 200 and emits ``data: {"error": ...}`` when an
    upstream fails after streaming has begun, so HTTP status alone is not a
    reliable success signal.
    """


@dataclass
class ProbeResult:
    """Outcome of a single probe against one model."""

    model_id: str
    ok: bool
    checked_at: str
    latency_ms: float | None = None
    ttft_ms: float | None = None
    completion_tokens: int | None = None
    throughput_tps: float | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Returns a JSON-serializable representation."""
        return asdict(self)


def _now_iso() -> str:
    """Returns the current UTC time as an ISO-8601 string."""
    return datetime.now(UTC).isoformat()


def _build_payload(
    *, model_id: str, settings: Settings, max_tokens: int, streaming: bool
) -> dict[str, Any]:
    """Builds the chat-completion request body for a probe."""
    payload: dict[str, Any] = {
        "model": model_id,
        "messages": [{"role": "user", "content": settings.probe_prompt}],
        "max_tokens": max_tokens,
        "temperature": settings.probe_temperature,
        "stream": streaming,
    }
    if streaming:
        payload["stream_options"] = {"include_usage": True}
    return payload


def _headers(gateway: GatewayConfig) -> dict[str, str]:
    """Builds request headers, including the synthetic-probe marker.

    The gateway recognizes ``X-Probe: synthetic`` to exclude requests from
    request logs, metrics, and cost tracking (see ``completions.py`` and
    ``request_log.py``), so probes must use exactly that header name.
    """
    headers = {
        "Authorization": f"Bearer {gateway.api_key}",
        "Content-Type": "application/json",
    }
    if gateway.probe_header:
        headers["X-Probe"] = gateway.probe_header
    return headers


async def _probe_streaming(
    client: httpx.AsyncClient,
    *,
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    started: float,
) -> tuple[float | None, int]:
    """Runs a streaming probe and returns ``(ttft_ms, completion_tokens)``."""
    ttft_ms: float | None = None
    tokens = 0
    usage_tokens: int | None = None
    saw_terminal = False  # [DONE], finish_reason, or end-of-stream usage
    async with client.stream("POST", url, headers=headers, json=payload) as response:
        response.raise_for_status()
        async for line in response.aiter_lines():
            # SSE comments start with ":"; data lines may use "data:" with or
            # without a leading space ("data: {..}" or "data:{..}").
            if not line or line.startswith(":") or not line.startswith("data:"):
                continue
            body = line[len("data:") :].strip()
            if body == "[DONE]":
                saw_terminal = True
                break
            try:
                chunk = json.loads(body)
            except json.JSONDecodeError:
                continue
            if chunk.get("error"):
                err = chunk["error"]
                message = err.get("message") if isinstance(err, dict) else str(err)
                raise StreamingProbeError(message or "stream error")
            # usage and finish_reason only appear at end-of-stream, so they mark
            # a complete response; a content delta alone does not.
            if chunk.get("usage"):
                usage_tokens = chunk["usage"].get("completion_tokens")
                saw_terminal = True
            choices = chunk.get("choices") or []
            if not choices:
                continue
            if choices[0].get("finish_reason"):
                saw_terminal = True
            delta = choices[0].get("delta", {}) or {}
            # The gateway's own TTFT tracker treats reasoning_content and tool
            # calls as first-token events too, so reasoning-only models don't
            # report a delayed or null TTFT.
            first_token = bool(
                delta.get("content") or delta.get("reasoning_content") or delta.get("tool_calls")
            )
            if first_token:
                if ttft_ms is None:
                    ttft_ms = (time.monotonic() - started) * 1000.0
                tokens += 1
    # A stream that closes without a terminal marker ([DONE]/finish_reason/usage)
    # is truncated — even if some content arrived — so fail rather than report
    # it healthy.
    if not saw_terminal:
        raise StreamingProbeError("incomplete stream (no terminal marker)")
    return ttft_ms, usage_tokens if usage_tokens is not None else tokens


async def _probe_nonstreaming(
    client: httpx.AsyncClient,
    *,
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
) -> int | None:
    """Runs a non-streaming probe and returns the completion token count."""
    response = await client.post(url, headers=headers, json=payload)
    response.raise_for_status()
    data = response.json()
    usage = data.get("usage") or {}
    return usage.get("completion_tokens")


async def _probe_embedding(
    client: httpx.AsyncClient,
    *,
    url: str,
    headers: dict[str, str],
    model_id: str,
    prompt: str,
) -> None:
    """Runs an embeddings probe; raises on any non-2xx response."""
    payload = {"model": model_id, "input": prompt}
    response = await client.post(url, headers=headers, json=payload)
    response.raise_for_status()


async def probe_model(
    client: httpx.AsyncClient,
    *,
    gateway: GatewayConfig,
    settings: Settings,
    model_id: str,
    streaming: bool = True,
    max_tokens: int | None = None,
    kind: str = "chat",
) -> ProbeResult:
    """Sends one dummy request for ``model_id`` and records the result.

    Args:
        client: Shared async HTTP client.
        gateway: Gateway connection settings.
        settings: General probe settings (prompt, temperature, defaults).
        model_id: The model to probe.
        streaming: Whether to use a streaming request (enables TTFT measurement).
        max_tokens: Optional per-model token budget override.
        kind: ``"chat"`` (chat completion) or ``"embedding"`` (embeddings).

    Returns:
        A :class:`ProbeResult` describing success/failure and latency metrics.
    """
    base = gateway.base_url.rstrip("/").removesuffix("/v1")
    headers = _headers(gateway)
    started = time.monotonic()

    if kind == "embedding":
        try:
            async with asyncio.timeout(settings.probe_deadline):
                await _probe_embedding(
                    client,
                    url=f"{base}/v1/embeddings",
                    headers=headers,
                    model_id=model_id,
                    prompt=settings.probe_prompt,
                )
            latency_ms = (time.monotonic() - started) * 1000.0
            return ProbeResult(
                model_id=model_id,
                ok=True,
                checked_at=_now_iso(),
                latency_ms=round(latency_ms, 1),
            )
        except Exception as exc:  # noqa: BLE001 - any failure is a probe failure
            latency_ms = (time.monotonic() - started) * 1000.0
            return ProbeResult(
                model_id=model_id,
                ok=False,
                checked_at=_now_iso(),
                latency_ms=round(latency_ms, 1),
                error=_describe_error(exc),
            )

    url = f"{base}/v1/chat/completions"
    tokens_budget = max_tokens if max_tokens is not None else settings.probe_max_tokens
    payload = _build_payload(
        model_id=model_id, settings=settings, max_tokens=tokens_budget, streaming=streaming
    )
    try:
        async with asyncio.timeout(settings.probe_deadline):
            if streaming:
                ttft_ms, completion_tokens = await _probe_streaming(
                    client, url=url, headers=headers, payload=payload, started=started
                )
            else:
                ttft_ms = None
                completion_tokens = await _probe_nonstreaming(
                    client, url=url, headers=headers, payload=payload
                )
        latency_ms = (time.monotonic() - started) * 1000.0
        # Decode throughput over the post-TTFT window, matching the gateway
        # metric: (tokens - 1) / (latency - ttft). Excludes queueing/TTFT.
        throughput = None
        if completion_tokens and completion_tokens > 1 and ttft_ms is not None and latency_ms > ttft_ms:
            throughput = (completion_tokens - 1) / ((latency_ms - ttft_ms) / 1000.0)
        return ProbeResult(
            model_id=model_id,
            ok=True,
            checked_at=_now_iso(),
            latency_ms=round(latency_ms, 1),
            ttft_ms=round(ttft_ms, 1) if ttft_ms is not None else None,
            completion_tokens=completion_tokens,
            throughput_tps=round(throughput, 2) if throughput else None,
        )
    except Exception as exc:  # noqa: BLE001 - any failure is a probe failure
        latency_ms = (time.monotonic() - started) * 1000.0
        return ProbeResult(
            model_id=model_id,
            ok=False,
            checked_at=_now_iso(),
            latency_ms=round(latency_ms, 1),
            error=_describe_error(exc),
        )


def _describe_error(exc: Exception) -> str:
    """Produces a short, log-friendly description of a probe failure."""
    import httpx

    if isinstance(exc, StreamingProbeError):
        return f"stream error: {exc}"[:200]
    if isinstance(exc, TimeoutError):  # includes asyncio.timeout deadline
        return "timeout"
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    if isinstance(exc, httpx.HTTPError):
        return f"{type(exc).__name__}"
    return f"{type(exc).__name__}: {exc}"[:200]
