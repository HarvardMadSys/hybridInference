"""Output-token predictor for production RouteWise routing.

The RouteWise body router uses a bucket-mean predictor keyed by
``(model_id, log2(prompt_tokens))``.

This module is independent of ``experiment/`` -- the algorithm is reimplemented
here for production use without importing simulation code.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass


@dataclass
class BucketMeanState:
    """Simple running mean for one output-length bucket."""

    total: float = 0.0
    count: int = 0

    @property
    def mean(self) -> float:
        """Return the current arithmetic mean, or zero before observations."""
        if self.count <= 0:
            return 0.0
        return self.total / self.count

    def update(self, value: float) -> None:
        """Add one positive observation to the running mean."""
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

        ``output_tokens`` is optional for legacy two-argument call sites.  When
        omitted, the observation updates the model/global fallback means and
        lands in bucket 0.
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
