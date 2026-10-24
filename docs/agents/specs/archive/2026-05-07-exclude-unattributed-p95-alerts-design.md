# Exclude Unattributed Requests From Per-Provider P95 Alerts

**Date:** 2026-05-07
**Status:** Approved (design)
**Author:** Juncheng Yang (with Kilo)

## Context

The `p95_latency_per_provider` Slack alert currently falls back to `provider="unknown"` when a request-log record has no provider field:

- `apps/backend/serving/observability/alert_rules.py:209`
- `provider = getattr(record, "provider", None) or "unknown"`

That provider value comes from request-scoped context in the request-log middleware:

- `apps/backend/serving/servers/middleware/request_log.py:71`
- `"provider": ctx.get("provider")`

So `provider unknown` in the alert does not represent a real upstream provider. It represents a request that completed without provider attribution in the request-log record.

This makes the alert misleading. The rule is defined as a per-provider latency alert, but it currently includes samples that are not attributable to any provider.

## Goals

- Stop emitting misleading `p95 latency exceeded for provider unknown` alerts.
- Preserve the semantics of the rule as strictly per-provider.
- Keep the fix minimal and low-risk.
- Add regression coverage so missing-provider records do not reintroduce the false alert.

## Non-Goals

- Fixing every request path that can lose provider attribution.
- Changing request logging, routing context propagation, or streaming metadata behavior.
- Introducing a new synthetic provider label such as `unattributed`.
- Changing non-p95 alert rules unless required by the minimal fix.

## Approaches Considered

### 1. Recommended: skip unattributed records in the p95-per-provider rule

Update `P95LatencyRule.on_record()` so that if `record.provider` is missing or falsey, the rule returns early and does not add the sample to any provider window.

Pros:
- Smallest change.
- Restores the intended meaning of the alert.
- Removes false provider identity from alert text and dedupe keys.

Cons:
- Slow unattributed requests no longer contribute to this specific alert.

### 2. Keep alerting, but rename the bucket to `unattributed`

Pros:
- Maintains visibility into slow requests with missing attribution.

Cons:
- Still not a per-provider alert.
- Still produces noisy alerts, just with a different label.
- Does not address the semantic mismatch.

### 3. Broader fix: repair all provider attribution gaps first

Pros:
- Best long-term observability outcome.

Cons:
- Much larger scope.
- Higher risk because routing and streaming attribution paths are subtle.
- Slower to land than needed for the immediate alert issue.

## Design

### 1. Alert rule behavior

Change `P95LatencyRule.on_record()` in `apps/backend/serving/observability/alert_rules.py` as follows:

1. Read `provider = getattr(record, "provider", None)` without fallback.
2. If `provider` is missing or falsey, return immediately.
3. Only create/update a sliding window for records that have a concrete provider.

This keeps the rule aligned with its name and configuration: latency is aggregated only for actual provider-labeled requests.

### 2. Test changes

Replace the current regression test that expects `unknown` fallback behavior:

- `tests/unit/observability/test_alert_rules.py:256-294`
- `test_p95_latency_uses_unknown_when_provider_missing`

with a regression test asserting that missing-provider records do not produce a p95 alert.

The updated test should:

1. Configure the p95 rule with a low enough sample threshold to trigger if records were counted.
2. Feed only records with `provider=None` and high latency values.
3. Assert that `alert_slack` is never awaited.

This verifies both the behavioral change and the rule's new boundary.

## Affected Components

| Component | Change |
|---|---|
| `apps/backend/serving/observability/alert_rules.py` | Skip missing-provider records in `P95LatencyRule` |
| `tests/unit/observability/test_alert_rules.py` | Replace fallback-to-unknown test with no-alert regression |

## Testing

Run targeted tests:

```bash
uv run pytest tests/unit/observability/test_alert_rules.py -q
```

If broader verification is desired during implementation, also run:

```bash
uv run pytest tests/unit/middleware/test_request_log.py -q
```

No formatting or lint-specific changes are expected beyond standard repo checks if touched files require them.

## Risks

- Unattributed slow requests will no longer page via this rule. This is acceptable because the rule is explicitly per-provider, and attributing them to `unknown` is actively misleading.
- If operators still need visibility into unattributed latency, that should be handled by a separate alert or a dedicated attribution fix, not by overloading a per-provider rule.

## Follow-Up

A separate follow-up can investigate why provider attribution is missing for some long-running requests, especially across async/streaming boundaries. That investigation is intentionally out of scope for this change.
