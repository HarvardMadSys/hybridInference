# h200-idle-proxy

Idle reverse proxy for **DeepSeek-V4-Flash (NVFP4)** on a multi-GPU **H200** box.
Reuses [`local_deployment_proxy.py`](../local_deployment_proxy/local_deployment_proxy.py)
with a dedicated model profile and port.

| Setting | Value |
|---|---|
| Listen port | **8003** (8001 = local RTX Qwen, 8002 = Spark) |
| Model | `deepseek-v4-flash` → `nvidia/DeepSeek-V4-Flash-NVFP4` |
| Engine | sglang, `tensor_parallel_size: 2`, MTP (EAGLE), `marlin` MoE |
| GPUs | **2,3** (GPUs **0,1** left free for other tenants) |
| Max context | **1,048,576** tokens (1M — model's YARN-extended architectural max) |
| Idle stop | 24 min (`IDLE_TIMEOUT=1440`) |

## How it works

```
Client → staging/prod host:8003 ──SSH tunnel──→ H200 box :8003 (proxy)
                                                  └─ model="deepseek-v4-flash" → :18003 (sglang, TP=2 on GPUs 2,3)
```

1. Proxy listens on port **8003**.
2. First request for `deepseek-v4-flash` starts the sglang container on GPUs 2 and 3.
3. After 24 minutes with no traffic the container stops; the proxy stays up.

> First cold start takes several minutes (marlin CUDA-graph capture).

## Quick start

### Foreground

```bash
MODELS_CONFIG=ops/h200_idle_proxy/models.json LISTEN_PORT=8003 \
  python ops/local_deployment_proxy/local_deployment_proxy.py
```

### Background daemon + tunnels to staging and prod

```bash
# spark2 = staging gateway; jason@internal.freeinference.org = production
SSH_HOST='spark2|jason@internal.freeinference.org' REMOTE_PORT=8003 \
  ./ops/h200_idle_proxy/h200_idle_service.sh start

./ops/h200_idle_proxy/h200_idle_service.sh status
./ops/h200_idle_proxy/h200_idle_service.sh stop
```

Logs: `/tmp/h200_idle_proxy_<uid>_8003/proxy.log` (per-user run dir, mode 700).

### systemd (recommended)

```bash
sudo ./ops/h200_idle_proxy/install.sh
# remove:
sudo ./ops/h200_idle_proxy/uninstall.sh
```

Default tunnels: `spark2` and `jason@internal.freeinference.org` on remote port 8003.

## Gateway configuration

On **both** staging and production, set:

```bash
H200_DEPLOYMENT_URL=http://host.docker.internal:8003/v1
LOCAL_API_KEY=freeinference_api   # must match the proxy
```

`config/models.yaml` registers an optional `kind: sglang` route for
`deepseek-v4-flash` on `${H200_DEPLOYMENT_URL}` (skipped when the env var is blank).

## Model config

See [`models.json`](models.json):

| Field | Value |
|---|---|
| `gpu_index` | `"2,3"` — pins the 2 TP ranks; GPUs 0,1 are never claimed |
| `tensor_parallel_size` | `2` |
| `backend_port` | `18003` |
| `model_dir` | `/netscratch/juncheng/models/DeepSeek-V4-Flash` (NVFP4) |
| `max_model_len` | `1048576` (1M — the model's YARN architectural max; not VRAM-bound at NVFP4) |
| `mem_fraction` | `0.90` |
| `moe_runner_backend` | `marlin` — **required** for NVFP4 on H200 (SM90) |
| `mtp` / `speculative_algorithm` | `true` / `EAGLE` — native MTP speculative decoding |

> **Why NVFP4 + TP=2 (not FP8 PP=3)?** The FP8 weights are ~274 GiB, which does
> not fit at TP=2 on two 143 GiB H200s — the earlier profile worked around this
> with PP=3 across 3 GPUs (0,2,3). The **NVFP4** checkpoint (4-bit MoE experts,
> FP8 attention) is only ~149 GiB, i.e. **~91 GiB per rank at TP=2** with ~13 GiB
> free per GPU for KV even at the full 1M context — so it fits on **two** GPUs and
> frees a third. `marlin` is mandatory: on pre-Blackwell (SM90) GPUs the default
> `triton` MoE runner asserts "Hidden size mismatch" on the packed FP4 experts.
> MTP uses `EAGLE` (sglang rejects `NEXTN` for this arch); the checkpoint ships a
> single native MTP layer, and speculative decoding (accept length ~2.0) roughly
> doubles single-stream decode.

## Benchmarks

Serving throughput measured at **TP=2 NVFP4 on 2×H200** with sglang's
`bench_serving` (1024-token input / 512-token output, output length fixed via
`ignore_eos`, saturating load). Output-token throughput (tok/s):

| Concurrency | No MTP | With MTP (EAGLE) |
|---:|---:|---:|
| 1 | 122 | 207 |
| 16 | 935 | 1,260 |
| 64 | 1,979 | 2,456 |
| 256 | 2,297 | 2,181 |

MTP (accept length ~2.0) roughly **doubles single-stream decode** (c=1) and helps
through mid concurrency; at saturation the extra draft/verify work no longer pays
off. Peak aggregate ≈ **2.5k tok/s** with MTP at moderate concurrency. VRAM is not
the limit — the KV pool holds >2M tokens. See
[`bench_decode.sh`](bench_decode.sh) to reproduce decode-latency profiles.

## Requirements

- 4× NVIDIA H200 (or at least GPUs 2 and 3 free)
- Docker + NVIDIA Container Toolkit
- `lmsysorg/sglang:latest`
- Weights at `model_dir` (or `hf_repo` download on first request)
- SSH to staging/prod with `GatewayPorts clientspecified` (or `yes`)
- `autossh` for durable tunnels (installed by `install.sh`)
