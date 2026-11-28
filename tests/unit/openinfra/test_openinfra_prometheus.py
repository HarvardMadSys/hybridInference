"""Engine metrics from SGLang's and vLLM's Prometheus text."""

from __future__ import annotations

import pytest

from serving.openinfra import prometheus

SGLANG = """\
# HELP sglang:num_running_reqs The number of running requests.
# TYPE sglang:num_running_reqs gauge
sglang:num_running_reqs{model_name="org/chat",tp_rank="0"} 6.0
sglang:num_running_reqs{model_name="org/chat",tp_rank="1"} 4.0
sglang:num_queue_reqs{model_name="org/chat"} 3
sglang:token_usage{model_name="org/chat"} 0.42
sglang:gen_throughput{model_name="org/chat"} 1234.5
sglang:time_to_first_token_seconds_sum{model_name="org/chat",tp_rank="0"} 10.0
sglang:time_to_first_token_seconds_sum{model_name="org/chat",tp_rank="1"} 12.0
sglang:time_to_first_token_seconds_count{model_name="org/chat",tp_rank="0"} 20
sglang:time_to_first_token_seconds_count{model_name="org/chat",tp_rank="1"} 24
sglang:time_to_first_token_seconds_bucket{model_name="org/chat",le="+Inf"} 44
sglang:per_stage_req_latency_seconds_sum{model_name="org/chat",stage="prefill_forward"} 4
sglang:per_stage_req_latency_seconds_count{model_name="org/chat",stage="prefill_forward"} 8
sglang:per_stage_req_latency_seconds_sum{model_name="org/chat",stage="chunked_prefill"} 9
sglang:per_stage_req_latency_seconds_count{model_name="org/chat",stage="chunked_prefill"} 3
sglang:time_per_output_token_seconds_sum{model_name="org/chat"} 1
sglang:time_per_output_token_seconds_count{model_name="org/chat"} 50
sglang:spec_accept_length{model_name="org/chat"} NaN
sglang:cache_hit_rate{model_name="org/chat"} +Inf
"""


def test_parse_skips_comments_and_non_finite_values():
    samples = prometheus.parse('a 1\n# HELP a x\nb{x="1",y="q\\"t"} 2e3\nc NaN\nd +Inf\n\n')
    assert [(s.name, s.labels, s.value) for s in samples] == [
        ("a", {}, 1.0),
        ("b", {"x": "1", "y": 'q"t'}, 2000.0),
    ]


def test_sglang_text():
    [metrics] = prometheus.sglang_metrics(SGLANG)
    assert metrics["model_name"] == "org/chat"
    # A gauge takes the largest series.
    assert metrics["num_running_reqs"] == 6.0
    assert metrics["num_queue_reqs"] == 3.0
    assert metrics["token_usage"] == 0.42
    # Rank replicas are averaged before the sum is divided by the count.
    assert metrics["ttft_avg_s"] == pytest.approx(11.0 / 22.0)
    assert metrics["prefill_avg_s"] == 0.5
    assert metrics["chunked_prefill_avg_s"] == 3.0
    # Without inter-token latency, time per output token stands in.
    assert metrics["tpot_avg_s"] == 0.02
    assert metrics["spec_accept_length"] is None
    assert metrics["cache_hit_rate"] is None


def test_one_mapping_per_model():
    text = 'sglang:num_running_reqs{model_name="a"} 1\nsglang:num_running_reqs{model_name="b"} 2\n'
    assert [(m["model_name"], m["num_running_reqs"]) for m in prometheus.sglang_metrics(text)] == [
        ("a", 1.0),
        ("b", 2.0),
    ]


def test_text_without_models_is_one_mapping():
    [metrics] = prometheus.engine_metrics("sglang:num_queue_reqs 9\n")
    assert metrics["model_name"] is None
    assert metrics["num_queue_reqs"] == 9.0


VLLM = """\
vllm:num_requests_running{model_name="org/chat"} 5
vllm:num_requests_waiting{model_name="org/chat"} 1
vllm:gpu_cache_usage_perc{model_name="org/chat"} 0.3
vllm:prefix_cache_hits_total{model_name="org/chat"} 30
vllm:prefix_cache_queries_total{model_name="org/chat"} 120
vllm:e2e_request_latency_seconds_sum{model_name="org/chat"} 30
vllm:e2e_request_latency_seconds_count{model_name="org/chat"} 10
vllm:request_time_per_output_token_seconds_sum{model_name="org/chat"} 2
vllm:request_time_per_output_token_seconds_count{model_name="org/chat"} 100
vllm:avg_prompt_throughput_toks_per_s{model_name="org/chat"} 999
"""


def test_vllm_text():
    [metrics] = prometheus.engine_metrics(VLLM)
    assert metrics["num_running_reqs"] == 5.0
    assert metrics["num_queue_reqs"] == 1.0
    assert metrics["token_usage"] == 0.3
    assert metrics["cache_hit_rate"] == 0.25
    assert metrics["e2e_latency_avg_s"] == 3.0
    assert metrics["tpot_avg_s"] == 0.02
    # The V0 prompt throughput counts cached tokens too, so it is not prefill.
    assert "prefill_tps" not in metrics


def test_vllm_without_vllm_series_is_nothing():
    assert prometheus.vllm_metrics("sglang:num_queue_reqs 1\n") == []
