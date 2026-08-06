# spark-idle-proxy

A lightweight reverse proxy that lazily starts and stops vLLM Docker containers on
**DGX Spark**. The proxy port stays open permanently; GPU-heavy containers are only
running when there is active traffic.

Serves **nvidia/diffusiongemma-26B-A4B-it-NVFP4** with NVFP4 kernels on Blackwell
(`sm_121`) out of the box. Add more models by editing `models.json`.

## How it works

```
gateway container → host.docker.internal:8002        (SPARK_DEPLOYMENT_URL)
   └─ gateway host :8002 ──reverse SSH tunnel──→ DGX Spark :8002 (proxy)
        opened *from* the Spark by                     └─ model=… → :18004 (vLLM)
        spark_idle_tunnel@<gateway>.service
```

The tunnel is not a convenience around the route, it **is** the route: the gateway
runs in a container on another host and can only find this proxy at
`host.docker.internal:8002`, so something has to bind 8002 over there. Note the
direction — the tunnel is opened *from* the Spark, so it is installed and
supervised here, not on the gateway.

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

Neither half started by hand comes back on its own — and the tunnel is the half
that has no health check pointing at it. Install both units:

```bash
sudo TUNNEL_USER=juncheng LOCAL_API_KEY='your-secret-key' ./ops/spark_idle_proxy/install_service.sh

# tunnels only — installs and bounces spark_idle_tunnel@*, leaves the proxy running
sudo TUNNEL_USER=juncheng ./ops/spark_idle_proxy/install_service.sh --tunnels-only

# remove (this also deletes the key drop-in and every tunnel instance):
sudo ./ops/spark_idle_proxy/install_service.sh --uninstall
```

Prefer `--tunnels-only` on a live box. The proxy stops its vLLM containers when it
receives SIGTERM, so an ordinary re-run's `systemctl restart` makes the next request
pay a cold start of up to `HEALTH_TIMEOUT` (900s). Adding a gateway host or moving a
port does not need that.

`TUNNEL_USER=juncheng` is not optional on the Spark: the unit defaults to `root`,
and root here has no SSH key for the gateway host — `sudo ssh` there fails host key
verification outright, so the tunnel would restart forever. The installer writes it
to a `User=` drop-in and a later run that omits it **keeps** the installed value,
the same contract `LOCAL_API_KEY` gets and for the same reason: silently reverting
it is an outage, not a cosmetic regression. To hand the tunnels back to root, ask
for it explicitly with `TUNNEL_USER=`.

The rest of the drop-in — the ports and the bind address — is rewritten on every
run, defaults included, so restoring a default actually restores it. A re-run also
retires any `spark_idle_tunnel@<host>` instance that `SSH_HOST` no longer names:
otherwise it keeps running under `Restart=always` with its boot symlink intact, and
a gateway removed from the list goes on being advertised this box indefinitely.

### Why the tunnel is a unit and not an `ssh -N -R`

`spark_idle_service.sh` can open the tunnel itself (`SSH_HOST=…
REMOTE_PORT=8002`), and until 2026-08-06 that is how production ran: a bare
`ssh -N -R` with no supervisor. It exited, nothing brought it back, and the failure
was invisible from this box — the proxy went on answering `/v1/models` with 200 and
its container stayed warm while every gateway request failed to connect and the
breaker opened on `diffusiongemma:local-8002`. It reads as a dead model server and
is not one; when this route is down, check for a listener on the **gateway host's**
8002 before looking at anything here.

`spark_idle_tunnel@.service` closes that with autossh plus `Restart=always` for a
dropped link, `WantedBy=multi-user.target` for a reboot, and
`BindsTo=`/`PartOf=spark_idle_proxy.service` so the tunnel neither outlives the
proxy nor stays down after it restarts. The by-hand path in
`spark_idle_service.sh` remains for one-off testing.

The unit reads the repo's `.env` for `LOCAL_API_KEY`, so on a box that also hosts
the gateway a rotation there reaches both ends at once and no `LOCAL_API_KEY=` is
needed on the command line. The Spark normally carries no `.env` — it is reached
over an SSH tunnel from the gateway host — so pass the key to the installer, which
writes it to a mode-0600 drop-in. A later run that does not pass it **keeps** that
drop-in, so a plain re-install does not drop the Spark back to the hardcoded
default. To remove the key, ask for it explicitly —
`sudo LOCAL_API_KEY= ./ops/spark_idle_proxy/install_service.sh` — or use
`--uninstall`. Either way the installer restarts the unit, so the value takes
effect immediately.

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
| `MAX_START_FAILURES` | `20` | Consecutive failed starts after which the proxy gives up on that model: `ensure_running` fails fast without touching docker, streaming chat gets that error instead of the warmup banner, and the give-up is latched (per model) until the proxy restarts. `0` disables the limit |
| `MODELS_CONFIG` | `models.json` | Path to the models config JSON. **Not settable from the environment under systemd** — see below |
| `LOCAL_API_KEY` | `freeinference_api` | API key for request auth. A blank value falls back to the default rather than disabling auth — there is no way to turn auth off |
| `HF_TOKEN` | (none) | Passed into vLLM containers for gated HF downloads |

### Tunnel variables

Read by `install_service.sh` (which writes any non-default into a drop-in on
`spark_idle_tunnel@.service`) and by `spark_idle_service.sh` for the by-hand path.

| Variable | Default | Description |
|---|---|---|
| `SSH_HOST` | `jason@internal.freeinference.org` | `\|`-separated SSH destinations, one tunnel instance each. Staging needs none — its gateway is on this box and reaches `0.0.0.0:8002` directly |
| `TUNNEL_USER` | (unset → `root`) | User the tunnel unit runs as, i.e. whose SSH key it dials with. **Set `juncheng` on the Spark**; root has no key for the gateway host |
| `REMOTE_PORT` | `8002` | Port bound on the gateway host. Must match its `SPARK_DEPLOYMENT_URL` |
| `REMOTE_BIND` | `0.0.0.0` | Bind address there. `0.0.0.0` is required for the gateway's container to reach it as `host.docker.internal`, and needs `GatewayPorts clientspecified` (or `yes`) in the gateway host's `sshd_config` |

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
