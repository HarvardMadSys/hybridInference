#!/usr/bin/env bash
# ops/h200_idle_proxy/bench_decode.sh — decode-throughput benchmark for the
# running DeepSeek-V4-Flash-0731 backend (sglang, TP=2 on GPUs 2,3).
#
# Runs sglang.bench_serving *inside* the backend container against
# 127.0.0.1:8001, so no host-side sglang install is needed. Output length is
# fixed via ignore_eos (--random-range-ratio 1.0), which isolates decode from
# prefill. Input length is controlled with the model's own tokenizer
# (--tokenizer /model) so token counts are exact.
#
# The backend must already be running — send one request through the idle proxy
# first to cold-start it (a streaming request returns immediately and loads it).
# A backend cold-started that way is the proxy's, and its idle timer keeps
# running: it can be reclaimed mid-benchmark after IDLE_TIMEOUT of no proxy
# traffic (docker exec does not count as activity). To bench a container the
# proxy will not touch at all, start it by hand with an owner of its own:
#
#   sudo docker run -d --name deepseek-v4-flash-sglang \
#     --label com.freeinference.proxy.owner=manual  …
#
# See ops/local_deployment_proxy/README.md, "Container ownership".
#
# Usage:
#   ./ops/h200_idle_proxy/bench_decode.sh
#   CONTAINER=deepseek-v4-flash-sglang ./ops/h200_idle_proxy/bench_decode.sh
#
# NOTE: this drives the live backend; if it is tunneled to staging/prod the load
# is shared with real traffic. Keep runs short.

set -euo pipefail

CONTAINER="${CONTAINER:-deepseek-v4-flash-sglang}"

if ! sudo docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null | grep -q true; then
  echo "ERROR: container '$CONTAINER' is not running. Cold-start it first." >&2
  exit 1
fi

run() { # concurrency num_prompts in_len out_len label
  local c=$1 n=$2 inl=$3 outl=$4 label=$5
  echo "########## ${label}: concurrency=${c} num_prompts=${n} in=${inl} out=${outl} ##########"
  sudo docker exec "$CONTAINER" python3 -m sglang.bench_serving \
    --backend sglang --host 127.0.0.1 --port 8001 \
    --model /model --tokenizer /model \
    --dataset-name random --random-input-len "$inl" --random-output-len "$outl" \
    --random-range-ratio 1.0 --num-prompts "$n" --max-concurrency "$c" 2>&1 \
    | grep -iE "Max request concurrency|Successful requests|Benchmark duration|Output token throughput|Peak output|Total token throughput|Median ITL|Median TTFT|Mean E2E"
  echo
}

# Concurrency saturation sweep (short context isolates decode scaling).
for c in 1 16 64 128 256; do
  n=$(( c * 2 )); (( n < 8 )) && n=8
  run "$c" "$n" 128 256 "SATURATION c=${c}"
done

# Long-context decode (single stream): shows how per-token decode holds up as
# the KV cache grows. MLA keeps this nearly flat.
run 1 3 32768  128 "LONGCTX 32k"
run 1 2 131072 128 "LONGCTX 128k"

echo "DONE"
