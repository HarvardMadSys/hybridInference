"""Unit-test level bootstrap to stub optional dependencies at import time.

Some packages (e.g., serving.__init__ importing serving.config) depend on
third-party modules optional in CI. We stub them here to avoid import-time
failures while focusing on pure-unit tests that don't need their behavior.

Stub only when the real import fails. Whatever this file puts in
``sys.modules`` replaces the module for the whole process, not just for
``tests/unit``: a stub installed because nothing had imported the package
*yet* breaks every later test that needs the real one, depending on which
paths a run happens to list first. aiohttp is a hard dependency, so it has
no stub.
"""

from __future__ import annotations

import importlib
import sys
from types import SimpleNamespace

# Stub python-dotenv only when the real package is unavailable.
if "dotenv" not in sys.modules:  # pragma: no cover - import-time shim
    try:
        sys.modules["dotenv"] = importlib.import_module("dotenv")
    except ImportError:
        sys.modules["dotenv"] = SimpleNamespace(
            load_dotenv=lambda *a, **k: None,
            dotenv_values=lambda *a, **k: {},
        )
