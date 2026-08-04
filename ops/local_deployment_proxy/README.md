# local-deployment-proxy

A lightweight reverse proxy that lazily starts and stops sglang Docker containers for multiple models. The proxy port stays open permanently; GPU-heavy containers are only running when there is active traffic. The least-used GPU is auto-selected.

Serves both **chat** and **embedding** models from one proxy/port. Supports
**Qwen3.6-35B-A3B-FP8** and the **bge-m3** embedding model out of the box (both
via the vLLM engine). Add more by editing `models.json` (set
`"is_embedding": true` for embedding models). Every model is loaded on-demand
and stopped when idle.

## How it works

```
Client → spark2:8001 ──SSH tunnel──→ GPU box :8001 (proxy)
                                        ├─ model="Qwen/..."    → :18001 (vLLM, colocated)
                                        └─ model="BAAI/bge-m3" → :18012 (vLLM --is-embedding, colocated)
```

1. The proxy listens on port 8001 and accepts all incoming HTTP requests.
2. Requests are routed to the correct backend based on the `model` field in the request body.
3. If an `hf_repo` model is not installed, the first request downloads it from Hugging Face.
4. It picks the configured/least-used GPU and launches the container (sglang by default, or vLLM when `engine: vllm`).
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

# With SSH reverse tunnel to a public LLM router.
# Uses autossh when installed so the tunnel auto-reconnects after a drop or a
# router reboot; falls back to plain ssh (no auto-recover) with a warning if
# autossh is missing — install it for durable tunnels (`apt-get install autossh`).
SSH_HOST='spark2|internal.freeinference.org' REMOTE_PORT=8001 ./local_deployment_proxy/local_deployment_service.sh start

# With API key auth
LOCAL_API_KEY='your-secret-key' ./local_deployment_proxy/local_deployment_service.sh start

# Check / stop
./local_deployment_proxy/local_deployment_service.sh status
./local_deployment_proxy/local_deployment_service.sh stop    # stops proxy + tunnel + all containers
```

Logs are written to `/tmp/local_deployment_proxy_8001.log`.

### Run as a systemd service (recommended for production)

The background daemon above does not survive a reboot of this GPU box. For a
durable setup, use the two units in [`deploy/systemd/`](../../deploy/systemd/):

- `local_deployment_proxy.service` — the local listener (port 8001).
- `local_deployment_tunnel@.service` — a **templated** reverse tunnel, one
  instance per router host, run with `autossh` (`Restart=always`) so it
  reconnects after a link drop **and** comes back after a reboot.

`install.sh` installs autossh, **renders** both units (substituting the
`__REPO_ROOT__` placeholder with this checkout's path, so the repo can live
anywhere), and enables the proxy plus a tunnel instance per router host:

```bash
# Defaults to SSH_HOST='internal.freeinference.org|spark2', ports 8001.
sudo ./local_deployment_proxy/install.sh

# Override hosts/ports if needed. Each SSH_HOST entry may include a user
# (user@host); replace 'user' with the router account that authorizes this
# box's root SSH key (the tunnel runs as root):
sudo SSH_HOST='user@internal.freeinference.org|user@spark2' REMOTE_PORT=8001 \
     ./local_deployment_proxy/install.sh

# Remove everything (proxy + all tunnel instances + drop-ins, including the key):
sudo ./local_deployment_proxy/uninstall.sh
```

The unit reads the repo's `.env` for `LOCAL_API_KEY` — the key the gateway signs
its requests with — so on a box that also hosts the gateway a rotation there
reaches both ends at once. A box that runs only this proxy and its tunnel has no
`.env`; pass the key to the installer instead and it writes a mode-0600 drop-in:

```bash
sudo LOCAL_API_KEY='…' ./local_deployment_proxy/install.sh
```

A later run that does not pass `LOCAL_API_KEY` **keeps** the drop-in it finds, so
re-installing to add a tunnel host or move a port does not take the key off a box
that has no `.env` to fall back on. To remove the key, ask for it explicitly —
`sudo LOCAL_API_KEY= ./local_deployment_proxy/install.sh` — or run `uninstall.sh`.
Either way the installer restarts the unit, since systemd does not re-read a
drop-in on its own and `enable --now` does nothing to an already-running one.

Equivalent manual steps, if you'd rather not use the script. The proxy unit
carries a `__REPO_ROOT__` placeholder, so render it (the tunnel unit has no
placeholder and is copied as-is):

```bash
REPO_ROOT="$(pwd)"   # run from the repo root
sed "s#__REPO_ROOT__#${REPO_ROOT}#g" deploy/systemd/local_deployment_proxy.service \
  | sudo tee /etc/systemd/system/local_deployment_proxy.service >/dev/null
