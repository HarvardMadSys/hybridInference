"""The OpenInfra reporter: where it posts, what it needs, and that the gateway takes what it sends."""

from __future__ import annotations

import argparse
import json
import sys
from itertools import pairwise

import pytest

from ops.openinfra import reporter
from serving.openinfra.store import SnapshotWrite
from serving.openinfra.wire import parse_snapshot


def test_the_snapshot_goes_to_the_gateways_ingest_route():
    assert reporter.ingest_url("https://gateway.example.org/") == (
        "https://gateway.example.org/internal/openinfra/ingest"
    )
    assert reporter.ingest_url("http://10.0.0.5:8080") == (
        "http://10.0.0.5:8080/internal/openinfra/ingest"
    )


def test_post_sends_json_with_the_bearer_token(monkeypatch):
    sent = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b'{"ok": true, "gpus": 1, "sglang": 0}'

    def fake_urlopen(request, timeout):
        sent["url"] = request.full_url
        sent["headers"] = dict(request.header_items())
        sent["body"] = json.loads(request.data)
        sent["timeout"] = timeout
        return Response()

    monkeypatch.setattr(reporter.urllib.request, "urlopen", fake_urlopen)
    answer = reporter.post("https://gateway.example.org", "s3cret", {"node": {"id": "a"}})
    assert answer == {"ok": True, "gpus": 1, "sglang": 0}
    assert sent["url"] == "https://gateway.example.org/internal/openinfra/ingest"
    assert sent["headers"]["Authorization"] == "Bearer s3cret"
    assert sent["headers"]["Content-type"] == "application/json"
    assert sent["body"] == {"node": {"id": "a"}}


