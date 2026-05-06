# Inference Throughput Benchmark Report
## Qwen3.6-35B (SGLang) vs GLM-4.7-Flash (vLLM) on NVIDIA GB10

**Date:** 2026-05-05  
**Hardware:** NVIDIA GB10 (Grace-Blackwell), 120 GB unified memory, aarch64  
**Methodology:** Concurrency=1, 3 measured requests per config (+ 1 warmup), non-streaming.  
Prefill latency is measured by issuing `max_tokens=1` requests; decode throughput is derived as `(output_tokens − 1) / (total_latency − prefill_latency)`.

---

## Setup

| | Qwen3.6-35B | GLM-4.7-Flash |
|---|---|---|
| **Model** | `Qwen/Qwen3.6-35B-A3B-FP8` | `zai-org/GLM-4.7-Flash` |
| **Precision** | FP8 | BF16 |
| **Architecture** | MoE (3.6B active / 35B total) | MoE (`glm4_moe_lite`) |
| **Serving framework** | SGLang (`lmsysorg/sglang:latest`) | vLLM (`scitrera/dgx-spark-vllm:0.14.0-t5`) |
| **Port** | 9001 | 9002 |
| **GPU memory utilization** | 80% | 80% |
| **Max model len** | 262,144 | 8,192 |

> **Note on vLLM image selection:** `vllm/vllm-openai:latest` (v0.18.0) and `nvcr.io/nvidia/vllm:26.01-py3` (v0.13.0) both fail to load `zai-org/GLM-4.7-Flash` because they bundle a Transformers version that does not yet recognize the `glm4_moe_lite` architecture. `scitrera/dgx-spark-vllm:0.14.0-t5` (Transformers 5.0.0.dev0) supports it and was used instead.

---

## Results

### Prefill Throughput (tok/s)

Prefill throughput scales with prompt length as more tokens are processed in parallel. Both models benefit from longer prompts, but GLM-4.7-Flash achieves significantly higher prefill throughput at every input length.

| Prompt tokens | Qwen3.6-35B (SGLang) | GLM-4.7-Flash (vLLM) | Ratio (GLM/Qwen) |
|---|---|---|---|
| 115 | 1,085 tok/s | 864 tok/s | 0.80× |
| 448 | 2,728 tok/s | 3,028 tok/s | 1.11× |
| 886 | 3,993 tok/s | 5,219 tok/s | 1.31× |
| 1,763 | 5,146 tok/s | 11,847 tok/s | 2.30× |
| 3,506 | 5,876 tok/s | 16,581 tok/s | 2.82× |

**Observation:** At short prompts (115 tokens), Qwen3.6-35B is slightly faster to prefill. Past ~500 tokens, GLM-4.7-Flash pulls ahead, and at 3,500+ tokens it is nearly **3× faster** in prefill throughput. This likely reflects GLM's smaller model footprint (BF16 vs FP8, smaller KV heads from MLA) and vLLM's chunked-prefill scheduler, which batches more prefill tokens per forward pass at long context.

### Prefill Latency (ms)

| Prompt tokens | Qwen3.6-35B (SGLang) | GLM-4.7-Flash (vLLM) |
|---|---|---|
| 115 | 107 ms | 122 ms |
| 448 | 164 ms | 141 ms |
| 886 | 222 ms | 162 ms |
| 1,763 | 342 ms | 142 ms |
| 3,506 | 596 ms | 203 ms |

Qwen3.6-35B prefill latency grows roughly linearly with prompt length (107 → 596 ms over a ~30× token increase). GLM-4.7-Flash grows far more slowly (122 → 203 ms), suggesting it benefits more from parallelism in the prefill compute path.

### Decode Throughput (tok/s)

Decode throughput is measured as output tokens generated per second after the first token. It is largely independent of output length (as expected for autoregressive generation), but degrades slightly with longer context due to KV-cache attention overhead.

