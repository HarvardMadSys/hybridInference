# spark-idle-proxy

A lightweight reverse proxy that lazily starts and stops vLLM Docker containers on
**DGX Spark**. The proxy port stays open permanently; GPU-heavy containers are only
running when there is active traffic.

Serves **nvidia/diffusiongemma-26B-A4B-it-NVFP4** with NVFP4 kernels on Blackwell
(`sm_121`) out of the box. Add more models by editing `models.json`.

## How it works

```
Client → spark2:8002 ──SSH tunnel──→ DGX Spark :8002 (proxy)
                                        └─ model="nvidia/diffusiongemma-26B-A4B-it-NVFP4" → :18004 (vLLM)
```

1. The proxy listens on port **8002** and accepts all incoming HTTP requests.
2. Requests are routed to the correct backend based on the `model` field in the request body.
3. If weights are missing, the first request downloads them from Hugging Face.
4. It launches a vLLM container (`vllm/vllm-openai:cu130-nightly` by default).
5. It waits for the container's `/v1/models` health endpoint, then proxies all traffic.
6. After **24 minutes** with no incoming requests for a model, that container is stopped.
7. `GET /v1/models` returns a static list of all configured models (no backend needed).

## Quick start

```bash
# Foreground
python spark_idle_proxy/spark_idle_proxy.py

# Background daemon
./spark_idle_proxy/spark_idle_service.sh start

# With SSH reverse tunnel to a public LLM router
SSH_HOST='spark2|internal.freeinference.org' REMOTE_PORT=8002 ./spark_idle_proxy/spark_idle_service.sh start

# With API key auth
LOCAL_API_KEY='your-secret-key' ./spark_idle_proxy/spark_idle_service.sh start

./spark_idle_proxy/spark_idle_service.sh status
./spark_idle_proxy/spark_idle_service.sh stop
```

Logs are written to `/tmp/spark_idle_proxy_8002.log`.

## As a systemd service

A daemon started by hand does not come back after a reboot. Install the unit
instead:

```bash
sudo LOCAL_API_KEY='your-secret-key' ./ops/spark_idle_proxy/install_service.sh
# remove (this also deletes the key drop-in):
sudo ./ops/spark_idle_proxy/install_service.sh --uninstall
```

The unit reads the repo's `.env` for `LOCAL_API_KEY`, so on a box that also hosts
the gateway a rotation there reaches both ends at once and no `LOCAL_API_KEY=` is
needed on the command line. The Spark normally carries no `.env` — it is reached
over an SSH tunnel from the gateway host — so pass the key to the installer, which
writes it to a mode-0600 drop-in. Omitting it on a later run **removes** that
drop-in, which drops the proxy back to the hardcoded default; pass the key on every
run you want it kept. Either way the installer restarts the unit, so the value
takes effect immediately.

## Usage with OpenAI-compatible clients

```python
from openai import OpenAI

client = OpenAI(base_url="http://spark2:8002/v1", api_key="unused")
resp = client.chat.completions.create(
    model="nvidia/diffusiongemma-26B-A4B-it-NVFP4",
    messages=[{"role": "user", "content": "Hello!"}],
)
print(resp.choices[0].message.content)
```

The first request after an idle period may block for several minutes while vLLM loads
weights and warms up kernels on Spark. Streaming requests return a warm-up SSE message
immediately; retry after the backend is ready.

## Model configuration

Models are defined in `spark_idle_proxy/models.json`:

```json
{
    "nvidia/diffusiongemma-26B-A4B-it-NVFP4": {
        "container": "diffusiongemma-vllm",
        "gpu_index": "0",
        "backend_port": 18004,
        "hf_repo": "nvidia/diffusiongemma-26B-A4B-it-NVFP4",
        "hf_cache_dir": "/home/juncheng/.cache/huggingface",
        "serve_hf_repo": true,
        "served_name": "nvidia/diffusiongemma-26B-A4B-it-NVFP4",
        "docker_image": "vllm/vllm-openai:gemma",
        "max_model_len": 262144,
        "gpu_memory_utilization": 0.6,
        "max_num_seqs": 2,
        "trust_remote_code": true,
        "enable_auto_tool_choice": true,
        "tool_call_parser": "gemma4",
        "reasoning_parser": "gemma4",
        "docker_env": {"VLLM_USE_V2_MODEL_RUNNER": "1"},
        "vllm_extra_args": [
            "--attention-backend", "TRITON_ATTN",
            "--override-generation-config", "{\"max_new_tokens\": null}",
            "--default-chat-template-kwargs", "{\"enable_thinking\": true}"
        ]
    }
}
```

