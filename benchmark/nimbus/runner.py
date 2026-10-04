"""Replay immutable workloads through a loopback instance of the real gateway.

Run with ``PYTHONPATH=apps/backend:. python -m benchmark.nimbus.runner --help``.
Each process starts its own gateway, uses one shared campaign spending ledger,
and writes a fresh run directory. It never starts or stops a model server.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import secrets
import socket
import subprocess
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiohttp
import uvicorn

from benchmark.nimbus.baselines import SOURCE_PROVENANCE, BaselineProfile
from benchmark.nimbus.budget import BudgetLedger
from benchmark.nimbus.gateway import (
    ExperimentRuntime,
    Journal,
    create_experiment_app,
    parse_sse,
    validate_endpoint,
)
from benchmark.nimbus.report import summarize
from benchmark.nimbus.workload import canonical_json, group_sessions, load_workload, sha256_bytes


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _number(value: Any, label: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a finite number")
    result = float(value)
    if not math.isfinite(result) or result < 0 or (positive and result == 0):
        raise ValueError(f"{label} is outside its permitted range")
    return result


def validate_config(config: dict[str, Any]) -> None:
    """Refuse unsafe or ambiguous campaign settings before initializing a listener."""
    if config.get("schema_version") != 2:
        raise ValueError("schema_version must be 2; reproduce older runs with their pinned commit")
    if config["policy"] not in {
        "all_local",
        "all_api",
        "concurrency",
        "greedy",
        "rolling_knapsack_v1",
    }:
        raise ValueError("unknown baseline policy")
    for route in ("local", "cloud"):
        validate_endpoint(config[route], route)
    if "profile" in config or "estimated_output_tokens" in config["replay"]:
        raise ValueError("schema 2 requires baseline_profile and its fixed max_tokens")
    profile = BaselineProfile(**config["baseline_profile"])
    if profile.slo_s != config["slo"]["ttft_s"]:
        raise ValueError("baseline_profile.slo_s must equal the reported slo.ttft_s")
    replay = config["replay"]
    slots = replay.get("local_max_inflight")
    if isinstance(slots, bool) or not isinstance(slots, int) or slots < 1:
        raise ValueError("replay.local_max_inflight must be a positive integer")
    window = replay.get("knapsack_window", 12)
    if isinstance(window, bool) or not isinstance(window, int) or not 1 <= window <= 16:
        raise ValueError("replay.knapsack_window must be an integer in [1, 16]")
    for key in ("arrival_speedup", "request_timeout_s"):
        _number(replay.get(key, 1 if key == "arrival_speedup" else 180), key, positive=True)
    _number(replay.get("dispatch_window_s", 0), "dispatch_window_s")
    for key in ("max_in_flight", "max_cloud_attempts"):
        value = replay.get(key)
        minimum = 0 if key == "max_cloud_attempts" else 1
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or value < minimum
        ):
            raise ValueError(f"{key} must be an integer >= {minimum} or null")
    for key in ("ttft_s", "tpot_s"):
        _number(config["slo"][key], key, positive=True)
    generation = config.get("generation", {})
    if set(generation) - {"temperature", "top_p", "seed"}:
        raise ValueError("generation only accepts temperature, top_p and seed; caps belong to rows")

    def check_secrets(value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key.lower() in {"api_key", "api_keys", "authorization", "password", "secret"}:
                    raise ValueError("credentials must be provided only by the API key environment")
                check_secrets(child)
        elif isinstance(value, list):
            for child in value:
                check_secrets(child)

    check_secrets(config)


def source_provenance(require_clean: bool = True) -> dict[str, Any]:
    """Capture the exact source commit and refuse dirty measured runs by default."""
    root = Path(__file__).resolve().parents[2]

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=root, check=True, capture_output=True, text=True
        ).stdout.strip()

    status = git("status", "--porcelain")
    if require_clean and status:
        raise ValueError(
            "measured runs require a clean source commit; commit experiment code first"
        )
    return {
        "commit": git("rev-parse", "HEAD"),
        "branch": git("branch", "--show-current"),
        "clean": not bool(status),
        "status": status,
        "uv_lock_sha256": sha256_bytes((root / "uv.lock").read_bytes()),
        "source_file_sha256": {
            str(path.relative_to(root)): sha256_bytes(path.read_bytes())
            for folder in (root / "benchmark/nimbus", root / "apps/backend/routing")
            for path in sorted(folder.glob("*.py"))
        },
    }


def prewarm_tokenizer() -> dict[str, Any]:
    """Warm the gateway's fallback tokenizer from a verified cache, never download."""
    expected_hash = "223921b76ee99bde995b7ff738513eef100fb51d18c93597a113bcffe865b2a7"
    cache_filename = "9b5ad71b2ce5302211f9c61530b329a4922fc6a4"
    cache_dir = os.environ.get(
        "TIKTOKEN_CACHE_DIR",
        os.environ.get("DATA_GYM_CACHE_DIR", str(Path(tempfile.gettempdir()) / "data-gym-cache")),
    )
    if not cache_dir:
        raise ValueError("tokenizer prewarm requires a cache directory; downloads are forbidden")
    path = Path(cache_dir) / cache_filename
    if not path.is_file() or sha256_bytes(path.read_bytes()) != expected_hash:
        raise ValueError(
            "verified cl100k_base cache is missing; stage the public asset before replay"
        )
    import tiktoken

    start = time.monotonic()
    encoding = tiktoken.get_encoding("cl100k_base")
    encoding.encode("Baseline tokenizer warmup", disallowed_special=())
    return {
        "encoding": "cl100k_base",
        "sha256": expected_hash,
        "cache_path": str(path),
        "duration_s": time.monotonic() - start,
        "network_used": False,
    }


