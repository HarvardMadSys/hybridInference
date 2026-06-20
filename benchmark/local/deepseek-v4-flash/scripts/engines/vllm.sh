#!/usr/bin/env bash
# vllm.sh — start/stop a native vLLM server for the DeepSeek-V4-Flash benchmark.
# Usage: vllm.sh start|stop
#   start: serves the FP8 model on GPUs 0-3 with TP=2 x PP=2, port 8000
#   stop:  kills the server process group
#
# No Docker on this shared host — vLLM runs from an isolated uv venv. The
# process is started with setsid so the whole TP/PP worker group can be torn
# down by signalling the negative PID (process-group kill).

set -euo pipefail

VENV=/netscratch/juncheng/venvs/vllm
MODEL_DIR=/netscratch/juncheng/models/DeepSeek-V4-Flash-FP8
PORT=8000
GPUS=0,1,2,3
MAX_MODEL_LEN=40960
GMU=0.90
TP=2
PP=2
LOGDIR=/netscratch/juncheng/logs
LOG="$LOGDIR/serve_dsv4_vllm.log"
PIDFILE="$LOGDIR/serve_dsv4_vllm.pid"

cmd="${1:-}"
case "$cmd" in
  start)
    mkdir -p "$LOGDIR"
    # No CUDA toolkit (nvcc) on this host, so the FlashInfer DeepGEMM FP8
    # block-scale GEMM can't JIT its cubin and asserts at startup. Disable it;
    # vLLM falls back to its prebuilt CUTLASS FP8 block-scale GEMM.
    CUDA_VISIBLE_DEVICES="$GPUS" \
    VLLM_USE_DEEP_GEMM=0 \
    setsid "$VENV/bin/vllm" serve "$MODEL_DIR" \
      --served-model-name "deepseek-v4-flash" \
      --tensor-parallel-size "$TP" \
      --pipeline-parallel-size "$PP" \
      --enable-expert-parallel \
      --max-model-len "$MAX_MODEL_LEN" \
      --gpu-memory-utilization "$GMU" \
      --kv-cache-dtype fp8 \
      --trust-remote-code \
      --host 0.0.0.0 --port "$PORT" > "$LOG" 2>&1 &
    echo $! > "$PIDFILE"
    echo "vllm pid $(cat "$PIDFILE") (log: $LOG)"
    ;;
  stop)
    if [[ -f "$PIDFILE" ]]; then
      kill -TERM -- "-$(cat "$PIDFILE")" 2>/dev/null || true
    fi
    pkill -f "vllm serve $MODEL_DIR" 2>/dev/null || true
    sleep 5
    rm -f "$PIDFILE"
    ;;
  *)
    echo "Usage: $0 start|stop" >&2
    exit 2
    ;;
esac
