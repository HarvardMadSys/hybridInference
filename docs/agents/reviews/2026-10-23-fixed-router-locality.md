# FixedRouter cache-locality selection — contract and regressions

Date: 2026-10-23
Scope: `apps/backend/routing/prefill_load.py`, `apps/backend/routing/routers.py`,
`apps/backend/routing/policies.py`
Status: implemented and locally qualified on this branch. The full investigation
narrative (staging diagnosis, offload-deadline correction, controlled local
experiment, staging plan) is the companion record
[2026-10-22-staging-cache-locality-routing.md](2026-10-22-staging-cache-locality-routing.md),
which lands with the offload-deadlines PR.

## Problem

FixedRouter's weighted draw chooses between candidates on operator weights and
load alone. Two equal-weight vLLM replicas differ enormously for a returning
conversation: one holds the caller's prefix in its radix cache (warm, TTFT
~0.7 s measured on staging), the other must prefill from scratch (TTFT ~1.7 s).
Selection never looked.

## The contract, as implemented

Weights still decide. Locality may *refine* a choice between comparable
candidates; it may not overturn a strong operator preference. Concretely, in
`PrefillLoadTracker.select_index(..., fingerprint, messages)`:

1. The weighted draw runs first and its result is the default; every locality
   path returns the drawn candidate unless a better one qualifies.
2. Ordinary routing admissibility is unchanged: `avoid`, elephant saturation,
   health, fallback and explicit pins all apply before and around locality.
3. A locality alternative must pass, in order:
   - a **weight gate** — at least `LOCALITY_MIN_WEIGHT_SHARE` (0.5) of the
     drawn candidate's weight. A 90/10 split stays on the 90; a 40/60 pair may
     follow the prefix. This is the operator-weight contract: locality never
     overrules a preference stronger than a cache hit is worth.
   - a **backlog gate** — no foreign backlog past `AFFINITY_BACKLOG_CEILING`
     (150 000 tokens), the same judgement `should_keep_affinity` makes for a
     sticky pin: past that the queue costs more than the prefill it saves.
4. Among the survivors the tracker picks the **lowest strict un-cached
   estimate** — strict meaning it charges in full any endpoint whose hint
   belongs to another conversation or whose anchor fails.
5. The survivor must still clear `LOCALITY_MIN_SAVING_TOKENS` (4 096) against
   the drawn candidate. Since no survivor can save more than the cheapest one,
   checking the threshold on the cheapest survivor is equivalent to requiring
   it of every candidate.

Filter-then-select (steps 3 then 4) replaced an earlier
cheapest-then-gate ordering: that version inspected only the single globally
cheapest candidate and, if it failed either gate, handed the conversation back
to a cold replica even when an eligible warm endpoint was available — a model
with three or more ordinary candidates kept sending warm conversations cold.

## Evidence, not vibes

A locality verdict moves a request *and* records affinity for the next turn,
so unlike a load estimate it does not self-correct. It therefore requires:

- a **conversation fingerprint** — no fingerprint, no locality: the draw
  stands. This also keeps one API key's unrelated conversations from
  inheriting each other's prefixes;
- a **prompt anchor** when messages are available — the later prompt must
  *contain* the remembered one (incremental blake2b prefix digest, one pass,
  no copy), which is what distinguishes a continuation from a fork or retry
  sharing only the opening;
- zero weights are handled: `select_index` never divides by a zero weight and
  a zero-weight candidate cannot pass the relative-weight gate in either
  direction.

The hybrid composition (`router_params.hybrid_composition: true`) carries the
same conversation evidence through `FixedPolicy.select_backend`, so the
composition's global draw cannot reintroduce a cache-blind choice.

## Test map (this branch)

`tests/unit/routing/test_prefill_load.py` — tracker level: warm-prefix
preference, weight-preference non-override (90/10), comparable-weight
movement, unrelated conversation on one key, fork/anchor mismatch, no
fingerprint, no caller, zero weights, immaterial saving, backlogged cached
endpoint, cheapest-fails-weight-gate three-endpoint, cheapest-backlogged
three-endpoint, all-candidates-disqualified draw-lock.
`tests/unit/routing/test_session_affinity.py` — router level through
`FixedRouter`, including avoided warm endpoints.
`tests/unit/routing/test_hybrid_composition.py` — composition draw carries
fingerprint/messages.

The three-endpoint regressions where the strongest cache candidate is
disqualified (weight, backlog) while the second-best remains valid both fail
against the pre-filter ordering; the draw-lock test passes against both and
locks required behavior rather than discriminating.

## Verification

On this branch (independent, based on `9b48afd9`): `tests/unit/routing`
passes; `make lint` clean. Combined-stack qualification on the integrating
branch: 6 857 passed, 0 failed across `tests/unit tests/api`.
