# h200-idle-proxy

Idle reverse proxy for **DeepSeek-V4-Flash-0731** on the **H200** boxes.
Reuses [`local_deployment_proxy.py`](../local_deployment_proxy/local_deployment_proxy.py)
with a dedicated model profile and port.

Runs on **both h200a and h200b**. h200a runs **two TP=2 replicas** (all four
GPUs); h200b runs one. Each replica is a separate proxy instance on its own port
and needs its own gateway route — one route only ever reaches one replica.

This package's [`models.json`](models.json) is the single definition of that
deployment. `local_deployment_proxy.py`'s hardware detection resolves its
`4+ × H200` branch to *this* file, so a bare hand-run on an H200 box and the
`h200_idle_proxy` unit agree on one config instead of two that can drift apart.
Both still name one container on one `backend_port`, so a second proxy on the
same host needs a config that collides on neither — ownership labels turn a
collision into a diagnosable 502 rather than the two processes destroying each
other's container. See
[Container ownership](../local_deployment_proxy/README.md#container-ownership).

| Setting | h200a replica A | h200a replica B | h200b |
|---|---|---|---|
| Listen / remote port | **8003** | **8005** | **8004** |
| GPUs | 2,3 | **0,1** | 2,3 |
| Gateway env var | `H200_DEPLOYMENT_URL` | `H200A2_DEPLOYMENT_URL` | `H200B_DEPLOYMENT_URL` |
| systemd unit | `h200_idle_proxy` | `h200_idle_proxy_b` | `h200_idle_proxy` |
| Config | [`models.json`](models.json) | derived (below) | [`models.json`](models.json) |

Install replica B with `sudo REPLICA=b ./install.sh` (add `TUNNEL_USER=juncheng`
on h200a, where root has no SSH key for the routers — later runs keep it). Its units are named
separately from A's on purpose: before `REPLICA` existed, re-running the installer
with a different port rewrote A's single unit in place, so one host could not hold
two.

### Why two TP=2 replicas instead of one TP=4

**TP=4 scales at only ~0.71 efficiency on these cards**, so two TP=2 replicas beat
one TP=4 instance on aggregate throughput. Measured on h200a — same node, same
flags, `/flush_cache` before every point:

| | prefill c=32 | decode c=64 | decode c=128 | decode c=1 ITL |
|---|---:|---:|---:|---:|
| 1× TP=4 | 25,181 | 3,979 | 6,014 | 1.92 ms |
| 1× TP=2 | 16,821 | 3,027 | 4,215 | 1.92 ms |
| **2× TP=2 (projected)** | **33,643** | **6,054** | **8,430** | 1.92 ms |
| gain over TP=4 | **+34%** | **+52%** | **+40%** | — |

TP=4 buys latency and KV headroom, not capacity: single-stream median ITL is
1.92 ms either way, and TP=4's KV pool is 7.38M tokens against TP=2's 2.94M.

Two caveats on that table. The 2× column is **twice a single-replica
measurement, not a measured concurrent aggregate** — running both replicas flat
out simultaneously has not been measured, and that is the only way to expose a
shared-host ceiling. And running two replicas claims all four GPUs permanently,
so the idle timeout no longer hands the box back to other tenants in practice.

TP=3 is not an option: DeepSeek-V4-Flash has 64 attention heads, indivisible by 3.

### Replica B's config is derived, not copied

Replica B ships no `models.json`. Its unit runs
[`replica_b_config.py`](replica_b_config.py) as `ExecStartPre`, which reads replica
A's `models.json`, overrides exactly four keys (`container`, `backend_port`,
`gpu_index`, `cache_dir`) and writes the result to a tmpfs path under `/run`.

That is deliberate, and it is the warning in the section above taken seriously: a
checked-in near-duplicate is what `models.h200.json` used to be, and it drifted —
FP8 at PP=3 on GPUs 0,2,3 on one side, NVFP4 at TP=2 on the other — until #1185
reconverged them, after which #1187's `mem_fraction` change still had to be
applied by hand twice. Between two replicas of one model the drift is worse,
because both answer the same model id behind one route set: a divergence surfaces
as unexplained latency skew, not as an error. Edit `models.json` and
`systemctl restart h200_idle_proxy_b`; B follows A.

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

`status` and `stop` need the **same** `LISTEN_PORT` as `start`. The run directory
holding the PID files is per-port, so a bare `stop` on h200b looks in the 8003
directory, reports the proxy as not running, and leaves its container holding the
GPUs:

