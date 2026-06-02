#!/usr/bin/env bash
# sglang.sh — start/stop the sglang serving container.
# Usage: sglang.sh start|stop

set -euo pipefail

CONTAINER=qwen36-sglang
MODEL_DIR=/scratch/juncheng/models/Qwen3.6-35B-A3B-FP8
PORT=8001
GPU_INDEX=1
MAX_MODEL_LEN=135168
GMU=0.90

cmd="${1:-}"
case "$cmd" in
  start)
    sudo docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
    sudo docker run -d --name "$CONTAINER" \
      --gpus "\"device=${GPU_INDEX}\"" \
      --shm-size 16g \
      -p "${PORT}:8001" \
      -v "${MODEL_DIR}:/model:ro" \
      lmsysorg/sglang:latest \
      python3 -m sglang.launch_server \
      --model-path /model \
      --served-model-name "Qwen/Qwen3.6-35B-A3B-FP8" \
      --host 0.0.0.0 --port 8001 \
      --context-length "$MAX_MODEL_LEN" \
      --mem-fraction-static "$GMU" \
      --tp 1 >/dev/null
    echo "$CONTAINER"
    ;;
  stop)
    sudo docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
    ;;
  *)
    echo "Usage: $0 start|stop" >&2
    exit 2
    ;;
esac
