"""Acceptance test for a neutral, overlay-less deployment.

Open-source criterion 3: someone who clones the repo, configures nothing
FreeInference-specific, and supplies a single provider key must get a
working, neutral gateway. This test *is* that criterion, executable.

It runs in a subprocess with a deliberately bare environment (no
DISTRIBUTION_CONFIG_PATH, no SITE_* overrides, DB disabled) so it cannot
inherit the shared suite's FreeInference-flavored test settings, and it
boots the real app through its lifespan rather than inspecting config.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_EXAMPLE_REGISTRY = "config/examples/models.openrouter.yaml"

_SCRIPT = """
import json

from fastapi.testclient import TestClient

from serving.servers.app import create_app

with TestClient(create_app()) as client:
    health = client.get("/health")
    assert health.status_code == 200, health.status_code

    models = client.get("/v1/models")
    assert models.status_code == 200, models.status_code
    served = {m["id"] for m in models.json()["data"]}
    expected = {"llama-3.3-70b", "llama-3.1-8b", "llama-3.1-8b-hybrid"}
    assert served == expected, f"served={sorted(served)}"

    site = client.get("/site-config").json()
    assert site["distribution"]["id"] == "neutral", site
    assert site["site"]["public_base_url"] == "", site

    # Nothing user-visible may leak the distribution's identity.
    blob = json.dumps([models.json(), site, health.json()]).lower()
    for marker in ("freeinference", "harvard", "madsys"):
        assert marker not in blob, f"{marker!r} leaked into a neutral response"

print("NEUTRAL_STARTUP_OK")
"""


def test_neutral_deployment_serves_the_reference_registry() -> None:
    env = {
        # A bare environment on purpose: PATH/HOME only, plus what an
        # operator would actually set. Inheriting os.environ would import
        # the suite's FreeInference-shaped settings and prove nothing.
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "PYTHONPATH": str(_REPO_ROOT / "apps" / "backend"),
        "DB_ENABLED": "false",
        "MODELS_CONFIG_PATH": _EXAMPLE_REGISTRY,
        "OPENROUTER_API_KEY": "sk-or-neutral-startup-dummy",
    }
    proc = subprocess.run(
        [sys.executable, "-c", _SCRIPT],
        capture_output=True,
        text=True,
        cwd=_REPO_ROOT,
        env=env,
        timeout=180,
    )
    assert proc.returncode == 0, f"stdout:\n{proc.stdout[-2000:]}\nstderr:\n{proc.stderr[-3000:]}"
    assert "NEUTRAL_STARTUP_OK" in proc.stdout
