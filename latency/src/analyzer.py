import numpy as np


def analyze_latencies(latencies):
    latencies = np.array(latencies)
    return {
        "p50": round(np.percentile(latencies, 50), 2),
        "p95": round(np.percentile(latencies, 95), 2),
        "p99": round(np.percentile(latencies, 99), 2),
        "avg": round(np.mean(latencies), 2),
        "max": round(np.max(latencies), 2),
    }