sudo cp deploy/systemd/local_deployment_tunnel@.service /etc/systemd/system/
sudo apt-get install -y autossh        # required by the tunnel unit
sudo systemctl daemon-reload
sudo systemctl enable --now local_deployment_proxy.service
# One instance per router host. The instance name is the SSH destination and may
# include a user (user@host); the tunnel connects as that user (default root):
sudo systemctl enable --now local_deployment_tunnel@user@internal.freeinference.org
sudo systemctl enable --now local_deployment_tunnel@user@spark2
```

Requirements / knobs:

- The tunnel unit runs as **root** and connects to each router as the user in
  the instance name (the part before `@`; defaults to root) using this box's
  root SSH key in `/root/.ssh`. That key must be in the target account's
  `~/.ssh/authorized_keys` on the router. Override `LISTEN_PORT` / `REMOTE_PORT`
  / `REMOTE_BIND` via a drop-in (`systemctl edit local_deployment_tunnel@…`) if
  the defaults (`8001` / `8001` / `0.0.0.0`) don't apply.
- Binding `REMOTE_BIND=0.0.0.0` on the router requires `GatewayPorts
  clientspecified` (or `yes`) in the router's `sshd_config`, so its Docker
  containers can reach the forwarded port via `host.docker.internal`.
- Logs: `journalctl -u local_deployment_tunnel@internal.freeinference.org -f`.

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
        "container": "qwen36-vllm",
        "engine": "vllm",
        "backend_port": 18001,
        "model_dir": "/scratch/juncheng/models/Qwen3.6-35B-A3B-FP8",
        "served_name": "Qwen/Qwen3.6-35B-A3B-FP8",
        "max_model_len": 135168,
        "mem_fraction": "0.80",
        "tool_call_parser": "qwen3_coder",
        "reasoning_parser": "qwen3",
        "colocate_group": "primary"
    },
    "BAAI/bge-m3": {
        "container": "bge-m3-vllm",
        "engine": "vllm",
        "backend_port": 18012,
        "model_dir": "/scratch/juncheng/models/bge-m3",
        "hf_repo": "BAAI/bge-m3",
        "hf_ignore_patterns": ["onnx/*", "imgs/*", "*.jpg"],
        "served_name": "BAAI/bge-m3",
        "max_model_len": 8192,
        "mem_fraction": "0.10",
        "is_embedding": true,
        "colocate_group": "primary"
    }
}
```

