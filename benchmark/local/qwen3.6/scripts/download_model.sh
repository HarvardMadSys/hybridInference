#!/usr/bin/env bash
# download_model.sh — download Qwen3.6-35B-A3B-FP8 weights to the shared
# dataset store. Idempotent (huggingface-cli skips already-present files).
#
# Requires HF_TOKEN environment variable if the repo is gated. Set with:
#   export HF_TOKEN=hf_...

set -euo pipefail

REPO=Qwen/Qwen3.6-35B-A3B-FP8
DEST=/scratch/juncheng/models/Qwen3.6-35B-A3B-FP8

mkdir -p "$DEST"

# Prefer the new `hf` CLI if available; fall back to the older `huggingface-cli`.
if command -v hf >/dev/null 2>&1; then
  hf download "$REPO" --local-dir "$DEST"
elif command -v huggingface-cli >/dev/null 2>&1; then
  huggingface-cli download "$REPO" --local-dir "$DEST"
else
  echo "Neither 'hf' nor 'huggingface-cli' found. Install with: pip install huggingface_hub" >&2
  exit 1
fi

# Sanity: weights file present
if ! ls "$DEST"/*.safetensors >/dev/null 2>&1; then
  echo "No .safetensors file in $DEST after download — repo may be gated." >&2
  echo "Set HF_TOKEN and re-run." >&2
  exit 1
fi
echo "Model downloaded to $DEST"
