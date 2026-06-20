# DeepSeek-V4-Flash (FP8) Throughput Benchmark — vLLM, TP=2 × PP=2

**Date:** 2026-06-20
**Author:** Juncheng Yang
**Hardware:** 4 × NVIDIA H200 (TP=2 × PP=2)
**Model:** [`sgl-project/DeepSeek-V4-Flash-FP8`](https://huggingface.co/sgl-project/DeepSeek-V4-Flash-FP8) — DeepseekV4 MoE, FP8 block-quantized, ~294 GB on disk (46 shards)
**Engine:** vLLM 0.23.0 · **Harness:** NVIDIA `genai-perf` 0.0.16

> **Status: results pending.** The harness and the vLLM launch configuration are
> complete and validated up to model load, but the benchmark run has **not yet
> produced numbers** — see [§ Status](#status). This is committed as a draft so
> the run can be reproduced as soon as the GPUs are healthy.

---

## Layout

```
benchmark/local/deepseek-v4-flash/
├── README.md                     # this file
└── scripts/
    ├── config.py                 # central config (TP=2, PP=2, FP8, sweep matrix)
    ├── orchestrate.py            # resumable, sentinel-gated pipeline driver (vLLM only)
    ├── run.sh                    # entry point (wires up the genai-perf venv)
    ├── run_genai_perf.py         # genai-perf argv wrapper
    ├── aggregate.py · plot.py · state.py
    └── engines/vllm.sh           # native vLLM launcher: TP=2 × PP=2 on GPUs 0-3
```

Adapted from the [minimax-m2.7](../minimax-m2.7/README.md) benchmark: same
genai-perf batteries (prefill 1K/4K/16K/32K, decode concurrency 1/4/16/32),
single engine (vLLM), native `uv` venv (no Docker on this shared host).

## vLLM configuration for DeepSeek-V4-Flash

Bringing this model up on vLLM 0.23 at TP=2 × PP=2 required three non-obvious
flags (all baked into `scripts/engines/vllm.sh`):

| Symptom at startup | Fix |
|---|---|
| `Assertion failed: !cubin.empty() …` in FlashInfer DeepGEMM | `VLLM_USE_DEEP_GEMM=0` — no CUDA toolkit (`nvcc`) on the host, so the FP8 DeepGEMM kernel can't JIT; falls back to the prebuilt CUTLASS FP8 block-scale GEMM |
| `DeepseekV4 FlashMLA fp8 layout only supports fp8 kv-cache, got auto` | `--kv-cache-dtype fp8` — the FP8 FlashMLA path rejects a bf16/auto KV cache |
| `The size of tensor a (2048) must match … b (4096)` in MoE `_load_w13` | `--enable-expert-parallel` — DeepSeek MoE experts must be distributed whole (expert parallelism), not tensor-sliced per expert |

The launch line (4 GPUs, ~74 GB weights/GPU, FP8 MLA KV):

```bash
CUDA_VISIBLE_DEVICES=0,1,2,3 VLLM_USE_DEEP_GEMM=0 \
  vllm serve <model_dir> --served-model-name deepseek-v4-flash \
    --tensor-parallel-size 2 --pipeline-parallel-size 2 --enable-expert-parallel \
    --max-model-len 40960 --gpu-memory-utilization 0.90 \
    --kv-cache-dtype fp8 --trust-remote-code --port 8000
```

## Running

```bash
# weights (294 GB FP8) — once
hf download sgl-project/DeepSeek-V4-Flash-FP8 \
  --local-dir /netscratch/juncheng/models/DeepSeek-V4-Flash-FP8

# full pipeline (serve -> prefill + decode batteries -> CSV + plots)
cd benchmark/local/deepseek-v4-flash/scripts
bash run.sh
```

The pipeline is sentinel-gated (`results/.state/`) and resumable.

## Status

The run reached **model load** but did not complete. After the three fixes
above, the TP=2 × PP=2 startup crashed mid-init, and the crashed
pipeline-parallel workers became **unreapable zombies** (`Zl`, threads stuck
inside the NCCL/CUDA driver) holding ~40 GB/GPU. They wedged the CUDA driver
for the whole node — every subsequent process fails CUDA init with
`device >= 0 && device < num_gpus INTERNAL ASSERT FAILED`.

Recovery needs a GPU reset or reboot: `nvidia-smi --gpu-reset` returned
`Not Supported` for one GPU (NVSwitch/Fabric-Manager node) and `In use by
another client` for the rest (the unkillable zombies). A reboot was deferred
because another tenant was active on GPU 0.

**To finish:** reboot (or otherwise clear the wedged driver state), then run
`bash scripts/run.sh`. The expert-parallel weight-load fix had not been
exercised end-to-end when the node wedged, so re-confirm load on the first
healthy run.
