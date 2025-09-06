# llama_benchmark/logger.py

import json
import os
from datetime import datetime


def log_result(
    model_id,
    provider,
    total_seconds_0,
    total_seconds,
    ping_rtt,
    status,
    response_text,
    prompt,
    prompt_type="default",
):
    log_data = {
        "timestamp": datetime.utcnow().isoformat(),
        "model_id": model_id,
        "provider": provider,
        "total_seconds_0": total_seconds_0,
        "total_seconds": total_seconds,
        "ping_rtt": ping_rtt,
        "status": status,
        "prompt_type": prompt_type,
        "prompt": prompt,
        "response": response_text,
    }

    # Sanitize model ID to be filename-safe
    safe_model_id = model_id.replace("/", "_").replace(" ", "_")
    log_dir = "logs"
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, f"{safe_model_id}.jsonl")

    with open(log_path, "a") as f:
        f.write(json.dumps(log_data) + "\n")
