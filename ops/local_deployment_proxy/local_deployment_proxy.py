#!/usr/bin/env python3
"""Multi-model reverse proxy that lazily starts/stops sglang containers.

The proxy listens on ``LISTEN_PORT`` and routes requests to the correct
sglang backend based on the ``model`` field in the request body.  Each model
has its own Docker container, GPU, idle timer, and health-check lifecycle.

When the first request for a model arrives its container is started; after
``IDLE_TIMEOUT`` seconds of inactivity the container is stopped.  The proxy
stays alive so callers always see an open port.

Usage:
    # Foreground (Ctrl-C to quit):
    python local_deployment_proxy/local_deployment_proxy.py

    # Background:
    nohup python local_deployment_proxy/local_deployment_proxy.py &

    # Custom settings via environment:
    LISTEN_PORT=9000 IDLE_TIMEOUT=600 python local_deployment_proxy/local_deployment_proxy.py

Environment variables
---------------------
LISTEN_PORT    : Port the proxy binds to                  (default 8001)
IDLE_TIMEOUT   : Seconds of inactivity before stopping    (default 1440 = 24 min)
HEALTH_TIMEOUT : Max seconds to wait for backend startup  (default 600)
HEALTH_INTERVAL: Seconds between health-check polls       (default 10)
MAX_START_FAILURES : Consecutive failed starts after which the proxy stops
                 trying to start that backend (default 20; 0 = no limit)
MODELS_CONFIG  : Path to a JSON config file               (see below)
PROXY_OWNER    : Identity stamped on containers this proxy starts, so it never
                 destroys or adopts a sibling proxy's backend of the same name
                 (default ``port-$LISTEN_PORT``; see "Container ownership")

When ``MODELS_CONFIG`` is unset the proxy auto-selects a hardware profile from
``nvidia-smi``: ``../h200_idle_proxy/models.json`` on a 4+ x H200 box
(DeepSeek-V4-Flash-0731 at ``tensor_parallel_size`` 2 on GPUs 2,3),
``models.rtx6000.json`` on an RTX (PRO) 6000 (Qwen3.6-35B), else
``models.json``. See ``_detect_profile_config``. For the dedicated H200 service
(port 8003 + staging/prod tunnels) use ``ops/h200_idle_proxy`` instead.

Model configuration
-------------------
Models are defined in a JSON file (default: ``models.json`` next to this
script).  Each key is a model name that clients send in the ``model`` field.
Example::

    {
        "Qwen/Qwen3.6-35B-A3B-FP8": {
            "container": "qwen36-sglang",
            "gpu_index": "1",
            "backend_port": 18001,
            "model_dir": "/scratch/juncheng/models/Qwen3.6-35B-A3B-FP8",
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

``gpu_index`` can be omitted to auto-pick the least-used GPU. Set
``tensor_parallel_size`` > 1 to shard one model across several GPUs: pin them
with a comma-list ``gpu_index`` (e.g. ``"0,1,2,3"``) or omit it to auto-pick N.
The launch then gets ``--tp`` / ``--tensor-parallel-size N`` and ``--ipc=host``.

Set ``pipeline_parallel_size`` > 1 to shard one model across GPUs *by layer*
(pipeline parallelism) instead of, or in addition to, tensor parallelism. Unlike
TP, PP has no attention-head divisibility constraint, so it can use a GPU count
TP cannot (e.g. 3 GPUs for a 64-head model that must otherwise be TP 1/2/4). The
backend claims ``tensor_parallel_size * pipeline_parallel_size`` GPUs (pin them
with ``gpu_index`` or auto-pick that many); the launch gets ``--pp-size``
(sglang) / ``--pipeline-parallel-size`` (vLLM). A multi-GPU ``gpu_index`` is
passed to Docker as a *quoted* ``--gpus '"device=0,2,3"'`` so the daemon does
not split the comma list into separate (and conflicting) device requests.

Give two or more models the same ``colocate_group`` to make them share one
GPU: the first to start auto-picks a free device and the rest follow it there
(instead of being spread onto separate GPUs). Size their ``mem_fraction``
values so the group sums to roughly 0.9 or less.

Set ``"is_embedding": true`` on a model to launch sglang in encode-only mode
(adds ``--is-embedding``); such models serve ``/v1/embeddings`` instead of chat.

Set ``"engine": "vllm"`` to serve a model with vLLM (``vllm/vllm-openai``)
instead of sglang (the default). vLLM listens on container port 8000 rather
than sglang's 8001, but callers, the health check, and the proxy all reach the
backend through the host ``backend_port``, so the switch is transparent. The
sglang-only knobs (``mtp``, ``mamba``, ``moe_runner_backend``,
``attention_backend``, …) are ignored for vLLM backends; see ``_vllm_run_cmd``
for the vLLM-specific options.

Set ``"mtp": true`` on a generative model that ships native Multi-Token
Prediction layers (Qwen3.6 MoE, DeepSeek V3, …) to enable speculative decoding
via sglang's ``NEXTN`` algorithm. The defaults (1 step, eagle-topk 1, 2 draft
tokens) suit a single MTP layer; override with ``speculative_num_steps``,
``speculative_eagle_topk``, ``speculative_num_draft_tokens``, or
``speculative_algorithm`` if needed. DeepSeek-V4-Flash requires
``"speculative_algorithm": "EAGLE"`` (sglang rejects ``NEXTN`` for that arch).

DeepSeek-V4-Flash-**0731** instead ships a DSpark head (3 blocks, plus markov and
confidence heads) and needs ``"speculative_algorithm": "DSPARK"`` on sglang
>= 0.5.16; earlier builds only implement the preview checkpoint's single-block
MTP and load an unpopulated draft head, which serves 500s. For DSPARK the
step/topk/draft-token knobs are left unset unless configured, so sglang can read
the draft block size (gamma) from the checkpoint and size the verify window as
gamma + 1. Pin ``"sglang_image"`` to hold a known-good tag.

Set ``"skip_server_warmup": true`` to pass ``--skip-server-warmup``, and
``"cache_dir"`` to bind-mount a persistent DeepGEMM/JIT kernel cache at
``/root/.cache``; both cut startup time on large MoE models. Keep ``cache_dir``
node-local rather than on shared storage.

Set ``"hicache_size"`` (GB) to give a generative model an L2 prefix-cache tier
in host DRAM (sglang HiCache), so prefixes evicted from HBM are restored over
PCIe instead of recomputed. The size is allocated **per scheduler process** --
per TP rank per PP stage -- so a box pins about ``tp * pp * hicache_size`` GB of
unswappable memory; size it against ``free -g`` divided by the rank count, not
against total RAM. sglang aborts a rank whose share exceeds free RAM minus a
10 GB reserve. ``hicache_ratio`` sizes the pool as a multiple of the device pool
instead (ignored when ``hicache_size`` is set); ``hicache_write_policy``,
``hicache_io_backend`` and ``hicache_mem_layout`` map to the matching flags.
Ignored for embedding models, which run ``--disable-radix-cache``.

Set ``"moe_runner_backend"`` (e.g. ``"marlin"``) to override the MoE runner.
NVFP4 / FP4-expert checkpoints need ``"marlin"`` on pre-Blackwell (SM90, e.g.
H200) GPUs; the default ``triton`` runner asserts on the packed FP4 shapes.

Additionally set ``"mamba": true`` on hybrid Mamba/linear-attention models
(Qwen3.5/3.6 MoE). sglang rejects MTP spec decoding alongside the default
radix cache for these unless the Mamba scheduler reserves extra buffers; the
flag adds ``--mamba-scheduler-strategy extra_buffer`` (override with
``mamba_scheduler_strategy``) and exports ``SGLANG_ENABLE_SPEC_V2=1``.
"""

from __future__ import annotations

import contextlib
import hashlib
import json as _json
import logging
import os
import re
import subprocess
import threading
import time
from http.client import RemoteDisconnected
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, NamedTuple
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("local_deployment_proxy")

LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "8001"))
IDLE_TIMEOUT = int(os.environ.get("IDLE_TIMEOUT", "1440"))
HEALTH_TIMEOUT = float(os.environ.get("HEALTH_TIMEOUT", "600"))
HEALTH_INTERVAL = float(os.environ.get("HEALTH_INTERVAL", "10"))
# Consecutive failed starts after which a backend stops being started at all.
#
# Nothing else here bounds a crash-looping container: every request for a model
# that is not `ready` calls `ensure_running`, so a backend that cannot start --
# a bad image tag, a checkpoint sglang rejects, a GPU another process owns --
# gets a fresh `docker run` per request, for as long as traffic keeps arriving.
# Each attempt claims a GPU, can sit in `_wait_healthy` for HEALTH_TIMEOUT
# (900s on the H200 units), and answers the caller with a slow 502; the gateway
# reads that as a timing-out upstream rather than a dead one, and the log fills
# with the same traceback deep enough to bury the first one, which is the only
# copy that says why.
#
# Past this many consecutive failures the proxy gives up on that model and fails
# fast instead: the gateway opens its circuit and fails the model over to its
# remote route, the GPU is left alone, and the log stops. The give-up is latched
# until the proxy restarts rather than being retried on a timer -- fixing the
# cause means editing the model config, which is read once at import, so a
# restart is already part of the repair. Other models are unaffected; the latch
# is per backend.
#
# Set to 0 to disable the limit and keep retrying forever.
MAX_START_FAILURES = int(os.environ.get("MAX_START_FAILURES", "20"))

# ── Forward-progress probe (GET /health answers from this) ─────────────────
# On 2026-08-09 the sglang scheduler on h200b wedged mid-serving. For eight
# minutes every inference request hung and then failed on the 300s `urlopen`
# timeout below, and throughout that window this proxy answered `GET /health`
# with an unconditional 200 — so the gateway kept the replica registered and
# kept feeding it requests that could only time out. sglang's own watchdog then
# SIGQUIT'd the process tree, the container exited, and for another 73 seconds
# the proxy still believed `state == "ready"` because nothing demotes a backend
# that dies out of band until a request discovers the corpse.
#
# `/health` now reports whether the proxy's belief matches reality (see
# `BackendManager.health_report`). Three knobs, all about *not flapping*:
#
# HEALTH_PROBE_PATH — sglang's own health handler, which is the only endpoint
#   that answers the question that matters. It submits a one-token generate and
#   waits for any inbound message from the detokenizer, so its verdict is
#   literally "has this server produced anything recently" — the definition of
#   forward progress, and the thing that stops during a wedge. `/v1/models`
#   (what `_backend_healthy` polls) reads in-process tokenizer attributes and
#   never touches the scheduler: it answered 200 for the whole eight minutes and
#   would have detected nothing. `/health_generate` rather than `/health`
#   because sglang's `/health` degrades to a static 200 when
#   SGLANG_ENABLE_HEALTH_ENDPOINT_GENERATION is turned off, and a wedge detector
#   an env var can silently disable is not a detector. Both are exempt from
#   sglang's API-key middleware, so no credential is needed here.
# HEALTH_PROBE_STRIKES — consecutive failed probes before the verdict flips. The
#   probe runs on the idle watcher's existing tick, `min(10, IDLE_TIMEOUT / 2)`
#   seconds, but a wedged backend does not *refuse* the probe, it swallows it: the
#   call blocks for the whole HEALTH_PROBE_TIMEOUT before failing, so the loop
#   period during the failure this exists to detect is the tick plus the timeout,
#   ~20s on the shipped units, not ~10s. Three strikes is therefore a ~40-60s
#   grace, not the ~30s the tick alone suggests — long enough that one slow
#   chunked prefill emitting nothing cannot deregister a replica, still inside
#   sglang's own 300s watchdog kill. Recovery is a single good probe, matching the
#   gateway's own asymmetry (one probe down, one probe up) so a stale verdict
#   cannot outlive the condition.
# HEALTH_PROBE_WARMUP — how long a backend may go without ever answering the probe
#   before silence starts counting as a fault. There has to be some window: the
#   backend reaches `ready` on `/v1/models`, which sglang answers as soon as
#   uvicorn binds, while the forward-progress endpoint keeps failing until warmup
#   completes, so without a grace every cold start would deregister its own
#   replica. But the grace cannot be open-ended, which is what "has the probe ever
#   succeeded?" alone amounts to: `_adopt_running_container` promotes on
#   `/v1/models` too, so a proxy restarted *during* a wedge — by systemd, or by
#   hand mid-incident — adopts the wedged container into `ready` with the latch
#   unearned, and an unbounded grace would then hide that wedge forever. Defaults
#   to HEALTH_TIMEOUT because that is already this deployment's answer to "how
#   long may this backend take to become usable" (900s on the H200 units), and a
#   backend that has had that budget twice over and still produced nothing is not
#   loading.
HEALTH_PROBE_PATH = os.environ.get("HEALTH_PROBE_PATH", "/health_generate")
HEALTH_PROBE_TIMEOUT = float(os.environ.get("HEALTH_PROBE_TIMEOUT", "10"))
HEALTH_PROBE_STRIKES = int(os.environ.get("HEALTH_PROBE_STRIKES", "3"))
HEALTH_PROBE_WARMUP = float(os.environ.get("HEALTH_PROBE_WARMUP", str(HEALTH_TIMEOUT)))

