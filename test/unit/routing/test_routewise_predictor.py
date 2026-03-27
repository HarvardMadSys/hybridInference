"""Tests for RouteWise EMA output-token predictor."""

from __future__ import annotations

import pytest

from routing.routewise.predictor import EMAOutputPredictor, EMAState, QuantilePrediction


@pytest.mark.unit
class TestEMAState:

    def test_first_update_sets_mean(self):
        """First observation sets the mean directly."""
        state = EMAState()
        state.update(100.0, alpha=0.1)
        assert state.mean == 100.0
        assert state.variance == 0.0
        assert state.count == 1

    def test_subsequent_update_smooths(self):
        """Subsequent updates apply EMA smoothing."""
        state = EMAState()
        state.update(100.0, alpha=0.1)
        state.update(200.0, alpha=0.1)
        # mean = 100 + 0.1 * (200 - 100) = 110
        assert state.mean == pytest.approx(110.0)
        assert state.count == 2

    def test_variance_tracking(self):
        """Variance increases with varied inputs."""
        state = EMAState()
        state.update(100.0, alpha=0.1)
        assert state.variance == 0.0

        state.update(200.0, alpha=0.1)
        assert state.variance > 0.0

        # Feed highly variable data.
        for v in [50.0, 300.0, 80.0, 250.0]:
            state.update(v, alpha=0.1)
        assert state.variance > 0.0
        assert state.std > 0.0

    def test_predict_quantile_ordering(self):
        """q10 < q50 < q90 when variance exists."""
        state = EMAState()
        for v in [100.0, 200.0, 150.0, 120.0, 180.0]:
            state.update(v, alpha=0.2)

        pred = state.predict(min_samples=3)
        assert pred.q10 < pred.q50 < pred.q90

    def test_predict_warmup_flag(self):
        """is_warmed_up reflects whether count >= min_samples."""
        state = EMAState()
        for i in range(5):
            state.update(float(100 + i), alpha=0.1)

        pred_cold = state.predict(min_samples=10)
        assert pred_cold.is_warmed_up is False

        for i in range(5, 10):
            state.update(float(100 + i), alpha=0.1)

        pred_warm = state.predict(min_samples=10)
        assert pred_warm.is_warmed_up is True


@pytest.mark.unit
class TestQuantilePrediction:

    def test_aliases(self):
        """lcb, median, ucb are aliases for q10, q50, q90."""
        pred = QuantilePrediction(q10=10.0, q50=50.0, q90=90.0, is_warmed_up=True)
        assert pred.lcb == 10.0
        assert pred.median == 50.0
        assert pred.ucb == 90.0


@pytest.mark.unit
class TestEMAOutputPredictor:

    def test_cold_start_prediction(self):
        """Returns default-based prediction before any updates."""
        predictor = EMAOutputPredictor(default_output=500.0)
        pred = predictor.predict("some-model")
        assert pred.q50 == 500.0
        assert pred.is_warmed_up is False
        # q10 should be < q50 and q90 > q50.
        assert pred.q10 < pred.q50
        assert pred.q90 > pred.q50

    def test_single_update(self):
        """After a single update, per-model state is set but not warmed up."""
        predictor = EMAOutputPredictor(min_samples=5)
        predictor.update("model-a", 200)

        # Not warmed up yet (1 < 5), so falls to global, also not warmed up.
        pred = predictor.predict("model-a")
        assert pred.is_warmed_up is False

    def test_warmup_transition(self):
        """is_warmed_up flips after min_samples updates."""
        predictor = EMAOutputPredictor(min_samples=5, default_output=100.0)
        for i in range(4):
            predictor.update("model-a", 100 + i)
            assert predictor.predict("model-a").is_warmed_up is False

        predictor.update("model-a", 104)
        pred = predictor.predict("model-a")
        assert pred.is_warmed_up is True

    def test_per_model_vs_global(self):
        """Per-model state is used when warmed, global as fallback."""
        predictor = EMAOutputPredictor(min_samples=3, default_output=500.0)

        # Warm up model-a with high values.
        for _ in range(5):
            predictor.update("model-a", 1000)

        # model-a should use per-model state.
        pred_a = predictor.predict("model-a")
        assert pred_a.is_warmed_up is True
        assert pred_a.q50 == pytest.approx(1000.0, rel=0.1)

        # model-b has no data but global is warmed, so uses global.
        pred_b = predictor.predict("model-b")
        assert pred_b.is_warmed_up is True
        # Global was trained on model-a's data (1000).
        assert pred_b.q50 == pytest.approx(1000.0, rel=0.1)

    def test_quantile_ordering(self):
        """q10 < q50 < q90 once predictor has variance."""
        predictor = EMAOutputPredictor(min_samples=3)
        for v in [100, 200, 300, 150, 250]:
            predictor.update("test-model", v)

        pred = predictor.predict("test-model")
        assert pred.q10 < pred.q50 < pred.q90

    def test_zero_output_tokens_ignored(self):
        """update() with output_tokens <= 0 is a no-op."""
        predictor = EMAOutputPredictor()
        predictor.update("m", 0)
        predictor.update("m", -10)
        # Cold start still in effect.
        pred = predictor.predict("m")
        assert pred.is_warmed_up is False
