"""Predictors for learning-augmented online routing.

This package re-exports predictors from experiment.strategies.online.predictors
for backward compatibility.

Uses importlib to avoid circular import issues.
"""

import importlib.util
import sys
from pathlib import Path

# Get the actual source file locations
_predictors_dir = Path(__file__).parent.parent / "strategies" / "online" / "predictors"


def _load_module_directly(module_name: str, file_path: Path):
    """Load a module directly from file without triggering __init__.py chain."""
    if module_name in sys.modules:
        return sys.modules[module_name]

    spec = importlib.util.spec_from_file_location(module_name, file_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


# Load modules directly
_base = _load_module_directly("experiment.predictors.base", _predictors_dir / "base.py")
_ema = _load_module_directly("experiment.predictors.ema", _predictors_dir / "ema.py")
_histogram = _load_module_directly(
    "experiment.predictors.histogram", _predictors_dir / "histogram.py"
)

# Re-export classes
CombinedPredictor = _base.CombinedPredictor
DurationPrediction = _base.DurationPrediction
DurationPredictor = _base.DurationPredictor
OutputTokenPredictor = _base.OutputTokenPredictor
PredictionContext = _base.PredictionContext
QuantilePrediction = _base.QuantilePrediction

EMAOutputPredictor = _ema.EMAOutputPredictor

HistogramDurationPredictor = _histogram.HistogramDurationPredictor
HistogramOutputPredictor = _histogram.HistogramOutputPredictor

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
