"""Test script for /v1/models endpoint of local server."""

import requests


def test_list_models():
    base_url = "http://localhost:8080"
    resp = requests.get(f"{base_url}/v1/models", timeout=10)
    assert resp.status_code == 200
    data = resp.json()
    assert data.get("object") == "list"
    assert isinstance(data.get("data"), list)
    if data["data"]:
        first = data["data"][0]
        assert "id" in first and "name" in first
