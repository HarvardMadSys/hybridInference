# Routing

## Purpose

Routing turns routing-relevant work into endpoint/provider decisions and owns
the routing lifecycle and outcomes associated with those decisions. This guide
defines the intended ownership boundary and durable contracts for work under
`apps/backend/routing/`; it is not an exhaustive description of the current
implementation.

## Ownership

Routing owns:

- endpoint-selection policy and route-table decisions;
- routing-attempt lifecycle, fallback, and hedging policy;
- routing outcomes and routing-specific learning or feedback where an explicit
  contract permits it;
- endpoint-health interaction through explicit routing contracts.

Routing does not own:

- HTTP request parsing, authentication, or account persistence;
- provider-specific wire-protocol translation or provider credentials;
- persistent API request logging or frontend behavior;
- arbitrary serving-layer state that is not part of a routing contract.

These are intended boundaries, not a claim that every current import or class
placement already conforms to them.

## Local Contracts

The following invariants should survive implementation changes:

- A provider-specific event belongs to an attempt, not to an entire request.
  One external request may contain multiple attempts.
- A provider that was never contacted must not receive endpoint-health failure
  evidence. A successful fallback does not make an earlier failed attempt
  successful.
- Every resource reservation has an explicit owner and a deterministic,
  idempotent release path.
- Once user-visible streaming content commits an attempt, fallback must not
  silently splice another provider response into that stream.
- Fixed and RouteWise may use different selection policies while sharing
  compatible attempt-lifecycle contracts.
- Correctness-critical lifecycle state must not depend solely on ambient
  request context.
- Serving code should consume typed routing contracts rather than duplicate
  router lifecycle rules in each request surface.

## Work Guidance

> **Active architectural revision:** Routing is undergoing substantial
> architectural change. Current file/class placement, duplicated execution
> paths, and compatibility shims are not automatically canonical.

- Do not infer intended architecture solely from the current tree or from code
  that exists only to bridge a migration.
- Do not expand a temporary compatibility path into a new extension point
  without explicit architectural justification.
- Do not reproduce lifecycle logic independently in a new serving surface or
  router. First look for an existing contract; if none exists, explain why the
  missing contract should be introduced.
- A small isolated routing defect may stay local. If a correctness change needs
  parallel edits across routing, serving handlers, request context, logging,
  or health handling, first determine whether a shared contract or lifecycle
  boundary is missing. Broad edits are acceptable only when the change explains
  why the concern cannot be localized.
- Do not perform cleanup merely to make transitional structure look uniform.
  Consult the newest applicable dated spec or plan before broad architectural
  changes, and state when a current structure is intentionally transitional.

### Dated design and implementation records

No single accepted target architecture is established by this pilot. The
following are dated records and retain their historical status; they are not
evergreen replacements for this guide:

- [RouteWise current body router design](../../../docs/agents/specs/2026-05-17-routewise-current-body-router-design.md)
  is a draft proposal for the RouteWise body-router semantics.
- [RouteWise session/cache-aware routing design](../../../docs/agents/specs/2026-06-01-routewise-session-cache-aware-routing-design.md)
  is a draft proposal whose status records a pending paper-boundary decision.
- [Routing config expressiveness plan](../../../docs/agents/plans/2026-05-03-routing-config-expressiveness.md)
  is a staged plan for one configuration migration, not a declaration of the
  routing target architecture.

Check [`docs/agents/specs/`](../../../docs/agents/specs/),
[`docs/agents/plans/`](../../../docs/agents/plans/), and
[`docs/reviews/`](../../../docs/reviews/) for newer or more applicable
records. A review or plan records what was proposed or verified at that time;
it does not by itself promote transitional code to a durable contract.

## Verification

After changing this area, select the applicable checks rather than running an
unrelated ceremony:

- routing unit tests, including FixedRouter or RouteWise behavior as relevant;
- endpoint-health and circuit tests when health semantics change;
- streaming, fallback, or hedging tests when attempt lifecycle changes;
- serving integration tests when a routing/serving boundary is crossed;
- repository lint and format gates for every changed file.

The implementation plan or PR should record the exact commands and results.

## Nested guidance

There are no subsystem-local `AGENTS.md` files below routing at present. Do not
add one for every router package; add a deeper guide only when a durable
ownership boundary independently meets the repository's sparse-guidance
criteria.
