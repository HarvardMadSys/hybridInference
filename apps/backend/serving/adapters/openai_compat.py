"""Generic adapter for OpenAI-compatible APIs (gateways, local VLLM, etc.)."""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import aiohttp

from serving.config.settings import get_settings
from serving.exceptions import UpstreamStreamIdleError
from serving.stream import done_sentinel
from serving.utils.context import notify_traffic_admitted
from serving.utils.logging import get_logger
from serving.utils.messages import flatten_text_content, merge_leading_system_messages
from serving.utils.tokens import estimate_prompt_tokens, estimate_text_tokens

from .base import BaseAdapter, UsageInfo
from .key_pool import KeyPool, KeyPoolExhausted, KeyPoolRoleRestricted, ReleaseOutcome
from .processors import get_processor
from .profiles import (
    ProviderProfile,
    default_chat_path,
    extract_tool_calls_for_profile,
    filter_response_format,
    filter_sampling_params,
    function_call_delta_to_tool_calls,
    get_stream_first_byte_timeout_seconds,
    get_stream_idle_timeout_seconds,
    get_usage_normalizer,
    normalize_messages_for_profile,
    normalize_tools_for_profile,
    resolve_tool_choice_for_profile,
    supports_guided_json,
)
from .upstream_limiter import UpstreamSaturated, UpstreamSlot, acquire_upstream_slot

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator

logger = get_logger(__name__)

_INCOMPLETE_STREAM_ERROR = (
    "Upstream stream ended without a terminal finish_reason or [DONE] sentinel"
)


class UpstreamStreamError(aiohttp.ClientError):
    """An error the upstream reported *inside* an otherwise-200 SSE stream.

    OpenAI-compatible servers -- this gateway among them -- answer a mid-stream
    failure with a ``data: {"error": {...}}`` frame and then close, emitting no
    terminal ``finish_reason`` and no ``[DONE]``. To a reader that only looks
    for terminators, that is indistinguishable from a truncated generation, so
    the adapter raised ``_INCOMPLETE_STREAM_ERROR`` -- a statusless
    ``aiohttp.ClientError`` -- and the frame's own explanation was dropped.

    A statusless exception is the problem. ``endpoint_health.record_failure``
    exempts client errors from the circuit breaker by duck-typing an HTTP
    status off the exception, so a relayed 4xx that the upstream had *already*
    excused as the caller's fault ("you've reached your concurrent request
    limit") arrived here with nothing to classify on, counted against the
    endpoint, and paged -- while the upstream itself had logged
    ``client_error_skip_breaker`` for the very same request.

    Carrying ``status`` restores that classification, and the message carries
    the upstream's own text so the caller reads the real reason instead of a
    framing complaint. ``ClientError`` is the base so the existing mid-stream
    ``except aiohttp.ClientError`` handling (key release, propagation) is
    unchanged.
    """

    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.status = status


# Used when an error frame names no usable HTTP status. Not a client error, so
# the breaker still counts it -- which is what a statusless error frame did
# before this, and the safe direction for an upstream fault we cannot classify.
_UNCLASSIFIED_STREAM_ERROR_STATUS = 502


def _stream_error_status(error: Any) -> int:
    """Extract the HTTP status an upstream error frame reports, or 502."""
    if isinstance(error, dict):
        # ``code`` is the OpenAI-compatible spelling, and it is frequently a
        # string enum ("rate_limit_exceeded") rather than a number -- only an
        # in-range int is a status. ``status`` / ``status_code`` cover the
        # servers that mirror the HTTP code under a different key.
        for key in ("code", "status", "status_code"):
            val = error.get(key)
            if isinstance(val, bool):
                continue
            if isinstance(val, int) and 100 <= val <= 599:
                return val
    return _UNCLASSIFIED_STREAM_ERROR_STATUS


def _stream_error_message(error: Any) -> str:
    """Render an upstream error frame as a single operator-readable line."""
    if isinstance(error, dict):
        msg = error.get("message")
        if isinstance(msg, str) and msg.strip():
            return msg.strip()
    if isinstance(error, str) and error.strip():
        return error.strip()
    return "Upstream reported an error mid-stream"


# Total timeout (seconds) for a non-streaming upstream completion POST. The old
# 120s cap killed long-but-healthy generations (reasoning models, large
# max_tokens) with a generic 502 even while the upstream was still producing.
# A non-streaming response arrives as one body at the end, so an idle/sock_read
# timeout can't distinguish "still generating" from "hung" -- only a generous
# total bound works. A persistent timeout still rotates onto another key, so
# raising the ceiling doesn't weaken failover. Override via env for slow local
# backends. Streaming requests are unaffected (they set their own timeout).
_DEFAULT_COMPLETION_TIMEOUT_S = 600.0
try:
    _COMPLETION_TIMEOUT_S = float(
        os.environ.get("UPSTREAM_COMPLETION_TIMEOUT_S", _DEFAULT_COMPLETION_TIMEOUT_S)
    )
except (TypeError, ValueError):
    logger.warning(
        "Invalid UPSTREAM_COMPLETION_TIMEOUT_S=%r; falling back to %.0fs",
        os.environ.get("UPSTREAM_COMPLETION_TIMEOUT_S"),
        _DEFAULT_COMPLETION_TIMEOUT_S,
    )
    _COMPLETION_TIMEOUT_S = _DEFAULT_COMPLETION_TIMEOUT_S


async def _iter_with_idle_timeout(
    source: AsyncIterator[str],
    idle_timeout: float | None,
    *,
    endpoint_id: str | None = None,
    frames_already_seen: int = 0,
) -> AsyncIterator[str]:
    """Re-yield ``source``, raising if it goes quiet for ``idle_timeout`` seconds.

    The caller has already pulled the first frame out of ``source`` (that is what
    commits the key lease), so every gap this measures is an *inter-chunk* gap.
    Prefill is over by the time the first frame lands, which is precisely why a
    budget this tight is safe here and would not be safe on the socket.

    The clock runs only while awaiting the upstream. A consumer that stops
    pulling -- a slow client, backpressure -- suspends this generator at the
    ``yield``, outside the timed await, so a slow *reader* can never be mistaken
    for a silent *writer*.

    ``wait_for`` cancels the pending ``__anext__`` on expiry, which throws
    ``CancelledError`` into ``stream_post`` at its read and unwinds its
    ``__aexit__`` -- so the wedged connection is torn down rather than left
    hanging on a backend that will never answer. A cancellation arriving from
    *outside* (client disconnect) is a ``BaseException`` that propagates through
    this generator untouched and is never converted into an upstream fault.
    """
    if idle_timeout is None:
        async for item in source:
            yield item
        return

    frames = frames_already_seen
    while True:
        try:
            item = await asyncio.wait_for(source.__anext__(), idle_timeout)
        except StopAsyncIteration:
            return
        except asyncio.TimeoutError as exc:
            raise UpstreamStreamIdleError(
                idle_timeout, endpoint_id=endpoint_id, frames=frames
            ) from exc
        frames += 1
        yield item


def _normalize_text_content(content: Any) -> Any:
    """Normalize structured content blocks into plain text when needed."""
    # Anything that is not a block mapping or a block list is already what the
    # upstream expects (a plain string, or a scalar the schema let through) and
    # is returned as-is rather than stringified. A bare block mapping not
    # wrapped in a list is still valid per the permissive `content: Any`
    # schema, so it flattens too.
    if not isinstance(content, dict | list):
        return content
    return flatten_text_content(content)


