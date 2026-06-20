# local-deployment-proxy

A lightweight reverse proxy that lazily starts and stops sglang Docker containers for multiple models. The proxy port stays open permanently; GPU-heavy containers are only running when there is active traffic. The least-used GPU is auto-selected.

Serves both **chat** and **embedding** models from one proxy/port. Supports
**Qwen3.6-35B-A3B-FP8**, **GLM-4.7-Flash**, and the **bge-m3** embedding model
out of the box. Add more by editing `models.json` (set `"is_embedding": true`
for embedding models). Every model is loaded on-demand and stopped when idle.

## How it works

```
Client → spark2:8001 ──SSH tunnel──→ GPU box :8001 (proxy)
                                        ├─ model="Qwen/..."             → :18001 (sglang on GPU 1)
                                        ├─ model="zai-org/GLM-4.7-Flash" → :18002 (sglang on GPU 2)
                                        └─ model="BAAI/bge-m3"          → :18012 (sglang --is-embedding on GPU 1)
```

1. The proxy listens on port 8001 and accepts all incoming HTTP requests.
2. Requests are routed to the correct backend based on the `model` field in the request body.
3. If an `hf_repo` model is not installed, the first request downloads it from Hugging Face.
4. It picks the configured/least-used GPU and launches the sglang container.
5. It waits for the container's `/v1/models` health endpoint, then proxies all traffic.
6. After **24 minutes** with no incoming requests for a model, that container is stopped.
7. The proxy keeps listening — the next request re-starts the container automatically.
8. `GET /v1/models` returns a static list of all configured models (no backend needed).

## Quick start

### Foreground (Ctrl-C to quit)

```bash
python local_deployment_proxy/local_deployment_proxy.py
```

### Background daemon

```bash
# Local only
./local_deployment_proxy/local_deployment_service.sh start

# With SSH reverse tunnel to a public LLM router
SSH_HOST='spark2|internal.freeinference.org' REMOTE_PORT=8001 ./local_deployment_proxy/local_deployment_service.sh start

# With API key auth
LOCAL_API_KEY='your-secret-key' ./local_deployment_proxy/local_deployment_service.sh start

# Check / stop
./local_deployment_proxy/local_deployment_service.sh status
./local_deployment_proxy/local_deployment_service.sh stop    # stops proxy + tunnel + all containers
```

Logs are written to `/tmp/local_deployment_proxy_8001.log`.

## Usage with OpenAI-compatible clients

```python
from openai import OpenAI

# Qwen
client = OpenAI(base_url="http://spark2:8001/v1", api_key="unused")
resp = client.chat.completions.create(
    model="Qwen/Qwen3.6-35B-A3B-FP8",
    messages=[{"role": "user", "content": "Hello!"}],
)
print(resp.choices[0].message.content)

# GLM
resp = client.chat.completions.create(
    model="zai-org/GLM-4.7-Flash",
    messages=[{"role": "user", "content": "Hello!"}],
)
```

If `LOCAL_API_KEY` is set, pass it as the `api_key` to the client or via `Authorization: Bearer` header.

The first request for a model after an idle period will block until the container is ready (~120s for non-streaming, or return a warm-up SSE for streaming). Subsequent requests are proxied immediately.

## Warm-up UX

When a model's backend is not running and a request comes in:

- **Streaming chat** (`"stream": true`) — immediately returns an SSE stream with a "thinking" warm-up message, then starts the backend in the background. The client should retry after ~120s.
- **Non-streaming / other endpoints** — blocks until the backend is ready, then proxies the response.

## Model configuration

Models are defined in `local_deployment_proxy/models.json`:

```json
{
    "Qwen/Qwen3.6-35B-A3B-FP8": {
        "container": "qwen36-sglang",
        "gpu_index": "1",
        "backend_port": 18001,
        "model_dir": "/netscratch/juncheng/models/Qwen3.6-35B-A3B-FP8",
        "served_name": "Qwen/Qwen3.6-35B-A3B-FP8",
        "max_model_len": 135168,
        "mem_fraction": "0.90",
        "tool_call_parser": "qwen3_coder"
    },
    "zai-org/GLM-4.7-Flash": {
        "container": "glm47-sglang",
        "gpu_index": "2",
        "backend_port": 18002,
        "model_dir": "/netscratch/juncheng/models/GLM-4.7-Flash",
        "served_name": "zai-org/GLM-4.7-Flash",
        "max_model_len": 131072,
        "mem_fraction": "0.90",
        "tool_call_parser": "glm47"
    }
}
```

