"""Full loopback HTTP serving, streaming measurements and paid-attempt boundaries."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from decimal import Decimal

import aiohttp
import pytest
from aiohttp import web

from benchmark.nimbus.budget import BudgetLedger
from benchmark.nimbus.gateway import OneAttemptHTTP, authoritative_counts
from benchmark.nimbus.report import summarize
from benchmark.nimbus.runner import execute_run, usable_delta, validate_config
from benchmark.nimbus.workload import load_workload


def _frame(delta=None, finish=None, usage=None):
    result = {
        "id": "provider-response",
        "model": "fake-dsv41",
        "choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}],
    }
    if usage is not None:
        result["usage"] = usage
    return ("data: " + json.dumps(result) + "\n\n").encode()


@asynccontextmanager
async def upstream(mode="normal"):
    requests = []

    async def respond(request):
        payload = await request.json()
        requests.append(payload)
        if mode == "redirect":
            raise web.HTTPTemporaryRedirect("http://127.0.0.1:1/should-not-send-key")
        if mode == "error":
            return web.Response(status=503, text="simulated upstream failure")
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        await response.write(_frame({"role": "assistant"}))
        await asyncio.sleep(0.005)
        await response.write(_frame({"content": "one"}))
        if mode == "slow":
            await asyncio.sleep(0.2)
            return response
        if mode == "misleading_terminal":
            await response.write(
                _frame(
                    {"content": "data: [DONE]"}, usage={"prompt_tokens": 12, "completion_tokens": 1}
                )
            )
            return response
        await asyncio.sleep(0.005)
        await response.write(_frame({"content": "two"}))
        if mode == "intermediate_usage":
            await response.write(_frame(usage={"prompt_tokens": 12, "completion_tokens": 1}))
        await response.write(_frame(finish="stop"))
        if mode == "normal":
            await response.write(
                b"data: "
                + json.dumps(
                    {
                        "choices": [],
                        "usage": {
                            "prompt_tokens": 12,
                            "completion_tokens": 4,
                            "total_tokens": 16,
                            "prompt_cache_hit_tokens": 2,
                            "prompt_cache_miss_tokens": 10,
                        },
                    }
                ).encode()
                + b"\n\n"
            )
        await response.write(b"data: [DONE]\n\n")
        return response

    app = web.Application()
    app.router.add_post("/v1/chat/completions", respond)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}/v1", requests
    finally:
        await runner.cleanup()


def config(url, policy="all_api"):
    endpoint = {
        "base_url": url,
        "model": "fake-dsv41",
        "context_length": 4096,
        "max_output_tokens": 64,
        "provider_profile": "deepseek",
    }
    return {
        "schema_version": 2,
        "campaign_id": "unit-campaign",
        "budget_cap_cny": "1000",
        "policy": policy,
        "local": endpoint,
        "cloud": {**endpoint, "input_tokens_upper_bound": 4096},
        "prices": {
            "input_cny_per_million": "2.4",
            "cached_input_cny_per_million": "0.048",
            "output_cny_per_million": "9.6",
        },
        "slo": {"ttft_s": 10, "tpot_s": 1},
        "baseline_profile": {
            "kv_capacity_tokens": 10000,
            "max_tokens": 64,
            "prefill_tput": 10000,
            "tpot_s": 0.001,
            "first_token_overhead_s": 0,
            "slo_s": 10,
            "ttft_guard_s": 0,
            "kv_block_size": 16,
        },
        "replay": {
            "arrival_speedup": 1,
            "request_timeout_s": 2,
            "dispatch_window_s": 0.01,
            "local_max_inflight": 4,
            "cancel_grace_s": 0,
        },
        "generation": {"temperature": 0},
    }


async def run(tmp_path, cfg, rows=None, output_name="run", *, api_key="unit-test-secret-not-real"):
    if rows is None:
        rows = [
            {
                "id": "a",
                "session_id": "session-a",
                "round_index": 0,
                "arrival_s": 0,
                "tool_wait_s": 0,
                "messages": [{"role": "user", "content": "Reply briefly"}],
                "max_tokens": 32,
            }
        ]
    config_path = tmp_path / f"{output_name}-config.json"
    workload_path = tmp_path / f"{output_name}-workload.jsonl"
    config_path.write_text(json.dumps(cfg))
    workload_path.write_text("\n".join(json.dumps(row) for row in rows))
    summary = await execute_run(
        config_path,
        workload_path,
        tmp_path / output_name,
        tmp_path / "budget.sqlite",
        api_key=api_key,
        require_clean=False,
    )
    records = [
        json.loads(line)
        for line in (tmp_path / output_name / "requests.jsonl").read_text().splitlines()
    ]
    manifest = json.loads((tmp_path / output_name / "manifest.json").read_text())
    return summary, records, manifest


@pytest.mark.parametrize(
    "policy", ["all_api", "all_local", "concurrency", "greedy", "rolling_knapsack_v1"]
)
async def test_real_gateway_registry_adapter_path_and_final_usage(tmp_path, policy):
    async with upstream() as (url, calls):
        summary, records, manifest = await run(tmp_path, config(url, policy))
    assert len(calls) == 1
    record = records[0]
    assert record["status"] == "success"
    assert record["response"]["content"] == "onetwo"
    assert record["usable_chunks"] == 2  # role-only frame did not start TTFT
    assert record["completion_tokens"] == 4
    assert record["cache_tokens_priced"] == 2
    assert record["tpot_s"] == pytest.approx((record["end_s"] - record["first_usable_s"]) / 3)
    assert record["tpot_s"] >= record["output_span_tpot_s"]
    assert record["gateway_request_id"].startswith("req_")
    assert record["decision_records"]
    assert summary["ttft_s"]["p90"] is not None
    assert summary["successful_requests"] == 1
    assert manifest["state"] == "completed"
    assert "ModelRouterRegistry" in manifest["transport"]
    assert calls[0]["max_tokens"] == 32
    assert "unit-test-secret-not-real" not in "".join(
        path.read_text() for path in (tmp_path / "run").glob("*.json*")
    )


async def test_concurrent_ids_sessions_and_fixed_online_estimates(tmp_path):
    rows = [
        {
            "id": identifier,
            "session_id": identifier,
            "round_index": 0,
            "arrival_s": 0,
            "tool_wait_s": 0,
            "messages": [{"role": "user", "content": identifier}],
            "max_tokens": 32,
            "actual_output_tokens": 99999,
        }
        for identifier in ("first", "second", "third")
    ]
    async with upstream() as (url, calls):
        _, records, _ = await run(tmp_path, config(url), rows)
    assert len(calls) == 3
    assert {r["id"] for r in records} == {"first", "second", "third"}
    assert len({r["gateway_request_id"] for r in records}) == 3
    assert all(len(r["attempt_ids"]) == 1 for r in records)
    assert all(r["decision_records"][0]["profile"]["max_tokens"] == 64 for r in records)
    assert all(
        set(r["decision_records"][0]["candidate"])
        == {"request_id", "arrival_s", "prompt_tokens", "estimated_remote_cost"}
        for r in records
    )


async def test_closed_loop_uses_predecessor_tool_delay(tmp_path):
    rows = [
        {
            "id": f"turn-{index}",
            "session_id": "one-session",
            "round_index": index,
            "arrival_s": 0,
            "tool_wait_s": delay,
            "messages": [{"role": "user", "content": "hello"}],
            "max_tokens": 32,
        }
        for index, delay in enumerate((0.025, 0.5))
    ]
    async with upstream() as (url, _):
        _, records, _ = await run(tmp_path, config(url), rows)
    assert records[1]["eligible_s"] == pytest.approx(records[0]["end_s"] + 0.025)
    assert records[1]["dispatch_s"] >= records[1]["eligible_s"]


@pytest.mark.parametrize(
    "mode",
    ["missing_usage", "intermediate_usage", "misleading_terminal", "error", "redirect", "slow"],
)
async def test_unknown_cloud_usage_keeps_reservation_and_never_retries(tmp_path, mode):
    async with upstream(mode) as (url, calls):
        cfg = config(url)
        if mode == "slow":
            cfg["replay"]["request_timeout_s"] = 0.04
        summary, records, _ = await run(tmp_path, cfg)
    assert len(calls) == 1
    ledger = BudgetLedger(tmp_path / "budget.sqlite", "unit-campaign", "1000")
    assert ledger.export_entries()[0]["status"] == "unknown"
    assert Decimal(ledger.snapshot()["outstanding_reserved_cny"]) > 0
    assert records[0]["tpot_s"] is None
    assert not records[0]["usage_authoritative"]
    assert summary["requests"] == 1


async def test_zero_cloud_attempt_limit_refuses_before_wire(tmp_path):
    async with upstream() as (url, calls):
        cfg = config(url)
        cfg["replay"]["max_cloud_attempts"] = 0
        summary, records, _ = await run(tmp_path, cfg)
    assert not calls
    assert records[0]["status"] == "error"
    assert summary["failed_requests"] == 1
    assert not BudgetLedger(tmp_path / "budget.sqlite", "unit-campaign", "1000").export_entries()


@pytest.mark.parametrize("policy", ["all_api", "all_local"])
async def test_explicit_cloud_proxy_is_used_but_local_stays_direct(tmp_path, monkeypatch, policy):
    async with upstream() as (origin_url, origin_calls), upstream() as (proxy_url, proxy_calls):
        monkeypatch.setenv("http_proxy", proxy_url.removesuffix("/v1"))
        monkeypatch.delenv("HTTP_PROXY", raising=False)
        monkeypatch.setenv("no_proxy", "")
        monkeypatch.setenv("NO_PROXY", "")
        cfg = config(origin_url, policy)
        cfg["cloud"]["trust_env_proxy"] = True
        summary, _, _ = await run(tmp_path, cfg)
    assert summary["successful_requests"] == 1
    assert len(proxy_calls) == (1 if policy == "all_api" else 0)
    assert len(origin_calls) == (0 if policy == "all_api" else 1)


@pytest.mark.parametrize(
    "timeout", [None, aiohttp.ClientTimeout(total=25, connect=7, sock_connect=30, sock_read=40)]
)
def test_connection_timeout_bound_preserves_total_and_read_limits(timeout):
    bounded = OneAttemptHTTP.bounded_timeout(timeout)
    assert bounded.connect == (7 if timeout else 15)
    assert bounded.sock_connect == 15
    assert bounded.total == (25 if timeout else None)
    assert bounded.sock_read == (40 if timeout else None)


async def test_missing_prewarm_cache_fails_before_cloud_and_keeps_failed_manifest(
    tmp_path, monkeypatch
):
    import tiktoken

    monkeypatch.setenv("TIKTOKEN_CACHE_DIR", str(tmp_path / "empty-cache"))

    def must_not_download(*args, **kwargs):
        raise AssertionError("cold tokenizer constructor must not run")

    monkeypatch.setattr(tiktoken, "get_encoding", must_not_download)
    async with upstream() as (url, calls):
        cfg = config(url)
        cfg["replay"]["prewarm_tokenizer"] = True
        with pytest.raises(ValueError, match="cache is missing"):
            await run(tmp_path, cfg)
    assert not calls
    assert json.loads((tmp_path / "run/manifest.json").read_text())["state"] == "failed"
    assert json.loads((tmp_path / "run/ledger.json").read_text()) == []


@pytest.mark.parametrize(
    "usage",
    [
        None,
        {},
        {"prompt_tokens": 3},
        {"prompt_tokens": 3, "completion_tokens": True},
        {"prompt_tokens": 3, "completion_tokens": 2, "prompt_tokens_details": []},
        {"prompt_tokens": 3, "completion_tokens": 2, "prompt_tokens_details": "bad"},
        {"prompt_tokens": 3, "completion_tokens": 2, "prompt_cache_hit_tokens": 4},
        {
            "prompt_tokens": 3,
            "completion_tokens": 2,
            "prompt_cache_hit_tokens": 1,
            "prompt_cache_miss_tokens": 1,
        },
    ],
)
def test_invalid_or_incomplete_authoritative_usage_is_not_refunded(usage):
    assert authoritative_counts(usage) is None


def test_tool_and_reasoning_are_usable_but_role_is_not():
    assert not usable_delta({"role": "assistant"})
    assert not usable_delta({"tool_calls": [{"id": "call-1"}]})
    assert usable_delta({"reasoning_content": "think"})
    assert usable_delta({"tool_calls": [{"function": {"arguments": "{"}}]})


def test_duplicate_workload_fails_before_network(tmp_path):
    path = tmp_path / "bad.jsonl"
    row = {
        "id": "same",
        "session_id": "s",
        "round_index": 0,
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 2,
    }
    path.write_text(json.dumps(row) + "\n" + json.dumps(row))
    with pytest.raises(ValueError, match="duplicate"):
        load_workload(path)


def test_summary_keeps_failed_and_undefined_requests_in_denominator():
    records = [
        {
            "id": "ok",
            "route": "cloud",
            "status": "success",
            "ttft_s": 0.1,
            "tpot_s": None,
            "joint_slo_satisfied": None,
            "tpot_undefined_reason": "at_most_one_output_token",
        },
        {"id": "failed", "route": "cloud", "status": "error", "joint_slo_satisfied": False},
    ]
    summary = summarize(records, {"ttft_s": 1, "tpot_s": 0.1})
    assert summary["requests"] == 2
    assert summary["failed_requests"] == 1
    assert summary["joint_slo_satisfied_fraction_all_requests"] == 0
    assert summary["ttft_s"]["count"] == 1
    assert summary["tpot_s"]["count"] == 0


@pytest.mark.parametrize("policy", ["density", "fifo", "knapsack", "nimbus"])
def test_schema2_rejects_retired_policy_names(policy):
    with pytest.raises(ValueError, match="unknown baseline policy"):
        validate_config(config("http://127.0.0.1:12345/v1", policy))


def test_schema2_refuses_historical_schema_and_ambiguous_profile():
    cfg = config("http://127.0.0.1:12345/v1")
    cfg["schema_version"] = 1
    with pytest.raises(ValueError, match="pinned commit"):
        validate_config(cfg)
    cfg["schema_version"] = 2
    cfg["profile"] = {}
    with pytest.raises(ValueError, match="baseline_profile"):
        validate_config(cfg)


async def test_workload_cap_exceeding_fixed_profile_is_refused_before_listener(tmp_path):
    cfg = config("http://127.0.0.1:12345/v1")
    cfg["baseline_profile"]["max_tokens"] = 16
    with pytest.raises(ValueError, match=r"baseline_profile\.max_tokens"):
        await run(tmp_path, cfg)
    assert not (tmp_path / "run").exists()


@pytest.mark.parametrize("policy", ["greedy", "rolling_knapsack_v1"])
@pytest.mark.parametrize("local_fits", [True, False])
async def test_zero_cloud_attempt_cap_runs_without_api_key_and_never_posts_cloud(
    tmp_path, policy, local_fits
):
    async with upstream() as (url, calls):
        cfg = config(url, policy)
        cfg["replay"]["max_cloud_attempts"] = 0
        cfg["baseline_profile"]["kv_capacity_tokens"] = 10000 if local_fits else 1
        summary, records, _ = await run(tmp_path, cfg, api_key=None)
    assert len(calls) == int(local_fits)
    assert summary["successful_requests"] == int(local_fits)
    assert records[0]["route"] == ("local" if local_fits else "cloud")
    entries = json.loads((tmp_path / "run" / "ledger.json").read_text())
    assert not entries
