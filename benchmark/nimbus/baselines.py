"""Prototype greedy and a bounded rolling-knapsack experiment on identical constraints.

Greedy ports ``realtmxi/nimbus:router/greedy.py`` and the slot/shared-prefill
predictor in ``router/nimbus.py``. The exact inspected working-copy digests are
below: greedy.py was untracked, so the repository HEAD alone is not provenance.
The port keeps fixed-cap, no-cache, block-rounded KV and TTFT-minus-guard rules.
It deliberately does not introduce a TPOT gate or an aggregate-work constraint.
TPOT remains an independently measured outcome, not a guaranteed bound.

``rolling_knapsack_v1`` is a new experimental baseline, not Glen's algorithm or
the prototype's FLOP knapsack. It maximizes estimated API cost saved in a bounded
arrived window under the same greedy feasibility checks and FIFO dispatch order.
Larger cohorts return deferred requests for another immediate window; they are
not automatically offloaded. The optimum is only within that window.

This module owns no live reservations or execution. Its caller commits the
returned snapshot atomically, dispatches accepted waiting work through real
sequence slots, and retains peak KV reservations until the terminal event.
"""

from __future__ import annotations

import heapq
import math
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Sequence

SOURCE_PROVENANCE = {
    "repository": "realtmxi/nimbus",
    "head": "13694272a41a10c9cf2eb6984a16b10115016adc",
    "inspected_date": "2026-10-04",
    "files": {
        "router/greedy.py": {
            "sha256": "335e5bcd44465eff7a4fb90e19f117626d41bd4828fac98164b9d68684217eb9",
            "working_copy_status": "untracked",
        },
        "router/nimbus.py": {
            "sha256": "c339f12e91ddce1c8cab2bc4443291cb16f9f02cbae2072ccab3a6662b7286b4",
            "working_copy_status": "tracked_clean",
        },
        "router/common.py": {
            "sha256": "f0be3543490a7cd94c098079dbc82ee971e1779bf6a9753b7acd23ba62041d40",
            "working_copy_status": "tracked_clean",
        },
    },
}

BaselinePolicy = Literal["greedy", "rolling_knapsack_v1", "all_local", "all_api", "concurrency"]


def _finite(value: float, name: str) -> None:
    if isinstance(value, bool) or not math.isfinite(value):
        raise ValueError(f"{name} must be finite")


def _integer(value: int, name: str, minimum: int = 0) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")


@dataclass(frozen=True)
class BaselineProfile:
    """The original greedy calibration, capacity, generation cap and guard.

    ``tpot_s`` is the predictor's fixed per-stream decode time, not aggregate
    throughput or a TPOT feasibility ceiling. Use one explicit generation cap
    on both local and remote endpoints; output labels are never inputs here.
    """

    kv_capacity_tokens: float
    max_tokens: int
    prefill_tput: float
    tpot_s: float
    first_token_overhead_s: float
    slo_s: float
    ttft_guard_s: float
    kv_block_size: int = 16

    def __post_init__(self) -> None:
        for name in (
            "kv_capacity_tokens",
            "prefill_tput",
            "tpot_s",
            "first_token_overhead_s",
            "slo_s",
            "ttft_guard_s",
        ):
            _finite(getattr(self, name), name)
        if min(self.kv_capacity_tokens, self.prefill_tput, self.slo_s) <= 0:
            raise ValueError("capacity, prefill_tput and slo_s must be positive")
        if min(self.tpot_s, self.first_token_overhead_s) < 0:
            raise ValueError("service times must be non-negative")
        if not 0 <= self.ttft_guard_s < self.slo_s:
            raise ValueError("ttft_guard_s must be in [0, slo_s)")
        _integer(self.max_tokens, "max_tokens", 1)
        _integer(self.kv_block_size, "kv_block_size", 1)

    @property
    def ttft_budget_s(self) -> float:
        """Return the source baseline's first-token admission budget."""
        return self.slo_s - self.ttft_guard_s


