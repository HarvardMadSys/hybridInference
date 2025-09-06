# llama_benchmark/benchmark.py

import time
from urllib.parse import urlparse

import requests

from llama_benchmark.config import API_BASE, HEADERS
from llama_benchmark.get_ping import tcp_ping
from llama_benchmark.logger import log_result
from llama_benchmark.prompt_generator import make_screenshot_prompt

session = requests.Session()


def benchmark_model(
    model_info, prompt_func, prompt_type="default", runs=10, warmup_runs=3, sleep_time=1
):
    model_id = model_info["model_id"]
    provider = model_info["provider"]
    url = f"{API_BASE}/chat/completions"
    latencies = []

    print(f"[{model_id}] Warm-up phase ({warmup_runs} runs)...")
    for _ in range(warmup_runs):
        messages = prompt_func()
        payload = {"model": model_id, "messages": messages, "max_tokens": 1, "temperature": 0.0}
        try:
            session.post(url, headers=HEADERS, json=payload, timeout=10)
        except requests.exceptions.RequestException:
            pass
        time.sleep(sleep_time)

    print(f"[{model_id}] Starting benchmark ({runs} runs)...")
    for i in range(runs):
        if (i + 1) % 10 == 0:
            print(f"[{model_id}] Run {i + 1}/{runs}...")
            # send the screenshot prompt every 10 runs
            payload = {
                "model": model_id,
                "messages": make_screenshot_prompt(),
                "max_tokens": 0,
                "temperature": 0.0,
            }
            try:
                response = session.post(url, headers=HEADERS, json=payload, timeout=10)
                log_result(
                    model_id=model_id,
                    provider=provider,
                    total_seconds_0=None,
                    total_seconds=response.elapsed.total_seconds(),
                    ping_rtt=None,
                    status=response.status_code,
                    response_text=response.text,
                    prompt=make_screenshot_prompt(),
                    prompt_type="screenshot",
                )
            except requests.exceptions.RequestException as e:
                print(f"[{model_id}] Screenshot prompt failed: {e}")
                log_result(
                    model_id=model_id,
                    provider=provider,
                    total_seconds_0=None,
                    total_seconds=None,
                    status="error",
                    response_text=str(e),
                    prompt=make_screenshot_prompt(),
                    prompt_type=prompt_type,
                )
                continue

        messages = prompt_func()
        payload = {"model": model_id, "messages": messages, "max_tokens": 1, "temperature": 0.0}

        try:
            ping_rtt = tcp_ping(urlparse(API_BASE).hostname)  # call right before the request

            start = time.time()
            response = session.post(url, headers=HEADERS, json=payload, timeout=10)
            end = time.time()

            total_latency = end - start
            elapsed_total_seconds = response.elapsed.total_seconds()

            log_result(
                model_id=model_id,
                provider=provider,
                total_seconds_0=total_latency,
                total_seconds=elapsed_total_seconds,
                ping_rtt=ping_rtt,
                status=response.status_code,
                response_text=response.text,
                prompt=messages,
                prompt_type=prompt_type,
            )

            print(
                f"[{model_id}] ({provider}) Run {i + 1}: {total_latency:.3f}s [net: {elapsed_total_seconds:.3f}s] [{prompt_type}]"
            )

        except requests.exceptions.RequestException as e:
            print(f"[{model_id}] Run {i + 1}: Request failed - {e}")
            log_result(
                model_id=model_id,
                provider=provider,
                total_seconds_0=None,
                total_seconds=None,
                status="error",
                response_text=str(e),
                prompt=messages,
                prompt_type=prompt_type,
            )

        time.sleep(sleep_time)

    return latencies
