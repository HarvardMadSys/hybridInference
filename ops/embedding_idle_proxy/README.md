# embedding-idle-proxy

A lightweight reverse proxy that lazily starts and stops sglang **embedding**
containers on the local GPU. Same lifecycle as
[`sglang_idle_proxy`](../sglang_idle_proxy/README.md): the proxy port stays open
permanently; GPU-heavy containers only run when there is active traffic, and are
stopped after an idle period. It reuses the same proxy server
(`ops/sglang_idle_proxy/sglang_idle_proxy.py`) with `--is-embedding` enabled per
model, and runs on its own port (default **8002**) so it can coexist with the
chat proxy.

Serves **BAAI/bge-m3** out of the box. Add more by editing `models.json`.

## How it works

```text
prod gateway (Docker) → host.docker.internal:8002 ──SSH reverse tunnel──→ GPU box :8002 (proxy)
                                                       └─ model="BAAI/bge-m3" → :18012 (sglang --is-embedding)
```

1. The proxy listens on `LISTEN_PORT` (8002) and accepts all incoming HTTP requests.
2. Requests are routed to the correct backend by the `model` field in the body.
3. On the first request for a model it launches the sglang container with
   `--is-embedding`, waits for `/v1/models` health, then proxies traffic.
4. `POST /v1/embeddings` is non-streaming — the first request after an idle
   period blocks until the backend is ready (~60–120s), then returns embeddings.
5. After `IDLE_TIMEOUT` (20 min) with no traffic the container is stopped; the
   next request re-starts it.

## Quick start

```bash
# Local only
./ops/embedding_idle_proxy/embedding_idle_service.sh start

# With SSH reverse tunnel to production / the public router
SSH_HOST='internal.freeinference.org' REMOTE_PORT=8002 \
    ./ops/embedding_idle_proxy/embedding_idle_service.sh start

# Check / stop
./ops/embedding_idle_proxy/embedding_idle_service.sh status
./ops/embedding_idle_proxy/embedding_idle_service.sh stop
```

Logs are written to `/tmp/embedding_idle_proxy_8002.log`.

## Usage

```python
from openai import OpenAI

client = OpenAI(base_url="http://GPU_BOX:8002/v1", api_key="unused")
resp = client.embeddings.create(
    model="BAAI/bge-m3",
    input=["hello world", "another sentence"],
)
print(len(resp.data[0].embedding))
```

## Model configuration

Models are defined in `models.json`. Fields match `sglang_idle_proxy` plus:

| Field | Description |
|---|---|
| `is_embedding` | `true` → launch sglang with `--is-embedding` (encode-only) |
| `attention_backend` | optional `--attention-backend` override |

```json
{
    "BAAI/bge-m3": {
        "container": "bge-m3-sglang",
        "gpu_index": "3",
        "backend_port": 18012,
        "model_dir": "/scratch/juncheng/models/bge-m3",
        "served_name": "BAAI/bge-m3",
        "max_model_len": 8192,
        "mem_fraction": "0.45",
        "is_embedding": true
    }
}
```

Update `model_dir` and `served_name` for your host before starting. `bge-m3` is
pinned to `gpu_index: "1"` — the same GPU that runs `Qwen/Qwen3.6-35B-A3B-FP8` in
`ops/sglang_idle_proxy`. To make room, the chat proxy's qwen3.6 `mem_fraction`
was lowered to `0.80`; bge-m3 takes `0.12`, so both fit on one device (~0.92
total). Omit `gpu_index` to auto-pick an empty GPU (memory utilization < 20%)
instead.

## Install as a systemd service

```bash
sudo ./ops/embedding_idle_proxy/install_service.sh
```

Installs `deploy/systemd/embedding_idle_proxy.service`. The systemd unit runs
only the proxy; open the SSH tunnel separately via the service script (or a
dedicated autossh unit), exactly as with `sglang_idle_proxy`.

## Register in the gateway

Add the models to `config/models.yaml` with `type: embedding` and a
`kind: sglang` route pointing at the tunneled port — see the
`bge-m3` / `Qwen3-Embedding-8B` entries there. Production reaches the reverse
tunnel via `http://host.docker.internal:8002`.

## Requirements

Same as `sglang_idle_proxy`: Python 3.10+, Docker with NVIDIA Container Toolkit,
`lmsysorg/sglang:latest`, model weights at the configured paths, and
`nvidia-smi`.