```bash
# h200a
./ops/h200_idle_proxy/h200_idle_service.sh status
./ops/h200_idle_proxy/h200_idle_service.sh stop

# h200b
LISTEN_PORT=8004 ./ops/h200_idle_proxy/h200_idle_service.sh status
LISTEN_PORT=8004 ./ops/h200_idle_proxy/h200_idle_service.sh stop
```

Pass `SSH_HOST` and `REMOTE_PORT` as well for `status` to report on the tunnels.

Logs: `/tmp/h200_idle_proxy_<uid>_<port>/proxy.log` (per-user run dir, mode 700).

None of this applies under systemd, which is how both nodes actually run — there the
port lives in the unit, so `systemctl status|restart h200_idle_proxy` works unqualified
on either node.

### systemd (recommended)

```bash
sudo ./ops/h200_idle_proxy/install.sh                                   # h200a
sudo LISTEN_PORT=8004 REMOTE_PORT=8004 ./ops/h200_idle_proxy/install.sh # h200b
# remove:
sudo ./ops/h200_idle_proxy/uninstall.sh
```

Default tunnels: `spark2` and `jason@internal.freeinference.org`.

`SSH_HOST` is the whole list, not an addition to it. A router an earlier run
enabled and this one leaves out is stopped and disabled, because `Restart=always`
plus the `multi-user.target` symlink would otherwise keep it advertising this box
across reboots. Only the replica being installed is considered, so `REPLICA=a`
never touches B's tunnels.

`TUNNEL_USER` behaves like `LOCAL_API_KEY` below: a later run that does not pass
it keeps the user already installed. Reverting h200a's tunnels to the unit's
`root` default would leave them unable to authenticate against the routers and
autossh restarting forever, so pass `TUNNEL_USER=` explicitly to hand them back.