| Field | Description |
|---|---|
| `container` | Docker container name |
| `engine` | serving engine: `sglang` (default) or `vllm` |
| `gpu_index` | GPU device index, or a comma list for tensor parallelism (e.g. `"0,1,2,3"`); omit to auto-pick |
| `tensor_parallel_size` | number of GPUs to shard the model across (default `1`); when >1 the launch gets `--tp`/`--tensor-parallel-size N` and Docker `--ipc=host` for NCCL |
| `colocate_group` | optional label; models sharing a value run on the same auto-picked GPU |
| `backend_port` | Host port mapped to the container (→ sglang `8001` / vLLM `8000` internally) |
| `model_dir` | Host path to model weights |
| `hf_repo` | optional Hugging Face repository downloaded into `model_dir` when absent |
| `hf_revision` | optional Hugging Face branch, tag, or commit |
| `hf_ignore_patterns` | optional file glob(s) excluded from the Hugging Face download (string or list) |
| `served_name` | `--served-model-name` for sglang |
| `max_model_len` | `--context-length` |
| `mem_fraction` | `--mem-fraction-static` |
| `tool_call_parser` | `--tool-call-parser` (omit to disable; chat models only) |
| `is_embedding` | `true` → launch with `--is-embedding` (encode-only); serves `/v1/embeddings` |
| `attention_backend` | optional `--attention-backend` (embedding models) |
| `disable_radix_cache` | `true` → launch embedding models with `--disable-radix-cache` |
| `mtp` | `true` → enable Multi-Token Prediction speculative decoding for models with native MTP layers (Qwen3.6 MoE, DeepSeek V3); uses `--speculative-algorithm NEXTN` (chat models only) |
| `speculative_algorithm` | optional override for `--speculative-algorithm` (default `NEXTN`; one of `EAGLE`/`EAGLE3`/`NEXTN`/`STANDALONE`/`NGRAM`) |
| `speculative_num_steps` | optional `--speculative-num-steps` (default `1`) |
| `speculative_eagle_topk` | optional `--speculative-eagle-topk` (default `1`) |
| `speculative_num_draft_tokens` | optional `--speculative-num-draft-tokens` (default `2`) |
| `mamba` | `true` → hybrid Mamba/linear-attention model (Qwen3.5/3.6 MoE); with `mtp` adds `--mamba-scheduler-strategy extra_buffer` and exports `SGLANG_ENABLE_SPEC_V2=1` so spec decoding works with radix cache |
| `mamba_scheduler_strategy` | optional override for `--mamba-scheduler-strategy` (default `extra_buffer`) |

**vLLM-only fields** (`engine: vllm`). `served_name`, `model_dir`, `max_model_len`, and `mem_fraction` map to the vLLM equivalents (`--served-model-name`, `--model`, `--max-model-len`, `--gpu-memory-utilization`); the sglang-only knobs above (`mtp`, `mamba`, `attention_backend`, …) are ignored.

| Field | Description |
|---|---|
| `kv_cache_dtype` | `--kv-cache-dtype` (default `fp8`, matching FP8 weights) |
| `vllm_tool_call_parser` | `--tool-call-parser` override when vLLM's parser name differs from sglang's; falls back to `tool_call_parser`. vLLM also gets `--enable-auto-tool-choice` automatically |

To add a new model, append an entry to `models.json` and restart the proxy.

## Configuration

