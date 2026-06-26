from __future__ import annotations

import asyncio
import os
import random
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

import pytest

from routing.executor import (
    AllCircuitsOpenError,
    ProviderPinError,
    RouteConfig,
    RouteExecutor,
    _has_non_empty_content,
)
from serving.adapters.base import BaseAdapter, ModelConfig

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


def _cfg(mid: str, provider: str = "p") -> ModelConfig:
    return ModelConfig(
        id=mid,
        name=mid,
        provider=provider,
        base_url="http://test",
        context_length=8192,
        max_output_length=4096,
    )


class _StaticWeightResolver:
    def __init__(self, overrides: dict[str, dict[str, float]]) -> None:
        self.overrides = overrides

    async def get_for_model(self, model_id: str) -> dict[str, float]:
        return self.overrides.get(model_id, {})


class _EchoAdapter(BaseAdapter):
    async def chat_completion(self, messages: list[dict[str, Any]], **params) -> dict[str, Any]:
        text = params.get("content", "ok")
        return self.format_response(content=text, model=self.config.id)

    async def stream_chat_completion(
        self, messages: list[dict[str, Any]], **params
    ) -> AsyncGenerator[str, None]:  # pragma: no cover - streaming covered elsewhere
        yield self.format_stream_chunk(model=self.config.id, content="ok")


class _FailAdapter(BaseAdapter):
    async def chat_completion(self, messages: list[dict[str, Any]], **params) -> dict[str, Any]:
        raise RuntimeError("fail")

    async def stream_chat_completion(
        self, messages: list[dict[str, Any]], **params
    ) -> AsyncGenerator[str, None]:
        raise RuntimeError("fail")
        yield  # make this an async generator  # pragma: no cover


@pytest.mark.unit
def test_weighted_selection_distribution():
    exe = RouteExecutor()
    a = _EchoAdapter(_cfg("m", provider="A"))
    b = _EchoAdapter(_cfg("m", provider="B"))
    exe.register_route("m", [(a, 0.8), (b, 0.2)])

    # Seed RNG for reproducibility
    random.seed(42)
    picks = {"A": 0, "B": 0}
    for _ in range(10000):
        chosen = exe._select_adapter("m")  # type: ignore[attr-defined]
        assert chosen is not None
        picks[chosen.config.provider] += 1

    frac_a = picks["A"] / 10000
    frac_b = picks["B"] / 10000
    # Allow small tolerance around target weights
    assert 0.77 <= frac_a <= 0.83
    assert 0.17 <= frac_b <= 0.23


