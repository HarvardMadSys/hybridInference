"""Predictors for learning-augmented online routing.

This subpackage provides predictors for estimating request characteristics
at arrival time, enabling learning-augmented online algorithms.

Predictors:
- HistogramOutputPredictor: Dependency-free streaming quantile estimator (default)
- EMAOutputPredictor: Simple EMA baseline (fallback)
- HistogramDurationPredictor: Duration predictor for Stage 2
"""

from experiment.strategies.online.predictors.base import (
    CombinedPredictor,
    DurationPrediction,
    DurationPredictor,
    OutputTokenPredictor,
    PredictionContext,
    QuantilePrediction,
)
from experiment.strategies.online.predictors.ema import EMAOutputPredictor
from experiment.strategies.online.predictors.histogram import (
    HistogramDurationPredictor,
    HistogramOutputPredictor,
)

__all__ = [
    "CombinedPredictor",
    "DurationPrediction",
    "DurationPredictor",
    "EMAOutputPredictor",
    "HistogramDurationPredictor",
    # Implementations
    "HistogramOutputPredictor",
    # Base interfaces
    "OutputTokenPredictor",
    "PredictionContext",
    # Data classes
    "QuantilePrediction",
]
