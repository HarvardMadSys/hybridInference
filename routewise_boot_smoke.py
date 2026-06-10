"""RouteWise boot smoke.

Construct the production ``RouteWiseRouter`` from the real ``config/models.yaml``
(no mocks) and dump routing decisions for the ``minimax-fast`` route, which
carries all three RouteWise provider categories (S_Q Chutes quota,
S_C Featherless C=1, P_O OpenRouter on-demand).

This is a read-only sanity check: it exercises config -> candidate -> effective
cost -> LP -> selection, but never issues a network request. Run from the repo
root:

    uv run python routewise_boot_smoke.py
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent
MODELS_YAML = REPO / "config" / "models.yaml"
MODEL_ID = "minimax-fast"

# Offline placeholder creds so the env-gated route registration succeeds without
# real keys. No network request is ever issued; selection logic only reads
# pricing/quota/concurrency config, not the keys. setdefault keeps any real env.
for _var in (
    "CHUTES_BASE_URL",
    "FEATHERLESS_BASE_URL",
):
    os.environ.setdefault(_var, "https://smoke.invalid/v1")
for _var in (
    "CHUTES_API_KEY",
    "FEATHERLESS_API_KEY",
    "OPENROUTER_API_KEY",
):
    os.environ.setdefault(_var, "smoke-offline-dummy")

# Quiet the per-model registry warnings for the other (key-less) models.
logging.getLogger("serving.servers.registry").setLevel(logging.ERROR)

from routing.routers import FixedRouter, RoutingObservation
from routing.strategies import build_router

# Per-provider-category synthetic TTFT (ms) used only to warm latency profiles
# so the LP has distinguishable inputs. Concurrency fastest, on-demand mid,
# quota slowest.
TTFT_MS_BY_PROVIDER_TYPE = {"quota": 4000.0, "concurrency": 800.0, "on_demand": 1500.0}


def build_fixed_router() -> tuple[FixedRouter, int]:
    """Populate a real FixedRouter from config/models.yaml."""
    from serving.servers.registry import register_from_models_yaml

    fr = FixedRouter()
    count, _infos = register_from_models_yaml(fr, MODELS_YAML, continue_on_missing_env=True)
    return fr, count


def routewise_params() -> dict:
    """Read the real router_params for MODEL_ID, asserting it is routewise."""
    data = yaml.safe_load(MODELS_YAML.read_text()) or {}
    for m in data.get("models", []):
        if m.get("id") == MODEL_ID:
            if m.get("router") != "routewise":
                raise SystemExit(f"{MODEL_ID} router is {m.get('router')!r}, not routewise")
            return m.get("router_params") or {}
    raise SystemExit(f"{MODEL_ID} not found in {MODELS_YAML}")


def make_router(fr: FixedRouter):
    """Build the RouteWiseRouter exactly as ModelRouterRegistry would."""
    params = routewise_params()
    router = build_router("routewise", params)  # validates RouteWiseParams(extra=forbid)
    router.attach_fixed_router(fr)
    return router


def inject_quota_snapshot(router, *, used: float = 500.0, limit: float = 5000.0):
    """Simulate a refreshed provider quota snapshot so the S_Q leg participates.

    Offline there is no Chutes usage API, so the refresh loop never populates
    the store and quota stays dark. We poke the store directly (diagnostic only)
    to prove the three-provider-category path fires.
    """
    from datetime import datetime, timezone

    from routing.routewise.quota_snapshot import ProviderQuotaSnapshot

    for cand in router.route_candidates[MODEL_ID]:
        if cand.provider_type.value == "quota" and cand.quota_source is not None:
            qs = cand.quota_source
            router.quota_snapshots._snapshots[qs] = ProviderQuotaSnapshot(
                source=qs,
                used=used,
                limit=limit,
                reset_at=None,
                fetched_at=datetime.now(timezone.utc),
            )
            return qs
    return None


def warm(router) -> None:
    """Replay realistic request records so latency + envelope calibrate."""
    rng_prompts = [240, 600, 1100, 1800, 3200, 512, 900, 1500]
    for cand in router.route_candidates[MODEL_ID]:
        provider_type = cand.provider_type.value
        ttft = TTFT_MS_BY_PROVIDER_TYPE.get(provider_type, 1500.0)
        for i in range(16):
            prompt = rng_prompts[i % len(rng_prompts)]
            router.record_observation(
                RoutingObservation(
                    model_id=MODEL_ID,
                    endpoint_id=cand.endpoint_id,
                    ttft_ms=ttft,
                    total_latency_ms=ttft + 600.0,
                    token_count=512,
                    success=True,
                    quota_committed=0.0,
                    prompt_tokens=prompt,
                    completion_tokens=512,
                )
            )


def dump(
    router,
    label: str,
    *,
    inject_quota: bool = False,
    saturate_concurrency: bool = False,
) -> None:
    """Print per-candidate cost/TTFT and the resulting routing decision."""
    if inject_quota:
        inject_quota_snapshot(router)
    if saturate_concurrency:
        for conc_pool in router.concurrency_pools.values():
            for _ in range(conc_pool.limit):
                conc_pool.try_acquire()

    pool = router._routewise_pool(MODEL_ID)
    envelope = router.envelope.snapshot(pool)
    ctx = {"prompt_tokens": 1000, "request_id": f"smoke-{label}"}
    prediction = router._predict_output(MODEL_ID, 1000, ctx)

    candidates, _prefix_context = router._build_candidates(
        MODEL_ID,
        prompt_tokens=1000,
        predicted_output_tokens=prediction.tokens,
        envelope=envelope,
        now=time.time(),
        context=ctx,
    )

    print(f"\n=== {label} ===")
    L = getattr(envelope, "lower", getattr(envelope, "L", None))
    U = getattr(envelope, "upper", getattr(envelope, "U", None))
    print(f"  envelope L/U = {L} / {U}   predicted_out_tokens = {prediction.tokens}")
    print(f"  feasible candidates ({len(candidates)}):")
    print(f"    {'endpoint_id':<34} {'provider_type':>14} {'eff_cost_usd':>14} {'mean_ttft_s':>12}")
    for c in candidates:
        print(
            f"    {c.endpoint_id:<34} {c.provider_type:>14} "
            f"{c.effective_cost_usd:>14.8f} {c.mean_ttft_sec:>12.3f}"
        )

    selected = router._select_adapter(MODEL_ID, ctx)
    sel_ep = router._candidate_endpoint_id(selected) if selected is not None else None
    print(f"  LP status  = {router._last_lp_statuses.get(MODEL_ID)}")
    print(f"  LP weights = {router._last_lp_weights.get(MODEL_ID)}")
    print(f"  -> selected endpoint = {sel_ep}")


def main() -> None:
    """Run the RouteWise boot smoke check."""
    print("RouteWise boot smoke: building from real config/models.yaml ...")
    fr, count = build_fixed_router()
    print(f"  register_from_models_yaml: {count} route ids registered")
    if MODEL_ID not in fr.routes:
        raise SystemExit(f"FixedRouter has no route for {MODEL_ID}; got {sorted(fr.routes)}")

    legs = [(a.config.__dict__.get("endpoint_id", "?")) for a, _w in fr.routes[MODEL_ID].adapters]
    print(f"  {MODEL_ID} legs: {legs}")

    cold = make_router(fr)
    print(
        f"  RouteWiseRouter built; budget_alpha = {cold.config.budget_alpha}, "
        f"quota_pools = {sorted(cold.quota_pools)}, "
        f"concurrency_pools = {sorted(cold.concurrency_pools)}"
    )
    dump(cold, "COLD (no traffic; quota dark: envelope uncalibrated)")

    warmed = make_router(fr)
    warm(warmed)
    dump(warmed, "WARM (16 records/leg; quota dark: no provider snapshot)")

    with_quota = make_router(fr)
    warm(with_quota)
    dump(
        with_quota,
        "WARM + quota snapshot (all provider categories feasible)",
        inject_quota=True,
    )

    spill = make_router(fr)
    warm(spill)
    dump(
        spill,
        "WARM + quota snapshot + S_C saturated (spill to S_Q/S_A)",
        inject_quota=True,
        saturate_concurrency=True,
    )

    print("\nboot smoke OK: constructed from real config and produced decisions.")


if __name__ == "__main__":
    main()
