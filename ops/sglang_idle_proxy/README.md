# sglang-idle-proxy

A lightweight reverse proxy that lazily starts and stops sglang Docker containers for multiple models. The proxy port stays open permanently; GPU-heavy containers are only running when there is active traffic. The least-used GPU is auto-selected.

Supports **Qwen3.6-35B-A3B-FP8** and **GLM-4.7-Flash** out of the box. Add more models by editing `models.json`.

## How it works

```
Client → spark2:8001 ──SSH tunnel──→ GPU box :8001 (proxy)
                                        ├─ model="Qwen/..." → :18001 (sglang on GPU 1)
                                        └─ model="zai-org/GLM-4.7-Flash" → :18002 (sglang on GPU 2)
```

1. The proxy listens on port 8001 and accepts all incoming HTTP requests.
2. Requests are routed to the correct backend based on the `model` field in the request body.
3. On first request for a model, it picks the least-used GPU and launches the sglang container.
4. It waits for the container's `/v1/models` health endpoint, then proxies all traffic.
5. After **20 minutes** with no incoming requests for a model, that container is stopped.
6. The proxy keeps listening — the next request re-starts the container automatically.
7. `GET /v1/models` returns a static list of all configured models (no backend needed).

## Quick start

### Foreground (Ctrl-C to quit)

```bash
python sglang_idle_proxy/sglang_idle_proxy.py
```

### Background daemon

```bash
# Local only
./sglang_idle_proxy/sglang_idle_service.sh start

# With SSH reverse tunnel to a public LLM router
SSH_HOST='spark2' REMOTE_PORT=8001 ./sglang_idle_proxy/sglang_idle_service.sh start

# Check / stop
./sglang_idle_proxy/sglang_idle_service.sh status
./sglang_idle_proxy/sglang_idle_service.sh stop    # stops proxy + tunnel + all containers
```

Logs are written to `/tmp/sglang_idle_proxy_8001.log`.

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

The first request for a model after an idle period will block until the container is ready (~50s for non-streaming, or return a warm-up SSE for streaming). Subsequent requests are proxied immediately.

## Warm-up UX

When a model's backend is not running and a request comes in:

- **Streaming chat** (`"stream": true`) — immediately returns an SSE stream with a "thinking" warm-up message, then starts the backend in the background. The client should retry after ~50s.
- **Non-streaming / other endpoints** — blocks until the backend is ready, then proxies the response.

## Model configuration

Models are defined in `sglang_idle_proxy/models.json`:

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
| `gpu_index` | GPU device index (omit to auto-pick) |
| `backend_port` | Host port mapped to the container |
| `model_dir` | Host path to model weights |
| `served_name` | `--served-model-name` for sglang |
| `max_model_len` | `--context-length` |
| `mem_fraction` | `--mem-fraction-static` |
| `tool_call_parser` | `--tool-call-parser` (omit to disable) |

To add a new model, append an entry to `models.json` and restart the proxy.

## Configuration

| Variable | Default | Description |
|---|---|---|
| `LISTEN_PORT` | `8001` | Port the proxy binds to |
| `IDLE_TIMEOUT` | `1200` | Seconds of inactivity before stopping a container (20 min) |
| `HEALTH_TIMEOUT` | `600` | Max seconds to wait for a container to become healthy |
| `HEALTH_INTERVAL` | `10` | Seconds between health-check polls |
| `MODELS_CONFIG` | `models.json` | Path to the models config JSON |

## GPU auto-selection

When `gpu_index` is not set for a model, the proxy queries `nvidia-smi` at container start time and picks the GPU with the lowest memory utilization. It also excludes GPUs already assigned to running backends. Set `gpu_index` explicitly to pin a model to a specific device.

## Requirements

- Python 3.10+
- Docker with NVIDIA Container Toolkit
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
python -m pytest tests/test_sglang_idle_proxy.py -v
```

Tests use mock HTTP backends and mock Docker commands — no GPU required.
