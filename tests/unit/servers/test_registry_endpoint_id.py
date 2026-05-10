"""Regression tests for serving.servers.registry — endpoint ID generation."""

from __future__ import annotations

from serving.servers.registry import _make_adapter, _make_provider_id

# ---------------------------------------------------------------------------
# Local endpoints — must include port to avoid collisions
# ---------------------------------------------------------------------------


def test_localhost_includes_port() -> None:
    assert _make_provider_id("m", "sglang", "http://localhost:8003") == "m:local-8003"


def test_host_docker_internal_includes_port() -> None:
    eid = _make_provider_id("glm-4.7-flash", "openai_compat", "http://host.docker.internal:8004/v1")
    assert eid == "glm-4.7-flash:local-8004"


def test_127_includes_port() -> None:
    assert _make_provider_id("m", "vllm", "http://127.0.0.1:5000") == "m:local-5000"


def test_different_ports_produce_different_ids() -> None:
    a = _make_provider_id("m", "sglang", "http://host.docker.internal:8004")
    b = _make_provider_id("m", "sglang", "http://host.docker.internal:8007")
    assert a != b


def test_local_without_port_fallback() -> None:
    assert _make_provider_id("m", "sglang", "http://localhost") == "m:local"


# ---------------------------------------------------------------------------
# Remote endpoints — unchanged behavior
# ---------------------------------------------------------------------------


def test_zai_api() -> None:

    assert _make_provider_id("glm-4.6", "zai", "https://api.z.ai/v4/") == "glm-4.6:zai-api"


def test_chutes_api() -> None:
    eid = _make_provider_id("qwen3-coder", "chutes", "https://llm.chutes.ai")
    assert eid == "qwen3-coder:chutes-api"


def test_minimax_extracted_from_hostname() -> None:
    eid = _make_provider_id("minimax-m2.7", "openai_compat", "https://api.minimax.io")
    assert eid == "minimax-m2.7:minimax-api"


def test_minimax_kind_uses_kind_api_format() -> None:
    eid = _make_provider_id("minimax-m2.7", "minimax", "https://api.minimax.io")
    assert eid == "minimax-m2.7:minimax-api"


def test_fallback_on_bad_url() -> None:
    # urlparse doesn't raise on malformed URLs; hostname becomes None → "unknown"
    eid = _make_provider_id("m", "sglang", "not-a-url")
    assert eid == "m:unknown-api"


def test_sglang_routes_request_stream_usage() -> None:
    adapter = _make_adapter(
        "sglang",
        {
            "id": "qwen3.6-35b",
            "name": "Qwen3.6 35B",
            "provider": "sglang",
            "base_url": "http://localhost:8001",
        },
    )

    assert adapter.config.include_usage_in_stream is True


def test_minimax_routes_request_stream_usage() -> None:
    # Regression: MiniMax requires stream_options.include_usage so cached
    # tokens surface in recent requests (parity with sglang).
    adapter = _make_adapter(
        "minimax",
        {
            "id": "minimax-m2.7",
            "name": "MiniMax M2.7",
            "provider": "minimax",
            "base_url": "https://api.minimax.io/v1",
        },
    )

    assert adapter.config.include_usage_in_stream is True
    assert adapter.config.provider_profile == "minimax"