@pytest.mark.unit
def test_weighted_selection_chi_square():
    """Validate distribution with a chi-square test at 95% confidence without SciPy."""
    exe = RouteExecutor()
    a = _EchoAdapter(_cfg("m", provider="A"))
    b = _EchoAdapter(_cfg("m", provider="B"))
    exe.register_route("m", [(a, 0.8), (b, 0.2)])

    random.seed(7)
    n = 10000
    picks = {"A": 0, "B": 0}
    for _ in range(n):
        chosen = exe._select_adapter("m")  # type: ignore[attr-defined]
        assert chosen is not None
        picks[chosen.config.provider] += 1

    observed = [picks["A"], picks["B"]]
    expected = [0.8 * n, 0.2 * n]
    chi2 = sum(((o - e) ** 2) / e for o, e in zip(observed, expected, strict=False))
    # df=1, alpha=0.05 => critical value ~3.841
    assert chi2 < 3.841


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fallback_on_primary_failure():
    exe = RouteExecutor()
    primary = _FailAdapter(_cfg("m", provider="primary"))
    backup = _EchoAdapter(_cfg("m", provider="backup"))
    exe.register_route("m", [(primary, 0.9), (backup, 0.1)])

    # Force primary selection by fixing RNG
    random_state = random.random
    try:
        random.random = lambda: 0.01  # always pick primary (weight 0.9)
        resp = await exe.chat_completion("m", messages=[{"role": "user", "content": "hi"}])
    finally:
        random.random = random_state
    assert resp["choices"][0]["message"]["content"] == "ok"
    assert resp["_routing"]["provider"] == "backup"
    assert resp["_routing"].get("fallback") is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fallback_response_records_failed_primary_attempt():
    exe = RouteExecutor()
    primary = _FailAdapter(_cfg("m", provider="primary"))
    backup = _EchoAdapter(_cfg("m", provider="backup"))
    exe.register_route("m", [(primary, 0.9), (backup, 0.1)])

    random_state = random.random
    try:
        random.random = lambda: 0.01
        resp = await exe.chat_completion("m", messages=[{"role": "user", "content": "hi"}])
    finally:
        random.random = random_state

    assert resp["_routing"]["failed_attempts"] == [
        {
            "provider": "primary",
            "endpoint_id": "primary",
            "error_type": "RuntimeError",
            "error": "fail",
        }
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fallback_failure_uses_current_adapter_endpoint_for_failure_recording(monkeypatch):
    from routing import routers as routers_mod

    exe = RouteExecutor()
    primary = _FailAdapter(_cfg("m", provider="primary"))
    bad_fallback = _EchoAdapter(_cfg("m", provider="bad"))
    backup = _EchoAdapter(_cfg("m", provider="backup"))
    exe.register_route("m", [(primary, 0.8), (bad_fallback, 0.1), (backup, 0.1)])

    recorded: list[str] = []
    original_on_failure = exe._on_failure  # type: ignore[attr-defined]

    def record_failure(
        endpoint_id: str,
        *,
        reason: str,
        detail: str | None = None,
        exc: BaseException | None = None,
    ) -> None:
        recorded.append(endpoint_id)
        original_on_failure(endpoint_id, reason=reason, detail=detail, exc=exc)

    original_push = routers_mod.req_ctx.push

    @contextmanager
    def raise_for_bad_provider(**values: Any):
        if values.get("provider") == "bad":
            raise RuntimeError("context setup failed")
        with original_push(**values):
            yield

    random_state = random.random
    try:
        random.random = lambda: 0.01
        monkeypatch.setattr(routers_mod.req_ctx, "push", raise_for_bad_provider)
        exe._on_failure = record_failure  # type: ignore[method-assign]
        await exe.chat_completion("m", messages=[{"role": "user", "content": "hi"}])
    finally:
        random.random = random_state
        exe._on_failure = original_on_failure  # type: ignore[method-assign]

    assert recorded[:2] == ["primary", "bad"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_no_route_configured_raises():
    exe = RouteExecutor()
    with pytest.raises(ValueError):
        await exe.chat_completion("unknown", messages=[{"role": "user", "content": "hi"}])


@pytest.mark.unit
def test_all_circuits_open_raises_all_circuits_open_error():
    """Regression: when every circuit breaker is open, _select_adapter raises
    AllCircuitsOpenError so the API layer can return 503 Service Unavailable
    rather than a generic 500."""
    exe = RouteExecutor()
    a = _EchoAdapter(_cfg("m", provider="A"))
    b = _EchoAdapter(_cfg("m", provider="B"))
    exe.register_route("m", [(a, 0.5), (b, 0.5)])

    # Force every circuit breaker for this route into the OPEN state so that
    # ``allow_request()`` returns False for all candidates, leaving ``allowed``
    # empty inside ``_select_adapter``.
    # Trigger circuit population by calling _select_adapter once successfully.
    assert exe._select_adapter("m") is not None  # type: ignore[attr-defined]
    for cb in exe._circuits.values():  # type: ignore[attr-defined]
        cb.state = "open"
        cb.last_opened = float("inf")  # never cool down within this test

    with pytest.raises(AllCircuitsOpenError):
        exe._select_adapter("m")  # type: ignore[attr-defined]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_concurrent_route_selection():
    """Verify concurrent selections do not race or corrupt state."""
    exe = RouteExecutor()
    echo = _EchoAdapter(_cfg("m"))
    exe.register_route("m", [(echo, 1.0)])

    async def do_one():
        r = await exe.chat_completion("m", messages=[{"role": "user", "content": "x"}])
        return r["choices"][0]["message"]["content"]

    results = await asyncio.gather(*(do_one() for _ in range(100)))
    assert all(res == "ok" for res in results)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_multiple_fallback_chain():
    """Primary and secondary fail; tertiary succeeds."""
    exe = RouteExecutor()
    p1 = _FailAdapter(_cfg("m", provider="p1"))
    p2 = _FailAdapter(_cfg("m", provider="p2"))
    p3 = _EchoAdapter(_cfg("m", provider="p3"))
    exe.register_route("m", [(p1, 0.6), (p2, 0.3), (p3, 0.1)])

    # Force selecting the primary first
    orig = exe._select_adapter  # type: ignore[attr-defined]
    try:
        exe._select_adapter = lambda model_id, **kw: p1  # type: ignore[assignment]
        resp = await exe.chat_completion("m", messages=[{"role": "user", "content": "hi"}])
    finally:
        exe._select_adapter = orig  # type: ignore[assignment]

    assert resp["choices"][0]["message"]["content"] == "ok"
    assert resp["_routing"]["provider"] == "p3"
    assert resp["_routing"].get("fallback") is True


@pytest.mark.unit
def test_alias_shares_route_config():
    """Aliases share the same RouteConfig; mutations affect both."""
    exe = RouteExecutor()
    a = _EchoAdapter(_cfg("m", provider="A"))
    b = _EchoAdapter(_cfg("m", provider="B"))
    exe.register_route("m", [(a, 0.8), (b, 0.2)], aliases=["m-alias"])

    # Same object reference
    assert exe.routes["m"] is exe.routes["m-alias"]

    # Mutate in-place (as RoutingManager.apply does)
    exe.routes["m"].adapters = [(a, 1.0), (b, 0.0)]

    # Alias reflects the change
    assert exe.routes["m-alias"].adapters == [(a, 1.0), (b, 0.0)]
    assert exe.routes["m"] is exe.routes["m-alias"]


@pytest.mark.unit
@pytest.mark.perf
@pytest.mark.skipif(
    os.getenv("RUN_PERF") != "1",
    reason="Performance tests are disabled by default (set RUN_PERF=1 to enable)",
)
def test_route_selection_performance():
    """Ensure adapter selection is fast enough for basic regression budgets.

    Wall-clock budget; flaky on shared CI runners, so gated behind RUN_PERF=1.
    """
    exe = RouteExecutor()
    a = _EchoAdapter(_cfg("m", provider="A"))
    b = _EchoAdapter(_cfg("m", provider="B"))
    exe.register_route("m", [(a, 0.8), (b, 0.2)])

    import time

    start = time.perf_counter()
    for _ in range(10000):
        _ = exe._select_adapter("m")  # type: ignore[attr-defined]
    elapsed = time.perf_counter() - start
    assert elapsed < 0.5, f"10k selections took {elapsed:.3f}s (budget: 0.5s)"


# ---------------------------------------------------------------------------
# _has_non_empty_content tests
# ---------------------------------------------------------------------------


class TestHasNonEmptyContent:
    """Regression tests for the TTFT-gating helper."""

    def test_text_content(self):
        chunk = 'data: {"choices": [{"delta": {"content": "hello"}}]}\n\n'
        assert _has_non_empty_content(chunk) is True

    def test_empty_content(self):
        chunk = 'data: {"choices": [{"delta": {"content": ""}}]}\n\n'
        assert _has_non_empty_content(chunk) is False

    def test_tool_calls_delta(self):
        """tool_calls in delta should be treated as content (blocks stream fallback)."""
        chunk = (
            'data: {"choices": [{"delta": {"tool_calls": '
            '[{"index": 0, "function": {"arguments": "{\\"x\\": 1}"}}]}}]}\n\n'
        )
        assert _has_non_empty_content(chunk) is True

    def test_empty_tool_calls(self):
        chunk = 'data: {"choices": [{"delta": {"tool_calls": []}}]}\n\n'
        assert _has_non_empty_content(chunk) is False

    def test_done_sentinel(self):
        assert _has_non_empty_content("data: [DONE]\n\n") is False

    def test_no_choices(self):
        chunk = 'data: {"choices": []}\n\n'
        assert _has_non_empty_content(chunk) is False

    def test_null_delta(self):
        chunk = 'data: {"choices": [{"delta": {}}]}\n\n'
        assert _has_non_empty_content(chunk) is False


# ---------------------------------------------------------------------------
# admin_only tests
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# pin_provider tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_pin_selects_matching_provider():
    """pin_provider deterministically selects the matching adapter."""
    exe = RouteExecutor()
    a = _EchoAdapter(_cfg("m", provider="zai"))
    b = _EchoAdapter(_cfg("m", provider="ollama"))
    exe.register_route("m", [(a, 0.8), (b, 0.2)])

    chosen = exe._select_adapter("m", pin_provider="ollama")
    assert chosen is b


@pytest.mark.unit
def test_pin_skips_zero_weight():
    """pin_provider must not route to a weight=0 (disabled) adapter."""
    exe = RouteExecutor()
    a = _EchoAdapter(_cfg("m", provider="zai"))
    b = _EchoAdapter(_cfg("m", provider="featherless"))
    exe.register_route("m", [(a, 1.0), (b, 0.0)])

    chosen = exe._select_adapter("m", pin_provider="featherless")
    assert chosen is None


@pytest.mark.unit
def test_runtime_weight_override_replaces_raw_yaml_weight():
    resolver = _StaticWeightResolver({"m": {"b-endpoint": 4.0}})
    exe = RouteExecutor(weight_override_resolver=resolver)
    a_cfg = _cfg("m", provider="A")
    a_cfg.endpoint_id = "a-endpoint"
    b_cfg = _cfg("m", provider="B")
    b_cfg.endpoint_id = "b-endpoint"
    a = _EchoAdapter(a_cfg)
    b = _EchoAdapter(b_cfg)
    exe.register_route("m", [(a, 1.0), (b, 2.0)])

    random_state = random.random
    try:
        random.random = lambda: 0.70
        chosen = exe._select_adapter("m")
    finally:
        random.random = random_state

    assert chosen is b


@pytest.mark.unit
def test_runtime_weight_override_zero_excludes_route_and_pin():
    resolver = _StaticWeightResolver({"m": {"b-endpoint": 0.0}})
    exe = RouteExecutor(weight_override_resolver=resolver)
    a_cfg = _cfg("m", provider="A")
    a_cfg.endpoint_id = "a-endpoint"
    b_cfg = _cfg("m", provider="B")
    b_cfg.endpoint_id = "b-endpoint"
    a = _EchoAdapter(a_cfg)
    b = _EchoAdapter(b_cfg)
    exe.register_route("m", [(a, 1.0), (b, 1.0)])

    for _ in range(25):
        assert exe._select_adapter("m") is a

    assert exe._select_adapter("m", pin_provider="b-endpoint") is None


@pytest.mark.unit
def test_runtime_weight_override_uses_canonical_model_id_for_aliases():
    resolver = _StaticWeightResolver({"m": {"b-endpoint": 0.0}})
    exe = RouteExecutor(weight_override_resolver=resolver)
    a_cfg = _cfg("m", provider="A")
    a_cfg.endpoint_id = "a-endpoint"
    b_cfg = _cfg("m", provider="B")
    b_cfg.endpoint_id = "b-endpoint"
    a = _EchoAdapter(a_cfg)
    b = _EchoAdapter(b_cfg)
    exe.register_route("m", [(a, 1.0), (b, 1.0)], aliases=["m-alias"])

    random_state = random.random
    try:
        random.random = lambda: 0.90
        chosen = exe._select_adapter("m-alias")
    finally:
        random.random = random_state

    assert chosen is a


@pytest.mark.unit
def test_pin_miss_returns_none():
    """pin_provider with unknown name returns None."""
    exe = RouteExecutor()
    a = _EchoAdapter(_cfg("m", provider="zai"))
    exe.register_route("m", [(a, 1.0)])

    chosen = exe._select_adapter("m", pin_provider="nonexistent")
    assert chosen is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_pin_miss_raises_provider_pin_error():
    """chat_completion with unmatched pin raises ProviderPinError, not generic ValueError."""
    exe = RouteExecutor()
    a = _EchoAdapter(_cfg("m", provider="zai"))
    exe.register_route("m", [(a, 1.0)])

    with pytest.raises(ProviderPinError, match="nonexistent"):
        await exe.chat_completion(
            "m", messages=[{"role": "user", "content": "hi"}], pin_provider="nonexistent"
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_pin_no_fallback_on_failure():
    """When pin_provider is set and the pinned adapter fails, must NOT fallback."""
    exe = RouteExecutor()
    primary = _FailAdapter(_cfg("m", provider="zai"))
    backup = _EchoAdapter(_cfg("m", provider="ollama"))
    exe.register_route("m", [(primary, 0.8), (backup, 0.2)])

    with pytest.raises(RuntimeError, match="fail"):
        await exe.chat_completion(
            "m", messages=[{"role": "user", "content": "hi"}], pin_provider="zai"
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_pin_success():
    """pin_provider routes to the correct adapter and returns its response."""
    exe = RouteExecutor()
    a = _EchoAdapter(_cfg("m", provider="zai"))
    b = _EchoAdapter(_cfg("m", provider="ollama"))
    exe.register_route("m", [(a, 0.8), (b, 0.2)])

    resp = await exe.chat_completion(
        "m", messages=[{"role": "user", "content": "hi"}], pin_provider="ollama"
    )
    assert resp["_routing"]["provider"] == "ollama"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fallback_skips_zero_weight():
    """Normal fallback loop must skip weight=0 adapters."""
    exe = RouteExecutor()
    primary = _FailAdapter(_cfg("m", provider="zai"))
    disabled = _EchoAdapter(_cfg("m", provider="featherless"))
    backup = _EchoAdapter(_cfg("m", provider="ollama"))
    exe.register_route("m", [(primary, 0.8), (disabled, 0.0), (backup, 0.2)])

    # Force primary selection
    orig = exe._select_adapter
    try:
        exe._select_adapter = lambda model_id, **kw: primary  # type: ignore[assignment]
        resp = await exe.chat_completion("m", messages=[{"role": "user", "content": "hi"}])
    finally:
        exe._select_adapter = orig  # type: ignore[assignment]

    # Should fallback to ollama, NOT to featherless (weight=0)
    assert resp["_routing"]["provider"] == "ollama"
    assert resp["_routing"].get("fallback") is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stream_pin_success():
    """stream_chat_completion with pin routes to correct adapter."""
    exe = RouteExecutor()
    a = _EchoAdapter(_cfg("m", provider="zai"))
    b = _EchoAdapter(_cfg("m", provider="ollama"))
    exe.register_route("m", [(a, 0.8), (b, 0.2)])

    chunks = []
    async for chunk in exe.stream_chat_completion(
        "m", messages=[{"role": "user", "content": "hi"}], pin_provider="ollama"
    ):
        chunks.append(chunk)
    assert len(chunks) > 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stream_pin_miss_raises():
    """stream_chat_completion with unmatched pin raises ProviderPinError."""
    exe = RouteExecutor()
    a = _EchoAdapter(_cfg("m", provider="zai"))
    exe.register_route("m", [(a, 1.0)])

    with pytest.raises(ProviderPinError, match="nonexistent"):
        async for _ in exe.stream_chat_completion(
            "m", messages=[{"role": "user", "content": "hi"}], pin_provider="nonexistent"
        ):
            pass


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stream_pin_no_fallback():
    """stream_chat_completion with pin must NOT fallback on failure."""
    exe = RouteExecutor()
    primary = _FailAdapter(_cfg("m", provider="zai"))
    backup = _EchoAdapter(_cfg("m", provider="ollama"))
    exe.register_route("m", [(primary, 0.8), (backup, 0.2)])

    with pytest.raises(RuntimeError, match="fail"):
        async for _ in exe.stream_chat_completion(
            "m", messages=[{"role": "user", "content": "hi"}], pin_provider="zai"
        ):
            pass


class _YieldThenFailAdapter(BaseAdapter):
    """Yields ``yield_count`` chunks then raises RuntimeError.

    Used to exercise the no-fallback-after-yield guard in
    stream_chat_completion: once the SSE stream has committed to a
    provider, falling back would produce a corrupt response.
    """

    def __init__(self, config: ModelConfig, yield_count: int = 1) -> None:
        super().__init__(config)
        self._yield_count = yield_count

    async def chat_completion(self, messages: list[dict[str, Any]], **params) -> dict[str, Any]:
        raise RuntimeError("not used in these tests")  # pragma: no cover

    async def stream_chat_completion(
        self, messages: list[dict[str, Any]], **params
    ) -> AsyncGenerator[str, None]:
        for _ in range(self._yield_count):
            yield self.format_stream_chunk(model=self.config.id, content="partial")
        raise RuntimeError("primary stream failed mid-flight")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stream_fallback_when_primary_fails_before_any_chunk():
    """Primary raises before yielding → fallback runs and serves the response."""
    exe = RouteExecutor()
    primary = _FailAdapter(_cfg("m", provider="primary"))
    backup = _EchoAdapter(_cfg("m", provider="backup"))
    exe.register_route("m", [(primary, 0.9), (backup, 0.1)])

    # Force primary selection.
    exe._select_adapter = lambda model_id, **kw: primary  # type: ignore[assignment]

    chunks = []
    async for chunk in exe.stream_chat_completion(
        "m", messages=[{"role": "user", "content": "hi"}]
    ):
        chunks.append(chunk)

    # Backup successfully delivered chunks after primary failed at chunk 0.
    assert len(chunks) > 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stream_no_fallback_after_chunks_yielded():
    """Primary yields then fails → no fallback; exception propagates.

    Falling back after partial output would corrupt the SSE stream
    (duplicate role/system events, mid-message provider switch,
    mismatched token-usage totals). The fix re-raises instead.
    """
    exe = RouteExecutor()
    primary = _YieldThenFailAdapter(_cfg("m", provider="primary"), yield_count=1)
    backup = _EchoAdapter(_cfg("m", provider="backup"))
    exe.register_route("m", [(primary, 0.9), (backup, 0.1)])

    exe._select_adapter = lambda model_id, **kw: primary  # type: ignore[assignment]

    chunks_seen: list[Any] = []
    with pytest.raises(RuntimeError, match="primary stream failed mid-flight"):
        async for chunk in exe.stream_chat_completion(
            "m", messages=[{"role": "user", "content": "hi"}]
        ):
            chunks_seen.append(chunk)

    # One synthetic _routing chunk + one chunk from primary, then the exception.
    # Backup must NOT have produced any chunks — that would indicate a fallback
    # corrupted the stream after partial output.
    assert len(chunks_seen) == 2
    # First chunk is the synthetic routing metadata for the primary adapter.
    assert '"_routing"' in chunks_seen[0]
    assert '"provider": "primary"' in chunks_seen[0]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stream_emits_routing_chunk_with_provider_and_base_url():
    """Regression: streaming must yield a synthetic _routing chunk first.

    Without this chunk, completions.py falls back to provider="router" and
    pricing=None for streaming requests, because req_ctx.push() inside
    _execute_stream_adapter happens in the background reader task and is
    invisible to the parent coroutine. api_logs would record cost_usd=NULL
    for every streaming request (notably Claude / Anthropic models).
    """
    import json as _json

    exe = RouteExecutor()
    primary = _EchoAdapter(_cfg("m", provider="anthropic"))
    primary.config.base_url = "https://api.anthropic.com"
    primary.config.endpoint_id = "m:anthropic-api"
    exe.register_route("m", [(primary, 1.0)])
    exe._select_adapter = lambda model_id, **kw: primary  # type: ignore[assignment]

    chunks: list[Any] = []
    async for chunk in exe.stream_chat_completion(
        "m", messages=[{"role": "user", "content": "hi"}]
    ):
        chunks.append(chunk)

    # First chunk is the synthetic routing metadata.
    assert chunks, "stream produced no chunks"
    first = chunks[0]
    assert isinstance(first, str) and first.startswith("data: ")
    payload = _json.loads(first[len("data: ") :].strip())
    assert payload["choices"] == []
    routing = payload["_routing"]
    assert routing["provider"] == "anthropic"
    assert routing["base_url"] == "https://api.anthropic.com"
    assert routing["endpoint_id"] == "m:anthropic-api"
    assert "fallback" not in routing


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stream_emits_routing_chunk_for_fallback_adapter():
    """Fallback adapter also gets a _routing chunk (with fallback=True)."""
    import json as _json

    exe = RouteExecutor()
    primary = _FailAdapter(_cfg("m", provider="primary"))
    backup = _EchoAdapter(_cfg("m", provider="backup"))
    backup.config.base_url = "https://backup.example"
    exe.register_route("m", [(primary, 0.9), (backup, 0.1)])
    exe._select_adapter = lambda model_id, **kw: primary  # type: ignore[assignment]

    chunks: list[Any] = []
    async for chunk in exe.stream_chat_completion(
        "m", messages=[{"role": "user", "content": "hi"}]
    ):
        chunks.append(chunk)

    # Two routing chunks (primary + backup) plus the backup's content chunk.
    routing_payloads = []
    for chunk in chunks:
        if isinstance(chunk, str) and chunk.startswith("data: "):
            try:
                p = _json.loads(chunk[len("data: ") :].strip())
            except (ValueError, _json.JSONDecodeError):
                continue
            if "_routing" in p:
                routing_payloads.append(p["_routing"])

    assert len(routing_payloads) == 2
    assert routing_payloads[0]["provider"] == "primary"
    assert routing_payloads[0].get("fallback") is not True
    assert routing_payloads[1]["provider"] == "backup"
    assert routing_payloads[1].get("fallback") is True
    assert routing_payloads[1]["failed_attempts"] == [
        {
            "provider": "primary",
            "endpoint_id": "primary",
            "error_type": "RuntimeError",
            "error": "fail",
        }
    ]


@pytest.mark.unit
def test_admin_only_default_false():
    """RouteConfig defaults admin_only to False."""
    cfg = RouteConfig(adapters=[])
    assert cfg.admin_only is False


@pytest.mark.unit
def test_admin_only_propagated():
    """register_route(..., admin_only=True) sets the flag on route and aliases."""
    exe = RouteExecutor()
    a = _EchoAdapter(_cfg("m", provider="A"))
    exe.register_route("m", [(a, 1.0)], aliases=["m-alias"], admin_only=True)

    assert exe.routes["m"].admin_only is True
    assert exe.routes["m-alias"].admin_only is True
    # Shared reference
    assert exe.routes["m"] is exe.routes["m-alias"]


@pytest.mark.unit
def test_weights_renormalized_when_circuit_breaker_excludes_adapter():
    """Regression: filtered adapters must not distort remaining weight distribution.

    When a circuit breaker excludes an adapter from the pool, the remaining
    weights must be renormalized so that ``random.random()`` (uniform [0,1))
    maps proportionally to the surviving adapters.  Without renormalization,
    the last adapter in the pool absorbed all overflow probability mass,
    getting disproportionately more traffic than its weight warranted.
    """
    exe = RouteExecutor()
    a = _EchoAdapter(_cfg("m", provider="A"))
    b = _EchoAdapter(_cfg("m", provider="B"))
    c = _EchoAdapter(_cfg("m", provider="C"))
    exe.register_route("m", [(a, 1.0), (b, 1.0), (c, 1.0)])

    for _ in range(3):
        exe._on_failure("B", reason="test_failure")
    status = exe.get_provider_status()
    assert status["B"]["circuit_state"] == "open"

    random.seed(42)
    n = 10000
    picks = {"A": 0, "C": 0}
    for _ in range(n):
        chosen = exe._select_adapter("m")
        assert chosen is not None
        assert chosen.config.provider != "B"
        picks[chosen.config.provider] += 1

    frac_a = picks["A"] / n
    frac_c = picks["C"] / n
    assert 0.47 <= frac_a <= 0.53, f"A fraction {frac_a} outside tolerance"
    assert 0.47 <= frac_c <= 0.53, f"C fraction {frac_c} outside tolerance"


@pytest.mark.unit
def test_select_skips_zero_weight_when_positive_circuits_open():
    """Regression: weight=0 (disabled) adapters must never be selected even when
    every positive-weight adapter has its circuit open.

    Pre-fix the cumulative-weight loop's terminal ``return pool[-1][0]``
    fallback could land on a weight=0 adapter when all positive-weight
    adapters were filtered out by the circuit breaker. AllCircuitsOpenError
    is the correct outcome instead.
    """
    from routing.routers import AllCircuitsOpenError

    exe = RouteExecutor()
    a = _EchoAdapter(_cfg("m", provider="A"))
    disabled = _EchoAdapter(_cfg("m", provider="DISABLED"))
    exe.register_route("m", [(a, 1.0), (disabled, 0.0)])

    for _ in range(3):
        exe._on_failure("A", reason="test_failure")

    with pytest.raises(AllCircuitsOpenError):
        exe._select_adapter("m")