The unit reads the repo's `.env` for `LOCAL_API_KEY` (see [Gateway
configuration](#gateway-configuration)). Neither H200 node normally has one, so
pass the key on the first install — `sudo LOCAL_API_KEY='…'
./ops/h200_idle_proxy/install.sh` — and it lands in a mode-0600 drop-in. The
port-only re-runs above keep whatever key is already installed; to remove it, pass
`LOCAL_API_KEY=` explicitly or run `uninstall.sh`.

## Gateway configuration

On **both** staging and production, set the following in `.env` — the first is
h200a, the second h200b, and the third must match the key the proxy checks
against:

```bash
H200_DEPLOYMENT_URL=http://host.docker.internal:8003/v1
H200B_DEPLOYMENT_URL=http://host.docker.internal:8004/v1
LOCAL_API_KEY=freeinference_api
```

Write those with no trailing `# …` comment and no `export ` prefix. Compose reads
`.env` with a dotenv parser that strips both; the proxies' systemd units read the
same file with systemd's parser, which per systemd.exec(5) ignores only lines
*starting* with `#` and keeps "interior whitespace within the line … verbatim".
So `LOCAL_API_KEY=freeinference_api   # must match the proxy` gives the gateway
`freeinference_api` and the proxy `freeinference_api   # must match the proxy` —
the same 401-on-every-request mismatch, arrived at from a file that looks right.

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
| `mem_fraction` | `0.80` — **not** 0.90; DSpark + a 1M prefill need the headroom (see below) |
| HiCache | **off** — no `hicache_*` key at all; it hangs the scheduler under DSpark (see below) |
| `moe_runner_backend` | `marlin` — **required** for FP4 experts on H200 (SM90) |
| `mtp` / `speculative_algorithm` | `true` / `DSPARK` |
| `sglang_image` | `lmsysorg/sglang:nightly-dev-20260806-ae5f8c94` — DSpark needs ≥ 0.5.16; the nightly is for grammar support (see below) |
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

> **Why `mem_fraction` 0.80 and not 0.90?** Two separate failures pushed it down, both
> measured on these boxes:
>
> 1. DSpark costs roughly 10 GB/GPU beyond the non-speculative configuration: ~5.5 GB of
>    draft weights, plus a *second* set of verify CUDA graphs (sglang captures a target
>    verify graph and a draft verify graph, each about 5 GB at the default `bs` ladder up
>    to 256). At **0.90** that left 12.33 GB free after weights+KV, 7.20 GB after the
>    target graph, and ~4.7 GB of 143.7 GB once the draft graph landed. The server
>    OOM-crashed within ~48 s of taking real production traffic; the idle proxy rebuilt
>    the container, so clients saw the "model is starting up" banner on a ~9-minute loop.
> 2. At **0.85** (10.89 GB free) sustained normal-length traffic was stable for 30+
>    minutes, but a single ~1M-token prefill still died: `CUDA out of memory. Tried to
>    allocate 7.12 GiB. 7.08 GiB is free` — short by ~40 MB. The scheduler raised, sglang
>    SIGQUIT'd itself and exited 0, so **one long-context request took down the whole
>    node**.
>
> **0.80** leaves 17.91 GB free and serves the full 1M context (see below). The KV pool
> drops from 4.54M tokens at 0.90 to 2.94M, which is still ~2.9x a single 1M request.
> Raise it again only alongside a smaller CUDA-graph batch ladder, and re-test 1M before
> trusting it — sustained-load stability does **not** imply long-context safety.

> **Why HiCache is off.** It deadlocks the scheduler when DSpark is on. sglang builds
> the tree cache hierarchically and then declines to manage the draft pool:
>
> ```
> Tree cache initialized: impl=UnifiedRadixCache hybrid_swa=True hierarchical=True
> Draft pool type DeepSeekV4TokenToKVPool not supported for HiCache, skipping.
> ```
>
> So the target KV pool is written back to host DRAM while the speculative draft pool
> is not tracked at all. Under ordinary traffic — single-digit concurrency, KV pool
> 4% full — both TP ranks stop making progress mid-decode, and 300 s later the
> scheduler watchdog SIGQUITs the server:
>
> ```
> [TP0] Scheduler watchdog timeout (self.watchdog_timeout=300, self.soft=False)
> [TP1] Scheduler watchdog timeout (self.watchdog_timeout=300, self.soft=False)
> ```
>
> On 2026-08-06 this took down **both** h200a replicas, twice each, roughly 20 minutes
> into serving. There is no OOM, no Xid and no CUDA error — the ranks simply stop, so
> it reads as a hang rather than a crash, and py-spy cannot dump the stack
> (`Failed to copy Py_Version symbol`). h200b ran the same model, image, TP size and
> `mem_fraction` for 3+ hours without a watchdog; the only difference was that the
> `dev` → `main` promotion had been held, so HiCache had not reached it. Removing the
> keys and cold-starting both replicas ended it.
>
> Turning it back on needs an sglang release whose HiCache path supports
> `DeepSeekV4TokenToKVPool` for the draft pool, or DSpark switched off — which costs
> far more than HiCache returns (DSpark is worth 3.2x at c=1 and ~1.3x at saturation;
> HiCache only improves TTFT on a prefix-cache hit and does not enlarge the KV budget
> or the usable context).
>
> Two traps for whoever re-enables it. **Any** `hicache_*` key turns the feature on —
> `_hicache_args` emits `--enable-hierarchical-cache` as soon as one is present — so a
> leftover `hicache_write_policy` is enough. And sglang **rejects `--hicache-size` for
> this architecture** (`ValueError: DeepSeek V4 HiCache currently does not support
> --hicache-size; use --hicache-ratio instead`, SIGQUIT at scheduler init), so sizing
> must go through `hicache_ratio`. The pool is **per scheduler process**, not per box:
> a ratio multiplies the ~34 GB device KV pool, and this box runs four ranks, so
> ratio 5 is ~700 GB of 1507 GB. Each rank guards its own allocation against
> `psutil.virtual_memory().available - 10 GB`, which makes over-sizing fail
> asymmetrically — rank 0 pins its share, rank 1 raises `Not enough host memory
> available`, or the two race past the check and the OOM-killer takes the node.

> **Why `DSPARK` and not `EAGLE`?** 0731's speculative module is DSpark, not the
> preview checkpoint's MTP: 3 blocks (`dspark_target_layer_ids: [40,41,42]`) with
> `main_proj`/`main_norm` plus `markov_head` and `confidence_head`, versus one block
> with `e_proj`/`h_proj`. sglang < 0.5.16 hardcodes the preview prefixes and asserts a
> single nextn layer, so `EAGLE` loads an unpopulated draft head — the server reaches
> "ready to roll" and then 500s every request. Note `config.json` still reports
> `num_nextn_predict_layers: 1`, which is misleading.

### Why a nightly image

`v0.5.16` rejects **every grammar-constrained request** on this deployment with an
HTTP 400:

```
DFLASH speculative decoding does not support grammar-constrained decoding yet.
```

The guard is `validate_dflash_request()`, and it fires for the whole DFlash family
— `is_dflash_family()` is `is_dflash() or is_dspark()`, so our `DSPARK` config is in
scope even though the message says DFLASH. It rejects `response_format`
(`json_object` **and** `json_schema`), `regex`, `ebnf`, `structural_tag`, and
`tool_choice: "required"` / a named function. Only `tool_choice: "auto"` survives.

On the streaming path sglang emits that 400 as an **in-band SSE `error` frame under
an HTTP 200**, followed by a placeholder `usage` of `prompt_tokens: 1,
completion_tokens: 1`. The gateway therefore logged these as successful 200s with
1/1 tokens and never failed over — 82 k requests in six hours, ~100% of all
`response_format` traffic.

[sgl-project/sglang#30096](https://github.com/sgl-project/sglang/pull/30096) makes
the rejection conditional on `spec_algorithm.supports_grammar_overlap()`, which is
true for the DFlash family, and adds the verify-time bitmask that makes it correct.
It merged to `main` on 2026-07-25 **11:36 UTC** — eleven hours after `v0.5.16` was
cut at 00:13 UTC the same day. No release contains it yet, so the pin is the
2026-08-06 nightly (`ae5f8c94`, 456 commits past the fix).

**Move back to a tag when v0.5.17 ships.** Releases have run roughly fortnightly
(0.5.13 Jun 13 → 0.5.14 Jun 26 → 0.5.15 Jul 10 → 0.5.16 Jul 25), so v0.5.17 is due
around 2026-08-08. A nightly is 456 unreviewed commits of drift on a 1M-context MoE;
it is a bridge, not a destination.

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
>
> It also issues a **warm-up request using the same prompt as the measured run**, so at
> small `--num-prompts` the measured request is a cache hit and `/flush_cache` before the
> run does not help. At 1M it reported 330k tok/s and a 2.8 s TTFT while the server log
> showed the measured request as `#new-token: 256, #cached-token: 999936`. Use
> [`longctx_probe.py`](longctx_probe.py) for long-context numbers: it streams one request
> per length with freshly randomised token ids, takes prefill from time-to-first-token and
> decode from the gaps between later tokens, and prints `cached` so a contaminated
> measurement is visible.

### Long context

Single-stream, `mem_fraction 0.80`, DSpark on, freshly randomised ids per request
(`cached` 0 for every row), 128 generated tokens with `ignore_eos`:

| Input | TTFT | Prefill tok/s | Decode tok/s | ITL | Tokens/SSE chunk |
|---:|---:|---:|---:|---:|---:|
| 4 K | 0.28 s | 14,681 | 332 | 3.01 ms | 3.6 |
| 32 K | 2.02 s | 16,221 | 445 | 2.25 ms | 4.7 |
| 128 K | 8.98 s | 14,594 | 391 | 2.56 ms | 4.3 |
| 256 K | 20.6 s | 12,716 | 400 | 2.50 ms | 4.4 |
| 512 K | 52.3 s | 10,032 | 277 | 3.62 ms | 3.2 |
| 1 M | **138 s** | 7,245 | 252 | 3.98 ms | 3.1 |

Prefill peaks near 32 K and falls to ~7.2k tok/s at 1M, so **a full 1M prompt costs ~2.3
minutes before the first token** — worth knowing before pointing a client timeout at it.
Decode degrades far more gently (445 → 252 tok/s). DSpark keeps paying off across the
whole range: tokens per SSE chunk stays between 3.1 and 4.7, so acceptance does not
collapse at long context. (`bench_serving` simply stops printing "Accept length" at
≥256 K, which looks like DSpark switching off but isn't.)

## Requirements

- 4× NVIDIA H200 (or at least GPUs 2 and 3 free)
- Docker + NVIDIA Container Toolkit
- `lmsysorg/sglang:v0.5.16` or newer (**DSpark is not in ≤ 0.5.15**); a
  post-v0.5.16 nightly if grammar-constrained decoding is needed (see
  [Why a nightly image](#why-a-nightly-image))
- Weights at `model_dir` (or `hf_repo` download on first request)
- SSH to staging/prod with `GatewayPorts clientspecified` (or `yes`)
- `autossh` for durable tunnels (installed by `install.sh`)
