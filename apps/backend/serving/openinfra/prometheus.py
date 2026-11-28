"""Engine metrics from the Prometheus text an SGLang or vLLM server exposes at ``/metrics``.

A snapshot may carry an engine's raw ``prometheus`` text instead of (or as well
as) parsed ``metrics``. One server can expose several models; each model found
in a ``model_name`` label yields its own metrics, and the ingest gives it its
own engine row. Gauges take the largest value across matching series;
histogram averages divide ``_sum`` by ``_count`` after averaging the replicas
of a tensor-, pipeline-, expert- or data-parallel rank, so a model sharded
over four ranks is not counted four times.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

_LINE = re.compile(
    r"^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(?P<labels>[^}]*)\})?\s+"
    r"(?P<value>[-+]?(?:Inf|NaN|\d+(?:\.\d+)?(?:[eE][-+]?\d+)?))"
)
_LABEL = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:\\.|[^"\\])*)"')
#: Labels that tell a series' parallel replicas apart.
_RANK_LABELS = frozenset({"tp_rank", "pp_rank", "moe_ep_rank", "dp_rank"})


@dataclass(frozen=True)
class Sample:
    """One series value."""

    name: str
    labels: dict[str, str]
    value: float


def _unescape(value: str) -> str:
    return value.replace("\\n", "\n").replace('\\"', '"').replace("\\\\", "\\")


def parse(text: str) -> list[Sample]:
    """Every finite sample in *text*; comments, blank lines and ``Inf``/``NaN`` are skipped."""
    samples: list[Sample] = []
    for raw_line in text.split("\n"):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = _LINE.match(line)
        if match is None:
            continue
        try:
            value = float(match["value"])
        except ValueError:
            continue
        if not math.isfinite(value):
            continue
        labels = {key: _unescape(raw) for key, raw in _LABEL.findall(match["labels"] or "")}
        samples.append(Sample(match["name"], labels, value))
    return samples


def _matches(sample: Sample, name: str, model: str | None) -> bool:
    if sample.name != name:
        return False
    other = sample.labels.get("model_name")
    return not model or not other or other == model


def _gauge(samples: list[Sample], name: str, model: str | None) -> float | None:
    found = [s.value for s in samples if _matches(s, name, model)]
    return max(found) if found else None


def _gauge_any(samples: list[Sample], names: tuple[str, ...], model: str | None) -> float | None:
    for name in names:
        value = _gauge(samples, name, model)
        if value is not None:
            return value
    return None


def _counter_sum_any(
    samples: list[Sample], names: tuple[str, ...], model: str | None
) -> float | None:
    for name in names:
        found = [s.value for s in samples if _matches(s, name, model)]
        if found:
            return sum(found)
    return None


def _grouped_sum(
    samples: list[Sample],
    name: str,
    model: str | None,
    keep: Callable[[Sample], bool] | None = None,
) -> float | None:
    """Sum over label sets, each the mean of its rank replicas; None when no series matched."""
    groups: dict[str, list[float]] = {}
    for sample in samples:
        if not _matches(sample, name, model):
            continue
        if keep is not None and not keep(sample):
            continue
        key = ",".join(
            f"{k}={v}"
            for k, v in sorted(sample.labels.items())
            if k != "le" and k not in _RANK_LABELS
        )
        groups.setdefault(key, []).append(sample.value)
    if not groups:
        return None
    return sum(sum(values) / len(values) for values in groups.values())


def _histogram_avg(
    samples: list[Sample],
    base: str,
    model: str | None,
    keep: Callable[[Sample], bool] | None = None,
) -> float | None:
    total = _grouped_sum(samples, f"{base}_sum", model, keep)
    count = _grouped_sum(samples, f"{base}_count", model, keep)
    if total is None or count is None or count <= 0:
        return None
    return total / count


def _models(samples: list[Sample]) -> list[str | None]:
    models: list[str | None] = list(
        dict.fromkeys(s.labels["model_name"] for s in samples if "model_name" in s.labels)
    )
    return models or [None]


def _stage(name: str) -> Callable[[Sample], bool]:
    return lambda sample: sample.labels.get("stage") == name


def sglang_metrics(text: str) -> list[dict[str, Any]]:
    """One metrics mapping per model in SGLang's ``/metrics`` text."""
    samples = parse(text)
    out: list[dict[str, Any]] = []
    for model in _models(samples):

        def gauge(name: str, model: str | None = model) -> float | None:
            return _gauge(samples, name, model)

        def hist(
            base: str, keep: Callable[[Sample], bool] | None = None, model: str | None = model
        ) -> float | None:
            return _histogram_avg(samples, base, model, keep)

        tpot = hist("sglang:inter_token_latency_seconds")
        out.append(
            {
                "model_name": model,
                "num_running_reqs": gauge("sglang:num_running_reqs"),
                "num_queue_reqs": gauge("sglang:num_queue_reqs"),
                "num_used_tokens": gauge("sglang:num_used_tokens"),
                "token_usage": gauge("sglang:token_usage"),
                "gen_throughput": gauge("sglang:gen_throughput"),
                "cache_hit_rate": gauge("sglang:cache_hit_rate"),
                "prompt_tokens_total": gauge("sglang:prompt_tokens_total"),
                "generation_tokens_total": gauge("sglang:generation_tokens_total"),
                "ttft_avg_s": hist("sglang:time_to_first_token_seconds"),
                "queue_avg_s": hist("sglang:queue_time_seconds"),
                "prefill_avg_s": hist(
                    "sglang:per_stage_req_latency_seconds", _stage("prefill_forward")
                ),
                "e2e_latency_avg_s": hist("sglang:e2e_request_latency_seconds"),
                "tpot_avg_s": tpot
                if tpot is not None
                else hist("sglang:time_per_output_token_seconds"),
                "chunked_prefill_avg_s": hist(
                    "sglang:per_stage_req_latency_seconds", _stage("chunked_prefill")
                ),
                "hicache_host_used": gauge("sglang:hicache_host_used_tokens"),
                "hicache_host_total": gauge("sglang:hicache_host_total_tokens"),
                "prompt_tokens_avg": hist("sglang:prompt_tokens_histogram"),
                "uncached_prompt_tokens_avg": hist("sglang:uncached_prompt_tokens_histogram"),
                "generation_tokens_avg": hist("sglang:generation_tokens_histogram"),
                "retracts": gauge("sglang:num_retracted_reqs"),
                "spec_accept_length": gauge("sglang:spec_accept_length"),
                "spec_accept_rate": gauge("sglang:spec_accept_rate"),
            }
        )
    return out