| Prompt tokens | Output tokens | Qwen3.6-35B (SGLang) | GLM-4.7-Flash (vLLM) |
|---|---|---|---|
| 115 | 64 | 51.7 tok/s | 26.0 tok/s |
| 115 | 256 | 51.5 tok/s | 25.7 tok/s |
| 115 | 512 | 51.4 tok/s | 25.6 tok/s |
| 448 | 256 | 51.5 tok/s | 25.6 tok/s |
| 886 | 256 | 51.3 tok/s | 25.3 tok/s |
| 1,763 | 256 | 51.1 tok/s | 24.5 tok/s |
| 3,506 | 256 | 50.6 tok/s | 23.7 tok/s |

**Observation:** Qwen3.6-35B delivers **~51 tok/s** decode, roughly **2× faster** than GLM-4.7-Flash's **~25 tok/s**, across all configurations. The gap is consistent and does not narrow with longer inputs or longer outputs. This is the dominant performance difference between the two models on this hardware.

The decode advantage of Qwen3.6-35B likely comes from two factors: (1) FP8 precision reduces memory bandwidth pressure during KV-cache reads and weight fetches, and (2) SGLang's CUDA graph execution path is more aggressively optimized for single-sequence decode on Blackwell.

### End-to-End Latency (ms)

For a representative workload of **1,024 input tokens and 256 output tokens**:

| Model | Prefill | Decode | Total |
|---|---|---|---|
| Qwen3.6-35B (SGLang) | 222 ms | 4,974 ms | **5,196 ms** |
| GLM-4.7-Flash (vLLM) | 161 ms | 10,090 ms | **10,250 ms** |

Qwen3.6-35B is **~2× faster end-to-end** on this typical workload, driven entirely by the decode gap. The 61 ms prefill advantage of GLM is negligible compared to the ~5 second difference in decode time.

At shorter outputs (64 tokens), the gap narrows slightly — GLM's faster prefill partially compensates:

| Model | in=1024, out=64 total |
|---|---|
| Qwen3.6-35B | **1,453 ms** |
| GLM-4.7-Flash | 2,651 ms |

Even here, Qwen3.6-35B is 1.8× faster.

---

## Summary

| Metric | Winner | Margin |
|---|---|---|
| **Decode throughput** | Qwen3.6-35B (SGLang) | ~2× (51 vs 25 tok/s) |
| **Prefill throughput (long context)** | GLM-4.7-Flash (vLLM) | ~3× at 3,500 tok input |
| **Prefill latency** | GLM-4.7-Flash (vLLM) | ~3× faster at 3,500 tok |
| **End-to-end latency (typical)** | Qwen3.6-35B (SGLang) | ~2× faster |

**Qwen3.6-35B on SGLang is the better choice for most serving scenarios** on this hardware, because decode dominates end-to-end latency for any output longer than a few tokens.

**GLM-4.7-Flash on vLLM has a clear prefill advantage** and would be preferred for workloads that are heavily prefill-bound (e.g., long-context summarization or classification where outputs are short), or where very low time-to-first-token matters more than generation speed.

---

## Caveats

- **Concurrency=1 only.** These are single-request, single-sequence measurements. At higher concurrency, SGLang and vLLM scheduling policies (continuous batching, chunked prefill) would change the relative throughput.
- **vLLM MoE config warning.** The vLLM container logs `Using default MoE config. Performance might be sub-optimal` for GLM. A tuned MoE kernel config could improve GLM decode throughput.
- **SGLang DeepGEMM warning.** SGLang logs `scale_fmt of checkpoint is not ue8m0, might cause accuracy degradation`. This is a Blackwell-specific FP8 format mismatch; performance is unaffected but numerical precision may differ slightly from a native FP8 deployment.
- **Max context length difference.** SGLang was launched with 262K context; vLLM was capped at 8K. KV cache allocation differs, which may affect memory pressure at longer contexts.
- **Framework versions are not matched.** SGLang and vLLM are different codebases at different maturity levels for GB10 support. Differences partly reflect framework optimization, not just model architecture.
