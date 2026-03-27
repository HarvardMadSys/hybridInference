"""Tests for RouteWise Prometheus metric emission."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from routing.routers import RoutingObservation
from routing.routewise.config import RouteWiseConfig
from routing.routewise.router import RouteWiseRouter


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_METRICS_MODULE = "serving.observability.metrics"


def _make_adapter(
    model_id: str = "test-model",
    subscription_type: str = "api",
    prompt_price: str = "0.001",
    completion_price: str = "0.002",
    endpoint_id: str | None = None,
) -> MagicMock:
    cfg = MagicMock()
    cfg.id = model_id
    cfg.provider = "openai_compat"
    cfg.subscription_type = subscription_type
    cfg.endpoint_id = endpoint_id or f"{model_id}:openai_compat"
    cfg.pricing = {"prompt": prompt_price, "completion": completion_price}
    adapter = MagicMock()
    adapter.config = cfg
    return adapter


@dataclass
class _FakeRouteConfig:
    adapters: list[tuple[Any, float]]


class _FakeFixedRouter:
    def __init__(self) -> None:
        self.routes: dict[str, _FakeRouteConfig] = {}

    def add(self, model_id: str, adapters: list[tuple[Any, float]]) -> None:
        self.routes[model_id] = _FakeRouteConfig(adapters=adapters)


def _build_router(**overrides: Any) -> RouteWiseRouter:
    """Build a minimal RouteWiseRouter with one API adapter."""
    fr = _FakeFixedRouter()
    adapter = _make_adapter()
    fr.add("test-model", [(adapter, 1.0)])
    cfg = RouteWiseConfig(**overrides)
    return RouteWiseRouter(fixed_router=fr, config=cfg)


def _make_obs(**overrides: Any) -> RoutingObservation:
    defaults: dict[str, Any] = {
        "model_id": "test-model",
        "endpoint_id": "test-model:openai_compat",
        "ttft_ms": 100.0,
        "total_latency_ms": 500.0,
        "token_count": 100,
        "success": True,
        "quota_committed": 0.0,
        "prompt_tokens": 50,
        "completion_tokens": 100,
        "selected_tier": "api",
        "sc_committed": False,
        "hedged": False,
        "backup_won": False,
        "lp_status": None,
    }
    defaults.update(overrides)
    return RoutingObservation(**defaults)


# ---------------------------------------------------------------------------
# Tests: _emit_metrics (called from record_observation)
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestRouteWiseMetrics:
    """Verify Prometheus metric emission via _emit_metrics."""

    def test_tier_decision_counter_incremented(self):
        router = _build_router()
        obs = _make_obs(selected_tier="quota")
        with patch(f"{_METRICS_MODULE}.ROUTEWISE_TIER_DECISIONS") as mock_td, \
             patch(f"{_METRICS_MODULE}.ROUTING_STRATEGY_SELECTED") as mock_rs, \
             patch(f"{_METRICS_MODULE}.ROUTEWISE_HEDGE_DECISIONS") as mock_hd, \
             patch(f"{_METRICS_MODULE}.ROUTEWISE_BACKUP_WINS"), \
             patch(f"{_METRICS_MODULE}.ROUTEWISE_LP_STATUS"), \
             patch(f"{_METRICS_MODULE}.normalize_model_label", return_value="test-model"):
            router._emit_metrics(obs)
            mock_td.labels.assert_called_once_with(model="test-model", tier="quota")
            mock_td.labels().inc.assert_called_once()
            mock_rs.labels.assert_called_once_with(model="test-model", strategy="routewise")
            mock_hd.labels.assert_called_with(model="test-model", outcome="no_hedge")

    def test_tier_decisions_all_tiers(self):
        router = _build_router()
        for tier in ("api", "quota", "concurrency"):
            obs = _make_obs(selected_tier=tier)
            with patch(f"{_METRICS_MODULE}.ROUTEWISE_TIER_DECISIONS") as mock_td, \
                 patch(f"{_METRICS_MODULE}.ROUTING_STRATEGY_SELECTED"), \
                 patch(f"{_METRICS_MODULE}.ROUTEWISE_HEDGE_DECISIONS"), \
                 patch(f"{_METRICS_MODULE}.ROUTEWISE_BACKUP_WINS"), \
                 patch(f"{_METRICS_MODULE}.ROUTEWISE_LP_STATUS"), \
                 patch(f"{_METRICS_MODULE}.normalize_model_label", return_value="m"):
                router._emit_metrics(obs)
                mock_td.labels.assert_called_once_with(model="m", tier=tier)

    def test_hedge_decision_counted_hedged(self):
        router = _build_router()
        obs = _make_obs(hedged=True, backup_won=False)
        with patch(f"{_METRICS_MODULE}.ROUTEWISE_TIER_DECISIONS"), \
             patch(f"{_METRICS_MODULE}.ROUTING_STRATEGY_SELECTED"), \
             patch(f"{_METRICS_MODULE}.ROUTEWISE_HEDGE_DECISIONS") as mock_hd, \
             patch(f"{_METRICS_MODULE}.ROUTEWISE_BACKUP_WINS") as mock_bw, \
             patch(f"{_METRICS_MODULE}.ROUTEWISE_LP_STATUS"), \
             patch(f"{_METRICS_MODULE}.normalize_model_label", return_value="m"):
            router._emit_metrics(obs)
            mock_hd.labels.assert_called_with(model="m", outcome="hedged")
            mock_bw.labels().inc.assert_not_called()

    def test_backup_win_counted(self):
        router = _build_router()
        obs = _make_obs(hedged=True, backup_won=True)
        with patch(f"{_METRICS_MODULE}.ROUTEWISE_TIER_DECISIONS"), \
             patch(f"{_METRICS_MODULE}.ROUTING_STRATEGY_SELECTED"), \
             patch(f"{_METRICS_MODULE}.ROUTEWISE_HEDGE_DECISIONS"), \
             patch(f"{_METRICS_MODULE}.ROUTEWISE_BACKUP_WINS") as mock_bw, \
             patch(f"{_METRICS_MODULE}.ROUTEWISE_LP_STATUS"), \
             patch(f"{_METRICS_MODULE}.normalize_model_label", return_value="m"):
            router._emit_metrics(obs)
            mock_bw.labels.assert_called_once_with(model="m")
            mock_bw.labels().inc.assert_called_once()

    def test_lp_status_counted(self):
        router = _build_router()
        obs = _make_obs(lp_status="optimal")
        with patch(f"{_METRICS_MODULE}.ROUTEWISE_TIER_DECISIONS"), \
             patch(f"{_METRICS_MODULE}.ROUTING_STRATEGY_SELECTED"), \
             patch(f"{_METRICS_MODULE}.ROUTEWISE_HEDGE_DECISIONS"), \
             patch(f"{_METRICS_MODULE}.ROUTEWISE_BACKUP_WINS"), \
             patch(f"{_METRICS_MODULE}.ROUTEWISE_LP_STATUS") as mock_lp, \
             patch(f"{_METRICS_MODULE}.normalize_model_label", return_value="m"):
            router._emit_metrics(obs)
            mock_lp.labels.assert_called_once_with(model="m", status="optimal")
            mock_lp.labels().inc.assert_called_once()

    def test_routing_strategy_selected(self):
        router = _build_router()
        obs = _make_obs()
        with patch(f"{_METRICS_MODULE}.ROUTEWISE_TIER_DECISIONS"), \
             patch(f"{_METRICS_MODULE}.ROUTING_STRATEGY_SELECTED") as mock_rs, \
             patch(f"{_METRICS_MODULE}.ROUTEWISE_HEDGE_DECISIONS"), \
             patch(f"{_METRICS_MODULE}.ROUTEWISE_BACKUP_WINS"), \
             patch(f"{_METRICS_MODULE}.ROUTEWISE_LP_STATUS"), \
             patch(f"{_METRICS_MODULE}.normalize_model_label", return_value="m"):
            router._emit_metrics(obs)
            mock_rs.labels.assert_called_once_with(model="m", strategy="routewise")
            mock_rs.labels().inc.assert_called_once()

    def test_no_tier_skips_tier_counter(self):
        """When selected_tier is None, tier counter is not incremented."""
        router = _build_router()
        obs = _make_obs(selected_tier=None)
        with patch(f"{_METRICS_MODULE}.ROUTEWISE_TIER_DECISIONS") as mock_td, \
             patch(f"{_METRICS_MODULE}.ROUTING_STRATEGY_SELECTED"), \
             patch(f"{_METRICS_MODULE}.ROUTEWISE_HEDGE_DECISIONS"), \
             patch(f"{_METRICS_MODULE}.ROUTEWISE_BACKUP_WINS"), \
             patch(f"{_METRICS_MODULE}.ROUTEWISE_LP_STATUS"), \
             patch(f"{_METRICS_MODULE}.normalize_model_label", return_value="m"):
            router._emit_metrics(obs)
            mock_td.labels.assert_not_called()


@pytest.mark.unit
class TestRouteWiseSelectAdapterGauges:
    """Verify gauge emissions in _select_adapter."""

    def test_quota_remaining_gauge_set_on_select(self):
        router = _build_router()
        with patch(f"{_METRICS_MODULE}.ROUTEWISE_QUOTA_REMAINING") as mock_qr, \
             patch(f"{_METRICS_MODULE}.ROUTEWISE_SC_ACTIVE"), \
             patch(f"{_METRICS_MODULE}.ROUTEWISE_VALUE_ESTIMATE") as mock_ve, \
             patch(f"{_METRICS_MODULE}.normalize_model_label", return_value="m"):
            router._select_adapter("test-model", {"prompt_tokens": 100})
            # Router-global gauge: called directly without .labels().
            mock_qr.set.assert_called_once()
            # v_t should be observed (per-model histogram)
            mock_ve.labels.assert_called_with(model="m")
            mock_ve.labels().observe.assert_called_once()

    def test_sc_active_gauge_set(self):
        router = _build_router(concurrency_enabled=True, concurrency_limit=4)
        with patch(f"{_METRICS_MODULE}.ROUTEWISE_QUOTA_REMAINING"), \
             patch(f"{_METRICS_MODULE}.ROUTEWISE_SC_ACTIVE") as mock_sc, \
             patch(f"{_METRICS_MODULE}.ROUTEWISE_VALUE_ESTIMATE"), \
             patch(f"{_METRICS_MODULE}.normalize_model_label", return_value="m"):
            router._select_adapter("test-model", {"prompt_tokens": 100})
            # Router-global gauge: called directly without .labels().
            mock_sc.set.assert_called_once_with(0)

    def test_value_estimate_observed(self):
        router = _build_router()
        with patch(f"{_METRICS_MODULE}.ROUTEWISE_QUOTA_REMAINING"), \
             patch(f"{_METRICS_MODULE}.ROUTEWISE_SC_ACTIVE"), \
             patch(f"{_METRICS_MODULE}.ROUTEWISE_VALUE_ESTIMATE") as mock_ve, \
             patch(f"{_METRICS_MODULE}.normalize_model_label", return_value="m"):
            router._select_adapter("test-model", {"prompt_tokens": 100})
            mock_ve.labels.assert_called_with(model="m")
            # v_t should be a positive float
            args = mock_ve.labels().observe.call_args[0]
            assert args[0] > 0