def usable_delta(delta: dict[str, Any]) -> bool:
    """Identify generated text/reasoning/tool data, excluding role-only chunks."""
    if any(
        isinstance(delta.get(k), str) and delta[k]
        for k in ("content", "reasoning_content", "reasoning", "thinking")
    ):
        return True
    for call in delta.get("tool_calls") or []:
        if not isinstance(call, dict):
            continue
        function = call.get("function") or {}
        if not isinstance(function, dict):
            continue
        if function.get("name") or function.get("arguments"):
            return True
    call = delta.get("function_call") or {}
    if not isinstance(call, dict):
        return False
    return bool(call.get("name") or call.get("arguments"))


async def _one_request(
    runtime: ExperimentRuntime,
    session: aiohttp.ClientSession,
    url: str,
    request: Any,
    eligible_s: float,
    semaphore: asyncio.Semaphore,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "id": request.request_id,
        "session_id": request.session_id,
        "round_index": request.round_index,
        "scheduled_arrival_s": request.arrival_s
        / runtime.config["replay"].get("arrival_speedup", 1),
        "eligible_s": eligible_s,
        "payload_sha256": request.payload_sha256,
        "max_tokens": request.max_tokens,
        "status": "error",
        "route": "unassigned",
        "first_usable_s": None,
        "last_usable_s": None,
        "usable_chunks": 0,
        "finish_reason": None,
        "sse_usage": None,
    }
    output = {"content": [], "reasoning": [], "tool_call_deltas": []}
    body = {
        "model": runtime.config.get("model_id", "baseline-dsv41"),
        "messages": request.messages,
        "max_tokens": request.max_tokens,
        "stream": True,
        "stream_options": {"include_usage": True},
        **runtime.config.get("generation", {}),
    }
    if request.tools:
        body["tools"] = request.tools
    terminal = False
    try:
        async with semaphore:
            record["dispatch_s"] = runtime.now()
            runtime.journal.append("client_events.jsonl", {"event": "dispatch", **record})
            async with session.post(
                url,
                json=body,
                headers={
                    "Authorization": "Bearer " + runtime.gateway_token,
                    "X-Experiment-Request-ID": request.request_id,
                    "X-Request-ID": request.request_id,
                    "X-Session-ID": request.session_id,
                },
                allow_redirects=False,
            ) as response:
                record["http_status"] = response.status
                record["headers_s"] = runtime.now()
                if response.status != 200:
                    record["error"] = f"gateway_http_{response.status}"
                    await response.read()
                else:
                    lines: list[str] = []
                    async for raw in response.content:
                        line = raw.decode("utf-8").rstrip("\r\n")
                        if line:
                            lines.append(line)
                            continue
                        frame, lines = "\n".join(lines), []
                        observed = runtime.now()
                        if not frame:
                            continue
                        runtime.journal.append(
                            "sse.jsonl",
                            {"id": request.request_id, "observed_s": observed, "frame": frame},
                            durable=False,
                        )
                        if frame.strip() == "data: [DONE]":
                            terminal = True
                            continue
                        event = parse_sse(frame)
                        if event is None:
                            continue
                        if event.get("error"):
                            record["error"] = "gateway_stream_error"
                            record["stream_error"] = event["error"]
                        if event.get("usage") is not None:
                            record["sse_usage"] = event["usage"]
                        frame_usable = False
                        for choice in event.get("choices") or []:
                            if choice.get("finish_reason"):
                                record["finish_reason"] = choice["finish_reason"]
                                terminal = True
                            delta = choice.get("delta") or {}
                            if not usable_delta(delta):
                                continue
                            frame_usable = True
                            if delta.get("content"):
                                output["content"].append(delta["content"])
                            for key in ("reasoning_content", "reasoning", "thinking"):
                                if delta.get(key):
                                    output["reasoning"].append(delta[key])
                            if delta.get("tool_calls") or delta.get("function_call"):
                                output["tool_call_deltas"].append(delta)
                        if frame_usable:
                            if record["first_usable_s"] is None:
                                record["first_usable_s"] = observed
                            record["last_usable_s"] = observed
                            record["usable_chunks"] += 1
                    if lines:
                        record["trailing_unframed_sse"] = True
                    if terminal and "error" not in record and record["first_usable_s"] is not None:
                        record["status"] = "success"
                    elif "error" not in record:
                        record["error"] = "incomplete_or_empty_stream"
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        record["error"] = type(exc).__name__
    finally:
        record["end_s"] = runtime.now()

    # Transport settlement normally precedes EOF. Waiting here only reconciles
    # the server's cancellation cleanup; it is excluded from latency metrics.
    attempts = runtime.attempts.get(request.request_id, [])
    for attempt in attempts:
        try:
            await asyncio.wait_for(attempt.completion.wait(), timeout=10)
        except asyncio.TimeoutError:
            record["audit_pending"] = True
    if attempts:
        latest = attempts[-1]
        record["route"] = latest.route
        record["gateway_request_id"] = latest.gateway_request_id
        record["attempt_ids"] = [a.attempt_id for a in attempts]
        record["upstream_wire_s"] = latest.wire_s
        record["upstream_usage"] = latest.upstream_usage
        record["usage_authoritative"] = latest.usage_counts is not None
        record["response_model"] = latest.response_model
        record["decision_records"] = runtime.decisions.get(latest.gateway_request_id, [])
        if latest.budget:
            record["budget_status"] = latest.budget.get("status")
            record["reserved_cny"] = latest.budget.get("reserved_cny")
            record["unresolved_reserved_cny"] = (
                latest.budget.get("reserved_cny", "0")
                if latest.budget.get("status") in {"reserved", "dispatched", "unknown"}
                else "0"
            )
            record["cost_cny_estimated"] = latest.budget.get("estimated_cost_cny")
            record["cost_billing_currency_estimated"] = latest.budget.get(
                "estimated_cost_billing_currency"
            )
            record["billing_currency"] = latest.budget.get("billing_currency")
            record["cost_basis"] = latest.budget.get("cost_basis")
        if latest.usage_counts:
            record["prompt_tokens"], record["completion_tokens"], record["cache_tokens_priced"] = (
                latest.usage_counts
            )
    record["truncated"] = record["finish_reason"] == "length"
    record["queue_s"] = record.get("dispatch_s", record["end_s"]) - eligible_s
    record["service_s"] = record["end_s"] - record.get("dispatch_s", record["end_s"])
    record["e2e_s"] = record["end_s"] - eligible_s
    first = record["first_usable_s"]
    record["ttft_s"] = first - eligible_s if first is not None else None
    record["dispatch_ttft_s"] = first - record["dispatch_s"] if first is not None else None
    count = record.get("completion_tokens")
    record["tpot_s"] = None
    record["output_span_tpot_s"] = None
    if first is None:
        record["tpot_undefined_reason"] = "no_usable_output"
    elif count is None:
        record["tpot_undefined_reason"] = "no_authoritative_usage"
    elif count <= 1:
        record["tpot_undefined_reason"] = "at_most_one_output_token"
    else:
        record["tpot_s"] = (record["end_s"] - first) / (count - 1)
        record["output_span_tpot_s"] = (record["last_usable_s"] - first) / (count - 1)
    slo = runtime.config["slo"]
    if record["status"] != "success":
        record["joint_slo_satisfied"] = False
    elif record["ttft_s"] is None or record["tpot_s"] is None:
        record["joint_slo_satisfied"] = None
    else:
        record["joint_slo_satisfied"] = (
            record["ttft_s"] <= slo["ttft_s"] and record["tpot_s"] <= slo["tpot_s"]
        )
    record["response"] = {
        "content": "".join(output["content"]),
        "reasoning": "".join(output["reasoning"]),
        "tool_call_deltas": output["tool_call_deltas"],
    }
    record["response_sha256"] = sha256_bytes(canonical_json(record["response"]).encode())
    runtime.journal.append("requests.jsonl", record)
    return record