| Field | Description |
|---|---|
| `container` | Docker container name |
| `engine` | serving engine: `sglang` (default) or `vllm` |
| `gpu_index` | GPU device index, or a comma list for tensor parallelism (e.g. `"0,1,2,3"`); omit to auto-pick |
| `tensor_parallel_size` | number of GPUs to shard the model across by tensor parallelism (default `1`); when >1 the launch gets `--tp`/`--tensor-parallel-size N` and Docker `--ipc=host` for NCCL |
| `pipeline_parallel_size` | number of GPUs to shard the model across **by layer** (pipeline parallelism, default `1`); adds `--pp-size` (sglang) / `--pipeline-parallel-size` (vLLM). Unlike TP it has no attention-head divisibility constraint, so it can use GPU counts TP cannot (e.g. 3). The backend claims `tensor_parallel_size × pipeline_parallel_size` GPUs |
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
| `MODELS_CONFIG` | auto-detected | Path to the models config JSON. When unset, selected by GPU hardware (see [Hardware profiles](#hardware-profiles)); set explicitly to override. **Not settable from the environment under systemd** — see below |
| `LOCAL_API_KEY` | `freeinference_api` | API key for request auth; accepts an `Authorization: Bearer` or `X-API-Key` header. A blank value falls back to the default rather than disabling auth — there is no way to turn auth off |
| `PROXY_OWNER` | `port-$LISTEN_PORT` | Identity stamped on the containers this proxy starts, so it never destroys or adopts another proxy's backend of the same name (see [Container ownership](#container-ownership)). The default is unique per host and stable across restarts; override only to give a hand-run proxy an identity of its own |

### `MODELS_CONFIG` under systemd

`deploy/systemd/local_deployment_proxy.service` reads the gateway's `.env` (that is
where `LOCAL_API_KEY` comes from), and `MODELS_CONFIG` is also a *gateway*
variable — a legacy alias of `MODELS_CONFIG_PATH` naming a YAML registry, which
this proxy would `json.load()` and find no backends in. An `EnvironmentFile=`
outranks every `Environment=` line whatever the order, so the unit drops the
variable with `UnsetEnvironment=MODELS_CONFIG`, which systemd applies last of all.
Hardware auto-detection is therefore always in charge under systemd, and no
ordinary route — `.env`, a drop-in `Environment=`, `systemctl set-environment` —
can override it.

To pin a config there, set it in the **child process** with a `/usr/bin/env`
prefix on `ExecStart`, the way `h200_idle_proxy.service` pins
`ops/h200_idle_proxy/models.json`. That is the one place that outranks
`EnvironmentFile=` and `UnsetEnvironment=` both, because it runs after systemd has
finished compiling the environment. The bare `ExecStart=` is what lets a drop-in
replace the command instead of appending a second one; take the rest of the line
from `systemctl cat local_deployment_proxy.service`:

```ini
# /etc/systemd/system/local_deployment_proxy.service.d/models-config.conf
[Service]
ExecStart=
ExecStart=/usr/bin/env MODELS_CONFIG=/path/to/models.json /srv/hybridInference/.venv/bin/python3 /srv/hybridInference/ops/local_deployment_proxy/local_deployment_proxy.py
```

```bash
sudo systemctl daemon-reload && sudo systemctl restart local_deployment_proxy
```

Resetting the unset list and setting the variable with an `Environment=` line
instead does **not** work, and fails precisely when it is needed:
`UnsetEnvironment=` only stops the final deletion, so a `.env` that does define
`MODELS_CONFIG` goes back to outranking that `Environment=` line and the proxy
loads the gateway's YAML after all.

Keep this in its own `.conf`, separate from the `local-api-key.conf` the installer
writes into the same `.d` directory, and re-check it if the unit's own `ExecStart`
ever changes, since the drop-in restates it. Running the proxy by hand is
unaffected: `MODELS_CONFIG` works normally there.

## GPU auto-selection

When `gpu_index` is not set for a model, the proxy queries `nvidia-smi` at container start time and picks the GPU with the lowest memory utilization. It also excludes the GPU that each other starting/running backend actually resolved to (tracked at runtime, since auto-selected models have no `gpu_index` in config), so concurrent backends do not collide on the same device. Set `gpu_index` explicitly to pin a model to a specific device.

To intentionally **colocate** models on one GPU, give them a shared `colocate_group`. The first member to start auto-picks a free GPU; every other member of the group then follows it onto that same device instead of being excluded from it. Keep the group's combined `mem_fraction` at ~0.9 or below.

For **tensor-parallel** or **pipeline-parallel** backends (`tensor_parallel_size` > 1 and/or `pipeline_parallel_size` > 1), either pin the devices with a comma-list `gpu_index` (e.g. `"0,2,3"`) or omit `gpu_index` to auto-pick the `tensor_parallel_size × pipeline_parallel_size` least-used GPUs. A multi-GPU `gpu_index` is passed to Docker as a quoted `--gpus '"device=0,2,3"'` so the daemon does not split the comma list into separate GPU requests.

## Hardware profiles

The same proxy runs on machines with different GPUs and serves the model set that fits the hardware. When `MODELS_CONFIG` is **unset**, the proxy inspects `nvidia-smi` once at startup and selects a profile JSON:

| Detected hardware | Profile | Serves |
|---|---|---|
| 4+ × H200 | [`../h200_idle_proxy/models.json`](../h200_idle_proxy/models.json) | `deepseek-v4-flash` — sglang, `tensor_parallel_size: 2` on GPUs **2,3** (GPUs 0,1 free) |
| RTX (PRO) 6000 | `models.rtx6000.json` | `Qwen/Qwen3.6-35B-A3B-FP8` + `BAAI/bge-m3` (single GPU) |
| anything else / no `nvidia-smi` | `models.json` | default fallback |

A matched-but-missing profile falls back to `models.json`; setting `MODELS_CONFIG` explicitly bypasses detection entirely.

The H200 row points **out of this directory on purpose.** A 4×H200 box has one DeepSeek deployment, not two, and the file the dedicated `h200_idle_proxy` unit pins on its `ExecStart` line *is* that deployment's definition — so the auto-detected profile is that same file rather than a copy of it. There used to be a local `models.h200.json` mirror: it drifted (FP8 at PP=3 on GPUs 0,2,3 on one side, NVFP4 at TP=2 on the other) until #1185 reconverged the two, and only a partial five-key test assertion held them in step afterwards — #1187's `mem_fraction` change still had to be applied twice by hand. Two entry points that resolve the same `container` and `backend_port` must resolve the same config, or they fight over it; see [Container ownership](#container-ownership).

The H200 profile serves `deepseek-ai/DeepSeek-V4-Flash-0731` on GPUs 2,3 (TP=2; GPUs 0,1 left free for other workloads), with DSpark speculative decoding and the `marlin` MoE runner that FP4 experts require on SM90. It previously needed PP=3 across GPUs 0,2,3, because ~274 GiB of FP8 weights do not fit at TP=2 on two H200s and TP=3 is illegal (64 attention heads are not divisible by 3); the 0731 release ships FP4 experts at ~156 GiB, so it fits on two GPUs and frees a third. For the dedicated service with reverse tunnels to staging and production (port 8003 on h200a, 8004 on h200b), prefer [`ops/h200_idle_proxy`](../h200_idle_proxy/README.md). Its `tool_call_parser` / `reasoning_parser` use the DeepSeek-V4 pairing (`deepseekv4` / `deepseek-v4`) that the [sglang DeepSeek-V4 cookbook](https://lmsysorg.mintlify.app/cookbook/autoregressive/DeepSeek/DeepSeek-V4) prescribes. Do **not** substitute the V3-era parsers: `deepseek-r1` treats the whole generation as reasoning until a `</think>` close tag — requests that do not enable thinking never produce that tag, so every reply comes back with empty `content` and the answer buried in `reasoning_content` — and `deepseekv3` does not recognize V4's DSML tool-call markup, so `tool_calls` stays null and agentic clients cannot run tools.

## Container ownership

Every lifecycle operation here addresses its backend by container *name*: a `docker rm -f <name>` before each start, another when the idle timer expires, and an adoption check that asks only "is something running under this name and answering on `backend_port`?". A name is not proof of ownership, so each container is stamped with two labels at `docker run` and they are consulted before anything is destroyed or adopted:

| Label | Value | Meaning |
|---|---|---|
| `com.freeinference.proxy.owner` | `port-<LISTEN_PORT>`, or `$PROXY_OWNER` when set | Which proxy runs it. Only one process can hold a listen port on a host, and the value is stable across restarts of the same unit — so restart-and-adopt keeps working. |
| `com.freeinference.proxy.profile` | first 16 hex of `sha256` over the **launch-affecting** config keys | Which config it was launched from. |

The rules:

- **Owner matches, or no owner label** → this proxy's container. Adopt it when healthy, replace it when not, stop it when idle. An *absent* label means "started before labelling existed, therefore mine" — that keeps the change a no-op for containers already running at upgrade time (see [Upgrading](#upgrading-from-an-unlabelled-deployment)).
- **Owner matches, profile differs** → the config on disk changed since launch (a `mem_fraction` bump, a new `sglang_image`). Replace the container; do not adopt a backend running the previous config. The replacement lands on the **same GPUs the old container held**, unless the config pins `gpu_index`, a `colocate_group` partner has already resolved a device, or the requested device count changed (a `tensor_parallel_size` / `pipeline_parallel_size` edit). Auto-selection cannot be used here: the container being replaced is still running when the choice is made, so `nvidia-smi` reports its device as busy and the backend would migrate — away from its colocation partner, or onto a device an idle-stopped model is pinned to, which then OOMs when that model wakes.
- **Owner differs, and that container is running** → hands off. The proxy neither adopts nor destroys it, and the request fails 502 with both owners named — for streaming chat requests too, which is the shape that matters: they are answered before the backend is up, so the diagnosis has to be produced before the `200` and the "starting up" banner are committed, or the gateway keeps seeing success and never fails over. Taking a live foreign container over would be worse either way: `docker rm -f` kills a backend another process may be eleven minutes into loading or actively streaming from, and adopting it puts one container under two idle watchers that cannot see each other's activity, so whichever fires first stops it out from under the other's traffic.
- **Owner differs, and that container is exited** → reclaimed: `docker rm -f` then a fresh launch. An exited container holds no GPU and serves no traffic, and *nothing* in this proxy removes a foreign container (the idle path declines one too), so refusing it would wedge the name permanently and 502 the model on that node until an operator removed the corpse by hand.

Which config keys force a replacement: all of them except `startup_estimate_seconds`, `hf_repo`, `hf_revision` and `hf_ignore_patterns`, which never reach a `docker run` command line — the first only words the warmup banner, the rest only steer the Hugging Face download. Re-tuning `startup_estimate_seconds` against a measured cold start is free, as it should be; editing anything else costs a reload on the next start.

This only ever triggers when two processes resolve the *same* container name. Running this proxy pinned to `models.json` (Qwen + bge-m3 on GPUs 0,1) alongside `h200_idle_proxy` (DeepSeek on GPUs 2,3) on one 4-GPU box shares no container name or port and is unaffected.

To protect a hand-started container from every proxy on the box — benchmarking with [`bench_decode.sh`](../h200_idle_proxy/bench_decode.sh), say — give it an owner of its own:

```bash
sudo docker run -d --name deepseek-v4-flash-sglang \
  --label com.freeinference.proxy.owner=manual  …
```

The protection lasts only as long as the container runs: `docker stop` hands the name back, and the next proxy start reclaims it. When the benchmark is done, `sudo docker rm -f deepseek-v4-flash-sglang` and let the proxy launch its own.

### Upgrading from an unlabelled deployment

Nothing to do, and nothing to restart. Containers running now carry no labels, are therefore read as this proxy's own, and keep being adopted and idle-stopped exactly as before. Each gets labelled at its next cold start, and is protected from then on. There is no window in which a running backend is orphaned or unreclaimable.

### Changing `LISTEN_PORT` on a box with a labelled backend running

The owner identity is derived from the listen port, so moving a node's port orphans the container it started: while that container keeps running the proxy reads it as another owner's and refuses to adopt or remove it. Either remove it once by hand (`sudo docker rm -f <container>`), or keep the old identity across the move by pinning `PROXY_OWNER` to what it used to be. Nothing is silently wrong in the meantime — requests fail 502 naming both owners, streaming ones included, and the 502 text carries both remediations.

## On-demand Hugging Face download

Models with an `hf_repo` are downloaded into `model_dir` on first use. A `.download_complete` sentinel file is written once the download finishes; if `config.json` is present without the sentinel (e.g. a download interrupted by OOM or a crash), the proxy re-runs `snapshot_download`, which only fetches missing or changed files. Manually installed models (no `hf_repo`) are used as-is.

## Requirements

- Python 3.10+
- Docker with NVIDIA Container Toolkit
- `huggingface_hub` when any model uses `hf_repo`
- `lmsysorg/sglang:latest` Docker image (sglang models), or a vLLM image (`engine: vllm` models)
- Model weights at the paths in `models.json`
- `nvidia-smi` (for GPU auto-selection; falls back to GPU 0)

## Tests

```bash
python -m pytest tests/test_local_deployment_proxy.py -v
```

Tests use mock HTTP backends and mock Docker commands — no GPU required.
