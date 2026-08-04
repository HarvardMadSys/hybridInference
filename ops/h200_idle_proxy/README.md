# h200-idle-proxy

Idle reverse proxy for **DeepSeek-V4-Flash-0731** on the **H200** boxes.
Reuses [`local_deployment_proxy.py`](../local_deployment_proxy/local_deployment_proxy.py)
with a dedicated model profile and port.

Runs on **both h200a and h200b**, distinguished only by port: the same
`models.json` is used on each node.

| Setting | h200a | h200b |
|---|---|---|
| Listen port / remote port | **8003** | **8004** |
| Gateway env var | `H200_DEPLOYMENT_URL` | `H200B_DEPLOYMENT_URL` |

| Setting | Value |
|---|---|
| Model | `deepseek-v4-flash` → `deepseek-ai/DeepSeek-V4-Flash-0731` |
| Engine | sglang, `tensor_parallel_size: 2`, DSpark spec decoding, `marlin` MoE |
| GPUs | **2,3** (GPUs **0,1** left free for other tenants) |
| Max context | **1,048,576** tokens (1M — model's YARN-extended architectural max) |
| Backend port | **18003** (node-local; not tunnelled) |
| Idle stop | 24 min (`IDLE_TIMEOUT=1440`) |

## How it works

```
Client → staging/prod host:800X ──SSH tunnel──→ H200 box :800X (proxy)
                                                  └─ model="deepseek-v4-flash" → :18003 (sglang, TP=2 on GPUs 2,3)
```

1. Proxy listens on **8003** (h200a) / **8004** (h200b).
2. First request for `deepseek-v4-flash` starts the sglang container on GPUs 2 and 3.
3. After 24 minutes with no traffic the container stops; the proxy stays up.

> Cold start is ~11 min (DeepGEMM JIT compile), ~6 min once `cache_dir` is warm.
> `HEALTH_TIMEOUT` is 900 s, so keep the persistent cache mount in place.

## Quick start

### Foreground

```bash
MODELS_CONFIG=ops/h200_idle_proxy/models.json LISTEN_PORT=8003 \
  python ops/local_deployment_proxy/local_deployment_proxy.py
```

### Background daemon + tunnels to staging and prod

On h200a:

```bash
# spark2 = staging gateway; jason@internal.freeinference.org = production
SSH_HOST='spark2|jason@internal.freeinference.org' REMOTE_PORT=8003 \
  ./ops/h200_idle_proxy/h200_idle_service.sh start
```

On h200b — same command with both ports moved to 8004:

```bash
SSH_HOST='spark2|jason@internal.freeinference.org' \
  LISTEN_PORT=8004 REMOTE_PORT=8004 \
  ./ops/h200_idle_proxy/h200_idle_service.sh start
```

```bash
./ops/h200_idle_proxy/h200_idle_service.sh status
./ops/h200_idle_proxy/h200_idle_service.sh stop
```

Logs: `/tmp/h200_idle_proxy_<uid>_<port>/proxy.log` (per-user run dir, mode 700).

### systemd (recommended)

```bash
sudo ./ops/h200_idle_proxy/install.sh                                   # h200a
sudo LISTEN_PORT=8004 REMOTE_PORT=8004 ./ops/h200_idle_proxy/install.sh # h200b
# remove:
sudo ./ops/h200_idle_proxy/uninstall.sh
```

Default tunnels: `spark2` and `jason@internal.freeinference.org`.

## Gateway configuration

On **both** staging and production, set:

```bash
H200_DEPLOYMENT_URL=http://host.docker.internal:8003/v1   # h200a
H200B_DEPLOYMENT_URL=http://host.docker.internal:8004/v1  # h200b
LOCAL_API_KEY=freeinference_api   # must match the proxy
```

`config/models.yaml` registers one optional `kind: sglang` route per node for
`deepseek-v4-flash`; each is skipped when its own env var is blank, so a node that
is down or unconfigured does not take the other's capacity with it. The unsuffixed
`H200_DEPLOYMENT_URL` is h200a — kept unsuffixed so gateways configured for the
earlier single-node deployment keep working without an env change.

## Model config

See [`models.json`](models.json):

| Field | Value |
|---|---|
| `gpu_index` | `"2,3"` — pins the 2 TP ranks; GPUs 0,1 are never claimed |
| `tensor_parallel_size` | `2` |
| `backend_port` | `18003` |
| `model_dir` | `/netscratch/juncheng/models/DeepSeek-V4-Flash-0731` |
| `hf_repo` | `deepseek-ai/DeepSeek-V4-Flash-0731` |
| `max_model_len` | `1048576` (1M — the model's YARN architectural max, not VRAM-bound) |
| `mem_fraction` | `0.90` |
| `moe_runner_backend` | `marlin` — **required** for FP4 experts on H200 (SM90) |
| `mtp` / `speculative_algorithm` | `true` / `DSPARK` |
| `sglang_image` | `lmsysorg/sglang:v0.5.16` — DSpark needs ≥ 0.5.16 |
| `cache_dir` | node-local DeepGEMM/JIT cache (**not** on shared `/netscratch`) |
| `skip_server_warmup` | `true` — the proxy's health check already gates readiness |

> **Why the official checkpoint and not an NVFP4 conversion?** `marlin` dequantizes
> both MXFP4 and NVFP4 to BF16, so there is no throughput gain from converting — and
> the NVFP4 conversion of 0731 *silently disables DSpark*. It converts only the main
> routed experts and lists `mtp.*` in `exclude_modules`, leaving the DSpark experts as
> MXFP4; sglang's `is_layer_skipped()` then builds them as `UnquantizedFusedMoEMethod`,
> which registers no scale parameter, so all 4,608 draft-expert scale tensors are
> dropped at load. Output stays *correct* (speculative decoding is lossless) but
> accept length falls to exactly 1.00 and decode drops ~24% below unspeculated. The
> official checkpoint has no ignore list, so the experts bind to
> `Mxfp4MarlinMoEMethod` and accept length is ~4.3.

> **Why `DSPARK` and not `EAGLE`?** 0731's speculative module is DSpark, not the
> preview checkpoint's MTP: 3 blocks (`dspark_target_layer_ids: [40,41,42]`) with
> `main_proj`/`main_norm` plus `markov_head` and `confidence_head`, versus one block
> with `e_proj`/`h_proj`. sglang < 0.5.16 hardcodes the preview prefixes and asserts a
> single nextn layer, so `EAGLE` loads an unpopulated draft head — the server reaches
> "ready to roll" and then 500s every request. Note `config.json` still reports
> `num_nextn_predict_layers: 1`, which is misleading.

## Benchmarks

Measured on **h200b**, official 0731 at TP=2 on GPUs 2,3, marlin, 1M context, with
`POST /flush_cache` before every point. Prefill (in=4096, out=1) saturates at
~17k input tok/s and DSpark neither helps nor hurts it (c=8: 16.4k → 17.0k;
c=32: 17.0k → 16.9k). Decode (in=128, out=512), output tok/s:

| Concurrency | No spec | DSpark | Speedup | Accept length |
|---:|---:|---:|---:|---:|
| 1 | 122 | **393** | 3.22× | 4.72 |
| 16 | 976 | **1,626** | 1.67× | 4.31 |
| 64 | 2,207 | **2,883** | 1.31× | 4.29 |
| 128 | 3,310 | **4,334** | 1.31× | 4.35 |

Median ITL drops 8.04 → 1.92 ms at c=1. Unlike the preview checkpoint's MTP, which
regressed at saturation, DSpark wins at every concurrency tested. See
[`bench_decode.sh`](bench_decode.sh) to reproduce decode-latency profiles.

> `sglang.bench_serving` reuses the same seeded random prompts across invocations, and
> radix prefix caching then serves them from cache — which inflates "prefill" to ~130k
> tok/s and roughly doubles apparent prefill at c=32. Flush between points.

## Requirements

- 4× NVIDIA H200 (or at least GPUs 2 and 3 free)
- Docker + NVIDIA Container Toolkit
- `lmsysorg/sglang:v0.5.16` or newer (**DSpark is not in ≤ 0.5.15**)
- Weights at `model_dir` (or `hf_repo` download on first request)
- SSH to staging/prod with `GatewayPorts clientspecified` (or `yes`)
- `autossh` for durable tunnels (installed by `install.sh`)