# ── Container ownership ────────────────────────────────────────────────────
# A container *name* is not proof of ownership. Several units run this same
# script with different MODELS_CONFIG values (deploy/systemd/*_idle_proxy.service),
# an operator can hand-start a backend to benchmark it
# (ops/h200_idle_proxy/bench_decode.sh), and every lifecycle call here used to
# address the container by name alone: `docker rm -f <name>` before every start,
# another on the idle path, and an adoption check that only asked "is something
# running and answering on this port?". So two processes that happen to resolve
# the same container name silently destroyed and re-adopted each other's backend,
# mid model load or mid stream.
#
# Fix: stamp two labels at `docker run` and consult them before destroying or
# adopting anything.
#   owner   — who runs it. The listen port is the natural identity: only one
#             process can hold it on a host, and it is stable across restarts of
#             the same unit (so restart-and-adopt keeps working). Override with
#             PROXY_OWNER to claim a distinct identity for a hand-run container.
#   profile — sha256 of the *launch-affecting* part of the resolved per-model
#             config. Same owner + different hash means "my own config changed on
#             disk" → replace. A *different* owner means hands off, whatever the
#             hash.
#
# The owner check is gated on the container being *unfinished*: an exited container
# of the same name holds no GPU and serves nothing, so refusing to touch it would
# only wedge the name forever (nothing on any path removes a foreign container).
# "Unfinished" and not merely "running", because docker reports a container that has
# been created and not yet started as not running too, and that is what a sibling's
# own `docker run -d` looks like for the moment between its create and its start —
# see `_RECLAIMABLE_STATUSES`.
#
# A label read is only worth as much as the gap between reading it and acting on
# it, and on the cold-start path that gap is not microseconds: the first version
# of this guard checked ownership once at the top of `_start_container` and then
# ran an unconditional `docker rm -f <name>` minutes later, after `_resolve_gpu`
# and an `_ensure_model_dir` that can be a several-hundred-GiB Hugging Face
# download. Two proxies cold-starting the same name both saw "nothing there",
# and the slower one destroyed the container the faster one had meanwhile
# created — the very cross-kill the labels exist to stop, on the one path where
# it is most likely (a node reboot brings both units up at once).
#
# So no removal is authorised by a stale reading, and no removal names the
# container by name:
#   * the ownership decision is re-taken immediately before the removal it
#     authorises, one docker call earlier rather than a download earlier
#     (`_clear_container_name`), and
#   * `docker rm -f` is given the container *id* that decision was taken about.
#     Ids are unique and never reused, so if the container we judged removable is
#     gone by the time the removal lands, the removal misses instead of hitting
#     whatever now holds the name.
# What is left is the window between that inspect and the `docker run` after it,
# and docker closes that one itself: container names are unique, so `docker run
# --name X` fails outright when X exists. That failure is the only atomic claim
# on a name available to us — it is detected (`_is_name_conflict`) and read as
# contention, which sends the ownership decision round again rather than
# destroying anything.
#
# All of that guards the moment a backend is *started*. The mirror-image gap is a
# backend already running: `_proxy` forwards a request straight to
# localhost:backend_port whenever the manager says "ready", with no ownership check
# and no docker call, because a warm request must not pay for one. Nothing demoted
# "ready" when the container died out of band — the idle watcher reads only local
# state, and the reactive reconcile in `_forward_with_body` needs the port to stop
# answering — so once a sibling reclaimed this backend's corpse and launched its own
# container on the same name and port, that fast path forwarded to the sibling's
# backend indefinitely: the wrong model, success-shaped, under two idle watchers that
# each believed they owned it. The identity the manager is ready over is therefore
# recorded (`_container_id`, free from `docker run -d`'s own output) and re-checked in
# the loop the proxy already runs, not on the request path — see
# `_disown_if_replaced`.
OWNER_LABEL = "com.freeinference.proxy.owner"
PROFILE_LABEL = "com.freeinference.proxy.profile"
PROXY_OWNER = os.environ.get("PROXY_OWNER", "").strip() or f"port-{LISTEN_PORT}"
# Identity, liveness and ownership are read with ONE `docker inspect`, formatted
# as a JSON object so all three come back from a single daemon round trip. Two
# inspects would double the daemon calls on the warmup path (where the contention
# check runs on every streaming request until the backend is ready) and leave a
# window in which the container can stop between the two answers — or, worse, be
# replaced between "whose is it?" and "what is its id?", which would hand a
# removal the id of a container nobody judged.
_INSPECT_STATE_FORMAT = (
    '{"id":{{json .Id}},"running":{{json .State.Running}},'
    '"status":{{json .State.Status}},"labels":{{json .Config.Labels}}}'
)
# `.State.Status` is read because "not running" is NOT the same as "a corpse", and
# only a corpse of another owner may be reclaimed. Docker derives the status from
# the same struct as `.State.Running` (`container.State.StateString`), so the two
# together say exactly which of docker's states this is:
#
#   running / paused / restarting  → Running true  (already refused: hands off)
#   exited / dead / removing       → Running false, and genuinely finished
#   created                        → Running false, and about to be started
#
# That last one is the one this set exists for. `docker run -d` is create-then-
# start, the name is reserved and the `--label` stamp is written at *create*, so a
# sibling's ordinary launch is observable as `created` for as long as its start
# takes (nvidia-container hooks, device injection — not instantaneous). Reclaiming
# on `Running == false` alone therefore destroys a stranger's brand-new container,
# with no operator and no stale reading involved: the labels we read are current,
# they just do not mean what "not running" was taken to mean.
#
# An empty status (a daemon that did not report one) falls back to the previous
# rule rather than refusing, for the reason `_inspect_state` fails towards
# "nothing to remove": a reading we cannot interpret must not be able to wedge a
# name, and this whole set is a narrowing of an existing removal, not a new one.
_RECLAIMABLE_STATUSES = frozenset({"exited", "dead", "removing"})
# `docker run` refusing a name that is already taken. Matched on the message
# rather than on exit status alone: the status is 125 for every daemon-side
# rejection, so it cannot tell a name conflict from a missing GPU driver, and
# `sudo` can rewrite it besides.
#
# Both halves of the pattern are needed. "already in use" alone also matches
# `listen tcp 0.0.0.0:18099: bind: address already in use` — a port collision,
# which is a real failure to report rather than contention to retry — so the
# phrase only counts when docker attributes it to a container name. Neither
# misreading is destructive: a hard failure misread as contention costs one
# retry and then raises, and contention misread as a hard failure fails the
# request that the next one retries. Nothing is removed on either path without
# its own fresh ownership check.
_NAME_CONFLICT_RE = re.compile(
    r"container name\b.{0,200}?\balready in use|already in use by container",
    re.IGNORECASE | re.DOTALL,
)
# Pause before the one retry a name conflict gets. Docker releases a name when the
# removal finishes inside the daemon, which can be after `docker rm -f` returned, so
# an instant re-run can collide with the release it is waiting for. Half a second is
# nothing against a container start and covers that flake.
_NAME_CONFLICT_RETRY_DELAY = 0.5


def _is_name_conflict(stderr: str) -> bool:
    """Return True if this ``docker run`` failure means "that name is taken"."""
    return bool(_NAME_CONFLICT_RE.search(stderr or ""))


# `docker rm` answering that the target is gone. Removals here are aimed at a
# container *id* precisely so that a container replaced since its labels were read
# is missed rather than destroyed, so this is the design working and not a failure
# to report — see `_remove_container`.
_MISSING_CONTAINER_RE = re.compile(r"no such container", re.IGNORECASE)


def _is_missing_container(stderr: str) -> bool:
    """Return True if this ``docker rm`` failure means "it was already gone"."""
    return bool(_MISSING_CONTAINER_RE.search(stderr or ""))


def _launched_container_id(stdout: str) -> str | None:
    """Read the container id ``docker run -d`` printed, or None if it did not.

    Both engines launch detached, and a detached run's whole stdout is the new
    container's id — so the identity this proxy later re-checks
    (``_disown_if_replaced``) is free: no second ``docker inspect``, on a path where
    an extra daemon call would land during a cold-start storm.

    The *last* non-empty line is taken, not the whole output: a sudo or docker
    wrapper that prints a banner first would otherwise make the id unusable. A
    single token is required for the same reason — anything else is a wrapper
    talking, and the honest answer is then "no id", which downgrades the identity
    check to the owner-label check rather than inventing a mismatch that would
    demote a healthy backend on every poll.
    """
    lines = [line.strip() for line in (stdout or "").splitlines() if line.strip()]
    if not lines:
        return None
    candidate = lines[-1]
    return candidate if len(candidate.split()) == 1 else None


class _ContainerState(NamedTuple):
    """What one ``docker inspect`` says about the container of a given name.

    ``container_id`` is the load-bearing field: it is what ``docker rm -f`` is
    given, so a removal can only ever land on the exact container whose labels
    were just read. It falls back to the container *name* when the inspect
    succeeded but reported no usable id — that is the pre-existing behaviour, and
    degrading to it beats declining to remove anything and wedging the name.

    ``status`` is docker's own state word (``created``, ``exited``, ``dead`` …).
    ``running`` cannot stand in for it: it is false for a container that has not
    started *yet* as well as for one that has finished, and only the second is a
    corpse another owner has no further use for. See ``_RECLAIMABLE_STATUSES``.
    """

    exists: bool
    container_id: str
    running: bool
    status: str
    labels: dict[str, str]


_ABSENT_CONTAINER = _ContainerState(
    exists=False, container_id="", running=False, status="", labels={}
)
# The device request is read as raw JSON and joined in Python (see
# `_running_container_gpu`). A Go template cannot do it: `range` emits its
# elements with no separator, so DeviceIDs ["2","3"] — how docker stores the
# single quoted request `--gpus '"device=2,3"'` — came back as the token "23".
_INSPECT_DEVICES_FORMAT = "{{json .HostConfig.DeviceRequests}}"
# Config keys deliberately left *out* of the profile hash: they never reach either
# `docker run` command line, so a container launched before such an edit is not
# stale after it. This matters because a hash mismatch destroys a healthy backend
# and pays a full weight reload -- ~14 minutes on DeepSeek-V4-Flash, during which
# the model fails over to its paid remote route. `startup_estimate_seconds` only
# words the warmup SSE banner (see `_warmup_thinking_sse`) and exists precisely to
# be re-tuned against measured cold-start times, so it is the key most likely to
# be edited on a live node; the `hf_*` keys are read once by `_ensure_model_dir`
# at download time. Everything else stays in -- including `colocate_group`, which
# steers GPU auto-selection and therefore the `--gpus` request.
NON_LAUNCH_CONFIG_KEYS = frozenset(
    {
        "startup_estimate_seconds",
        "hf_repo",
        "hf_revision",
        "hf_ignore_patterns",
    }
)
# LOCAL_API_KEY is the key the gateway signs its requests to this proxy with. (A
# comment here used to name FREEINFERENCE_API_KEY as a compatibility fallback; no
# such fallback is read, and that variable is the gateway's *own* client key --
# see ops/setup/setup_claude_code.sh -- so it never belonged in this lookup.)
#
# A *blank* value falls back to the default rather than through it: `or` is
# deliberate where `os.environ.get(name, default)` would not do. This listener
# binds 0.0.0.0 and starts and stops GPU containers via the Docker socket, and
# its systemd unit reads the gateway's whole .env, where a placeholder
# `LOCAL_API_KEY=` line is an ordinary thing to find. Read with a two-argument
# get(), that line is a real assignment of "", and an empty key used to mean
# "serve everyone" -- so a blank line in a config file would have turned request
# auth off with nothing in the log to say so. There is no way to disable auth
# now; point the key at a value both ends share.
LOCAL_API_KEY = os.environ.get("LOCAL_API_KEY", "").strip() or "freeinference_api"

_SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = _SCRIPT_DIR / "models.json"
# The H200 profile is NOT a copy kept next to this script: it is the very file
# the dedicated ops/h200_idle_proxy unit pins on its ExecStart line. A 4x-H200
# box has one DeepSeek deployment, not two, and both entry points must resolve
# to the same (container, backend_port, gpu_index) or they fight over the same
# container name — see ``_container_owner`` for what happens when they do.
# There used to be a local ``models.h200.json`` mirror of this file, kept in
# step only by a partial 5-key test assertion; #1187 had to apply one
# mem_fraction change to both by hand. One file, no drift.
H200_CONFIG_PATH = _SCRIPT_DIR.parent / "h200_idle_proxy" / "models.json"


def _detect_profile_config() -> Path:
    """Pick a hardware-specific models profile by inspecting the local GPUs.

    The same proxy code runs on machines with very different GPUs, and each
    machine should serve the model that fits it. We inspect ``nvidia-smi`` once
    at import and map the hardware to a profile JSON:

      * **4+ x H200**       -> ``../h200_idle_proxy/models.json``
                               (DeepSeek-V4-Flash-0731, TP=2 on 2,3)
      * **RTX (PRO) 6000**  -> ``models.rtx6000.json``  (Qwen3.6-35B)
      * anything else / no ``nvidia-smi`` → ``models.json`` (default fallback)

    Only consulted when ``MODELS_CONFIG`` is unset, so an explicit override
    always wins. A matched profile that is missing on disk falls back to the
    default rather than leaving the proxy with no backends.
    """
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        log.info("nvidia-smi unavailable — using default profile %s.", DEFAULT_CONFIG_PATH.name)
        return DEFAULT_CONFIG_PATH

    names = [n.strip() for n in result.stdout.strip().splitlines() if n.strip()]
    h200_count = sum("h200" in n.lower() for n in names)
    has_rtx6000 = any("6000" in n and "rtx" in n.lower() for n in names)

    chosen: Path | None = None
    if h200_count >= 4:
        chosen = H200_CONFIG_PATH
    elif has_rtx6000:
        chosen = _SCRIPT_DIR / "models.rtx6000.json"

    if chosen is not None and chosen.is_file():
        log.info("Detected GPUs %s — using profile %s.", names, chosen.name)
        return chosen
    if chosen is not None:
        log.warning(
            "Detected GPUs %s but profile %s is missing — falling back to %s.",
            names,
            chosen.name,
            DEFAULT_CONFIG_PATH.name,
        )
    else:
        log.info("GPUs %s match no hardware profile — using %s.", names, DEFAULT_CONFIG_PATH.name)
    return DEFAULT_CONFIG_PATH


# An explicit MODELS_CONFIG always wins; otherwise auto-select by hardware.
MODELS_CONFIG = os.environ.get("MODELS_CONFIG") or str(_detect_profile_config())


def _load_models_config() -> dict[str, dict[str, Any]]:
    p = Path(MODELS_CONFIG)
    if not p.exists():
        log.warning("Models config not found at %s — proxy will have no backends.", p)
        return {}
    with open(p) as f:
        cfg = _json.load(f)
    log.info("Loaded %d model(s) from %s", len(cfg), p)
    for name, mc in cfg.items():
        log.info(
            "  %s → container=%s  gpu=%s  port=%s",
            name,
            mc.get("container"),
            mc.get("gpu_index", "auto"),
            mc.get("backend_port"),
        )
    return cfg


MODELS_CONFIG_DATA = _load_models_config()


def _pick_free_gpu(exclude: set[str] | None = None) -> str:
    """Return the index of the GPU with the lowest memory utilization.

    Prefers GPUs with memory utilization < 20%; falls back to the least-used.
    """
    exclude = exclude or set()
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=index,memory.used,memory.total",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
    except FileNotFoundError:
        log.warning("nvidia-smi not found — defaulting to GPU 0.")
        return "0"
    best_idx = "0"
    best_usage = 1.0
    free_idx = "0"
    free_usage = 1.0
    for line in result.stdout.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != 3:
            continue
        idx, used, total = parts[0], float(parts[1]), float(parts[2])
        if idx in exclude:
            continue
        usage = used / total if total > 0 else 1.0
        log.info("GPU %s: %.0f / %.0f MiB (%.0f%%)", idx, used, total, usage * 100)
        if usage < best_usage:
            best_usage = usage
            best_idx = idx
        if usage < 0.20 and usage < free_usage:
            free_usage = usage
            free_idx = idx
    if free_usage < 1.0:
        log.info(
            "Auto-picked GPU %s (%.0f%% mem used — under 20%% threshold).",
            free_idx,
            free_usage * 100,
        )
        return free_idx
    log.warning(
        "No GPU under 20%% memory utilization — falling back to least-used GPU %s (%.0f%% mem used).",
        best_idx,
        best_usage * 100,
    )
    return best_idx


def _pick_free_gpus(count: int, exclude: set[str] | None = None) -> str:
    """Return a comma-joined list of the ``count`` least-used GPU indices.

    Used for backends sharded across several devices by tensor and/or pipeline
    parallelism. Picks greedily — least-used first, excluding each chosen
    device from the next pick — and returns a string like ``"0,1,2,3"`` suitable
    for a Docker ``--gpus device=...`` request.
    """
    exclude = set(exclude or set())
    chosen: list[str] = []
    for _ in range(max(1, count)):
        gpu = _pick_free_gpu(exclude=exclude)
        chosen.append(gpu)
        exclude.add(gpu)
    return ",".join(chosen)


def _docker_gpu_arg(gpu: str) -> str:
    """Return the ``--gpus`` value for a device list.

    Docker splits an *unquoted* ``device=0,2,3`` on commas into several separate
    GPU requests (``device=0``, then ``Count=2``, ``Count=3``), which the daemon
    rejects with "cannot set both Count and DeviceIDs on device request". Wrapping
    a multi-GPU list in double quotes makes docker parse it as a single request.
    A single device (no comma) needs no quoting.
    """
    return f'"device={gpu}"' if "," in gpu else f"device={gpu}"


