#!/usr/bin/env bash
# vllm.sh — start/stop the vLLM serving container for the throughput benchmark.
# Usage: vllm.sh start|stop
#   start: launches vllm/vllm-openai with the FP8 model on GPU 1, port 8000
#   stop:  stops and removes the container

set -euo pipefail

CONTAINER=qwen36-vllm
MODEL_DIR=/netscratch/juncheng/models/Qwen3.6-35B-A3B-FP8
PORT=8000
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
      -p "${PORT}:8000" \
      -v "${MODEL_DIR}:/model:ro" \
      vllm/vllm-openai:latest \
      --model /model \
      --served-model-name "Qwen/Qwen3.6-35B-A3B-FP8" \
      --max-model-len "$MAX_MODEL_LEN" \
      --gpu-memory-utilization "$GMU" \
      --tensor-parallel-size 1 \
      --kv-cache-dtype fp8 \
      --port 8000 >/dev/null
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
