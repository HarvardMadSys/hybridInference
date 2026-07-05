"""Regression guard for MiniMax M2.x reasoning control in config/models.yaml.

MiniMax M2.x (M2.5, M2.7, and the M2.5-backed ``minimax-fast``) starts reasoning
whenever a ``thinking`` field is *present* on the upstream request — even
``{type: disabled}`` is a no-op that leaves reasoning ON. The only way to get a
clean, non-reasoning answer is to send NO ``thinking`` param at all.

So an M2.x model may still let clients opt IN to reasoning by advertising
``thinking`` in ``supported_params`` — but only if it ALSO sets
``thinking_disable_by_omission: true``, which makes the gateway honor an explicit
disable (and the no-param default) by omitting the param instead of forwarding a
``{type: disabled}`` that would silently keep reasoning on. Without that flag, a
model that advertises ``thinking`` reintroduces the original user-reported bug.

M3 is exempt — ``{type: disabled}`` works there, so it needs neither the flag.

History: ``minimax-m2.5`` shipped with ``thinking`` advertised and later gained a
``default_thinking: {type: disabled}`` that made the default (no-param) path
*reason* instead of answering cleanly. This test prevents a regression.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]

# MiniMax M2.x upstream ids, e.g. "MiniMax-M2.5", "minimax-m2.5",
# "MiniMaxAI/MiniMax-M2.5-TEE", "minimax/minimax-m2.5", "minimax-m2.7".
# Deliberately excludes M3 (M2.x only).
_M2X_PROVIDER_MODEL_ID = re.compile(r"minimax-m2\.\d", re.IGNORECASE)


def _load_models() -> list[dict]:
    return yaml.safe_load((ROOT / "config" / "models.yaml").read_text())["models"]


def _is_m2x_backed(model: dict) -> bool:
    return any(
        _M2X_PROVIDER_MODEL_ID.search(str(route.get("provider_model_id", "")))
        for route in model.get("route", [])
    )


def test_m2x_models_that_advertise_thinking_disable_by_omission() -> None:
    """Any M2.x-backed model exposing ``thinking`` must omit-on-disable."""
    m2x_models = [m for m in _load_models() if _is_m2x_backed(m)]
    # Sanity: the fixture actually covers the models we think it does.
    covered = {m["id"] for m in m2x_models}
    assert {"minimax-m2.5", "minimax-m2.7", "minimax-fast"} <= covered, covered

    for model in m2x_models:
        model_id = model["id"]
        advertises_thinking = "thinking" in model.get("supported_params", [])
        omits_on_disable = model.get("thinking_disable_by_omission", False)
        assert (not advertises_thinking) or omits_on_disable, (
            f"{model_id} is M2.x-backed and advertises 'thinking' but does not set "
            f"thinking_disable_by_omission: true. M2.x reasons on any present "
            f"thinking field, even {{type: disabled}}, so an explicit disable would "
            f"silently keep reasoning on. Either drop 'thinking' from "
            f"supported_params or set the flag so disable is honored by omission."
        )
        # A static disable-shaped default is pointless here (it would be omitted)
        # and misleading — guard against reintroducing the old backfiring config.
        default_thinking = model.get("default_thinking")
        assert default_thinking is None, (
            f"{model_id} sets default_thinking={default_thinking!r}; M2.x models "
            f"default off by omission, so this is redundant and confusing."
        )


def test_minimax_m2x_models_allow_opt_in_thinking() -> None:
    """Lock in the product requirement: M2.5/M2.7 clients can opt in to reasoning."""
    models = {m["id"]: m for m in _load_models()}
    for model_id in ("minimax-m2.5", "minimax-m2.7"):
        model = models[model_id]
        assert "thinking" in model["supported_params"], (model_id, model["supported_params"])
        assert model.get("thinking_disable_by_omission") is True, model_id
