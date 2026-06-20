#!/usr/bin/env bash
# run.sh — drive the full MiniMax-M2.7 benchmark pipeline.
#
# Invokes orchestrate.py with the genai-perf venv's Python (which also carries
# pandas, matplotlib, and requests for the aggregate/plot stages) and puts
# genai-perf on PATH. All arguments are forwarded to orchestrate.py.
#
# Examples:
#   bash run.sh                      # full run, both engines
#   bash run.sh --dry-run
#   bash run.sh --engines vllm
#   bash run.sh --rerun sglang_decode_done

set -euo pipefail

GENAI_VENV=/netscratch/juncheng/venvs/genai
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

cd "$HERE"
PATH="$GENAI_VENV/bin:$PATH" "$GENAI_VENV/bin/python" orchestrate.py "$@"
