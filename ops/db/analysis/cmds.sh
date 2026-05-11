#!/bin/bash


TRACE=prod
HASH_N=16
python3 /home/juncheng/workspace/hybridInference/ops/db/analysis/tokenize_log_prompts.py \
    -o data/api_logs_export_${TRACE}_token_h${HASH_N}.jsonl --hash-n ${HASH_N} --workers 16 \
    --qwen-trace-format data/api_logs_export_${TRACE}_raw.jsonl

python3 /home/juncheng/workspace/hybridInference/ops/db/analysis/split_api_logs_sessions.py data/api_logs_export_${TRACE}_token_h${HASH_N}.jsonl --overwrite --output-dir per_session_${TRACE}_h${HASH_N} &

for c in 8 16 32 64 128; do 
    python3 ops/db/analysis/interleave_per_session_requests.py --concurrency ${c} --output data/api_logs_export_${TRACE}_h${HASH_N}_c${c}.jsonl data/per_session_${TRACE}_h${HASH_N}
done