@dataclass(frozen=True)
class BaselineRequest:
    """Caller-known features with no cache hint or actual output-length field.

    ``prompt_tokens`` must represent the full tokenizer-aligned prompt for an
    exact reproduction. If a caller uses an approximation, its experiment must
    explicitly report that deviation. ``estimated_remote_cost`` uses only
    currently known input and the fixed cap, in one consistent currency.
    """

    request_id: str
    arrival_s: float
    prompt_tokens: int
    estimated_remote_cost: float = 0.0

    def __post_init__(self) -> None:
        if not isinstance(self.request_id, str) or not self.request_id:
            raise ValueError("request_id must be a nonempty string")
        _finite(self.arrival_s, "arrival_s")
        _integer(self.prompt_tokens, "prompt_tokens")
        _finite(self.estimated_remote_cost, "estimated_remote_cost")
        if self.estimated_remote_cost < 0:
            raise ValueError("estimated_remote_cost must be non-negative")


@dataclass(frozen=True)
class InflightPrefill:
    """A sent request with no first token, in dispatch order.

    Keep its full no-cache prompt work until the first token is observed, as
    in the source predictor. ``decode_tokens`` is the known cap, not a future
    output label. The first output token is included in this count.
    ``slot_release_floor_s`` retains an uncertain cancellation's slot for at
    least this relative duration without removing its shared prefill work.
    Its default zero preserves the original predictor.
    """

    prompt_tokens: int
    decode_tokens: int
    slot_release_floor_s: float = 0.0

    def __post_init__(self) -> None:
        _integer(self.prompt_tokens, "prompt_tokens")
        _integer(self.decode_tokens, "decode_tokens")
        _finite(self.slot_release_floor_s, "slot_release_floor_s")
        if self.slot_release_floor_s < 0:
            raise ValueError("slot_release_floor_s must be non-negative")


@dataclass(frozen=True)
class BaselineSnapshot:
    """Already accepted local work; the caller owns lifecycle and synchronization.

    ``reserved_tokens`` includes active AND accepted waiting peak KV commitments.
    ``waiting`` contains accepted, unsent requests in FIFO dispatch order.
    ``inflight_remaining_s`` contains relative slot-release estimates for sent
    requests already decoding. ``inflight_prefills`` contains the other sent
    requests; never count one request in both collections. Waiting requests may
    exceed ``max_inflight``: greedy allows slot waiting when predicted TTFT fits.
    """

    reserved_tokens: int = 0
    waiting: tuple[BaselineRequest, ...] = ()
    inflight_remaining_s: tuple[float, ...] = ()
    max_inflight: int = 1
    inflight_prefills: tuple[InflightPrefill, ...] = ()

    def __post_init__(self) -> None:
        _integer(self.reserved_tokens, "reserved_tokens")
        _integer(self.max_inflight, "max_inflight", 1)
        for remaining in self.inflight_remaining_s:
            _finite(remaining, "inflight_remaining_s")


@dataclass(frozen=True)
class BaselineDecision:
    """One admission result, with the exact features needed for an audit."""

    request_id: str
    selected: bool
    reason: str
    predicted_ttft_s: float | None
    request_commitment_tokens: int
    reserved_tokens_before: int
    reserved_tokens_after: int
    estimated_remote_cost: float


@dataclass(frozen=True)
class BaselineSelection:
    """Pure selection; ``deferred_ids`` must remain undecided for the next window.

    Future requests receive ``not_arrived`` and are never in ``deferred_ids``.
    Selected IDs and the appended waiting queue retain FIFO order. Commit the
    snapshot before processing deferred candidates in the same flush. There is
    no claim that sequential windows optimize a whole larger cohort jointly.
    """

    policy: BaselinePolicy
    selected_ids: tuple[str, ...]
    decisions: tuple[BaselineDecision, ...]
    resulting_snapshot: BaselineSnapshot
    deferred_ids: tuple[str, ...]
    window_ids: tuple[str, ...]
    window_truncated: bool
    saved_cost: float


def request_commitment_tokens(request: BaselineRequest, profile: BaselineProfile) -> int:
    """Round full prompt plus fixed generation cap up to a KV block."""
    peak_tokens = request.prompt_tokens + profile.max_tokens
    return (
        (peak_tokens + profile.kv_block_size - 1) // profile.kv_block_size * profile.kv_block_size
    )


