"""EMA output-token predictor for production RouteWise routing.

Provides per-model exponential moving average tracking of output token counts
with normal-approximation quantile predictions.  The predictor falls back from
per-model state to a global aggregate, and finally to a configurable cold-start
default when no observations are available.

This module is independent of ``experiment/`` -- the algorithm is reimplemented
here for production use without importing simulation code.
"""

from __future__ import annotations

import math
from collections import defaultdict, deque
from dataclasses import dataclass

from serving.utils.logging import get_logger

logger = get_logger(__name__)

# z-score for 10th / 90th percentile of the standard normal distribution.
# Uses 1.28 (matching the simulation reference implementation) rather than
# the full-precision 1.2816 so that replay tests achieve exact equivalence.
_Z_10 = 1.28


@dataclass
class QuantilePrediction:
    """Quantile prediction for output token count.

    Attributes:
        q10: 10th percentile (conservative lower bound).
        q50: 50th percentile (median / point estimate).
        q90: 90th percentile (upper bound).
        is_warmed_up: Whether the underlying state has enough samples.
    """

    q10: float
    q50: float
    q90: float
    is_warmed_up: bool = True

    @property
    def lcb(self) -> float:
        """Lower confidence bound (alias for q10)."""
        return self.q10

    @property
    def median(self) -> float:
        """Median estimate (alias for q50)."""
        return self.q50

    @property
    def ucb(self) -> float:
        """Upper confidence bound (alias for q90)."""
        return self.q90


@dataclass
class EMAState:
    """Online EMA tracking state for a single stream of observations.

    Attributes:
        mean: Exponential moving average of observed values.
        variance: EMA of squared deviation (Welford-style).
        count: Total number of observations ingested.
    """

    mean: float = 0.0
    variance: float = 0.0
    count: int = 0

    def update(self, value: float, alpha: float) -> None:
        """Incorporate a new observation.

        On the first sample the mean is set directly; subsequent samples use
        exponential smoothing for both mean and variance.

        Args:
            value: Observed output token count.
            alpha: EMA smoothing factor (0 < alpha <= 1).
        """
        if self.count == 0:
            self.mean = value
            self.variance = 0.0
        else:
            delta = value - self.mean
            self.mean = self.mean + alpha * delta
            # Welford-style online variance with EMA weighting.
            self.variance = (1 - alpha) * (self.variance + alpha * delta * delta)
        self.count += 1

    @property
    def std(self) -> float:
        """Standard deviation estimate derived from EMA variance."""
        return math.sqrt(max(self.variance, 0.0))

    def predict(self, min_samples: int) -> QuantilePrediction:
        """Produce a quantile prediction from the current state.

        Uses a normal approximation:
        ``q10 = mean - 1.28*std``, ``q50 = mean``, ``q90 = mean + 1.28*std``.

        Args:
            min_samples: Minimum observation count to consider warmed up.

        Returns:
            A ``QuantilePrediction`` with the ``is_warmed_up`` flag set
            according to whether *count >= min_samples*.
        """
        std = max(self.std, self.mean * 0.1) if self.mean > 0 else self.std
        q10 = max(1.0, self.mean - _Z_10 * std)
        q50 = max(1.0, self.mean)
        q90 = max(q50, self.mean + _Z_10 * std)
        return QuantilePrediction(
            q10=q10,
            q50=q50,
            q90=q90,
            is_warmed_up=self.count >= min_samples,
        )