| Variable | Default | Description |
|---|---|---|
| `LISTEN_PORT` | `8001` | Port the proxy binds to |
| `IDLE_TIMEOUT` | `1440` | Seconds of inactivity before stopping a container (24 min) |
| `HEALTH_TIMEOUT` | `600` | Max seconds to wait for a container to become healthy |
| `HEALTH_INTERVAL` | `10` | Seconds between health-check polls |
| `MODELS_CONFIG` | auto-detected | Path to the models config JSON. When unset, selected by GPU hardware (see [Hardware profiles](#hardware-profiles)); set explicitly to override |
| `LOCAL_API_KEY` | (none) | API key for request auth; accept `Authorization: Bearer` or `X-API-Key` header |

## GPU auto-selection

When `gpu_index` is not set for a model, the proxy queries `nvidia-smi` at container start time and picks the GPU with the lowest memory utilization. It also excludes the GPU that each other starting/running backend actually resolved to (tracked at runtime, since auto-selected models have no `gpu_index` in config), so concurrent backends do not collide on the same device. Set `gpu_index` explicitly to pin a model to a specific device.

To intentionally **colocate** models on one GPU, give them a shared `colocate_group`. The first member to start auto-picks a free GPU; every other member of the group then follows it onto that same device instead of being excluded from it. Keep the group's combined `mem_fraction` at ~0.9 or below.

For **tensor-parallel** backends (`tensor_parallel_size` > 1), either pin the devices with a comma-list `gpu_index` (e.g. `"0,1,2,3"`) or omit `gpu_index` to auto-pick the N least-used GPUs.

## Hardware profiles

The same proxy runs on machines with different GPUs and serves the model set that fits the hardware. When `MODELS_CONFIG` is **unset**, the proxy inspects `nvidia-smi` once at startup and selects a profile JSON next to the script:

| Detected hardware | Profile | Serves |
|---|---|---|
| 4+ × H200 | `models.h200.json` | `deepseek-v4-flash` — sglang, `tensor_parallel_size: 4` across all 4 GPUs |
| RTX (PRO) 6000 | `models.rtx6000.json` | `Qwen/Qwen3.6-35B-A3B-FP8` + `BAAI/bge-m3` (single GPU) |
| anything else / no `nvidia-smi` | `models.json` | default fallback |

A matched-but-missing profile falls back to `models.json`; setting `MODELS_CONFIG` explicitly bypasses detection entirely.

The H200 profile serves `sgl-project/DeepSeek-V4-Flash-FP8` (294 GB FP8, won't fit at TP<4 on 143 GB H200s), auto-downloaded via `hf_repo` on first request. Its `tool_call_parser` / `reasoning_parser` default to the DeepSeek-V3 values (`deepseekv3` / `deepseek-r1`) as the closest registered sglang parsers — adjust if your sglang build ships V4-specific names.

## On-demand Hugging Face download

Models with an `hf_repo` are downloaded into `model_dir` on first use. A `.download_complete` sentinel file is written once the download finishes; if `config.json` is present without the sentinel (e.g. a download interrupted by OOM or a crash), the proxy re-runs `snapshot_download`, which only fetches missing or changed files. Manually installed models (no `hf_repo`) are used as-is.

## Requirements

- Python 3.10+
- Docker with NVIDIA Container Toolkit
- `huggingface_hub` when any model uses `hf_repo`
- `lmsysorg/sglang:latest` Docker image
- Model weights at the paths in `models.json`
- `nvidia-smi` (for GPU auto-selection; falls back to GPU 0)

## Prefill throughput

Measured on a single GPU (97 GB H100), `max_tokens=1`, 3 trials, sglang backend. Both models loaded concurrently on separate GPUs.

| Prompt length | Qwen3.6-35B-A3B (FP8) | GLM-4.7-Flash (BF16) |
|---|---|---|
| ~16 tokens | 279 tok/s | 512 tok/s |
| ~1,000 tokens | 18,405 tok/s | 26,856 tok/s |
| ~5,000 tokens | 29,946 tok/s | 63,602 tok/s |

GLM-4.7-Flash has ~2x higher prefill throughput than Qwen3.6-35B across all prompt lengths.

## Tests

```bash
python -m pytest tests/test_local_deployment_proxy.py -v
```

Tests use mock HTTP backends and mock Docker commands — no GPU required.
