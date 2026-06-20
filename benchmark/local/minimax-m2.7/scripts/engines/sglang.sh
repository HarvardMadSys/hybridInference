#!/usr/bin/env bash
# sglang.sh — start/stop a native sglang server for the MiniMax-M2.7 benchmark.
# Usage: sglang.sh start|stop
#   start: serves the native-FP8 model on GPUs 2,3 (TP=2), port 8001
#   stop:  kills the server process group
#
# No Docker on this shared host — sglang runs from an isolated uv venv. The
# process is started with setsid so the whole TP worker group can be torn
# down by signalling the negative PID (process-group kill).

set -euo pipefail

VENV=/netscratch/juncheng/venvs/sglang
MODEL_DIR=/netscratch/juncheng/models/MiniMax-M2.7-FP8
PORT=8001
GPUS=2,3
MAX_MODEL_LEN=40960
GMU=0.92
LOGDIR=/netscratch/juncheng/logs
LOG="$LOGDIR/serve_sglang.log"
PIDFILE="$LOGDIR/serve_sglang.pid"

cmd="${1:-}"
case "$cmd" in
  start)
    mkdir -p "$LOGDIR"
    CUDA_VISIBLE_DEVICES="$GPUS" setsid "$VENV/bin/python" -m sglang.launch_server \
      --model-path "$MODEL_DIR" \
      --served-model-name "MiniMaxAI/MiniMax-M2.7" \
      --tp 2 \
      --context-length "$MAX_MODEL_LEN" \
      --mem-fraction-static "$GMU" \
      --kv-cache-dtype fp8_e4m3 \
      --enable-cache-report \
      --trust-remote-code \
      --host 0.0.0.0 --port "$PORT" > "$LOG" 2>&1 &
    echo $! > "$PIDFILE"
    echo "sglang pid $(cat "$PIDFILE") (log: $LOG)"
    ;;
  stop)
    if [[ -f "$PIDFILE" ]]; then
      kill -TERM -- "-$(cat "$PIDFILE")" 2>/dev/null || true
    fi
    pkill -f "sglang.launch_server" 2>/dev/null || true
    sleep 5
    rm -f "$PIDFILE"
    ;;
  *)
    echo "Usage: $0 start|stop" >&2
    exit 2
    ;;
esac
