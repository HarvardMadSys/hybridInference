# Inference Throughput Benchmark — NVIDIA GB10 Local Deployment

**Date:** 2026-05-05  
**Hardware:** NVIDIA GB10 (Grace-Blackwell), 120 GB unified memory (aarch64)  
**Concurrency:** 1 (single-request, single-sequence)  
**Samples:** 3 measured requests per configuration + 1 warmup  

---

## Setup

| Config | Model | Precision | Framework | Image |
|---|---|---|---|---|
| **Qwen / SGLang** | `Qwen/Qwen3.6-35B-A3B-FP8` | FP8 | SGLang | `lmsysorg/sglang:latest` |
| **Qwen / vLLM** | `Qwen/Qwen3.6-35B-A3B-FP8` | FP8 | vLLM v0.20.1 | `vllm/vllm-openai:v0.20.1-ubuntu2404` |
| **GLM / vLLM** | `zai-org/GLM-4.7-Flash` | BF16 | vLLM v0.20.1 | `scitrera/dgx-spark-vllm:0.14.0-t5`† |

† Standard vLLM images (v0.18.0, v0.13.0) do not support the `glm4_moe_lite` architecture. The scitrera image bundles Transformers 5.0.0.dev0 which includes GLM-4.7 support. vLLM v0.20.1 (Transformers 5.7.0) was used for the Qwen run because `qwen3_5_moe` is supported from that version onwards.

**Methodology.** Prefill latency is isolated by sending `max_tokens=1` requests against the same prompt. Decode throughput is computed as `(output_tokens − 1) / (total_latency − prefill_latency)` using the full-generation response. Both phases are measured separately and averaged over 3 runs.

---

## 1. Decode Throughput

![Decode throughput vs prompt length](fig1_decode_throughput.png)

Decode throughput is the dominant cost for any response longer than a few tokens. All measurements are averaged across output lengths 64–512.

| Prompt tokens | Qwen / SGLang | Qwen / vLLM | GLM / vLLM |
|---|---|---|---|
| ~115 | 51.6 tok/s | **53.2 tok/s** | 25.7 tok/s |
| ~448 | 51.3 tok/s | **53.4 tok/s** | 25.6 tok/s |
| ~886 | 51.2 tok/s | **53.3 tok/s** | 25.3 tok/s |
| ~1,763 | 51.0 tok/s | **53.1 tok/s** | 24.6 tok/s |
| ~3,506 | 50.6 tok/s | **52.7 tok/s** | 23.7 tok/s |

**Key observations:**

- **Qwen3.6-35B on vLLM v0.20.1 is the fastest decoder at ~53 tok/s**, edging SGLang by ~4% despite using the same FP8 weights. This is consistent and statistically significant across all input lengths.
- **Qwen3.6-35B on SGLang delivers ~51 tok/s**, close to vLLM but with slightly higher variance. SGLang uses CUDA graphs and a custom MoE dispatch path tuned for Blackwell.
- **GLM-4.7-Flash on vLLM is ~2× slower in decode (~25 tok/s)** throughout. This is likely attributable to: (a) BF16 vs FP8 — BF16 doubles memory bandwidth demand per token; (b) the default MoE kernel config (vLLM logged `Using default MoE config — performance might be sub-optimal`); (c) GLM-4.7-Flash being a larger active-parameter model per decode step than Qwen3.6's MoE routing suggests.
- Decode throughput decreases slightly with longer inputs (~2% across the 30× input range tested) due to the growing KV cache inflating attention cost per decode step.

![Decode throughput vs output length (in≈886)](fig4_decode_vs_output.png)

Decode throughput is stable across output lengths 64–512, confirming it is not affected by response length.

---

## 2. Prefill Throughput

![Prefill throughput vs prompt length](fig2_prefill_throughput.png)

| Prompt tokens | Qwen / SGLang | Qwen / vLLM | GLM / vLLM |
|---|---|---|---|
| ~115 | **1,085 tok/s** | 968 tok/s | 864 tok/s |
| ~448 | **2,728 tok/s** | 2,648 tok/s | 3,014 tok/s |
| ~886 | **3,993 tok/s** | 3,776 tok/s | 5,213 tok/s |
| ~1,763 | **5,146 tok/s** | 5,008 tok/s | 11,847 tok/s |
| ~3,506 | **5,876 tok/s** | 5,279 tok/s | **16,631 tok/s** |

**Key observations:**

- **SGLang leads prefill for Qwen3.6-35B** at every input length, beating vLLM by 5–11%. The gap is widest at short inputs (115 tokens: 1,085 vs 968 tok/s, +12%) and narrows at long inputs.
- **GLM-4.7-Flash shows dramatically superior prefill scaling** beyond ~500 prompt tokens. At 3,500 tokens its prefill throughput (16,631 tok/s) is **3.2× higher than Qwen3.6-35B on SGLang** (5,876 tok/s). This is consistent with GLM-4.7-Flash having a more parallelism-friendly attention architecture (MLA with compressed KV) and vLLM's chunked-prefill scheduler batching more tokens per forward pass.
- Both Qwen configs plateau around 5,000–6,000 tok/s at long inputs, suggesting the per-layer MoE expert routing has become the bottleneck rather than attention.

