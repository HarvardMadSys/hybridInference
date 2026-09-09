"""Load explicitly configured, trusted local backend extensions at startup."""

from __future__ import annotations

import importlib
import inspect
import logging
import os
import re

logger = logging.getLogger(__name__)
_MODULE_NAME_RE = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", re.ASCII)
_loaded_modules: set[str] = set()


def load_backend_extensions() -> None:
    """Import BACKEND_EXTENSIONS modules and call each synchronous register once.

    The comma-separated list is deployment configuration, not model config or
    request input. Extensions execute trusted Python code with backend access.
    Configured import or registration failures abort startup; never fall back
    to a different adapter silently. Extensions must eagerly import their own
    runtime code here before the deployment's checkout can change.
    """
    names = [
        name.strip() for name in os.getenv("BACKEND_EXTENSIONS", "").split(",") if name.strip()
    ]
    if len(set(names)) != len(names):
        raise RuntimeError("BACKEND_EXTENSIONS contains duplicate module names")
    for name in names:
        if not _MODULE_NAME_RE.fullmatch(name):
            raise RuntimeError(f"Invalid backend extension module name: {name!r}")
        if name in _loaded_modules:
            continue
        try:
            module = importlib.import_module(name)
            register = getattr(module, "register", None)
            if not callable(register) or inspect.iscoroutinefunction(register):
                raise TypeError("Backend extension must expose a synchronous register()")
            result = register()
            if inspect.isawaitable(result):
                if inspect.iscoroutine(result):
                    result.close()
                raise TypeError("Backend extension register() must not return an awaitable")
        except Exception as exc:
            raise RuntimeError(f"Failed to load backend extension {name!r}") from exc
        _loaded_modules.add(name)
        logger.info("Loaded backend extension %s", name)
