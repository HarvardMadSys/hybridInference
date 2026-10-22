"""Explicit engine ownership reaches every adapter's admission wrapper."""

from __future__ import annotations

import pytest

from serving.adapters import (
    AnthropicAdapter,
    ClaudeAdapter,
    GeminiAdapter,
    ModelConfig,
    OpenAICompatAdapter,
    OpenRouterAdapter,
)
from serving.adapters.dispatch_watch import report_first_token
from serving.adapters.upstream_limiter import (
    EngineHold,
    UpstreamConcurrencyLimiter,
    is_local_endpoint,
    reset_upstream_limiter,
)
from serving.utils import context as req_ctx


@pytest.mark.parametrize(
    ("base_url", "deployment_type", "expected"),
    [
        ("http://localhost:8000/v1", "auto", True),
        ("http://127.0.0.1:8000/v1", "auto", True),
        ("http://0.0.0.0:8000/v1", "auto", True),
        ("http://host.docker.internal:8000/v1", "auto", True),
        ("http://10.0.0.5:8000/v1", "auto", False),
        ("http://inference.svc:8000/v1", "auto", False),
        ("http://10.0.0.5:8000/v1", "self_hosted", True),
        ("http://inference.svc:8000/v1", "self_hosted", True),
        ("http://localhost:8000/v1", "provider_api", False),
        ("http://127.0.0.1:8000/v1", "provider_api", False),
    ],
)
def test_explicit_deployment_precedes_compatible_auto_detection(
    base_url, deployment_type, expected
):
    assert is_local_endpoint(base_url, deployment_type=deployment_type) is expected


@pytest.mark.parametrize(
    ("adapter_class", "method"),
    [
        (OpenAICompatAdapter, "acquire"),
        (OpenRouterAdapter, "acquire"),
        (ClaudeAdapter, "acquire"),
        (ClaudeAdapter, "guard"),
        (GeminiAdapter, "acquire"),
        (GeminiAdapter, "guard"),
        (AnthropicAdapter, "guard"),
    ],
)
@pytest.mark.parametrize("deployment_type", ["self_hosted", "provider_api"])
async def test_all_admission_wrappers_honor_explicit_deployment(
    adapter_class, method, deployment_type
):
    limiter = UpstreamConcurrencyLimiter(initial_limit=1, max_limit=2, acquire_timeout=0.01)
    reset_upstream_limiter(limiter)
    adapter = adapter_class(
        ModelConfig(
            id="test-model",
            name="Test model",
            provider="test-provider",
            api_key="test-key",
            base_url=(
                "http://inference.svc:8000/v1"
                if deployment_type == "self_hosted"
                else "http://localhost:8000/v1"
            ),
            deployment_type=deployment_type,
        )
    )

    def verify_admission():
        if deployment_type == "self_hosted":
            assert limiter.snapshot() == {}
            assert limiter.engine_snapshot()["inference.svc:8000"]["pending"] == 1
            report_first_token()
            assert limiter.engine_snapshot()["inference.svc:8000"]["pending"] == 0
        else:
            assert limiter.engine_snapshot() == {}
            state = next(iter(limiter.snapshot().values()))
            assert state["in_flight"] == 1
            report_first_token()
            assert next(iter(limiter.snapshot().values()))["in_flight"] == 1

    try:
        with req_ctx.push(**{req_ctx.UPSTREAM_ENGINE_HOLD: EngineHold(1)}):
            if method == "guard":
                async with adapter._upstream_slot():
                    verify_admission()
            else:
                if isinstance(adapter, OpenAICompatAdapter):
                    slot = await adapter._acquire_upstream_slot(adapter.config.api_key)
                else:
                    slot = await adapter._acquire_upstream_slot()
                try:
                    verify_admission()
                finally:
                    slot.release()
        assert all(state["in_flight"] == 0 for state in limiter.snapshot().values())
        assert all(state["pending"] == 0 for state in limiter.engine_snapshot().values())
    finally:
        reset_upstream_limiter()