def _launch_profile(config: dict[str, Any]) -> str:
    """Fingerprint the launch-affecting part of a per-model config.

    Stamped on the container at ``docker run`` and compared on adoption, so it
    must answer exactly one question: *would this container have been launched
    differently?* Hashing the whole config answers a different and more
    pessimistic question — "has anything in the file changed?" — which turns a
    purely cosmetic edit into a ``docker rm -f`` of a healthy, serving backend.
    See ``NON_LAUNCH_CONFIG_KEYS`` for what is excluded and why.
    """
    launch = {k: v for k, v in config.items() if k not in NON_LAUNCH_CONFIG_KEYS}
    return hashlib.sha256(_json.dumps(launch, sort_keys=True).encode()).hexdigest()[:16]


class ForeignContainerError(RuntimeError):
    """Raised when a *running* container of this name belongs to another proxy.

    Deliberately fails the request instead of taking the container over. The two
    alternatives are both worse: destroying it kills a backend another process is
    loading or streaming from, and adopting it puts a backend under two idle
    watchers that cannot see each other's activity, so whichever fires first
    stops it out from under the other's traffic.
    """


class BackendManager:
    """Manages the lifecycle of a single sglang Docker container."""

    def __init__(self, model_name: str, config: dict[str, Any]) -> None:
        self.model_name = model_name
        self.config = config
        self.container: str = config["container"]
        self.backend_port: int = int(config.get("backend_port", 18001))
        self._lock = threading.Lock()
        self._state: str = "stopped"
        self._last_activity: float = 0.0
        self._watcher_thread: threading.Thread | None = None
        self._ready_event = threading.Event()
        self._start_error: Exception | None = None
        # GPU this backend actually resolved to at launch. Recorded so other
        # auto-selecting backends can exclude it (config gpu_index is empty for
        # auto-selected models). Cleared when the container stops.
        self._current_gpu: str | None = None
        # Fingerprint of the config this manager was built from, stamped onto the
        # container so a later process can tell "the backend I asked for" from
        # "some other backend wearing the same name".
        self._profile = _launch_profile(config)
        # Id of the container this manager launched or adopted, so the identity it
        # believes in can be re-checked without a per-request docker call. See
        # `_disown_if_replaced`: the labels prove *who owns the name now*, and this
        # proves *whether it is still the container we became ready over*.
        self._container_id: str | None = None
        # Identity of a container this manager positively established was *not* the
        # one it became ready over, carried forward out of `_disown_if_replaced`.
        # Needed because the demotion there clears `_container_id`, and every
        # refusal on the start path is gated on a foreign owner *label*: a same-
        # `PROXY_OWNER` sibling or an unlabelled hand-restart reads as this proxy's
        # own, so without this the next request would `docker rm -f` — or silently
        # re-adopt — the very container the re-check had just disowned. See
        # `_disowned_container_error`.
        self._disowned_container_id: str | None = None
        self._disowned_reason: str | None = None
        # Last unrunnable-`docker inspect` reason already reported, so the
        # ownership check on the streaming warmup path says it once instead of on
        # every request (see `_log_inspect_failure`).
        self._inspect_failure: str | None = None
        # Consecutive failed starts, and the error that latched this backend off
        # once they reached MAX_START_FAILURES. Any successful start clears both.
        self._start_failures = 0
        self._giveup_error: RuntimeError | None = None
        # What `GET /health` answers from, refreshed once per idle-watcher tick and
        # read — never computed — by the handler. `_health_reason` is None while the
        # proxy's belief about this backend matches reality; the rest are the working
        # state behind it (see `_recheck_serving`). `_health_probed_ok` is the latch
        # that keeps a cold start from being read as a wedge: `_wait_healthy` promotes
        # to `ready` on `/v1/models`, which sglang answers as soon as uvicorn binds,
        # while the forward-progress probe keeps returning 503 until warmup finishes —
        # minutes, on the MoE models this proxy fronts. Until the probe has succeeded
        # once, a failure is "not up yet", not "wedged" — but only for
        # HEALTH_PROBE_WARMUP after `_health_grace_since`, or a backend adopted into
        # `ready` while already wedged would sit behind that excuse forever.
        self._health_reason: str | None = None
        self._health_detail: str | None = None
        self._health_since: float | None = None
        self._health_grace_since = time.monotonic()
        self._health_strikes = 0
        self._health_probed_ok = False

    @property
    def state(self) -> str:
        """Return the current backend lifecycle phase."""
        with self._lock:
            return self._state

    def touch(self) -> None:
        """Record activity to reset the idle timer."""
        with self._lock:
            self._last_activity = time.monotonic()

    def alive(self) -> bool:
        """Return True if the backend container is currently running."""
        return self._container_running()

    def mark_stopped(self) -> None:
        """Reset a ``ready`` backend to ``stopped`` so it relaunches on use.

        Called when the proxy discovers a backend died outside its control
        (crash, OOM, the sglang scheduler exiting on an internal error). Only
        a ``ready`` → ``stopped`` transition is performed so that concurrent
        callers racing on the same dead backend do not knock an in-flight
        restart (state ``starting``) back to ``stopped``.
        """
        with self._lock:
            self._reset_health_verdict()
            if self._state == "ready":
                self._state = "stopped"
                self._current_gpu = None
                self._container_id = None
                self._ready_event.clear()

    def _reset_health_verdict(self) -> None:
        """Forget everything ``/health`` reports about this backend.

        Caller must hold ``self._lock``.

        Called wherever this manager stops holding the container it formed the
        verdict over — a demotion, and the ``ready`` transition of a fresh start.
        The verdict describes one container's behaviour, so carrying it across a
        relaunch would report the corpse's wedge against its replacement, and the
        ``_health_probed_ok`` latch has to be re-earned or the replacement's own
        multi-minute warmup would read as a wedge on its third tick.

        Re-earning that latch is what restarts the warmup clock, so this is also
        where ``_health_grace_since`` is stamped: the excuse "it has never answered,
        so it must still be loading" is only good for HEALTH_PROBE_WARMUP from the
        moment this manager began believing in the container, and every call site
        here is exactly such a moment.
        """
        self._health_reason = None
        self._health_detail = None
        self._health_since = None
        self._health_grace_since = time.monotonic()
        self._health_strikes = 0
        self._health_probed_ok = False

    def health_report(self) -> dict[str, Any] | None:
        """Return why this backend is not serving, or None if nothing is wrong.

        The whole of what ``GET /health`` does per backend, and deliberately so:
        the gateway probes that endpoint on a timer with a two-second timeout
        (``timeout: 2`` in ``distributions/freeinference/config/routing.yaml``), and
        a handler that ran a ``docker inspect`` or an HTTP probe per request would
        score itself unhealthy on its own latency — most reliably during a wedge,
        when every other thread is parked in a 300s ``urlopen``. So this reads
        cached state under the lock (held for microseconds here, never across a
        docker or HTTP call) and computes nothing; the verdict is produced once per
        tick by ``_recheck_serving`` on the idle watcher, a thread that already
        wakes at that cadence and already pays a ``docker inspect``.

        None for every phase except ``ready``. A backend that is idle-stopped or
        cold-loading is not down — lazy start is the design, and a DeepSeek-class
        cold start runs 6-14 minutes with the warmup SSE stream depending on
        requests still arriving, so reporting it as unhealthy would deregister the
        replica for the entire load and there would be nothing left to finish it.
        """
        with self._lock:
            if self._state != "ready" or self._health_reason is None:
                return None
            since = self._health_since if self._health_since is not None else time.monotonic()
            return {
                "model": self.model_name,
                "container": self.container,
                "state": self._state,
                "reason": self._health_reason,
                "detail": self._health_detail,
                "unhealthy_for_seconds": round(time.monotonic() - since, 1),
            }

    def giveup_error(self) -> RuntimeError | None:
        """Return the error this backend was given up on, if it has been.

        Companion to ``contention_error`` and there for the same reason: the
        streaming path commits a ``200`` and a warmup banner *before* it starts
        anything, so a backend nobody is going to start any more would answer
        every stream with "the model is starting up…" and never start. The
        gateway would see 200s, never open a circuit, and never fail the model
        over to its remote route. Cheap enough to call per request — it reads
        state, and runs no docker command.
        """
        with self._lock:
            return self._giveup_error

    def _record_start_failure(self, exc: BaseException) -> None:
        """Count a failed start, latching the backend off at the limit.

        Caller must hold ``self._lock``.

        Contention does not count. A ``ForeignContainerError`` says another proxy
        or an operator holds the container name right now, which is a condition
        that ends when they let go; it is also already cheap to refuse (one
        ``docker inspect``, no GPU, no wait), so nothing here is protecting
        against it. Counting it would let a long benchmark run on a hand-started
        container latch a healthy model off until someone restarts the proxy.
        """
        if isinstance(exc, ForeignContainerError):
            return
        self._start_failures += 1
        if MAX_START_FAILURES <= 0 or self._start_failures < MAX_START_FAILURES:
            log.warning(
                "[%s] Start attempt %d failed: %s",
                self.model_name,
                self._start_failures,
                exc,
            )
            return
        self._giveup_error = RuntimeError(
            f"[{self.model_name}] Giving up on container {self.container}: "
            f"{self._start_failures} consecutive failed starts. The proxy will not "
            f"try again until it is restarted. Last error: {exc}"
        )
        log.error(
            "[%s] Container %s failed to start %d times in a row — giving up. "
            "No further start attempts until this proxy is restarted "
            "(fix the cause, then `systemctl restart` the unit). Last error: %s",
            self.model_name,
            self.container,
            self._start_failures,
            exc,
        )

    def ensure_running(self) -> None:
        """Start the container if needed and block until it is healthy.

        Raises without touching docker once the backend has been given up on
        after ``MAX_START_FAILURES`` consecutive failed starts.
        """
        should_start = False
        with self._lock:
            if self._state == "ready":
                self._last_activity = time.monotonic()
                return
            if self._giveup_error is not None:
                raise self._giveup_error
            if self._state == "starting":
                pass
            else:
                self._state = "starting"
                self._ready_event.clear()
                self._start_error = None
                should_start = True

        if should_start:
            try:
                if self._adopt_running_container():
                    log.info(
                        "[%s] Adopted already-running container %s (gpu=%s).",
                        self.model_name,
                        self.container,
                        self._current_gpu,
                    )
                else:
                    log.info("[%s] Starting container %s …", self.model_name, self.container)
                    self._start_container()
                self._wait_healthy()
                with self._lock:
                    self._state = "ready"
                    self._last_activity = time.monotonic()
                    # Consecutive failures only: a start that worked says the
                    # backend is startable, whatever went wrong before it.
                    self._start_failures = 0
                    # This is a different container from whatever the last verdict
                    # was about, including on the adoption path — an adopted backend
                    # is one this process has never watched.
                    self._reset_health_verdict()
                    if self._watcher_thread is None or not self._watcher_thread.is_alive():
                        self._watcher_thread = threading.Thread(
                            target=self._idle_watcher, daemon=True
                        )
                        self._watcher_thread.start()
                log.info("[%s] Container %s is ready.", self.model_name, self.container)
                self._ready_event.set()
            except Exception as exc:
                with self._lock:
                    self._state = "stopped"
                    self._current_gpu = None
                    self._start_error = exc
                    self._record_start_failure(exc)
                self._ready_event.set()
                raise
        else:
            self._ready_event.wait(timeout=HEALTH_TIMEOUT)
            with self._lock:
                if self._state == "ready":
                    self._last_activity = time.monotonic()
                    return
                if self._start_error is not None:
                    raise self._start_error
            raise RuntimeError(
                f"[{self.model_name}] Backend did not become healthy within {HEALTH_TIMEOUT}s"
            )

    def _resolve_gpu(self, replacing_gpu: str | None = None) -> str:
        """Return the ``--gpus`` device list this backend should launch on.

        ``replacing_gpu`` is the device list currently held by a still-running
        container of this name that the caller is about to replace (see
        ``_start_container``). It is used only where the choice would otherwise be
        auto-selected, and only when it still fits the requested shape.
        """
        # An explicit gpu_index pins the model to that device (lets several
        # models share one GPU); only auto-pick when it is unset.
        pinned = self.config.get("gpu_index")
        if pinned not in (None, ""):
            log.info("[%s] Using pinned GPU %s", self.model_name, pinned)
            self._current_gpu = str(pinned)
            return self._current_gpu
        # Colocation: if another backend sharing this model's colocate_group is
        # already starting or running, land on the same GPU it resolved to.
        # Whichever group member starts first auto-picks a free GPU; the rest
        # follow it onto that device (the inverse of the exclusion below). This
        # lets, e.g., a chat model and its embedding model share one GPU.
        group = self.config.get("colocate_group")
        if group:
            for mgr in _backends.values():
                if mgr is self:
                    continue
                if (
                    mgr.state in ("starting", "ready")
                    and mgr._current_gpu is not None
                    and mgr.config.get("colocate_group") == group
                ):
                    log.info(
                        "[%s] Colocating on GPU %s with %s (group %r)",
                        self.model_name,
                        mgr._current_gpu,
                        mgr.model_name,
                        group,
                    )
                    self._current_gpu = mgr._current_gpu
                    return self._current_gpu
        tp = int(self.config.get("tensor_parallel_size", 1))
        pp = int(self.config.get("pipeline_parallel_size", 1))
        # A model sharded by both tensor and pipeline parallelism needs one GPU
        # per (tp rank x pp stage).
        n_gpus = tp * pp
        # Replacing our own still-running container: stay on the device it already
        # holds instead of auto-picking, which would read that device as busy (by
        # the container being replaced) and move the backend somewhere else. Ranked
        # below the colocate group on purpose: a group anchor that has already
        # resolved a device is the placement the whole group must share, so
        # following it is right even when this backend's old container sat
        # elsewhere. Skipped when the device count no longer matches — a
        # tp/pp change needs a different number of GPUs, so re-select.
        if replacing_gpu and len(replacing_gpu.split(",")) == n_gpus:
            log.info(
                "[%s] Reusing GPU %s from the container being replaced.",
                self.model_name,
                replacing_gpu,
            )
            self._current_gpu = replacing_gpu
            return self._current_gpu
        # Exclude GPUs already claimed by other backends that are starting or
        # running. Auto-selected backends carry no gpu_index in their config, so
        # rely on the runtime GPU each one actually resolved to.
        # A tensor-parallel backend records several devices ("0,1,2,3"); expand
        # them so each is excluded individually.
        used_gpus: set[str] = set()
        for mgr in _backends.values():
            if mgr is self:
                continue
            if mgr.state in ("starting", "ready") and mgr._current_gpu is not None:
                used_gpus.update(str(mgr._current_gpu).split(","))
        log.info(
            "[%s] Auto-selecting %d GPU(s) (tp=%d, pp=%d, excluding %s)",
            self.model_name,
            n_gpus,
            tp,
            pp,
            sorted(used_gpus) if used_gpus else "none",
        )
        gpu = (
            _pick_free_gpus(n_gpus, exclude=used_gpus)
            if n_gpus > 1
            else _pick_free_gpu(exclude=used_gpus)
        )
        self._current_gpu = gpu
        return gpu

    def _docker_env_args(self) -> list[str]:
        """Build ``-e VAR=val`` flags for the ``docker run`` invocation.

        Hybrid Mamba models running MTP spec decoding with radix cache need the
        v2 speculative path (paired with ``--mamba-scheduler-strategy
        extra_buffer``); see ``_start_container``.
        """
        env: dict[str, str] = {}
        if self.config.get("mtp") and self.config.get("mamba"):
            env["SGLANG_ENABLE_SPEC_V2"] = "1"
        args: list[str] = []
        for key, val in env.items():
            args += ["-e", f"{key}={val}"]
        return args

    def _ownership_label_args(self) -> list[str]:
        """Build the ``--label`` flags that make this container's owner readable.

        Stamped by both engines' run commands. Without them the container name is
        the only ownership token there is, and it is not one — see the
        "Container ownership" block near the top of this module.
        """
        return [
            "--label",
            f"{OWNER_LABEL}={PROXY_OWNER}",
            "--label",
            f"{PROFILE_LABEL}={self._profile}",
        ]

    def _start_container(self) -> None:
        # Ownership first: everything below either destroys the container of this
        # name or binds its host port, so a *live* foreign container must stop us
        # before we touch the GPU or the filesystem. This is the early, cheap
        # refusal — it saves a several-hundred-GiB download that was going to be
        # thrown away — and NOT the one the removal below relies on. That one is
        # re-taken immediately before the removal, in `_clear_container_name`,
        # because a collision that appears while `_ensure_model_dir` runs would be
        # invisible to a check made here.
        #
        # Only checked when the label says foreign, and only when that container has
        # not finished, because nothing anywhere removes a foreign container:
        # `_stop_container` declines one too. An exited container of this name holds
        # no GPU, serves no traffic, and cannot be harmed by a `docker rm -f` —
        # refusing it would wedge the name permanently and 502 the model until an
        # operator removed the corpse by hand. That is reachable: `bench_decode.sh`
        # has the operator hand-start this exact container with `owner=manual`, and a
        # `docker stop` (rather than `docker rm`) afterwards leaves one behind. The
        # edge this guard exists for — a foreign backend eight minutes into a
        # fourteen-minute load, failing the health probe — is running, and is still
        # refused; so is one that is only `created`, which is a sibling's launch
        # between `docker run`'s create and its start (see `_RECLAIMABLE_STATUSES`).
        state = self._inspect_state()
        running = state.running
        owner = self._container_owner(state.labels)
        if owner is not None and (running or not self._is_reclaimable(state)):
            raise self._foreign_container_error(
                owner,
                "replace",
                state_note=(
                    "and running"
                    if running
                    else f"and in docker state {state.status!r}, so it has not finished"
                ),
            )
        # The label is not the only positive evidence available: a takeover the idle
        # watcher caught by container id reads as *ours* here, and would otherwise be
        # destroyed by the removal below. Refused early for the same reason the label
        # is — before the GPU is claimed and before a several-hundred-GiB download
        # that was going to be thrown away — and re-taken in
        # `_clear_container_name`, which is the check the removal actually rests on.
        disowned = self._disowned_container_error(state, "replace")
        if disowned is not None:
            raise disowned
        # A *running* container of this name that is ours is about to be replaced
        # — a changed profile hash, or one that never became healthy. Its device
        # request is the only reliable record of where this backend lives, and it
        # has to be read before the `docker rm -f` below: `_resolve_gpu`'s
        # auto-selection asks nvidia-smi for the least-used device, and nvidia-smi
        # reports the current one as busy *because this very container is still on
        # it*. Auto-selection would therefore relocate the replacement — off its
        # colocation partner, or onto a device an idle-stopped model is pinned to,
        # to OOM when that model wakes. Both real auto-selecting profiles
        # (models.json, models.rtx6000.json) omit `gpu_index` and share a
        # `colocate_group`, so this is the ordinary case, not an exotic one.
        #
        # Reading the GPU rather than removing the container first is deliberate:
        # `docker rm -f` sits immediately before `docker run` so the backend is
        # down for one docker call, not for however long `_ensure_model_dir` takes
        # to fetch a few hundred GiB — and destroying first would not even
        # guarantee the same device, since nvidia-smi accounting lags process
        # teardown. Relaunching onto the device just released is exactly what the
        # pinned-`gpu_index` path already does on every profile change.
        replacing_gpu = self._running_container_gpu() if running else None
        gpu = self._resolve_gpu(replacing_gpu=replacing_gpu)
        self._ensure_model_dir()
        engine = str(self.config.get("engine", "sglang")).lower()
        cmd = self._vllm_run_cmd(gpu) if engine == "vllm" else self._sglang_run_cmd(gpu)
        self._claim_container_name(cmd)

    def _claim_container_name(self, cmd: list[str]) -> None:
        """Free the container name and launch under it, or refuse to.

        The name is the one resource two proxies of the same config both need, and
        `docker run --name` is the only atomic claim on it available: names are
        unique, so a run that returns a name conflict proves someone else got
        there first. Every removal is therefore authorised by an ownership check
        one docker call old (``_clear_container_name``) and aimed at a container
        *id*, and a lost race is answered by taking the decision again rather than
        by insisting on the name.

        Bounded at two attempts on purpose. One retry covers the real case — a
        sibling that claimed the name during our download and has since exited —
        and a name that is taken twice over is a persistent collision, not a race
        to keep re-running weight downloads against.

        The retry is not immediate. Docker frees a name as the removal completes in
        the daemon, which can be after `docker rm -f` has already returned, so a
        conflict answered by an instant re-run can hit the very name release it is
        waiting for ("removal of container … is already in progress"). A short pause
        costs nothing next to a container start and takes that flake out of the one
        retry there is.

        A successful launch records the new container's id. ``docker run -d`` prints
        it on stdout, which is already captured, so the identity
        ``_disown_if_replaced`` re-checks later costs no extra daemon round trip.
        """
        attempts = 2
        removal_error: str | None = None
        for attempt in range(1, attempts + 1):
            if attempt > 1:
                time.sleep(_NAME_CONFLICT_RETRY_DELAY)
            removal_error = self._clear_container_name()
            log.info("Running: %s", " ".join(cmd))
            result = subprocess.run(cmd, check=False, capture_output=True, text=True)
            if result.returncode == 0:
                self._container_id = _launched_container_id(result.stdout)
                self._forget_disowned_container()
                return
            stderr = (result.stderr or "").strip()
            if not _is_name_conflict(stderr):
                # Not contention: a missing driver, an unavailable device, a bad
                # image tag. Logged because `check=True` used to swallow the
                # captured output into a CalledProcessError whose str() carries
                # only the exit status, leaving the operator with a bare number.
                log.error(
                    "[%s] docker run for %s failed (exit %s): %s",
                    self.model_name,
                    self.container,
                    result.returncode,
                    stderr or "(no output)",
                )
                # The same exception `check=True` used to raise, so callers see no
                # change: `ensure_running` stores it as `_start_error` and the
                # handler turns it into a 502.
                raise subprocess.CalledProcessError(
                    result.returncode, cmd, output=result.stdout, stderr=result.stderr
                )
            if removal_error:
                # Not contention at all: this proxy tried to free the name, docker
                # refused, and the container is still there holding it. Saying
                # "another process" here would be a claim the code can see is false.
                log.warning(
                    "[%s] Container name %s is still held after a removal this proxy could "
                    "not complete (attempt %d/%d). docker rm said: %s -- docker run said: %s",
                    self.model_name,
                    self.container,
                    attempt,
                    attempts,
                    removal_error,
                    stderr,
                )
            else:
                log.warning(
                    "[%s] Container name %s was claimed by another process while this "
                    "backend was being prepared (attempt %d/%d): %s",
                    self.model_name,
                    self.container,
                    attempt,
                    attempts,
                    stderr,
                )
        # Out of attempts. Name whoever holds it now, so the 502 the caller turns
        # this into carries the same diagnosis a collision seen up front would.
        state = self._inspect_state()
        owner = self._container_owner(state.labels)
        if owner is not None and state.running:
            raise self._foreign_container_error(owner, "replace")
        if state.exists and owner is None:
            # The name is held by a container this proxy reads as its *own* — either
            # one whose removal failed just now, or an unlabelled one (the upgrade
            # rule). Either way there is no sibling to blame, and the diagnosis the
            # operator needs is which container it is and what docker said about
            # removing it, not a guess about a second unit.
            raise ForeignContainerError(
                f"[{self.model_name}] Gave up starting container {self.container}: the name is "
                f"still held by container {state.container_id} (docker state "
                f"{state.status or 'unknown'!r}), which carries no foreign owner label and so "
                f"reads as this proxy's own ({PROXY_OWNER!r}), yet it is still there after "
                f"{attempts} attempts to remove it and launch. "
                + (
                    f"docker rm -f reported: {removal_error}. "
                    if removal_error
                    else "The removal reported success, so something recreated the name. "
                )
                + f"Remove it by hand with 'sudo docker rm -f {self.container}'. If it belongs to "
                f"another process after all, that process is running a build of this script from "
                f"before ownership labelling, or started the container without a "
                f"'--label {OWNER_LABEL}=<owner>' -- label it, or point one of the two at a "
                f"config whose 'container' and 'backend_port' do not collide."
            )
        raise ForeignContainerError(
            f"[{self.model_name}] Gave up starting container {self.container}: the name was "
            f"taken by another process on each of {attempts} attempts, so this proxy "
            f"({PROXY_OWNER!r}) never launched. Something else on this host is starting a "
            f"container of that name in a loop -- most likely a second unit running "
            f"ops/local_deployment_proxy/local_deployment_proxy.py with a different "
            f"MODELS_CONFIG. Point one of them at a config whose 'container' and "
            f"'backend_port' do not collide, or disable it."
        )

    def _clear_container_name(self) -> str | None:
        """Make the container name free to launch under, or refuse to touch it.

        Raises ``ForeignContainerError`` for a container of another owner that is
        either *live* or not yet started; removes one that is ours, unlabelled (the
        upgrade rule), or a *finished* container of any owner — nothing else in this
        module removes a foreign corpse, so declining that would wedge the name
        until an operator cleared it by hand.

        "Finished" is decided by docker's own state word and not by
        ``.State.Running``, which is also false for a container that is merely
        `created`: `docker run -d` reserves the name and writes the labels at create
        time and starts the container after, so a sibling's ordinary launch passes
        through a state that a running-only test reads as a corpse (see
        ``_RECLAIMABLE_STATUSES``). Our own container is removed whatever its state:
        refusing there would wedge our own name, and there is no other owner to
        take it from.

        Returns the daemon's message if a removal was attempted and failed, so the
        caller can say the name is still held by a container *this* proxy could not
        remove instead of blaming a sibling that does not exist. ``None`` when
        nothing needed removing or the removal succeeded.

        The removal is by container id rather than by name, which is what keeps
        this from being just a narrower version of the race it replaces: the
        judgement and the `docker rm -f` are one docker call apart, and should the
        container we judged be replaced even inside that window, the removal
        misses (docker answers "no such container") instead of destroying a
        stranger's brand-new backend.
        """
        state = self._inspect_state()
        if not state.exists:
            return None
        owner = self._container_owner(state.labels)
        if owner is not None and state.running:
            raise self._foreign_container_error(owner, "replace")
        if owner is not None and not self._is_reclaimable(state):
            raise self._foreign_container_error(
                owner,
                "replace",
                state_note=(
                    f"and in docker state {state.status!r}, which is not a container that has "
                    f"finished -- a foreign container is reclaimed only once it has exited, "
                    f"since 'docker run -d' reserves a name and stamps its labels before it "
                    f"starts, so removing this one would destroy a launch in progress"
                ),
            )
        # The reading the owner label cannot take, re-taken here rather than trusted
        # from `_start_container`: this is the check the `docker rm -f` below rests
        # on, and a container the idle watcher disowned by id reads as this proxy's
        # own at every one of the tests above. Removing it would destroy a running
        # backend this proxy has established it did not launch.
        disowned = self._disowned_container_error(state, "replace")
        if disowned is not None:
            raise disowned
        if owner is not None:
            log.warning(
                "[%s] Reclaiming %s container %s owned by %s: it holds no GPU and "
                "serves nothing, and nothing else here would ever remove it.",
                self.model_name,
                state.status or "exited",
                self.container,
                owner,
            )
        return self._remove_container(state, "clearing the name for a fresh launch")

    @staticmethod
    def _is_reclaimable(state: _ContainerState) -> bool:
        """Return True if this *foreign* container has finished and may be removed."""
        return not state.status or state.status in _RECLAIMABLE_STATUSES

    def _remove_container(self, state: _ContainerState, why: str) -> str | None:
        """``docker rm -f`` the container just judged removable; report a failure.

        Returns the daemon's message on failure, ``None`` on success. The result
        used to be discarded at both call sites, which made this the one error in
        the ownership guard that was swallowed rather than surfaced: `docker rm -f`
        failing while the container survives is an ordinary docker failure mode (an
        overlay2 or cgroup mount that is busy, a removal already in progress, a
        CUDA-wedged process in D state), and a wedged backend is exactly when this
        proxy is trying to replace one. Unreported, the start path then read its own
        undead container as contention and blamed a sibling unit, and the idle path
        reported GPUs freed that were still held.

        "No such container" is not a failure here, and is deliberately not reported
        as one: aiming the removal at an *id* is what makes a container replaced
        since the label read get missed instead of destroyed, so a miss is this
        design working. Whatever holds the name now is then reported by the `docker
        run` that finds it, on its own fresh ownership check.
        """
        result = subprocess.run(
            ["sudo", "docker", "rm", "-f", state.container_id],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return None
        stderr = (result.stderr or "").strip() or "(no output)"
        if _is_missing_container(stderr):
            log.info(
                "[%s] Container %s (%s) was already gone when the removal landed while %s.",
                self.model_name,
                state.container_id,
                self.container,
                why,
            )
            return None
        log.error(
            "[%s] docker rm -f %s (%s) failed while %s (exit %s): %s",
            self.model_name,
            state.container_id,
            self.container,
            why,
            result.returncode,
            stderr,
        )
        return stderr

    def _vllm_run_cmd(self, gpu: str) -> list[str]:
        """Build the ``docker run`` command for a vLLM backend.

        vLLM serves the OpenAI-compatible API on container port 8000 (sglang
        uses 8001); the health check and proxy address the backend through the
        host ``backend_port``, so the internal-port difference is transparent.

        Generation models enable prefix caching and prompt-token detail
        reporting so repeated prefixes are reused and surfaced to clients as
        ``usage.prompt_tokens_details.cached_tokens``. They use vLLM's default
        (bf16) KV cache: an FP8 KV cache (``--kv-cache-dtype fp8``) silently
        disables prefix-cache *hits* in vLLM 0.20, so it is opt-in only via
        ``kv_cache_dtype`` and trades away cache reporting. Embedding models run
        vLLM's pooling runner, which has no KV cache, so ``--kv-cache-dtype`` and
        the generation parsers are omitted for them.
        """
        tp = int(self.config.get("tensor_parallel_size", 1))
        pp = int(self.config.get("pipeline_parallel_size", 1))
        # A backend spans several GPUs when sharded by tensor parallelism,
        # pipeline parallelism, or both; NCCL then needs host IPC for fast
        # peer-to-peer transport (the --ipc=host below).
        multi_gpu = tp * pp > 1
        cmd = [
            "sudo",
            "docker",
            "run",
            "-d",
            "--name",
            self.container,
            *self._ownership_label_args(),
            "--gpus",
            _docker_gpu_arg(gpu),
            "--shm-size",
            "16g",
            *(["--ipc=host"] if multi_gpu else []),
            "-p",
            f"{self.backend_port}:8000",
            "-v",
            f"{self.config['model_dir']}:/model:ro",
            "vllm/vllm-openai:latest",
            "--model",
            "/model",
            "--served-model-name",
            self.config.get("served_name", self.model_name),
            "--port",
            "8000",
            "--max-model-len",
            str(self.config.get("max_model_len", 131072)),
            "--gpu-memory-utilization",
            str(self.config.get("mem_fraction", "0.90")),
            "--tensor-parallel-size",
            str(tp),
            *(["--pipeline-parallel-size", str(pp)] if pp > 1 else []),
        ]
        if self.config.get("is_embedding"):
            # vLLM >= 0.20 selects the embedding runner with --runner pooling
            # (the older --task embed was removed). Pooling models keep no KV
            # cache, so --kv-cache-dtype must not be passed.
            cmd += ["--runner", "pooling"]
        else:
            # Reuse repeated prefixes (system prompts, few-shot blocks) and report
            # the hits as usage.prompt_tokens_details.cached_tokens. Both flags are
            # required: --enable-prompt-tokens-details alone reports nothing, and an
            # FP8 KV cache yields zero prefix-cache hits (so cached_tokens stays 0).
            cmd += ["--enable-prefix-caching", "--enable-prompt-tokens-details"]
            # Opt-in FP8 KV cache only; defaulting to it would disable cache hits.
            kv_dtype = self.config.get("kv_cache_dtype")
            if kv_dtype:
                cmd += ["--kv-cache-dtype", str(kv_dtype)]
            # Reasoning models emit a thinking block; the parser splits it into
            # message.reasoning_content so it does not leak into content (and is
            # excluded from tool-call arguments).
            rp = self.config.get("reasoning_parser")
            if rp:
                cmd += ["--reasoning-parser", str(rp)]
            # vLLM names some tool-call parsers differently from sglang; allow a
            # vLLM-specific override, falling back to the shared parser name.
            tcp = self.config.get("vllm_tool_call_parser", self.config.get("tool_call_parser"))
            if tcp:
                cmd += ["--enable-auto-tool-choice", "--tool-call-parser", str(tcp)]
        return cmd

    def _hicache_args(self) -> list[str]:
        """Build the HiCache (host-DRAM KV tier) flags for an sglang backend.

        HiCache adds an L2 prefix-cache tier in host DRAM: prefixes evicted from
        HBM are restored over PCIe instead of recomputed, which cuts TTFT on
        cache hits. It does *not* enlarge the running-request KV budget or the
        usable context length.

        ``hicache_size`` is in GB and is what sglang allocates **per scheduler
        process** -- i.e. per TP rank per PP stage, since each rank builds its
        own host pool. A box therefore pins roughly ``tp * pp * hicache_size``
        GB of unswappable memory, and sglang refuses to start a rank whose share
        exceeds free RAM minus a 10 GB reserve. Size it against ``free -g``
        divided by the rank count, never against total RAM.

        Off unless the model config opts in. Never emitted for embedding models:
        those run with ``--disable-radix-cache``, which sglang rejects alongside
        ``--enable-hierarchical-cache``.
        """
        size = self.config.get("hicache_size")
        ratio = self.config.get("hicache_ratio")
        policy = self.config.get("hicache_write_policy")
        io_backend = self.config.get("hicache_io_backend")
        mem_layout = self.config.get("hicache_mem_layout")
        if not any((size, ratio, policy, io_backend, mem_layout)):
            return []

        args = ["--enable-hierarchical-cache"]
        # An absolute size overrides the ratio inside sglang; pass only one so the
        # launch command says what the pool will actually be.
        if size:
            args += ["--hicache-size", str(size)]
        elif ratio:
            args += ["--hicache-ratio", str(ratio)]
        if policy:
            args += ["--hicache-write-policy", str(policy)]
        if io_backend:
            args += ["--hicache-io-backend", str(io_backend)]
        if mem_layout:
            args += ["--hicache-mem-layout", str(mem_layout)]
        return args

    def _sglang_run_cmd(self, gpu: str) -> list[str]:
        """Build the ``docker run`` command for an sglang backend."""
        tp = int(self.config.get("tensor_parallel_size", 1))
        pp = int(self.config.get("pipeline_parallel_size", 1))
        # A backend spans several GPUs when sharded by tensor parallelism,
        # pipeline parallelism, or both; NCCL then needs host IPC for fast
        # peer-to-peer transport (the --ipc=host below).
        multi_gpu = tp * pp > 1
        cmd = [
            "sudo",
            "docker",
            "run",
            "-d",
            "--name",
            self.container,
            *self._ownership_label_args(),
            "--gpus",
            _docker_gpu_arg(gpu),
            "--shm-size",
            "16g",
            *(["--ipc=host"] if multi_gpu else []),
            "-p",
            f"{self.backend_port}:8001",
            "-v",
            f"{self.config['model_dir']}:/model:ro",
            # Persist sglang's DeepGEMM/JIT kernel cache across container restarts.
            # Cold-compiling it costs several minutes on DeepSeek-V4 (~680s to ready
            # vs ~370s warm), which can outrun the health-check budget after an
            # idle-timeout teardown. Keep the dir node-local -- a cache shared over
            # NFS between boxes would have them racing on the same files.
            *(
                ["-v", f"{self.config['cache_dir']}:/root/.cache"]
                if self.config.get("cache_dir")
                else []
            ),
            *self._docker_env_args(),
            # `or`, not a dict default: a key present but explicitly "" or null would
            # otherwise become the literal image reference "" / "None" and fail the run.
            str(self.config.get("sglang_image") or "lmsysorg/sglang:latest"),
            "python3",
            "-m",
            "sglang.launch_server",
            "--model-path",
            "/model",
            "--served-model-name",
            self.config.get("served_name", self.model_name),
            "--host",
            "0.0.0.0",
            "--port",
            "8001",
            "--context-length",
            str(self.config.get("max_model_len", 131072)),
            "--mem-fraction-static",
            str(self.config.get("mem_fraction", "0.90")),
            "--tp",
            str(tp),
            *(["--pp-size", str(pp)] if pp > 1 else []),
        ]
        # MoE runner backend override (e.g. "marlin"). Required for NVFP4 / FP4-expert
        # models on pre-Blackwell (SM90, e.g. H200) GPUs, where the default "triton"
        # MoE runner asserts "Hidden size mismatch" on the packed FP4 expert weights.
        moe_backend = self.config.get("moe_runner_backend")
        if moe_backend:
            cmd += ["--moe-runner-backend", str(moe_backend)]
        # sglang's startup warmup request runs after the scheduler is up and can add
        # minutes on a large MoE model. The proxy's own health check already gates
        # readiness, so skipping it keeps slow models inside HEALTH_TIMEOUT.
        if self.config.get("skip_server_warmup"):
            cmd += ["--skip-server-warmup"]
        if self.config.get("is_embedding"):
            # Embedding models run sglang in encode-only mode; tool-call parsing
            # and chat-completion endpoints are irrelevant for them.
            cmd += ["--is-embedding"]
            attn = self.config.get("attention_backend")
            if attn:
                cmd += ["--attention-backend", attn]
            if self.config.get("disable_radix_cache"):
                cmd += ["--disable-radix-cache"]
        else:
            # Without this flag sglang omits prompt_tokens_details.cached_tokens
            # from the usage block, so prefix-cache hits never surface to clients.
            cmd += ["--enable-cache-report"]
            cmd += self._hicache_args()
            tcp = self.config.get("tool_call_parser")
            if tcp:
                cmd += ["--tool-call-parser", tcp]
            # Split the model's thinking block into reasoning_content (see the
            # matching note in _vllm_run_cmd).
            rp = self.config.get("reasoning_parser")
            if rp:
                cmd += ["--reasoning-parser", str(rp)]
            # Multi-Token Prediction (MTP) speculative decoding. Models that ship
            # native MTP layers (e.g. Qwen3.6 MoE, DeepSeek V3) use sglang's
            # NEXTN algorithm with the in-checkpoint MTP module, so no separate
            # draft model path is needed. The step/topk/draft-token counts are
            # tunable; the defaults suit a single MTP layer (one extra token).
            if self.config.get("mtp"):
                algo = str(self.config.get("speculative_algorithm", "NEXTN"))
                cmd += ["--speculative-algorithm", algo]
                # DSpark (DeepSeek-V4-Flash-0731) carries its own draft geometry in the
                # checkpoint: sglang reads dspark_block_size (gamma) and derives the
                # verify window as gamma + 1. The single-MTP-layer defaults below would
                # override that -- --speculative-num-draft-tokens 2 collapses a 5-token
                # DSpark block to 1 and gives up most of the speedup -- so pass only the
                # knobs that were set explicitly and let sglang infer the rest.
                if algo.upper() == "DSPARK":
                    for key, flag in (
                        ("speculative_num_steps", "--speculative-num-steps"),
                        ("speculative_eagle_topk", "--speculative-eagle-topk"),
                        ("speculative_num_draft_tokens", "--speculative-num-draft-tokens"),
                        ("speculative_dspark_block_size", "--speculative-dspark-block-size"),
                    ):
                        value = self.config.get(key)
                        if value is not None:
                            cmd += [flag, str(value)]
                else:
                    cmd += [
                        "--speculative-num-steps",
                        str(self.config.get("speculative_num_steps", 1)),
                        "--speculative-eagle-topk",
                        str(self.config.get("speculative_eagle_topk", 1)),
                        "--speculative-num-draft-tokens",
                        str(self.config.get("speculative_num_draft_tokens", 2)),
                    ]
                # Hybrid Mamba/linear-attention models (Qwen3.5/3.6 MoE) reject
                # spec decoding alongside radix cache unless the Mamba scheduler
                # reserves extra cache buffers and the v2 spec path is enabled
                # (SGLANG_ENABLE_SPEC_V2 is set in _docker_env_args). Set
                # "mamba": true on such models; harmless to omit otherwise.
                if self.config.get("mamba"):
                    cmd += [
                        "--mamba-scheduler-strategy",
                        str(self.config.get("mamba_scheduler_strategy", "extra_buffer")),
                    ]
        return cmd

    def _ensure_model_dir(self) -> None:
        model_dir = Path(self.config["model_dir"])
        config_path = model_dir / "config.json"
        sentinel_path = model_dir / ".download_complete"
        repo_id = self.config.get("hf_repo")

        # Skip when weights are present *and* either there is no managed download
        # (a manual install) or our completion sentinel proves the prior download
        # finished. An interrupted download can leave config.json behind without
        # the sentinel; in that case we re-run snapshot_download, which only
        # fetches missing/changed files, rather than trusting a partial directory.
        if config_path.is_file() and (not repo_id or sentinel_path.is_file()):
            return

        if not repo_id:
            raise RuntimeError(f"[{self.model_name}] model_dir missing config.json: {model_dir}")

        model_dir.mkdir(parents=True, exist_ok=True)
        revision = self.config.get("hf_revision")
        ignore_patterns = self.config.get("hf_ignore_patterns")
        if isinstance(ignore_patterns, str):
            # A single glob may be given as a bare string; wrap it so it is not
            # iterated character-by-character.
            ignore_patterns = [ignore_patterns]
        elif ignore_patterns:
            ignore_patterns = list(ignore_patterns)
        else:
            ignore_patterns = None
        log.info(
            "[%s] Downloading %s from Hugging Face to %s …", self.model_name, repo_id, model_dir
        )
        try:
            from huggingface_hub import snapshot_download

            snapshot_download(
                repo_id=str(repo_id),
                revision=str(revision) if revision else None,
                local_dir=str(model_dir),
                ignore_patterns=ignore_patterns,
            )
        except Exception as exc:
            raise RuntimeError(
                f"[{self.model_name}] failed to download Hugging Face model {repo_id}: {exc}"
            ) from exc

        if not config_path.is_file():
            raise RuntimeError(
                f"[{self.model_name}] Hugging Face download completed without config.json: {model_dir}"
            )
        # Mark the download complete so an interrupted retry is not mistaken for
        # a finished install on the next start.
        sentinel_path.touch()
        log.info("[%s] Downloaded %s.", self.model_name, repo_id)

    def _stop_container(self) -> None:
        # The idle watcher measures *this* process's last activity, which says
        # nothing about traffic another proxy is putting through a container of
        # the same name. Dropping local state without removing the container is
        # the safe outcome: the owner's own watcher will reclaim its GPUs.
        #
        # Unlike `_start_container` this is *not* gated on liveness, and does not
        # need to be: this path only ever frees GPUs, an exited container holds
        # none, and the start path now reclaims a foreign corpse of this name. So
        # declining unconditionally here strands nothing.
        #
        # The removal is aimed at the id this inspect returned, for the same reason
        # the start path does it: the idle timer can fire on a container that died
        # minutes ago and has since been replaced by another proxy reclaiming the
        # corpse, and `docker rm -f <name>` would then destroy that proxy's live
        # backend on the strength of a label read that never saw it.
        state = self._inspect_state()
        owner = self._container_owner(state.labels)
        if owner is not None:
            log.warning(
                "[%s] Not removing container %s: it is owned by %s, not by this proxy (%s). "
                "Releasing local state only.",
                self.model_name,
                self.container,
                owner,
                PROXY_OWNER,
            )
            with self._lock:
                self._state = "stopped"
                self._current_gpu = None
                self._container_id = None
            return
        log.info("[%s] Stopping container %s …", self.model_name, self.container)
        removal_error = (
            self._remove_container(state, "releasing an idle backend") if state.exists else None
        )
        with self._lock:
            self._state = "stopped"
            self._current_gpu = None
            # "Stopped" must mean "this manager holds no container", or a later
            # identity check could compare against an id from a previous life.
            self._container_id = None
        if removal_error:
            # Local state is released either way — the alternative is a backend this
            # proxy believes is ready and cannot reach. But the GPUs are *not* back,
            # so say so rather than logging "stopped" over a container that is still
            # running: the next start of any model here will auto-select against a
            # device that is still busy.
            log.warning(
                "[%s] Container %s was NOT removed, so its GPUs are still held; local state "
                "released anyway. Remove it by hand with 'sudo docker rm -f %s'.",
                self.model_name,
                self.container,
                self.container,
            )
            return
        log.info("[%s] Container %s stopped.", self.model_name, self.container)

    def _inspect_state(self) -> _ContainerState:
        """Return identity, liveness, state word and labels for this container name.

        One ``docker inspect`` answers all of them because every decision here needs
        more than one: a foreign container only matters while it is alive, a live
        container's owner decides whether we may touch it, a container that is not
        alive is only reclaimable if it has *finished* rather than not started yet
        (``status``), and the id is what any removal is aimed at, so it has to
        describe the same container the labels came from. Asking separately also
        left a window in which the container could stop, or be replaced, between
        two answers.

        Fails towards ``_ABSENT_CONTAINER`` — absent container, unreadable daemon,
        unparseable output. That reads as "nothing to remove, nobody to refuse":
        an empty label map means "mine" (see ``_container_owner``), which is both
        the pre-labelling behaviour and the safe direction, since a docker hiccup
        cannot invent a collision that wedges a model. A parseable answer, though,
        means the container exists, so an id that cannot be read falls back to the
        name rather than to "absent" — otherwise a removal that is genuinely due
        would be skipped and the name would stay taken.

        Those failures are silent because ``docker inspect`` exits non-zero with
        the same status for "no such object" as for "cannot connect to the daemon",
        and the first is the ordinary cold-start case on every single start — so a
        warning here would be noise on the happy path. Only a docker command that
        cannot be executed at all raises, and ``contention_error`` reports that.
        """
        result = subprocess.run(
            ["sudo", "docker", "inspect", "-f", _INSPECT_STATE_FORMAT, self.container],
            capture_output=True,
            text=True,
            check=False,
        )
        # Missing container (non-zero exit) or unparseable output.
        if result.returncode != 0:
            return _ABSENT_CONTAINER
        try:
            payload = _json.loads(result.stdout.strip() or "null")
        except ValueError:
            return _ABSENT_CONTAINER
        if not isinstance(payload, dict):
            return _ABSENT_CONTAINER
        labels = payload.get("labels")
        if not isinstance(labels, dict):
            labels = {}
        container_id = payload.get("id")
        if not isinstance(container_id, str) or not container_id:
            # A parseable answer means the container exists, so an unreadable id
            # must not read as "absent" — that would skip a removal that is
            # genuinely due and leave the name taken. Degrade to the name, which
            # is what this code did before ids were read at all.
            container_id = self.container
        status = payload.get("status")
        if not isinstance(status, str):
            # Same direction as the id fallback: an unreadable status degrades to
            # the empty string, which `_is_reclaimable` reads as the pre-narrowing
            # rule rather than as grounds to refuse and wedge the name.
            status = ""
        return _ContainerState(
            exists=True,
            container_id=container_id,
            running=payload.get("running") is True,
            status=status.strip().lower(),
            labels={str(k): str(v) for k, v in labels.items()},
        )

    def _inspect_container(self) -> tuple[bool, dict[str, str]]:
        """Return ``(running, labels)`` for the container of this name.

        The narrow view, for callers that need nothing else: a bare liveness probe
        (``_container_running``) and an ownership question asked without a state to
        hand (``_container_owner``). Anything that removes a container, or that has
        to remember *which* container it decided about, goes through
        ``_inspect_state`` for the id — the same one inspect either way.
        """
        state = self._inspect_state()
        return state.running, state.labels

    def _container_running(self) -> bool:
        """Return True while the backend container is still up.

        A crashed container (e.g. sglang OOM on startup) exits within seconds;
        without this check the health loop would keep polling a dead backend
        until ``HEALTH_TIMEOUT`` elapses, making the client request appear to
        hang forever.
        """
        return self._inspect_container()[0]

    def _container_owner(self, labels: dict[str, str] | None = None) -> str | None:
        """Return the owner of the container of this name, or None if it is ours.

        Pass ``labels`` to reuse an inspect the caller has already paid for.

        UPGRADE RULE — an *absent* owner label means "started by a build of this
        script from before labelling, therefore mine". Do not invert this. Every
        container running right now is unlabelled, so reading absent-label as
        "foreign" would make the idle watcher refuse to stop any of them and the
        GPUs would never be reclaimed after the idle timeout. With this rule an
        already-running deployment behaves exactly as it does today until its
        next cold start, which labels it.
        """
        if labels is None:
            labels = self._inspect_container()[1]
        owner = labels.get(OWNER_LABEL)
        if owner is None or owner == PROXY_OWNER:
            return None
        return owner

    def _foreign_container_error(
        self, owner: str, action: str, state_note: str = "and running"
    ) -> ForeignContainerError:
        """Build the error raised when another proxy owns this container name.

        ``state_note`` says *why* that container is untouchable, because "running"
        is not the only reason: one that is merely `created` has not started yet and
        is a launch in progress, not a corpse to reclaim. Getting this wrong in the
        text would send an operator looking for a live backend that is not there.

        Kept to plain ASCII on purpose. This message reaches clients through
        ``BaseHTTPRequestHandler.send_error``, which puts it in the HTTP status
        line and encodes that latin-1: one em dash there raises
        ``UnicodeEncodeError`` mid-response and the client sees a dropped
        connection instead of the diagnosis.
        """
        return ForeignContainerError(
            f"[{self.model_name}] Refusing to {action} container {self.container}: it is "
            f"owned by {owner!r} {state_note}, not by this proxy ({PROXY_OWNER!r}). Two "
            f"proxies have resolved the same container name, most likely two units running "
            f"ops/local_deployment_proxy/local_deployment_proxy.py with different "
            f"MODELS_CONFIG files on one host. Point one of them at a config whose "
            f"'container' and 'backend_port' do not collide, or disable it. If that owner "
            f"is instead a hand-started identity (a benchmark container) or this same unit "
            f"before a LISTEN_PORT change, remove the container with "
            f"'sudo docker rm -f {self.container}' or set PROXY_OWNER to {owner!r}."
        )

    def _disowned_container_error(
        self, state: _ContainerState, action: str
    ) -> ForeignContainerError | None:
        """Refuse to destroy or re-adopt the running container this manager disowned.

        ``_disown_if_replaced`` establishes, from container ids, that the name no
        longer resolves to the container this manager became ready over — and it
        does so in the one case the owner label cannot see, where the replacement
        reads as *ours* (an unlabelled hand-restart, or a second unit given the same
        ``PROXY_OWNER``). That finding has to outlive the demotion, because nothing
        downstream can re-derive it: ``mark_stopped`` clears ``_container_id``, and
        every refusal on the start path is gated on ``owner is not None``. Left to
        itself the next request would either ``docker rm -f`` a running container
        this proxy has positively established it did not launch — which is worse
        than the wrong-model forwarding the re-check exists to stop, and is what
        ``_clear_container_name`` would do, since an absent owner label reads as ours
        — or re-adopt it and go straight back to ``ready`` over it, at which point
        the ids agree again and the re-check can never fire a second time. So the
        evidence is kept, and it refuses both.

        Gated on ``running`` on purpose. Once that container exits it holds no GPU
        and serves nothing, and the start path's reclaim rule applies as usual —
        otherwise this guard would wedge the name until an operator cleared it by
        hand, which is the failure mode ``_clear_container_name`` already refuses to
        create for a foreign corpse.

        Plain ASCII, like ``_foreign_container_error`` and for the same reason: this
        text reaches clients through ``send_error``, which puts it in the HTTP status
        line and encodes that latin-1.
        """
        disowned = self._disowned_container_id
        if not disowned or not state.running or state.container_id != disowned:
            return None
        return ForeignContainerError(
            f"[{self.model_name}] Refusing to {action} container {self.container}: "
            f"{self._disowned_reason or 'this proxy disowned the container of that name'}, and "
            f"that container is still running. This proxy read the takeover itself, on the idle "
            f"watcher's ownership re-check, and released its local state without touching the "
            f"container; removing it now would destroy a backend this proxy has established it "
            f"did not launch (possibly mid-load or mid-stream), and adopting it would resume "
            f"serving whatever model it runs under this model's name. It carries no foreign "
            f"owner label, so it was either started by hand or by a second unit given this "
            f"proxy's own identity ({PROXY_OWNER!r}) -- 'sudo docker inspect {self.container}' "
            f"says which. If it should not be there, 'sudo docker rm -f {self.container}' and "
            f"the next request launches a fresh one. If it is the backend this host should "
            f"serve, restart this proxy so it adopts that container deliberately. To keep two "
            f"units apart for good, point one at a config whose 'container' and 'backend_port' "
            f"do not collide, or give it its own PROXY_OWNER."
        )

    def _forget_disowned_container(self) -> None:
        """Drop the disowned-container evidence once this manager holds a backend again.

        Called on a successful launch and on a successful adoption, both of which
        end with the name resolving to a container this manager owns — so the
        finding is spent. Keeping it would be harmless (ids are unique and never
        reused, and the check is gated on that id still running) but it would leave
        the manager carrying a claim about a container that no longer holds its
        name, which is the sort of stale state this whole guard exists to remove.
        """
        self._disowned_container_id = None
        self._disowned_reason = None

    def _log_inspect_failure(self, exc: BaseException) -> None:
        """Report an ownership inspect that could not be run at all.

        At ``warning`` because this module configures the root handler at ``INFO``
        (see ``logging.basicConfig`` at the top) and exposes no level knob, so a
        ``debug`` record here would be dropped before it reached the journal — the
        operator this exists for would see exactly nothing.

        Latched on the reason instead, because both callers repeat:
        ``contention_error`` runs on *every* streaming request until the backend is
        ready, and ``_disown_if_replaced`` on every idle-watcher tick while it is.
        The causes are not all transient either — a proxy that cannot execute ``sudo
        docker`` at all answers "no contention" for every request it will ever serve,
        which at one line per request or per tick would bury the journal. Cleared by
        the next ownership read whose inspect succeeds, so a genuinely intermittent
        failure is reported each time it recurs.
        """
        reason = f"{type(exc).__name__}: {exc}"
        if reason == self._inspect_failure:
            return
        self._inspect_failure = reason
        log.warning(
            "[%s] Ownership inspect of %s could not run (%s); assuming no contention. "
            "A foreign container of this name would go unnoticed until this is fixed.",
            self.model_name,
            self.container,
            reason,
        )

    def contention_error(self) -> ForeignContainerError | None:
        """Return the diagnosis if an untouchable foreign container holds this name.

        Exists for the streaming warmup path, which commits ``200`` plus a "the
        model is starting up" banner *before* ``ensure_running`` runs, on a
        background thread that can only log what it raises. A
        ``ForeignContainerError`` there reaches the journal and nothing else, so a
        contended proxy would answer every streaming request success-shaped
        forever: the gateway sees 200s, never opens a circuit, and never fails
        over — the exact silent failure the labels were added to end. Calling this
        before the response line is written keeps the 502 available.

        Never raises. A ``docker inspect`` that cannot even be executed answers
        "no contention", so a transient docker hiccup cannot turn every streaming
        request into a 502. The reason is logged (see
        ``_log_inspect_failure``): the return value cannot distinguish "nobody
        else owns this" from "the docker CLI is not installed" or "this user
        cannot run sudo", and an operator chasing a proxy that never starts a
        backend needs to see which it was.

        Reports exactly what the start path will refuse, which is a foreign
        container that has not finished — not only a running one. A foreign
        container left in `created` (its launcher died between docker's create and
        its start) is refused by ``_start_container`` on the background thread, so
        answering "no contention" for it would restore the silent-success-forever
        shape this method exists to prevent.
        """
        try:
            state = self._inspect_state()
        except Exception as exc:  # docker unreachable: not evidence of a collision
            self._log_inspect_failure(exc)
            return None
        self._inspect_failure = None
        owner = self._container_owner(state.labels)
        if owner is None:
            # No foreign label, but the start path has a second refusal that does not
            # need one: a container the idle watcher disowned by id. Reporting it here
            # too is what keeps this method's contract — "exactly what the start path
            # will refuse" — from going stale, and without it a warmup stream would
            # commit its 200 and banner and then discover the refusal on a background
            # thread that can only log, which is the silent-success shape this exists
            # to prevent.
            return self._disowned_container_error(state, "adopt")
        if not state.running and self._is_reclaimable(state):
            # A foreign corpse is reclaimed rather than refused, so it is not a
            # collision to report — the start path will remove it and launch.
            return None
        return self._foreign_container_error(
            owner,
            "adopt",
            state_note=(
                "and running"
                if state.running
                else f"and in docker state {state.status!r}, so it has not finished"
            ),
        )

    def _stale_ownership_reason(self, state: _ContainerState) -> str | None:
        """Say why the container of this name is no longer the one we became ready over.

        Answers on *positive evidence only*. An inspect that reports no container —
        which is also what an unreachable daemon and an unparseable answer degrade
        to (see ``_inspect_state``) — returns None, so a docker hiccup can never
        demote a warm backend. A container that has genuinely gone away is already
        reconciled reactively, by the ``URLError`` branch of ``_forward_with_body``:
        the port stops answering, ``alive()`` says the container is gone, and the
        request path restarts it. What that branch cannot see is the case here,
        because the port *does* answer — it is another proxy's backend answering.

        Two disagreements count, and they catch different things:

        * a **foreign owner label**. Container names are unique, so a foreign
          container holding this name proves ours is no longer under it: whatever
          this manager's ``ready`` state refers to, it is not this container. There
          is no false positive to worry about — our own container cannot be wearing
          a sibling's owner label.
        * a **different container id**, when one was recorded. This is what catches
          a replacement that reads as *ours*: an operator who hand-restarts the
          backend leaves an unlabelled container, which the upgrade rule in
          ``_container_owner`` deliberately reads as this proxy's own, and two units
          can also be given the same ``PROXY_OWNER``. Same name, same port, possibly
          a different profile — and nothing in the labels to say so.

        The two do *not* end the same way, and the difference matters enough to
        state. A foreign label is re-read by every guard on the start path, so the
        next request is refused there and 502s naming both owners. An id mismatch is
        not: it fires precisely when the label reads as ours, so there is no second
        owner to name and nothing downstream would refuse anything — the finding is
        instead carried out of ``_disown_if_replaced`` in
        ``_disowned_container_id``, and the start path refuses on *that*. See
        ``_disowned_container_error`` for what would happen otherwise.

        Only compared when both ids are known, so a launch whose id could not be
        read (``_launched_container_id``) degrades to the owner check instead of
        mismatching against the empty string every poll.
        """
        if not state.exists:
            return None
        owner = self._container_owner(state.labels)
        if owner is not None:
            return (
                f"the name is now held by container {state.container_id} owned by {owner!r}, "
                f"not by this proxy ({PROXY_OWNER!r})"
            )
        launched = self._container_id
        if launched and state.container_id and state.container_id != launched:
            return (
                f"the name is now held by container {state.container_id}, not by "
                f"{launched}, the container this proxy became ready over"
            )
        return None

    def _watch_inspect(self) -> _ContainerState | None:
        """Read identity, liveness and labels once for a whole idle-watcher tick.

        Both re-checks the watcher runs — the ownership one below and the
        "is this backend still serving?" one in ``_recheck_serving`` — ask about
        the same container at the same instant, so they share one ``docker
        inspect``. A second round trip per tick would buy nothing and this loop's
        cost is precisely what two earlier review rounds on this code pushed back
        on.

        ``None`` means the inspect could not be *run* at all, which is evidence of
        nothing: neither re-check may act on it. Note that this is narrower than
        "no answer" — ``_inspect_state`` deliberately degrades an unreachable
        daemon to ``_ABSENT_CONTAINER``, so an absent reading still arrives here as
        a state, and callers have to decide for themselves whether absence is
        evidence (see ``_recheck_serving``, where it is not).
        """
        try:
            state = self._inspect_state()
        except Exception as exc:  # docker unreachable: not evidence of a takeover
            self._log_inspect_failure(exc)
            return None
        self._inspect_failure = None
        return state

    def _probe_forward_progress(self) -> bool | None:
        """Ask the backend whether it has produced anything recently.

        ``True``/``False``, or ``None`` when this backend has no such endpoint — a
        vLLM or Ollama container answers 404/405 here, and "the probe does not
        apply" must not read as "the backend is wedged". The caller leaves the
        verdict untouched on ``None``.

        Deliberately *not* ``_backend_healthy``. That polls ``/v1/models``, whose
        sglang handler reads in-process tokenizer attributes and never touches the
        scheduler, so it returned 200 for every second of the 2026-08-09 wedge and
        right up until the process died. This path submits a one-token generate
        and returns 200 as soon as *any* inbound detokenizer message lands, which
        means it costs nothing on a busy server (another request's tokens satisfy
        it) and answers precisely the question a wedge changes.

        Two limits worth knowing. Aborting the probe client-side does not cancel
        the queued generate on the sglang side, so a wedged backend accumulates
        roughly one orphaned one-token request per tick — bounded by sglang's own
        300s watchdog kill and negligible at that size. And with ``dp_size > 1``
        the "anything recently" test is satisfied by any DP rank, so a single
        wedged rank among several would still read healthy; not the case on the
        current TP-only H200 units, but it bounds what this signal proves.
        """
        url = f"http://localhost:{self.backend_port}{HEALTH_PROBE_PATH}"
        try:
            req = Request(url, method="GET")
            with urlopen(req, timeout=HEALTH_PROBE_TIMEOUT) as resp:
                return resp.status == 200
        except HTTPError as exc:
            if exc.code in (404, 405, 501):
                return None
            return False
        except Exception:
            return False

    def _recheck_serving(self, state: _ContainerState | None) -> None:
        """Reconcile this manager's belief with the backend, once per watcher tick.

        The 2026-08-09 incident had two sub-cases and this covers both — but they
        want opposite treatments, and the difference is whether this thread can do
        anything about what it just found.

        (a) The container is gone. ``docker inspect`` says it exists and is not
        running while this manager still says ``ready`` — the 73 seconds at the end
        of the incident, when the proxy went on advertising a corpse until a request
        tripped over it. The belief is simply wrong, and here, uniquely, this thread
        can repair it: ``mark_stopped``. What it must *not* do is report the corpse
        as unhealthy and leave the belief standing. ``/health`` answering 503 is a
        request to the gateway to stop sending traffic, and traffic is the only
        thing that was going to repair a stale ``ready`` — the reconcile in
        ``_forward_with_body`` runs on the request path. A 503 here would therefore
        cut its own repair path and hold the replica out of rotation until the idle
        timer fired on activity that had already stopped: up to IDLE_TIMEOUT, 24
        minutes on the shipped units, over a backend the very next request would
        have restarted in seconds. ``stopped`` is the honest answer and the contract
        already gives it a 200: not down, startable on demand.

        An *absent* container is not this finding and is not acted on:
        ``_inspect_state`` degrades an unreachable daemon to the same reading, and a
        docker hiccup must not demote a serving replica. The probe below still
        catches a genuinely dead one.

        (b) The container runs but the backend is not serving. The eight minutes
        where every request hung: ``state == "ready"``, container up, scheduler
        making no forward progress. Nothing here can repair that — the wedge is
        sglang's, and its own watchdog resolves it by SIGQUIT at 300s — so this is
        the sub-case ``/health`` exists to report, and the only one that 503s.
        ``HEALTH_PROBE_STRIKES`` consecutive failures are required, and either the
        probe has already succeeded once for this container or its warmup budget is
        spent (see ``_health_probed_ok`` and HEALTH_PROBE_WARMUP), so neither a
        single blip nor a cold start can deregister a replica.

        Recovery is one good probe, so the verdict can never outlive the condition
        it describes. That matters more than it looks: the gateway samples
        ``/health`` on a 300-second timer, and unlike sub-case (a) this one really
        does clear itself without traffic — ``/health_generate`` submits its own
        one-token generate, so a wedge that lifts is observed on the next tick even
        with the replica fully drained.
        """
        if state is not None and state.exists and not state.running:
            log.warning(
                "[%s] Container %s is %s while this proxy still reported the backend "
                "ready; releasing that belief so the next request launches a fresh "
                "container instead of forwarding into a corpse.",
                self.model_name,
                self.container,
                state.status or "not running",
            )
            self.mark_stopped()
            return

        probe = self._probe_forward_progress()
        if probe is None:
            return
        if probe:
            with self._lock:
                self._health_probed_ok = True
                if self._health_reason is not None:
                    log.info(
                        "[%s] Backend %s is answering again; clearing the %s verdict.",
                        self.model_name,
                        self.container,
                        self._health_reason,
                    )
                self._health_reason = None
                self._health_detail = None
                self._health_since = None
                self._health_strikes = 0
            return
        with self._lock:
            answered_once = self._health_probed_ok
        if answered_once:
            detail = (
                f"{HEALTH_PROBE_PATH} on port {self.backend_port} answered earlier and has "
                f"now failed {HEALTH_PROBE_STRIKES} consecutive checks; the container is up "
                f"but is producing nothing"
            )
        else:
            detail = (
                f"{HEALTH_PROBE_PATH} on port {self.backend_port} has never answered, and the "
                f"{HEALTH_PROBE_WARMUP:.0f}s warmup budget is spent; the container is up but "
                f"has produced nothing since this proxy took it over"
            )
        self._latch_unserving("no_forward_progress", detail)

    def _latch_unserving(self, reason: str, detail: str) -> None:
        """Count one failed probe and, at the threshold, publish the verdict.

        Timed from the *first* bad observation rather than from the threshold, so
        ``unhealthy_for_seconds`` in the response body tells a postmortem when the
        backend stopped serving, not when this proxy got around to saying so.

        The cold-load suppression below is what keeps a 6-14 minute start from
        deregistering its own replica, and it is bounded on purpose. "The probe has
        never succeeded here" is a fair reading of a container this proxy just
        launched and a false one for a container it *adopted*: ``ensure_running``
        promotes an adopted backend to ``ready`` on ``_backend_healthy``, i.e.
        ``/v1/models``, which is precisely the endpoint a wedged sglang keeps
        answering. So a proxy restarted mid-wedge inherits the wedge with the latch
        unearned, and an unbounded excuse would hide it for as long as the process
        lived. Past HEALTH_PROBE_WARMUP from ``_health_grace_since`` the silence
        stops being warmup.
        """
        with self._lock:
            if self._health_strikes == 0:
                self._health_since = time.monotonic()
            self._health_strikes += 1
            if (
                not self._health_probed_ok
                and time.monotonic() - self._health_grace_since < HEALTH_PROBE_WARMUP
            ):
                return
            if self._health_strikes < HEALTH_PROBE_STRIKES or self._health_reason == reason:
                return
            self._health_reason = reason
            self._health_detail = detail
        log.warning(
            "[%s] Backend %s is ready but not serving (%s): %s. GET /health now reports "
            "this deployment unhealthy so the gateway can drain it.",
            self.model_name,
            self.container,
            reason,
            detail,
        )

    def _disown_if_replaced(self, state: _ContainerState) -> bool:
        """Drop a ``ready`` state that refers to a container someone else replaced.

        The gap this closes. ``_proxy`` forwards a request straight to
        ``localhost:backend_port`` whenever ``state == "ready"``, with no ownership
        check and no docker call — that fast path is the whole point of being ready.
        But nothing demoted ``ready`` when the container died *out of band*: the idle
        watcher only reads local state, and the reactive ``URLError`` reconcile in
        ``_forward_with_body`` needs the port to go quiet. So after a sibling proxy
        reclaims this backend's corpse (which the start path now does, by design) and
        launches its own container on the same name and host port, this manager keeps
        answering "ready" and forwards to a port that is now the sibling's backend.
        Two configs that collide on name and port but differ in profile then serve
        each other's clients the wrong model, success-shaped, for as long as traffic
        keeps the idle timer alive — and two idle watchers each believe they own it.

        Checked here, in a loop the proxy already runs, rather than on the request
        path: a warm request must still cost zero docker round trips, and the
        cold-start storm this guard's earlier rounds were about must not gain a
        ``docker inspect`` per request. The watcher only runs while a backend is
        ``ready``, so a starting backend adds nothing either. The exposure is
        therefore bounded by one watcher tick — ``min(10, IDLE_TIMEOUT / 2)`` seconds
        — instead of being unbounded.

        Removes nothing, ever. The container belongs to its new owner and may be
        mid-load or mid-stream; local state is all this proxy has any claim on, which
        is exactly what ``_stop_container`` concluded for the same situation arriving
        through the idle door. Returns True when the backend was demoted.

        And it makes sure the *next* request removes nothing either, which does not
        follow from this method touching nothing. Demotion is what puts the manager
        back on the start path, and every refusal there is gated on a foreign owner
        *label*: an unlabelled hand-restart or a same-``PROXY_OWNER`` sibling reads as
        this proxy's own, so ``_clear_container_name`` would ``docker rm -f`` the very
        container just disowned — the running backend of whoever replaced ours. So the
        id evidence is recorded in ``_disowned_container_id`` before ``mark_stopped``
        discards ``_container_id``, and the start path refuses on it. What the next
        request then does depends on which reading fired: a foreign label 502s naming
        both owners, an id mismatch 502s naming the two containers.

        ``state`` is the tick's one inspect, read by ``_watch_inspect`` and shared
        with the serving re-check rather than paid for twice.
        """
        reason = self._stale_ownership_reason(state)
        if reason is None:
            return False
        log.warning(
            "[%s] Backend %s was ready, but %s. Releasing local state without touching the "
            "container: this proxy has been serving another owner's backend on port %d, and "
            "the next request must re-decide ownership rather than forward to it.",
            self.model_name,
            self.container,
            reason,
            self.backend_port,
        )
        if state.container_id and self._container_owner(state.labels) is None:
            # The id branch fired: the name resolves to a container that reads as
            # *ours*. Every guard on the start path re-reads the owner label and
            # would agree, so this reading is the only thing standing between that
            # container and a `docker rm -f` (or a re-adoption) on the next request.
            # `mark_stopped` clears `_container_id` immediately below, so carry it.
            # A foreign owner label needs no help: it is re-read, and refused, by
            # `_start_container`, `_clear_container_name` and
            # `_adopt_running_container` alike.
            self._disowned_container_id = state.container_id
            self._disowned_reason = reason
        self.mark_stopped()
        return True

    def _adopt_running_container(self) -> bool:
        """Adopt an already-running, healthy container instead of reloading it.

        The proxy loses its in-memory state when it restarts, but the backend
        containers keep running. Without adoption the next request would
        ``docker rm -f`` a perfectly healthy backend and pay the multi-minute
        model reload. Returns True if the existing container was adopted.

        Raises ``ForeignContainerError`` when the container of this name belongs
        to another proxy — including while it is still loading and therefore not
        yet answering health checks. That case is the sharp one: a container
        eight minutes into a fourteen-minute load fails ``_backend_healthy``, so
        without this guard the caller would fall through to ``_start_container``
        and ``docker rm -f`` it.

        Reads the full state rather than ``(running, labels)`` so the adopted
        container's *id* is recorded too — an adopted backend needs the same later
        identity re-check as a launched one, and this inspect is already paid for.
        """
        state = self._inspect_state()
        running, labels = state.running, state.labels
        if not running:
            return False
        owner = self._container_owner(labels)
        if owner is not None:
            raise self._foreign_container_error(owner, "adopt")
        # A container the idle watcher disowned by id must not be taken back. It
        # reads as ours (that is the whole point of the id check), it is unlabelled
        # or same-owner so the profile test below cannot see it either, and a healthy
        # one would otherwise be adopted straight back into `ready` — with
        # `_container_id` overwritten by its id, so the ids agree from then on and
        # the re-check can never fire again. That turns a mis-serve bounded at one
        # watcher tick into a permanent one.
        disowned = self._disowned_container_error(state, "adopt")
        if disowned is not None:
            raise disowned
        stamped = labels.get(PROFILE_LABEL)
        if stamped is not None and stamped != self._profile:
            # My own container, but the config it was launched from has changed
            # since (a mem_fraction bump, a new image tag). Replace it rather
            # than adopt a backend running the previous config.
            log.info(
                "[%s] Container %s runs profile %s but the config is now %s — replacing it.",
                self.model_name,
                self.container,
                stamped,
                self._profile,
            )
            return False
        if self._backend_healthy():
            self._current_gpu = self._running_container_gpu()
            self._container_id = state.container_id or None
            self._forget_disowned_container()
            return True
        return False

    def _backend_healthy(self) -> bool:
        """Return True if the backend answers ``/v1/models`` right now.

        A single-shot probe (unlike ``_wait_healthy``, which polls until a
        deadline). Used to decide whether an already-running container can be
        adopted instead of reloaded.
        """
        url = f"http://localhost:{self.backend_port}/v1/models"
        try:
            req = Request(url, method="GET")
            with urlopen(req, timeout=5) as resp:
                return resp.status == 200
        except Exception:
            return False

    def _running_container_gpu(self) -> str | None:
        """Return the device list assigned to the running container, if any.

        Read back from the container's ``--gpus device=…`` request so an
        adopted backend keeps the right device for colocation/GPU-exclusion
        bookkeeping — and so a container being *replaced* hands its devices to the
        replacement instead of letting auto-selection move the backend elsewhere
        (see ``_start_container``).

        Returned in the same comma-separated shape ``_docker_gpu_arg`` consumes
        and ``_current_gpu`` records, because both round-trip it: the value is
        counted against ``tensor_parallel_size x pipeline_parallel_size``, handed
        to ``docker run``, and split on commas to exclude each device from other
        backends' auto-selection. Docker stores ``--gpus '"device=2,3"'`` as *one*
        request whose ``DeviceIDs`` is ``["2", "3"]`` — that single-request parse
        is exactly what the quoting in ``_docker_gpu_arg`` buys — so the ids are
        joined here rather than read through a Go template, whose ``range``
        concatenates without a separator and turned a two-GPU container into the
        one bogus token ``"23"``.
        """
        result = subprocess.run(
            [
                "sudo",
                "docker",
                "inspect",
                "-f",
                _INSPECT_DEVICES_FORMAT,
                self.container,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            return None
        try:
            requests = _json.loads(result.stdout.strip() or "null")
        except ValueError:
            return None
        if not isinstance(requests, list):
            return None
        # Flattened across requests: this proxy always emits one, but a
        # hand-started container can carry several (`--gpus device=0 --gpus
        # device=1`), and every one of them holds a device this backend owns.
        # A request that asks for a *count* rather than named devices
        # (`--gpus all`) contributes nothing — there is no id to reuse.
        devices = [
            str(device)
            for request in requests
            if isinstance(request, dict)
            for device in (request.get("DeviceIDs") or [])
        ]
        return ",".join(devices) or None

    def _container_logs_tail(self, lines: int = 20) -> str:
        """Return the last ``lines`` of the container log for error reporting."""
        result = subprocess.run(
            ["sudo", "docker", "logs", "--tail", str(lines), self.container],
            capture_output=True,
            text=True,
            check=False,
        )
        return (result.stdout + result.stderr).strip()

    def _wait_healthy(self) -> None:
        url = f"http://localhost:{self.backend_port}/v1/models"
        deadline = time.monotonic() + HEALTH_TIMEOUT
        while time.monotonic() < deadline:
            try:
                req = Request(url, method="GET")
                with urlopen(req, timeout=5) as resp:
                    if resp.status == 200:
                        return
            except Exception:
                pass
            # Fail fast if the container has died instead of polling a dead
            # backend until the full timeout expires.
            if not self._container_running():
                logs = self._container_logs_tail()
                raise RuntimeError(
                    f"[{self.model_name}] Backend container {self.container} exited "
                    f"before becoming healthy. Recent logs:\n{logs}"
                )
            time.sleep(HEALTH_INTERVAL)
        raise RuntimeError(
            f"[{self.model_name}] Backend did not become healthy within {HEALTH_TIMEOUT}s"
        )

    def _idle_watcher(self) -> None:
        """Poll a ``ready`` backend: whose container is it, is it serving, is it idle.

        Also where ``GET /health``'s verdict is computed, because this thread is
        already awake every ``min(10, IDLE_TIMEOUT / 2)`` seconds, already runs only
        while a backend is ``ready``, and already pays a ``docker inspect`` per tick —
        so the health signal costs one sub-second HTTP call and nothing at all on the
        request path. The handler then reads a cached answer (``health_report``).
        """
        while True:
            time.sleep(min(10, IDLE_TIMEOUT / 2))
            with self._lock:
                if self._state != "ready":
                    if self._state == "stopped":
                        return
                    continue
                idle_for = time.monotonic() - self._last_activity
            # Outside the lock: this issues a `docker inspect` and an HTTP probe, and
            # holding the lock across either would stall every request thread reading
            # `.state` for the length of the round trip. Before the idle check,
            # because a backend whose container someone else replaced has nothing
            # left to idle-stop.
            state = self._watch_inspect()
            if state is not None and self._disown_if_replaced(state):
                return
            self._recheck_serving(state)
            if idle_for >= IDLE_TIMEOUT:
                with self._lock:
                    if self._state != "ready":
                        continue
                    # Re-read rather than trust `idle_for`: it was measured before
                    # the inspect and the forward-progress probe, and the probe
                    # blocks for the whole HEALTH_PROBE_TIMEOUT exactly when the
                    # backend is slow. A request that arrived in that window has
                    # already been forwarded, and tearing its container down under
                    # it is the one thing the ready fast path cannot survive.
                    if time.monotonic() - self._last_activity < IDLE_TIMEOUT:
                        continue
                self._stop_container()
                return


def _copy_stream(resp: Any, wfile: Any) -> None:
    # read1 returns as soon as any data is available; read(8192) would block
    # accumulating 8192 bytes, batching short SSE streams into a single blob.
    while True:
        chunk = resp.read1(8192)
        if not chunk:
            break
        wfile.write(chunk)
        wfile.flush()


_backends: dict[str, BackendManager] = {}
for _name, _cfg in MODELS_CONFIG_DATA.items():
    _backends[_name] = BackendManager(_name, _cfg)


def _get_backend(
    body: bytes, request_path: str = "", request_method: str = ""
) -> BackendManager | None:
    """Pick the right backend from the ``model`` field in the request body."""
    try:
        model = _json.loads(body).get("model", "")
    except Exception:
        model = ""
    backend = _backends.get(model)
    if backend is not None:
        log.info("[%s] Request: %s %s", model, request_method, request_path)
    else:
        log.info("[unknown model] Request: %s %s  model=%s", request_method, request_path, model)
    return backend


DEFAULT_STARTUP_ESTIMATE_SECONDS = 120


def _warmup_thinking_sse(backend: BackendManager) -> str:
    """Build the "still starting" SSE chunk, with a per-model time estimate.

    A single hardcoded figure misleads badly on large MoE models: DeepSeek-V4-Flash
    takes 9-14 minutes to reach ready from cold (weight load, CUDA-graph capture and
    DeepGEMM JIT), so a flat "about 120 seconds" tells the caller to wait roughly a
    tenth of the real time. Models can set ``startup_estimate_seconds`` to say how
    long they actually take.
    """
    config = getattr(backend, "config", None) or {}
    seconds = int(config.get("startup_estimate_seconds", DEFAULT_STARTUP_ESTIMATE_SECONDS))
    # <= 120 so every model that never sets an estimate keeps the original wording.
    estimate = (
        f"about {seconds} seconds" if seconds <= 120 else f"about {round(seconds / 60)} minutes"
    )
    return (
        'data: {"id":"warmup","object":"chat.completion.chunk",'
        '"choices":[{"index":0,"delta":{"role":"assistant",'
        f'"content":"⏳ The model is starting up — this takes {estimate}. '
        'Please wait…"},"finish_reason":null}]}\n\n'
    )


WARMUP_THINKING_SSE_DONE = (
    'data: {"id":"warmup","object":"chat.completion.chunk",'
    '"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
)


class ProxyHandler(BaseHTTPRequestHandler):
    """Forwards requests to the correct sglang backend based on model name."""

    def _check_api_key(self) -> bool:
        # No "key is blank, so let everyone in" branch: LOCAL_API_KEY cannot be
        # blank (see its definition above), and an escape hatch that opens the
        # listener up on a *missing* config value is the wrong way round.
        auth = self.headers.get("Authorization", "")
        auth = auth[7:] if auth.startswith("Bearer ") else self.headers.get("X-API-Key", "")

        if auth != LOCAL_API_KEY:
            self.send_response(401)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"Missing or invalid API key")
            return False

        return True

    def _proxy(self) -> None:
        if self.path == "/v1/models" and self.command == "GET":
            self._handle_models_list()
            return

        # The routing layer's HealthMonitor probes the origin root at GET /health
        # with no API key and expects 200 (see apps/backend/routing/health.py), so
        # this stays before `_check_api_key`: a 401 here marks every local model
        # unhealthy.
        #
        # It used to answer 200 unconditionally, on the reasoning that the proxy
        # starts backends on demand so its own liveness is what the gateway is
        # asking about. On 2026-08-09 that reasoning cost eight minutes of an
        # outage: the sglang scheduler on h200b wedged, every request hung until
        # the 300s forward timeout, and this endpoint went on answering 200
        # throughout — the one signal the gateway polls said the replica was fine
        # while nothing it sent there could ever return.
        #
        # So the answer is now about the backends, but only in the one direction
        # that is honest. Idle-stopped and cold-loading stay 200: lazy start is the
        # design, a DeepSeek-class cold start takes 6-14 minutes, and the warmup
        # SSE stream needs requests to keep arriving to finish it — reporting those
        # as down would deregister the replica for the whole load. 503 is reserved
        # for the case nothing here can repair: a backend this proxy calls ready,
        # whose container is up, that has stopped producing anything. A ready
        # belief over a container that has *exited* is not reported but fixed —
        # `_recheck_serving` demotes it, and `stopped` is a 200 because the next
        # request starts it. See `_recheck_serving`, which forms the verdict on the
        # idle watcher's tick, and `_handle_health`, which only reads it.
        if self.path == "/health" and self.command == "GET":
            self._handle_health()
            return

        if not self._check_api_key():
            return

        body = self._read_body()
        backend = _get_backend(body, request_path=self.path, request_method=self.command)
        if backend is None:
            self.send_error(404, f"Unknown model. Available: {list(_backends.keys())}")
            return

        backend.touch()
        if backend.state != "ready":
            is_chat = self.command == "POST" and self.path.startswith("/v1/chat/completions")
            is_stream = False
            if is_chat and body:
                with contextlib.suppress(Exception):
                    is_stream = _json.loads(body).get("stream", False)

            if is_chat and is_stream:
                # Diagnose a contended container name *before* committing the 200
                # and the warmup banner: once the response line is written the
                # background start has no way to reach the client, and a
                # success-shaped answer keeps the gateway from failing over.
                contention = backend.contention_error()
                if contention is not None:
                    self.send_error(502, str(contention))
                    return
                # Same reasoning for a backend given up on after
                # MAX_START_FAILURES: "starting up…" would be a lie, and the
                # start it promises is never going to be attempted.
                giveup = backend.giveup_error()
                if giveup is not None:
                    self.send_error(502, str(giveup))
                    return
                self._handle_warmup_stream(backend)
                return
            try:
                backend.ensure_running()
            except RuntimeError as exc:
                self.send_error(502, str(exc))
                return
            self._forward_with_body(backend, body)
            return

        self._forward_with_body(backend, body)

    def _handle_health(self) -> None:
        """Answer the gateway's unauthenticated probe from cached backend verdicts.

        200 unless some backend this proxy calls ``ready`` is demonstrably not
        serving; a backend that is idle-stopped or still cold-loading is not
        "down", it is exactly what a lazy proxy is for. The 200 body is left as
        the bare ``{"status": "ok"}`` it has always been, because other callers
        parse it.

        The 503 body is the part worth having: which backend, what it is doing
        instead of serving, and how long it has been doing it. During the incident
        the journal recorded only that ``/health`` returned 200 while requests
        timed out, so reconstructing what the proxy believed and when took the
        container's own logs. Whatever answers this next time should not need them.

        Runs no docker command and issues no HTTP call. The gateway allows this
        handler two seconds (``timeout: 2`` in
        ``distributions/freeinference/config/routing.yaml``) and scores a slow
        answer exactly as it scores a 503, so a probe that did any I/O would fail
        itself first during the very wedge it exists to report — every other thread
        is parked in a 300s ``urlopen`` by then.

        One verdict covers every backend this proxy fronts, which is not a
        simplification but the only shape the caller can consume: ``HealthMonitor``
        rewrites whatever endpoint it was given to ``scheme://netloc/health``
        (``_check_once`` in apps/backend/routing/health.py) and stores one boolean
        per origin, so a proxy serving two models — ``ops/local_deployment_proxy/
        models.json`` fronts a chat model and an embedding model on one listener —
        has no channel to say which of them is unwell. The cost is real: a wedged
        chat model deregisters the local route of the embedding model beside it,
        which is then served from whatever other route it has. What bounds that
        cost is how narrow the 503 is. It needs a backend this proxy calls
        ``ready``, whose container is running, which answered the probe (or has
        outlived its warmup budget) and has since failed HEALTH_PROBE_STRIKES in a
        row — a live wedge, nothing else. In particular a dead container no longer
        reaches here at all: ``_recheck_serving`` demotes it to ``stopped``, which
        is a 200. Narrowing this further means giving the gateway a per-model health
        signal, which is a change to the gateway, not to this file.
        """
        unhealthy = [
            report for mgr in _backends.values() if (report := mgr.health_report()) is not None
        ]
        if unhealthy:
            payload = _json.dumps({"status": "unhealthy", "backends": unhealthy}).encode()
            status = 503
        else:
            payload = _json.dumps({"status": "ok"}).encode()
            status = 200
        # Hand-built rather than `send_error`, which answers text/html: the gateway
        # and the operator both read this as JSON.
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _handle_models_list(self) -> None:
        """Return a /v1/models response derived from live backend state.

        A model is reported ``loaded`` only when the proxy considers it ready
        *and* its container is actually running, so a backend that died outside
        the proxy's control is not advertised as available.
        """
        models = [
            {
                "id": name,
                "object": "model",
                "owned_by": "sglang",
                "status": "loaded" if (mgr.state == "ready" and mgr.alive()) else "not_loaded",
            }
            for name, mgr in _backends.items()
        ]
        payload = _json.dumps({"object": "list", "data": models}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(length) if length else b""

    def _send_sse_chunk(self, data: str) -> None:
        self.wfile.write(data.encode())
        self.wfile.flush()

    def _handle_warmup_stream(self, backend: BackendManager) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        self._send_sse_chunk(_warmup_thinking_sse(backend))
        self._send_sse_chunk(WARMUP_THINKING_SSE_DONE)
        self._send_sse_chunk("data: [DONE]\n\n")
        self.wfile.flush()
        self.close_connection = True

        def _warm_up() -> None:
            try:
                backend.ensure_running()
            except RuntimeError as exc:
                log.error("[%s] Backend failed to start: %s", backend.model_name, exc)
                return
            log.info("[%s] Backend ready — client should retry.", backend.model_name)

        threading.Thread(target=_warm_up, daemon=True).start()

    def _forward_with_body(
        self, backend: BackendManager, body: bytes, allow_restart: bool = True
    ) -> None:
        target = f"http://localhost:{backend.backend_port}{self.path}"
        headers = {k: v for k, v in self.headers.items() if k.lower() != "host"}
        req = Request(target, data=body if body else None, headers=headers, method=self.command)
        # Whether a status line has gone out yet. A failure after that point cannot
        # be retried (the client would get two responses concatenated) and cannot be
        # reported with `send_error`, which the stdlib documents as callable only
        # before any output — and both are now reachable, because the reconcile
        # below catches read-side failures that used to fall through untouched.
        committed = False
        try:
            with urlopen(req, timeout=300) as resp:
                is_streaming = resp.headers.get("Content-type", "").startswith("text/event-stream")
                self.send_response(resp.status)
                for key, val in resp.getheaders():
                    if key.lower() in ("transfer-encoding", "connection"):
                        continue
                    self.send_header(key, val)
                self.end_headers()
                committed = True
                if is_streaming:
                    _copy_stream(resp, self.wfile)
                else:
                    self.wfile.write(resp.read())
        except HTTPError as exc:
            # The backend returned an HTTP error response (it is alive and
            # answered). Pass the upstream status and body through UNCHANGED.
            # Remapping a client 4xx (e.g. vLLM's 400 "max context exceeded")
            # to a generic 502 makes the gateway's circuit breaker treat a bad
            # request as an upstream fault and open the circuit for everyone —
            # one user's oversized prompt then DoSes the model globally.
            try:
                body_bytes = exc.read()
            except Exception:  # body may be unreadable; fall back to message
                body_bytes = str(exc).encode()
            self.send_response(exc.code)
            for key, val in exc.headers.items():
                if key.lower() in ("transfer-encoding", "connection", "content-length"):
                    continue
                self.send_header(key, val)
            self.send_header("Content-Length", str(len(body_bytes)))
            self.end_headers()
            self.wfile.write(body_bytes)
        except (URLError, TimeoutError, RemoteDisconnected) as exc:
            # A connection error here means the backend port is dead. A backend
            # can die outside the proxy's control (crash, OOM, the sglang
            # scheduler exiting on an internal error) while the proxy still
            # believes it is "ready" — so it would 502 forever. Reconcile: if
            # the container really is gone, reset state, relaunch, and retry the
            # request once.
            #
            # `TimeoutError` and `RemoteDisconnected` are here because on
            # 2026-08-09 this reconcile never ran at all. urllib wraps only the
            # `OSError` raised by `h.request()`; anything raised by
            # `h.getresponse()` or by a body read propagates unwrapped, so a
            # forward that timed out or that died as the container exited landed
            # in the generic handler below instead — which is exactly why the
            # journal for that hour reads `code 500, message timed out` and
            # `code 500, message Remote end closed connection without response`
            # rather than the 502 this branch emits. The proxy therefore went on
            # believing a dead backend was ready for 73 seconds.
            #
            # The retry is gated on nothing having been sent yet. `urlopen` fails
            # before any client bytes are written, but a read timeout mid-SSE does
            # not: the status line is already out, so a second response would be
            # concatenated onto the first.
            if committed:
                log.warning(
                    "[%s] Forward failed after the response was committed (%s); "
                    "the client sees a truncated stream. Leaving reconciliation to "
                    "the next request.",
                    backend.model_name,
                    exc,
                )
                self.close_connection = True
                return
            if allow_restart and not backend.alive():
                log.warning(
                    "[%s] Backend unreachable (%s) and container is not running; "
                    "restarting and retrying once.",
                    backend.model_name,
                    exc,
                )
                backend.mark_stopped()
                try:
                    backend.ensure_running()
                except RuntimeError as start_exc:
                    self.send_error(502, str(start_exc))
                    return
                self._forward_with_body(backend, body, allow_restart=False)
                return
            self.send_error(502, f"Backend error: {exc}")
        except Exception as exc:
            if committed:
                # Same reason as above, and the commonest cause here is the client
                # hanging up mid-stream, which is not the backend's fault and has
                # no one left to report it to.
                log.info("[%s] Stream ended early: %s", backend.model_name, exc)
                self.close_connection = True
                return
            self.send_error(500, str(exc))

    def do_GET(self) -> None:
        """Handle GET by proxying to the matching backend."""
        self._proxy()

    def do_POST(self) -> None:
        """Handle POST by proxying to the matching backend."""
        self._proxy()

    def do_PUT(self) -> None:
        """Handle PUT by proxying to the matching backend."""
        self._proxy()

    def do_DELETE(self) -> None:
        """Handle DELETE by proxying to the matching backend."""
        self._proxy()

    def do_PATCH(self) -> None:
        """Handle PATCH by proxying to the matching backend."""
        self._proxy()

    def do_OPTIONS(self) -> None:
        """Handle OPTIONS by proxying to the matching backend."""
        self._proxy()

    def do_HEAD(self) -> None:
        """Handle HEAD by proxying to the matching backend."""
        self._proxy()

    def log_message(self, fmt: str, *args: object) -> None:  # type: ignore[override]
        """Route stdlib HTTP server logs through the module logger."""
        log.info(fmt, *args)


def main() -> None:
    """Start the proxy HTTP server and serve until interrupted."""
    models = list(_backends.keys())
    log.info(
        "local deployment proxy listening on :%d  (%d models: %s)  (idle timeout %ds)",
        LISTEN_PORT,
        len(models),
        ", ".join(models) if models else "none",
        IDLE_TIMEOUT,
    )
    server = ThreadingHTTPServer(("0.0.0.0", LISTEN_PORT), ProxyHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down proxy …")
        server.server_close()
        for mgr in _backends.values():
            if mgr.state != "stopped":
                mgr._stop_container()


if __name__ == "__main__":
    main()
