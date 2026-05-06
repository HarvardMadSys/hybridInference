#!/usr/bin/env bash
# Launch sglang or vllm containers for benchmarking on NVIDIA GB10.
#
# Usage:
#   ./ops/perf/launch_servers.sh sglang   # qwen3.6-35b on port 8001
#   ./ops/perf/launch_servers.sh vllm     # glm-4.7-flash on port 8002
#   ./ops/perf/launch_servers.sh all      # both (run sequentially on single GPU)
#   ./ops/perf/launch_servers.sh stop     # stop all containers
#
# Models:
#   sglang: Qwen/Qwen3.6-35B-A3B-FP8  (FP8, ~35B active params, MoE)
#   vllm:   THUDM/GLM-4.7-Flash        (quantized, needs HuggingFace access)
#
# Notes:
#   - On single GPU, run ONE server at a time.
#   - Add --gpu <id> to select a specific GPU (default: 0).
#   - vLLM needs --trust-remote-code for GLM models.

set -euo pipefail

GPU="${GPU:-0}"
SGLANG_PORT="${SGLANG_PORT:-8001}"
VLLM_PORT="${VLLM_PORT:-8002}"

SGLANG_MODEL="Qwen/Qwen3.6-35B-A3B-FP8"
VLLM_MODEL="zai-org/GLM-4.7-Flash"

launch_sglang() {
    echo "Launching sglang server for ${SGLANG_MODEL} on port ${SGLANG_PORT} (GPU ${GPU})..."
    docker rm -f bench-sglang 2>/dev/null || true
    docker run -d \
        --name bench-sglang \
        --gpus "\"device=${GPU}\"" \
        --ipc=host \
        -p "${SGLANG_PORT}:30000" \
        -v ~/.cache/huggingface:/root/.cache/huggingface \
        lmsys/sglang:latest \
        python3 -m sglang.launch_server \
        --model-path "${SGLANG_MODEL}" \
        --port 30000 \
        --host 0.0.0.0 \
        --mem-fraction-static 0.85 \
        --tp 1
    echo "sglang container started: bench-sglang"
    echo "Wait for model to load (~2-5 min), then check: curl http://localhost:${SGLANG_PORT}/v1/models"
}

launch_vllm() {
    echo "Launching vllm server for ${VLLM_MODEL} on port ${VLLM_PORT} (GPU ${GPU})..."
    docker rm -f bench-vllm 2>/dev/null || true
    docker run -d \
        --name bench-vllm \
        --gpus "\"device=${GPU}\"" \
        --ipc=host \
        -p "${VLLM_PORT}:8000" \
        -v ~/.cache/huggingface:/root/.cache/huggingface \
        vllm/vllm-openai:latest \
        --model "${VLLM_MODEL}" \
        --port 8000 \
        --host 0.0.0.0 \
        --trust-remote-code \
        --gpu-memory-utilization 0.85 \
        --dtype auto \
        --max-model-len 8192
    echo "vllm container started: bench-vllm"
    echo "Wait for model to load (~2-5 min), then check: curl http://localhost:${VLLM_PORT}/v1/models"
}

stop_all() {
    echo "Stopping benchmark containers..."
    docker rm -f bench-sglang 2>/dev/null && echo "Stopped bench-sglang" || true
    docker rm -f bench-vllm 2>/dev/null && echo "Stopped bench-vllm" || true
}

wait_for_server() {
    local url="$1"
    local name="$2"
    local max_wait="${3:-300}"
    echo "Waiting for ${name} to be ready (max ${max_wait}s)..."
    local start=$(date +%s)
    while true; do
        if curl -s "${url}/v1/models" | grep -q "data"; then
            echo "${name} is ready!"
            return 0
        fi
        local now=$(date +%s)
        local elapsed=$((now - start))
        if [ "$elapsed" -ge "$max_wait" ]; then
            echo "Timeout waiting for ${name} after ${max_wait}s"
            return 1
        fi
        sleep 5
        echo "  ... still loading (${elapsed}s elapsed)"
    done
}

case "${1:-}" in
    sglang)
        launch_sglang
        ;;
    vllm)
        launch_vllm
        ;;
    all)
        echo "WARNING: Running both on a single GPU will OOM."
        echo "Run them sequentially instead:"
        echo "  1. $0 sglang && # wait, benchmark, then $0 stop"
        echo "  2. $0 vllm   && # wait, benchmark, then $0 stop"
        exit 1
        ;;
    wait-sglang)
        wait_for_server "http://localhost:${SGLANG_PORT}" "sglang"
        ;;
    wait-vllm)
        wait_for_server "http://localhost:${VLLM_PORT}" "vllm"
        ;;
    stop)
        stop_all
        ;;
    *)
        echo "Usage: $0 {sglang|vllm|wait-sglang|wait-vllm|stop}"
        echo ""
        echo "Commands:"
        echo "  sglang       Launch sglang with Qwen3.6-35B-A3B-FP8"
        echo "  vllm         Launch vllm with GLM-4.7-Flash"
        echo "  wait-sglang  Wait for sglang to be ready"
        echo "  wait-vllm    Wait for vllm to be ready"
        echo "  stop         Stop all benchmark containers"
        echo ""
        echo "Environment variables:"
        echo "  GPU          GPU device ID (default: 0)"
        echo "  SGLANG_PORT  Port for sglang (default: 8001)"
        echo "  VLLM_PORT    Port for vllm (default: 8002)"
        exit 1
        ;;
esac