## 3. Prefill Latency

![Prefill latency vs prompt length](fig3_prefill_latency.png)

| Prompt tokens | Qwen / SGLang | Qwen / vLLM | GLM / vLLM |
|---|---|---|---|
| ~115 | **106 ms** | 118 ms | 122 ms |
| ~448 | **164 ms** | 170 ms | 141 ms |
| ~886 | **222 ms** | 235 ms | 162 ms |
| ~1,763 | **341 ms** | 352 ms | 142 ms |
| ~3,506 | **596 ms** | 664 ms | 203 ms |

GLM-4.7-Flash's prefill latency grows sublinearly: from 122 ms at 115 tokens to only 203 ms at 3,506 tokens (+66%), while Qwen grows from 106 ms to 596 ms (+462%). The crossover where GLM becomes faster in prefill latency is around 450 prompt tokens.

---

## 4. End-to-End Latency

![End-to-end latency at in≈886 tokens](fig5_e2e_latency.png)

For the representative workload of **~886 prompt tokens**:

| Output tokens | Qwen / SGLang | Qwen / vLLM | GLM / vLLM |
|---|---|---|---|
| 64 | 1.45 s | **1.42 s** | 2.65 s |
| 128 | 2.70 s | **2.61 s** | 5.17 s |
| 256 | 5.20 s | **5.02 s** | 10.25 s |
| 512 | 10.18 s | **9.81 s** | 20.45 s |

Qwen3.6-35B on vLLM is the fastest end-to-end configuration for all output lengths. The advantage over GLM-4.7-Flash grows with output length because decode dominates. At 512 output tokens, Qwen/vLLM is **2.1× faster** than GLM/vLLM.

---

## 5. SGLang vs vLLM for Qwen3.6-35B

![SGLang vs vLLM comparison](fig6_sglang_vs_vllm.png)

The two frameworks are closely matched on this model:

| Metric | SGLang | vLLM v0.20.1 | Delta |
|---|---|---|---|
| Decode throughput | 51.2 tok/s | **53.2 tok/s** | vLLM +4% |
| Prefill throughput (886 tok) | **3,993 tok/s** | 3,776 tok/s | SGLang +6% |
| Prefill latency (886 tok) | **222 ms** | 235 ms | SGLang −6% |
| E2E latency (886 in, 256 out) | 5.20 s | **5.02 s** | vLLM −3% |

The differences are small enough that framework choice for Qwen3.6-35B on GB10 should be driven by operational factors (ease of deployment, observability, feature support) rather than raw throughput.

---

## Summary

| | Qwen3.6-35B / SGLang | Qwen3.6-35B / vLLM | GLM-4.7-Flash / vLLM |
|---|---|---|---|
| **Decode throughput** | 51 tok/s | **53 tok/s** | 25 tok/s |
| **Prefill throughput (short, ~115 tok)** | **1,085 tok/s** | 968 tok/s | 864 tok/s |
| **Prefill throughput (long, ~3,500 tok)** | 5,876 tok/s | 5,279 tok/s | **16,631 tok/s** |
| **Prefill latency (3,500 tok)** | 596 ms | 664 ms | **203 ms** |
| **E2E latency (886 in, 256 out)** | 5.20 s | **5.02 s** | 10.25 s |
| **Best use case** | General serving | General serving | Long-context prefill-heavy |

**Recommendations:**

- **For general chat and agentic workloads** (moderate prompt, multi-token response): use **Qwen3.6-35B on vLLM v0.20.1** — it delivers the best end-to-end latency and is stable across all configurations.
- **If decode throughput is the only concern** (e.g., streaming long responses): vLLM's ~4% edge over SGLang is marginal; either framework is acceptable.
- **For prefill-dominated workloads** (RAG retrieval, document summarization, classification over long context with short output): **GLM-4.7-Flash on vLLM** is the better choice — its prefill throughput at 3,500 tokens is 3× higher and its prefill latency is 3× lower than Qwen3.6-35B.
- **Note:** The vLLM MoE kernel for GLM-4.7-Flash is untuned (default config). A tuned expert-placement config could meaningfully improve its decode throughput.

---

## Figures

| File | Description |
|---|---|
| `fig1_decode_throughput.png` | Decode throughput vs prompt length (all 3 configs) |
| `fig2_prefill_throughput.png` | Prefill throughput vs prompt length |
| `fig3_prefill_latency.png` | Prefill latency vs prompt length |
| `fig4_decode_vs_output.png` | Decode throughput vs output length (in≈886) |
| `fig5_e2e_latency.png` | End-to-end latency by output length (in≈886) |
| `fig6_sglang_vs_vllm.png` | SGLang vs vLLM head-to-head for Qwen3.6-35B |

## Raw Data

| File | Description |
|---|---|
| `../../ops/perf/results_qwen_sglang.json` | Qwen3.6-35B on SGLang (20 configs) |
| `../../ops/perf/results_qwen_vllm.json` | Qwen3.6-35B on vLLM v0.20.1 (20 configs) |
| `../../ops/perf/results_glm_vllm.json` | GLM-4.7-Flash on vLLM (20 configs) |