def _parse(monkeypatch, *argv: str, **env: str) -> argparse.Namespace:
    for name in ("OPENINFRA_URL", "OPENINFRA_TOKEN", "INGEST_TOKEN", "OPENINFRA_NODE_CONFIG"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(sys, "argv", ["reporter.py", *argv])
    return reporter.parse_args()


def test_the_gateway_address_is_required(monkeypatch, capsys):
    with pytest.raises(SystemExit):
        _parse(monkeypatch)
    assert "OPENINFRA_URL" in capsys.readouterr().err


def test_the_address_and_token_come_from_the_environment(monkeypatch):
    args = _parse(monkeypatch, OPENINFRA_URL="https://gateway.example.org", INGEST_TOKEN="t")
    assert (args.dashboard, args.token) == ("https://gateway.example.org", "t")
    assert args.region is None
    assert args.cluster is None


def test_a_node_configuration_supplies_identity_and_engines(monkeypatch, tmp_path):
    path = tmp_path / "node.json"
    path.write_text(
        json.dumps(
            {
                "dashboard": "https://gateway.example.org",
                "node_id": "node-a",
                "region": "lab",
                "flush_interval": 10,
                "sglang": [{"url": "http://127.0.0.1:30000/metrics", "id": "node-a-chat"}],
            }
        )
    )
    args = _parse(monkeypatch, "--config", str(path))
    assert (args.dashboard, args.node_id, args.region, args.flush_interval) == (
        "https://gateway.example.org",
        "node-a",
        "lab",
        10.0,
    )
    assert reporter.sglang_targets(args) == [
        {"url": "http://127.0.0.1:30000/metrics", "id": "node-a-chat"}
    ]


def test_the_example_node_configuration_is_valid(monkeypatch):
    from pathlib import Path

    example = Path(reporter.__file__).with_name("node.example.json")
    args = _parse(monkeypatch, "--config", str(example), OPENINFRA_URL="https://g.example.org")
    targets = reporter.sglang_targets(args)
    assert targets
    assert all({"url", "id", "name", "engine", "gpu_indexes"} <= set(t) for t in targets)


SGLANG_METRICS = """\
sglang:num_running_reqs{{model_name="org/chat"}} 3
sglang:num_queue_reqs{{model_name="org/chat"}} 1
sglang:num_used_tokens{{model_name="org/chat"}} 5000
sglang:max_total_num_tokens{{model_name="org/chat"}} 100000
sglang:token_usage{{model_name="org/chat"}} 0.05
sglang:gen_throughput{{model_name="org/chat"}} 250
sglang:prompt_tokens_total{{model_name="org/chat"}} {prompt}
sglang:generation_tokens_total{{model_name="org/chat"}} {generated}
sglang:num_requests_total{{model_name="org/chat"}} {requests}
sglang:time_to_first_token_seconds_sum{{model_name="org/chat"}} {ttft_sum}
sglang:time_to_first_token_seconds_count{{model_name="org/chat"}} {requests}
"""

VLLM_METRICS = """\
vllm:num_requests_running{{model_name="org/embed"}} 2
vllm:num_requests_waiting{{model_name="org/embed"}} 0
vllm:kv_cache_usage_perc{{model_name="org/embed"}} 0.1
vllm:prompt_tokens_total{{model_name="org/embed"}} {prompt}
vllm:generation_tokens_total{{model_name="org/embed"}} {generated}
vllm:request_success_total{{model_name="org/embed",finished_reason="stop"}} {requests}
"""


def _scraped(target: dict, text: str) -> dict:
    return {"target": target, **reporter.parse_sglang_body(text)}


def test_the_gateway_accepts_what_a_flush_sends(monkeypatch):
    """The reporter's real flush, fed a scrape, produces a body the gateway stores."""
    monkeypatch.setattr(reporter, "container_for_port", lambda port: None)
    sent: list[dict] = []
    monkeypatch.setattr(
        reporter, "post", lambda url, token, body: sent.append(body) or {"ok": True}
    )
    args = argparse.Namespace(
        node_id="node-a", dashboard="https://gateway.example.org", token="t", skip_gpu=False
    )
    node = {"id": "node-a", "hostname": "node-a", "region": "lab", "cluster": None}
    chat = {
        "url": "http://127.0.0.1:30000/metrics",
        "id": "node-a-chat",
        "name": "chat-model",
        "engine": "sglang",
        "endpoint": "http://node-a:30000",
        "gpu_indexes": [0],
    }
    embed = {
        "url": "http://127.0.0.1:8000/metrics",
        "id": "node-a-embed",
        "name": "embed-model",
        "engine": "vllm",
        "endpoint": "http://node-a:8000",
        "gpu_indexes": [1],
    }
    gpus = [
        {
            "index": 0,
            "name": "Test GPU",
            "utilization_pct": 70.0,
            "memory_used_mb": 1000.0,
            "memory_total_mb": 80000.0,
        },
        {"index": 1, "name": "Test GPU", "utilization_pct": 20.0},
    ]
    cpu = {"cpu_util_pct": 12.0, "load1": 0.5, "mem_used_mb": 1024, "mem_total_mb": 4096}

    def scrape(step: int) -> dict:
        return {
            "node-a-chat": _scraped(
                chat,
                SGLANG_METRICS.format(
                    prompt=1000 + 500 * step,
                    generated=2000 + 800 * step,
                    requests=10 + 2 * step,
                    ttft_sum=1.0 + 0.5 * step,
                ),
            ),
            "node-a-embed": _scraped(
                embed,
                VLLM_METRICS.format(prompt=100 + 50 * step, generated=0, requests=4 + step),
            ),
        }

    prev = reporter.flush(args, node, gpus, scrape(0), cpu, {}, 5.0, False)
    reporter.flush(args, node, gpus, scrape(1), cpu, prev, 5.0, True)

    assert len(sent) == 2
    body = sent[-1]
    snapshot = parse_snapshot(json.loads(json.dumps(body)))
    write = SnapshotWrite(snapshot, 1_800_000_000_000)
    assert snapshot.node is not None
    assert snapshot.node.id == "node-a"
    assert [g.index for g in snapshot.gpus] == [0, 1]
    assert [e.server.id for e in snapshot.engines] == ["node-a-chat", "node-a-embed"]
    assert [e.server.engine for e in snapshot.engines] == ["sglang", "vllm"]
    assert [row[0] for row in write.engines] == ["node-a-chat", "node-a-embed"]
    chat_metrics = snapshot.engines[0].metrics
    assert chat_metrics is not None
    assert chat_metrics["num_running_reqs"] == 3.0
    # The engine's busiest mapped GPU is reported as its SM utilization.
    assert chat_metrics["sm_util_pct"] == 70.0
    # Two requests in 5 s is 24 a minute.
    assert chat_metrics["req_per_min"] == pytest.approx(24.0)


def test_one_second_flushes_count_their_counters(monkeypatch):
    """A flush that comes a little early still gets rates from its counters."""
    monkeypatch.setattr(reporter, "container_for_port", lambda port: None)
    sent: list[dict] = []
    monkeypatch.setattr(
        reporter, "post", lambda url, token, body: sent.append(body) or {"ok": True}
    )
    args = argparse.Namespace(
        node_id="node-a", dashboard="https://gateway.example.org", token="t", skip_gpu=True
    )
    node = {"id": "node-a", "hostname": "node-a", "region": None, "cluster": None}
    chat = {
        "url": "http://127.0.0.1:30000/metrics",
        "id": "chat",
        "name": "chat",
        "engine": "sglang",
        "endpoint": "",
    }

    def scrape(requests: int) -> dict:
        text = SGLANG_METRICS.format(prompt=0, generated=0, requests=requests, ttft_sum=0)
        return {"chat": _scraped(chat, text)}

    prev = reporter.flush(args, node, [], scrape(10), None, {}, 1.0, False)
    reporter.flush(args, node, [], scrape(11), None, prev, 0.97, False, taken=1_700_000_000.5)

    # One request in 0.97 s.
    assert sent[-1]["sglang"][0]["metrics"]["req_per_min"] == pytest.approx(61.9, abs=0.1)
    assert sent[-1]["ts"] == 1_700_000_000_500
    assert "ts" not in sent[0]


# --- Both engines' figures mean the same ------------------------------------
#
# The text follows SGLang 0.5.20 and vLLM 0.34: SGLang's prompt, generation,
# cached and request counters move when a request finishes, its realtime and
# prefill_effective counters and vLLM's token counters as each step runs.

_SCHED = 'model_name="m",engine_type="unified",tp_rank="0",pp_rank="0",moe_ep_rank="0"'
_TOK = 'model_name="m",is_streaming="false"'
_VLLM = 'model_name="m",engine="0"'


def _sglang(**v: float) -> str:
    """SGLang /metrics text; a value left out leaves its series out."""
    series = {
        "gauge": f"sglang:gen_throughput{{{_SCHED}}}",
        "utilization": f"sglang:utilization{{{_SCHED}}}",
        "spec_len": f"sglang:spec_accept_length{{{_SCHED}}}",
        "spec_rate": f"sglang:spec_accept_rate{{{_SCHED}}}",
        "retract_gauge": f"sglang:num_retracted_reqs{{{_SCHED}}}",
        "retracts": f"sglang:num_retracted_requests_total{{{_SCHED}}}",
        "decode": f'sglang:realtime_tokens_total{{{_SCHED},mode="decode"}}',
        "rt_compute": f'sglang:realtime_tokens_total{{{_SCHED},mode="prefill_compute"}}',
        "rt_cache": f'sglang:realtime_tokens_total{{{_SCHED},mode="prefill_cache"}}',
        "input": f'sglang:prefill_effective_tokens_total{{{_SCHED},mode="input"}}',
        "device_hit": f'sglang:prefill_effective_tokens_total{{{_SCHED},mode="device_hit"}}',
        "host_hit": f'sglang:prefill_effective_tokens_total{{{_SCHED},mode="host_hit"}}',
        "storage_hit": f'sglang:prefill_effective_tokens_total{{{_SCHED},mode="storage_hit"}}',
        "prompt": f"sglang:prompt_tokens_total{{{_TOK}}}",
        "generated": f"sglang:generation_tokens_total{{{_TOK}}}",
        "cached": 'sglang:cached_tokens_total{model_name="m",cache_source="device"}',
        "requests": f"sglang:num_requests_total{{{_TOK}}}",
        "e2e_sum": f"sglang:e2e_request_latency_seconds_sum{{{_TOK}}}",
        "e2e_count": f"sglang:e2e_request_latency_seconds_count{{{_TOK}}}",
    }
    lines = [f"sglang:num_running_reqs{{{_SCHED}}} 4"]
    lines += [f"{series[k]} {value}" for k, value in v.items()]
    return "\n".join(lines)


def _vllm(**v: float) -> str:
    """vLLM /metrics text; a value left out leaves its series out."""
    series = {
        "prompt": f"vllm:prompt_tokens_total{{{_VLLM}}}",
        "generated": f"vllm:generation_tokens_total{{{_VLLM}}}",
        "cached": f"vllm:prompt_tokens_cached_total{{{_VLLM}}}",
        "computed": f'vllm:prompt_tokens_by_source_total{{{_VLLM},source="local_compute"}}',
        "requests": f'vllm:request_success_total{{{_VLLM},finished_reason="stop"}}',
        "queries": f"vllm:prefix_cache_queries_total{{{_VLLM}}}",
        "hits": f"vllm:prefix_cache_hits_total{{{_VLLM}}}",
        "preemptions": f"vllm:num_preemptions_total{{{_VLLM}}}",
        "drafts": f"vllm:spec_decode_num_drafts_total{{{_VLLM}}}",
        "draft_tokens": f"vllm:spec_decode_num_draft_tokens_total{{{_VLLM}}}",
        "accepted": f"vllm:spec_decode_num_accepted_tokens_total{{{_VLLM}}}",
        "ttft_sum": f"vllm:time_to_first_token_seconds_sum{{{_VLLM}}}",
        "ttft_count": f"vllm:time_to_first_token_seconds_count{{{_VLLM}}}",
        "e2e_sum": f"vllm:e2e_request_latency_seconds_sum{{{_VLLM}}}",
        "e2e_count": f"vllm:e2e_request_latency_seconds_count{{{_VLLM}}}",
    }
    lines = [f"vllm:num_requests_running{{{_VLLM}}} 4"]
    lines += [f"{series[k]} {value}" for k, value in v.items()]
    return "\n".join(lines)


def _windows(texts: list[str]) -> list[dict]:
    """The metrics of each one-second window between consecutive scrapes of *texts*."""
    scrapes = [{"target": {}, **reporter.parse_sglang_body(text)} for text in texts]
    return [
        reporter.snapshot_sglang(last, prev, 1.0)["metrics"] for prev, last in pairwise(scrapes)
    ]


def test_sglang_decode_is_counted_once_however_requests_finish():
    """A steady 1000 tokens/s reads as 1000 every second, a request finishing or not.

    SGLang's generation counter takes a request's whole generation when it
    ends; read beside the gauge, the windows a request ended in counted its
    tokens a second time.
    """
    windows = _windows(
        [
            _sglang(gauge=1000, decode=0, generated=0),
            _sglang(gauge=1000, decode=1000, generated=0),
            _sglang(gauge=1000, decode=2000, generated=2000),  # a request ends
            _sglang(gauge=1000, decode=3000, generated=2000),
        ]
    )
    assert [w["gen_throughput"] for w in windows] == [1000.0, 1000.0, 1000.0]


def test_sglang_without_a_step_counter_reads_its_gauge_not_finished_requests():
    windows = _windows(
        [
            _sglang(gauge=1000, generated=0),
            _sglang(gauge=1000, generated=2000),
            _sglang(gauge=900, generated=2000),
        ]
    )
    assert [w["gen_throughput"] for w in windows] == [1000, 900]


def test_sglang_prompt_rates_come_from_its_step_counters():
    """The window's prefill, not the prompts of the requests that ended in it."""
    windows = _windows(
        [
            _sglang(input=0, device_hit=0, host_hit=0, storage_hit=0, prompt=0, cached=0),
            # 1200 tokens computed, 600 read from the GPU cache and 200 from the
            # host's; no request has ended yet.
            _sglang(input=1200, device_hit=600, host_hit=200, storage_hit=0, prompt=0, cached=0),
            _sglang(input=1200, device_hit=600, host_hit=200, storage_hit=0, prompt=0, cached=0),
        ]
    )
    busy, idle = windows
    assert (busy["prefill_tps"], busy["cached_prefill_tps"]) == (1200.0, 800.0)
    assert busy["cache_hit_rate"] == pytest.approx(0.4)
    assert (busy["device_cached_tps"], busy["host_cached_tps"]) == (600.0, 200.0)
    assert (idle["prefill_tps"], idle["cached_prefill_tps"]) == (0.0, 0.0)
    assert idle["cache_hit_rate"] is None


def test_an_sglang_without_effective_counters_reads_its_realtime_ones():
    windows = _windows(
        [
            _sglang(rt_compute=100),  # no cache hit yet: prefill_cache has no series
            _sglang(rt_compute=400),
            _sglang(rt_compute=500, rt_cache=300),
        ]
    )
    assert [(w["prefill_tps"], w["cached_prefill_tps"]) for w in windows] == [
        (300.0, 0.0),
        (100.0, 300.0),
    ]
    assert windows[1]["cache_hit_rate"] == pytest.approx(0.75)


def test_vllm_prompt_rates_mean_what_sglangs_do():
    windows = _windows(
        [
            _vllm(prompt=0, computed=0, cached=0, generated=0),
            _vllm(prompt=2000, computed=1200, cached=800, generated=1000),
        ]
    )
    (window,) = windows
    assert (window["prefill_tps"], window["cached_prefill_tps"]) == (1200.0, 800.0)
    assert window["cache_hit_rate"] == pytest.approx(0.4)
    assert window["gen_throughput"] == 1000.0


def test_the_accept_length_counts_the_bonus_token_on_both_engines():
    """vLLM's counters give accepted drafts per step; SGLang's gauge, the bonus token too."""
    vllm = _windows(
        [
            _vllm(drafts=0, draft_tokens=0, accepted=0, generated=0),
            _vllm(drafts=100, draft_tokens=300, accepted=210, generated=310),
        ]
    )[0]
    sglang = _windows(
        [
            _sglang(decode=0, spec_len=3.1, spec_rate=0.7),
            _sglang(decode=310, spec_len=3.1, spec_rate=0.7),
        ]
    )[0]
    assert vllm["spec_accept_length"] == pytest.approx(sglang["spec_accept_length"]) == 3.1
    assert vllm["spec_accept_rate"] == pytest.approx(sglang["spec_accept_rate"]) == 0.7


def test_speculation_reads_nothing_when_off_or_idle():
    # SGLang reports zeros with speculative decoding off, as vLLM reports nothing.
    off = _windows([_sglang(decode=0, spec_len=0, spec_rate=0), _sglang(decode=50)])[0]
    assert off.get("spec_accept_length") is None
    assert off.get("spec_accept_rate") is None
    # SGLang's gauges keep their last value through idle time.
    for idle in (
        _windows([_sglang(decode=10, spec_len=3.1, spec_rate=0.7)] * 2)[0],
        _windows([_vllm(drafts=5, draft_tokens=15, accepted=9)] * 2)[0],
    ):
        assert (idle["spec_accept_length"], idle["spec_accept_rate"]) == (None, None)


def test_an_idle_window_is_blank_on_both_engines():
    """Not the averages since the engine started, which the samples carried before."""
    sglang = _windows([_sglang(e2e_sum=40.0, e2e_count=10)] * 2)[0]
    vllm = _windows(
        [_vllm(prompt=900, cached=300, queries=900, hits=300, ttft_sum=4.0, ttft_count=10)] * 2
    )[0]
    assert sglang["e2e_latency_avg_s"] is None
    assert vllm["ttft_avg_s"] is None
    assert vllm["cache_hit_rate"] is None
    busy = _windows([_sglang(e2e_sum=40.0, e2e_count=10), _sglang(e2e_sum=46.0, e2e_count=12)])
    assert busy[0]["e2e_latency_avg_s"] == pytest.approx(3.0)


def test_retractions_count_the_window_on_both_engines():
    sglang = _windows(
        [
            _sglang(retract_gauge=3, retracts=10),
            _sglang(retract_gauge=3, retracts=12),
            _sglang(retract_gauge=3, retracts=12),  # the gauge holds while idle
        ]
    )
    vllm = _windows([_vllm(preemptions=10), _vllm(preemptions=12), _vllm(preemptions=12)])
    assert [w["retracts"] for w in sglang] == [w["retracts"] for w in vllm] == [2, 0]


@pytest.mark.parametrize(("ratio", "pct"), [(0.42, 42.0), (1.05, 105.0), (0, None), (-1, None)])
def test_sglang_utilization_is_a_percentage(ratio, pct):
    scraped = reporter.parse_sglang_body(_sglang(utilization=ratio))["metrics"]
    assert scraped.get("sglang_util_pct") == pct
    loads = reporter.parse_sglang_body(json.dumps({"utilization": ratio}))["metrics"]
    assert loads.get("sglang_util_pct") == pct


def test_vllm_counters_are_summed_over_a_data_parallel_servers_engines():
    text = (
        _vllm(prompt=1000, generated=500, cached=400)
        + "\n"
        + _vllm(prompt=600, generated=300, cached=200).replace('engine="0"', 'engine="1"')
    )
    metrics = reporter.parse_vllm_prom(text)
    assert (metrics["prompt_tokens_total"], metrics["generation_tokens_total"]) == (1600, 800)
    assert metrics["cached_tokens_total"] == 600


def test_no_rate_is_taken_between_two_different_counters():
    """A /v1/loads target whose sibling /metrics fails for one scrape keeps its own count."""

    def loads(sampled: float) -> dict:
        return {"metrics": {"engine": "sglang", "loads_generated_total": sampled}}

    def both(sampled: float, step: float) -> dict:
        metrics = {**loads(sampled)["metrics"], "step_generated_total": step}
        return {"metrics": metrics}

    scrapes = [both(900, 50_000), loads(1_800), both(2_700, 52_000)]
    rates = [
        reporter.snapshot_sglang(last, prev, 1.0)["metrics"]["gen_throughput"]
        for prev, last in pairwise(scrapes)
    ]
    assert rates == [900.0, 900.0]


def test_a_log_read_fills_no_metric_the_counters_measured(monkeypatch):
    """The once-a-minute log read leaves an idle window's measured blanks blank."""
    log_stats = {
        "posts": 30.0,
        "http_errors": 0.0,
        "tiled": False,
        "cached_tokens": 500.0,
        "new_tokens": 1500.0,
        "prefill_s": 3.0,
        "prefill_done": 30.0,
        "decode_tps": 800.0,
        "chunked_prefill_avg_s": None,
        "req_prefill_avg_s": 0.1,
        "req_tpot_avg_s": 0.02,
        "req_e2e_avg_s": 9.0,
        "req_ttft_avg_s": 0.2,
        "req_prompt_avg": 2000,
        "req_uncached_avg": 1500,
        "req_gen_avg": 300,
        "req_spec_len": None,
        "req_spec_rate": None,
        "requests": [],
    }
    monkeypatch.setattr(reporter, "summarize_engine_logs", lambda *args: log_stats)
    monkeypatch.setattr(reporter, "_log_holds", {})
    sent: list[dict] = []
    monkeypatch.setattr(
        reporter, "post", lambda url, token, body: sent.append(body) or {"ok": True}
    )
    args = argparse.Namespace(
        node_id="node-a", dashboard="https://gateway.example.org", token="t", skip_gpu=True
    )
    node = {"id": "node-a", "hostname": "node-a", "region": None, "cluster": None}
    chat = {"url": "http://127.0.0.1:30000/metrics", "id": "chat", "name": "chat", "endpoint": ""}
    idle = _sglang(
        gauge=0,
        decode=5000,
        input=900,
        device_hit=100,
        host_hit=0,
        storage_hit=0,
        requests=7,
        e2e_sum=40.0,
        e2e_count=10,
    )
    scrape = {"chat": _scraped(chat, idle)}
    prev = reporter.flush(args, node, [], scrape, None, {}, 1.0, False)
    reporter.flush(args, node, [], scrape, None, prev, 1.0, True)
    metrics = sent[-1]["sglang"][0]["metrics"]
    assert metrics["e2e_latency_avg_s"] is None
    assert metrics["cache_hit_rate"] is None
    assert metrics["gen_throughput"] == 0.0
    assert (metrics["prefill_tps"], metrics["req_per_min"]) == (0.0, 0.0)


@pytest.mark.parametrize(
    ("now", "interval", "slot"),
    [
        (100.2, 1.0, 100.5),
        (100.5, 1.0, 101.5),  # its own slot has begun: the next one
        (100.9, 1.0, 101.5),
        (101.0, 5.0, 102.5),
        (103.0, 5.0, 107.5),
        (100.1, 0.5, 100.25),
    ],
)
def test_snapshots_are_taken_mid_interval(now, interval, slot):
    assert reporter.next_slot(now, interval) == pytest.approx(slot)


def test_a_slow_snapshot_skips_the_slots_it_overran():
    # A snapshot taken at 100.5 that took 2.3 s: 101.5 and 102.5 have gone.
    assert reporter.next_slot(102.8, 1.0) == pytest.approx(103.5)


def test_the_flush_interval_must_be_positive(monkeypatch, capsys):
    with pytest.raises(SystemExit):
        _parse(monkeypatch, "--flush-interval", "0", OPENINFRA_URL="https://g.example.org")
    assert "positive" in capsys.readouterr().err


def test_flushes_are_one_second_apart_and_logged_once_a_minute_by_default(monkeypatch):
    args = _parse(monkeypatch, OPENINFRA_URL="https://g.example.org")
    assert (args.flush_interval, args.log_interval) == (1.0, 60.0)


def test_a_repeating_failure_is_reported_once_per_interval(monkeypatch, capsys):
    clock = iter([0.0, 1.0, 2.0, 61.0])
    monkeypatch.setattr(reporter.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(reporter, "_error_log", {})
    monkeypatch.setattr(reporter, "_error_interval", 60.0)
    for _ in range(4):
        reporter.log_error("flush", "flush error: refused")
    assert capsys.readouterr().err.splitlines() == [
        "flush error: refused",
        "flush error: refused (2 more since the last report)",
    ]


@pytest.mark.parametrize(
    ("line", "parsed"),
    [
        ("    0    1200    3400", (0, {"pcie_rx_bps": 1.2e9, "pcie_tx_bps": 3.4e9})),
        ("# gpu  rxpci  txpci", None),
        ("# Idx   MB/s   MB/s", None),
        ("    1      -      -", None),
        ("", None),
    ],
)
def test_a_dmon_line(line, parsed):
    assert reporter.parse_dmon_line(line) == parsed


def test_the_pcie_monitor_reports_recent_readings(monkeypatch):
    lines = ["# gpu rxpci txpci\n", "    0  100  200\n", "    1  300  400\n"]

    class Process:
        terminated = False

        def __init__(self, *args, **kwargs):
            self.stdout = iter(lines)

        def poll(self):
            return None

        def terminate(self):
            Process.terminated = True

        def wait(self):
            raise SystemExit  # ends the reader thread

    monkeypatch.setattr(reporter.subprocess, "Popen", Process)
    monitor = reporter.PcieMonitor()
    monitor.latest()  # starts the reader
    monitor._thread.join(timeout=5)
    assert monitor.latest() == {
        0: {"pcie_rx_bps": 1e8, "pcie_tx_bps": 2e8},
        1: {"pcie_rx_bps": 3e8, "pcie_tx_bps": 4e8},
    }
    # Readings go stale.
    monkeypatch.setattr(reporter.PcieMonitor, "MAX_AGE_S", -1.0)
    assert monitor.latest() == {}
    # The reporter's exit ends it.
    monitor.stop()
    assert Process.terminated