def predict_waiting_ttfts(
    waiting: Sequence[BaselineRequest],
    snapshot: BaselineSnapshot,
    profile: BaselineProfile,
    now_s: float,
) -> tuple[float, ...]:
    """Port the source slot/shared-prefill predictor with fixed-cap full prompts.

    ``waiting`` is the complete FIFO queue to simulate, normally the snapshot's
    waiting queue followed by proposed admissions. Existing active work is
    accounted for only through the snapshot's two disjoint in-flight fields.
    The source's handling of oversubscribed snapshots and negative slot-release
    estimates is preserved; it is not a substitute for runtime slot enforcement.
    """
    _finite(now_s, "now_s")
    slots = [max(0.0, float(value)) for value in snapshot.inflight_remaining_s]
    prefill_ready_s = 0.0
    for state in snapshot.inflight_prefills:
        prefill_ready_s += float(state.prompt_tokens) / max(profile.prefill_tput, 1e-9)
        heapq.heappush(
            slots,
            max(
                prefill_ready_s
                + profile.first_token_overhead_s
                + max(0, state.decode_tokens - 1) * profile.tpot_s,
                state.slot_release_floor_s,
            ),
        )
    slot_count = max(snapshot.max_inflight, len(slots))
    slots.extend([0.0] * (slot_count - len(slots)))
    heapq.heapify(slots)
    predictions = []
    for request in waiting:
        slot_ready_s = heapq.heappop(slots)
        prefill_s = request.prompt_tokens / max(profile.prefill_tput, 1e-9)
        prefill_done_s = max(slot_ready_s, prefill_ready_s) + prefill_s
        age_s = max(0.0, now_s - request.arrival_s)
        predictions.append(age_s + prefill_done_s + profile.first_token_overhead_s)
        heapq.heappush(
            slots,
            prefill_done_s
            + profile.first_token_overhead_s
            + max(0, profile.max_tokens - 1) * profile.tpot_s,
        )
        prefill_ready_s = prefill_done_s
    return tuple(predictions)


def _check(
    request: BaselineRequest,
    snapshot: BaselineSnapshot,
    profile: BaselineProfile,
    now_s: float,
) -> tuple[str, float | None]:
    if (
        snapshot.reserved_tokens + request_commitment_tokens(request, profile)
        > profile.kv_capacity_tokens
    ):
        return "kv_capacity", None
    ttft = predict_waiting_ttfts((*snapshot.waiting, request), snapshot, profile, now_s)[-1]
    return ("local_feasible" if ttft <= profile.ttft_budget_s else "ttft_budget"), ttft


def _append(
    snapshot: BaselineSnapshot, request: BaselineRequest, profile: BaselineProfile
) -> BaselineSnapshot:
    return replace(
        snapshot,
        reserved_tokens=snapshot.reserved_tokens + request_commitment_tokens(request, profile),
        waiting=(*snapshot.waiting, request),
    )


def _best_subset(
    candidates: Sequence[BaselineRequest],
    snapshot: BaselineSnapshot,
    profile: BaselineProfile,
    now_s: float,
) -> tuple[int, ...]:
    best: tuple[int, ...] = ()
    best_key: tuple[float, int, tuple[int, ...]] = (0.0, 0, ())

    def search(index: int, current: BaselineSnapshot, chosen: tuple[int, ...]) -> None:
        nonlocal best, best_key
        upper_value = math.fsum(
            candidates[i].estimated_remote_cost for i in (*chosen, *range(index, len(candidates)))
        )
        max_count = len(chosen) + len(candidates) - index
        if upper_value < -best_key[0] or (upper_value == -best_key[0] and max_count < -best_key[1]):
            return
        if index == len(candidates):
            value = math.fsum(candidates[i].estimated_remote_cost for i in chosen)
            key = (-value, -len(chosen), chosen)
            if key < best_key:
                best, best_key = chosen, key
            return
        candidate = candidates[index]
        if _check(candidate, current, profile, now_s)[0] == "local_feasible":
            search(index + 1, _append(current, candidate, profile), (*chosen, index))
        search(index + 1, current, chosen)

    search(0, snapshot, ())
    return best