async def execute_run(
    config_path: str | Path,
    workload_path: str | Path,
    output: str | Path,
    ledger_path: str | Path,
    *,
    api_key: str | None,
    require_clean: bool = True,
) -> dict[str, Any]:
    """Start a real loopback HTTP gateway and run a finite immutable cohort."""
    config_bytes = Path(config_path).read_bytes()
    config = json.loads(config_bytes)
    validate_config(config)
    requests = load_workload(workload_path)
    for request in requests:
        if request.max_tokens > config["baseline_profile"]["max_tokens"]:
            raise ValueError("a workload output cap exceeds baseline_profile.max_tokens")
        if any(
            request.max_tokens > config[route]["max_output_tokens"] for route in ("local", "cloud")
        ):
            raise ValueError("a workload output cap exceeds an endpoint limit")
    if (
        config["policy"] != "all_local"
        and config["replay"].get("max_cloud_attempts") != 0
        and not api_key
    ):
        raise ValueError("cloud-capable policies require the configured API key environment")
    source = source_provenance(require_clean)
    directory = Path(output)
    directory.mkdir(parents=True, exist_ok=False)
    workload_bytes = Path(workload_path).read_bytes()
    (directory / "config.json").write_bytes(config_bytes)
    (directory / "workload.jsonl").write_bytes(workload_bytes)
    run_id = (
        "nimbus-"
        + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        + "-"
        + uuid.uuid4().hex[:8]
    )
    journal = Journal(directory)
    ledger = BudgetLedger(ledger_path, config["campaign_id"], config["budget_cap_cny"])
    runtime = ExperimentRuntime(
        config, run_id, ledger, journal, {r.request_id for r in requests}, secrets.token_urlsafe(32)
    )
    manifest = {
        "schema_version": 2,
        "run_id": run_id,
        "state": "running",
        "started_at": _utc_now(),
        "source": source,
        "config_sha256": sha256_bytes(config_bytes),
        "workload_sha256": sha256_bytes(workload_bytes),
        "request_count": len(requests),
        "session_count": len(group_sessions(requests)),
        "policy": config["policy"],
        "budget_before": ledger.snapshot(),
        "transport": "loopback HTTP -> real completions handler -> experiment-local ModelRouterRegistry subclass -> BaselineRouter (RouterProtocol) -> LeafBackend -> OpenAICompatAdapter -> single POST; experiment HTTP seam disables connect retries and redirects but reuses existing SSE parser",
        "bypasses": [
            "production bootstrap",
            "private ModelRouterRegistry construction hook for baseline_experiment; no production strategy registration or Admin support",
            "Postgres/account billing",
            "production user auth (per-run loopback token instead)",
            "production alerts and unrelated HTTP endpoints",
        ],
        "baseline_source_provenance": SOURCE_PROVENANCE,
        "baseline_observation_adaptations": [
            "prompt tokens use the repository UTF-8 byte estimate including tools/response schema, not the source deployment tokenizer",
            "fixed generation cap is known before dispatch; actual output labels are excluded",
            "first usable output observes one token; remaining decode estimate stays fixed at (max_tokens-1)*tpot until terminal, with no elapsed-time decrement or chunk-to-token inference",
            "canceled local work retains physical slot and peak KV through configured grace; a closed transport is not engine cancellation acknowledgement",
        ],
        "fixed_history_replay": True,
        "audit_policy": "SQLite and decision/attempt/request boundaries fsynced; raw SSE chunks buffered and fsynced on close. Synchronous accounting and audit overhead remain in measured latency.",
        "tools_executed": False,
        "arrivals": "first round per session scheduled; later rounds previous completion plus predecessor tool_wait_s",
        "multi_round_arrival_note": "closed-loop arrivals are policy-dependent; cohort rates are not matched exogenous offered RPS",
    }
    journal.write_json("manifest.json", manifest)
    server = None
    server_task = None
    sock = None
    records: list[dict[str, Any]] = []
    try:
        if config["replay"].get("prewarm_tokenizer", False):
            manifest["tokenizer_prewarm"] = prewarm_tokenizer()
            journal.write_json("manifest.json", manifest)
        app = create_experiment_app(runtime, api_key)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        sock.listen(128)
        port = sock.getsockname()[1]
        server = uvicorn.Server(
            uvicorn.Config(
                app,
                host="127.0.0.1",
                port=port,
                access_log=False,
                log_level="warning",
                lifespan="off",
                timeout_graceful_shutdown=5,
            )
        )
        server_task = asyncio.create_task(server.serve(sockets=[sock]))
        while not server.started:
            if server_task.done():
                await server_task
                raise RuntimeError("experiment gateway failed to start")
            await asyncio.sleep(0.01)
        runtime.origin = time.monotonic()
        journal.append("client_events.jsonl", {"event": "replay_start", "at": _utc_now()})
        replay = config["replay"]
        speedup = replay.get("arrival_speedup", 1)
        semaphore = asyncio.Semaphore(replay.get("max_in_flight") or len(requests))
        timeout = aiohttp.ClientTimeout(total=replay.get("request_timeout_s", 180))
        connector = aiohttp.TCPConnector(limit=0)
        async with aiohttp.ClientSession(timeout=timeout, connector=connector) as client:

            async def run_session(group: list[Any]) -> None:
                previous = None
                predecessor_wait = 0.0
                for request in group:
                    eligible = (
                        request.arrival_s / speedup
                        if previous is None
                        else previous["end_s"] + predecessor_wait / speedup
                    )
                    await asyncio.sleep(max(0, eligible - runtime.now()))
                    record = await _one_request(
                        runtime,
                        client,
                        f"http://127.0.0.1:{port}/v1/chat/completions",
                        request,
                        eligible,
                        semaphore,
                    )
                    records.append(record)
                    previous = record
                    predecessor_wait = request.tool_wait_s

            await asyncio.gather(*(run_session(group) for group in group_sessions(requests)))
        duration = runtime.now()
        summary = summarize(records, config["slo"])
        summary.update(
            {
                "run_id": run_id,
                "duration_s": duration,
                "cohort_requests_per_s": len(records) / duration,
                "session_count": len(group_sessions(requests)),
                "successful_sessions": sum(
                    all(
                        r["status"] == "success"
                        for r in records
                        if r["session_id"] == group[0].session_id
                    )
                    for group in group_sessions(requests)
                ),
            }
        )
        journal.write_json("summary.json", summary)
        manifest["state"] = "completed"
        return summary
    except BaseException as exc:
        manifest["state"] = (
            "interrupted"
            if isinstance(exc, (KeyboardInterrupt, asyncio.CancelledError))
            else "failed"
        )
        manifest["failure_type"] = type(exc).__name__
        raise
    finally:
        if server is not None:
            server.should_exit = True
        if server_task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(server_task), timeout=8)
            except asyncio.TimeoutError:
                server.force_exit = True
                server_task.cancel()
                await asyncio.gather(server_task, return_exceptions=True)
        if sock is not None:
            sock.close()
        await runtime.close()
        manifest["ended_at"] = _utc_now()
        manifest["budget_after"] = ledger.snapshot()
        manifest["recorded_requests"] = len(records)
        journal.write_json("ledger.json", ledger.export_entries(run_id=run_id))
        journal.write_json("manifest.json", manifest)
        journal.close()


def main() -> None:
    """Parse paths only; credentials are read from the named environment variable."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--workload", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--ledger", required=True)
    parser.add_argument("--api-key-env", default="NIMBUS_DEEPSEEK_API_KEY")
    args = parser.parse_args()
    summary = asyncio.run(
        execute_run(
            args.config,
            args.workload,
            args.output,
            args.ledger,
            api_key=os.environ.get(args.api_key_env),
        )
    )
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