def _repaired_tool_arguments(raw: Any) -> str | None:
    """Return a replacement for a tool call's ``arguments``, or None to keep it.

    ``function.arguments`` must be a string holding a JSON *object*. sglang and
    vLLM enforce that on every historical assistant tool call in the request,
    not just the newest one, and reject the whole turn with a 400
    (``Assistant tool call function.arguments must be valid JSON.`` or
    ``... must be a JSON object.``). Because clients replay the transcript, a
    single malformed call poisons that conversation permanently: every later
    turn resends it and fails the same way. Three broken shapes were observed
    in production -- a truncated ``"{"``, a fragment missing its leading brace,
    and an empty string -- all of them produced by a client-side streaming
    tool-call parser, none of them repairable into the arguments the model
    originally meant.

    So normalize rather than validate: anything that does not decode to an
    object becomes ``"{}"``. The substitution only ever turns a guaranteed 400
    into a request the upstream accepts -- every input that would have
    succeeded (a JSON object string) is handed back untouched, by identity, so
    the caller can tell a repair happened. A ``dict`` is re-encoded rather than
    discarded: it is not the shape the OpenAI schema asks for, but several
    clients send the decoded object and its content is intact — unless it will
    not encode to JSON, which puts it back with every other unusable shape.

    This mirrors what the gateway's other ingresses already do --
    ``anthropic_translator._normalize_tool_input``, ``claude_format`` and
    ``gemini`` all coerce a malformed tool input to an empty object. The
    ``/v1/messages`` surface is structurally immune because it re-serializes
    through ``json.dumps``; ``/v1/chat/completions`` forwards the client's
    string verbatim and so needs this.
    """
    if isinstance(raw, dict):
        try:
            # allow_nan=False: `json.loads` accepts the non-standard NaN and
            # Infinity literals, so a decoded object can hold a float that the
            # default `dumps` re-emits bare -- not JSON, and rejected by the
            # very upstream check this function exists to satisfy.
            return json.dumps(raw, allow_nan=False)
        except (TypeError, ValueError, RecursionError):
            return "{}"
    try:
        # Non-strings and blank strings never reach `json.loads`: the guard
        # sends them straight to `parsed = None` to be repaired. The catch
        # covers what parsing a real string can still throw -- malformed JSON,
        # and RecursionError from a deeply nested argument string, which a
        # client can reach in a few kilobytes because the request body's own
        # parse saw `arguments` as an opaque string and never decoded its
        # contents. TypeError is belt and braces for a non-str slipping past
        # the guard.
        parsed = json.loads(raw) if isinstance(raw, str) and raw.strip() else None
    except (json.JSONDecodeError, TypeError, RecursionError):
        parsed = None
    return None if isinstance(parsed, dict) else "{}"


def _sanitize_tool_call_arguments(
    messages: list[dict[str, Any]], *, endpoint_id: str
) -> list[dict[str, Any]]:
    """Repair malformed ``tool_calls[*].function.arguments`` copy-on-write.

    Copy-on-write is load-bearing, not stylistic: the list handed in is the
    router's ``self._messages``, which is written verbatim to
    ``api_logs.prompt`` and re-read on every fallback attempt. Mutating it
    would rewrite the request log to something the client never sent and change
    what the next route in the fallback chain sees. Only the messages and tool
    calls that actually needed a repair are rebuilt; a well-formed list is
    handed back as the same object, like the system-ordering pass above it.

    Every unexpected shape (``tool_calls`` absent, None, or not a list; an
    entry that is not a mapping or carries no ``function``) is skipped rather
    than raised on -- this sits on the request path for every model, so it must
    never be the thing that fails a turn.
    """
    repaired_messages: list[dict[str, Any]] | None = None

    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            continue
        tool_calls = message.get("tool_calls")
        if not isinstance(tool_calls, list):
            continue

        repaired_calls: list[Any] | None = None
        for call_index, tool_call in enumerate(tool_calls):
            if not isinstance(tool_call, dict):
                continue
            function = tool_call.get("function")
            if not isinstance(function, dict):
                continue
            raw = function.get("arguments")
            repaired = _repaired_tool_arguments(raw)
            if repaired is None:
                continue

            if repaired_calls is None:
                repaired_calls = list(tool_calls)
            repaired_calls[call_index] = {
                **tool_call,
                "function": {**function, "arguments": repaired},
            }
            # The producer-side bug outlives the consumer-side symptom: once
            # the 400s stop, this line is the only remaining evidence that a
            # client is still emitting unparseable tool calls. The argument
            # text itself is user data and is never logged -- the id and the
            # function name are enough to find the conversation.
            logger.warning(
                "tool_call_arguments_repaired",
                extra={
                    "event": "tool_call_arguments_repaired",
                    "tool_call_id": tool_call.get("id"),
                    # Not "name": logging refuses an `extra` key that collides
                    # with a LogRecord attribute, and LogRecord.name is the
                    # logger's own name.
                    "tool_name": function.get("name"),
                    "endpoint_id": endpoint_id,
                },
            )

        if repaired_calls is None:
            continue
        if repaired_messages is None:
            repaired_messages = list(messages)
        repaired_messages[index] = {**message, "tool_calls": repaired_calls}

    return messages if repaired_messages is None else repaired_messages


def _caller_role() -> str | None:
    """Return the requesting user's role, or None for an unrestricted caller.

    Set by the API-key auth dependency on the request context. Absent for
    internal callers with no user identity (health probes, warmups, the admin
    playground), which the key pool treats as unrestricted — a reserved key is
    kept away from lower *tiers*, not from the gateway's own machinery.
    """
    from serving.utils import context as req_ctx

    role = req_ctx.get().get(req_ctx.USER_ROLE)
    return role if isinstance(role, str) and role else None


def _pool_affinity_key() -> str:
    """Return the caller identity the key pool binds an upstream key to.

    Prefers ``affinity_key`` — the per-caller value every request surface
    publishes (the API-key hash when authenticated, an IP bucket otherwise) —
    and falls back to ``auth_key_hash`` for any producer that still writes only
    that. ``_anon`` is the last resort for internal traffic with no caller
    identity at all (health probes, warmups, the admin playground); sharing one
    binding is correct there, since there is no caller to keep sticky.
    """
    from serving.utils import context as req_ctx

    ctx = req_ctx.get()
    return ctx.get("affinity_key") or ctx.get("auth_key_hash") or "_anon"


def _key_pool_provider_label(config: Any) -> str:
    """Return a stable operator-facing provider label for key-pool errors/logs."""
    provider = getattr(config, "provider", None)
    if isinstance(provider, str) and provider.strip():
        return provider.strip()

    metadata = getattr(config, "route_metadata", None)
    if isinstance(metadata, dict):
        for key in ("key_provider", "upstream_provider", "route_provider", "provider"):
            value = metadata.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()

    base_url = getattr(config, "base_url", None)
    if isinstance(base_url, str) and base_url.strip():
        try:
            host = (urlsplit(base_url).hostname or "").lower()
        except ValueError:
            host = ""
        for known in ("openrouter", "featherless", "chutes", "minimax", "ollama"):
            if known in host:
                return known
        if host:
            return host

    endpoint_id = getattr(config, "endpoint_id", None)
    if isinstance(endpoint_id, str) and endpoint_id.strip():
        return endpoint_id.strip()
    return "unknown"


