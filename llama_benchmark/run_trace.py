#!/usr/bin/env python3
"""Run trace-based load testing. Simple and direct."""

import asyncio
import time
import json
import sys
from pathlib import Path

from serving.config import get_config
from serving.providers.local import LocalProvider
from serving.base import LLMRequest
from client.loader import BurstGPTLoader, SplitwiseLoader
from client.metrics import MetricsCollector


async def run_trace(trace_file="data/BurstGPT_1.csv", 
                    hours=24.0,
                    time_scale=1.0):
    """
    Run a trace. That's it.
    
    Args:
        trace_file: CSV file with timestamps
        hours: How long to run (max)
        time_scale: 1.0 = original speed, 2.0 = 2x faster
    """
    print(f"Running {trace_file} for {hours}h at {time_scale}x speed")
    
    # Setup - no abstractions, just what we need
    config = get_config()
    provider = LocalProvider(config["local"])
    metrics = MetricsCollector()
    
    # Pick loader based on trace format
    if 'splitwise' in trace_file.lower():
        loader = SplitwiseLoader(trace_file, time_scale=time_scale)
    else:
        loader = BurstGPTLoader(trace_file, time_scale=time_scale)
    
    model = "/root/.cache/modelscope/hub/models/LLM-Research/Meta-Llama-3-8B"
    start = time.time()
    max_seconds = hours * 3600
    count = 0
    
    # Metrics collection - simple loop
    vllm_peaks = {'running': 0, 'waiting': 0, 'gpu': 0.0}
    
    async def monitor():
        while True:
            m = await provider.get_metrics()
            if m:
                vllm_peaks['running'] = max(vllm_peaks['running'], m.requests_running)
                vllm_peaks['waiting'] = max(vllm_peaks['waiting'], m.requests_waiting)
                vllm_peaks['gpu'] = max(vllm_peaks['gpu'], m.gpu_cache_usage_perc)
            await asyncio.sleep(10)  # Check every 10s is enough
    
    monitor_task = asyncio.create_task(monitor())
    
    # Process requests
    tasks = []
    async for request in loader.load_requests():
        if time.time() - start > max_seconds:
            break
            
        request.model = model
        count += 1
        
        # Status every 100 requests
        if count % 100 == 0:
            elapsed = (time.time() - start) / 3600
            print(f"[{elapsed:.1f}h] Sent {count} requests")
        
        # Send request
        async def handle(req, req_id):
            start_t = time.time()
            metrics.record_request_start(req_id, "local", req.model, len(req.prompt.split()))
            try:
                resp = await provider.generate(req)
                metrics.record_request_end(
                    req_id, 
                    len(resp.text.split()),
                    (time.time() - start_t) * 1000,
                    True
                )
            except Exception as e:
                metrics.record_request_end(
                    req_id, 0,
                    (time.time() - start_t) * 1000,
                    False, str(e)
                )
        
        task = asyncio.create_task(handle(request, f"r{count}"))
        tasks.append(task)
    
    # Wait for completion
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    
    monitor_task.cancel()
    
    # Results - just the facts
    duration = time.time() - start
    summary = metrics.get_summary()
    provider_metrics = metrics.get_provider_metrics("local")
    
    results = {
        'duration_hours': duration / 3600,
        'requests': summary['total_requests'],
        'success_rate': summary['successful_requests'] / max(1, summary['total_requests']),
        'latency_avg_ms': provider_metrics.avg_latency_ms if provider_metrics else 0,
        'latency_p99_ms': provider_metrics.p99_latency_ms if provider_metrics else 0,
        'vllm_peak_concurrent': vllm_peaks['running'],
        'vllm_peak_waiting': vllm_peaks['waiting'],
        'vllm_peak_gpu_pct': vllm_peaks['gpu']
    }
    
    # Print summary
    print(f"\n{'='*40}")
    print(f"Duration: {results['duration_hours']:.1f}h")
    print(f"Requests: {results['requests']} ({results['success_rate']*100:.1f}% success)")
    print(f"Latency: {results['latency_avg_ms']:.0f}ms avg, {results['latency_p99_ms']:.0f}ms p99")
    print(f"vLLM peaks: {results['vllm_peak_concurrent']} running, {results['vllm_peak_gpu_pct']:.2f}% GPU")
    
    # Save to file
    output = Path(f"trace_{int(time.time())}.json")
    output.write_text(json.dumps(results, indent=2))
    print(f"Saved to {output}")
    

if __name__ == "__main__":
    # Parse args without argparse bloat
    trace = "data/BurstGPT_1.csv"
    hours = 24.0
    scale = 1.0
    
    if len(sys.argv) > 1:
        trace = sys.argv[1]
    if len(sys.argv) > 2:
        hours = float(sys.argv[2])
    if len(sys.argv) > 3:
        scale = float(sys.argv[3])
    
    print(f"Usage: {sys.argv[0]} [trace_file] [hours=24] [time_scale=1.0]")
    asyncio.run(run_trace(trace_file=trace, hours=hours, time_scale=scale))