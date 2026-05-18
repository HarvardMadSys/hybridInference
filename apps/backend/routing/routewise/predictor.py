"""Output-token predictors for production RouteWise routing.

The current RouteWise body router uses a bucket-mean predictor keyed by
``(model_id, log2(prompt_tokens))``.  The older EMA predictor remains exported
for compatibility with tests and any code that still imports it directly, but
``RouteWiseRouter`` no longer uses EMA quantiles for routing decisions.

This module is independent of ``experiment/`` -- the algorithm is reimplemented
here for production use without importing simulation code.
"""

from __future__ import annotations

import math
from collections import defaultdict
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


@dataclass
class BucketMeanState:
    """Simple running mean for one output-length bucket."""

    total: float = 0.0
    count: int = 0

    @property
    def mean(self) -> float:
        if self.count <= 0:
            return 0.0
        return self.total / self.count

    def update(self, value: float) -> None:
        if value <= 0:
            return
        self.total += value
        self.count += 1


@dataclass(frozen=True)
class BucketMeanPrediction:
    """Point prediction from the bucket-mean output predictor."""

    tokens: float
    source: str
    bucket: int
    sample_count: int


class BucketMeanOutputPredictor:
    """Bucket-mean output-token predictor.

    Fallback order:
    1. mean for ``(model_id, log2_bucket(prompt_tokens))``
    2. model-level mean
    3. global mean
    4. configured cold-start default
    """

    def __init__(
        self,
        *,
        default_output: float = 512.0,
        min_bucket_samples: int = 3,
        min_model_samples: int = 3,
        min_global_samples: int = 3,
    ) -> None:
        self._default_output = max(float(default_output), 1.0)
        self._min_bucket_samples = max(int(min_bucket_samples), 1)
        self._min_model_samples = max(int(min_model_samples), 1)
        self._min_global_samples = max(int(min_global_samples), 1)
        self._bucket_states: dict[tuple[str, int], BucketMeanState] = defaultdict(BucketMeanState)
        self._model_states: dict[str, BucketMeanState] = defaultdict(BucketMeanState)
        self._global_state: BucketMeanState = BucketMeanState()

    @staticmethod
    def bucket_for_prompt(prompt_tokens: int | float) -> int:
        """Return an integer log2 prompt-length bucket."""
        tokens = max(int(prompt_tokens or 0), 1)
        return tokens.bit_length() - 1

    def predict(
        self,
        model_id: str,
        prompt_tokens: int | float,
        *,
        max_tokens: int | float | None = None,
    ) -> BucketMeanPrediction:
        """Predict completion tokens for a request."""
        bucket = self.bucket_for_prompt(prompt_tokens)
        key = (model_id, bucket)
        state = self._bucket_states.get(key)
        if state is not None and state.count >= self._min_bucket_samples:
            value = state.mean
            source = "bucket"
            count = state.count
        else:
            model_state = self._model_states.get(model_id)
            if model_state is not None and model_state.count >= self._min_model_samples:
                value = model_state.mean
                source = "model"
                count = model_state.count
            elif self._global_state.count >= self._min_global_samples:
                value = self._global_state.mean
                source = "global"
                count = self._global_state.count
            else:
                value = self._default_output
                source = "default"
                count = 0

        if max_tokens is not None:
            try:
                cap = float(max_tokens)
            except (TypeError, ValueError):
                cap = 0.0
            if cap > 0:
                value = min(value, cap)
        return BucketMeanPrediction(
            tokens=max(value, 1.0),
            source=source,
            bucket=bucket,
            sample_count=count,
        )

    def update(
        self,
        model_id: str,
        prompt_tokens: int | float,
        output_tokens: int | float | None = None,
    ) -> None:
        """Record one observed completion length.

        ``output_tokens`` is optional for compatibility with the old
        ``EMAOutputPredictor.update(model_id, output_tokens)`` call shape.
        When omitted, the observation updates the model/global fallback means
        and lands in bucket 0.
        """
        if output_tokens is None:
            output_tokens = prompt_tokens
            prompt_tokens = 0
        try:
            value = float(output_tokens)
        except (TypeError, ValueError):
            return
        if value <= 0:
            return
        bucket = self.bucket_for_prompt(prompt_tokens)
        self._bucket_states[(model_id, bucket)].update(value)
        self._model_states[model_id].update(value)
        self._global_state.update(value)