def vllm_metrics(text: str) -> list[dict[str, Any]]:
    """One metrics mapping per model in vLLM's ``/metrics`` text; empty without ``vllm:`` series.

    No ``prefill_tps``: V0's ``vllm:avg_prompt_throughput_toks_per_s`` counted
    cached prompt tokens too, and ``prefill_tps`` holds the uncached rate.
    """
    samples = parse(text)
    if not any(s.name.startswith("vllm:") for s in samples):
        return []
    out: list[dict[str, Any]] = []
    for model in _models(samples):

        def gauge(name: str, model: str | None = model) -> float | None:
            return _gauge(samples, name, model)

        def hist(base: str, model: str | None = model) -> float | None:
            return _histogram_avg(samples, base, model)

        hits = _counter_sum_any(
            samples, ("vllm:prefix_cache_hits_total", "vllm:prefix_cache_hits"), model
        )
        queries = _counter_sum_any(
            samples, ("vllm:prefix_cache_queries_total", "vllm:prefix_cache_queries"), model
        )
        usage = gauge("vllm:kv_cache_usage_perc")
        tpot = hist("vllm:inter_token_latency_seconds")
        out.append(
            {
                "model_name": model,
                "num_running_reqs": gauge("vllm:num_requests_running"),
                "num_queue_reqs": gauge("vllm:num_requests_waiting"),
                "token_usage": usage if usage is not None else gauge("vllm:gpu_cache_usage_perc"),
                "prompt_tokens_total": gauge("vllm:prompt_tokens_total"),
                "generation_tokens_total": gauge("vllm:generation_tokens_total"),
                "gen_throughput": _gauge_any(
                    samples,
                    (
                        "vllm:avg_generation_throughput_toks_per_s",
                        "vllm:generation_tokens_per_second",
                    ),
                    model,
                ),
                "cache_hit_rate": hits / queries
                if hits is not None and queries is not None and queries > 0
                else None,
                "ttft_avg_s": hist("vllm:time_to_first_token_seconds"),
                "queue_avg_s": hist("vllm:request_queue_time_seconds"),
                "prefill_avg_s": hist("vllm:request_prefill_time_seconds"),
                "e2e_latency_avg_s": hist("vllm:e2e_request_latency_seconds"),
                "tpot_avg_s": tpot
                if tpot is not None
                else hist("vllm:request_time_per_output_token_seconds"),
                "prompt_tokens_avg": hist("vllm:request_prompt_tokens"),
                "generation_tokens_avg": hist("vllm:request_generation_tokens"),
            }
        )
    return out


def engine_metrics(text: str) -> list[dict[str, Any]]:
    """The metrics in an engine's ``/metrics`` text, read as vLLM's when it has ``vllm:`` series."""
    return vllm_metrics(text) if "vllm:" in text else sglang_metrics(text)
