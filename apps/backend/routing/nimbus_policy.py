"""Deterministic, experimental admission policies for one local inference pool.

These functions only select from arrived, pending, *unsent* requests. They do
not acquire capacity, execute adapters, migrate running work, or promise SLOs.
The caller must serialize selection and reservation commit against one shared
pool ledger. Snapshot estimates must be refreshed/released by that owner.

The deliberately simple calibrated model uses aggregate prefill/decode rates,
full prompt plus estimated output as a KV token proxy, and estimated remaining
service seconds as an effective work budget. Cached prefixes reduce prefill,
not the full-context token reservation. This is not a hardware KV model.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Sequence

PolicyName = Literal["all_local", "all_api", "concurrency", "fifo", "greedy", "density", "knapsack"]


def _nonnegative(name: str, value: float) -> None:
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{name} must be finite and non-negative")


def _token_count(name: str, value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")


@dataclass(frozen=True)
class Candidate:
    """Online features; deadline is an absolute first-token deadline in seconds.

    All timestamps must use the same clock as ``now_s``. Output length and
    remote cost must be estimates available at admission, not future trace
    labels. The cost currency is chosen by the caller and must be consistent.
    """

    request_id: str
    arrival_s: float
    deadline_s: float
    prompt_tokens: int
    uncached_prefill_tokens: int
    estimated_output_tokens: int
    estimated_remote_cost: float

    def __post_init__(self) -> None:
        if not self.request_id:
            raise ValueError("request_id must not be empty")
        for name in ("arrival_s", "deadline_s"):
            if not math.isfinite(getattr(self, name)):
                raise ValueError(f"{name} must be finite")
        if self.deadline_s < self.arrival_s:
            raise ValueError("deadline_s must not precede arrival_s")
        for name in ("prompt_tokens", "uncached_prefill_tokens", "estimated_output_tokens"):
            _token_count(name, getattr(self, name))
        if self.uncached_prefill_tokens > self.prompt_tokens:
            raise ValueError("uncached_prefill_tokens must not exceed prompt_tokens")
        _nonnegative("estimated_remote_cost", self.estimated_remote_cost)


@dataclass(frozen=True)
class LocalProfile:
    """Explicit deployment calibration and experimental admission ceilings.

    ``decode_tokens_per_s`` is aggregate throughput, so the TPOT proxy is
    active stream count / decode throughput. The TTFT proxy serializes pending
    prefill work, with an optional fraction of remaining decode work added as
    interference. Neither formula models continuous batching exactly.

    ``starvation_s`` prioritizes individually feasible old requests ahead of
    value optimization. Knapsack enumerates subsets of at most 20 candidates
    in oldest-first chunks; its default window is 16. All arrived requests are
    considered, with reservations carried forward between chunks. This is bounded
    online selection, not an offline workload oracle.
    """

    prefill_tokens_per_s: float
    decode_tokens_per_s: float
    max_running_requests: int
    max_reserved_tokens: int
    max_work_s: float
    ttft_overhead_s: float = 0.0
    decode_interference: float = 0.0
    max_predicted_tpot_s: float | None = None
    starvation_s: float | None = None
    knapsack_window: int = 16

    def __post_init__(self) -> None:
        for name in ("prefill_tokens_per_s", "decode_tokens_per_s", "max_work_s"):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        for name in ("max_running_requests", "max_reserved_tokens"):
            _token_count(name, getattr(self, name))
        for name in ("ttft_overhead_s", "decode_interference"):
            _nonnegative(name, getattr(self, name))
        for name in ("max_predicted_tpot_s", "starvation_s"):
            value = getattr(self, name)
            if value is not None:
                _nonnegative(name, value)
        _token_count("knapsack_window", self.knapsack_window)
        if not 1 <= self.knapsack_window <= 20:
            raise ValueError("knapsack_window must be between 1 and 20")


@dataclass(frozen=True)
class CapacitySnapshot:
    """Unreleased local reservations and estimated remaining work, not telemetry."""

    running_requests: int = 0
    reserved_tokens: int = 0
    remaining_prefill_tokens: int = 0
    remaining_decode_tokens: int = 0

    def __post_init__(self) -> None:
        for name in (
            "running_requests",
            "reserved_tokens",
            "remaining_prefill_tokens",
            "remaining_decode_tokens",
        ):
            _token_count(name, getattr(self, name))


@dataclass(frozen=True)
class AdmissionDecision:
    """A local-admission decision; unselected work is owned by the caller.

    TTFT includes time since arrival. TPOT is the aggregate throughput proxy.
    Predictions for rejected candidates describe appending them after the
    selected batch; future candidates have no latency prediction.
    """

    request_id: str
    selected: bool
    reason: str
    predicted_ttft_s: float | None
    predicted_tpot_s: float | None
    reserved_tokens: int
    work_s: float
    value: float


@dataclass(frozen=True)
class Selection:
    """Selection and a proposed snapshot, without any side effects.

    Dispatch selected IDs in the returned order to preserve the TTFT proxy's
    prefill ordering. ``resulting_snapshot`` is the original snapshot plus
    selected reservations. ``search_truncated`` means the arrived queue required
    multiple bounded chunks, so subsets crossing chunks were not searched.
    """

    selected_ids: tuple[str, ...]
    decisions: tuple[AdmissionDecision, ...]
    resulting_snapshot: CapacitySnapshot
    search_truncated: bool = False


def _work(prefill: int, decode: int, profile: LocalProfile) -> float:
    return prefill / profile.prefill_tokens_per_s + decode / profile.decode_tokens_per_s


def _reserve(snapshot: CapacitySnapshot, candidate: Candidate) -> CapacitySnapshot:
    return CapacitySnapshot(
        running_requests=snapshot.running_requests + 1,
        reserved_tokens=(
            snapshot.reserved_tokens + candidate.prompt_tokens + candidate.estimated_output_tokens
        ),
        remaining_prefill_tokens=(
            snapshot.remaining_prefill_tokens + candidate.uncached_prefill_tokens
        ),
        remaining_decode_tokens=(
            snapshot.remaining_decode_tokens + candidate.estimated_output_tokens
        ),
    )


def _predict(
    candidate: Candidate, snapshot: CapacitySnapshot, profile: LocalProfile, now_s: float
) -> AdmissionDecision:
    ttft = (
        now_s
        - candidate.arrival_s
        + profile.ttft_overhead_s
        + (snapshot.remaining_prefill_tokens + candidate.uncached_prefill_tokens)
        / profile.prefill_tokens_per_s
        + profile.decode_interference
        * snapshot.remaining_decode_tokens
        / profile.decode_tokens_per_s
    )
    return AdmissionDecision(
        request_id=candidate.request_id,
        selected=False,
        reason="",
        predicted_ttft_s=ttft,
        predicted_tpot_s=(snapshot.running_requests + 1) / profile.decode_tokens_per_s,
        reserved_tokens=candidate.prompt_tokens + candidate.estimated_output_tokens,
        work_s=_work(candidate.uncached_prefill_tokens, candidate.estimated_output_tokens, profile),
        value=candidate.estimated_remote_cost,
    )


def _check(
    candidate: Candidate,
    snapshot: CapacitySnapshot,
    profile: LocalProfile,
    now_s: float,
    policy: PolicyName,
) -> str | None:
    if policy == "all_local":
        return None
    updated = _reserve(snapshot, candidate)
    if updated.running_requests > profile.max_running_requests:
        return "concurrency_capacity"
    if policy == "concurrency":
        return None
    if updated.reserved_tokens > profile.max_reserved_tokens:
        return "token_capacity"
    if (
        _work(updated.remaining_prefill_tokens, updated.remaining_decode_tokens, profile)
        > profile.max_work_s
    ):
        return "work_capacity"
    prediction = _predict(candidate, snapshot, profile, now_s)
    assert prediction.predicted_ttft_s is not None
    assert prediction.predicted_tpot_s is not None
    if prediction.predicted_ttft_s > candidate.deadline_s - candidate.arrival_s:
        return "ttft_deadline"
    if (
        profile.max_predicted_tpot_s is not None
        and prediction.predicted_tpot_s > profile.max_predicted_tpot_s
    ):
        return "tpot_capacity"
    return None


def _old(candidate: Candidate, profile: LocalProfile, now_s: float) -> bool:
    return profile.starvation_s is not None and now_s - candidate.arrival_s >= profile.starvation_s


def _knapsack(
    candidates: list[Candidate], snapshot: CapacitySnapshot, profile: LocalProfile, now_s: float
) -> list[Candidate]:
    # Feasible aged work has priority, then optimize the remaining bounded set.
    aged: list[Candidate] = []
    rest: list[Candidate] = []
    state = snapshot
    for candidate in candidates:
        if _old(candidate, profile, now_s):
            if _check(candidate, state, profile, now_s, "knapsack") is None:
                aged.append(candidate)
                state = _reserve(state, candidate)
        else:
            rest.append(candidate)
    rest.sort(
        key=lambda candidate: (candidate.deadline_s, candidate.arrival_s, candidate.request_id)
    )
    best: tuple[Candidate, ...] = ()
    best_value = 0.0
    remaining_value = [0.0] * (len(rest) + 1)
    for index in range(len(rest) - 1, -1, -1):
        remaining_value[index] = remaining_value[index + 1] + rest[index].estimated_remote_cost

    def search(
        index: int, current: CapacitySnapshot, chosen: tuple[Candidate, ...], value: float
    ) -> None:
        nonlocal best, best_value
        upper_value = value + remaining_value[index]
        if upper_value < best_value and not math.isclose(upper_value, best_value):
            return
        if index == len(rest):
            key = (-value, -len(chosen), tuple(candidate.request_id for candidate in chosen))
            best_key = (-best_value, -len(best), tuple(candidate.request_id for candidate in best))
            if key < best_key:
                best, best_value = chosen, value
            return
        candidate = rest[index]
        if _check(candidate, current, profile, now_s, "knapsack") is None:
            search(
                index + 1,
                _reserve(current, candidate),
                (*chosen, candidate),
                value + candidate.estimated_remote_cost,
            )
        search(index + 1, current, chosen, value)

    search(0, state, (), 0.0)
    return [*aged, *best]


def select_requests(
    policy: PolicyName,
    candidates: Sequence[Candidate],
    snapshot: CapacitySnapshot,
    profile: LocalProfile,
    now_s: float,
) -> Selection:
    """Select local work from a caller-owned pending queue, without mutations.

    FIFO/greedy scans arrival order; density uses estimated API savings per
    effective work second. Knapsack searches oldest-first bounded chunks,
    choosing a subset by estimated savings, with earliest-deadline dispatch
    order after aged requests. Equal scores are resolved deterministically.

    ``all_local`` intentionally ignores admission ceilings/deadlines, while
    ``concurrency`` enforces only the stream-count ceiling. Those baselines can
    overload the engine; the experiment runner must bound offered load.
    """
    if policy not in (
        "all_local",
        "all_api",
        "concurrency",
        "fifo",
        "greedy",
        "density",
        "knapsack",
    ):
        raise ValueError(f"unknown policy: {policy}")
    if not math.isfinite(now_s):
        raise ValueError("now_s must be finite")
    if len({candidate.request_id for candidate in candidates}) != len(candidates):
        raise ValueError("candidate request IDs must be unique")
    ordered = sorted(candidates, key=lambda candidate: (candidate.arrival_s, candidate.request_id))
    arrived = [candidate for candidate in ordered if candidate.arrival_s <= now_s]
    selected: list[Candidate] = []
    search_truncated = False
    if policy == "knapsack":
        search_truncated = len(arrived) > profile.knapsack_window
        current = snapshot
        for offset in range(0, len(arrived), profile.knapsack_window):
            chunk = _knapsack(
                arrived[offset : offset + profile.knapsack_window], current, profile, now_s
            )
            selected.extend(chunk)
            for candidate in chunk:
                current = _reserve(current, candidate)
    elif policy != "all_api":
        if policy == "density":

            def density_key(candidate: Candidate) -> tuple[bool, float, float, str]:
                work = _work(
                    candidate.uncached_prefill_tokens, candidate.estimated_output_tokens, profile
                )
                density = candidate.estimated_remote_cost / max(work, 1e-12)
                old = _old(candidate, profile, now_s)
                return (
                    not old,
                    candidate.arrival_s if old else -density,
                    candidate.arrival_s,
                    candidate.request_id,
                )

            arrived.sort(key=density_key)
        current = snapshot
        for candidate in arrived:
            if _check(candidate, current, profile, now_s, policy) is None:
                selected.append(candidate)
                current = _reserve(current, candidate)

    predictions: dict[str, AdmissionDecision] = {}
    current = snapshot
    selected_reason = {
        "all_local": "baseline_all_local",
        "concurrency": "baseline_concurrency",
    }.get(policy, "admitted")
    for candidate in selected:
        prediction = _predict(candidate, current, profile, now_s)
        predictions[candidate.request_id] = replace(
            prediction, selected=True, reason=selected_reason
        )
        current = _reserve(current, candidate)
    final_tpot = current.running_requests / profile.decode_tokens_per_s
    for request_id, prediction in predictions.items():
        predictions[request_id] = replace(prediction, predicted_tpot_s=final_tpot)
    for candidate in ordered:
        if candidate.request_id in predictions:
            continue
        prediction = _predict(candidate, current, profile, now_s)
        if candidate.arrival_s > now_s:
            prediction = replace(prediction, predicted_ttft_s=None, predicted_tpot_s=None)
            reason = "not_arrived"
        elif policy == "all_api":
            reason = "baseline_all_api"
        else:
            reason = _check(candidate, current, profile, now_s, policy) or "not_selected"
        predictions[candidate.request_id] = replace(prediction, reason=reason)
    return Selection(
        selected_ids=tuple(candidate.request_id for candidate in selected),
        decisions=tuple(predictions[candidate.request_id] for candidate in ordered),
        resulting_snapshot=current,
        search_truncated=search_truncated,
    )
