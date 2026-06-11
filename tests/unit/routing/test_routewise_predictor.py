"""Tests for RouteWise bucket-mean output-token predictor."""

from __future__ import annotations

import pytest

from routing.routewise.predictor import BucketMeanOutputPredictor, BucketMeanState


@pytest.mark.unit
class TestBucketMeanState:
    def test_mean_tracks_positive_values(self):
        state = BucketMeanState()
        assert state.mean == 0.0

        state.update(100.0)
        state.update(300.0)
        state.update(0.0)
        state.update(-10.0)

        assert state.count == 2
        assert state.mean == pytest.approx(200.0)


@pytest.mark.unit
class TestBucketMeanOutputPredictor:
    def test_cold_start_prediction(self):
        predictor = BucketMeanOutputPredictor(default_output=500.0)

        pred = predictor.predict("some-model", prompt_tokens=512)

        assert pred.tokens == 500.0
        assert pred.source == "default"
        assert pred.bucket == 9
        assert pred.sample_count == 0

    def test_bucket_for_prompt_uses_log2_bucket(self):
        assert BucketMeanOutputPredictor.bucket_for_prompt(0) == 0
        assert BucketMeanOutputPredictor.bucket_for_prompt(1) == 0
        assert BucketMeanOutputPredictor.bucket_for_prompt(2) == 1
        assert BucketMeanOutputPredictor.bucket_for_prompt(3) == 1
        assert BucketMeanOutputPredictor.bucket_for_prompt(4) == 2

    def test_bucket_mean_takes_precedence_when_warm(self):
        predictor = BucketMeanOutputPredictor(
            default_output=500.0,
            min_bucket_samples=2,
            min_model_samples=2,
            min_global_samples=2,
        )

        predictor.update("model-a", prompt_tokens=512, output_tokens=100)
        predictor.update("model-a", prompt_tokens=512, output_tokens=300)
        predictor.update("model-a", prompt_tokens=1024, output_tokens=900)

        pred = predictor.predict("model-a", prompt_tokens=512)

        assert pred.tokens == pytest.approx(200.0)
        assert pred.source == "bucket"
        assert pred.sample_count == 2

    def test_model_fallback_when_bucket_is_cold(self):
        predictor = BucketMeanOutputPredictor(
            default_output=500.0,
            min_bucket_samples=3,
            min_model_samples=2,
            min_global_samples=10,
        )

        predictor.update("model-a", prompt_tokens=512, output_tokens=100)
        predictor.update("model-a", prompt_tokens=1024, output_tokens=300)

        pred = predictor.predict("model-a", prompt_tokens=512)

        assert pred.tokens == pytest.approx(200.0)
        assert pred.source == "model"
        assert pred.sample_count == 2

    def test_global_fallback_when_model_is_cold(self):
        predictor = BucketMeanOutputPredictor(
            default_output=500.0,
            min_bucket_samples=10,
            min_model_samples=10,
            min_global_samples=2,
        )

        predictor.update("model-a", prompt_tokens=512, output_tokens=100)
        predictor.update("model-b", prompt_tokens=512, output_tokens=300)

        pred = predictor.predict("model-c", prompt_tokens=512)

        assert pred.tokens == pytest.approx(200.0)
        assert pred.source == "global"
        assert pred.sample_count == 2

    def test_max_tokens_caps_prediction(self):
        predictor = BucketMeanOutputPredictor(default_output=500.0)

        pred = predictor.predict("model-a", prompt_tokens=512, max_tokens=128)

        assert pred.tokens == 128.0
        assert pred.source == "default"

    def test_legacy_two_argument_update_lands_in_bucket_zero(self):
        predictor = BucketMeanOutputPredictor(
            default_output=500.0,
            min_bucket_samples=1,
            min_model_samples=1,
            min_global_samples=1,
        )

        predictor.update("model-a", 200)

        pred = predictor.predict("model-a", prompt_tokens=1)

        assert pred.tokens == 200.0
        assert pred.source == "bucket"
        assert pred.bucket == 0
