"""Per-model load gate for batch execution.

Batch work is a *scavenger* of spare model capacity, never a first-class
consumer. For each target model the scheduler asks two questions:

1. Are other (non-batch) callers using this model right now?
2. If not, has the batch already filled the model's headroom?

Fresh tokens = ``prompt_tokens - cache_read_tokens``. Request counts would
misread load: one cold 700k prompt occupies a replica for minutes while a cache
hit costs milliseconds.

Two sides of the comparison, deliberately asymmetric:

- **Baseline** is interactive-only -- historical fresh tokens/hour for this
  model at this hour of day, with batch rows excluded. A batch cannot inflate
  its own bar.
- **Recent** is split: interactive (non-batch) recent usage decides whether
  someone else is present, and batch recent usage is capped against the
  baseline so the batch can never fill the model to the point that a real user
  is starved.

The gate reads only ``api_logs``. It is one input to the worker, which also
enforces availability, per-model leases and concurrency.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

DEFAULT_WINDOW_SECONDS = 900
DEFAULT_BASELINE_DAYS = 7
DEFAULT_HEADROOM_RATIO = 0.8

_INTERACTIVE_RECENT_SQL = """
    SELECT COALESCE(SUM(
        GREATEST(COALESCE(prompt_tokens, 0) - COALESCE(cache_read_tokens, 0), 0)
    ), 0) AS fresh
    FROM api_logs
    WHERE timestamp >= NOW() - make_interval(secs => $1)
      AND model_id = $2
      AND batch_job_id IS NULL
"""

_BATCH_RECENT_SQL = """
    SELECT COALESCE(SUM(
        GREATEST(COALESCE(prompt_tokens, 0) - COALESCE(cache_read_tokens, 0), 0)
    ), 0) AS fresh
    FROM api_logs
    WHERE timestamp >= NOW() - make_interval(secs => $1)
      AND model_id = $2
      AND batch_job_id IS NOT NULL
"""

_BASELINE_SQL = """
    SELECT AVG(hourly) AS baseline
    FROM (
        SELECT date_trunc('hour', timestamp) AS bucket,
               SUM(GREATEST(COALESCE(prompt_tokens, 0)
                            - COALESCE(cache_read_tokens, 0), 0)) AS hourly
        FROM api_logs
        WHERE timestamp >= NOW() - make_interval(days => $1)
          AND timestamp < date_trunc('hour', NOW())
          AND model_id = $2
          AND batch_job_id IS NULL
          AND EXTRACT(HOUR FROM timestamp) = EXTRACT(HOUR FROM NOW())
          AND ($3 = false
               OR (EXTRACT(ISODOW FROM timestamp) >= 6)
                  = (EXTRACT(ISODOW FROM NOW()) >= 6))
        GROUP BY bucket
    ) hourly_buckets
"""


@dataclass
class ModelLoad:
    """Recent and baseline fresh-token rates for one model, per hour."""

    interactive_recent_per_hour: float
    batch_recent_per_hour: float
    baseline_per_hour: float

    @property
    def others_present(self) -> bool:
        """True when a non-batch caller used the model in the window."""
        return self.interactive_recent_per_hour > 0


@dataclass
class LoadGate:
    """Reads per-model load from ``api_logs``."""

    window_seconds: int = DEFAULT_WINDOW_SECONDS
    baseline_days: int = DEFAULT_BASELINE_DAYS

    async def model_load(
        self, pool: Any, model_id: str, *, weekend_split: bool = True
    ) -> ModelLoad:
        """Return interactive/batch recent rates and the interactive baseline."""
        async with pool.acquire() as conn:
            interactive = await conn.fetchval(
                _INTERACTIVE_RECENT_SQL, self.window_seconds, model_id
            )
            batch = await conn.fetchval(_BATCH_RECENT_SQL, self.window_seconds, model_id)
            baseline = await conn.fetchval(
                _BASELINE_SQL, self.baseline_days, model_id, weekend_split
            )
        scale = 3600.0 / self.window_seconds
        return ModelLoad(
            interactive_recent_per_hour=float(interactive or 0) * scale,
            batch_recent_per_hour=float(batch or 0) * scale,
            baseline_per_hour=float(baseline or 0),
        )


def batch_may_run(load: ModelLoad, *, headroom_ratio: float = DEFAULT_HEADROOM_RATIO) -> bool:
    """Whether the batch may launch items for this model right now.

    No other callers, and the batch's own recent load still below
    ``headroom_ratio`` of the model's normal load. A zero baseline (no history)
    is treated as idle with no cap.
    """
    if load.others_present:
        return False
    if load.baseline_per_hour <= 0:
        return True
    return load.batch_recent_per_hour < headroom_ratio * load.baseline_per_hour
