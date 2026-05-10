#!/usr/bin/env bash
# benchmark/serve.sh — Launch Qwen3.6-27B-FP8 and Qwen3.6-35B-A3B-FP8 via vLLM
# and expose them on spark2 via reverse SSH tunnels.
#
# Usage:
#   ./benchmark/serve.sh [--no-tunnel]
#
# What it does:
#   1. Starts qwen36-vllm-27b  on local port 8000 (GPU 0)
#   2. Starts qwen36-vllm-35b  on local port 8001 (GPU 1)
#   3. Waits for both /v1/models endpoints to respond 200
#   4. Opens reverse SSH tunnels so that:
#        spark2:10800  →  localhost:8000  (27B)
#        spark2:10801  →  localhost:8001  (35B)
#   Ctrl-C tears everything down cleanly.
#
# Requirements: Docker with NVIDIA Container Toolkit, SSH access to spark2.
# Override SSH_HOST to use a different remote, e.g.:
#   SSH_HOST=user@spark2.example.com ./benchmark/serve.sh

set -euo pipefail

# ── Config ────────────────────────────────────────────────────────────────────
MODEL_27B_DIR=/scratch/juncheng/models/Qwen3.6-27B-FP8
MODEL_27B_NAME="Qwen/Qwen3.6-27B-FP8"
CONTAINER_27B=qwen36-vllm-27b
PORT_27B=8000
GPU_27B=0

MODEL_35B_DIR=/scratch/juncheng/models/Qwen3.6-35B-A3B-FP8
MODEL_35B_NAME="Qwen/Qwen3.6-35B-A3B-FP8"
CONTAINER_35B=qwen36-vllm-35b
PORT_35B=8001
GPU_35B=1

MAX_MODEL_LEN=135168
GMU=0.90

REMOTE_PORT_27B=10800
REMOTE_PORT_35B=10801
SSH_HOST=${SSH_HOST:-spark2}

HEALTH_TIMEOUT=600   # seconds to wait for each model to be ready
HEALTH_INTERVAL=10

NO_TUNNEL=false
if [[ "${1:-}" == "--no-tunnel" ]]; then
  NO_TUNNEL=true
fi

# ── Cleanup ───────────────────────────────────────────────────────────────────
TUNNEL_PID=""

cleanup() {
  echo ""
  echo "[serve] Shutting down…"
  [[ -n "$TUNNEL_PID" ]] && kill "$TUNNEL_PID" 2>/dev/null || true
  sudo docker rm -f "$CONTAINER_27B" "$CONTAINER_35B" >/dev/null 2>&1 || true
  echo "[serve] Done."
}
trap cleanup EXIT INT TERM

# ── Helpers ───────────────────────────────────────────────────────────────────
start_container() {
  local name=$1 model_dir=$2 model_name=$3 port=$4 gpu=$5
  echo "[serve] Starting $name on GPU $gpu → localhost:$port"
  sudo docker rm -f "$name" >/dev/null 2>&1 || true
  sudo docker run -d --name "$name" \
    --gpus "\"device=${gpu}\"" \
    --shm-size 16g \
    -p "${port}:8000" \
    -v "${model_dir}:/model:ro" \
    vllm/vllm-openai:latest \
    --model /model \
    --served-model-name "$model_name" \
    --max-model-len "$MAX_MODEL_LEN" \
    --gpu-memory-utilization "$GMU" \
    --tensor-parallel-size 1 \
    --kv-cache-dtype fp8 \
    --port 8000 >/dev/null
}

wait_healthy() {
  local name=$1 port=$2
  local url="http://localhost:${port}/v1/models"
  local deadline=$(( $(date +%s) + HEALTH_TIMEOUT ))
  echo "[serve] Waiting for $name at $url (up to ${HEALTH_TIMEOUT}s)…"
  while true; do
    if curl -sf "$url" >/dev/null 2>&1; then
      echo "[serve] $name is ready."
      return 0
    fi
    if (( $(date +%s) >= deadline )); then
      echo "[serve] ERROR: $name did not become healthy within ${HEALTH_TIMEOUT}s." >&2
      return 1
    fi
    sleep "$HEALTH_INTERVAL"
  done
}

# ── Launch containers ─────────────────────────────────────────────────────────
start_container "$CONTAINER_27B" "$MODEL_27B_DIR" "$MODEL_27B_NAME" "$PORT_27B" "$GPU_27B"
start_container "$CONTAINER_35B" "$MODEL_35B_DIR" "$MODEL_35B_NAME" "$PORT_35B" "$GPU_35B"

# Wait for both in parallel
wait_healthy "$CONTAINER_27B" "$PORT_27B" &
PID_27B=$!
wait_healthy "$CONTAINER_35B" "$PORT_35B" &
PID_35B=$!

wait "$PID_27B" || { echo "[serve] 27B failed health check." >&2; exit 1; }
wait "$PID_35B" || { echo "[serve] 35B failed health check." >&2; exit 1; }

echo "[serve] Both models are healthy."
echo "[serve]   localhost:${PORT_27B}  →  $MODEL_27B_NAME"
echo "[serve]   localhost:${PORT_35B}  →  $MODEL_35B_NAME"

# ── SSH reverse tunnels ───────────────────────────────────────────────────────
if $NO_TUNNEL; then
  echo "[serve] --no-tunnel set; skipping SSH tunnel. Press Ctrl-C to stop."
  wait
else
  echo "[serve] Opening reverse tunnels on ${SSH_HOST}:"
  echo "[serve]   ${SSH_HOST}:${REMOTE_PORT_27B} → localhost:${PORT_27B}  (27B)"
  echo "[serve]   ${SSH_HOST}:${REMOTE_PORT_35B} → localhost:${PORT_35B}  (35B)"

  ssh -N \
    -R "${REMOTE_PORT_27B}:localhost:${PORT_27B}" \
    -R "${REMOTE_PORT_35B}:localhost:${PORT_35B}" \
    -o ServerAliveInterval=30 \
    -o ServerAliveCountMax=3 \
    -o ExitOnForwardFailure=yes \
    "$SSH_HOST" &
  TUNNEL_PID=$!

  echo "[serve] Tunnel PID: $TUNNEL_PID — press Ctrl-C to stop."
  wait "$TUNNEL_PID"
fi
