"""The backend must import (and wire routers) without the RouteWise package.

RouteWise is a required dependency, so this never happens in a correct
install. The contract exists so that a partial install degrades to a clear
configuration error instead of an import crash at boot.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]

# Blocks the *external* llm_routewise distribution only; the in-repo
# routing.routewise wrappers then fail organically through their own imports,
# exactly as they would in an environment missing the dependency.
_SCRIPT = """
import sys


class _BlockRouteWise:
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "llm_routewise" or fullname.startswith("llm_routewise."):
            raise ImportError("blocked: llm_routewise package")
        return None


sys.meta_path.insert(0, _BlockRouteWise())

import serving.servers.bootstrap  # noqa: F401  (boot path must import)
from serving.servers.routers.admin import router as admin_router  # noqa: F401
from serving.servers.routewise_compat import ROUTEWISE_AVAILABLE, RouteWiseRouter
from serving.servers.routewise_compat import model_routewise_setting_keys

assert ROUTEWISE_AVAILABLE is False
assert not isinstance(object(), RouteWiseRouter)
assert model_routewise_setting_keys("any-model") == ()

from routing.strategies import build_router

try:
    build_router("routewise", {})
except ValueError:
    pass
else:
    raise SystemExit("routewise strategy built without the package")

print("NEUTRAL_IMPORT_OK")
"""


def test_backend_imports_without_the_routewise_package() -> None:
    env = {**os.environ, "PYTHONPATH": str(_REPO_ROOT / "apps" / "backend")}
    proc = subprocess.run(
        [sys.executable, "-c", _SCRIPT],
        capture_output=True,
        text=True,
        cwd=_REPO_ROOT,
        env=env,
        timeout=120,
    )
    assert proc.returncode == 0, f"stderr tail:\n{proc.stderr[-3000:]}"
    assert "NEUTRAL_IMPORT_OK" in proc.stdout