def unpack_first_choice(
    response: dict[str, Any], provider: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return ``(choice, message)`` from a non-streaming completion body.

    A 200 does not guarantee a completion. OpenAI-compatible servers answer 200
    with ``choices: []`` when an input content filter trips (Azure-style
    deployments do this, and so do several self-hosted servers), and some return
    200 carrying only an in-band ``error`` object when the upstream they proxy to
    failed after their own request succeeded. Indexing straight into
    ``choices[0]["message"]`` turns those into an IndexError or KeyError raised
    from inside the parser -- which the router does treat as a failure and does
    fall back on, but which reaches the operator as "list index out of range"
    with no provider, no status and no upstream text.

    Raising a described error instead keeps the fallback behavior and makes the
    log say what the upstream actually did. The streaming paths already use the
    ``chunk.get("choices") or []`` form; this is the non-streaming counterpart.

    Args:
        response: Decoded JSON body from the upstream completion call.
        provider: Provider label, for the message only.

    Returns:
        The first choice and its message object.

    Raises:
        ValueError: The body carries no usable choice.
    """
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices:
        upstream_error = response.get("error")
        detail = f": {upstream_error}" if upstream_error else ""
        raise ValueError(
            f"{provider} returned a 200 with no choices{detail} (keys: {sorted(response)})"
        )
    choice = choices[0]
    if not isinstance(choice, dict):
        raise ValueError(f"{provider} returned a non-object choice: {type(choice).__name__}")
    message = choice.get("message")
    if not isinstance(message, dict):
        # A bare `text` choice is the legacy completions shape; accept it rather
        # than failing a response that does carry content.
        text = choice.get("text")
        if isinstance(text, str):
            return choice, {"content": text}
        raise ValueError(f"{provider} returned a choice with no message object")
    return choice, message


class OpenAICompatAdapter(BaseAdapter):
    """Generic adapter for OpenAI-compatible APIs.

    Works with:
    - API gateways (Chutes, Featherless, OpenRouter)
    - Local deployments (VLLM, sglang)
    - Any OpenAI-compatible service

    Configuration is fully driven by ModelConfig fields.
    Optional overrides are applied when present on the config:
    - chat_path (default: /v1/chat/completions)
    - auth_header_name (default: Authorization)
    - auth_format (default: Bearer {api_key})
    - extra_headers / extra_query (dict[str,str])
    """

    def __init__(self, config):
        super().__init__(config)

        # Multi-key API rotation pool (None when single api_key is configured).
        # For RouteWise, this is intentionally modeled as one aggregate
        # endpoint candidate. Per-key scarcity must be represented by separate
        # route entries, not hidden behind this adapter-level pool.
        self._key_pool: KeyPool | None = None
        self._key_pool_provider_label = _key_pool_provider_label(config)
        if config.api_keys:
            self._key_pool = KeyPool(
                keys=list(config.api_keys),
                provider_label=self._key_pool_provider_label,
            )

        logger.info(f"[OpenAICompat] Initialized for {config.id} at {config.base_url}")

        # Store model ID for per-request processor creation (avoids shared mutable state)
        self._processor_model_id = config.provider_model_id or config.id
        self._processor_override = config.processor
        processor_name = get_processor(
            self._processor_model_id, override=self._processor_override
        ).__class__.__name__
        logger.debug(f"[OpenAICompat] Processor type: {processor_name}")

        # Provider profile for usage extraction (e.g. DeepSeek cache hit/miss semantics)
        profile_str = getattr(config, "provider_profile", None)
        try:
            self._usage_profile = (
                ProviderProfile(profile_str) if profile_str else ProviderProfile.DEFAULT
            )
        except ValueError:
            self._usage_profile = ProviderProfile.DEFAULT
        # Route-level statement that a null details block from this endpoint is
        # a reported cache miss rather than "no cache reporting at all".
        self._usage_normalizer = get_usage_normalizer(
            self._usage_profile,
            null_cache_details_means_miss=bool(
                getattr(config, "null_cache_details_means_miss", False)
            ),
        )

    def _no_usable_key_error(self, provider: str, role: str | None) -> KeyPoolExhausted:
        """Build the pre-flight "no key for this caller" error, typed by cause.

        Must classify the same way ``KeyPool.acquire`` does — "could this pool serve
        an unrestricted caller right now" — because the two errors are accounted for
        differently: a role-restricted refusal is exempt from endpoint health, a
        genuine exhaustion is not. ``acquire`` asks its cooldown-aware selector, so
        asking the cooldown-blind ``size`` here would mislabel the case where every
        key is muted (an endpoint problem) as a tier problem, and quietly excuse it
        from the breaker.
        """
        message = (
            f"No active API keys for provider {provider!r} available to "
            f"role={role or 'unrestricted'}"
        )
        pool = self._key_pool
        if pool is not None and role is not None and pool.can_serve_role(None):
            return KeyPoolRoleRestricted(message)
        return KeyPoolExhausted(message)

    def has_capacity_for_role(self, role: str | None) -> bool:
        """Whether this adapter can serve *role* right now.

        Used by surfaces that pick one adapter up front instead of walking the
        router's fallback chain (``/v1/messages``), so tier reservation cannot
        turn an otherwise routable request into a hard failure. A pool-less
        single-``api_key`` adapter carries no reservation and always qualifies.

        Cooldown counts here, unlike in the rotation-bounding ``size`` checks: the
        caller commits to this adapter and has nowhere to rotate, so an adapter
        whose only key for this tier is muted must not be preselected while another
        adapter can serve. Free callers made that newly reachable — a muted shared
        key alongside a healthy reserved one is a pool that ``size`` calls usable
        and ``acquire`` does not.
        """
        pool = self._key_pool
        if pool is None:
            return True
        return pool.can_serve_role(role)

    def ensure_key_pool(self) -> KeyPool | None:
        """Create a pool from the adapter's static keys if it has none yet.

        Used to enforce env-key tombstones even when no runtime key is present:
        a single-``api_key`` adapter otherwise serves ``config.api_key`` via the
        legacy path, bypassing tombstones. Returns the pool (existing or new),
        or None when there is no static key to seed.
        """
        if self._key_pool is not None:
            return self._key_pool
        seed: list[str] = []
        if self.config.api_keys:
            seed.extend(k.strip() for k in self.config.api_keys if isinstance(k, str) and k.strip())
        static = self.config.api_key
        if isinstance(static, str) and static.strip() and static.strip() not in seed:
            seed.append(static.strip())
        if not seed:
            return None
        self._key_pool = KeyPool(keys=seed, provider_label=self._key_pool_provider_label)
        return self._key_pool

    def add_runtime_key(self, key: str) -> bool:
        """Attach a runtime-managed API key, creating the pool if needed.

        Single-key adapters are constructed without a ``KeyPool`` (the legacy
        fast path in ``_post_with_pool``/``_stream_with_pool``). When an admin
        adds a key at runtime (provider-keys dashboard) we lazily promote the
        adapter to a pool seeded with the original static ``api_key`` so both
        the env-configured key and the new key keep serving traffic. The
        request path reads ``self._key_pool`` per request, so the promotion is
        picked up without a restart.

        Keys land untiered: ``dynamic_keys`` owns tier reservations and sweeps the
        pool right after attaching, so a tier written here would only be a second
        writer racing that one.

        Returns True once the key is attached (always, for pool-capable
        adapters).
        """
        normalized = key.strip() if isinstance(key, str) else ""
        if not normalized:
            return False
        if self._key_pool is None:
            seed: list[str] = []
            static = self.config.api_key
            if isinstance(static, str) and static.strip():
                seed.append(static.strip())
            if normalized not in seed:
                seed.append(normalized)
            self._key_pool = KeyPool(keys=seed, provider_label=self._key_pool_provider_label)
            return True
        self._key_pool.add_key(normalized)
        return True

    def _apply_supported_passthrough_params(
        self, payload: dict[str, Any], params: dict[str, Any]
    ) -> None:
        """Forward OpenAI-like params that do not need special normalization."""
        passthrough_params = (
            "top_k",
            "min_p",
            "frequency_penalty",
            "presence_penalty",
            "reasoning_effort",
            "thinking",
            "tool_stream",
        )
        for name in passthrough_params:
            if name in params and name in self.config.supported_params:
                payload[name] = params[name]

    def _prepare_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Normalize request messages for OpenAI-compatible upstreams.

        System-message ordering is normalized for every profile, not just
        strict ones. sglang and vLLM — the local inference servers this
        gateway is built around — reject a list with more than one ``system``
        message, or one that is not first, with a 400 (``System message must
        be at the beginning``), so the whole turn fails rather than degrading.
        Both shapes reach here unrewritten: the northbound Anthropic surface
        folds an inline system message into the top-level field, but a client
        posting straight to ``/v1/chat/completions`` can send several system
        messages, or leave one mid-transcript when it re-sends a transcript.

        Collapsing them here rather than at the northbound schema covers
        every inbound surface at once, and keeps the logged prompt as the
        client sent it — bar a request that used ``developer``, which
        :class:`~serving.schemas.ChatCompletionRequest` has to fold before
        logging to relabel the role. It also puts this path in line with the
        gateway's other
        adapters, which already hoist: ``claude`` gathers system text from
        anywhere in the list into the Anthropic top-level ``system`` field,
        and ``gemini`` into ``systemInstruction``. The ordering pass is a
        no-op on a list already in the accepted shape — it hands back the
        argument itself, no copy — while the profile normalization and
        per-message cleaning below apply as they always have.

        Historical tool-call ``arguments`` are repaired for the same reason and
        on the same terms — see :func:`_sanitize_tool_call_arguments`. This
        method is the single chokepoint both :meth:`chat_completion` and
        :meth:`stream_chat_completion` build their payload through, and
        ``OpenRouterAdapter`` inherits all three, so every OpenAI-compatible
        route is covered by the one call.
        """
        messages = merge_leading_system_messages(messages)
        messages = normalize_messages_for_profile(self._usage_profile, messages)
        messages = _sanitize_tool_call_arguments(
            messages,
            endpoint_id=getattr(self.config, "endpoint_id", None) or self.config.provider,
        )
        return [self._clean_message(msg) for msg in messages]

    def _normalize_tools(self, tools: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
        """Normalize tool definitions for the active provider profile."""
        return normalize_tools_for_profile(self._usage_profile, tools)

    def _resolve_tool_choice(self, tool_choice: Any) -> Any:
        """Resolve the tool_choice value for the active provider profile."""
        return resolve_tool_choice_for_profile(self._usage_profile, tool_choice)

    def _build_stream_timeout(self) -> aiohttp.ClientTimeout | None:
        """Return the socket-level streaming timeout, if one is configured.

        ``sock_read`` is a *byte-anchored* clock: aiohttp restarts it on every
        read, including the one that waits out prefill for the first token. It
        therefore cannot express "give the first token as long as it needs, but
        not the tenth" -- so only the first-byte budget goes here (unbounded by
        default), and the mid-stream idle budget is enforced per frame in
        ``_iter_with_idle_timeout`` where the two can be told apart.
        """
        first_byte_timeout = get_stream_first_byte_timeout_seconds(self._usage_profile)
        if first_byte_timeout is None:
            return None
        try:
            return aiohttp.ClientTimeout(total=None, sock_read=first_byte_timeout)
        except TypeError:
            # Test doubles may expose a simplified ClientTimeout(total=...) shim.
            return SimpleNamespace(total=None, sock_read=first_byte_timeout)

    def _format_passthrough_chunk(self, processed_chunk: dict[str, Any]) -> str:
        """Forward an upstream delta while normalizing model/role fields."""
        chunk_copy = dict(processed_chunk)
        chunk_copy["model"] = self.config.id

        choices = list(chunk_copy.get("choices") or [])
        if choices:
            choice = dict(choices[0])
            delta = dict(choice.get("delta") or {})
            # The router emits the initial role chunk, so avoid duplicating it here.
            delta.pop("role", None)
            choice["delta"] = delta
            choices[0] = choice
            chunk_copy["choices"] = choices

        return f"data: {json.dumps(chunk_copy)}\n\n"

    def _clean_message(self, message: dict[str, Any]) -> dict[str, Any]:
        """Remove None values and normalize text content for API compatibility.

        Structured content (lists of text/image/audio blocks) is forwarded
        verbatim when the model declares any non-text input modality. For
        text-only models the blocks are flattened to a plain string so that
        upstreams which only accept string content don't 400. Block types the
        model cannot handle are rejected earlier by the router pre-flight, so
        anything reaching here is safe to pass through.
        """
        cleaned = {k: v for k, v in message.items() if v is not None}
        modalities = self.config.input_modalities or ["text"]
        supports_structured_content = any(m != "text" for m in modalities)
        if not supports_structured_content and "content" in cleaned:
            cleaned["content"] = _normalize_text_content(cleaned["content"])
        return cleaned

    def _build_headers(self, api_key_override: str | None = None) -> dict[str, str]:
        """Build HTTP headers for request.

        Args:
            api_key_override: when set (multi-key flow), use this key instead
                of ``self.config.api_key``.
        """
        headers = {"Content-Type": "application/json"}
        raw = api_key_override if api_key_override is not None else self.config.api_key
        api_key = raw.strip() if isinstance(raw, str) else raw

        # Add standard OpenAI authentication
        if api_key and getattr(self.config, "use_bearer_auth", True):
            headers["Authorization"] = f"Bearer {api_key}"

        # Custom header formatting (if provided)
        auth_name = getattr(self.config, "auth_header_name", None)
        auth_format = getattr(self.config, "auth_format", None)
        if auth_name and auth_format and api_key:
            headers[auth_name] = auth_format.format(api_key=api_key)

        # Merge any extra headers
        extra_headers = getattr(self.config, "extra_headers", None)
        if isinstance(extra_headers, dict):
            headers.update(extra_headers)

        return headers

    async def _acquire_upstream_slot(self, api_key: str | None) -> UpstreamSlot:
        """Take an outbound concurrency slot for the key this request will use.

        Buckets are per (provider label, key), so a pooled adapter must pass the
        key it just leased rather than the route's configured one — sibling keys
        are usually separate accounts with separate allowances. Local inference
        servers get an inert slot (see ``upstream_limiter.is_local_endpoint``).

        Raises:
            UpstreamSaturated: no slot came free within the acquire timeout.
        """
        return await acquire_upstream_slot(
            self._key_pool_provider_label,
            api_key,
            base_url=self.config.base_url,
        )

    async def _post_with_pool(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        """POST JSON with sequential key-pool rotation on any upstream error.

        When ``self._key_pool`` is None, falls through to the legacy single-key
        path with retries. When set, the pool hands out keys sequentially: a
        request uses one key until it hits a key-specific or transient failure
        (429, 401/402/403, 408/425, 5xx, or a timeout/connection error), at
        which point the loop advances to the next key. Keys already tried are
        passed back to the pool so the advance actually happens: rotation comes
        before muting, so the key that just failed is usually still selectable.
        ``release`` reports ``PROPAGATE`` when there is nothing to rotate to —
        a request-scoped error (other 4xx like 400/422, which fail on every key),
        or a transient error on the last usable key — and the error propagates.
        Pool exhaustion re-raises the last error (or KeyPoolExhausted if none was
        seen yet), which the caller surfaces as an upstream failure for the
        router fallback chain.

        Both branches hold an outbound concurrency slot for the duration of the
        request (see ``upstream_limiter``). In the pooled branch a saturated key
        rotates like any other key-specific failure — a sibling key may have
        room — and only a request that finds every usable key saturated fails
        with ``UpstreamSaturated``.
        """
        if self._key_pool is None:
            headers = self._build_headers()
            slot = await self._acquire_upstream_slot(self.config.api_key)
            # retries=1 => exactly one attempt, NO retry. A chat.completion POST
            # is non-idempotent: re-sending on any ClientError (which includes a
            # response-phase >=400, or a total timeout that fires while the
            # upstream has already generated and billed the response) would
            # double-bill the generation, hammer a 429'd provider ignoring
            # Retry-After, and just add latency on deterministic 4xx. Resilience
            # comes from the router's provider fallback chain, not from blindly
            # re-running the same generation (mirrors the pooled path, which does
            # one json_post per key).
            try:
                notify_traffic_admitted()
                response = await self.http.json_post_with_retry(
                    url=url,
                    json=payload,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=_COMPLETION_TIMEOUT_S),
                    retries=1,
                )
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                slot.release(
                    status_code=e.status if isinstance(e, aiohttp.ClientResponseError) else 0
                )
                raise
            else:
                slot.release(status_code=200)
                return response
            finally:
                # Idempotent; the release that matters ran above. This one only
                # covers an exit neither branch saw — cancellation, most of all.
                slot.release()

        affinity_key = _pool_affinity_key()
        provider = self._key_pool_provider_label
        role = _caller_role()

        # Bound the loop to the number of keys this caller may use. ``tried``
        # is what makes each pass a different key: acquire filters muted and
        # higher-tier-reserved keys, but a key that just failed is left
        # selectable (rotation precedes muting), so without the exclusion the
        # loop would re-acquire it every time.
        max_attempts = self._key_pool.size(role)
        if max_attempts <= 0:
            raise self._no_usable_key_error(provider, role)
        last_error: BaseException | None = None
        tried: set[int] = set()

        for _ in range(max_attempts):
            try:
                api_key, lease = self._key_pool.acquire(affinity_key, role=role, exclude=tried)
            except KeyPoolExhausted as exhausted:
                logger.warning(
                    "key_pool_exhausted",
                    extra={
                        "event": "key_pool_exhausted",
                        "provider": provider,
                        "stage": "acquire",
                    },
                )
                if last_error is not None:
                    raise last_error from exhausted
                raise

            tried.add(lease.key_index)
            logger.debug(
                "key_pool_request",
                extra={
                    "event": "key_pool_request",
                    "provider": provider,
                    "key_index": lease.key_index,
                },
            )

            try:
                slot = await self._acquire_upstream_slot(api_key)
            except UpstreamSaturated as saturated:
                # This key's outbound allowance is full. Rotate rather than
                # fail: sibling keys are separate accounts with their own
                # allowances, and ``tried`` already holds this one so the next
                # pass picks a different key. The lease is released neutrally —
                # nothing was sent, so the key is neither credited with a
                # success nor charged with a failure, and it must not be muted.
                self._key_pool.release(lease, status_code=None)
                last_error = saturated
                continue

            try:
                notify_traffic_admitted()
                headers = self._build_headers(api_key_override=api_key)
                response = await self.http.json_post(
                    url=url,
                    json=payload,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=_COMPLETION_TIMEOUT_S),
                )
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                status = e.status if isinstance(e, aiohttp.ClientResponseError) else 0
                slot.release(status_code=status)
                outcome = self._key_pool.release(lease, status_code=status, tried=tried)
                if outcome is ReleaseOutcome.PROPAGATE:
                    # Either a request-scoped client error (fails identically on
                    # every key) or a transient error on the last usable key —
                    # nothing to rotate to, so propagate.
                    raise
                # Rotated off this key (muted only if it was the last one this
                # caller could have used); advance to the next.
                logger.warning(
                    "key_pool_cooldown",
                    extra={
                        "event": "key_pool_cooldown",
                        "provider": provider,
                        "key_index": lease.key_index,
                        "status": status,
                        # The event name predates rotate-before-mute and is kept
                        # so existing log filters still match, but it now covers
                        # both outcomes. These two say which: ``muted`` for a
                        # dashboard predicate, ``outcome`` for a human reading
                        # the line.
                        "muted": outcome is ReleaseOutcome.MUTED,
                        "outcome": outcome.name.lower(),
                    },
                )
                last_error = e
                continue  # try next key
            except BaseException:
                # Admission bookkeeping is request-local and may still fail
                # (for example, while hashing malformed client metadata). It
                # must never strand either resource already acquired here.
                slot.release()
                self._key_pool.release(lease, status_code=None)
                raise
            else:
                slot.release(status_code=200)
                self._key_pool.release(lease, status_code=200)
            logger.debug(
                "key_pool_active_affinities",
                extra={
                    "event": "key_pool_active_affinities",
                    "provider": provider,
                    "count": self._key_pool.affinity_count(),
                },
            )
            return response

        # Loop exhausted: every key errored in this call, or the pool had no
        # usable keys to begin with (size 0 — e.g. the only key was disabled).
        # Raise the last error if we saw one, else a controlled KeyPoolExhausted
        # so the router gets a clean upstream-failure signal (not an assertion).
        logger.warning(
            "key_pool_exhausted",
            extra={
                "event": "key_pool_exhausted",
                "provider": provider,
                "stage": "all_errored",
            },
        )
        if last_error is not None:
            raise last_error
        raise KeyPoolExhausted(f"No usable API keys for provider {provider!r}")

    async def _open_stream_with_pool(
        self, url: str, payload: dict[str, Any], timeout: Any = None
    ) -> AsyncGenerator[tuple[Any, Any, str, UpstreamSlot], None]:
        """Open a streaming POST with sequential key-pool rotation on opening errors.

        Yields exactly one tuple: ``(stream_iter, lease, first_chunk, slot)``.

        - ``stream_iter`` is the underlying async iterator from ``stream_post``;
          the caller should continue iterating it after processing
          ``first_chunk``.
        - ``lease`` is the ``Lease`` to release according to the stream outcome,
          or ``None`` when no pool is configured.
        - ``first_chunk`` is the first chunk already pulled from the iterator
          (must be processed first by the caller).
        - ``slot`` is the outbound concurrency slot, which the caller must
          release wherever it releases the lease. Concurrency means *generations
          in flight*, not responses opened, so the slot has to live as long as
          the stream does — every exit path included (normal end, mid-stream
          error, client disconnect). Releasing it here, where the body has not
          been read yet, would cap nothing.

        Rotates keys only on status (``ClientResponseError``) opening errors,
        which the client raises before any response-body byte is read: 429/auth
        rotate (muting the key only once there is nothing left to rotate to),
        request-scoped 4xx propagate. A key whose outbound allowance is full
        rotates the same way, without touching the key's own state. A non-status
        I/O error may instead be a disconnect after the upstream returned 2xx and
        began streaming, so it propagates without rotating to avoid re-submitting
        (duplicate generation / double billing). Mid-stream errors are handled by
        the streaming consumer, not here.
        """
        if self._key_pool is None:
            headers = self._build_headers()
            slot = await self._acquire_upstream_slot(self.config.api_key)
            try:
                notify_traffic_admitted()
                stream_iter = self.http.stream_post(
                    url=url, json=payload, headers=headers, timeout=timeout
                )
                first = await stream_iter.__anext__()
            except StopAsyncIteration:
                # Empty stream is incomplete. Yield no lease/chunk so the
                # caller's terminal-signal check raises the upstream error — and
                # hand the slot back here, since nobody downstream will.
                slot.release(status_code=0)
                return
            except BaseException as exc:
                # Nothing was yielded, so the slot is this frame's to return. A
                # status error is the AIMD signal that matters (429); anything
                # else — I/O failure, cancellation — is a neutral release.
                slot.release(
                    status_code=exc.status if isinstance(exc, aiohttp.ClientResponseError) else None
                )
                raise
            yield stream_iter, None, first, slot
            return

        affinity_key = _pool_affinity_key()
        provider = self._key_pool_provider_label
        role = _caller_role()
        max_attempts = self._key_pool.size(role)
        if max_attempts <= 0:
            raise self._no_usable_key_error(provider, role)
        last_error: BaseException | None = None
        tried: set[int] = set()

        for _ in range(max_attempts):
            try:
                api_key, lease = self._key_pool.acquire(affinity_key, role=role, exclude=tried)
            except KeyPoolExhausted as exhausted:
                logger.warning(
                    "key_pool_exhausted",
                    extra={
                        "event": "key_pool_exhausted",
                        "provider": provider,
                        "stage": "stream_acquire",
                    },
                )
                if last_error is not None:
                    raise last_error from exhausted
                raise

            tried.add(lease.key_index)
            logger.debug(
                "key_pool_request",
                extra={
                    "event": "key_pool_request",
                    "provider": provider,
                    "key_index": lease.key_index,
                    "stage": "stream",
                },
            )

            try:
                slot = await self._acquire_upstream_slot(api_key)
            except UpstreamSaturated as saturated:
                # Same rotation as the non-streaming path: this key's outbound
                # allowance is full, a sibling's may not be. Neutral lease
                # release — nothing was sent, so the key is neither credited nor
                # muted.
                self._key_pool.release(lease, status_code=None)
                last_error = saturated
                continue

            try:
                notify_traffic_admitted()
                headers = self._build_headers(api_key_override=api_key)
                stream_iter = self.http.stream_post(
                    url=url, json=payload, headers=headers, timeout=timeout
                )
                first = await stream_iter.__anext__()
            except StopAsyncIteration:
                # A 2xx response with no stream events is incomplete. Do not
                # retry after the upstream accepted the generation request, but
                # count the outcome as a non-HTTP failure so a multi-key route
                # rotates off this key on the next request. Released without
                # ``tried`` deliberately: that is the pool's "cannot rotate"
                # signal, and it is the mute -- not a rotation here -- that moves
                # the next request along.
                slot.release(status_code=0)
                self._key_pool.release(lease, status_code=0)
                logger.debug(
                    "key_pool_active_affinities",
                    extra={
                        "event": "key_pool_active_affinities",
                        "provider": provider,
                        "count": self._key_pool.affinity_count(),
                    },
                )
                return
            except aiohttp.ClientResponseError as e:
                # Status error — raised by the client before any response body
                # byte is read, so re-issuing the request on another key is safe.
                slot.release(status_code=e.status)
                outcome = self._key_pool.release(lease, status_code=e.status, tried=tried)
                if outcome is ReleaseOutcome.PROPAGATE:
                    # Nothing to rotate to: request-scoped error, or a transient
                    # error on the last usable key — propagate.
                    raise
                # Rotated off this key (muted only if it was the last one this
                # caller could have used); open on the next.
                logger.warning(
                    "key_pool_cooldown",
                    extra={
                        "event": "key_pool_cooldown",
                        "provider": provider,
                        "key_index": lease.key_index,
                        "status": e.status,
                        "stage": "stream",
                        # See the non-streaming path: the event name covers both
                        # outcomes, and these two say which.
                        "muted": outcome is ReleaseOutcome.MUTED,
                        "outcome": outcome.name.lower(),
                    },
                )
                last_error = e
                continue
            except (aiohttp.ClientError, asyncio.TimeoutError):
                # A non-status I/O failure may have arrived after the upstream
                # returned 2xx and began streaming the body. ``http.stream_post``
                # deliberately scopes its retry to the connect phase and lets
                # body-phase errors propagate so the upstream is never asked to
                # regenerate (duplicate work / double billing). Mirror that: do
                # not mute or rotate — release the key unchanged and propagate.
                # Neutral (None), NOT 200: crediting a success here would reset
                # the sole-key backoff streak mid-outage.
                slot.release(status_code=None)
                self._key_pool.release(lease, status_code=None)
                raise
            except BaseException:
                # Cancellation before the first chunk: the slot is held for a
                # request that no longer exists, so hand it back here — nothing
                # downstream ever learns this attempt happened. The key pool
                # needs nothing; a neutral release is its no-op.
                slot.release()
                self._key_pool.release(lease, status_code=None)
                raise

            # First chunk read successfully — commit the lease and the slot
            # (caller releases both on stream end).
            yield stream_iter, lease, first, slot
            return

        # Loop exhausted — every key errored on open, or the pool had no usable
        # keys (size 0). Raise the last error if any, else a controlled
        # KeyPoolExhausted rather than an AssertionError.
        logger.warning(
            "key_pool_exhausted",
            extra={
                "event": "key_pool_exhausted",
                "provider": provider,
                "stage": "stream_all_errored",
            },
        )
        if last_error is not None:
            raise last_error
        raise KeyPoolExhausted(f"No usable API keys for provider {provider!r}")

    def _build_url(self) -> str:
        """Build full endpoint URL (standard OpenAI path)."""
        base = (self.config.base_url or "").rstrip("/")

        chat_path = getattr(self.config, "chat_path", None) or default_chat_path(
            self._usage_profile
        )
        if not chat_path and base.endswith("/v1"):
            chat_path = "/chat/completions"
        if not chat_path:
            chat_path = "/v1/chat/completions"
        if not chat_path.startswith("/"):
            chat_path = f"/{chat_path}"
        url = f"{base}{chat_path}"

        extra_query = getattr(self.config, "extra_query", None) or {}
        if not extra_query:
            return url

        parts = urlsplit(url)
        query_pairs = dict(parse_qsl(parts.query, keep_blank_values=True))
        query_pairs.update({str(k): str(v) for k, v in extra_query.items()})
        return urlunsplit(
            (parts.scheme, parts.netloc, parts.path, urlencode(query_pairs), parts.fragment)
        )

    def _get_model_identifier(self) -> str:
        """Get model ID to send to upstream API."""
        return self.config.provider_model_id or self.config.id

    def _apply_upstream_priority(self, payload: dict[str, Any]) -> None:
        """Stamp the router's scheduling priority for priority-scheduling endpoints.

        sglang declares ``priority`` on its OpenAI-compatible request schema and
        honours it when the server was started with
        ``--enable-priority-scheduling``; the value only means anything there, so
        it is emitted for routes that declare the server runs with it and for no
        others. A server without the flag ignores the field, but a remote
        provider that validates its request body strictly would not, and this
        adapter serves both.

        The value comes from the router (via req_ctx), never from the caller: it
        ranks a request by the cost it imposes on a shared replica, which is not
        a number its sender should get to choose. A client that puts ``priority``
        in its request body is already dropped by ``validate_params``.
        """
        if not self.config.priority_scheduling:
            return
        from serving.utils import context as req_ctx

        priority = req_ctx.get().get(req_ctx.UPSTREAM_PRIORITY)
        # Absent means no router published one -- a direct adapter call, a warmup
        # probe. Upstream's own default is the right answer then.
        if isinstance(priority, int) and not isinstance(priority, bool):
            payload["priority"] = priority

    def _augment_payload(self, payload: dict[str, Any], *, stream: bool) -> dict[str, Any]:
        """Subclass extension point for provider-specific payload mutation.

        Called inside chat_completion / stream_chat_completion right before
        the request is dispatched, after profile-level transforms have run.
        Default implementation returns the payload unchanged.
        """
        return payload

    async def chat_completion(
        self, messages: list[dict[str, Any]], **params: Any
    ) -> dict[str, Any]:
        """Execute non-streaming chat completion.

        Args:
            messages: Chat messages in OpenAI format
            **params: Optional parameters (temperature, max_tokens, tools, etc.)

        Returns:
            OpenAI-compatible response dict
        """
        validated = filter_sampling_params(self._usage_profile, self.validate_params(params))

        # Clean messages to remove None fields (some APIs reject them)
        cleaned_messages = self._prepare_messages(messages)

        # Build request payload
        payload = {
            **self.config.extra_body,
            "messages": cleaned_messages,
            "model": self._get_model_identifier(),
            **validated,
        }
        self._apply_supported_passthrough_params(payload, params)

        # Add optional features
        if params.get("tools") and self.config.supports_tools:
            payload["tools"] = self._normalize_tools(params["tools"])
            tool_choice = self._resolve_tool_choice(params.get("tool_choice"))
            if tool_choice is not None:
                payload["tool_choice"] = tool_choice

        filtered_rf = filter_response_format(self._usage_profile, params.get("response_format"))
        if filtered_rf and self.config.supports_structured_output:
            payload["response_format"] = filtered_rf
        self._apply_upstream_priority(payload)
        payload = self._augment_payload(payload, stream=False)

        # Make request
        url = self._build_url()
        logger.debug(f"[OpenAICompat] POST {url} model={payload.get('model', '<omitted>')}")
        _log_payload = get_settings().log_full_payload
        try:
            from serving.config.runtime_settings import get_runtime_settings_instance

            rs = get_runtime_settings_instance()
            _log_payload = await rs.get_bool("log_full_payload")
        except (RuntimeError, KeyError):
            pass
        if _log_payload:
            logger.debug(f"[OpenAICompat] Payload: {payload}")

        response = await self._post_with_pool(url, payload)

        # Process output format (e.g. remove XML tags)
        processor = get_processor(self._processor_model_id, override=self._processor_override)
        processed_response = processor.process_response(response)

        # Parse response
        return self._parse_completion_response(processed_response)

    async def stream_chat_completion(
        self, messages: list[dict[str, Any]], **params: Any
    ) -> AsyncGenerator[str, None]:
        """Execute streaming chat completion.

        Args:
            messages: Chat messages in OpenAI format
            **params: Optional parameters

        Yields:
            SSE-formatted chunks
        """
        validated = filter_sampling_params(self._usage_profile, self.validate_params(params))

        # Clean messages to remove None fields (some APIs reject them)
        cleaned_messages = self._prepare_messages(messages)

        payload = {
            **self.config.extra_body,
            "messages": cleaned_messages,
            "model": self._get_model_identifier(),
            "stream": True,
            **validated,
        }
        self._apply_supported_passthrough_params(payload, params)

        # Add optional features
        if params.get("tools") and self.config.supports_tools:
            payload["tools"] = self._normalize_tools(params["tools"])
            tool_choice = self._resolve_tool_choice(params.get("tool_choice"))
            if tool_choice is not None:
                payload["tool_choice"] = tool_choice

        filtered_rf = filter_response_format(self._usage_profile, params.get("response_format"))
        if filtered_rf and self.config.supports_structured_output:
            payload["response_format"] = filtered_rf
            if supports_guided_json(self._usage_profile) and (
                schema := params.get("response_format", {}).get("schema")
            ):
                payload["guided_json"] = schema
        if getattr(self.config, "include_usage_in_stream", False):
            existing_options = payload.get("stream_options") or {}
            payload["stream_options"] = {**existing_options, "include_usage": True}
        self._apply_upstream_priority(payload)
        payload = self._augment_payload(payload, stream=True)

        url = self._build_url()
        # NOTE: headers are built per-attempt inside _open_stream_with_pool

        # Fresh processor per request — avoids shared mutable state across concurrent streams
        processor = get_processor(self._processor_model_id, override=self._processor_override)

        total_content = ""
        total_tool_text = ""
        finish_reason = "stop"
        upstream_usage: dict[str, Any] | None = None
        prompt_tokens_override: int | None = None
        saw_tool_calls = False
        saw_done = False
        saw_terminal_finish_reason = False

        # Helper to yield formatted chunks from processed data
        def format_and_yield(processed_chunk: dict[str, Any]) -> str | None:
            nonlocal \
                total_content, \
                total_tool_text, \
                finish_reason, \
                upstream_usage, \
                prompt_tokens_override, \
                saw_tool_calls

            choices = processed_chunk.get("choices") or []

            # Capture usage if present
            if processed_chunk.get("usage"):
                upstream_usage = processed_chunk["usage"]
                pt = upstream_usage.get("prompt_tokens")
                if isinstance(pt, int):
                    prompt_tokens_override = pt

            if not choices:
                return None

            delta = choices[0].get("delta", {})

            # Capture finish_reason FIRST (before any early returns)
            fr = choices[0].get("finish_reason")
            if fr:
                finish_reason = fr

            # Accumulate visible content and reasoning independently for the fallback
            content = delta.get("content")
            if isinstance(content, str) and content:
                total_content += content

            reasoning = (
                delta.get("reasoning_content") or delta.get("reasoning") or delta.get("thinking")
            )
            if isinstance(reasoning, str) and reasoning:
                total_content += reasoning

            legacy_tool_calls = function_call_delta_to_tool_calls(
                self._usage_profile, delta.get("function_call")
            )
            if legacy_tool_calls:
                saw_tool_calls = True
                # Accumulate tool-call text so the fallback usage estimate can
                # count tool tokens when the provider omits a usage chunk.
                # Skip malformed entries (non-dict entry or `function` value)
                # rather than crashing the stream.
                for entry in legacy_tool_calls:
                    fn = entry.get("function") if isinstance(entry, dict) else None
                    if not isinstance(fn, dict):
                        continue
                    name = fn.get("name") or ""
                    args = fn.get("arguments") or ""
                    if isinstance(name, str):
                        total_tool_text += name
                    if isinstance(args, str):
                        total_tool_text += args
                chunk_copy = dict(processed_chunk)
                chunk_copy["model"] = self.config.id
                new_choices = list(chunk_copy.get("choices") or [])
                if new_choices:
                    new_choice = dict(new_choices[0])
                    new_delta = dict(new_choice.get("delta") or {})
                    new_delta.pop("role", None)
                    new_delta.pop("function_call", None)
                    new_delta["tool_calls"] = legacy_tool_calls
                    new_choice["delta"] = new_delta
                    new_choices[0] = new_choice
                    chunk_copy["choices"] = new_choices
                return f"data: {json.dumps(chunk_copy)}\n\n"

            has_reasoning = (
                bool(delta.get("reasoning_content"))
                or bool(delta.get("reasoning"))
                or bool(delta.get("thinking"))
            )
            has_tool_calls = isinstance(delta.get("tool_calls"), list) and bool(
                delta.get("tool_calls")
            )

            if has_reasoning or has_tool_calls:
                if has_tool_calls:
                    saw_tool_calls = True
                    # Accumulate tool-call text so the fallback usage estimate
                    # can count tool tokens when the provider omits a usage
                    # chunk. Skip malformed entries (non-dict entry or
                    # `function` value) rather than crashing the stream.
                    for entry in delta.get("tool_calls") or []:
                        fn = entry.get("function") if isinstance(entry, dict) else None
                        if not isinstance(fn, dict):
                            continue
                        name = fn.get("name") or ""
                        args = fn.get("arguments") or ""
                        if isinstance(name, str):
                            total_tool_text += name
                        if isinstance(args, str):
                            total_tool_text += args
                return self._format_passthrough_chunk(processed_chunk)

            if isinstance(content, str) and content:
                return self.format_stream_chunk(
                    content=content,
                    model=self.config.id,
                )

            return None

        stream_timeout = self._build_stream_timeout()

        # Open the stream via the key-pool-aware helper. The helper rotates
        # keys on opening errors BEFORE the first chunk is yielded; once we
        # receive the primed first chunk, the lease is committed for the
        # lifetime of the stream. A mid-stream error has nowhere to rotate to
        # -- the upstream has already begun answering -- so it releases with an
        # error status and no ``tried`` set, taking the pool's mute path and
        # putting the key in cooldown so the next request starts elsewhere.
        primed: str | None = None
        stream_iter: AsyncIterator[str] | None = None
        active_lease = None
        active_slot: UpstreamSlot | None = None

        async for it, lease, first, slot in self._open_stream_with_pool(
            url, payload, timeout=stream_timeout
        ):
            stream_iter = it
            active_lease = lease
            active_slot = slot
            primed = first
            break  # helper yields exactly once

        idle_timeout = get_stream_idle_timeout_seconds(self._usage_profile)
        stream_endpoint_id = getattr(self.config, "endpoint_id", None) or self.config.provider

        async def _drain() -> AsyncIterator[str]:
            if primed is not None:
                yield primed
            if stream_iter is not None:
                # Guarded from here on, not from the first frame: ``primed`` is
                # already in hand, so prefill is behind us and what remains is
                # decode, where silence means a stalled backend rather than a
                # long prompt.
                async for c in _iter_with_idle_timeout(
                    stream_iter,
                    idle_timeout,
                    endpoint_id=stream_endpoint_id,
                    frames_already_seen=1,  # ``primed``
                ):
                    yield c

        # Stream response. Only failures reading from the upstream iterator mute
        # the leased key: an idle timeout or a mid-stream I/O error
        # (aiohttp.ClientError — disconnect, ClientPayloadError) is key-specific
        # or transient. Chunk-processing errors (json / processor / format) are
        # request/model/processor-scoped and propagate WITHOUT muting, and
        # client-side cancellations (CancelledError, GeneratorExit) are
        # BaseExceptions that never mute either.
        stream_error = False
        stream_error_status: int | None = None
        try:
            async for chunk in _drain():
                if not chunk.strip():
                    continue

                if chunk.startswith("data: "):
                    data_str = chunk[6:]

                    if data_str.strip() == "[DONE]":
                        saw_done = True
                        break

                    try:
                        data = json.loads(data_str)

                        raw_choices = data.get("choices") if isinstance(data, dict) else None

                        # An error frame ends the stream: the upstream is
                        # telling us why it stopped, so surface *that* rather
                        # than letting the loop fall out and report the missing
                        # terminator. Guarded on empty choices because a normal
                        # content chunk may legitimately carry a null `error`.
                        if isinstance(data, dict) and data.get("error") and not raw_choices:
                            error = data["error"]
                            raise UpstreamStreamError(
                                _stream_error_message(error),
                                _stream_error_status(error),
                            )
                        if isinstance(raw_choices, list) and any(
                            isinstance(choice, dict) and bool(choice.get("finish_reason"))
                            for choice in raw_choices
                        ):
                            saw_terminal_finish_reason = True

                        # Process output format (returns a list of chunks)
                        processed_chunks = processor.process_stream_chunk(data)

                        for p_chunk in processed_chunks:
                            formatted = format_and_yield(p_chunk)
                            if formatted:
                                yield formatted

                    except json.JSONDecodeError:
                        logger.warning(f"[OpenAICompat] Failed to parse chunk: {data_str[:100]}")

            if not saw_done and not saw_terminal_finish_reason:
                # A clean TCP/HTTP EOF is not proof that generation completed.
                # Surfacing it as an upstream error keeps partial content
                # observable without fabricating a normal final chunk + [DONE].
                raise aiohttp.ClientError(_INCOMPLETE_STREAM_ERROR)
        except UpstreamStreamIdleError as exc:
            # The mid-stream idle detector fired. Mute the key and propagate --
            # the router charges the endpoint a ``stream_exception``, which is
            # the whole point: a wedged replica has to stop being selected.
            stream_error = True
            logger.warning(
                "upstream_stream_idle",
                extra={
                    "event": "upstream_stream_idle",
                    "model": self.config.id,
                    "provider": self.config.provider,
                    "endpoint_id": stream_endpoint_id,
                    "idle_seconds": exc.idle_seconds,
                    "frames": exc.frames,
                },
            )
            raise
        except asyncio.TimeoutError as exc:
            # A socket-level read timeout, i.e. the optional first-byte budget.
            # It used to be swallowed here and the stream capped with a normal
            # flush + [DONE], which handed the caller a truncated answer dressed
            # as a complete one and told the router nothing. Re-raised as the
            # same distinct fault so both timers land in one place.
            stream_error = True
            sock_read = getattr(stream_timeout, "sock_read", None) if stream_timeout else None
            raise UpstreamStreamIdleError(
                float(sock_read) if sock_read else 0.0,
                endpoint_id=stream_endpoint_id,
            ) from exc
        except aiohttp.ClientError as exc:
            # Mid-stream upstream I/O failure (disconnect, ClientPayloadError):
            # mute the key, then propagate to the client as before.
            stream_error = True
            # An error *frame* names a status, and a 4xx one is the caller's
            # request being refused -- the key is healthy and muting it would
            # sideline a working credential over someone else's bad request.
            # Hand the pool the real status and let its own policy decide;
            # everything statusless keeps the historical 0 ("non-HTTP failure").
            if isinstance(exc, UpstreamStreamError):
                stream_error_status = exc.status
            raise
        finally:
            # The outbound slot was held for the whole generation, not just the
            # response open, so it comes back here — on every exit path this
            # ``finally`` covers, client disconnect and mid-stream error
            # included. Released with the same outcome as the lease, and
            # unconditionally on the pool: a pool-less adapter still holds one.
            if active_slot is not None:
                active_slot.release(status_code=0 if stream_error else 200)
            if active_lease is not None and self._key_pool is not None:
                if stream_error:
                    release_status = stream_error_status if stream_error_status else 0
                else:
                    release_status = 200
                self._key_pool.release(active_lease, status_code=release_status)
                logger.debug(
                    "key_pool_active_affinities",
                    extra={
                        "event": "key_pool_active_affinities",
                        "provider": self.config.provider,
                        "count": self._key_pool.affinity_count(),
                    },
                )

        # Flush processor buffer at end of stream
        # This is crucial for buffered tool calls (e.g. GLM XML, Qwen XML)
        final_chunks = processor.flush()
        for p_chunk in final_chunks:
            formatted = format_and_yield(p_chunk)
            if formatted:
                yield formatted

        # Final usage and done sentinel. Some providers stream tool calls but
        # report finish_reason="stop"; normalize those to "tool_calls". Do NOT
        # override a "length" finish -- a tool call truncated at max_tokens must
        # stay "length" so the client sees max_tokens (truncated/unparseable
        # arguments) rather than a spuriously complete tool_use.
        if saw_tool_calls and finish_reason in (None, "", "stop"):
            finish_reason = "tool_calls"
        if upstream_usage:
            usage_info = self._usage_normalizer(upstream_usage)
            final_usage = usage_info.to_dict()
        else:
            usage_info = None
            final_usage = self._build_fallback_usage(
                messages=cleaned_messages,
                total_content=total_content,
                prompt_tokens_override=prompt_tokens_override,
                tool_text=total_tool_text,
            )
        final_chunk_str = self._build_final_chunk(
            usage=final_usage,
            finish_reason=finish_reason,
            usage_info=usage_info,
        )
        yield final_chunk_str
        yield done_sentinel()

    def _build_embeddings_url(self) -> str:
        """Build full endpoint URL for embeddings."""
        base = (self.config.base_url or "").rstrip("/")
        override = self.config.embeddings_path
        if override:
            return f"{base}/{override.lstrip('/')}"
        if base.endswith("/v1"):
            return f"{base}/embeddings"
        return f"{base}/v1/embeddings"

    async def embeddings(self, input_data: str | list[str], **params: Any) -> dict[str, Any]:
        """Execute an embedding request against the upstream API.

        Args:
            input_data: Text string or list of strings to embed.
            **params: Optional parameters (encoding_format, dimensions).

        Returns:
            OpenAI-compatible embedding response dict.
        """
        payload: dict[str, Any] = {
            "model": self._get_model_identifier(),
            "input": input_data,
        }
        if params.get("encoding_format"):
            payload["encoding_format"] = params["encoding_format"]
        if params.get("dimensions"):
            payload["dimensions"] = params["dimensions"]

        url = self._build_embeddings_url()

        logger.debug(f"[OpenAICompat] POST {url} model={payload['model']}")

        return await self._post_with_pool(url, payload)

    def _parse_completion_response(self, response: dict[str, Any]) -> dict[str, Any]:
        """Parse response into OpenAI-compatible format."""
        choice, message = unpack_first_choice(response, self.config.provider)
        tool_calls = extract_tool_calls_for_profile(self._usage_profile, message)

        usage = self._parse_usage(response.get("usage", {}))

        return self.format_response(
            content=message.get("content", ""),
            model=self.config.id,
            usage=usage,
            tool_calls=tool_calls,
            reasoning_content=message.get("reasoning_content"),
            finish_reason=choice.get("finish_reason", "stop"),
        )

    def _parse_usage(self, usage_data: dict[str, Any]) -> UsageInfo:
        """Parse usage information from response using the adapter's profile."""
        return self._usage_normalizer(usage_data)

    def _build_fallback_usage(
        self,
        *,
        messages: list[dict[str, Any]],
        total_content: str,
        prompt_tokens_override: int | None,
        tool_text: str = "",
    ) -> dict[str, int]:
        prompt_tokens = (
            int(prompt_tokens_override)
            if prompt_tokens_override is not None and prompt_tokens_override > 0
            else int(estimate_prompt_tokens(messages))
        )
        # The tool-text estimate intentionally skews low: it counts only the
        # function name + arguments text, not the per-call function-calling
        # scaffolding overhead (~4-11 tokens per call depending on the model).
        completion_tokens = int(estimate_text_tokens(total_content)) + int(
            estimate_text_tokens(tool_text)
        )
        return {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        }

    def _build_final_chunk(
        self,
        *,
        usage: dict[str, Any],
        finish_reason: str,
        usage_info: UsageInfo | None = None,
    ) -> str:
        routing: dict[str, Any] = {
            "provider": self.config.provider,
            "base_url": self.config.base_url,
            "endpoint_id": getattr(self.config, "endpoint_id", None) or self.config.provider,
        }
        if usage_info is not None and usage_info.upstream_cost_usd is not None:
            routing["upstream_cost_usd"] = usage_info.upstream_cost_usd

        chunk = {
            "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": self.config.id,
            "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}],
            "usage": usage,
            "_routing": routing,
        }
        return f"data: {json.dumps(chunk)}\n\n"
