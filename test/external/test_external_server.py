#!/usr/bin/env python3
"""External server tests (health, models, completions) against local 8080."""

import json

import pytest
import requests

pytestmark = pytest.mark.external


def _ensure_up(base_url: str) -> None:
    try:
        requests.get(f"{base_url}/health", timeout=0.2)
    except Exception:
        pytest.skip("Local server not running on :8080")


def test_health_check():
    base_url = "http://localhost:8080"
    _ensure_up(base_url)
    r = requests.get(f"{base_url}/health", timeout=2)
    assert r.status_code == 200


def test_models_endpoint():
    base_url = "http://localhost:8080"
    _ensure_up(base_url)
    r = requests.get(f"{base_url}/v1/models", timeout=10)
    assert r.status_code == 200
    data = r.json()
    assert "data" in data


def test_non_streaming_completion():
    base_url = "http://localhost:8080"
    _ensure_up(base_url)
    # Pick a model after listing; fallback to a generic id
    models = requests.get(f"{base_url}/v1/models", timeout=5).json().get("data", [])
    model_id = models[0]["id"] if models else "llama-4-scout"
    payload = {
        "model": model_id,
        "messages": [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Hello! Please respond with 'Hello, World!'"},
        ],
        "max_tokens": 32,
        "temperature": 0.1,
    }
    r = requests.post(f"{base_url}/v1/chat/completions", json=payload, timeout=15)
    assert r.status_code == 200
    data = r.json()
    assert "choices" in data


def test_streaming_completion():
    base_url = "http://localhost:8080"
    _ensure_up(base_url)
    models = requests.get(f"{base_url}/v1/models", timeout=5).json().get("data", [])
    model_id = models[0]["id"] if models else "llama-4-scout"
    payload = {
        "model": model_id,
        "messages": [{"role": "user", "content": "Stream a short reply."}],
        "max_tokens": 64,
        "temperature": 0.1,
        "stream": True,
    }
    r = requests.post(f"{base_url}/v1/chat/completions", json=payload, stream=True, timeout=30)
    assert r.status_code == 200
    chunk_count = 0
    for line in r.iter_lines():
        if not line:
            continue
        s = line.decode("utf-8")
        if s.startswith("data: "):
            body = s[6:].strip()
            if body == "[DONE]":
                break
            try:
                json.loads(body)
                chunk_count += 1
            except json.JSONDecodeError:
                continue
    assert chunk_count > 0
