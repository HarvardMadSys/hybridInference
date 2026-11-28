# Routing

## Purpose

Routing turns requests into endpoint and provider decisions and owns the
lifecycle and outcomes of those decisions. This guide sets the intended
ownership boundary and lasting contracts for `apps/backend/routing/`; it is not
a full description of the current code.

## Ownership

Routing owns endpoint-selection policy and route-table decisions; the attempt
lifecycle, fallback and hedging policy; routing outcomes, and routing-specific
learning or feedback where an explicit contract allows it; and interaction with
endpoint health through explicit routing contracts.

Routing does not own HTTP request parsing, authentication or account
persistence; provider wire protocols or credentials; persistent request logging
or frontend behavior; or serving-layer state outside a routing contract.

These are the intended boundaries; some current imports and class placements do
not conform yet.

## Local Contracts

These invariants must survive implementation changes:

- A provider-specific event belongs to an attempt, not to the whole request.
  One external request can contain several attempts.
- A provider that was never contacted gets no endpoint-health failure evidence.
  A successful fallback does not make an earlier failed attempt successful.
- Every resource reservation has an explicit owner and a deterministic,
  idempotent release path.
- Once user-visible streaming content has committed an attempt, fallback must
  not silently splice another provider's response into the stream.
- Fixed and RouteWise may use different selection policies while sharing
  compatible attempt-lifecycle contracts.
- Correctness-critical lifecycle state must not depend only on ambient request
  context.
- Serving code consumes typed routing contracts instead of duplicating router
  lifecycle rules in each request surface.

## Work Guidance

> **Under revision.** Routing is in the middle of a substantial architectural
> change. Current file and class placement, duplicated execution paths and
> compatibility shims are not automatically the intended design.

- Do not infer the intended architecture from the current tree alone, or from
  code that only bridges a migration.
- Do not grow a temporary compatibility path into a new extension point without
  an explicit architectural reason.
- Do not reimplement lifecycle logic in a new serving surface or router. Find
  the existing contract first; if there is none, explain why it should be
  introduced.
- Fix a small, isolated defect locally. If a correctness change needs parallel
  edits across routing, serving handlers, request context, logging or health
  handling, first check whether a shared contract or lifecycle boundary is
  missing; make broad edits only when the change explains why the concern
  cannot stay local.
- Do not clean up only to make transitional structure look uniform. Read the
  design record before broad architectural changes, and say when a structure is
  transitional on purpose.

The
[composable hybrid routing design](../../../docs/agents/specs/2026-09-14-composable-hybrid-routing-design.zh.md)
is the agreed contract for routers, leaves and pools, including the opt-in
wiring and deferred work. The root [AGENTS.md](../../../AGENTS.md) summarizes it
in section 6.2, and [Routing Internals](../../../docs/developer/routing-internals.md)
shows the current code with diagrams. A dated record does not by itself make
transitional code a lasting contract.

## Verification

Run the checks that apply, not every suite:

- routing unit tests, including FixedRouter or RouteWise behavior as relevant;
- endpoint-health and circuit tests when health semantics change;
- streaming, fallback or hedging tests when the attempt lifecycle changes;
- serving integration tests when a change crosses the routing/serving boundary;
- the lint and format gates for every changed file.

Record the exact commands and results in the PR.

## Nested guidance

There is no `AGENTS.md` below routing. Do not add one per router package; add
one only when a lasting ownership boundary meets the repository's criteria on
its own.
