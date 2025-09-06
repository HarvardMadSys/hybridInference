"""External models endpoint test (requires local server)."""

import pytest
import requests

pytestmark = pytest.mark.external


def test_list_models():
    base_url = "http://localhost:8080"
    try:
        requests.get(f"{base_url}/health", timeout=0.2)
    except Exception:
        pytest.skip("Local server not running on :8080")
    resp = requests.get(f"{base_url}/v1/models", timeout=10)
    assert resp.status_code == 200
    data = resp.json()
    assert data.get("object") == "list"
    assert isinstance(data.get("data"), list)
    if data["data"]:
        first = data["data"][0]
        assert "id" in first and "name" in first