class EMAOutputPredictor:
    """Production EMA output-token predictor.

    Maintains per-model and global EMA states.  When predicting:

    1. If the per-model state is warmed up, use it.
    2. Else if the global state is warmed up, use that.
    3. Else return a cold-start default.

    Args:
        alpha: EMA smoothing factor (higher = more weight on recent).
        min_samples: Minimum samples before the **global** state is warmed up.
        min_samples_per_model: Minimum samples before a **per-model** state is
            warmed up.  Defaults to 10 (matching the simulation), lower than
            the global threshold because a single model accumulates homogeneous
            data faster.
        default_output: Default output-token prediction for cold start.
    """

    def __init__(
        self,
        alpha: float = 0.1,
        min_samples: int = 20,
        min_samples_per_model: int = 10,
        default_output: float = 500.0,
    ) -> None:
        self._alpha = alpha
        self._min_samples = min_samples
        self._min_samples_per_model = min_samples_per_model
        self._default_output = default_output
        self._model_states: dict[str, EMAState] = defaultdict(EMAState)
        self._global_state: EMAState = EMAState()

    def predict(self, model_id: str) -> QuantilePrediction:
        """Predict output-token quantiles for *model_id*.

        Args:
            model_id: The model identifier (e.g. ``"gpt-4o"``).

        Returns:
            Quantile prediction with warmup status.
        """
        # Per-model state preferred when it has enough samples.
        if model_id in self._model_states:
            state = self._model_states[model_id]
            if state.count >= self._min_samples_per_model:
                return state.predict(self._min_samples_per_model)

        # Fall back to global aggregate.
        if self._global_state.count >= self._min_samples:
            return self._global_state.predict(self._min_samples)

        # Cold start: return default-based prediction.
        return QuantilePrediction(
            q10=self._default_output * 0.3,
            q50=self._default_output,
            q90=self._default_output * 2.0,
            is_warmed_up=False,
        )

    def update(self, model_id: str, output_tokens: int) -> None:
        """Record an observed completion length.

        Updates both the per-model and global EMA states.

        Args:
            model_id: Model identifier.
            output_tokens: Number of completion tokens observed.
        """
        if output_tokens <= 0:
            return
        value = float(output_tokens)
        self._model_states[model_id].update(value, self._alpha)
        self._global_state.update(value, self._alpha)


class HistogramOutputPredictor:
    """Bucketed rolling-window output-token predictor.

    Buckets are based on prompt token count.  Predictions use the bucket mean
    as the stable value estimate while still returning empirical q10/q50/q90
    for callers that need conservative bounds.
    """

    def __init__(
        self,
        min_samples: int = 20,
        default_output: float = 500.0,
        max_samples_per_bucket: int = 1000,
    ) -> None:
        self._min_samples = min_samples
        self._default_output = default_output
        self._max_samples_per_bucket = max_samples_per_bucket
        self._buckets: dict[tuple[str, int], deque[float]] = defaultdict(
            lambda: deque(maxlen=max_samples_per_bucket)
        )
        self._global_samples: deque[float] = deque(maxlen=max_samples_per_bucket)

    def _bucket_for_prompt(self, prompt_tokens: int) -> int:
        """Return a stable power-of-two prompt-size bucket."""
        return max(1, int(prompt_tokens)).bit_length() - 1

    def update(self, model_id: str, prompt_tokens: int, output_tokens: int) -> None:
        """Record observed completion tokens for a prompt-size bucket."""
        if output_tokens <= 0:
            return
        value = float(output_tokens)
        bucket = self._bucket_for_prompt(prompt_tokens)
        self._buckets[(model_id, bucket)].append(value)
        self._global_samples.append(value)

    def predict(self, model_id: str, prompt_tokens: int) -> QuantilePrediction:
        """Predict output-token quantiles for a model and prompt size."""
        bucket = self._bucket_for_prompt(prompt_tokens)
        samples = list(self._buckets.get((model_id, bucket), ()))
        is_warmed = len(samples) >= self._min_samples
        if not is_warmed:
            samples = list(self._global_samples)
            is_warmed = len(samples) >= self._min_samples
        if not is_warmed:
            return QuantilePrediction(
                q10=self._default_output * 0.3,
                q50=self._default_output,
                q90=self._default_output * 2.0,
                is_warmed_up=False,
            )

        sorted_samples = sorted(samples)
        mean = sum(sorted_samples) / len(sorted_samples)
        return QuantilePrediction(
            q10=_empirical_quantile(sorted_samples, 0.10),
            q50=mean,
            q90=_empirical_quantile(sorted_samples, 0.90),
            is_warmed_up=True,
        )


def _empirical_quantile(sorted_samples: list[float], q: float) -> float:
    """Return linearly interpolated empirical quantile from sorted samples."""
    if not sorted_samples:
        return 0.0
    idx = q * (len(sorted_samples) - 1)
    lower = math.floor(idx)
    upper = min(lower + 1, len(sorted_samples) - 1)
    frac = idx - lower
    return sorted_samples[lower] * (1.0 - frac) + sorted_samples[upper] * frac
