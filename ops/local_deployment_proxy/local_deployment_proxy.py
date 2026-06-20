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
MODELS_CONFIG  : Path to a JSON config file               (see below)

When ``MODELS_CONFIG`` is unset the proxy auto-selects a hardware profile from
``nvidia-smi``: ``models.h200.json`` on a 4+ x H200 box (DeepSeek-V4-Flash at
``tensor_parallel_size`` 4), ``models.rtx6000.json`` on an RTX (PRO) 6000
(Qwen3.6-35B), else ``models.json``. See ``_detect_profile_config``.

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
sglang-only knobs (``mtp``, ``mamba``, ``attention_backend``, …) are ignored
for vLLM backends; see ``_vllm_run_cmd`` for the vLLM-specific options.

Set ``"mtp": true`` on a generative model that ships native Multi-Token
Prediction layers (Qwen3.6 MoE, DeepSeek V3, …) to enable speculative decoding
via sglang's ``NEXTN`` algorithm. The defaults (1 step, eagle-topk 1, 2 draft
tokens) suit a single MTP layer; override with ``speculative_num_steps``,
``speculative_eagle_topk``, ``speculative_num_draft_tokens``, or
``speculative_algorithm`` if needed.

Additionally set ``"mamba": true`` on hybrid Mamba/linear-attention models
(Qwen3.5/3.6 MoE). sglang rejects MTP spec decoding alongside the default
radix cache for these unless the Mamba scheduler reserves extra buffers; the
flag adds ``--mamba-scheduler-strategy extra_buffer`` (override with
``mamba_scheduler_strategy``) and exports ``SGLANG_ENABLE_SPEC_V2=1``.
"""

from __future__ import annotations

import contextlib
import json as _json
import logging
import os
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import URLError
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
# Accept LOCAL_API_KEY as the canonical local upstream key, with FREEINFERENCE_API_KEY
# as a compatibility fallback.
LOCAL_API_KEY = os.environ.get("LOCAL_API_KEY", "freeinference_api")
LOCAL_API_KEY = LOCAL_API_KEY.strip()

_SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = _SCRIPT_DIR / "models.json"


def _detect_profile_config() -> Path:
    """Pick a hardware-specific models profile by inspecting the local GPUs.

    The same proxy code runs on machines with very different GPUs, and each
    machine should serve the model that fits it. We inspect ``nvidia-smi`` once
    at import and map the hardware to a profile JSON next to this script:

      * **4+ x H200**       -> ``models.h200.json``     (DeepSeek-V4-Flash, TP=4)
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
        chosen = _SCRIPT_DIR / "models.h200.json"
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

    Used for tensor-parallel backends (``tensor_parallel_size`` > 1) that need
    several devices. Picks greedily — least-used first, excluding each chosen
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
            if self._state == "ready":
                self._state = "stopped"
                self._current_gpu = None
                self._ready_event.clear()

    def ensure_running(self) -> None:
        """Start the container if needed and block until it is healthy."""
        should_start = False
        with self._lock:
            if self._state == "ready":
                self._last_activity = time.monotonic()
                return
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

    def _resolve_gpu(self) -> str:
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
        tp = int(self.config.get("tensor_parallel_size", 1))
        log.info(
            "[%s] Auto-selecting %d GPU(s) (excluding %s)",
            self.model_name,
            tp,
            sorted(used_gpus) if used_gpus else "none",
        )
        gpu = (
            _pick_free_gpus(tp, exclude=used_gpus) if tp > 1 else _pick_free_gpu(exclude=used_gpus)
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

    def _start_container(self) -> None:
        gpu = self._resolve_gpu()
        self._ensure_model_dir()
        subprocess.run(
            ["sudo", "docker", "rm", "-f", self.container],
            check=False,
            capture_output=True,
        )
        engine = str(self.config.get("engine", "sglang")).lower()
        cmd = self._vllm_run_cmd(gpu) if engine == "vllm" else self._sglang_run_cmd(gpu)
        log.info("Running: %s", " ".join(cmd))
        subprocess.run(cmd, check=True, capture_output=True)

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
        cmd = [
            "sudo",
            "docker",
            "run",
            "-d",
            "--name",
            self.container,
            "--gpus",
            f"device={gpu}",
            "--shm-size",
            "16g",
            # Tensor-parallel backends span several GPUs inside one container;
            # NCCL needs host IPC for fast peer-to-peer transport.
            *(["--ipc=host"] if tp > 1 else []),
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

    def _sglang_run_cmd(self, gpu: str) -> list[str]:
        """Build the ``docker run`` command for an sglang backend."""
        tp = int(self.config.get("tensor_parallel_size", 1))
        cmd = [
            "sudo",
            "docker",
            "run",
            "-d",
            "--name",
            self.container,
            "--gpus",
            f"device={gpu}",
            "--shm-size",
            "16g",
            # Tensor-parallel backends span several GPUs inside one container;
            # NCCL needs host IPC for fast peer-to-peer transport.
            *(["--ipc=host"] if tp > 1 else []),
            "-p",
            f"{self.backend_port}:8001",
            "-v",
            f"{self.config['model_dir']}:/model:ro",
            *self._docker_env_args(),
            "lmsysorg/sglang:latest",
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
        ]
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
                cmd += [
                    "--speculative-algorithm",
                    str(self.config.get("speculative_algorithm", "NEXTN")),
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
        log.info("[%s] Stopping container %s …", self.model_name, self.container)
        subprocess.run(
            ["sudo", "docker", "rm", "-f", self.container],
            check=False,
            capture_output=True,
        )
        with self._lock:
            self._state = "stopped"
            self._current_gpu = None
        log.info("[%s] Container %s stopped.", self.model_name, self.container)

    def _container_running(self) -> bool:
        """Return True while the backend container is still up.

        A crashed container (e.g. sglang OOM on startup) exits within seconds;
        without this check the health loop would keep polling a dead backend
        until ``HEALTH_TIMEOUT`` elapses, making the client request appear to
        hang forever.
        """
        result = subprocess.run(
            ["sudo", "docker", "inspect", "-f", "{{.State.Running}}", self.container],
            capture_output=True,
            text=True,
            check=False,
        )
        # Missing container (non-zero exit) or any non-"true" status means it is
        # no longer running.
        return result.returncode == 0 and result.stdout.strip() == "true"

    def _adopt_running_container(self) -> bool:
        """Adopt an already-running, healthy container instead of reloading it.

        The proxy loses its in-memory state when it restarts, but the backend
        containers keep running. Without adoption the next request would
        ``docker rm -f`` a perfectly healthy backend and pay the multi-minute
        model reload. Returns True if the existing container was adopted.
        """
        if self._container_running() and self._backend_healthy():
            self._current_gpu = self._running_container_gpu()
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
        """Return the GPU device id assigned to the running container, if any.

        Read back from the container's ``--gpus device=N`` request so an
        adopted backend keeps the right device for colocation/GPU-exclusion
        bookkeeping.
        """
        result = subprocess.run(
            [
                "sudo",
                "docker",
                "inspect",
                "-f",
                "{{range .HostConfig.DeviceRequests}}{{range .DeviceIDs}}{{.}}{{end}}{{end}}",
                self.container,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        gpu = result.stdout.strip()
        return gpu or None

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
        while True:
            time.sleep(min(10, IDLE_TIMEOUT / 2))
            with self._lock:
                if self._state != "ready":
                    if self._state == "stopped":
                        return
                    continue
                idle_for = time.monotonic() - self._last_activity
            if idle_for >= IDLE_TIMEOUT:
                with self._lock:
                    if self._state != "ready":
                        continue
                self._stop_container()
                return


def _copy_stream(resp: Any, wfile: Any) -> None:
    while True:
        chunk = resp.read(8192)
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


WARMUP_THINKING_SSE = (
    'data: {"id":"warmup","object":"chat.completion.chunk",'
    '"choices":[{"index":0,"delta":{"role":"assistant",'
    '"content":"⏳ The model is starting up — this takes about 120 seconds. '
    'Please wait…"},"finish_reason":null}]}\n\n'
)
WARMUP_THINKING_SSE_DONE = (
    'data: {"id":"warmup","object":"chat.completion.chunk",'
    '"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
)


class ProxyHandler(BaseHTTPRequestHandler):
    """Forwards requests to the correct sglang backend based on model name."""

    def _check_api_key(self) -> bool:
        if not LOCAL_API_KEY:
            return True

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

        # The routing layer's HealthMonitor probes the origin root at GET
        # /health with no API key and expects 200 (see apps/backend/routing/
        # health.py). The proxy lazily starts backends on demand, so liveness of
        # the proxy itself means the deployment is available — a backend that is
        # idle-stopped is not "down". Answer 200 here without auth; otherwise the
        # health check 401s and the gateway marks every local model unhealthy.
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
        """Return 200 while the proxy is alive (unauthenticated liveness probe).

        Reports the proxy's own liveness, not any backend's, because backends
        start on demand — an idle-stopped backend is healthy, just not loaded.
        """
        payload = _json.dumps({"status": "ok"}).encode()
        self.send_response(200)
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

        self._send_sse_chunk(WARMUP_THINKING_SSE)
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
        try:
            with urlopen(req, timeout=300) as resp:
                is_streaming = resp.headers.get("Content-type", "").startswith("text/event-stream")
                self.send_response(resp.status)
                for key, val in resp.getheaders():
                    if key.lower() in ("transfer-encoding", "connection"):
                        continue
                    self.send_header(key, val)
                self.end_headers()
                if is_streaming:
                    _copy_stream(resp, self.wfile)
                else:
                    self.wfile.write(resp.read())
        except URLError as exc:
            # A connection error here means the backend port is dead. A backend
            # can die outside the proxy's control (crash, OOM, the sglang
            # scheduler exiting on an internal error) while the proxy still
            # believes it is "ready" — so it would 502 forever. Reconcile: if
            # the container really is gone, reset state, relaunch, and retry the
            # request once. urlopen fails before any client bytes are written,
            # so a single retry is safe (no partially-sent response).
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
