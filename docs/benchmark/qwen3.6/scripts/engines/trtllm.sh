#!/usr/bin/env bash
# trtllm.sh — verify support, build engine, start/stop the TensorRT-LLM container.
# Usage: trtllm.sh verify|build|start|stop
#   verify: prints "supported" or "unsupported" for the Qwen3.6 architecture
#   build:  compiles a TRT engine from the FP8 checkpoint (one-time, slow)
#   start:  launches the TRT-LLM OpenAI-compatible server
#   stop:   stops and removes the container

set -euo pipefail

CONTAINER=qwen36-trtllm
IMAGE=nvcr.io/nvidia/tensorrt-llm/release:latest
MODEL_DIR=/netscratch/juncheng/models/Qwen3.6-35B-A3B-FP8
ENGINE_DIR=/netscratch/juncheng/models/Qwen3.6-35B-A3B-FP8-trtllm-engine
PORT=8002
GPU_INDEX=1
MAX_MODEL_LEN=135168

cmd="${1:-}"
case "$cmd" in
  verify)
    sudo docker run --rm --gpus "\"device=${GPU_INDEX}\"" "$IMAGE" \
      python3 -c "
import sys
try:
    from tensorrt_llm.models import MODEL_MAP
except Exception as e:
    print('unsupported:', e); sys.exit(0)
keys = ' '.join(sorted(MODEL_MAP.keys())).lower()
if 'qwen3' in keys or 'qwen3moe' in keys or 'qwen3_moe' in keys:
    print('supported')
else:
    print('unsupported: no qwen3 entry in MODEL_MAP')
"
    ;;

  build)
    mkdir -p "$ENGINE_DIR"
    sudo docker run --rm --gpus "\"device=${GPU_INDEX}\"" \
      -v "${MODEL_DIR}:/model:ro" -v "${ENGINE_DIR}:/engine" \
      "$IMAGE" \
      trtllm-build \
        --checkpoint_dir /model \
        --output_dir /engine \
        --max_input_len "$MAX_MODEL_LEN" \
        --max_seq_len "$MAX_MODEL_LEN" \
        --max_batch_size 128 \
        --gpt_attention_plugin float16 \
        --gemm_plugin auto
    ;;

  start)
    # Use trtllm-serve --backend pytorch which loads HF checkpoints directly
    # (no offline trtllm-build step needed). The pytorch backend still goes
    # through TRT-LLM's runtime/scheduler/KV cache, so it's a fair comparison
    # point even if it's not the AOT-compiled TRT engine path.
    sudo docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
    sudo docker run -d --name "$CONTAINER" \
      --gpus "\"device=${GPU_INDEX}\"" \
      --shm-size 16g \
      --ipc=host --ulimit memlock=-1 --ulimit stack=67108864 \
      -p "${PORT}:8002" \
      -v "${MODEL_DIR}:/model:ro" \
      "$IMAGE" \
      trtllm-serve serve /model \
        --backend pytorch \
        --host 0.0.0.0 --port 8002 \
        --max_seq_len "$MAX_MODEL_LEN" \
        --max_batch_size 128 \
        --tp_size 1 \
        --kv_cache_free_gpu_memory_fraction 0.90 \
        --trust_remote_code >/dev/null
    echo "$CONTAINER"
    ;;

  stop)
    sudo docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
    ;;

  *)
    echo "Usage: $0 verify|build|start|stop" >&2
    exit 2
    ;;
esac