| Field | Description |
|---|---|
| `container` | Docker container name |
| `gpu_index` | GPU device index (omit to auto-pick) |
| `backend_port` | Host port mapped to the container |
| `model_dir` | Host path to model weights (for local `/model` mount) |
| `hf_repo` | Hugging Face repo id; also used for on-demand download into `model_dir` |
| `hf_cache_dir` | Host Hugging Face cache mounted at `/root/.cache/huggingface` |
| `serve_hf_repo` | `true` → serve `hf_repo` from cache (required for HF hub snapshots with blob symlinks) |
| `docker_image` | vLLM OpenAI server image (default: `vllm/vllm-openai:cu130-nightly`; diffusiongemma uses `vllm/vllm-openai:gemma`) |
| `served_name` | `--served-model-name` for vLLM |
| `max_model_len` | `--max-model-len` |
| `gpu_memory_utilization` | `--gpu-memory-utilization` |
| `max_num_seqs` | `--max-num-seqs` |
| `trust_remote_code` | Adds `--trust-remote-code` when true |
| `enable_auto_tool_choice` | Adds `--enable-auto-tool-choice` when true |
| `tool_call_parser` | `--tool-call-parser` |
| `reasoning_parser` | `--reasoning-parser` |
| `vllm_extra_args` | Extra CLI args appended to `vllm serve` |
| `docker_env` | Extra `-e KEY=val` flags for `docker run` |

## Configuration

| Variable | Default | Description |
|---|---|---|
| `LISTEN_PORT` | `8002` | Port the proxy binds to |
| `IDLE_TIMEOUT` | `1440` | Seconds of inactivity before stopping a container (24 min) |
| `HEALTH_TIMEOUT` | `900` | Max seconds to wait for a container to become healthy |
| `HEALTH_INTERVAL` | `10` | Seconds between health-check polls |
| `MODELS_CONFIG` | `models.json` | Path to the models config JSON. **Not settable from the environment under systemd** — see below |
| `LOCAL_API_KEY` | `freeinference_api` | API key for request auth. A blank value falls back to the default rather than disabling auth — there is no way to turn auth off |
| `HF_TOKEN` | (none) | Passed into vLLM containers for gated HF downloads |

### `MODELS_CONFIG` under systemd

`deploy/systemd/spark_idle_proxy.service` reads the gateway's `.env` (that is where
`LOCAL_API_KEY` comes from), and `MODELS_CONFIG` is also a *gateway* variable — a
legacy alias of `MODELS_CONFIG_PATH` naming a YAML registry, which this proxy would
`json.load()` and find no backends in. An `EnvironmentFile=` outranks every
`Environment=` line whatever the order, so the unit drops the variable with
`UnsetEnvironment=MODELS_CONFIG`, which systemd applies last of all. Under systemd
the proxy therefore always uses `models.json` next to the script, and no ordinary
route — `.env`, a drop-in `Environment=`, `systemctl set-environment` — can
override it.

To serve a different config there, set it in the **child process** with a
`/usr/bin/env` prefix on `ExecStart`, the way `h200_idle_proxy.service` pins
`ops/h200_idle_proxy/models.json`. That is the one place that outranks
`EnvironmentFile=` and `UnsetEnvironment=` both, because it runs after systemd has
finished compiling the environment. The bare `ExecStart=` is what lets a drop-in
replace the command instead of appending a second one; take the rest of the line
from `systemctl cat spark_idle_proxy.service`:

```ini
# /etc/systemd/system/spark_idle_proxy.service.d/models-config.conf
[Service]
ExecStart=
ExecStart=/usr/bin/env MODELS_CONFIG=/path/to/models.json /srv/hybridInference/.venv/bin/python3 /srv/hybridInference/ops/spark_idle_proxy/spark_idle_proxy.py
```

```bash
sudo systemctl daemon-reload && sudo systemctl restart spark_idle_proxy
```

Resetting the unset list and setting the variable with an `Environment=` line
instead does **not** work, and fails precisely when it is needed:
`UnsetEnvironment=` only stops the final deletion, so a `.env` that does define
`MODELS_CONFIG` goes back to outranking that `Environment=` line and the proxy
loads the gateway's YAML after all.

Keep this in its own `.conf`, separate from the `local-api-key.conf` the installer
writes, and re-check it if the unit's own `ExecStart` ever changes, since the
drop-in restates it. Running the proxy by hand or via `spark_idle_service.sh` is
unaffected: `MODELS_CONFIG` works normally there.

## Requirements

- Python 3.10+
- Docker with NVIDIA Container Toolkit on DGX Spark
- `huggingface_hub` when any model uses `hf_repo`
- `vllm/vllm-openai:cu130-nightly` (or another Spark-validated image)
- `nvidia-smi` (for GPU auto-selection; falls back to GPU 0)

## Tests

```bash
python -m pytest tests/test_spark_idle_proxy.py -v
```
