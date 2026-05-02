from __future__ import annotations

import asyncio
import random
from typing import TYPE_CHECKING, Any

import pytest

from routing.executor import ProviderPinError, RouteConfig, RouteExecutor, _has_non_empty_content
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
async def test_no_route_configured_raises():
    exe = RouteExecutor()
    with pytest.raises(ValueError):
        await exe.chat_completion("unknown", messages=[{"role": "user", "content": "hi"}])


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
def test_route_selection_performance():
    """Ensure adapter selection is fast enough for basic regression budgets."""
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
    a = _EchoAdapter(_cfg("m", provider="zhipu"))
    b = _EchoAdapter(_cfg("m", provider="ollama"))
    exe.register_route("m", [(a, 0.8), (b, 0.2)])

    chosen = exe._select_adapter("m", pin_provider="ollama")
    assert chosen is b


@pytest.mark.unit
def test_pin_skips_zero_weight():
    """pin_provider must not route to a weight=0 (disabled) adapter."""
    exe = RouteExecutor()
    a = _EchoAdapter(_cfg("m", provider="zhipu"))
    b = _EchoAdapter(_cfg("m", provider="featherless"))
    exe.register_route("m", [(a, 1.0), (b, 0.0)])

    chosen = exe._select_adapter("m", pin_provider="featherless")
    assert chosen is None


@pytest.mark.unit
def test_pin_miss_returns_none():
    """pin_provider with unknown name returns None."""
    exe = RouteExecutor()
    a = _EchoAdapter(_cfg("m", provider="zhipu"))
    exe.register_route("m", [(a, 1.0)])

    chosen = exe._select_adapter("m", pin_provider="nonexistent")
    assert chosen is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_pin_miss_raises_provider_pin_error():
    """chat_completion with unmatched pin raises ProviderPinError, not generic ValueError."""
    exe = RouteExecutor()
    a = _EchoAdapter(_cfg("m", provider="zhipu"))
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
    primary = _FailAdapter(_cfg("m", provider="zhipu"))
    backup = _EchoAdapter(_cfg("m", provider="ollama"))
    exe.register_route("m", [(primary, 0.8), (backup, 0.2)])

    with pytest.raises(RuntimeError, match="fail"):
        await exe.chat_completion(
            "m", messages=[{"role": "user", "content": "hi"}], pin_provider="zhipu"
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_pin_success():
    """pin_provider routes to the correct adapter and returns its response."""
    exe = RouteExecutor()
    a = _EchoAdapter(_cfg("m", provider="zhipu"))
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
    primary = _FailAdapter(_cfg("m", provider="zhipu"))
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
    a = _EchoAdapter(_cfg("m", provider="zhipu"))
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
    a = _EchoAdapter(_cfg("m", provider="zhipu"))
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
    primary = _FailAdapter(_cfg("m", provider="zhipu"))
    backup = _EchoAdapter(_cfg("m", provider="ollama"))
    exe.register_route("m", [(primary, 0.8), (backup, 0.2)])

    with pytest.raises(RuntimeError, match="fail"):
        async for _ in exe.stream_chat_completion(
            "m", messages=[{"role": "user", "content": "hi"}], pin_provider="zhipu"
        ):
            pass


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