def select_baseline(
    policy: BaselinePolicy,
    candidates: Sequence[BaselineRequest],
    snapshot: BaselineSnapshot,
    profile: BaselineProfile,
    now_s: float,
    *,
    max_candidates: int = 12,
) -> BaselineSelection:
    """Select arrived work without inspecting future arrivals or output labels.

    Greedy reproduces source arrival-order decisions at the supplied snapshot
    time. Equal arrivals preserve input order. Knapsack searches at most
    ``max_candidates`` (1..16), maximizing savings, then request count, then
    earliest input positions. It never reorders the selected FIFO work.
    The default remains 12: exact search is synchronous and even bounded
    contention at 16 can noticeably delay the caller's event loop.

    Control policies are explicit: all_local ignores admission limits, all_api
    selects none, and concurrency checks only active plus waiting request count.
    All local execution still requires the caller's real sequence-slot limiter.
    """
    if policy not in ("greedy", "rolling_knapsack_v1", "all_local", "all_api", "concurrency"):
        raise ValueError(f"unknown baseline policy: {policy}")
    _finite(now_s, "now_s")
    _integer(max_candidates, "max_candidates", 1)
    if max_candidates > 16:
        raise ValueError("max_candidates must be <= 16")
    all_requests = [*snapshot.waiting, *candidates]
    if len({request.request_id for request in all_requests}) != len(all_requests):
        raise ValueError("request IDs must be unique across waiting and candidates")
    if any(request.arrival_s > now_s for request in snapshot.waiting):
        raise ValueError("accepted waiting requests must already have arrived")
    waiting_tokens = sum(
        request_commitment_tokens(request, profile) for request in snapshot.waiting
    )
    if snapshot.reserved_tokens < waiting_tokens:
        raise ValueError("reserved_tokens must include accepted waiting commitments")
    ordered = sorted(candidates, key=lambda request: request.arrival_s)
    arrived = [request for request in ordered if request.arrival_s <= now_s]
    deferred = arrived[max_candidates:] if policy == "rolling_knapsack_v1" else []
    window = arrived[:max_candidates] if policy == "rolling_knapsack_v1" else arrived
    chosen = (
        set(_best_subset(window, snapshot, profile, now_s))
        if policy == "rolling_knapsack_v1"
        else set()
    )
    current = snapshot
    selected = []
    decisions = []
    for index, request in enumerate(window):
        before = current.reserved_tokens
        prediction = None
        if policy == "all_api":
            reason, keep = "baseline_all_api", False
        elif policy == "all_local":
            reason, keep = "baseline_all_local", True
        elif policy == "concurrency":
            active = len(current.inflight_remaining_s) + len(current.inflight_prefills)
            keep = active + len(current.waiting) < current.max_inflight
            reason = "baseline_concurrency" if keep else "concurrency_capacity"
        else:
            reason, prediction = _check(request, current, profile, now_s)
            keep = (
                index in chosen if policy == "rolling_knapsack_v1" else reason == "local_feasible"
            )
            if not keep and reason == "local_feasible":
                reason = "not_selected"
        if keep:
            selected.append(request)
            current = _append(current, request, profile)
        decisions.append(
            BaselineDecision(
                request.request_id,
                keep,
                reason,
                prediction,
                request_commitment_tokens(request, profile),
                before,
                current.reserved_tokens,
                request.estimated_remote_cost,
            )
        )
    deferred_ids = {request.request_id for request in deferred}
    for request in ordered[len(window) :]:
        reason = "deferred_window" if request.request_id in deferred_ids else "not_arrived"
        decisions.append(
            BaselineDecision(
                request.request_id,
                False,
                reason,
                None,
                request_commitment_tokens(request, profile),
                current.reserved_tokens,
                current.reserved_tokens,
                request.estimated_remote_cost,
            )
        )
    return BaselineSelection(
        policy=policy,
        selected_ids=tuple(request.request_id for request in selected),
        decisions=tuple(decisions),
        resulting_snapshot=current,
        deferred_ids=tuple(request.request_id for request in deferred),
        window_ids=tuple(request.request_id for request in window),
        window_truncated=bool(deferred),
        saved_cost=math.fsum(request.estimated_remote_cost for request in selected),
    )
